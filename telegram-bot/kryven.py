"""Kryven chat backend for Ruslan Helper."""

import os

import requests

try:
    from grok import SYSTEM_PROMPT
except Exception:
    SYSTEM_PROMPT = (
        "Ты — Ruslan Helper, умный персональный ассистент Руслана. "
        "Отвечай на русском языке: спокойно, по делу, без воды. "
        "Не проси пароли, коды, токены или другие чувствительные данные."
    )


KRYVEN_API_KEY = os.environ.get("KRYVEN_API_KEY", "").strip()
KRYVEN_BASE_URL = os.environ.get("KRYVEN_BASE_URL", "https://kryven.cc").rstrip("/")
KRYVEN_MODEL = os.environ.get("KRYVEN_MODEL", "").strip()
DEFAULT_MAX_TOKENS = int(os.environ.get("KRYVEN_MAX_TOKENS", "2200"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("KRYVEN_TIMEOUT_SECONDS", "90"))
HISTORY_WINDOW = 20


def kryven_available() -> bool:
    return bool(KRYVEN_API_KEY)


def _extract_content(data) -> str:
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message") or {}
        if isinstance(message, dict) and message.get("content"):
            return str(message["content"]).strip()
        if first.get("text"):
            return str(first["text"]).strip()

    for key in ("content", "response", "answer", "text"):
        if data.get(key):
            return str(data[key]).strip()

    return ""


def _call_api(messages: list, max_tokens: int | None = None) -> str:
    if not KRYVEN_API_KEY:
        return "❌ Kryven не настроен — добавь KRYVEN_API_KEY в Railway Variables."

    payload = {
        "messages": messages,
        "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
        "temperature": 0.5,
    }
    if KRYVEN_MODEL:
        payload["model"] = KRYVEN_MODEL

    try:
        resp = requests.post(
            f"{KRYVEN_BASE_URL}/v1/apichat",
            headers={
                "Authorization": f"Bearer {KRYVEN_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        if resp.status_code == 401:
            return "❌ Неверный API ключ Kryven."
        if resp.status_code == 429:
            return "⏳ Лимит запросов Kryven. Подожди минуту и повтори."
        if resp.status_code != 200:
            return f"❌ Ошибка Kryven ({resp.status_code}): {resp.text[:300]}"

        data = resp.json()
        content = _extract_content(data)
        return content or "❌ Kryven ответил пустым сообщением."
    except requests.exceptions.Timeout:
        return "⏳ Kryven не ответил вовремя. Попробуй ещё раз."
    except Exception as e:
        return f"❌ Ошибка Kryven: {str(e)[:200]}"


def ask_kryven(user_message: str, history: list = None, memory_block: str = "") -> str:
    system_content = SYSTEM_PROMPT + ("\n" + memory_block if memory_block else "")
    messages = [{"role": "system", "content": system_content}]

    if history:
        window = history[-HISTORY_WINDOW:]
        valid = [
            m for m in window
            if isinstance(m, dict) and "role" in m and "content" in m
        ]
        messages.extend(valid)

    messages.append({"role": "user", "content": str(user_message)})
    return _call_api(messages)
