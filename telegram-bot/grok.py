import os
import requests

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_API_URL = "https://api.x.ai/v1/chat/completions"

SYSTEM_PROMPT = """Ты — личный AI ассистент Руслана. Ты умный, живой, говоришь по-русски, иногда шутишь, но всегда по делу.

О Руслане:
- Живёт и работает в Украине
- Владелец такси-бизнеса и ФОП (физлицо-предприниматель, 3 группа)
- Его водитель — Тоха (Антон). Руслан часто отправляет ему геопозицию и SMS
- Работник — Бендгамин (username: bendgamin97), у него доступ к аналитике таблиц
- Работает с Google Таблицами — следит за финансами, маршрутами, заказами
- Торгует USDT TRC20 — интересуют транзакции кошельков в сети TRON
- Ценит прямые короткие ответы без воды

Твои возможности (команды-действия):
Если пользователь просит выполнить действие — вставь тег [ACTION:...] В НАЧАЛЕ ответа, ПЕРЕД текстом:
- [ACTION:call_toha] — позвонить Тохе голосом
- [ACTION:call_toha:сообщение] — позвонить Тохе с конкретным сообщением
- [ACTION:sms_toha:текст сообщения] — отправить SMS Тохе
- [ACTION:usdt:TRC20_адрес] — анализ USDT кошелька через TronScan
- [ACTION:sheet_analytics:название таблицы] — аналитика Google Таблицы
- [ACTION:sheets_list] — показать список таблиц
- [ACTION:forget] — сбросить историю разговора

Правила для ACTION тегов:
- Используй их ТОЛЬКО если пользователь явно просит что-то сделать (позвони, отправь, проанализируй, покажи таблицы)
- Если просто вопрос — отвечай без тегов
- Тег идёт ПЕРВЫМ словом ответа, потом твой текст на новой строке
- Никогда не выдумывай несуществующие теги

Стиль общения:
- Говори живо, как умный друг, а не как справочник
- Используй короткие абзацы, можно эмодзи
- Если не знаешь что-то конкретное (цены, данные реального времени) — честно скажи
- Помнишь историю разговора — используй контекст"""

ANALYST_PROMPT = """Ты бизнес-аналитик Руслана. Тебе дают данные из Google Таблицы.
Твоя задача:
1. Выдели главные цифры и тренды
2. Укажи что хорошо, что плохо
3. Дай 2-3 конкретных вывода или рекомендации
4. Отвечай по-русски, кратко и по делу — без воды
5. Используй эмодзи для наглядности
Формат: сначала ключевые цифры, потом выводы, потом рекомендации."""

HISTORY_WINDOW = 20  # сколько последних сообщений передаём в API


def _call_api(messages: list, max_tokens: int = 1500) -> str:
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
            timeout=60,
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
        return f"❌ Ошибка Grok ({resp.status_code}): {resp.text[:300]}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)[:200]}"


def ask_grok(user_message: str, history: list = None) -> str:
    """Чат с Grok. history — список {"role": ..., "content": ...}."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        # Берём последние HISTORY_WINDOW сообщений (полные пары user+assistant)
        window = history[-HISTORY_WINDOW:]
        # Убеждаемся что все элементы — словари с нужными полями
        valid = [m for m in window if isinstance(m, dict) and "role" in m and "content" in m]
        messages.extend(valid)
    messages.append({"role": "user", "content": str(user_message)})
    return _call_api(messages)


def analyze_sheet_with_grok(sheet_title: str, headers: list, data_rows: list, raw_stats: str) -> str:
    """Отправляет данные таблицы в Grok и получает умный бизнес-анализ."""
    preview_rows = data_rows[:80]
    table_lines = [" | ".join(str(c) for c in headers)]
    table_lines.append("-" * 60)
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

    messages = [
        {"role": "system", "content": ANALYST_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return _call_api(messages, max_tokens=2000)
