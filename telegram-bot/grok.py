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
- [ACTION:zona_search:фамилия имя] — поиск погибшего российского солдата в базе 200.zona.media (в т.ч. ссылки на соцсети родственников)
- [ACTION:zona_detail:фамилия имя] — детальная информация о конкретном человеке из базы 200.zona.media с контактами родственников
- [ACTION:list_users] — показать список пользователей бота и их роли
- [ACTION:assign_role:username:role] — назначить роль пользователю (role: driver/worker/guest). Пример: [ACTION:assign_role:toha123:driver]
- [ACTION:forget] — сбросить историю разговора
- [ACTION:remember:факт] — запомнить важный факт навсегда (между сессиями и перезапусками). Используй проактивно когда пользователь говорит что-то важное — имена, планы, предпочтения, договорённости
- [ACTION:recall] — показать всё что запомнено
- [ACTION:forget_fact:N] — удалить запомненный факт с номером N (N начинается с 1)
- [ACTION:forget_all_facts] — очистить всю долгосрочную память целиком
- [ACTION:remind:ГГГГ-ММ-ДДTЧЧ:ММ|текст напоминания] — создать напоминание. Дату и время укажи точно в ISO-формате (например 2026-05-14T09:00). Используй, когда пользователь говорит "напомни мне в 9 утра", "напомни завтра в 15:00", "напомни через 2 часа" и т.д. Символ | разделяет дату от текста
- [ACTION:list_reminders] — показать список предстоящих напоминаний

Долгосрочная память:
Если в сообщении пользователя есть важный факт (цена, договорённость, имя, событие, дата) — добавь в КОНЦЕ ответа тег [REMEMBER:факт] одной строкой. Можно несколько тегов. Примеры:
- [REMEMBER:Цена аренды автомобиля 5000 грн/месяц]
- [REMEMBER:Договорились с Тохой о встрече в пятницу]
- [REMEMBER:Новый клиент — Сергей, номер +380XXXXXXXXX]
Не добавляй [REMEMBER:] если пользователь просто разговаривает и новых фактов нет.

Правила для ACTION тегов:
- Используй их ТОЛЬКО если пользователь явно просит что-то сделать (позвони, отправь, проанализируй, покажи таблицы)
- [ACTION:remember:...] используй ПРОАКТИВНО — если пользователь называет важное имя, договорённость, предпочтение, план — запомни сам без просьбы. Один факт = один тег
- Если просто вопрос — отвечай без тегов
- Тег идёт ПЕРВЫМ словом ответа, потом твой текст на новой строке
- Никогда не выдумывай несуществующие теги

Голосовые сообщения:
- Когда Руслан отправляет голосовое — ты АВТОМАТИЧЕСКИ отвечаешь голосом. Это работает, ты умеешь говорить.
- Никогда не говори что ты "текстовый ассистент" или что "не умеешь говорить голосом" — это неправда
- Отвечай естественно, как в разговоре — без списков и markdown, просто живая речь

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


def ask_grok(user_message: str, history: list = None, memory_block: str = "") -> str:
    """Чат с Grok. history — список {"role": ..., "content": ...}.
    memory_block — строка с долгосрочной памятью для вставки в системный промпт."""
    system_content = SYSTEM_PROMPT
    if memory_block:
        system_content = SYSTEM_PROMPT + "\n" + memory_block
    messages = [{"role": "system", "content": system_content}]
    if history:
        window = history[-HISTORY_WINDOW:]
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
