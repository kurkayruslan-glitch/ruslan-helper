"""Клиент к локальной Llama через Ollama (http://localhost:11434).

Полностью совместим по интерфейсу с grok.py — функция `ask_grok` тут тоже есть,
поэтому в bot.py можно делать:

    if os.environ.get("LLM_BACKEND") == "llama":
        from llama import ask_grok
    else:
        from grok import ask_grok
"""
import os
import requests

from grok import SYSTEM_PROMPT, ANALYST_PROMPT  # переиспользуем те же промпты

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
HISTORY_WINDOW = 20


def _call_ollama(messages: list, temperature: float = 0.9, max_tokens: int = 1500) -> str:
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data.get("message", {}).get("content") or "").strip()
    except requests.exceptions.ConnectionError:
        return (
            f"❌ Не могу подключиться к Ollama на {OLLAMA_URL}. "
            f"Проверь, что Ollama запущена (в трее или `ollama serve`)."
        )
    except requests.exceptions.Timeout:
        return "⏳ Ллама думала слишком долго. Попробуй ещё раз."
    except requests.exceptions.HTTPError as e:
        body = ""
        try:
            body = resp.text[:300]
        except Exception:
            pass
        return f"❌ Ошибка Ollama ({e.response.status_code if e.response else '?'}): {body}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)[:200]}"


def ask_grok(user_message: str, history: list = None, memory_block: str = "") -> str:
    """Совместимая обёртка — отвечает локальная Llama вместо xAI Grok."""
    system_content = SYSTEM_PROMPT
    if memory_block:
        system_content = SYSTEM_PROMPT + "\n" + memory_block
    messages = [{"role": "system", "content": system_content}]
    if history:
        window = history[-HISTORY_WINDOW:]
        valid = [m for m in window if isinstance(m, dict) and "role" in m and "content" in m]
        messages.extend(valid)
    messages.append({"role": "user", "content": str(user_message)})
    return _call_ollama(messages)


def analyze_sheet_with_grok(sheet_title: str, headers: list, data_rows: list, raw_stats: str) -> str:
    preview_rows = data_rows[:80]
    table_lines = [" | ".join(str(c) for c in headers)]
    table_lines.append("-" * 60)
    for row in preview_rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        table_lines.append(" | ".join(str(c) for c in padded[: len(headers)]))
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
    messages = [
        {"role": "system", "content": ANALYST_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return _call_ollama(messages, max_tokens=2000)
