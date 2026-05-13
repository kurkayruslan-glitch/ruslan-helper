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
from analytics import analyze_sheet_data, register_sheet, find_sheet_id, list_sheets
from roles import get_role, set_role, list_roles

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

# Последняя геопозиция пользователя
last_location = {}

# Состояние ожидания ID таблицы
waiting_for_sheet_id = {}

# Известные пользователи: username (без @) → chat_id
known_users = {}


def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("📊 Статистика Я Тигр", "🛣️ Маршрут")
    markup.add("📋 ФОП Отчёт", "📍 Геопозиция")
    markup.add("🚕 Тоха", "❓ Что ты можешь?")
    markup.add("📗 Google Таблицы")
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


def get_menu_for_role(role: str):
    if role == "driver":
        return driver_menu()
    return main_menu()


def is_toha_geo_command(text: str) -> bool:
    """Распознать голосовую команду отправки гео Тохе"""
    t = text.lower()
    keywords = ["отправь тоше", "отправь тохе", "скинь тоше", "скинь тохе",
                "пошли тоше", "пошли тохе", "отправь гео тох", "скинь гео тох",
                "тоха гео", "тоше гео", "тохе гео"]
    return any(k in t for k in keywords)


def process_text(chat_id, text):
    t = text.lower()

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
        bot.send_message(chat_id, "📗 *Google Таблицы*\n\nЧто хочешь сделать?",
                         parse_mode="Markdown", reply_markup=sheets_menu())

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
        if chat_id in waiting_for_sheet_id:
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
            bot.send_message(chat_id,
                             f"✅ Принял: «{text}»\n\nЧто нужно сделать дальше?",
                             reply_markup=main_menu())


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
            bot.send_message(chat_id, f"⏳ Анализирую таблицу «{text.strip()}»...")
            result = analyze_sheet_data(sheet_id)
            # Разбиваем на части если текст слишком длинный
            if len(result) > 4000:
                chunks = [result[i:i+4000] for i in range(0, len(result), 4000)]
                for chunk in chunks:
                    bot.send_message(chat_id, chunk, parse_mode="Markdown", reply_markup=sheets_menu())
            else:
                bot.send_message(chat_id, result, parse_mode="Markdown", reply_markup=sheets_menu())

    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Ошибка: {str(e)}", reply_markup=main_menu())


@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    if message.from_user and message.from_user.username:
        known_users[message.from_user.username.lower()] = chat_id
        print(f"✅ Запомнил пользователя @{message.from_user.username} → {chat_id}")
    if not is_allowed(chat_id):
        bot.send_message(chat_id, "🔒 Введи секретный код для доступа:")
        return
    role = get_role(chat_id)
    if role == "driver":
        bot.send_message(chat_id, "👋 Привет! Нажми кнопку чтобы узнать где Руслан.", reply_markup=driver_menu())
    elif chat_id == OWNER_ID:
        set_role(chat_id, "owner")
        bot.send_message(chat_id, "👋 Привет, Руслан! Я твой личный помощник 🔥", reply_markup=main_menu())
    else:
        bot.send_message(chat_id, "👋 Привет! Чем могу помочь?", reply_markup=main_menu())


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
            role = get_role(chat_id)
            bot.send_message(chat_id, "✅ Доступ открыт! Добро пожаловать.",
                             reply_markup=get_menu_for_role(role))
        else:
            bot.send_message(chat_id, "🔒 Нет доступа.")
        return
    role = get_role(chat_id)
    if role == "driver":
        process_driver(chat_id, text)
    else:
        process_text(chat_id, text)


if __name__ == "__main__":
    keep_alive()
    print("🚀 Ruslan Personal Helper с SMS для Тохи!")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Ошибка polling: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)
