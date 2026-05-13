import os
import requests

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_API_URL = "https://api.x.ai/v1/chat/completions"

SYSTEM_PROMPT = """Ты личный помощник Руслана — умный, краткий, по делу.
Отвечай на русском языке. Будь полезным, конкретным и дружелюбным.
Не лей воду — только суть. Если вопрос деловой — отвечай профессионально."""

ANALYST_PROMPT = """Ты бизнес-аналитик Руслана. Тебе дают данные из Google Таблицы.
Твоя задача:
1. Выдели главные цифры и тренды
2. Укажи что хорошо, что плохо
3. Дай 2-3 конкретных вывода или рекомендации
4. Отвечай по-русски, кратко и по делу — без воды
5. Используй эмодзи для наглядности
Формат: сначала ключевые цифры, потом выводы, потом рекомендации."""


def _call_api(messages: list, max_tokens: int = 1024) -> str:
    if not XAI_API_KEY:
        return "❌ Grok не настроен — нет API ключа."
    try:
        resp = requests.post(
            XAI_API_URL,
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "grok-3",
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            },
            timeout=45,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except requests.exceptions.Timeout:
        return "⏳ Grok не ответил вовремя. Попробуй ещё раз."
    except requests.exceptions.HTTPError:
        if resp.status_code == 401:
            return "❌ Неверный API ключ Grok."
        if resp.status_code == 429:
            return "⏳ Лимит запросов. Попробуй через минуту."
        return f"❌ Ошибка Grok ({resp.status_code}): {resp.text[:200]}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)[:200]}"


def ask_grok(user_message: str, history: list = None) -> str:
    """Обычный чат с Grok с историей."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_message})
    return _call_api(messages)


def analyze_sheet_with_grok(sheet_title: str, headers: list, data_rows: list, raw_stats: str) -> str:
    """
    Отправляет данные таблицы в Grok и получает умный бизнес-анализ.
    Ограничиваем до 80 строк чтобы не превышать лимит токенов.
    """
    # Формируем компактное представление данных
    preview_rows = data_rows[:80]
    table_lines = [" | ".join(str(c) for c in headers)]
    table_lines.append("-" * 60)
    for row in preview_rows:
        # Дополняем пустыми ячейками если строка короче
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

    messages = [
        {"role": "system", "content": ANALYST_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return _call_api(messages, max_tokens=1500)
