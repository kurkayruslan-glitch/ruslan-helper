"""Kryven AI backend for Ruslan Helper."""

import os
import requests

KRYVEN_API_KEY = os.environ.get("KRYVEN_API_KEY", "").strip()
KRYVEN_API_URL = "https://kryven.cc/v1/apichat"
KRYVEN_TIMEOUT_SECONDS = int(os.environ.get("KRYVEN_TIMEOUT_SECONDS", "90"))


def _call_kryven(messages: list) -> str:
    """Call Kryven API with OpenAI-style message format."""
    if not KRYVEN_API_KEY:
        return "❌ Kryven не настроен — добавь KRYVEN_API_KEY в Railway Variables."

    try:
        resp = requests.post(
            KRYVEN_API_URL,
            headers={
                "Authorization": f"Bearer {KRYVEN_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"messages": messages},
            timeout=KRYVEN_TIMEOUT_SECONDS,
        )
        if resp.status_code == 401:
            return "❌ Неверный API ключ Kryven."
        if resp.status_code == 429:
            return "⏳ Лимит запросов Kryven. Подожди минуту и повтори."
        if resp.status_code != 200:
            return f"❌ Ошибка Kryven ({resp.status_code}): {resp.text[:300]}"
        return resp.json()["choices"][0]["message"]["content"].strip()
    except requests.exceptions.Timeout:
        return "⏳ Kryven не ответил вовремя. Попробуй ещё раз."
    except Exception as e:
        return f"❌ Ошибка Kryven: {str(e)[:200]}"


def ask_kryven(user_message: str, history: list = None, memory_block: str = "", system_prompt: str = "") -> str:
    """Chat with Kryven. Compatible with ask_grok interface."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if memory_block:
        messages.append({"role": "system", "content": memory_block})
    if history:
        window = history[-20:]  # Keep last 20 messages
        valid = [
            m for m in window
            if isinstance(m, dict) and "role" in m and "content" in m
        ]
        messages.extend(valid)
    messages.append({"role": "user", "content": str(user_message)})
    return _call_kryven(messages)

