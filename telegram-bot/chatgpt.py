"""OpenAI/ChatGPT LLM-backend for Ruslan Helper."""

import os
import requests
from grok import SYSTEM_PROMPT, ANALYST_PROMPT

_DIRECT_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
_PROXY_OPENAI_KEY = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY", "").strip()
OPENAI_API_KEY = _DIRECT_OPENAI_KEY
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_MAX_TOKENS = int(os.environ.get("OPENAI_MAX_TOKENS", "2200"))
AUDIO_ANALYSIS_MAX_TOKENS = int(os.environ.get("OPENAI_AUDIO_ANALYSIS_MAX_TOKENS", "7000"))
VOICE_CALL_MAX_TOKENS = int(os.environ.get("OPENAI_VOICE_CALL_MAX_TOKENS", "220"))

_explicit_base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
_proxy_base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", "").strip()
if _explicit_base_url:
    OPENAI_BASE_URL = _explicit_base_url.rstrip("/")
elif _DIRECT_OPENAI_KEY:
    OPENAI_BASE_URL = "https://api.openai.com/v1"
else:
    OPENAI_BASE_URL = (_proxy_base_url or "https://api.openai.com/v1").rstrip("/")

HISTORY_WINDOW = 20


def _call_api(messages: list, max_tokens: int = None) -> str:
    key = _DIRECT_OPENAI_KEY or _PROXY_OPENAI_KEY
    if not key:
        return "❌ OpenAI не настроен — добавь OPENAI_API_KEY в Railway Variables"
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    try:
        resp = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.9,
            },
            timeout=60,
        )
        if resp.status_code == 401:
            return "❌ Неверный API ключ OpenAI."
        if resp.status_code == 429:
            return "⏳ Лимит запросов OpenAI. Подожди минуту."
        if resp.status_code != 200:
            return f"❌ Ошибка OpenAI ({resp.status_code}): {resp.text[:300]}"
        return resp.json()["choices"][0]["message"]["content"].strip()
    except requests.exceptions.Timeout:
        return "⏳ OpenAI не ответил вовремя. Попробуй ещё раз."
    except Exception as e:
        return f"❌ Ошибка OpenAI: {str(e)[:200]}"


def ask_grok(user_message: str, history: list = None, memory_block: str = "") -> str:
    """Чат с ChatGPT. Совместимое имя функции (так зовёт bot.py)."""
    system_content = SYSTEM_PROMPT + ("\n" + memory_block if memory_block else "")
    messages = [{"role": "system", "content": system_content}]
    if history:
        window = history[-HISTORY_WINDOW:]
        valid = [m for m in window
                 if isinstance(m, dict) and "role" in m and "content" in m]
        messages.extend(valid)
    messages.append({"role": "user", "content": str(user_message)})
    if "Анализ аудиозаписи разговора" in memory_block:
        max_tokens = AUDIO_ANALYSIS_MAX_TOKENS
    elif "голосовой помощник Руслана в телефонном звонке" in memory_block:
        max_tokens = VOICE_CALL_MAX_TOKENS
    else:
        max_tokens = None
    return _call_api(messages, max_tokens=max_tokens)


def analyze_sheet_with_grok(sheet_title: str, headers: list,
                             data_rows: list, raw_stats: str) -> str:
    """Анализ Google Таблицы через ChatGPT."""
    preview_rows = data_rows[:80]
    table_lines = [" | ".join(str(c) for c in headers), "-" * 60]
    for row in preview_rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        table_lines.append(" | ".join(str(c) for c in padded[:len(headers)]))
    table_text = "\n".join(table_lines)
    if len(data_rows) > len(preview_rows):
        table_text += f"\n... и ещё {len(data_rows) - len(preview_rows)} строк"
    prompt = (
        f"Таблица: «{sheet_title}»\n"
        f"Строк данных: {len(data_rows)}\n\n"
        f"ДАННЫЕ:\n{table_text}\n\n"
        f"БАЗОВАЯ СТАТИСТИКА:\n{raw_stats}\n\n"
        f"Сделай анализ этих данных для Руслана."
    )
    messages = [
        {"role": "system", "content": ANALYST_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return _call_api(messages, max_tokens=2400)
