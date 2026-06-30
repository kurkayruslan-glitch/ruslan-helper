"""Kryven chat backend for Ruslan Helper."""

import os
import time

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
KRYVEN_BASE_URL = os.environ.get("KRYVEN_BASE_URL", "https://kryven.cc/v1").rstrip("/")
KRYVEN_MODEL = os.environ.get("KRYVEN_MODEL", "kryven-extended").strip()
DEFAULT_MAX_TOKENS = int(os.environ.get("KRYVEN_MAX_TOKENS", "2200"))
DIALOG_ANALYSIS_MAX_TOKENS = int(os.environ.get("KRYVEN_DIALOG_ANALYSIS_MAX_TOKENS", "5200"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("KRYVEN_TIMEOUT_SECONDS", "90"))
DIALOG_ANALYSIS_TIMEOUT_SECONDS = int(os.environ.get("KRYVEN_DIALOG_ANALYSIS_TIMEOUT_SECONDS", "240"))
HISTORY_WINDOW = 20


def kryven_available() -> bool:
    return bool(KRYVEN_API_KEY)


def _chat_completions_url() -> str:
    """Return an OpenAI-compatible chat completions URL for Kryven."""
    custom_path = os.environ.get("KRYVEN_API_PATH", "").strip()
    if custom_path:
        path = custom_path if custom_path.startswith("/") else "/" + custom_path
        return KRYVEN_BASE_URL.rstrip("/") + path
    if KRYVEN_BASE_URL.endswith("/chat/completions"):
        return KRYVEN_BASE_URL
    if KRYVEN_BASE_URL.endswith("/v1"):
        return f"{KRYVEN_BASE_URL}/chat/completions"
    return f"{KRYVEN_BASE_URL}/v1/chat/completions"


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


def _extract_api_error(resp) -> tuple[str, str]:
    code = ""
    message = resp.text[:500]
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or message)
            code = str(err.get("code") or "")
        elif isinstance(data, dict):
            message = str(data.get("message") or data.get("error") or message)
    except Exception:
        pass
    return code, message


def _call_api(
    messages: list,
    max_tokens: int | None = None,
    timeout_seconds: int | None = None,
    temperature: float = 0.5,
    retries: int = 1,
) -> str:
    if not KRYVEN_API_KEY:
        return "❌ Kryven не настроен — добавь KRYVEN_API_KEY в Railway Variables."

    payload = {
        "model": KRYVEN_MODEL,
        "messages": messages,
        "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
        "temperature": temperature,
    }
    timeout_seconds = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
    last_error = ""

    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                _chat_completions_url(),
                headers={
                    "Authorization": f"Bearer {KRYVEN_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout_seconds,
            )
            if resp.status_code == 200:
                data = resp.json()
                content = _extract_content(data)
                if content:
                    return content
                last_error = "❌ Kryven ответил пустым сообщением."
            elif resp.status_code == 401:
                return "❌ Неверный API ключ Kryven."
            elif resp.status_code == 429:
                last_error = "⏳ Лимит запросов Kryven. Подожди минуту и повтори."
            else:
                code, message = _extract_api_error(resp)
                if resp.status_code == 503 and (code == "empty_response" or "did not return a response" in message.lower()):
                    last_error = "⏳ Kryven временно вернул пустой ответ. Запрос будет повторён или упрощён."
                else:
                    last_error = f"❌ Ошибка Kryven ({resp.status_code}): {message[:300]}"

            retryable = resp.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            if attempt < retries and retryable:
                time.sleep(min(2 + attempt * 2, 8))
                continue
            return last_error
        except requests.exceptions.Timeout:
            last_error = "⏳ Kryven не ответил вовремя. Попробуй ещё раз."
            if attempt < retries:
                time.sleep(min(2 + attempt * 2, 8))
                continue
            return last_error
        except Exception as e:
            return f"❌ Ошибка Kryven: {str(e)[:200]}"

    return last_error or "❌ Kryven не вернул ответ."


def _messages(user_message: str, history: list = None, memory_block: str = "") -> list:
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
    return messages


def ask_kryven(user_message: str, history: list = None, memory_block: str = "") -> str:
    return _call_api(_messages(user_message, history, memory_block))


def ask_kryven_dialog_analysis(user_message: str, history: list = None, memory_block: str = "") -> str:
    return _call_api(
        _messages(user_message, history, memory_block),
        max_tokens=DIALOG_ANALYSIS_MAX_TOKENS,
        timeout_seconds=DIALOG_ANALYSIS_TIMEOUT_SECONDS,
        temperature=0.35,
        retries=2,
    )
