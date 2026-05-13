import telebot
from telebot import types
import os
import time
import io
from urllib.parse import quote
from openai import OpenAI
from keep_alive import keep_alive
from sheets import get_values, append_values, get_sheet_info, format_table
from sms import send_geo_to_toha, send_sms_to_toha
from analytics import analyze_sheet_data, analyze_sheet_with_ai, register_sheet, find_sheet_id, list_sheets
from roles import get_role, set_role, list_roles
from calls import make_call
from grok import ask_grok
from tron import get_usdt_transactions, get_account_balance, build_tx_summary

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

OPENAI_BASE_URL = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
OPENAI_API_KEY = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")

openai_client = OpenAI(
    base_url=OPENAI_BASE_URL,
    api_key=OPENAI_API_KEY,
)

bot = telebot.TeleBot(TOKEN)

# ──────────────────────────────────────────────
# БЕЗОПАСНОСТЬ — белый список
# ──────────────────────────────────────────────
OWNER_ID = 7959647798          # Руслан — всегда имеет доступ
SECRET_CODE = "ruslan2024vip"  # Секретный пароль для новых пользователей
WHITELIST_FILE = "whitelist.json"

def _load_whitelist() -> set:
    if os.path.exists(WHITELIST_FILE):
        try:
            import json
            with open(WHITELIST_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return {OWNER_ID}

def _save_whitelist():
    import json
    with open(WHITELIST_FILE, "w") as f:
        json.dump(list(allowed_users), f)

allowed_users: set = _load_whitelist()
allowed_users.add(OWNER_ID)

def is_allowed(chat_id: int) -> bool:
    return chat_id in allowed_users

def grant_access(chat_id: int):
    allowed_users.add(chat_id)
    _save_whitelist()

# ──────────────────────────────────────────────

def safe_send(chat_id: int, text: str, reply_markup=None):
    """Отправляет сообщение — сначала с Markdown, при ошибке — без форматирования."""
    for chunk in [text[i:i+4000] for i in range(0, max(len(text), 1), 4000)]:
        try:
            bot.send_message(chat_id, chunk, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception:
            try:
                bot.send_message(chat_id, chunk, reply_markup=reply_markup)
            except Exception as e2:
                bot.send_message(chat_id, f"⚠️ Не удалось отправить ответ: {str(e2)[:100]}")

# Последняя геопозиция пользователя
last_location = {}

# Состояние ожидания ID таблицы
waiting_for_sheet_id = {}

# Состояние ожидания текста для звонка (worker)
waiting_for_call_msg = {}

# Состояние звонка владельца: {chat_id: {"step": "number"} | {"step": "message", "number": "..."}}
waiting_for_owner_call = {}

# Ожидание адреса TRC20 кошелька
waiting_for_wallet = set()

# История чата с Grok — сохраняется на диск
GROK_HISTORY_FILE = "grok_history.json"

def _load_grok_history() -> dict:
    if os.path.exists(GROK_HISTORY_FILE):
        try:
            import json
            with open(GROK_HISTORY_FILE) as f:
                return {int(k): v for k, v in json.load(f).items()}
        except Exception:
            pass
    return {}

def _save_grok_history():
    import json
    with open(GROK_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in grok_history.items()}, f, ensure_ascii=False, indent=2)

grok_history: dict = _load_grok_history()

# Известные пользователи: username (без @) → chat_id (сохраняем на диск)
KNOWN_USERS_FILE = "known_users.json"

def _load_known_users() -> dict:
    if os.path.exists(KNOWN_USERS_FILE):
        try:
            import json
            with open(KNOWN_USERS_FILE) as f:
                return {k: int(v) for k, v in json.load(f).items()}
        except Exception:
            pass
    return {}

def _save_known_users():
    import json
    with open(KNOWN_USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(known_users, f, ensure_ascii=False, indent=2)

known_users: dict = _load_known_users()


def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("📞 Позвонить",   "🚕 Тоха")
    markup.add("📍 Геопозиция",  "📗 Таблицы")
    markup.add("💰 USDT крипто", "📊 Я Тигр")
    markup.add("📋 ФОП",         "🗑️ Забыть")
    markup.add("🛣️ Маршрут")
    return markup


def sheets_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("📊 Аналитика таблицы", "📋 Мои таблицы")
    markup.add("📖 Читать таблицу", "✏️ Записать в таблицу")
    markup.add("➕ Сохранить таблицу", "ℹ️ Инфо о таблице")
    markup.add("🔙 Назад")
    return markup


def toha_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("📍 Отправить гео Тохе")
    markup.add("💬 Написать Тохе SMS")
    markup.add("🔙 Назад")
    return markup


def driver_menu():
    """Меню для водителя (Тоха и другие с ролью driver)"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("📍 Геопозиция Руслана")
    return markup


def worker_menu():
    """Меню для рабочего — аналитика + звонок Руслану"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("📞 Позвонить Руслану")
    markup.add("📊 Аналитика таблицы")
    markup.add("📋 Мои таблицы")
    return markup


def get_menu_for_role(role: str):
    if role == "driver":
        return driver_menu()
    if role == "worker":
        return worker_menu()
    return main_menu()


def is_toha_geo_command(text: str) -> bool:
    """Распознать голосовую команду отправки гео Тохе"""
    t = text.lower()
    keywords = ["отправь тоше", "отправь тохе", "скинь тоше", "скинь тохе",
                "пошли тоше", "пошли тохе", "отправь гео тох", "скинь гео тох",
                "тоха гео", "тоше гео", "тохе гео"]
    return any(k in t for k in keywords)


def _parse_action(reply: str) -> tuple[str | None, str | None, str]:
    """
    Извлекает ACTION-тег из начала ответа Grok.
    Возвращает (action_type, action_param, cleaned_text).
    Примеры тегов: [ACTION:call_toha], [ACTION:usdt:TAddr...], [ACTION:forget]
    """
    import re
    match = re.match(r"^\[ACTION:([^\]]+)\]\s*", reply)
    if not match:
        return None, None, reply
    tag_content = match.group(1)
    text_after = reply[match.end():]
    parts = tag_content.split(":", 1)
    action_type = parts[0].strip()
    action_param = parts[1].strip() if len(parts) > 1 else None
    return action_type, action_param, text_after


def _handle_grok_action(chat_id: int, action_type: str, action_param: str | None):
    """Выполняет действие из ACTION-тега Grok."""
    if action_type == "call_toha":
        toha_number = os.environ.get("TOHA_PHONE_NUMBER", "")
        if not toha_number:
            bot.send_message(chat_id, "⚠️ Номер Тохи не настроен.")
            return
        msg = action_param or "Руслан звонит тебе!"
        bot.send_message(chat_id, f"📞 Звоню Тохе...")
        ok, info = make_call(toha_number, msg)
        if ok:
            bot.send_message(chat_id, f"✅ Позвонил Тохе! Скажет: «{msg}»")
        else:
            bot.send_message(chat_id, f"❌ Ошибка звонка: {info}")

    elif action_type == "sms_toha":
        toha_number = os.environ.get("TOHA_PHONE_NUMBER", "")
        msg = action_param or ""
        if toha_number and msg:
            sms_link = make_sms_link(toha_number, msg)
            markup = types.InlineKeyboardMarkup()
            markup.row(types.InlineKeyboardButton("📱 Открыть SMS для Тохи", url=sms_link))
            bot.send_message(chat_id, f"💬 Нажми — откроется SMS с текстом:\n«{msg}»", reply_markup=markup)

    elif action_type == "usdt":
        address = action_param or ""
        if not (address.startswith("T") and len(address) == 34):
            bot.send_message(chat_id, "⚠️ Некорректный TRC20 адрес в запросе.")
            return
        bot.send_message(chat_id, f"🔍 Ищу USDT транзакции для `{address}`...", parse_mode="Markdown")
        ok, txs, err = get_usdt_transactions(address, limit=50)
        if not ok:
            bot.send_message(chat_id, f"❌ TronScan: {err}")
            return
        if not txs:
            bot.send_message(chat_id, "📭 Транзакции USDT TRC20 не найдены.")
            return
        balance = get_account_balance(address)
        summary, _, _ = build_tx_summary(address, txs)
        bot.send_message(chat_id, f"💹 Баланс: *{balance}* | {len(txs)} транзакций — анализирую...",
                         parse_mode="Markdown")
        prompt = (
            f"Проанализируй транзакции USDT TRC20 кошелька, дай бизнес-аналитику на русском.\n\n"
            f"{summary}\n\n"
            f"1. Резюме активности\n2. Топ контрагентов\n3. Паттерны по времени\n4. Риски\n5. Итог"
        )
        analysis = ask_grok(prompt, [])
        safe_send(chat_id, f"💰 *Аналитика USDT TRC20*\n\n{analysis}", main_menu())

    elif action_type == "sheet_analytics":
        name = action_param or ""
        sheet_id = find_sheet_id(name.lower())
        if not sheet_id:
            saved = list_sheets()
            names = "\n".join([f"• {n}" for n in saved.keys()]) if saved else "нет"
            bot.send_message(chat_id, f"⚠️ Таблица «{name}» не найдена.\n\nДоступные:\n{names}")
            return
        bot.send_message(chat_id, f"🤖 Анализирую «{name}»...")
        bot.send_chat_action(chat_id, "typing")
        result = analyze_sheet_with_ai(sheet_id)
        safe_send(chat_id, result, reply_markup=main_menu())

    elif action_type == "sheets_list":
        saved = list_sheets()
        inline = types.InlineKeyboardMarkup(row_width=1)
        if saved:
            for name, sid in saved.items():
                url = f"https://docs.google.com/spreadsheets/d/{sid}"
                inline.add(types.InlineKeyboardButton(f"📄 {name.title()}", url=url))
        inline.add(types.InlineKeyboardButton("➕ Добавить таблицу", callback_data="add_sheet"))
        text = "📗 *Мои таблицы:*" if saved else "📗 Пока нет таблиц."
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=inline)

    elif action_type == "forget":
        grok_history.pop(chat_id, None)
        _save_grok_history()
        bot.send_message(chat_id, "🗑️ История разговора очищена. Начинаем с чистого листа!", reply_markup=main_menu())


def _ask_grok_and_route(chat_id: int, text: str):
    """Отправляет сообщение в Grok, разбирает ACTION-теги, показывает ответ."""
    history = grok_history.get(chat_id, [])
    bot.send_chat_action(chat_id, "typing")
    reply = ask_grok(text, history)

    # Сохраняем в историю (полный ответ с тегом для контекста модели не нужен — сохраняем чистый)
    action_type, action_param, clean_reply = _parse_action(reply)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": clean_reply or reply})
    grok_history[chat_id] = history
    _save_grok_history()

    # Сначала выполняем действие (если есть)
    if action_type and action_type != "forget":
        _handle_grok_action(chat_id, action_type, action_param)
    elif action_type == "forget":
        _handle_grok_action(chat_id, action_type, action_param)
        return  # forget сам выводит сообщение

    # Потом показываем текстовый ответ Grok (если не пустой)
    if clean_reply and clean_reply.strip():
        safe_send(chat_id, clean_reply, main_menu())


def process_text(chat_id, text):
    import re
    t = text.lower()

    # ── Кнопка «🗑️ Забыть» — сброс истории ─────────────────────────────────
    if "забыть" in t or "🗑️" in t or "/forget" in t:
        grok_history.pop(chat_id, None)
        _save_grok_history()
        bot.send_message(chat_id, "🗑️ Готово — я забыл нашу историю. Начинаем с нуля!", reply_markup=main_menu())
        return

    # ── Состояние: ожидаем номер для звонка ──────────────────────────────────
    if chat_id in waiting_for_owner_call:
        state = waiting_for_owner_call[chat_id]
        if state["step"] == "number":
            # Убираем пробелы/дефисы, принимаем любой формат
            number = re.sub(r"[\s\-\(\)]", "", text.strip())
            if not re.match(r"^\+?\d{7,15}$", number):
                bot.send_message(chat_id, "⚠️ Не похоже на номер. Напиши в формате +380XXXXXXXXX:")
                return
            waiting_for_owner_call[chat_id] = {"step": "message", "number": number}
            bot.send_message(chat_id, f"✅ Номер: *{number}*\n\nТеперь напиши что сказать голосом:",
                             parse_mode="Markdown")
            return
        elif state["step"] == "message":
            number = state["number"]
            waiting_for_owner_call.pop(chat_id)
            bot.send_message(chat_id, f"📞 Звоню на {number}...")
            ok, info = make_call(number, text)
            if ok:
                bot.send_message(chat_id, f"✅ Позвонил на *{number}*!\nГолосом скажет: _{text}_",
                                 parse_mode="Markdown", reply_markup=main_menu())
            else:
                bot.send_message(chat_id, f"❌ Ошибка звонка: {info}", reply_markup=main_menu())
            return

    # ── Кнопка «📞 Позвонить» ─────────────────────────────────────────────────
    if "📞 позвонить" in t or t.strip() == "📞":
        bot.send_message(chat_id, "📞 *Звонок*\n\nНа какой номер позвонить? Напиши в формате +380XXXXXXXXX:",
                         parse_mode="Markdown")
        waiting_for_owner_call[chat_id] = {"step": "number"}
        return

    # ── Inline: «позвони +380XXXXXXXXX скажи [текст]» ────────────────────────
    inline_call = re.match(
        r"позвони\s*(\+?\d[\d\s\-]{6,14})\s*(?:и\s*)?(?:скажи|сказать|передай)?\s*(.+)?",
        t, re.IGNORECASE
    )
    if inline_call:
        number = re.sub(r"[\s\-]", "", inline_call.group(1))
        message = inline_call.group(2) or ""
        if not message:
            waiting_for_owner_call[chat_id] = {"step": "message", "number": number}
            bot.send_message(chat_id, f"✅ Номер: *{number}*\n\nЧто сказать голосом?",
                             parse_mode="Markdown")
            return
        bot.send_message(chat_id, f"📞 Звоню на {number}...")
        ok, info = make_call(number, message)
        if ok:
            bot.send_message(chat_id, f"✅ Позвонил на *{number}*!\nГолосом скажет: _{message}_",
                             parse_mode="Markdown", reply_markup=main_menu())
        else:
            bot.send_message(chat_id, f"❌ Ошибка звонка: {info}", reply_markup=main_menu())
        return

    # Голосовая команда — отправить Серёже пароль
    if any(phrase in t for phrase in [
        "отправь серёже", "отправь сереже",
        "серёже пароль", "сереже пароль",
        "отправь пароль серёже", "отправь пароль сереже",
        "пароль серёже", "пароль сереже",
        "скинь серёже", "скинь сереже",
        "отправь серёжке", "серёжке пароль",
    ]):
        seryozha_id = known_users.get("yebash1")
        target = seryozha_id if seryozha_id else "@yebash1"
        try:
            bot.send_message(target, "сост хуй")
            bot.send_message(chat_id, "✅ Пароль отправлен Серёже!", reply_markup=main_menu())
        except Exception as e:
            print(f"Ошибка отправки Серёже: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.row(types.InlineKeyboardButton("💬 Открыть чат с Серёжей", url="https://t.me/yebash1"))
            bot.send_message(
                chat_id,
                "⚠️ Серёжа ещё не запустил бота.\nПопроси его открыть бота и нажать /start, потом попробуй снова.\n\nИли отправь вручную — скопируй:\n\n<code>сост хуй</code>",
                parse_mode="HTML",
                reply_markup=markup
            )
        return

    # Голосовая команда — отправить гео Тохе
    if is_toha_geo_command(t):
        if chat_id in last_location:
            lat, lon = last_location[chat_id]
            maps_link = f"https://maps.google.com/?q={lat},{lon}"
            toha_number = os.environ.get("TOHA_PHONE_NUMBER", "")
            sms_link = make_sms_link(toha_number, f"Гео Руслана: {maps_link}")
            markup = types.InlineKeyboardMarkup()
            markup.row(types.InlineKeyboardButton("📱 Открыть SMS и отправить Тохе", url=sms_link))
            markup.row(types.InlineKeyboardButton("🗺️ Открыть в картах", url=maps_link))
            bot.send_message(chat_id,
                             f"📍 Нажми кнопку — откроется SMS с геопозицией:\n{maps_link}",
                             reply_markup=markup)
        else:
            bot.send_message(chat_id,
                             "📍 Сначала отправь мне свою геопозицию — нажми скрепку 📎 → Геопозиция.\n"
                             "Потом скажи «Отправь Тохе гео»",
                             reply_markup=main_menu())
        return

    if any(word in t for word in ["привет", "здравствуй", "эй", "hi"]):
        bot.send_message(chat_id, "Привет, Руслан! 😊 Как дела? Чем помочь?", reply_markup=main_menu())

    elif "как дела" in t:
        bot.send_message(chat_id, "Отлично! Готов помогать 24/7. А у тебя как? 🚀")

    elif "тигр" in t or "статистика" in t:
        bot.send_message(chat_id, "📊 Делаю полную статистику по Я Тигр...\n\nОтправь ID Google таблицы с данными.", reply_markup=main_menu())

    elif "маршрут" in t or "лубны" in t or "куда" in t:
        bot.send_message(chat_id, "🛣️ Отправь геопозицию или напиши куда ехать — построю маршрут через Google Maps")

    elif "тоха" in t or "водитель" in t:
        bot.send_message(chat_id,
                         "🚕 Что делаем с Тохой?",
                         reply_markup=toha_menu())

    elif "отправить гео тохе" in t or "📍 отправить гео тохе" in t:
        if chat_id in last_location:
            lat, lon = last_location[chat_id]
            maps_link = f"https://maps.google.com/?q={lat},{lon}"
            toha_number = os.environ.get("TOHA_PHONE_NUMBER", "")
            sms_link = make_sms_link(toha_number, f"Гео Руслана: {maps_link}")
            markup = types.InlineKeyboardMarkup()
            markup.row(types.InlineKeyboardButton("📱 Открыть SMS и отправить Тохе", url=sms_link))
            markup.row(types.InlineKeyboardButton("🗺️ Открыть в картах", url=maps_link))
            bot.send_message(chat_id,
                             f"📍 Нажми кнопку — откроется SMS с геопозицией для Тохи:\n{maps_link}",
                             reply_markup=markup)
        else:
            bot.send_message(chat_id,
                             "📍 Сначала отправь мне геопозицию через скрепку 📎 → Геопозиция.",
                             reply_markup=toha_menu())

    elif "написать тохе sms" in t or "💬 написать тохе sms" in t:
        bot.send_message(chat_id, "💬 Напиши текст SMS для Тохи:")
        waiting_for_sheet_id[chat_id] = "toha_sms"

    elif "гео" in t or "где я" in t or "геопозиция" in t:
        bot.send_message(chat_id, "📍 Отправь геопозицию — нажми скрепку 📎 → Геопозиция")

    elif "фоп" in t:
        bot.send_message(chat_id, "📋 Готовлю отчёт по ФОП 3 группы.\n\nОтправь ID таблицы с данными ФОП.")

    elif "google" in t or "таблиц" in t or "гугл" in t:
        saved = list_sheets()
        # Inline-кнопки: каждая таблица открывается в приложении Гугл Таблицы
        inline = types.InlineKeyboardMarkup(row_width=1)
        if saved:
            for name, sheet_id in saved.items():
                url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
                inline.add(types.InlineKeyboardButton(f"📄 {name.title()}", url=url))
        inline.add(types.InlineKeyboardButton("➕ Добавить таблицу", callback_data="add_sheet"))
        inline.add(types.InlineKeyboardButton("📊 Аналитика", callback_data="analytics_menu"))
        text = "📗 *Мои таблицы*\n\nНажми — откроется в приложении:" if saved else "📗 *Таблицы*\n\nПока нет сохранённых таблиц."
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=inline)

    elif "аналитика таблицы" in t or "📊 аналитика" in t or "статистика таблиц" in t or "аналитику таблиц" in t or "сводку" in t:
        saved = list_sheets()
        if not saved:
            bot.send_message(chat_id,
                             "📊 Пока нет сохранённых таблиц.\n\nНажми *➕ Сохранить таблицу* и добавь свою таблицу по ID.",
                             parse_mode="Markdown", reply_markup=sheets_menu())
        else:
            names = "\n".join([f"• {name}" for name in saved.keys()])
            bot.send_message(chat_id,
                             f"📊 Напиши название таблицы для анализа:\n\n{names}",
                             parse_mode="Markdown")
            waiting_for_sheet_id[chat_id] = "analytics"

    elif "мои таблицы" in t or "📋 мои" in t or "список таблиц" in t:
        saved = list_sheets()
        if not saved:
            bot.send_message(chat_id, "Нет сохранённых таблиц. Нажми *➕ Сохранить таблицу*.",
                             parse_mode="Markdown", reply_markup=sheets_menu())
        else:
            names = "\n".join([f"• *{name}*" for name in saved.keys()])
            bot.send_message(chat_id, f"📋 *Твои таблицы:*\n\n{names}",
                             parse_mode="Markdown", reply_markup=sheets_menu())

    elif "сохранить таблицу" in t or "➕ сохранить" in t or "добавить таблицу" in t:
        bot.send_message(chat_id,
                         "➕ Отправь название и ID таблицы через пробел:\n\n"
                         "Пример: `Продажи 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms`\n\n"
                         "ID таблицы — это часть ссылки Google Sheets после /d/",
                         parse_mode="Markdown")
        waiting_for_sheet_id[chat_id] = "save_sheet"

    elif "читать таблицу" in t or "📖" in t:
        bot.send_message(chat_id,
                         "📖 Отправь ID таблицы и диапазон через пробел.\n\nПример:\n`ID_ТАБЛИЦЫ Лист1!A1:E10`",
                         parse_mode="Markdown")
        waiting_for_sheet_id[chat_id] = "read"

    elif "записать в таблицу" in t or "✏️" in t:
        bot.send_message(chat_id,
                         "✏️ Отправь ID таблицы, диапазон и данные через пробел.\n\nПример:\n`ID_ТАБЛИЦЫ Лист1!A1 Данные`",
                         parse_mode="Markdown")
        waiting_for_sheet_id[chat_id] = "write"

    elif "инфо о таблице" in t or "ℹ️" in t:
        bot.send_message(chat_id,
                         "ℹ️ Отправь ID таблицы.\n\nПример:\n`ID_ТАБЛИЦЫ`",
                         parse_mode="Markdown")
        waiting_for_sheet_id[chat_id] = "info"

    elif any(p in t for p in ["назначь роль", "назначить роль", "дать роль", "роль водитель", "сделай водителем"]):
        saved_users = {v: k for k, v in known_users.items()}
        if not known_users:
            bot.send_message(chat_id,
                             "👥 Пока никто не запускал бота.\n\nКогда человек напишет /start — он появится здесь.\n"
                             "Потом напиши: *назначь роль водитель @username*",
                             parse_mode="Markdown")
        else:
            names = "\n".join([f"• @{u} (ID: {uid})" for u, uid in known_users.items()])
            bot.send_message(chat_id,
                             f"👥 *Пользователи которые запускали бота:*\n\n{names}\n\n"
                             "Напиши: *назначь водителем @username*\nНапример: `назначь водителем @toha123`",
                             parse_mode="Markdown")
        waiting_for_sheet_id[chat_id] = "assign_role"

    elif "назначь водителем" in t or "сделай водителем" in t:
        # Быстрая команда: "назначь водителем @username"
        words = text.strip().split()
        username = next((w.lstrip("@").lower() for w in words if w.startswith("@")), None)
        if username and username in known_users:
            target_id = known_users[username]
            grant_access(target_id)
            set_role(target_id, "driver")
            bot.send_message(chat_id, f"✅ @{username} назначен водителем!\nТеперь у него кнопка «📍 Геопозиция Руслана».")
        elif username:
            bot.send_message(chat_id, f"⚠️ @{username} ещё не запускал бота. Попроси его написать /start.")
        else:
            bot.send_message(chat_id, "⚠️ Укажи username: *назначь водителем @username*", parse_mode="Markdown")

    elif "список пользователей" in t or "кто в боте" in t or "мои пользователи" in t:
        roles_data = list_roles()
        if not known_users:
            bot.send_message(chat_id, "👥 Пока никто не запускал бота.")
        else:
            role_names = {"owner": "Владелец", "driver": "Водитель", "guest": "Гость"}
            lines = []
            for uname, uid in known_users.items():
                role = roles_data.get(str(uid), "guest")
                lines.append(f"• @{uname} — {role_names.get(role, role)}")
            bot.send_message(chat_id, "👥 *Пользователи бота:*\n\n" + "\n".join(lines), parse_mode="Markdown")

    elif "usdt" in t or "крипто" in t or "💰" in t or "trc20" in t or "кошелёк" in t or "кошелек" in t:
        waiting_for_wallet.add(chat_id)
        bot.send_message(chat_id,
                         "💰 *USDT TRC20 Аналитика*\n\n"
                         "Отправь адрес кошелька TRC20 — найду все транзакции и сделаю анализ через Grok:",
                         parse_mode="Markdown")

    elif "назад" in t or "🔙" in t:
        bot.send_message(chat_id, "Главное меню 👇", reply_markup=main_menu())

    elif "что можешь" in t or "что ты умеешь" in t or "❓" in t:
        bot.send_message(chat_id,
                         "Я могу очень много:\n"
                         "• Статистику по Я Тигр 📊\n"
                         "• Отчёты по ФОП 📋\n"
                         "• Маршруты 🛣️\n"
                         "• Отправить гео Тохе по SMS 🚕\n"
                         "• Принимать голосовые 🎤\n"
                         "• Читать и писать в Google Таблицы 📗\n"
                         "• И многое другое!\n\nЧто сейчас нужно?",
                         reply_markup=main_menu())

    elif "спасибо" in t or "благодарю" in t:
        bot.send_message(chat_id, "Пожалуйста! Рад помочь 😊")

    else:
        if chat_id in waiting_for_wallet:
            waiting_for_wallet.discard(chat_id)
            address = text.strip()
            # Базовая валидация адреса TRC20 (начинается с T, 34 символа)
            if not (address.startswith("T") and len(address) == 34):
                bot.send_message(chat_id,
                                 "⚠️ Это не похоже на TRC20 адрес.\n"
                                 "Адрес должен начинаться с *T* и содержать 34 символа.\n\n"
                                 "Попробуй ещё раз — нажми «💰 USDT крипто».",
                                 parse_mode="Markdown")
                return
            bot.send_message(chat_id, f"🔍 Ищу транзакции для `{address}`...", parse_mode="Markdown")
            ok, txs, err = get_usdt_transactions(address, limit=50)
            if not ok:
                bot.send_message(chat_id, f"❌ Ошибка TronScan: {err}")
                return
            if not txs:
                bot.send_message(chat_id, "📭 Транзакции USDT TRC20 не найдены для этого адреса.")
                return
            balance = get_account_balance(address)
            summary, total_in, total_out = build_tx_summary(address, txs)
            bot.send_message(chat_id, f"💹 Баланс: *{balance}*\nНашёл *{len(txs)}* транзакций — анализирую через Grok...",
                             parse_mode="Markdown")
            prompt = (
                f"Проанализируй транзакции USDT TRC20 кошелька и дай бизнес-аналитику на русском языке.\n\n"
                f"{summary}\n\n"
                f"Сделай:\n"
                f"1. Краткое резюме активности кошелька\n"
                f"2. Топ контрагентов (кто платит/кому платят)\n"
                f"3. Паттерны по времени (когда активен)\n"
                f"4. Риски и необычные транзакции\n"
                f"5. Итог и выводы"
            )
            reply = ask_grok(prompt, [])
            safe_send(chat_id, f"💰 *Аналитика USDT TRC20*\n\n{reply}", main_menu())

        elif chat_id in waiting_for_sheet_id:
            mode = waiting_for_sheet_id.pop(chat_id)
            if mode == "toha_sms":
                toha_number = os.environ.get("TOHA_PHONE_NUMBER", "")
                sms_link = make_sms_link(toha_number, text)
                markup = types.InlineKeyboardMarkup()
                markup.row(types.InlineKeyboardButton("📱 Открыть SMS и отправить Тохе", url=sms_link))
                bot.send_message(chat_id,
                                 f"💬 Нажми кнопку — откроется SMS с текстом для Тохи:\n«{text}»",
                                 reply_markup=markup)
            else:
                handle_sheet_command(chat_id, text, mode)
        else:
            # Любой не распознанный текст → Grok
            _ask_grok_and_route(chat_id, text)


def handle_sheet_command(chat_id, text, mode):
    parts = text.strip().split(" ", 2)
    try:
        if mode == "read":
            if len(parts) < 2:
                bot.send_message(chat_id, "⚠️ Нужно указать ID таблицы и диапазон через пробел.")
                return
            sheet_id, range_name = parts[0], parts[1]
            bot.send_message(chat_id, "⏳ Читаю таблицу...")
            values = get_values(sheet_id, range_name)
            result = format_table(values)
            bot.send_message(chat_id, f"📊 Данные ({range_name}):\n\n`{result}`",
                             parse_mode="Markdown", reply_markup=sheets_menu())

        elif mode == "write":
            if len(parts) < 3:
                bot.send_message(chat_id, "⚠️ Нужно указать ID таблицы, диапазон и данные.")
                return
            sheet_id, range_name, data = parts[0], parts[1], parts[2]
            bot.send_message(chat_id, "⏳ Записываю в таблицу...")
            append_values(sheet_id, range_name, [[data]])
            bot.send_message(chat_id, f"✅ Записано!\nДиапазон: {range_name}\nДанные: {data}",
                             reply_markup=sheets_menu())

        elif mode == "info":
            sheet_id = parts[0]
            bot.send_message(chat_id, "⏳ Получаю информацию...")
            info = get_sheet_info(sheet_id)
            title = info.get("properties", {}).get("title", "Неизвестно")
            sheets = info.get("sheets", [])
            sheet_names = [s.get("properties", {}).get("title", "") for s in sheets]
            bot.send_message(chat_id,
                             f"ℹ️ *Таблица:* {title}\n"
                             f"*Листов:* {len(sheets)}\n"
                             f"*Листы:* {', '.join(sheet_names)}",
                             parse_mode="Markdown", reply_markup=sheets_menu())

        elif mode == "save_sheet":
            if len(parts) < 2:
                bot.send_message(chat_id, "⚠️ Нужно: название и ID через пробел.\nПример: `Продажи 1BxiMVs0XRA5n...`",
                                 parse_mode="Markdown")
                return
            name, sheet_id = parts[0], parts[1]
            register_sheet(name, sheet_id)
            bot.send_message(chat_id,
                             f"✅ Таблица *{name}* сохранена!\n\nТеперь скажи «Аналитика таблицы» и выбери *{name}*.",
                             parse_mode="Markdown", reply_markup=sheets_menu())

        elif mode == "analytics":
            name = text.strip().lower()
            sheet_id = find_sheet_id(name)
            if not sheet_id:
                saved = list_sheets()
                names = "\n".join([f"• {n}" for n in saved.keys()]) if saved else "нет сохранённых"
                bot.send_message(chat_id,
                                 f"⚠️ Таблица «{text.strip()}» не найдена.\n\nДоступные:\n{names}",
                                 reply_markup=sheets_menu())
                return
            bot.send_message(chat_id, f"🤖 Grok анализирует «{text.strip()}»... Это займёт 10-20 секунд.")
            bot.send_chat_action(chat_id, "typing")
            result = analyze_sheet_with_ai(sheet_id)
            safe_send(chat_id, result, reply_markup=sheets_menu())

    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Ошибка: {str(e)}", reply_markup=main_menu())


@bot.message_handler(commands=['forget'])
def cmd_forget(message):
    chat_id = message.chat.id
    if not is_allowed(chat_id):
        return
    grok_history.pop(chat_id, None)
    _save_grok_history()
    bot.send_message(chat_id, "🗑️ История разговора очищена. Начинаем с нуля!", reply_markup=main_menu())


@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    if message.from_user and message.from_user.username:
        known_users[message.from_user.username.lower()] = chat_id
        _save_known_users()
        print(f"✅ Запомнил пользователя @{message.from_user.username} → {chat_id}")
    if not is_allowed(chat_id):
        bot.send_message(chat_id, "🔒 Введи секретный код для доступа:",
                         reply_markup=types.ReplyKeyboardRemove())
        return
    role = get_role(chat_id)
    if chat_id == OWNER_ID:
        set_role(chat_id, "owner")
        history = grok_history.get(chat_id, [])
        mem_note = f"\n_Помню {len(history) // 2} сообщений из прошлого разговора._" if history else ""
        bot.send_message(
            chat_id,
            f"👋 Привет, Руслан! Я твой личный AI-ассистент 🔥\n\n"
            f"Пиши или говори что нужно — позвоню, проанализирую, найду транзакции, "
            f"открою таблицу. Просто скажи.{mem_note}",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
    elif role == "driver":
        bot.send_message(chat_id, "👋 Привет! Нажми кнопку чтобы узнать где Руслан.", reply_markup=driver_menu())
    elif role == "worker":
        bot.send_message(chat_id, "👋 Привет! Здесь ты можешь смотреть аналитику таблиц.", reply_markup=worker_menu())
    else:
        bot.send_message(chat_id, "✅ Ты уже в системе. Ожидай — Руслан назначит тебе доступ.",
                         reply_markup=types.ReplyKeyboardRemove())


@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    chat_id = message.chat.id
    if not is_allowed(chat_id):
        return
    bot.send_message(chat_id, "🎤 Расшифровываю...")
    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)
        audio_file = io.BytesIO(downloaded)
        audio_file.name = "voice.ogg"
        transcript = openai_client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file,
            response_format="json",
        )
        text = transcript.text
        bot.send_message(chat_id, f"🗣️ Ты сказал: «{text}»")
        process_text(chat_id, text)
    except Exception as e:
        print(f"Ошибка расшифровки: {e}")
        bot.send_message(chat_id, "⚠️ Не удалось расшифровать голосовое. Попробуй ещё раз.")


def make_sms_link(phone: str, text: str) -> str:
    """Создать https ссылку на страницу-редирект которая откроет SMS приложение"""
    domain = os.environ.get("REPLIT_DEV_DOMAIN", "localhost")
    clean_phone = phone.replace(" ", "").replace("-", "")
    return f"https://{domain}/api/sms?to={quote(clean_phone)}&body={quote(text)}"


def build_location_markup(lat, lon, is_live=False):
    maps_link = f"https://maps.google.com/?q={lat},{lon}"
    toha_number = os.environ.get("TOHA_PHONE_NUMBER", "")
    label = "Живое гео" if is_live else "Гео"
    sms_text = f"{label} Руслана: {maps_link}"
    sms_link = make_sms_link(toha_number, sms_text)

    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("🗺️ Открыть в Google картах", url=maps_link))
    markup.row(types.InlineKeyboardButton("📱 Отправить SMS Тохе", url=sms_link))
    markup.row(types.InlineKeyboardButton("🚕 Отправить через Twilio", callback_data="send_geo_toha"))
    return markup, maps_link


@bot.message_handler(content_types=['location'])
def handle_location(message):
    chat_id = message.chat.id
    if not is_allowed(chat_id):
        return
    lat = message.location.latitude
    lon = message.location.longitude
    is_live = bool(message.location.live_period)

    last_location[chat_id] = (lat, lon)
    markup, maps_link = build_location_markup(lat, lon, is_live)

    if is_live:
        mins = message.location.live_period // 60
        bot.send_message(
            chat_id,
            f"🔴 *Живая геолокация запущена!*\nТранслируется {mins} минут\n\n"
            f"Координаты обновляются автоматически.\n📍 {maps_link}",
            parse_mode="Markdown",
            reply_markup=markup
        )
    else:
        bot.send_message(
            chat_id,
            f"📍 Геопозиция сохранена!\nШирота: {lat}\nДолгота: {lon}",
            reply_markup=markup
        )


@bot.edited_message_handler(content_types=['location'])
def handle_live_location_update(message):
    """Обновления живой геолокации"""
    chat_id = message.chat.id
    if message.location is None:
        return

    lat = message.location.latitude
    lon = message.location.longitude
    last_location[chat_id] = (lat, lon)

    # Тихо обновляем координаты, без спама сообщениями
    print(f"📍 Обновление live location от {chat_id}: {lat}, {lon}")


@bot.callback_query_handler(func=lambda call: call.data == "send_geo_toha")
def callback_send_geo(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    if chat_id in last_location:
        lat, lon = last_location[chat_id]
        maps_link = f"https://maps.google.com/?q={lat},{lon}"
        toha_number = os.environ.get("TOHA_PHONE_NUMBER", "")
        sms_link = make_sms_link(toha_number, f"Гео Руслана: {maps_link}")
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("📱 Открыть SMS и отправить Тохе", url=sms_link))
        markup.row(types.InlineKeyboardButton("🗺️ Открыть в картах", url=maps_link))
        bot.send_message(chat_id,
                         f"📍 Нажми кнопку — откроется SMS приложение с геопозицией:\n{maps_link}",
                         reply_markup=markup)
    else:
        bot.send_message(chat_id, "⚠️ Геопозиция не найдена. Сначала отправь своё гео.", reply_markup=main_menu())


def process_worker(chat_id: int, text: str):
    """Обработка команд для рабочего — аналитика + звонок Руслану"""
    t = text.lower()
    # ── Ожидаем текст для звонка ──────────────────────
    if chat_id in waiting_for_call_msg:
        waiting_for_call_msg.pop(chat_id)
        owner_phone = os.environ.get("RUSLAN_PHONE_NUMBER", "")
        if not owner_phone:
            bot.send_message(chat_id, "⚠️ Номер Руслана не настроен. Обратись напрямую.", reply_markup=worker_menu())
            return
        username = ""
        for u, cid in known_users.items():
            if cid == chat_id:
                username = u
                break
        say_text = f"Привет Руслан, твой рабочий {username} говорит: {text}"
        bot.send_message(chat_id, "📞 Звоню Руслану...")
        ok, info = make_call(owner_phone, say_text)
        if ok:
            bot.send_message(chat_id, "✅ Позвонил! Руслан услышит твоё сообщение.", reply_markup=worker_menu())
            # Уведомление Руслану в Telegram
            bot.send_message(OWNER_ID,
                             f"📞 *Рабочий @{username} звонит тебе!*\n\nСообщение: _{text}_",
                             parse_mode="Markdown")
        else:
            bot.send_message(chat_id, f"❌ Не удалось позвонить: {info}", reply_markup=worker_menu())
        return
    # ── Кнопка звонка ────────────────────────────────
    if "позвонить руслану" in t or "📞" in t:
        bot.send_message(chat_id, "✍️ Напиши сообщение — я позвоню Руслану и скажу его голосом:")
        waiting_for_call_msg[chat_id] = True
        return
    # ── Аналитика ────────────────────────────────────
    if "аналитика" in t or "📊" in t or "статистика таблиц" in t or "сводк" in t:
        saved = list_sheets()
        if not saved:
            bot.send_message(chat_id, "📊 Нет сохранённых таблиц. Обратись к Руслану.", reply_markup=worker_menu())
        else:
            names = "\n".join([f"• {name}" for name in saved.keys()])
            bot.send_message(chat_id, f"📊 Выбери таблицу:\n\n{names}", reply_markup=worker_menu())
            waiting_for_sheet_id[chat_id] = "analytics"
    elif "мои таблицы" in t or "📋" in t:
        saved = list_sheets()
        if not saved:
            bot.send_message(chat_id, "Нет таблиц. Обратись к Руслану.", reply_markup=worker_menu())
        else:
            names = "\n".join([f"• *{name}*" for name in saved.keys()])
            bot.send_message(chat_id, f"📋 *Доступные таблицы:*\n\n{names}",
                             parse_mode="Markdown", reply_markup=worker_menu())
    elif chat_id in waiting_for_sheet_id and waiting_for_sheet_id[chat_id] == "analytics":
        waiting_for_sheet_id.pop(chat_id)
        name = text.strip().lower()
        sheet_id = find_sheet_id(name)
        if not sheet_id:
            saved = list_sheets()
            names = "\n".join([f"• {n}" for n in saved.keys()]) if saved else "нет"
            bot.send_message(chat_id, f"⚠️ Таблица «{text.strip()}» не найдена.\n\nДоступные:\n{names}",
                             reply_markup=worker_menu())
            return
        bot.send_message(chat_id, f"🤖 Grok анализирует «{text.strip()}»... Подожди 10-20 секунд.")
        bot.send_chat_action(chat_id, "typing")
        result = analyze_sheet_with_ai(sheet_id)
        safe_send(chat_id, result, reply_markup=worker_menu())
    else:
        bot.send_message(chat_id, "Нажми кнопку 👇", reply_markup=worker_menu())


def process_driver(chat_id: int, text: str):
    """Обработка команд для водителя"""
    t = text.lower()
    if "геопозиция руслана" in t or "📍 геопозиция руслана" in t or "где руслан" in t:
        owner_loc = last_location.get(OWNER_ID)
        if owner_loc:
            lat, lon = owner_loc
            maps_link = f"https://maps.google.com/?q={lat},{lon}"
            markup = types.InlineKeyboardMarkup()
            markup.row(types.InlineKeyboardButton("🗺️ Открыть в картах", url=maps_link))
            bot.send_message(chat_id,
                             f"📍 Руслан сейчас здесь:\n{maps_link}",
                             reply_markup=markup)
        else:
            bot.send_message(chat_id,
                             "📍 Руслан ещё не поделился геопозицией.\nПопробуй позже.",
                             reply_markup=driver_menu())
    else:
        bot.send_message(chat_id, "Нажми кнопку 👇", reply_markup=driver_menu())


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id = message.chat.id
    text = message.text or ""
    # Проверка секретного кода для незарегистрированных
    if not is_allowed(chat_id):
        if text.strip() == SECRET_CODE:
            grant_access(chat_id)
            bot.send_message(chat_id,
                             "✅ Код принят. Ожидай — Руслан назначит тебе доступ.",
                             reply_markup=types.ReplyKeyboardRemove())
        else:
            bot.send_message(chat_id, "🔒 Нет доступа.")
        return
    role = get_role(chat_id)
    if role == "driver":
        process_driver(chat_id, text)
    elif role == "worker":
        process_worker(chat_id, text)
    elif role == "guest" and chat_id != OWNER_ID:
        bot.send_message(chat_id,
                         "⏳ Ожидай — Руслан ещё не назначил тебе роль.",
                         reply_markup=types.ReplyKeyboardRemove())
    else:
        process_text(chat_id, text)


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)

    if call.data == "add_sheet":
        bot.send_message(chat_id,
                         "➕ Отправь название и ID таблицы через пробел:\n\n"
                         "Пример: `Продажи 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms`\n\n"
                         "ID — часть ссылки после /d/ в адресе таблицы",
                         parse_mode="Markdown")
        waiting_for_sheet_id[chat_id] = "save_sheet"

    elif call.data == "analytics_menu":
        saved = list_sheets()
        if not saved:
            bot.send_message(chat_id, "📊 Нет таблиц. Сначала добавь таблицу.")
            return
        names = "\n".join([f"• {name}" for name in saved.keys()])
        bot.send_message(chat_id, f"📊 Напиши название таблицы для анализа:\n\n{names}",
                         parse_mode="Markdown")
        waiting_for_sheet_id[chat_id] = "analytics"

    elif call.data.startswith("open_sheet:"):
        sheet_id = call.data.split(":", 1)[1]
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📄 Открыть таблицу", url=url))
        bot.send_message(chat_id, "Нажми чтобы открыть:", reply_markup=markup)


if __name__ == "__main__":
    keep_alive()
    print("🚀 Ruslan Personal Helper с SMS для Тохи!")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Ошибка polling: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)
