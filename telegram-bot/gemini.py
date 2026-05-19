import os
import requests
from grok import SYSTEM_PROMPT, ANALYST_PROMPT

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

HISTORY_WINDOW = 20


def _to_gemini_contents(history: list, user_message: str) -> list:
    contents = []
    if history:
        for m in history[-HISTORY_WINDOW:]:
            if not isinstance(m, dict) or "role" not in m or "content" not in m:
                continue
            role = m["role"]
            if role == "system":
                continue
            g_role = "model" if role == "assistant" else "user"
            contents.append({"role": g_role, "parts": [{"text": str(m["content"])}]})
    contents.append({"role": "user", "parts": [{"text": str(user_message)}]})
    return contents


def _call_api(system_text: str, contents: list, max_tokens: int = 1500) -> str:
    if not GEMINI_API_KEY:
        return "❌ Gemini не настроен — добавь GEMINI_API_KEY в .env"
    body = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.9,
            "maxOutputTokens": max_tokens,
        },
    }
    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=body,
            timeout=60,
        )
        if resp.status_code == 401 or resp.status_code == 403:
            return "❌ Неверный API ключ Gemini."
        if resp.status_code == 429:
            return "⏳ Лимит запросов Gemini. Подожди минутку."
        if resp.status_code != 200:
            return f"❌ Ошибка Gemini ({resp.status_code}): {resp.text[:300]}"
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            return "❌ Gemini не вернул ответ (возможно, сработал фильтр безопасности)."
        parts = cands[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or "❌ Gemini вернул пустой ответ."
    except requests.exceptions.Timeout:
        return "⏳ Gemini не ответил вовремя. Попробуй ещё раз."
    except Exception as e:
        return f"❌ Ошибка: {str(e)[:200]}"


def ask_grok(user_message: str, history: list = None, memory_block: str = "") -> str:
    """Совместимое имя функции (так зовёт bot.py). На самом деле — Gemini."""
    system_text = SYSTEM_PROMPT + ("\n" + memory_block if memory_block else "")
    contents = _to_gemini_contents(history or [], user_message)
    return _call_api(system_text, contents)


def analyze_sheet_with_grok(sheet_title: str, headers: list, data_rows: list, raw_stats: str) -> str:
    """Анализ Google Таблицы через Gemini."""
    preview_rows = data_rows[:80]
    table_lines = [" | ".join(str(c) for c in headers), "-" * 60]
    for row in preview_rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        table_lines.append(" | ".join(str(c) for c in padded[:len(headers)]))
    table_text = "\n".join(table_lines)
    skipped = len(data_rows) - len(preview_rows)
    if skipped > 0:
        table_text += f"\n... и ещё {skipped} строк"
    prompt = (
        f"Таблица: «{sheet_title}»\n"
        f"Строк данных: {len(data_rows)}\n\n"
        f"ДАННЫЕ:\n{table_text}\n\n"
        f"БАЗОВАЯ СТАТИСТИКА:\n{raw_stats}\n\n"
        f"Сделай анализ этих данных для Руслана."
    )
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    return _call_api(ANALYST_PROMPT, contents, max_tokens=2000)
