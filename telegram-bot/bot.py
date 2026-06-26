# Загружаем переменные из .env (если есть python-dotenv и .env-файл)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import telebot
import sys
from telebot import types
import os
import time
import io
from urllib.parse import quote
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # на ПК без OpenAI-ключа просто не будет TTS
from keep_alive import keep_alive
from sheets import get_values, append_values, get_sheet_info, format_table
from sms import send_geo_to_toha, send_sms_to_toha, send_sms
from analytics import analyze_sheet_data, analyze_sheet_with_ai, register_sheet, find_sheet_id, list_sheets
from roles import get_role, set_role, list_roles
from calls import make_call
import pc_control
import pc_apps
import price_search
import crm
import sauron
import file_search
import sauron_file_search

# ──────────────────────────────────────────────────────────────────
# РЕЖИМ ЗАПУСКА
# development — workspace workflow (только пока открыт браузер)
# production  — Replit Reserved VM Deployment (24/7)
# Устанавливается автоматически через run command в Deployment:
#   cd telegram-bot && DEPLOYMENT_MODE=production python3 -u bot.py
# ──────────────────────────────────────────────────────────────────
DEPLOYMENT_MODE = os.environ.get("DEPLOYMENT_MODE", "development").lower()
IS_PRODUCTION = DEPLOYMENT_MODE == "production"

# Бэкенд ИИ: openai (ChatGPT), grok (xAI), gemini (Google), llama (Ollama).
# По умолчанию — openai. Переключается через LLM_BACKEND в .env.
_backend = os.environ.get("LLM_BACKEND", "openai").lower()
if _backend == "llama":
    from llama import ask_grok
    print("🧠 LLM backend: llama (Ollama)")
elif _backend == "grok":
    from grok import ask_grok
    _grok_key = os.environ.get("XAI_API_KEY", "")
    if _grok_key:
        print(f"🧠 LLM backend: grok (xAI), модель: {os.environ.get('GROK_MODEL','grok-3')}")
    else:
        print("🧠 LLM backend: grok (xAI) ⚠️  XAI_API_KEY не задан — бот запустится, но ИИ-ответы вернут ошибку.")
elif _backend == "gemini":
    from gemini import ask_grok
    print("🧠 LLM backend: gemini (Google)")
else:
    from chatgpt import ask_grok
    print("🧠 LLM backend: openai (ChatGPT)")
from memory import get_facts, add_fact, delete_fact, clear_facts, format_for_prompt, format_for_display, clear_all as clear_memory
from tron import get_usdt_transactions, get_account_balance, build_tx_summary
from reminders import add_reminder, get_due, mark_fired, mark_failed, list_pending, cancel_reminder
import tax_calendar
import sheet_monitor

import threading
import json
import re
from datetime import datetime, timedelta

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

def _ukraine_tz_hours() -> int:
    """UTC+3 летом (последнее вс марта — последнее вс октября), UTC+2 зимой.
    Если задан TZ_OFFSET_HOURS в .env — использует его (ручной override)."""
    manual = os.environ.get("TZ_OFFSET_HOURS", "").strip()
    if manual.lstrip("-").isdigit():
        return int(manual)
    now_utc = datetime.utcnow()
    y = now_utc.year
    dst_start = max(
        datetime(y, 3, d) for d in range(25, 32)
        if datetime(y, 3, d).weekday() == 6
    ).replace(hour=1)   # 01:00 UTC = 03:00 местного
    dst_end = max(
        datetime(y, 10, d) for d in range(25, 32)
        if datetime(y, 10, d).weekday() == 6
    ).replace(hour=1)   # 01:00 UTC = 03:00 местного
    return 3 if dst_start <= now_utc < dst_end else 2


def _now_local() -> datetime:
    """Текущее местное время (Украина, с учётом летнего/зимнего времени)."""
    return datetime.utcnow() + timedelta(hours=_ukraine_tz_hours())

OPENAI_BASE_URL = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
OPENAI_API_KEY = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")

# Клиент для текстового ИИ-чата — через Replit proxy (если есть) или прямой ключ
if OpenAI and OPENAI_API_KEY:
    openai_client = OpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
    )
else:
    openai_client = None

# Клиент ТОЛЬКО для голоса (STT/TTS) — исключительно прямой OPENAI_API_KEY.
# Replit AI proxy НЕ поддерживает audio-эндпоинты (whisper, tts-1) → UNSUPPORTED_MODEL.
_DIRECT_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
if OpenAI and _DIRECT_OPENAI_KEY:
    voice_openai_client = OpenAI(api_key=_DIRECT_OPENAI_KEY)
else:
    voice_openai_client = None  # голос недоступен; текстовый чат продолжает работать

bot = telebot.TeleBot(TOKEN)

# ──────────────────────────────────────────────
# БЕЗОПАСНОСТЬ — белый список
# ──────────────────────────────────────────────
OWNER_ID = 7959647798          # Руслан — всегда имеет доступ
# Секретный код берётся из .env (BOT_SECRET_CODE), fallback — на случай первого запуска
SECRET_CODE = os.environ.get("BOT_SECRET_CODE", "ruslan2024vip")
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


def _tts_send_voice(chat_id: int, text: str):
    """Конвертирует текст в голос через OpenAI TTS (прямой ключ) и отправляет голосовым."""
    if not voice_openai_client:
        return  # голос не настроен — текстовый ответ уже отправлен выше
    import re
    try:
        clean = re.sub(r"\[ACTION:[^\]]*\]", "", text)
        clean = re.sub(r"[*_`#>~]", "", clean).strip()
        if len(clean) > 3800:
            clean = clean[:3800] + "…"
        if not clean:
            return
        response = voice_openai_client.audio.speech.create(
            model="tts-1",
            voice="onyx",
            input=clean,
            response_format="opus",
        )
        audio_bytes = io.BytesIO(response.content)
        audio_bytes.name = "reply.ogg"
        bot.send_voice(chat_id, audio_bytes)
    except Exception as e:
        print(f"TTS ошибка: {e}")
        # Graceful fallback — текстовый ответ уже отправлен выше

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

# Ожидание поискового запроса для Sauron
waiting_for_sauron_query: set = set()

# Поиск по файлу через Sauron
waiting_for_file_sauron: set = set()          # chat_id ожидает отправки файла
file_sauron_pending: dict = {}                 # chat_id → {"records": [...], "filename": str}

# Чаты в режиме свободного ИИ-диалога («🤖 ИИ чат»)
ai_chat_mode: set = set()

# Чаты в режиме Jarvis — командный центр
jarvis_mode: set = set()

# Чаты в режиме тишины — короткие ответы без подсказок
silence_mode: set = set()

# Журнал действий раздела «Мой ПК» (in-memory, последние 200 записей)
remote_action_log: list = []

# Чаты, ожидающие подтверждения опасной команды ПК (shutdown/restart)
waiting_remote_confirm: set = set()
_remote_confirm_action: dict = {}  # chat_id → "shutdown" | "restart"

# Чаты, ожидающие голосового ответа (запрос пришёл голосом)
voice_request_chats: set = set()

# История чата — сохраняется на диск, максимум HISTORY_MAX пар на пользователя
GROK_HISTORY_FILE = "grok_history.json"
HISTORY_MAX = 40  # максимум сообщений в истории (пар user+assistant = 80 записей)

def _load_grok_history() -> dict:
    if os.path.exists(GROK_HISTORY_FILE):
        try:
            import json
            with open(GROK_HISTORY_FILE) as f:
                return {int(k): v for k, v in json.load(f).items()}
        except Exception:
            pass
    return {}

def _trim_history(history: list) -> list:
    """Оставляет последние HISTORY_MAX сообщений, не обрезая пару user/assistant."""
    if len(history) <= HISTORY_MAX:
        return history
    trimmed = history[-HISTORY_MAX:]
    # Если первый элемент — assistant (не user), убираем его, чтобы не начинать с ответа
    if trimmed and trimmed[0].get("role") == "assistant":
        trimmed = trimmed[1:]
    return trimmed

def _save_grok_history():
    import json
    # Обрезаем перед записью — так файл не растёт бесконечно
    trimmed = {str(k): _trim_history(v) for k, v in grok_history.items()}
    with open(GROK_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)

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
    """Главное меню — красиво, чисто, без лишнего."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("⚡ Jarvis",       "🩺 Статус")
    markup.add("💬 Поговорить",   "🎤 Голос")
    markup.add("📞 Звонок",       "🏨 Бронирование")
    markup.add("📋 Задачи",       "🚕 Тоха")
    markup.add("💻 Мой ПК",       "🎮 Dota 2")
    markup.add("🔍 Саурон",       "📁 Файл → Саурон")
    markup.add("📊 Я Тигр",       "📋 ФОП")
    return markup


def ai_chat_menu():
    """Клавиатура внутри режима разговора — только выход."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("🔙 Выйти из чата")
    return markup


def jarvis_menu():
    """Командный центр Jarvis — все функции под рукой."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("☀️ Бриф",          "🩺 Статус")
    markup.add("💬 Поговорить",     "📞 Звонок")
    markup.add("📋 Задачи",         "🧠 Память")
    markup.add("📊 Таблицы",        "🚕 Тоха")
    markup.add("💻 Мой ПК",         "🎮 Dota 2")
    markup.add("📋 ФОП",            "💸 Расход ИИ")
    markup.add("🧠 Что умею?",      "🔙 Выйти из Jarvis")
    return markup


def remote_access_menu():
    """Меню раздела «Мой ПК» — только стандартные легальные инструменты."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("🖥 Статус ПК",       "🔑 ID подключения")
    markup.add("🚀 Запустить AnyDesk", "🚀 Запустить TeamViewer")
    markup.add("🌐 Chrome Remote Desktop")
    markup.add("📖 Инструкции по настройке")
    markup.add("📄 Журнал действий ПК", "🔒 Безопасность")
    markup.add("🔙 Назад")
    return markup


def remote_instructions_menu():
    """Меню выбора приложения для инструкций."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("📖 AnyDesk — инструкция")
    markup.add("📖 TeamViewer — инструкция")
    markup.add("📖 Chrome Remote Desktop — инструкция")
    markup.add("🔙 Назад к ПК")
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
    # Устойчиво к ведущим пробелам/переносам строки перед тегом
    stripped = reply.lstrip()
    # 1) Канонический формат: [ACTION:type:param]
    match = re.search(r"\[ACTION:([^\]]+)\]\s*", stripped)
    if match:
        tag_content = match.group(1)
        text_after = (stripped[:match.start()] + stripped[match.end():]).strip()
        parts = tag_content.split(":", 1)
        action_type = parts[0].strip()
        action_param = parts[1].strip() if len(parts) > 1 else None
        return action_type, action_param, text_after
    # 2) Терпимый формат без скобок: ACTION:type[:param] — Llama часто их забывает
    match = re.search(r"(?<![A-Za-z_])ACTION:([A-Za-z_]+)(?::([^\n\r]*))?", stripped)
    if match:
        action_type = match.group(1).strip()
        action_param = (match.group(2) or "").strip() or None
        text_after = (stripped[:match.start()] + stripped[match.end():]).strip()
        # Если параметр не указан, а в ответе есть осмысленный текст —
        # для call_wife/call_restaurant используем сам текст как сообщение.
        if not action_param and action_type in ("call_wife",) and text_after:
            action_param = text_after
            text_after = ""
        return action_type, action_param, text_after
    return None, None, reply


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
        if not toha_number:
            bot.send_message(chat_id, "⚠️ Номер Тохи не настроен в системе.")
        elif not msg:
            bot.send_message(chat_id, "⚠️ Текст SMS не указан — напиши что передать Тохе.")
        else:
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

    elif action_type == "list_users":
        roles_data = list_roles()
        if not known_users:
            bot.send_message(chat_id, "👥 Пока никто не запускал бота.")
        else:
            role_names = {"owner": "Владелец", "driver": "Водитель", "worker": "Работник", "guest": "Гость"}
            lines = [f"• @{u} — {role_names.get(roles_data.get(str(uid), 'guest'), 'Гость')}"
                     for u, uid in known_users.items()]
            bot.send_message(chat_id, "👥 *Пользователи бота:*\n\n" + "\n".join(lines),
                             parse_mode="Markdown")

    elif action_type == "assign_role":
        if not action_param or ":" not in action_param:
            bot.send_message(chat_id, "⚠️ Укажи @username и роль. Пример: назначь @toha водителем")
            return
        username_raw, role = action_param.split(":", 1)
        username = username_raw.lstrip("@").lower().strip()
        role = role.strip()
        valid_roles = {"driver", "worker", "guest", "owner"}
        if role not in valid_roles:
            bot.send_message(chat_id, f"⚠️ Неверная роль «{role}». Допустимые: driver, worker, guest.")
            return
        if username not in known_users:
            bot.send_message(chat_id, f"⚠️ @{username} ещё не запускал бота. Попроси его написать /start.")
            return
        target_id = known_users[username]
        grant_access(target_id)
        set_role(target_id, role)
        role_labels = {"driver": "Водитель", "worker": "Работник", "guest": "Гость", "owner": "Владелец"}
        bot.send_message(chat_id, f"✅ @{username} назначен как *{role_labels.get(role, role)}*.",
                         parse_mode="Markdown")

    elif action_type == "remember":
        fact = action_param or ""
        if fact:
            added = add_fact(fact)
            if added:
                bot.send_message(chat_id, f"🧠 Запомнил: «{fact}»")
        # Если fact пустой или дубликат — молчим, это фоновое действие

    elif action_type == "recall":
        facts = get_facts()
        if not facts:
            bot.send_message(chat_id, "🧠 Пока ничего не запомнено. Скажи мне что запомнить!")
        else:
            lines = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(facts))
            bot.send_message(chat_id, f"🧠 *Запомненные факты:*\n\n{lines}", parse_mode="Markdown")

    elif action_type == "forget_fact":
        try:
            idx = int(action_param or "0") - 1
            removed = delete_fact(idx)
            if removed:
                bot.send_message(chat_id, f"🗑️ Забыл: «{removed}»")
            else:
                facts = get_facts()
                bot.send_message(chat_id, f"⚠️ Нет факта с номером {(idx + 1)}. Всего запомнено: {len(facts)}.")
        except (ValueError, TypeError):
            bot.send_message(chat_id, "⚠️ Укажи номер факта. Например: забудь факт 2")

    elif action_type == "forget_all_facts":
        clear_facts()
        bot.send_message(chat_id, "🗑️ Вся долгосрочная память очищена.")

    elif action_type == "forget":
        grok_history.pop(chat_id, None)
        _save_grok_history()
        bot.send_message(chat_id, "🗑️ История разговора очищена. Начинаем с чистого листа!", reply_markup=main_menu())

    elif action_type == "remind":
        # action_param: "YYYY-MM-DDTHH:MM|текст напоминания"
        if not action_param or "|" not in action_param:
            bot.send_message(chat_id, "⚠️ Не удалось разобрать время напоминания. Попробуй уточнить: «напомни мне завтра в 9 утра проверить таблицу».")
            return
        dt_str, reminder_text = action_param.split("|", 1)
        dt_str = dt_str.strip()
        reminder_text = reminder_text.strip()
        try:
            fire_at = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M")
        except ValueError:
            bot.send_message(chat_id, f"⚠️ Неверный формат даты: «{dt_str}». Попробуй ещё раз.")
            return
        if fire_at <= _now_local():
            bot.send_message(chat_id, "⚠️ Время напоминания уже прошло. Укажи будущее время.")
            return
        reminder = add_reminder(chat_id, reminder_text, fire_at)
        friendly_time = fire_at.strftime("%d.%m.%Y в %H:%M")
        bot.send_message(chat_id, f"⏰ Запомнил! Напомню тебе *{friendly_time}*:\n_{reminder_text}_",
                         parse_mode="Markdown")

    elif action_type == "list_reminders":
        pending = list_pending(chat_id)
        if not pending:
            bot.send_message(chat_id, "⏰ Нет предстоящих напоминаний.")
        else:
            lines = []
            for r in pending:
                try:
                    fire_at = datetime.strptime(r["fire_at"], "%Y-%m-%dT%H:%M")
                    when = fire_at.strftime("%d.%m.%Y в %H:%M")
                except Exception:
                    when = r.get("fire_at", "?")
                lines.append(f"• `{r.get('id','?')}` — {when} — {r['text']}")
            bot.send_message(chat_id, "⏰ *Предстоящие напоминания:*\n\n" + "\n".join(lines) +
                             "\n\n_Чтобы отменить, скажи «отмени напоминание <id>» или просто «отмени про …»._",
                             parse_mode="Markdown")

    elif action_type == "show_code":
        _send_code_file(chat_id, action_param or "")

    elif action_type == "list_code":
        _send_code_list(chat_id)

    elif action_type == "open_url":
        safe_send(chat_id, pc_control.open_url(action_param or ""))

    elif action_type == "search_files":
        safe_send(chat_id, pc_control.search_files(action_param or "", by_content=False))

    elif action_type == "search_content":
        safe_send(chat_id, pc_control.search_files(action_param or "", by_content=True))

    elif action_type == "screenshot":
        safe_send(chat_id, "📸 Снимаю экран…")
        ok, info = pc_control.take_screenshot()
        if ok:
            try:
                with open(info, "rb") as f:
                    bot.send_photo(chat_id, f, caption="🖥️ Скриншот твоего ПК")
            except Exception as e:
                safe_send(chat_id, f"❌ Не получилось отправить скриншот: {e}")
        else:
            safe_send(chat_id, info)

    elif action_type == "screenshot_site":
        url = (action_param or "").strip()
        safe_send(chat_id, f"📸 Делаю скриншот сайта {url}…")
        ok, info = pc_control.screenshot_site(url)
        if ok:
            try:
                with open(info, "rb") as f:
                    bot.send_photo(chat_id, f, caption=f"🌐 {url}")
            except Exception as e:
                safe_send(chat_id, f"❌ Не получилось отправить скриншот: {e}")
        else:
            safe_send(chat_id, info)

    elif action_type == "crm_expense":
        raw = (action_param or "").strip()
        parts = [p.strip() for p in raw.split(":")]
        while len(parts) < 3:
            parts.append("")
        amount_s, currency, description = parts[0], parts[1] or "USDT", parts[2]
        date = parts[3] if len(parts) > 3 else ""
        if not amount_s or not description:
            safe_send(chat_id, "❌ Не хватает данных. Формат: сумма:валюта:описание[:дата]")
            return
        ok, info = crm.add_expense(amount_s, description, currency, date=date)
        safe_send(chat_id, info)

    elif action_type == "open_folder":
        safe_send(chat_id, pc_control.open_folder(action_param or ""))

    elif action_type == "launch_app":
        safe_send(chat_id, pc_apps.launch_app(action_param or ""))

    elif action_type == "close_app":
        safe_send(chat_id, pc_apps.close_app(action_param or ""))

    elif action_type == "list_apps":
        safe_send(chat_id, pc_apps.list_apps())

    elif action_type == "search_sauron":
        query = (action_param or "").strip()
        if not query:
            safe_send(chat_id, "🔍 Что искать на Sauron? Укажи ФИО, номер или другой запрос.")
            return
        bot.send_message(chat_id, f"🔍 Ищу «{query}» на Sauron…")
        try:
            result = sauron.search(query)
        except Exception as e:
            result = f"❌ Ошибка Sauron: {str(e)[:200]}"
        safe_send(chat_id, result)

    elif action_type == "search_price":
        safe_send(chat_id, "🔎 Ищу цены, секунду…")
        try:
            result = price_search.search_prices(action_param or "")
        except Exception as e:
            result = f"❌ Поиск упал: {str(e)[:200]}"
        safe_send(chat_id, result)

    elif action_type == "send_sms":
        raw = (action_param or "").strip()
        if ":" in raw:
            phone, message = raw.split(":", 1)
        else:
            phone, message = raw, ""
        phone = phone.strip().lower()
        message = message.strip()
        aliases = {
            "тоха": os.environ.get("TOHA_PHONE_NUMBER", ""),
            "toha": os.environ.get("TOHA_PHONE_NUMBER", ""),
            "жена": os.environ.get("WIFE_PHONE_NUMBER", ""),
            "wife": os.environ.get("WIFE_PHONE_NUMBER", ""),
        }
        if phone in aliases:
            phone = aliases[phone]
        if not phone:
            safe_send(chat_id, "❌ Не указан номер получателя SMS.")
            return
        if not message:
            safe_send(chat_id, "❌ Пустой текст SMS — скажи, что написать.")
            return
        ok, info = send_sms(phone, message)
        if ok:
            safe_send(chat_id, f"✅ SMS отправил на {phone}\n💬 «{message}»")
        else:
            safe_send(chat_id, f"❌ Не получилось отправить SMS: {info}")

    elif action_type == "call_wife":
        message = (action_param or "").strip()
        wife_number = os.environ.get("WIFE_PHONE_NUMBER", "")
        if not wife_number:
            safe_send(chat_id, "❌ Номер жены не настроен. Добавь WIFE_PHONE_NUMBER в .env (формат +380XXXXXXXXX) и перезапусти бота.")
            return
        if not message:
            safe_send(chat_id, "❌ Не понял, что передать жене. Скажи: «позвони жене и скажи …».")
            return
        ok, info = make_call(wife_number, message)
        if ok:
            safe_send(chat_id, f"📞 Звоню жене ({wife_number})\n💬 Скажу: «{message}»")
        else:
            safe_send(chat_id, f"❌ Не получилось дозвониться жене: {info}")

    elif action_type == "call_restaurant":
        raw = (action_param or "").strip()
        if ":" in raw:
            phone, message = raw.split(":", 1)
        else:
            phone, message = raw, "Здравствуйте, хочу забронировать столик. Перезвоните, пожалуйста."
        phone = phone.strip()
        message = message.strip() or "Здравствуйте, хочу забронировать столик."
        if not phone:
            safe_send(chat_id, "❌ Не указан номер ресторана.")
        else:
            ok, info = make_call(phone, message)
            if ok:
                safe_send(chat_id, f"📞 Звоню в ресторан {phone}\n💬 Скажу: «{message}»")
            else:
                safe_send(chat_id, f"❌ Не получилось дозвониться: {info}")

    elif action_type == "cancel_reminder":
        reminder_id = (action_param or "").strip()
        if not reminder_id:
            bot.send_message(chat_id, "⚠️ Не понял, какое напоминание отменить. Назови его id или скажи «покажи напоминания».")
            return
        # Match only against this chat's pending reminders to avoid cross-chat cancellation
        pending = list_pending(chat_id)
        target = next((r for r in pending if r.get("id") == reminder_id), None)
        if not target:
            bot.send_message(chat_id, f"⚠️ Не нашёл активное напоминание с id `{reminder_id}`.", parse_mode="Markdown")
            return
        if cancel_reminder(reminder_id):
            try:
                fire_at = datetime.strptime(target["fire_at"], "%Y-%m-%dT%H:%M")
                when = fire_at.strftime("%d.%m.%Y в %H:%M")
            except Exception:
                when = target.get("fire_at", "?")
            bot.send_message(chat_id, f"🗑️ Отменил напоминание на *{when}*:\n_{target.get('text','')}_",
                             parse_mode="Markdown")
        else:
            bot.send_message(chat_id, f"⚠️ Не удалось отменить напоминание `{reminder_id}`.", parse_mode="Markdown")


def _extract_remember_tags(text: str) -> tuple[list, str]:
    """Извлекает [REMEMBER:факт] и [ACTION:remember:факт] теги из текста."""
    import re
    facts_r = re.findall(r"\[REMEMBER:([^\]]+)\]", text, flags=re.IGNORECASE)
    facts_a = re.findall(r"\[ACTION:remember:([^\]]+)\]", text)
    all_facts = facts_r + facts_a
    clean = re.sub(r"\[REMEMBER:[^\]]+\]\s*", "", text, flags=re.IGNORECASE)
    clean = re.sub(r"\[ACTION:remember:[^\]]+\]\s*", "", clean).strip()
    return all_facts, clean


def _ask_grok_and_route(chat_id: int, text: str):
    """Отправляет сообщение в Grok, разбирает ACTION-теги и REMEMBER-теги, показывает ответ."""
    history = grok_history.get(chat_id, [])
    memory_block = format_for_prompt()
    # Добавляем текущую дату и время, чтобы Grok правильно рассчитывал относительные сроки
    now = _now_local()
    tz = _ukraine_tz_hours()
    date_line = f"\nСейчас: {now.strftime('%Y-%m-%dT%H:%M')} (UTC+{tz}, Украина).\n"
    memory_block = date_line + memory_block
    # Базовая личность — всегда активна
    personality = (
        "Ты — Jarvis, персональный ИИ-ассистент Руслана. "
        "Руслан — украинец, владелец такси-бизнеса «Я Тигр» (Украина, 2024–2025). "
        "Он занятой человек: управляет водителями, клиентами, финансами. "
        "Общайся на русском языке, дружелюбно но чётко. "
        "Без лишней воды, без извинений, без вступлений «Конечно!», «Отличный вопрос!». "
        "Отвечай умно, коротко, по делу — как лучший друг и эксперт одновременно. "
        "Если чего-то не знаешь — говори прямо и предлагай альтернативу. "
        "Никогда не проси пароли, коды SMS, токены или личные данные. "
        "Для поиска информации о людях, телефонах, адресах через sauron.info — используй "
        "[ACTION:search_sauron:запрос]. Пример: если Руслан говорит «найди в Sauron Иванова Петра» — "
        "ответь [ACTION:search_sauron:Иванов Петр]. Никогда не проси логин/пароль от Sauron — "
        "они берутся из настроек бота автоматически. "
    )
    memory_block = "\n" + personality + "\n" + memory_block

    # Jarvis mode — командный центр, ещё более чёткий стиль
    if chat_id in jarvis_mode:
        memory_block = (
            "[КОМАНДНЫЙ ЦЕНТР JARVIS АКТИВЕН]\n"
            "Ты управляешь всеми функциями бота. "
            "Отвечай как Iron Man's Jarvis: лаконично, умно, с уверенностью. "
            "При необходимости сам предлагай следующий шаг. "
        ) + memory_block

    # Silence mode — только суть
    if chat_id in silence_mode:
        memory_block += "\n[ТИШИНА]: Только факт или ответ. Максимум 2 предложения."
    bot.send_chat_action(chat_id, "typing")
    reply = ask_grok(text, history, memory_block=memory_block)

    # Извлекаем оба формата тегов памяти: [REMEMBER:] и [ACTION:remember:] (только для владельца)
    new_facts, reply_without_remember = _extract_remember_tags(reply)
    remember_matches = []  # обработано ниже — post-conflict код становится no-op
    saved_count = 0
    if chat_id == OWNER_ID:
        for fact in new_facts:
            if add_fact(fact.strip()):
                saved_count += 1

    # Разбираем основной ACTION тег
    action_type, action_param, clean_reply = _parse_action(reply_without_remember)

    # Сохраняем в историю (без служебных тегов)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": clean_reply or reply_without_remember})
    grok_history[chat_id] = history
    _save_grok_history()

    # Фоново сохраняем все remember-факты (тихо — без уведомления пользователя)
    for fact in remember_matches:
        add_fact(fact.strip())

    # Проверяем — пришёл ли запрос голосом (для TTS-ответа)
    is_voice = chat_id in voice_request_chats
    if is_voice:
        voice_request_chats.discard(chat_id)

    # Выполняем основное действие (если есть)
    if action_type == "forget":
        _handle_grok_action(chat_id, action_type, action_param)
        return  # forget сам выводит сообщение
    if action_type:
        _handle_grok_action(chat_id, action_type, action_param)

    # Показываем текстовый ответ Grok (если не пустой)
    if clean_reply and clean_reply.strip():
        if is_voice:
            _tts_send_voice(chat_id, clean_reply)
        safe_send(chat_id, clean_reply, main_menu())

    # Тихо уведомляем если что-то запомнено
    if saved_count > 0:
        noun = "факт" if saved_count == 1 else ("факта" if saved_count < 5 else "фактов")
        bot.send_message(chat_id, f"🧠 Запомнил {saved_count} {noun}.")


def process_text(chat_id, text):
    import re
    t = text.strip()
    tl = t.lower()

    # ══════════════════════════════════════════════════════════════════
    # 1. СОСТОЯНИЯ ОЖИДАНИЯ — всегда первые (мультишаговые диалоги)
    # ══════════════════════════════════════════════════════════════════

    # Ожидаем номер / текст для звонка
    if chat_id in waiting_for_owner_call:
        state = waiting_for_owner_call[chat_id]
        if state["step"] == "number":
            number = re.sub(r"[\s\-\(\)]", "", t)
            if not re.match(r"^\+?\d{7,15}$", number):
                bot.send_message(chat_id, "⚠️ Не похоже на номер. Напиши в формате +380XXXXXXXXX:")
                return
            waiting_for_owner_call[chat_id] = {"step": "message", "number": number}
            bot.send_message(chat_id, f"✅ Номер: *{number}*\n\nЧто сказать голосом?",
                             parse_mode="Markdown")
            return
        elif state["step"] == "message":
            number = state["number"]
            msg = t
            # Показываем план и просим подтверждение ПЕРЕД реальным звонком
            waiting_for_owner_call[chat_id] = {"step": "confirm", "number": number, "message": msg}
            bot.send_message(
                chat_id,
                f"📞 *Готов позвонить*\n\n"
                f"Номер: `{number}`\n"
                f"Скажу: _{msg}_\n\n"
                f"⚠️ Это *реальный звонок*. Подтвердить?\n"
                f"Напиши *да* чтобы позвонить, *нет* чтобы отменить.",
                parse_mode="Markdown",
            )
            return
        elif state["step"] == "confirm":
            number = state["number"]
            message = state["message"]
            waiting_for_owner_call.pop(chat_id)
            if tl.strip() in ("да", "yes", "давай", "подтверждаю", "звони", "ok", "ок"):
                bot.send_message(chat_id, f"📞 Звоню на {number}...")
                ok, info = make_call(number, message)
                if ok:
                    bot.send_message(
                        chat_id,
                        f"✅ Позвонил на *{number}*\nГолосом скажет: _{message}_",
                        parse_mode="Markdown", reply_markup=main_menu(),
                    )
                else:
                    bot.send_message(chat_id, f"❌ Ошибка звонка: {info}", reply_markup=main_menu())
            else:
                bot.send_message(chat_id, "✅ Звонок отменён. Ничего не произошло.", reply_markup=main_menu())
            return

    # Ожидаем ввод для таблиц / SMS Тохе / роли
    if chat_id in waiting_for_sheet_id:
        mode = waiting_for_sheet_id.pop(chat_id)
        if mode == "toha_sms":
            toha_number = os.environ.get("TOHA_PHONE_NUMBER", "")
            sms_link = make_sms_link(toha_number, t)
            markup = types.InlineKeyboardMarkup()
            markup.row(types.InlineKeyboardButton("📱 Открыть SMS и отправить Тохе", url=sms_link))
            bot.send_message(chat_id, f"💬 Нажми — откроется SMS с текстом для Тохи:\n«{t}»",
                             reply_markup=markup)
        else:
            handle_sheet_command(chat_id, t, mode)
        return

    # Подтверждение опасной команды ПК (shutdown/restart)
    if chat_id in waiting_remote_confirm:
        action = _remote_confirm_action.get(chat_id, "")
        waiting_remote_confirm.discard(chat_id)
        _remote_confirm_action.pop(chat_id, None)
        if tl.strip() in ("да", "yes", "подтверждаю", "✅ да, подтверждаю"):
            _log_remote(chat_id, f"подтверждено: {action}")
            if action == "shutdown":
                import platform, subprocess
                if platform.system() == "Windows":
                    subprocess.Popen(["shutdown", "/s", "/t", "30"])
                    safe_send(chat_id, "🔴 Выключение ПК через 30 секунд. Для отмены: shutdown /a", remote_access_menu())
                else:
                    safe_send(chat_id, "⚠️ Выключение работает только на Windows (при локальном запуске bot.py).", remote_access_menu())
            elif action == "restart":
                import platform, subprocess
                if platform.system() == "Windows":
                    subprocess.Popen(["shutdown", "/r", "/t", "30"])
                    safe_send(chat_id, "🔄 Перезагрузка ПК через 30 секунд. Для отмены: shutdown /a", remote_access_menu())
                else:
                    safe_send(chat_id, "⚠️ Перезагрузка работает только на Windows (при локальном запуске bot.py).", remote_access_menu())
        else:
            _log_remote(chat_id, f"отменено: {action}")
            bot.send_message(chat_id, "✅ Отменено. Ничего не произошло.", reply_markup=remote_access_menu())
        return

    # Ожидаем поисковый запрос для Sauron
    if chat_id in waiting_for_sauron_query:
        if tl in ("отмена", "cancel", "стоп", "нет"):
            waiting_for_sauron_query.discard(chat_id)
            markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
            bot.send_message(chat_id, "🔍 Поиск отменён.", reply_markup=markup)
            return
        waiting_for_sauron_query.discard(chat_id)
        query = t
        msg = bot.send_message(chat_id, f"🔍 Ищу «{query}» на Sauron…")
        try:
            result = sauron.search(query)
        except Exception as e:
            result = f"❌ Ошибка Sauron: {str(e)[:200]}"
        markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
        try:
            bot.delete_message(chat_id, msg.message_id)
        except Exception:
            pass
        safe_send(chat_id, result, markup)
        return

    # Ожидаем файл для поиска через Sauron
    if chat_id in waiting_for_file_sauron:
        if tl in ("отмена", "cancel", "стоп", "нет"):
            waiting_for_file_sauron.discard(chat_id)
            markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
            bot.send_message(chat_id, "📁 Отменено.", reply_markup=markup)
        else:
            bot.send_message(
                chat_id,
                "📁 Отправь файл (txt, csv, xlsx, docx, pdf) — не текст.\n"
                "Напиши «отмена» чтобы выйти.",
            )
        return

    # Ожидаем TRC20-адрес кошелька
    if chat_id in waiting_for_wallet:
        waiting_for_wallet.discard(chat_id)
        address = t
        if not (address.startswith("T") and len(address) == 34):
            bot.send_message(chat_id,
                             "⚠️ Некорректный TRC20 адрес (должен начинаться с T, 34 символа).\n"
                             "Нажми *💰 USDT крипто* и попробуй снова.",
                             parse_mode="Markdown")
            return
        _handle_grok_action(chat_id, "usdt", address)
        return

    # ══════════════════════════════════════════════════════════════════
    # 1.5 ЯВНЫЕ КОМАНДЫ ПАМЯТИ — «запомни что...» (только для владельца)
    # ══════════════════════════════════════════════════════════════════

    if chat_id == OWNER_ID:
        remember_prefixes = [
            "запомни что ", "запомни: ", "запомни ",
            "сохрани что ", "сохрани: ", "сохрани факт ",
            "не забудь что ", "не забудь: ",
            "запиши что ", "запиши: ",
        ]
        for prefix in remember_prefixes:
            if tl.startswith(prefix):
                fact = t[len(prefix):].strip()
                if fact:
                    added = add_fact(fact)
                    if added:
                        bot.send_message(chat_id, f"🧠 Запомнил: {fact}",
                                         reply_markup=main_menu())
                    else:
                        bot.send_message(chat_id, f"🧠 Уже знаю это: {fact}",
                                         reply_markup=main_menu())
                else:
                    bot.send_message(chat_id, "⚠️ Что именно запомнить? Напиши после «запомни».")
                return

    # ══════════════════════════════════════════════════════════════════
    # 2. ТОЧНЫЕ МЕТКИ КНОПОК МЕНЮ — только для UI-навигации
    # ══════════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════════
    # 1.7 РЕЖИМ ИИ-ЧАТА — если активен, весь текст идёт в LLM напрямую
    # ══════════════════════════════════════════════════════════════════
    if chat_id in ai_chat_mode:
        if tl.strip() in ("🔙 выйти из чата", "/exit", "выйти", "exit", "стоп", "stop"):
            ai_chat_mode.discard(chat_id)
            bot.send_message(chat_id, "👋 Вышли из ИИ-чата. Главное меню:", reply_markup=main_menu())
            return
        _ask_grok_and_route(chat_id, t)
        return

    # ══════════════════════════════════════════════════════════════════
    # 1.8 JARVIS MODE — командный центр, все команды внутри режима
    # ══════════════════════════════════════════════════════════════════
    if chat_id in jarvis_mode:
        cmd = tl.strip()
        # Выход
        if cmd in ("🔙 выйти из jarvis", "/jarvis_off", "выйти из jarvis", "выйти"):
            jarvis_mode.discard(chat_id)
            silence_mode.discard(chat_id)
            bot.send_message(chat_id, "⚡ Jarvis отключён. До встречи, Руслан.", reply_markup=main_menu())
            return
        # Тишина
        if cmd in ("режим тишины", "/тишина", "тишина"):
            silence_mode.add(chat_id)
            bot.send_message(chat_id, "🔇 Режим тишины. Отвечаю минимально.", reply_markup=jarvis_menu())
            return
        if cmd in ("выйти из тишины", "выйти из режима тишины", "без тишины", "/без тишины"):
            silence_mode.discard(chat_id)
            bot.send_message(chat_id, "🔊 Тишина отключена. Работаю в полном режиме.", reply_markup=jarvis_menu())
            return
        # Специальные команды
        if cmd in ("что дальше?", "что дальше", "/дальше", "дальше"):
            _btn_what_next(chat_id)
            return
        if cmd in ("панель управления", "командный центр", "/панель", "/центр", "⚡ jarvis"):
            _btn_jarvis(chat_id)
            return
        # Быстрые кнопки меню Jarvis
        _JARVIS_SHORTCUTS = {
            "☀️ бриф": lambda: _btn_morning_brief(chat_id),
            "бриф": lambda: _btn_morning_brief(chat_id),
            "🩺 статус": lambda: _btn_status(chat_id),
            "статус": lambda: _btn_status(chat_id),
            "🧠 память": lambda: _btn_show_memory(chat_id),
            "память": lambda: _btn_show_memory(chat_id),
            "📊 таблицы": lambda: _btn_sheets(chat_id),
            "таблицы": lambda: _btn_sheets(chat_id),
            "💸 расход ии": lambda: _btn_ai_budget(chat_id),
            "расход": lambda: _btn_ai_budget(chat_id),
            "🧠 что ты умеешь?": lambda: _btn_skills(chat_id),
            "что ты умеешь?": lambda: _btn_skills(chat_id),
            "что ты умеешь": lambda: _btn_skills(chat_id),
            "📞 позвонить": lambda: _btn_call(chat_id),
            "позвонить": lambda: _btn_call(chat_id),
            "💬 ии чат": lambda: _btn_ai_chat(chat_id),
            "📋 фоп": lambda: _show_tax_calendar(chat_id),
            "фоп": lambda: _show_tax_calendar(chat_id),
            "🗑️ забыть": lambda: _btn_forget(chat_id),
        }
        if cmd in _JARVIS_SHORTCUTS:
            _JARVIS_SHORTCUTS[cmd]()
            return
        # Всё остальное — в LLM с Jarvis-контекстом
        _ask_grok_and_route(chat_id, t)
        return

    BUTTON_LABELS = {
        # ── Главное меню ─────────────────────────────────────────
        "⚡ jarvis":             lambda: _btn_jarvis(chat_id),
        "🩺 статус":             lambda: _btn_status(chat_id),
        "💬 поговорить":         lambda: _btn_talk(chat_id),
        "🎤 голос":              lambda: _btn_voice_help(chat_id),
        "📞 звонок":             lambda: _btn_call(chat_id),
        "📞 позвонить":          lambda: _btn_call(chat_id),       # legacy
        "🏨 бронирование":       lambda: _btn_booking(chat_id),
        "📋 задачи":             lambda: _btn_tasks(chat_id),
        "🚕 тоха":               lambda: bot.send_message(chat_id, "🚕 Что делаем с Тохой?", reply_markup=toha_menu()),
        "💻 мой пк":             lambda: _btn_remote_access(chat_id),
        "🎮 dota 2":             lambda: _btn_dota(chat_id),
        "📊 я тигр":             lambda: _ask_grok_and_route(chat_id, "Сделай полную статистику по бизнесу Я Тигр"),
        "📋 фоп":                lambda: _show_tax_calendar(chat_id),
        # ── Jarvis-меню ──────────────────────────────────────────
        "☀️ бриф":               lambda: _btn_morning_brief(chat_id),
        "📊 таблицы":            lambda: _btn_sheets(chat_id),
        "🧠 память":             lambda: _btn_show_memory(chat_id),
        "💸 расход ии":          lambda: _btn_ai_budget(chat_id),
        "🧠 что умею?":          lambda: _btn_skills(chat_id),
        "🧠 что ты умеешь?":     lambda: _btn_skills(chat_id),    # legacy
        "🔙 выйти из jarvis":    lambda: _jarvis_exit(chat_id),
        # ── Раздел «Мой ПК» ──────────────────────────────────────
        "🖥 статус пк":          lambda: _btn_remote_status(chat_id),
        "🔑 id подключения":     lambda: _btn_remote_ids(chat_id),
        "🚀 запустить anydesk":  lambda: _btn_remote_launch(chat_id, "AnyDesk"),
        "🚀 запустить teamviewer": lambda: _btn_remote_launch(chat_id, "TeamViewer"),
        "🌐 chrome remote desktop": lambda: _btn_remote_crd(chat_id),
        "📖 инструкции по настройке": lambda: bot.send_message(
            chat_id, "📖 Выбери приложение:", reply_markup=remote_instructions_menu()),
        "📖 anydesk — инструкция":    lambda: _btn_remote_instructions(chat_id, "anydesk"),
        "📖 teamviewer — инструкция": lambda: _btn_remote_instructions(chat_id, "teamviewer"),
        "📖 chrome remote desktop — инструкция": lambda: _btn_remote_instructions(chat_id, "crd"),
        "📄 журнал действий пк": lambda: _btn_remote_log(chat_id),
        "🔒 безопасность":       lambda: _btn_remote_safety(chat_id),
        "🔙 назад к пк":         lambda: _btn_remote_access(chat_id),
        # ── Тоха под-меню ────────────────────────────────────────
        "📍 отправить гео тохе": lambda: _btn_geo_toha(chat_id),
        "💬 написать тохе sms":  lambda: _btn_sms_toha(chat_id),
        # ── Таблицы под-меню ─────────────────────────────────────
        "📊 аналитика таблицы":  lambda: _btn_analytics(chat_id),
        "📋 мои таблицы":        lambda: _btn_my_sheets(chat_id),
        "➕ сохранить таблицу":  lambda: _btn_save_sheet(chat_id),
        "📖 читать таблицу":     lambda: _btn_read_sheet(chat_id),
        "✏️ записать в таблицу": lambda: _btn_write_sheet(chat_id),
        "ℹ️ инфо о таблице":     lambda: _btn_info_sheet(chat_id),
        # ── Разное ───────────────────────────────────────────────
        "📍 геопозиция":         lambda: bot.send_message(chat_id, "📍 Отправь геопозицию — скрепка 📎 → Геопозиция"),
        "💰 usdt крипто":        lambda: _btn_usdt(chat_id),
        "🤖 ии чат":             lambda: _btn_talk(chat_id),       # legacy → новое название
        "📗 таблицы":            lambda: _btn_sheets(chat_id),     # legacy
        "🗑️ забыть":             lambda: _btn_forget(chat_id),
        "🛣️ маршрут":            lambda: _ask_grok_and_route(chat_id, "Помоги с маршрутом"),
        "📝 анкета":             lambda: _start_anketa(chat_id),
        "анкета":                lambda: _start_anketa(chat_id),
        "/анкета":               lambda: _start_anketa(chat_id),
        "/anketa":               lambda: _start_anketa(chat_id),
        "/profile":              lambda: _start_anketa(chat_id),
        "/налоги":               lambda: _show_tax_calendar(chat_id),
        "/податки":              lambda: _show_tax_calendar(chat_id),
        "налоги":                lambda: _show_tax_calendar(chat_id),
        "податки":               lambda: _show_tax_calendar(chat_id),
        "🔍 саурон":             lambda: _btn_sauron_search(chat_id),
        "📁 файл → саурон":     lambda: _btn_file_sauron(chat_id),
        "🔙 назад":              lambda: bot.send_message(chat_id, "Главное меню 👇", reply_markup=main_menu()),
    }

    # ── Естественные фразы для поиска по файлу ───────────────────────────
    file_triggers = [
        "проверь файл", "проверить файл", "поиск по файлу",
        "найди в файле", "загрузи файл", "отправь файл",
        "файл саурон", "файл в саурон", "найди связанных",
        "найди приближенных", "поиск номеров в файле",
        "найди номера в файле", "проверь номера",
    ]
    if any(trigger in tl for trigger in file_triggers):
        _btn_file_sauron(chat_id)
        return

    label_key = tl.strip()
    if label_key in BUTTON_LABELS:
        BUTTON_LABELS[label_key]()
        return

    # ══════════════════════════════════════════════════════════════════
    # 3. ВСЁ ОСТАЛЬНОЕ → GROK (основной AI-мозг)
    # ══════════════════════════════════════════════════════════════════
    _ask_grok_and_route(chat_id, t)


# ── Вспомогательные функции для кнопок ───────────────────────────────────────

def _btn_call(chat_id):
    bot.send_message(chat_id, "📞 *Звонок*\n\nНа какой номер? Напиши в формате +380XXXXXXXXX:",
                     parse_mode="Markdown")
    waiting_for_owner_call[chat_id] = {"step": "number"}


def _btn_usdt(chat_id):
    waiting_for_wallet.add(chat_id)
    bot.send_message(chat_id,
                     "💰 *USDT TRC20 Аналитика*\n\n"
                     "Отправь адрес TRC20 кошелька (начинается с T, 34 символа):",
                     parse_mode="Markdown")


def _btn_ai_chat(chat_id):
    ai_chat_mode.add(chat_id)
    bot.send_message(
        chat_id,
        "🤖 *Режим ИИ-чата активен*\n\n"
        "Пиши или говори — я отвечу как ChatGPT.\n"
        "История и память сохраняются между сообщениями.\n\n"
        "Могу выполнять твои команды: SMS, звонки, открыть сайт, запустить программу — "
        "просто попроси словами.\n\n"
        "_Нажми «🔙 Выйти из чата» чтобы вернуться в главное меню._",
        parse_mode="Markdown",
        reply_markup=ai_chat_menu(),
    )


# ══════════════════════════════════════════════════════════════════
# ⚡ JARVIS MODE — функции командного центра
# ══════════════════════════════════════════════════════════════════

def _jarvis_system_status() -> str:
    """Генерирует компактный статус-блок всех подсистем."""
    now = _now_local()
    tz = _ukraine_tz_hours()
    lines = []

    # ИИ backend
    backend = os.environ.get("LLM_BACKEND", "openai").lower()
    key_map = {
        "openai":  ("OPENAI_API_KEY", "AI_INTEGRATIONS_OPENAI_API_KEY"),
        "grok":    ("XAI_API_KEY",),
        "gemini":  ("GEMINI_API_KEY",),
        "llama":   (),
    }
    keys = key_map.get(backend, ())
    ai_ok = not keys or any(os.environ.get(k) for k in keys)
    lines.append(f"🧠 ИИ: *{backend}* {'✅' if ai_ok else '⚠️ нет ключа'}")

    # Голос/TTS — только прямой OPENAI_API_KEY (Replit proxy audio не поддерживает)
    voice_ok = bool(os.environ.get("OPENAI_API_KEY"))
    if voice_ok:
        lines.append("🎤 Голос (STT/TTS): ✅ настроен")
    else:
        lines.append("🎤 Голос (STT/TTS): ⚠️ не настроен — добавь OPENAI_API_KEY в Secrets")

    # Sauron
    try:
        lines.append(f"🔍 Sauron: {sauron.status()}")
    except Exception:
        lines.append("🔍 Sauron: ⚠️ ошибка модуля")

    # Звонки
    tw_ok = all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"))
    tx_ok = all(os.environ.get(k) for k in ("TELNYX_API_KEY", "TELNYX_FROM_NUMBER"))
    calls_str = " + ".join(filter(None, [
        "Twilio" if tw_ok else None,
        "Telnyx" if tx_ok else None,
    ]))
    lines.append(f"📞 Звонки: {('✅ ' + calls_str) if calls_str else '⚠️ не настроены'}")

    # SMS Тоха
    sms_ok = bool(os.environ.get("TOHA_PHONE_NUMBER"))
    lines.append(f"💬 SMS Тохе: {'✅' if sms_ok else '⚠️ нет TOHA_PHONE_NUMBER'}")

    # Google Sheets
    try:
        from sheets import list_sheets
        saved = list_sheets()
        n = len(saved) if saved else 0
        lines.append(f"📊 Google Sheets: {'✅ ' + str(n) + ' таблиц' if n else '✅ подключено (0 таблиц)'}")
    except Exception:
        lines.append("📊 Google Sheets: ⚠️ ошибка")

    # Память
    try:
        facts = get_facts()
        lines.append(f"💾 Память: {len(facts)} фактов")
    except Exception:
        lines.append("💾 Память: ⚠️ недоступна")

    # Напоминания
    try:
        pending = list_pending(OWNER_ID)
        n = len(pending)
        if n:
            fire = (pending[0].get("fire_at") or "")[:16].replace("T", " ")
            lines.append(f"🔔 Напоминания: {n} активных · ближайшее {fire}")
        else:
            lines.append("🔔 Напоминания: нет")
    except Exception:
        lines.append("🔔 Напоминания: ⚠️ ошибка")

    # ФОП дедлайн
    try:
        deadlines = tax_calendar.upcoming_deadlines(now, months_ahead=3)
        if deadlines:
            d = deadlines[0]
            days_left = (d["deadline"].date() - now.date()).days
            warn = " ⚠️" if days_left <= 14 else ""
            lines.append(f"📋 ФОП: {d['kind']} {d['quarter']} через {days_left} дн.{warn}")
        else:
            lines.append("📋 ФОП: дедлайнов нет")
    except Exception:
        lines.append("📋 ФОП: ─")

    # Время
    silence_mark = " · 🔇 тишина" if OWNER_ID in silence_mode else ""
    lines.append(f"🕐 Время: {now.strftime('%H:%M')} (UTC+{tz}){silence_mark}")

    return "\n".join(lines)


def _btn_jarvis(chat_id):
    """Активирует Jarvis Mode и показывает командный центр."""
    jarvis_mode.add(chat_id)
    now = _now_local()
    h = now.hour
    if h < 6:
        greet = "Ночью не спится"
    elif h < 12:
        greet = "Доброе утро"
    elif h < 17:
        greet = "Добрый день"
    else:
        greet = "Добрый вечер"

    status = _jarvis_system_status()
    silence_hint = "🔇 _Режим тишины активен_\n" if chat_id in silence_mode else ""
    text = (
        "⚡ *КОМАНДНЫЙ ЦЕНТР*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{greet}, Руслан. Системы онлайн.\n\n"
        f"{status}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{silence_hint}"
        "Готов. Что делаем, шеф?"
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=jarvis_menu())


def _btn_status(chat_id):
    """Системная диагностика — показывает здоровье всех подсистем."""
    status = _jarvis_system_status()
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
    safe_send(chat_id, f"🩺 *Диагностика системы*\n━━━━━━━━━━━━━━━━━━━━━━\n\n{status}", markup)


def _btn_skills(chat_id):
    """Показывает список доступных функций — только включённые по ключам."""
    lines = [
        "🤖 *Jarvis — полный список возможностей*\n",
        "💬 *Разговор и голос*",
    ]
    tts_ok = bool(os.environ.get("OPENAI_API_KEY"))  # только прямой ключ, proxy audio не поддерживает
    if tts_ok:
        lines.append("  ✅ Свободный диалог на русском — текстом и голосом")
        lines.append("  ✅ Голосовой ввод → Whisper AI → ответ голосом (TTS)")
    else:
        lines.append("  ✅ Свободный диалог на русском")
        lines.append("  ⚠️ Голос/TTS: нужен OPENAI_API_KEY")

    lines.append("\n🧠 *Память и контекст*")
    try:
        from memory import get_facts
        n = len(get_facts())
        lines.append(f"  ✅ Долгосрочная память — {n} фактов о тебе")
    except Exception:
        lines.append("  ✅ Долгосрочная память (SQLite)")
    lines.append("  ✅ История разговора в рамках сессии")

    lines.append("\n📋 *Бизнес «Я Тигр»*")
    lines.append("  ✅ Статистика и аналитика бизнеса")
    lines.append("  ✅ Google Sheets — читать, писать, анализировать")
    lines.append("  ✅ Налоговый календарь ФОП + дедлайны")
    lines.append("  ✅ Утренний бриф — автоматически в 08:00")
    lines.append("  ✅ USDT TRC20 — аналитика кошелька")

    lines.append("\n📞 *Звонки и коммуникация*")
    tw = all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"))
    tx = all(os.environ.get(k) for k in ("TELNYX_API_KEY", "TELNYX_FROM_NUMBER"))
    if tw or tx:
        prov = "/".join(filter(None, ["Twilio" if tw else None, "Telnyx" if tx else None]))
        lines.append(f"  ✅ Звонки через {prov} — голосовой сценарий + подтверждение")
    else:
        lines.append("  ⚠️ Звонки: добавь TWILIO_* или TELNYX_* в Secrets")
    sms_ok = bool(os.environ.get("TOHA_PHONE_NUMBER"))
    lines.append(f"  {'✅' if sms_ok else '⚠️'} SMS Тохе{'✅' if sms_ok else ' (нет TOHA_PHONE_NUMBER)'}")
    lines.append("  ✅ Геопозиция → отправка Тохе")
    lines.append("  ✅ Бронирование (план → подтверждение → звонок)")

    lines.append("\n💻 *Удалённый доступ (Мой ПК)*")
    lines.append("  ✅ TeamViewer / AnyDesk / Chrome Remote Desktop")
    lines.append("  ✅ Поиск по прямым путям Windows + инструкции")
    lines.append("  ✅ Статус ПК: CPU, RAM, диск")
    lines.append("  🔒 Без кейлоггера, без паролей, без произвольных команд")

    lines.append("\n🎮 *Dota 2 Coach*")
    lines.append("  ✅ Герои, counter-pick, билды, мета 2024–2025")
    lines.append("  ✅ Разбор ситуаций, советы по ролям")

    lines.append("\n🔔 *Задачи и напоминания*")
    lines.append("  ✅ Голосовые и текстовые напоминания")
    lines.append("  ✅ Проверка каждую минуту — ни одно не пропустит")

    lines.append(
        "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "_Просто пиши или говори — всё понимаю._\n"
        "_Если что-то не работает — жми «🩺 Статус»._"
    )
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
    safe_send(chat_id, "\n".join(lines), markup)


def _btn_what_next(chat_id):
    """Предлагает следующий лучший шаг на основе контекста."""
    now = _now_local()
    context_parts = [f"Время: {now.strftime('%d.%m.%Y %H:%M')} (Украина)"]

    try:
        pending = list_pending(OWNER_ID)
        if pending:
            fires = [(r.get("fire_at", "")[:16].replace("T", " "), r.get("text", "")[:60]) for r in pending[:3]]
            context_parts.append("Ближайшие напоминания: " + "; ".join(f"{f} — {tx}" for f, tx in fires))
        else:
            context_parts.append("Активных напоминаний нет.")
    except Exception:
        pass

    try:
        deadlines = tax_calendar.upcoming_deadlines(now, months_ahead=2)
        if deadlines:
            d = deadlines[0]
            days = (d["deadline"].date() - now.date()).days
            context_parts.append(f"ФОП дедлайн: {d['kind']} {d['quarter']} через {days} дн.")
    except Exception:
        pass

    prompt = (
        "Ты — личный ассистент владельца такси-бизнеса «Я Тигр» (Украина).\n"
        "Вот текущий контекст:\n" + "\n".join(context_parts) + "\n\n"
        "Предложи 3 конкретных следующих шага, которые стоит сделать прямо сейчас или сегодня. "
        "Коротко, по делу, без воды. Нумерованный список."
    )
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
    bot.send_chat_action(chat_id, "typing")
    try:
        reply = ask_grok(prompt, [], memory_block="")
        safe_send(chat_id, f"🎯 *Что дальше:*\n\n{reply}", markup)
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Не смог сгенерировать план: {e}", reply_markup=markup)


def _btn_morning_brief(chat_id):
    """Ручной запуск утренней сводки по кнопке."""
    bot.send_chat_action(chat_id, "typing")
    now = _now_local()
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()

    # Погода
    weather = _fetch_weather_kyiv() if callable(globals().get("_fetch_weather_kyiv")) else "─"

    # ФОП
    fop_lines = []
    try:
        deadlines = tax_calendar.upcoming_deadlines(now, months_ahead=2)
        for d in deadlines[:2]:
            days = (d["deadline"].date() - now.date()).days
            warn = " ⚠️" if days <= 14 else ""
            fop_lines.append(f"  · {d['kind']} {d['quarter']}: через {days} дн.{warn}")
    except Exception:
        pass

    # Напоминания
    rem_lines = []
    try:
        pending = list_pending(OWNER_ID)
        for r in (pending or [])[:3]:
            fire = (r.get("fire_at") or "")[:16].replace("T", " ")
            txt = (r.get("text") or "")[:60]
            rem_lines.append(f"  · {fire} — {txt}")
    except Exception:
        pass

    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    date_str = f"{day_names[now.weekday()]}, {now.strftime('%d.%m.%Y')}"

    lines = [
        "☀️ *Утренний бриф*",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 {date_str}",
        f"🌡 {weather}",
        "",
    ]
    if fop_lines:
        lines.append("📋 *ФОП дедлайны:*")
        lines.extend(fop_lines)
        lines.append("")
    if rem_lines:
        lines.append("🔔 *Ближайшие напоминания:*")
        lines.extend(rem_lines)
        lines.append("")
    else:
        lines.append("🔔 Напоминаний нет.")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("_Хорошего дня, Руслан!_ 🚕")

    safe_send(chat_id, "\n".join(lines), markup)


def _btn_ai_budget(chat_id):
    """Краткая информация о бюджете ИИ и переменных."""
    monthly = os.environ.get("MONTHLY_AI_BUDGET_USD", "")
    daily = os.environ.get("DAILY_AI_BUDGET_USD", "")
    backend = os.environ.get("LLM_BACKEND", "openai").lower()

    cost_info = {
        "openai":  "gpt-4o-mini ≈ $0.15/1M токенов входящих · $0.60/1M исходящих",
        "grok":    "grok-3 — см. xai.com/pricing",
        "gemini":  "gemini-flash ≈ бесплатный лимит + платный",
        "llama":   "Бесплатно (локальный Ollama)",
    }
    lines = [
        "💸 *Бюджет ИИ*\n",
        f"Текущий бэкенд: *{backend}*",
        f"Примерная стоимость: _{cost_info.get(backend, '─')}_",
        "",
    ]
    if monthly:
        lines.append(f"Месячный лимит: ${monthly} (MONTHLY_AI_BUDGET_USD)")
    else:
        lines.append("Месячный лимит: ─ _(не задан — добавь MONTHLY_AI_BUDGET_USD в .env)_")
    if daily:
        lines.append(f"Дневной лимит: ${daily} (DAILY_AI_BUDGET_USD)")
    else:
        lines.append("Дневной лимит: ─ _(не задан — добавь DAILY_AI_BUDGET_USD в .env)_")

    lines.append("\n_Точный учёт расходов — в дашборде OpenAI/xAI/Google._")
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
    safe_send(chat_id, "\n".join(lines), markup)


def _btn_show_memory(chat_id):
    """Показывает сохранённые факты из памяти."""
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
    try:
        text = format_for_display()
        if not text or not text.strip():
            text = "💾 Память пуста. Скажи «запомни что...» чтобы добавить факт."
        safe_send(chat_id, text, markup)
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Ошибка чтения памяти: {e}", reply_markup=markup)


def _jarvis_exit(chat_id):
    """Выход из Jarvis Mode."""
    jarvis_mode.discard(chat_id)
    silence_mode.discard(chat_id)
    bot.send_message(chat_id, "⚡ Jarvis отключён. До встречи, Руслан.", reply_markup=main_menu())


# ══════════════════════════════════════════════════════════════════
# конец блока Jarvis
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# 💻 МОЙ ПК — удалённый доступ через стандартные приложения
# ══════════════════════════════════════════════════════════════════

def _log_remote(chat_id: int, action: str):
    """Записывает действие в журнал раздела «Мой ПК»."""
    now = _now_local()
    entry = f"[{now.strftime('%d.%m %H:%M')}] {action}"
    remote_action_log.append(entry)
    if len(remote_action_log) > 200:
        remote_action_log.pop(0)


def _remote_owner_check(chat_id: int) -> bool:
    """Возвращает True если это владелец, иначе отправляет отказ."""
    if chat_id == OWNER_ID:
        return True
    bot.send_message(
        chat_id,
        "⛔ Раздел «Мой ПК» доступен только владельцу бота.\n"
        "Если ты владелец — убедись что твой Telegram ID совпадает с OWNER_ID в настройках.",
        reply_markup=main_menu(),
    )
    return False


def _btn_remote_access(chat_id: int):
    """Главный экран раздела «Мой ПК»."""
    if not _remote_owner_check(chat_id):
        return
    _log_remote(chat_id, "открыл раздел «Мой ПК»")
    text = (
        "💻 *Мой ПК — Удалённый доступ*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Управление через *стандартные* приложения:\n"
        "• AnyDesk\n"
        "• TeamViewer\n"
        "• Chrome Remote Desktop\n\n"
        "🔒 *Безопасность:*\n"
        "• Я НЕ читаю твои пароли, сессии, историю браузера или куки\n"
        "• НЕ присылай мне коды из Telegram, SMS-коды или пароли\n"
        "• Мне достаточно твоего *Telegram ID* — личный аккаунт не нужен\n"
        "• Все действия записываются в журнал\n\n"
        "Выбери действие 👇"
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=remote_access_menu())


def _btn_remote_status(chat_id: int):
    """Статус ПК/сервера через psutil."""
    if not _remote_owner_check(chat_id):
        return
    _log_remote(chat_id, "запросил статус ПК")
    lines = ["🖥 *Статус системы*\n"]
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        lines.append(f"💻 CPU: {cpu}%")
        lines.append(f"🧠 RAM: {ram.percent}% "
                     f"({ram.used // 1024 // 1024} МБ / {ram.total // 1024 // 1024} МБ)")
        lines.append(f"💾 Диск: {disk.percent}% занято "
                     f"({disk.free // 1024 // 1024 // 1024} ГБ свободно)")
    except ImportError:
        lines.append("_(psutil не установлен — детальный статус недоступен)_")
    except Exception as e:
        lines.append(f"_(Ошибка: {e})_")

    mode = "🏠 Локальный ПК" if DEPLOYMENT_MODE != "production" else "☁️ Replit сервер"
    now = _now_local()
    lines.append(f"\n⚡ Режим: {mode}")
    lines.append(f"🕐 Время: {now.strftime('%H:%M')} (UTC+{_ukraine_tz_hours()})")

    if DEPLOYMENT_MODE == "production":
        lines.append(
            "\n_Это статус Replit-сервера, а не твоего домашнего ПК.\n"
            "Для статуса домашнего ПК запусти bot.py локально._"
        )
    safe_send(chat_id, "\n".join(lines), remote_access_menu())


def _btn_remote_ids(chat_id: int):
    """Показывает сохранённые ID подключения из Secrets."""
    if not _remote_owner_check(chat_id):
        return
    _log_remote(chat_id, "запросил ID подключения")
    anydesk_id = os.environ.get("ANYDESK_ID", "")
    tv_id = os.environ.get("TEAMVIEWER_ID", "")
    pc_name = os.environ.get("REMOTE_PC_NAME", "")
    lines = ["🔑 *Сохранённые ID подключения*\n"]
    if anydesk_id:
        lines.append(f"🟢 AnyDesk ID: `{anydesk_id}`")
    else:
        lines.append("⚠️ AnyDesk ID: не сохранён")
        lines.append("  → Добавь `ANYDESK_ID` в Replit Secrets")
    if tv_id:
        lines.append(f"🟢 TeamViewer ID: `{tv_id}`")
    else:
        lines.append("⚠️ TeamViewer ID: не сохранён")
        lines.append("  → Добавь `TEAMVIEWER_ID` в Replit Secrets")
    if pc_name:
        lines.append(f"\n🖥 ПК: {pc_name}")
    lines.append(
        "\n🔒 _ID хранятся в Replit Secrets — бот не просит пароли._\n"
        "_Пароль сеанса задаёшь вручную в самом приложении._"
    )
    safe_send(chat_id, "\n".join(lines), remote_access_menu())


def _btn_remote_launch(chat_id: int, app_name: str):
    """Запускает приложение удалённого доступа — сначала ищет по прямым путям Windows."""
    if not _remote_owner_check(chat_id):
        return
    _log_remote(chat_id, f"запрос запуска: {app_name}")

    # ── Бот на Replit-сервере — проверяем статус, не запускаем ──
    if DEPLOYMENT_MODE == "production":
        app_key = app_name.lower()
        found_path = pc_apps.find_remote_app_path(app_key)
        is_running = pc_apps.is_remote_app_running(app_key) if found_path else False
        if found_path:
            status = "🟢 запущен" if is_running else "🟡 установлен, но не запущен"
            text = (
                f"💻 *{app_name}*\n"
                f"Статус: {status}\n"
                f"Путь: `{found_path}`\n\n"
                f"☁️ Бот работает на Replit — запускать приложения на *твоём ПК* он не может.\n\n"
                f"Что сделать:\n"
                f"• Открой {app_name} на ПК вручную (ярлык на рабочем столе)\n"
                f"• Или запусти bot.py *локально* — тогда бот сможет запускать за тебя\n\n"
                f"Подключайся с телефона/планшета через ID 👇"
            )
        else:
            # Сообщаем куда скачать
            download = {
                "teamviewer": "https://teamviewer.com → Скачать",
                "anydesk": "https://anydesk.com → Скачать",
            }.get(app_key, "официальный сайт")
            text = (
                f"⚠️ *{app_name}* не найден по стандартным путям.\n\n"
                f"Проверил:\n"
                + "\n".join(
                    f"• `{p}`"
                    for p in pc_apps._REMOTE_APPS_DIRECT.get(app_key, [])
                )
                + f"\n\n"
                f"📥 Установи: {download}\n"
                f"После установки нажми эту кнопку снова — я найду автоматически."
            )
        bot.send_message(chat_id, text, parse_mode="Markdown",
                         reply_markup=remote_access_menu())
        return

    # ── Бот локально на ПК — запускаем напрямую ──
    _log_remote(chat_id, f"запускаю локально: {app_name}")
    found_path = pc_apps.find_remote_app_path(app_name)
    is_running = pc_apps.is_remote_app_running(app_name) if found_path else False

    if found_path and is_running:
        safe_send(chat_id,
                  f"✅ *{app_name}* уже запущен.\n`{found_path}`\n\n"
                  f"Подключайся — программа активна.",
                  remote_access_menu())
        return

    result = pc_apps.launch_app(app_name)
    safe_send(chat_id, result, remote_access_menu())


def _btn_remote_crd(chat_id: int):
    """Информация о Chrome Remote Desktop."""
    if not _remote_owner_check(chat_id):
        return
    _log_remote(chat_id, "открыл Chrome Remote Desktop")
    crd_url = os.environ.get("CRD_SHARE_URL", "")
    text = (
        "🌐 *Chrome Remote Desktop*\n\n"
        "Google's официальный инструмент — бесплатно, без регистрации на сторонних серверах.\n\n"
    )
    if crd_url:
        text += f"🔗 Ссылка подключения: {crd_url}\n\n"
        text += "_Ссылка из REPLIT SECRET `CRD_SHARE_URL`. Обновляй при каждой новой сессии._\n\n"
    else:
        text += "⚠️ Ссылка не сохранена. Добавь `CRD_SHARE_URL` в Replit Secrets после создания сессии.\n\n"
    text += (
        "📱 Подключение с телефона:\n"
        "1. Установи *Chrome Remote Desktop* из Google Play / App Store\n"
        "2. Войди в тот же Google-аккаунт что и на ПК\n"
        "3. Твой ПК появится в списке — нажми для подключения"
    )
    safe_send(chat_id, text, remote_access_menu())


def _btn_remote_instructions(chat_id: int, app: str):
    """Подробная инструкция по настройке приложения удалённого доступа."""
    if not _remote_owner_check(chat_id):
        return
    _log_remote(chat_id, f"запросил инструкцию: {app}")
    if app == "anydesk":
        text = (
            "📖 *AnyDesk — настройка*\n\n"
            "*На ПК (один раз):*\n"
            "1. Скачай с [anydesk.com](https://anydesk.com) — бесплатная версия подходит\n"
            "2. Запусти — AnyDesk ID появится в главном окне (9 цифр)\n"
            "3. Сохрани ID: добавь `ANYDESK_ID=123456789` в Replit Secrets\n"
            "4. Включи _Запускать при старте Windows_ в настройках AnyDesk\n"
            "5. Установи пароль доступа в настройках → Безопасность\n\n"
            "*Подключение с телефона:*\n"
            "1. Установи AnyDesk из Google Play / App Store\n"
            "2. Введи ID из бота (кнопка «🔑 ID подключения»)\n"
            "3. Введи пароль который ты задал на ПК\n\n"
            "🔒 _Пароль хранится только в AnyDesk — не присылай его в Telegram._"
        )
    elif app == "teamviewer":
        text = (
            "📖 *TeamViewer — настройка*\n\n"
            "*На ПК (один раз):*\n"
            "1. Скачай с [teamviewer.com](https://teamviewer.com) — бесплатно для личного использования\n"
            "2. Запусти — в главном окне будет ID (9 цифр) и пароль сеанса\n"
            "3. Для постоянного доступа: зарегистрируйся и добавь ПК в «Мои компьютеры»\n"
            "4. Сохрани ID: добавь `TEAMVIEWER_ID=987654321` в Replit Secrets\n\n"
            "*Подключение с телефона:*\n"
            "1. Установи TeamViewer из Google Play / App Store\n"
            "2. Войди в свой аккаунт → выбери свой ПК\n"
            "   _или_ введи ID вручную и введи пароль сеанса\n\n"
            "🔒 _Пароль сеанса меняется каждый запуск — не присылай его в Telegram._"
        )
    else:  # crd
        text = (
            "📖 *Chrome Remote Desktop — настройка*\n\n"
            "*На ПК (один раз):*\n"
            "1. Открой [remotedesktop.google.com/access](https://remotedesktop.google.com/access) в Chrome\n"
            "2. Войди в Google-аккаунт\n"
            "3. Нажми «Включить» → скачай и установи расширение\n"
            "4. Задай PIN (минимум 6 цифр) — запомни его\n"
            "5. ПК появится в списке как доступный\n\n"
            "*Подключение с телефона:*\n"
            "1. Установи _Chrome Remote Desktop_ из Google Play / App Store\n"
            "2. Войди в тот же Google-аккаунт\n"
            "3. Нажми на свой ПК → введи PIN\n\n"
            "*Быстрая ссылка (без аккаунта):*\n"
            "1. На [remotedesktop.google.com/support](https://remotedesktop.google.com/support) → «Поделиться экраном»\n"
            "2. Скопируй код → сохрани в Replit Secrets как `CRD_SHARE_URL`\n\n"
            "🔒 _PIN хранится только на ПК — не присылай его в Telegram._"
        )
    safe_send(chat_id, text, remote_instructions_menu())


def _btn_remote_log(chat_id: int):
    """Показывает последние 20 записей журнала действий."""
    if not _remote_owner_check(chat_id):
        return
    if not remote_action_log:
        bot.send_message(
            chat_id, "📄 Журнал пуст — действий пока не было.",
            reply_markup=remote_access_menu()
        )
        return
    entries = remote_action_log[-20:]
    text = "📄 *Журнал действий «Мой ПК»* (последние 20):\n\n" + "\n".join(entries)
    safe_send(chat_id, text, remote_access_menu())


def _btn_remote_safety(chat_id: int):
    """Напоминание о безопасности и чего бот НЕ делает."""
    if not _remote_owner_check(chat_id):
        return
    _log_remote(chat_id, "открыл раздел безопасности")
    text = (
        "🔒 *Безопасность — важно знать*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ *Что бот умеет:*\n"
        "• Показывать сохранённые ID подключения из Secrets\n"
        "• Давать инструкции по AnyDesk / TeamViewer / CRD\n"
        "• Запускать приложения (только при локальном запуске bot.py)\n"
        "• Показывать статус CPU/RAM/диска\n\n"
        "🚫 *Что бот НЕ делает — никогда:*\n"
        "• Не читает пароли, куки, токены, историю браузера\n"
        "• Не устанавливает скрытые программы\n"
        "• Не записывает нажатия клавиш (кейлоггер)\n"
        "• Не включает камеру или микрофон без ведома\n"
        "• Не выполняет произвольные команды терминала\n"
        "• Не подключается к твоему личному Telegram-аккаунту\n\n"
        "🔑 *Идентификация:*\n"
        "Бот знает тебя по *Telegram ID* (`"
        + str(OWNER_ID) +
        "`). Никаких паролей аккаунта не нужно.\n\n"
        "⚠️ *Никогда не присылай в Telegram:*\n"
        "• Код подтверждения из SMS\n"
        "• Пароль Telegram-аккаунта\n"
        "• Пароли от AnyDesk / TeamViewer\n"
        "• Пароли от Wi-Fi, банков, любых сервисов\n\n"
        "_Если кто-то просит тебя прислать такие данные — это мошенник._"
    )
    safe_send(chat_id, text, remote_access_menu())


# ══════════════════════════════════════════════════════════════════
# конец блока «Мой ПК»
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# 💬 ПОГОВОРИТЬ / 🎤 ГОЛОС / 📋 ЗАДАЧИ / 🏨 БРОНЬ / 🎮 DOTA 2
# ══════════════════════════════════════════════════════════════════

def _btn_talk(chat_id: int):
    """Активирует режим свободного разговора с ИИ."""
    ai_chat_mode.add(chat_id)
    tts_ok = bool(os.environ.get("OPENAI_API_KEY"))  # только прямой ключ, proxy audio не поддерживает
    voice_hint = "🎤 Или отправь голосовое — отвечу голосом." if tts_ok else "🎤 Голосовые принимаю, но TTS-ответа нет (нет OPENAI_API_KEY)."
    text = (
        "💬 *Режим разговора*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Пиши всё что угодно — вопросы, задачи, идеи, анализ.\n"
        f"{voice_hint}\n\n"
        "Темы которые знаю хорошо:\n"
        "• Такси-бизнес, водители, клиенты\n"
        "• Финансы ФОП, налоги Украины\n"
        "• Dota 2 — герои, билды, мета\n"
        "• Маршруты, бронирование, переговоры\n"
        "• Что угодно ещё — попробуй!\n\n"
        "_Нажми «🔙 Выйти из чата» чтобы вернуться в меню._"
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=ai_chat_menu())


def _btn_voice_help(chat_id: int):
    """Объясняет как работает голосовой режим."""
    tts_ok = bool(os.environ.get("OPENAI_API_KEY"))  # только прямой ключ, proxy audio не поддерживает
    if tts_ok:
        status_line = "✅ Голос *включён* — принимаю голосовые, отвечаю голосом."
        how = (
            "1. Нажми 🎤 в поле ввода Telegram → запиши сообщение\n"
            "2. Я расшифрую через Whisper AI\n"
            "3. Придумаю умный ответ\n"
            "4. Отвечу голосовым сообщением (TTS)\n\n"
            "💡 *Совет:* говори чётко, по-русски, с контекстом — «позвони Тохе и скажи...»"
        )
    else:
        status_line = "⚠️ Голос частично работает — расшифровываю, но голосовых ответов нет."
        how = (
            "1. Нажми 🎤 → запиши сообщение\n"
            "2. Я расшифрую и отвечу *текстом*\n\n"
            "Для голосовых ответов добавь в Replit Secrets:\n"
            "`OPENAI_API_KEY` — ключ OpenAI"
        )
    text = (
        f"🎤 *Голосовой режим*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{status_line}\n\n"
        f"*Как пользоваться:*\n{how}"
    )
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


def _btn_tasks(chat_id: int):
    """Показывает активные задачи / напоминания и предлагает добавить."""
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
    lines = ["📋 *Задачи и напоминания*\n"]
    try:
        pending = list_pending(OWNER_ID)
        if pending:
            now = _now_local()
            for r in pending[:10]:
                fire_raw = (r.get("fire_at") or "")[:16].replace("T", " ")
                txt = r.get("text", "")[:60]
                lines.append(f"🔔 {fire_raw} — {txt}")
            if len(pending) > 10:
                lines.append(f"_…и ещё {len(pending) - 10} напоминаний_")
        else:
            lines.append("_Активных задач нет._")
    except Exception as e:
        lines.append(f"_Ошибка загрузки: {e}_")

    lines.append(
        "\n💡 *Добавить напоминание:*\n"
        "Напиши мне в чате, например:\n"
        "«напомни завтра в 10 утра позвонить клиенту»\n"
        "«напоминание через 2 часа — проверить водителя»"
    )
    safe_send(chat_id, "\n".join(lines), markup)


def _btn_booking(chat_id: int):
    """Безопасный сценарий бронирования — сначала план, потом подтверждение."""
    ai_chat_mode.add(chat_id)
    text = (
        "🏨 *Бронирование и звонки*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Помогу с:\n"
        "• Отель — поиск, бронирование через сайт/телефон\n"
        "• Ресторан — столик по телефону\n"
        "• Сервис — договориться, уточнить, перенести\n\n"
        "⚠️ *Важно:* я сначала соберу данные → покажу тебе план → "
        "спрошу подтверждение → только потом звоню или бронирую.\n\n"
        "🔒 Никаких автоплатежей без явного *да* от тебя.\n\n"
        "Расскажи что нужно — куда, когда, что?"
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=ai_chat_menu())


def _btn_dota(chat_id: int):
    """Dota 2 Coach — анализ, советы, мета."""
    ai_chat_mode.add(chat_id)
    text = (
        "🎮 *Dota 2 Coach*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Твой персональный тренер по Dota 2. Спрашивай:\n\n"
        "⚔️ *Герои и counter-pick:*\n"
        "«что взять против Pudge + Axe?»\n"
        "«лучший carry для solo-ranked?»\n\n"
        "🛡️ *Сборки и предметы:*\n"
        "«билд на Phantom Assassin в текущем патче»\n"
        "«когда покупать Black King Bar?»\n\n"
        "🗺️ *Стратегия и мета:*\n"
        "«как играть trilane в 2024?»\n"
        "«объясни роль 4-ки»\n\n"
        "📊 *Разбор игр:*\n"
        "Опиши ситуацию — разберём что пошло не так.\n\n"
        "_Нажми «🔙 Выйти из чата» когда закончишь._"
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=ai_chat_menu())


# ══════════════════════════════════════════════════════════════════
# конец новых хендлеров
# ══════════════════════════════════════════════════════════════════

def _btn_sauron_search(chat_id: int):
    """Ручной поиск через Sauron — спрашивает запрос, потом ищет."""
    # Проверяем настройку не показывая credentials
    s_status = sauron.status()
    if "не настроен" in s_status.lower() or "не задан" in s_status:
        markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
        bot.send_message(
            chat_id,
            "🔍 *Sauron — поиск информации*\n\n"
            "⚠️ Не настроено. Добавь в Replit Secrets хотя бы одно:\n\n"
            "*Вариант 1 — API-ключ (рекомендуется):*\n"
            "• `SAURON_API_KEY` — ключ от sauron.info\n\n"
            "*Вариант 2 — логин/пароль:*\n"
            "• `SAURON_USERNAME` — твой логин\n"
            "• `SAURON_PASSWORD` — твой пароль\n\n"
            "После добавления перезапусти бота.\n"
            "_Ключи и пароли в Telegram не присылай — только через Replit Secrets._",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return
    waiting_for_sauron_query.add(chat_id)
    bot.send_message(
        chat_id,
        "🔍 *Sauron — поиск*\n\n"
        "Что искать? Напиши запрос:\n"
        "• ФИО — «Иванов Петр Сергеевич»\n"
        "• Номер телефона — «+380XXXXXXXXX»\n"
        "• Адрес, ИНН или другое\n\n"
        "_Напиши «отмена» чтобы выйти._",
        parse_mode="Markdown",
    )


def _btn_file_sauron(chat_id: int):
    """Запускает поиск по файлу через Sauron — предлагает отправить файл."""
    s_status = sauron.status()
    if "не настроен" in s_status.lower() or "не задан" in s_status:
        markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
        bot.send_message(
            chat_id,
            "📁 *Поиск по файлу — Sauron не настроен*\n\n"
            "Добавь в Replit Secrets:\n"
            "• `SAURON_API_KEY` — API-ключ _(рекомендуется)_\n"
            "• или `SAURON_USERNAME` + `SAURON_PASSWORD`\n\n"
            "После добавления перезапусти бота.",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return
    fmts = file_search.supported_formats()
    waiting_for_file_sauron.add(chat_id)
    bot.send_message(
        chat_id,
        "📁 *Поиск по файлу через Sauron*\n\n"
        f"Поддерживаемые форматы: `{fmts}`\n\n"
        "Отправь файл — бот извлечёт из него телефоны и ФИО, "
        "покажет что нашёл и спросит подтверждение перед отправкой в Sauron.\n\n"
        "_Напиши «отмена» чтобы выйти._",
        parse_mode="Markdown",
    )


def _btn_forget(chat_id):
    grok_history.pop(chat_id, None)
    _save_grok_history()
    bot.send_message(chat_id, "🗑️ Готово — забыл нашу историю. Начинаем с нуля!", reply_markup=main_menu())


def _btn_sheets(chat_id):
    saved = list_sheets()
    inline = types.InlineKeyboardMarkup(row_width=1)
    if saved:
        for name, sheet_id in saved.items():
            url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            inline.add(types.InlineKeyboardButton(f"📄 {name.title()}", url=url))
    inline.add(types.InlineKeyboardButton("➕ Добавить таблицу", callback_data="add_sheet"))
    inline.add(types.InlineKeyboardButton("📊 Аналитика", callback_data="analytics_menu"))
    if saved and chat_id == OWNER_ID:
        inline.add(types.InlineKeyboardButton("🔔 Мониторинг изменений", callback_data="monitor_menu"))
    text = "📗 *Мои таблицы*\n\nНажми — откроется в приложении:" if saved else "📗 *Таблицы*\n\nПока нет сохранённых."
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=inline)


def _btn_geo_toha(chat_id):
    if chat_id in last_location:
        lat, lon = last_location[chat_id]
        maps_link = f"https://maps.google.com/?q={lat},{lon}"
        toha_number = os.environ.get("TOHA_PHONE_NUMBER", "")
        sms_link = make_sms_link(toha_number, f"Гео Руслана: {maps_link}")
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("📱 Открыть SMS для Тохи", url=sms_link))
        markup.row(types.InlineKeyboardButton("🗺️ Открыть в картах", url=maps_link))
        bot.send_message(chat_id, f"📍 Нажми — откроется SMS с геопозицией:\n{maps_link}", reply_markup=markup)
    else:
        bot.send_message(chat_id,
                         "📍 Сначала отправь геопозицию — скрепка 📎 → Геопозиция.",
                         reply_markup=toha_menu())


def _btn_sms_toha(chat_id):
    bot.send_message(chat_id, "💬 Напиши текст SMS для Тохи:")
    waiting_for_sheet_id[chat_id] = "toha_sms"


def _btn_analytics(chat_id):
    saved = list_sheets()
    if not saved:
        bot.send_message(chat_id, "📊 Нет таблиц. Нажми *➕ Сохранить таблицу*.",
                         parse_mode="Markdown", reply_markup=sheets_menu())
    else:
        names = "\n".join([f"• {name}" for name in saved.keys()])
        bot.send_message(chat_id, f"📊 Напиши название таблицы для анализа:\n\n{names}",
                         parse_mode="Markdown")
        waiting_for_sheet_id[chat_id] = "analytics"


def _btn_my_sheets(chat_id):
    saved = list_sheets()
    if not saved:
        bot.send_message(chat_id, "Нет таблиц. Нажми *➕ Сохранить таблицу*.",
                         parse_mode="Markdown", reply_markup=sheets_menu())
    else:
        names = "\n".join([f"• *{name}*" for name in saved.keys()])
        bot.send_message(chat_id, f"📋 *Твои таблицы:*\n\n{names}",
                         parse_mode="Markdown", reply_markup=sheets_menu())


def _btn_save_sheet(chat_id):
    bot.send_message(chat_id,
                     "➕ Отправь название и ID таблицы через пробел:\n\n"
                     "Пример: `Продажи 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms`",
                     parse_mode="Markdown")
    waiting_for_sheet_id[chat_id] = "save_sheet"


def _btn_read_sheet(chat_id):
    bot.send_message(chat_id,
                     "📖 Отправь ID таблицы и диапазон через пробел.\n\nПример:\n`ID_ТАБЛИЦЫ Лист1!A1:E10`",
                     parse_mode="Markdown")
    waiting_for_sheet_id[chat_id] = "read"


def _btn_write_sheet(chat_id):
    bot.send_message(chat_id,
                     "✏️ Отправь ID таблицы, диапазон и данные через пробел.\n\nПример:\n`ID_ТАБЛИЦЫ Лист1!A1 Данные`",
                     parse_mode="Markdown")
    waiting_for_sheet_id[chat_id] = "write"


def _btn_info_sheet(chat_id):
    bot.send_message(chat_id, "ℹ️ Отправь ID таблицы:", parse_mode="Markdown")
    waiting_for_sheet_id[chat_id] = "info"


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


ANKETA_QUESTIONS = [
    ("name",     "1/10 👤 Как тебя полностью зовут? (Имя, фамилия, отчество)"),
    ("birth",    "2/10 🎂 Когда у тебя день рождения? (ДД.ММ.ГГГГ)"),
    ("city",     "3/10 🏙️ В каком городе живёшь?"),
    ("work",     "4/10 💼 Чем занимаешься? Работа, бизнес, должность?"),
    ("family",   "5/10 👨‍👩‍👧 Семья: жена/девушка, дети? Как зовут?"),
    ("goals",    "6/10 🎯 Какие у тебя сейчас главные цели (на 3–6 месяцев)?"),
    ("dislikes", "7/10 🚫 Что не любишь? Что бесит, чего избегаешь?"),
    ("routine",  "8/10 ⏰ Распорядок дня: во сколько встаёшь, ложишься, привычки?"),
    ("contacts", "9/10 ☎️ Кто экстренные контакты? (имя — телефон, через запятую)"),
    ("extra",    "10/10 ✨ Что ещё мне важно о тебе знать? (любая важная инфа)"),
]

anketa_state: dict = {}  # chat_id -> {"step": int, "answers": {key: text}}


def _show_tax_calendar(chat_id: int):
    if chat_id != OWNER_ID:
        return
    text = tax_calendar.format_calendar(_now_local(), months_ahead=6)
    safe_send(chat_id, text, main_menu())


def _seed_tax_reminders_safe():
    """Створює нагадування про податки ФОП на наступні 12 місяців."""
    try:
        created = tax_calendar.seed_reminders(
            chat_id=OWNER_ID,
            now=_now_local(),
            add_reminder_fn=add_reminder,
            list_pending_fn=list_pending,
            months_ahead=12,
        )
        if created:
            print(f"💰 Налоговый календарь ФОП: добавлено {created} новых напоминаний.")
    except Exception as e:
        print(f"⚠️ Не удалось засеять налоговые напоминания: {e}")


def _start_anketa(chat_id: int):
    if chat_id != OWNER_ID:
        return
    anketa_state[chat_id] = {"step": 0, "answers": {}}
    bot.send_message(
        chat_id,
        "📝 *Анкета о тебе*\n\nЯ задам 10 вопросов — отвечай как удобно. "
        "Если на вопрос ответа нет — напиши «-» или «пропустить».\n\n"
        "Чтобы прервать в любой момент — /cancel",
        parse_mode="Markdown",
    )
    _ask_next_anketa(chat_id)


@bot.message_handler(commands=['anketa', 'анкета', 'profile'])
def cmd_anketa(message):
    _start_anketa(message.chat.id)


def _ask_next_anketa(chat_id):
    state = anketa_state.get(chat_id)
    if state is None:
        return
    step = state["step"]
    if step >= len(ANKETA_QUESTIONS):
        _finish_anketa(chat_id)
        return
    _, question = ANKETA_QUESTIONS[step]
    msg = bot.send_message(chat_id, question)
    bot.register_next_step_handler(msg, _handle_anketa_answer)


def _handle_anketa_answer(message):
    chat_id = message.chat.id
    state = anketa_state.get(chat_id)
    if state is None:
        return
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "отмена", "стоп", "/stop"):
        anketa_state.pop(chat_id, None)
        bot.send_message(chat_id, "❌ Анкета отменена.", reply_markup=main_menu())
        return
    step = state["step"]
    key, _ = ANKETA_QUESTIONS[step]
    if text and text not in ("-", "—", "пропустить", "skip"):
        state["answers"][key] = text
    state["step"] = step + 1
    _ask_next_anketa(chat_id)


def _finish_anketa(chat_id):
    state = anketa_state.pop(chat_id, None)
    if not state:
        return
    answers = state["answers"]
    if not answers:
        bot.send_message(chat_id, "🤷 Ничего не записал — все вопросы пропущены.",
                         reply_markup=main_menu())
        return

    LABELS = {
        "name": "ФИО",
        "birth": "День рождения",
        "city": "Город",
        "work": "Чем занимается",
        "family": "Семья",
        "goals": "Текущие цели",
        "dislikes": "Что не любит",
        "routine": "Распорядок дня",
        "contacts": "Экстренные контакты",
        "extra": "Дополнительно",
    }

    saved = 0
    summary_lines = []
    for key, _ in ANKETA_QUESTIONS:
        if key in answers:
            label = LABELS.get(key, key)
            fact = f"{label}: {answers[key]}"
            if add_fact(fact):
                saved += 1
            summary_lines.append(f"• *{label}:* {answers[key]}")

    summary = "\n".join(summary_lines)
    bot.send_message(
        chat_id,
        f"✅ *Анкета сохранена!* Записал {saved} новых факта(ов) в долгосрочную память.\n\n"
        f"{summary}\n\n"
        f"Теперь я буду помнить это в каждом разговоре. "
        f"Посмотреть всё — /memory, добавить факт — /memory <текст>.",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ──────────────────────────────────────────────
# ДОСТУП К СВОЕМУ КОДУ — бот может показать свои исходники
# ──────────────────────────────────────────────
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
CODE_ALLOWED_EXT = {".py", ".json", ".txt", ".md"}
CODE_DENYLIST = {"whitelist.json", "known_users.json", "memory.json"}  # содержат приватные данные

def _list_code_files() -> list[str]:
    files = []
    for name in sorted(os.listdir(CODE_DIR)):
        full = os.path.join(CODE_DIR, name)
        if not os.path.isfile(full):
            continue
        if name in CODE_DENYLIST:
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in CODE_ALLOWED_EXT:
            continue
        files.append(name)
    return files

def _resolve_code_file(name: str) -> str | None:
    """Возвращает абсолютный путь к файлу, если он разрешён, иначе None."""
    name = (name or "").strip().lstrip("/").replace("\\", "/")
    if not name or "/" in name or ".." in name:
        return None
    if name in CODE_DENYLIST:
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in CODE_ALLOWED_EXT:
        return None
    full = os.path.join(CODE_DIR, name)
    if not os.path.isfile(full):
        return None
    return full

def _send_code_list(chat_id: int):
    if chat_id != OWNER_ID:
        return
    files = _list_code_files()
    if not files:
        bot.send_message(chat_id, "Файлов не нашёл.")
        return
    lines = "\n".join(f"• `{f}`" for f in files)
    bot.send_message(chat_id,
        f"📂 Мои файлы:\n\n{lines}\n\n"
        f"Покажу любой — напиши «покажи код <имя>» или /code <имя>",
        parse_mode="Markdown")

def _send_code_file(chat_id: int, name: str):
    if chat_id != OWNER_ID:
        return
    if not name:
        _send_code_list(chat_id)
        return
    path = _resolve_code_file(name)
    if not path:
        bot.send_message(chat_id, f"Нет такого файла или он закрыт: `{name}`", parse_mode="Markdown")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        bot.send_message(chat_id, f"Не смог прочитать: {e}")
        return
    real_name = os.path.basename(path)
    size = len(content)
    # Если короткое — шлём текстом, иначе документом
    if size <= 3500:
        ext = os.path.splitext(real_name)[1].lstrip(".") or "txt"
        bot.send_message(chat_id,
            f"`{real_name}` ({size} симв.)\n```{ext}\n{content}\n```",
            parse_mode="Markdown")
    else:
        bio = io.BytesIO(content.encode("utf-8"))
        bio.name = real_name
        bot.send_document(chat_id, bio, caption=f"📄 {real_name} ({size} симв.)")

@bot.message_handler(commands=['files', 'code_list'])
def cmd_files(message):
    _send_code_list(message.chat.id)

@bot.message_handler(commands=['code'])
def cmd_code(message):
    chat_id = message.chat.id
    if chat_id != OWNER_ID:
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        _send_code_list(chat_id)
        return
    _send_code_file(chat_id, parts[1].strip())


@bot.message_handler(commands=['memory'])
def cmd_memory(message):
    chat_id = message.chat.id
    if chat_id != OWNER_ID:
        return
    args = message.text.strip().split(maxsplit=1)
    # /memory clear — очистить память
    if len(args) > 1 and args[1].strip().lower() in ("clear", "очистить", "сбросить"):
        clear_memory()
        bot.send_message(chat_id, "🗑️ Долгосрочная память очищена.", reply_markup=main_menu())
        return
    # /memory <факт> — добавить вручную
    if len(args) > 1:
        fact = args[1].strip()
        added = add_fact(fact)
        if added:
            bot.send_message(chat_id, f"🧠 Запомнил: {fact}", reply_markup=main_menu())
        else:
            bot.send_message(chat_id, f"🧠 Уже знаю это: {fact}", reply_markup=main_menu())
        return
    safe_send(chat_id, format_for_display(), main_menu())


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
        now = _now_local()
        h = now.hour
        if h < 6:
            greet = "Ночь продуктивная"
        elif h < 12:
            greet = "Доброе утро"
        elif h < 18:
            greet = "Добрый день"
        else:
            greet = "Добрый вечер"
        mem_note = f" Помню {len(history)//2} сообщений из прошлой сессии." if history else ""
        tts_ok = bool(os.environ.get("OPENAI_API_KEY"))  # только прямой ключ, proxy audio не поддерживает
        voice_tip = "🎤 Можешь говорить — отвечу голосом." if tts_ok else "💬 Пиши что нужно."
        bot.send_message(
            chat_id,
            f"⚡ *{greet}, Руслан.*\n\n"
            f"Jarvis онлайн и готов.{mem_note}\n\n"
            f"{voice_tip}\n\n"
            f"Нажми *⚡ Jarvis* для командного центра\n"
            f"или просто пиши/говори — пойму сам.",
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

    # Голос работает ТОЛЬКО через прямой OPENAI_API_KEY — Replit proxy не поддерживает audio.
    if not voice_openai_client:
        markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
        bot.send_message(
            chat_id,
            "🎤 Голос пока не включён: нужно добавить OPENAI_API_KEY в Replit Secrets "
            "и перезапустить бота.\n\n"
            "Ключи и пароли в Telegram не присылай — только через Replit Secrets.",
            reply_markup=markup,
        )
        return

    msg = bot.send_message(chat_id, "🎤 Слушаю…")
    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)
        audio_file = io.BytesIO(downloaded)
        audio_file.name = "voice.ogg"

        # STT — Whisper через прямой OpenAI API (не proxy)
        transcript = voice_openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="text",
            language="ru",
        )
        text = transcript.strip() if isinstance(transcript, str) else transcript.text.strip()

        if not text:
            bot.edit_message_text(
                "⚠️ Не смог расшифровать. Говори чётче или ближе к микрофону.",
                chat_id, msg.message_id,
            )
            return

        bot.edit_message_text(f"🗣️ «{text}»", chat_id, msg.message_id)

        voice_request_chats.add(chat_id)
        try:
            process_text(chat_id, text)
        finally:
            voice_request_chats.discard(chat_id)

    except Exception as e:
        err_str = str(e)
        print(f"Ошибка voice: {err_str}")
        if "timeout" in err_str.lower():
            hint = "Сервер не ответил вовремя — попробуй ещё раз."
        elif "rate" in err_str.lower():
            hint = "Слишком много запросов — подожди секунду."
        elif "auth" in err_str.lower() or "401" in err_str or "403" in err_str:
            hint = "Неверный OPENAI_API_KEY — проверь ключ в Replit Secrets."
        else:
            hint = "Попробуй ещё раз или напиши текстом."
        try:
            bot.edit_message_text(f"⚠️ {hint}", chat_id, msg.message_id)
        except Exception:
            bot.send_message(chat_id, f"⚠️ {hint}")


def _run_file_sauron(chat_id: int, doc, filename: str):
    """Скачивает файл и запускает полный Sauron-поиск через sauron_file_search."""
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
    msg = bot.send_message(
        chat_id,
        f"📄 Получил *{filename}* — читаю файл…",
        parse_mode="Markdown",
    )

    # ── Скачиваем ─────────────────────────────────────────────────────────
    try:
        file_info = bot.get_file(doc.file_id)
        data = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.edit_message_text(f"❌ Не удалось скачать файл: {str(e)[:100]}", chat_id, msg.message_id)
        return

    # ── Превью-парсинг для показа что нашли в файле ──────────────────────
    try:
        inp_records, parse_err = sauron_file_search.parse_input_file(data, filename)
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка чтения файла: {str(e)[:150]}", chat_id, msg.message_id)
        return

    if parse_err:
        try:
            bot.edit_message_text(
                f"📄 *{filename}*\n\n⚠️ {parse_err}", chat_id, msg.message_id, parse_mode="Markdown",
            )
        except Exception:
            pass
        bot.send_message(chat_id, "Главное меню 👇", reply_markup=markup)
        return

    # Показываем превью файла
    preview = sauron_file_search.build_preview(inp_records, filename)
    n = len(inp_records)
    to_check = min(n, sauron_file_search.MAX_FIO)
    skipped  = n - to_check
    try:
        bot.edit_message_text(
            preview + f"\n\n🔍 _Запускаю поиск по {to_check} ФИО…_",
            chat_id, msg.message_id, parse_mode="Markdown",
        )
    except Exception:
        pass

    # ── Основной поиск ────────────────────────────────────────────────────
    try:
        persons, relatives, phone_checks, errors, stop_reason = sauron_file_search.run_file_search(
            data, filename, chat_id, bot, msg.message_id,
        )
    except Exception as e:
        try:
            bot.edit_message_text(f"❌ Ошибка поиска: {str(e)[:200]}", chat_id, msg.message_id)
        except Exception:
            pass
        return

    # ── Краткая сводка в чате ─────────────────────────────────────────────
    summary = sauron_file_search.build_chat_summary(persons, relatives, phone_checks, errors, stop_reason)
    try:
        bot.edit_message_text(summary, chat_id, msg.message_id, parse_mode="Markdown")
    except Exception:
        safe_send(chat_id, summary, markup)

    # ── Отчёт-файл ───────────────────────────────────────────────────────
    if not persons:
        bot.send_message(chat_id, "Главное меню 👇", reply_markup=markup)
        return

    try:
        base      = filename.rsplit('.', 1)[0]
        found_cnt = sum(1 for p in persons if p.found)
        rel_cnt   = len(relatives)
        xlsx_bytes = sauron_file_search.build_xlsx_report(persons, relatives, phone_checks, errors)
        if xlsx_bytes:
            report_name = base + "_sauron.xlsx"
            bio = io.BytesIO(xlsx_bytes)
            bio.name = report_name
            caption = (
                f"📊 Отчёт «{filename}» — 4 листа\n"
                f"✅ Найдено: {found_cnt} / {len(persons)} чел.\n"
                f"👨‍👩‍👧 Родственников: {rel_cnt}"
                + (f"\n⚠️ Пропущено: {skipped}" if skipped else "")
            )
            bot.send_document(chat_id, bio, caption=caption, reply_markup=markup)
        else:
            csv_bytes = sauron_file_search.build_csv_report(persons, relatives)
            report_name = base + "_sauron.csv"
            bio = io.BytesIO(csv_bytes)
            bio.name = report_name
            bot.send_document(
                chat_id, bio,
                caption=(
                    f"📊 Отчёт «{filename}»\n"
                    f"✅ Найдено: {found_cnt} / {len(persons)} чел.\n"
                    f"👨‍👩‍👧 Родственников: {rel_cnt}"
                ),
                reply_markup=markup,
            )
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Отчёт не отправлен: {str(e)[:100]}", reply_markup=markup)


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


CODE_MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 МБ хватит на любой .py
CODE_BACKUP_DIR = os.path.join(CODE_DIR, ".backups")

def _backup_code_file(path: str) -> str | None:
    """Сохраняет текущую версию файла в .backups/имя.timestamp."""
    try:
        os.makedirs(CODE_BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.basename(path)
        dst = os.path.join(CODE_BACKUP_DIR, f"{base}.{ts}.bak")
        with open(path, "rb") as src, open(dst, "wb") as out:
            out.write(src.read())
        return dst
    except Exception:
        return None


@bot.message_handler(content_types=['document'])
def handle_document(message):
    """Единый обработчик документов.

    Роутинг:
    • csv / xlsx / xls / docx / pdf → всегда Sauron-поиск (автоматически)
    • txt → Sauron если пользователь в режиме ожидания файла, иначе — код
    • py / json / md  → загрузка кода (только владелец)
    """
    chat_id = message.chat.id
    if not is_allowed(chat_id):
        return

    doc      = message.document
    filename = (doc.file_name or "file").strip()
    ext      = os.path.splitext(filename)[1].lower()

    # ── Роутинг: Sauron ───────────────────────────────────────────────────
    # .txt включён: список ФИО чаще всего приходит именно в txt/csv/xlsx
    SAURON_EXTS = {'.csv', '.xlsx', '.xls', '.docx', '.pdf', '.txt'}
    is_sauron_mode = chat_id in waiting_for_file_sauron
    is_sauron_ext  = ext in SAURON_EXTS
    if is_sauron_ext or is_sauron_mode:
        waiting_for_file_sauron.discard(chat_id)
        _run_file_sauron(chat_id, doc, filename)
        return

    # ── Роутинг: загрузка кода (только владелец) ─────────────────────────
    if chat_id != OWNER_ID:
        # Не владелец, не Sauron-файл — молча игнорируем
        return

    caption     = (message.caption or "").strip()
    target_name = caption if caption else filename

    if not target_name:
        bot.send_message(chat_id, "❌ Нет имени файла.")
        return
    if doc.file_size and doc.file_size > CODE_MAX_UPLOAD_BYTES:
        bot.send_message(chat_id, f"❌ Файл слишком большой. Лимит — {CODE_MAX_UPLOAD_BYTES // 1024} КБ.")
        return

    safe_name = os.path.basename(target_name).strip().lstrip(".")
    if not safe_name or "/" in safe_name or "\\" in safe_name or ".." in safe_name:
        bot.send_message(chat_id, f"❌ Плохое имя файла: `{target_name}`", parse_mode="Markdown")
        return
    if safe_name in CODE_DENYLIST:
        bot.send_message(chat_id, f"❌ Этот файл менять не буду: `{safe_name}` (приватные данные).", parse_mode="Markdown")
        return
    code_ext = os.path.splitext(safe_name)[1].lower()
    if code_ext not in CODE_ALLOWED_EXT:
        # Неизвестный формат от владельца — подсказываем про Sauron
        if code_ext in {'.txt'}:
            waiting_for_file_sauron.add(chat_id)
            _run_file_sauron(chat_id, doc, filename)
        else:
            allowed = ", ".join(sorted(CODE_ALLOWED_EXT))
            bot.send_message(chat_id, f"❌ Расширение `{code_ext or '(нет)'}` не разрешено для кода. Допустимо: {allowed}", parse_mode="Markdown")
        return

    full_path  = os.path.join(CODE_DIR, safe_name)
    is_replace = os.path.isfile(full_path)

    try:
        file_info = bot.get_file(doc.file_id)
        raw = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Не смог скачать файл: {e}")
        return

    if code_ext == ".py":
        try:
            import ast as _ast
            _ast.parse(raw.decode("utf-8"))
        except SyntaxError as e:
            bot.send_message(chat_id, f"❌ В `{safe_name}` синтаксическая ошибка — не сохраняю:\n`{e}`", parse_mode="Markdown")
            return
        except Exception as e:
            bot.send_message(chat_id, f"❌ Не смог разобрать как Python: {e}")
            return
    elif code_ext == ".json":
        try:
            import json as _json
            _json.loads(raw.decode("utf-8"))
        except Exception as e:
            bot.send_message(chat_id, f"❌ Невалидный JSON — не сохраняю:\n`{e}`", parse_mode="Markdown")
            return

    backup_path = _backup_code_file(full_path) if is_replace else None

    try:
        tmp_path = full_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(raw)
        os.replace(tmp_path, full_path)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Не смог записать файл: {e}")
        return

    size = len(raw)
    verb = "Перезаписал" if is_replace else "Создал"
    out_msg = f"✅ {verb} `{safe_name}` ({size} байт)."
    if backup_path:
        out_msg += f"\n💾 Бэкап: `{os.path.relpath(backup_path, CODE_DIR)}`"
    if code_ext == ".py":
        out_msg += "\n\n♻️ Чтобы изменения подхватились, перезапусти бота (workflow «Telegram Bot»)."
    bot.send_message(chat_id, out_msg, parse_mode="Markdown")


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)

    # ── Поиск по файлу — подтверждение (legacy callback) ─────────────────
    if call.data == "filesauron_yes":
        file_sauron_pending.pop(chat_id, None)
        # Теперь поиск запускается автоматически при получении файла.
        # Этот callback оставлен для совместимости.
        bot.send_message(
            chat_id,
            "📁 Отправь файл снова — поиск запустится автоматически.",
        )
        return

    elif call.data == "filesauron_no":
        file_sauron_pending.pop(chat_id, None)
        markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
        bot.send_message(chat_id, "📁 Поиск отменён.", reply_markup=markup)
        return

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

    elif call.data == "monitor_menu":
        if chat_id != OWNER_ID:
            bot.send_message(chat_id, "🔒 Мониторингом управляет только Руслан.")
            return
        _show_monitor_menu(chat_id)

    elif call.data.startswith("alert_analytics:"):
        if chat_id != OWNER_ID:
            bot.send_message(chat_id, "🔒 Аналитика по таблицам — только для Руслана.")
            return
        sheet_id = call.data.split(":", 1)[1]
        if sheet_id not in set(list_sheets().values()):
            bot.send_message(chat_id, "⚠️ Эта таблица больше не зарегистрирована.")
            return
        try:
            result = analyze_sheet_with_ai(sheet_id)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Не удалось получить аналитику: {e}")
            return
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "📄 Открыть таблицу",
            url=f"https://docs.google.com/spreadsheets/d/{sheet_id}",
        ))
        safe_send(chat_id, result, markup)

    elif call.data.startswith("alert_monitor_off:"):
        if chat_id != OWNER_ID:
            bot.send_message(chat_id, "🔒 Мониторингом управляет только Руслан.")
            return
        sheet_id = call.data.split(":", 1)[1]
        try:
            sheet_monitor.set_enabled(sheet_id, False)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Не удалось выключить мониторинг: {e}")
            return
        bot.send_message(chat_id, "🔕 Мониторинг этой таблицы выключен. Включить обратно — в меню 📗 Таблицы → 🔔 Мониторинг изменений.")

    elif call.data.startswith("alert_snooze:"):
        if chat_id != OWNER_ID:
            bot.send_message(chat_id, "🔒 Мониторингом управляет только Руслан.")
            return
        sheet_id = call.data.split(":", 1)[1]
        try:
            wake = sheet_monitor.snooze_until_morning(sheet_id, _now_local())
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Не удалось приглушить мониторинг: {e}")
            return
        bot.send_message(
            chat_id,
            f"😴 Тише до {wake.strftime('%H:%M')}. С утра мониторинг сам проснётся.",
        )

    elif call.data.startswith("monitor_toggle:"):
        if chat_id != OWNER_ID:
            bot.send_message(chat_id, "🔒 Мониторингом управляет только Руслан.")
            return
        sheet_id = call.data.split(":", 1)[1]
        new_state = not sheet_monitor.is_enabled(sheet_id)
        try:
            sheet_monitor.set_enabled(sheet_id, new_state)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Не удалось включить мониторинг: {e}")
            return
        status = "включён" if new_state else "выключен"
        bot.send_message(chat_id, f"🔔 Мониторинг {status}.")
        _show_monitor_menu(chat_id)

    elif call.data == "monitor_settings":
        if chat_id != OWNER_ID:
            bot.send_message(chat_id, "🔒 Настройками управляет только Руслан.")
            return
        _show_monitor_settings(chat_id)

    elif call.data.startswith("monitor_set_interval:"):
        if chat_id != OWNER_ID:
            return
        try:
            value = float(call.data.split(":", 1)[1])
            sheet_monitor.update_settings(interval_hours=value)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Не удалось сохранить: {e}")
            return
        _show_monitor_settings(chat_id)

    elif call.data.startswith("monitor_set_pct:"):
        if chat_id != OWNER_ID:
            return
        try:
            value = float(call.data.split(":", 1)[1])
            sheet_monitor.update_settings(change_pct=value)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Не удалось сохранить: {e}")
            return
        _show_monitor_settings(chat_id)

    elif call.data.startswith("ms:"):
        # Per-sheet settings menu (короткий префикс из-за 64-байтного лимита
        # callback_data в Telegram: sheet_id уже занимает ~44 символа).
        if chat_id != OWNER_ID:
            bot.send_message(chat_id, "🔒 Настройками управляет только Руслан.")
            return
        sheet_id = call.data.split(":", 1)[1]
        _show_sheet_monitor_settings(chat_id, sheet_id)

    elif call.data.startswith("msp:"):
        # msp:<sheet_id>:<value|none> — порог % для конкретной таблицы.
        if chat_id != OWNER_ID:
            return
        try:
            _, sheet_id, value = call.data.split(":", 2)
            sheet_monitor.set_sheet_override(
                sheet_id, "change_pct", None if value == "none" else float(value)
            )
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Не удалось сохранить: {e}")
            return
        _show_sheet_monitor_settings(chat_id, sheet_id)

    elif call.data.startswith("msi:"):
        # msi:<sheet_id>:<value|none> — интервал проверки для конкретной таблицы.
        if chat_id != OWNER_ID:
            return
        try:
            _, sheet_id, value = call.data.split(":", 2)
            sheet_monitor.set_sheet_override(
                sheet_id, "interval_hours", None if value == "none" else float(value)
            )
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Не удалось сохранить: {e}")
            return
        _show_sheet_monitor_settings(chat_id, sheet_id)

    elif call.data.startswith("monitor_set_hours:"):
        if chat_id != OWNER_ID:
            return
        try:
            start_s, end_s = call.data.split(":", 1)[1].split("-")
            sheet_monitor.update_settings(
                business_hour_start=int(start_s),
                business_hour_end=int(end_s),
            )
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Не удалось сохранить: {e}")
            return
        _show_monitor_settings(chat_id)


def _show_monitor_menu(chat_id):
    monitored = sheet_monitor.list_monitored()
    if not monitored:
        bot.send_message(chat_id, "📊 Нет таблиц для мониторинга.")
        return
    inline = types.InlineKeyboardMarkup(row_width=2)
    for sheet_id, info in monitored.items():
        mark = "✅" if info["enabled"] else "⬜️"
        # Каждой таблице — отдельная строка: переключатель + кнопка
        # перехода к её собственным настройкам чувствительности.
        inline.row(
            types.InlineKeyboardButton(
                f"{mark} {info['name'].title()}",
                callback_data=f"monitor_toggle:{sheet_id}",
            ),
            types.InlineKeyboardButton(
                "⚙️ настроить",
                callback_data=f"ms:{sheet_id}",
            ),
        )
    inline.add(types.InlineKeyboardButton("⚙️ Общие настройки", callback_data="monitor_settings"))
    s = sheet_monitor.get_settings()
    interval = _fmt_hours(s["interval_hours"])
    pct = int(s["change_pct"])
    bh_start, bh_end = s["business_hour_start"], s["business_hour_end"]
    if bh_start <= 0 and bh_end >= 24:
        hours_str = "круглосуточно"
    else:
        hours_str = f"{bh_start:02d}:00–{bh_end:02d}:00"
    text = (
        "🔔 *Мониторинг таблиц*\n\n"
        f"Проверка каждые {interval}, порог изменений ≥ {pct}%.\n"
        f"Рабочие часы: {hours_str}.\n"
        "Нажми чтобы включить/выключить."
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=inline)


def _fmt_hours(h: float) -> str:
    return f"{int(h)} ч" if float(h).is_integer() else f"{h} ч"


def _show_monitor_settings(chat_id):
    s = sheet_monitor.get_settings()
    cur_interval = float(s["interval_hours"])
    cur_pct = float(s["change_pct"])
    cur_start = int(s["business_hour_start"])
    cur_end = int(s["business_hour_end"])

    inline = types.InlineKeyboardMarkup(row_width=4)

    interval_buttons = []
    for v in sheet_monitor.ALLOWED_INTERVALS:
        mark = "✅ " if v == cur_interval else ""
        label = f"{mark}{int(v)}ч"
        interval_buttons.append(types.InlineKeyboardButton(
            label, callback_data=f"monitor_set_interval:{int(v)}"
        ))
    inline.row(*interval_buttons)

    pct_buttons = []
    for v in sheet_monitor.ALLOWED_CHANGE_PCT:
        mark = "✅ " if v == cur_pct else ""
        pct_buttons.append(types.InlineKeyboardButton(
            f"{mark}{int(v)}%", callback_data=f"monitor_set_pct:{int(v)}"
        ))
    inline.row(*pct_buttons)

    hours_buttons = []
    for start, end in sheet_monitor.ALLOWED_BUSINESS_HOURS:
        mark = "✅ " if start == cur_start and end == cur_end else ""
        if start <= 0 and end >= 24:
            label = f"{mark}24/7"
        else:
            label = f"{mark}{start}-{end}"
        hours_buttons.append(types.InlineKeyboardButton(
            label, callback_data=f"monitor_set_hours:{start}-{end}"
        ))
    inline.row(*hours_buttons)

    inline.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="monitor_menu"))

    if cur_start <= 0 and cur_end >= 24:
        hours_str = "круглосуточно"
    else:
        hours_str = f"{cur_start:02d}:00–{cur_end:02d}:00"
    text = (
        "⚙️ *Настройки мониторинга*\n\n"
        f"⏱ Интервал проверки: *{_fmt_hours(cur_interval)}*\n"
        f"📊 Порог изменений: *≥ {int(cur_pct)}%*\n"
        f"🕒 Рабочие часы: *{hours_str}*\n\n"
        "Выбери новые значения — применятся сразу."
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=inline)


def _show_sheet_monitor_settings(chat_id, sheet_id):
    """Меню настроек чувствительности конкретной таблицы.
    Кнопка «как общие» сбрасывает override; конкретное значение — задаёт его.
    Рабочие часы оставлены глобальными — их Руслан меняет реже и едино."""
    if sheet_id not in set(list_sheets().values()):
        bot.send_message(chat_id, "⚠️ Эта таблица больше не зарегистрирована.")
        return
    # Имя таблицы для шапки — ищем обратным проходом по реестру.
    name = next((n for n, sid in list_sheets().items() if sid == sheet_id), sheet_id)
    common = sheet_monitor.get_settings()
    overrides = sheet_monitor.get_sheet_overrides(sheet_id)
    cur_pct = overrides.get("change_pct")          # None = «как общие»
    cur_interval = overrides.get("interval_hours")  # None = «как общие»

    inline = types.InlineKeyboardMarkup(row_width=4)

    # Порог %: «как общие» + конкретные значения.
    pct_buttons = [types.InlineKeyboardButton(
        ("✅ " if cur_pct is None else "") + "как общие",
        callback_data=f"msp:{sheet_id}:none",
    )]
    for v in sheet_monitor.ALLOWED_CHANGE_PCT:
        mark = "✅ " if cur_pct == v else ""
        pct_buttons.append(types.InlineKeyboardButton(
            f"{mark}{int(v)}%", callback_data=f"msp:{sheet_id}:{int(v)}",
        ))
    inline.row(*pct_buttons)

    # Интервал проверки: «как общие» + конкретные значения.
    int_buttons = [types.InlineKeyboardButton(
        ("✅ " if cur_interval is None else "") + "как общие",
        callback_data=f"msi:{sheet_id}:none",
    )]
    for v in sheet_monitor.ALLOWED_INTERVALS:
        mark = "✅ " if cur_interval == v else ""
        int_buttons.append(types.InlineKeyboardButton(
            f"{mark}{int(v)}ч", callback_data=f"msi:{sheet_id}:{int(v)}",
        ))
    inline.row(*int_buttons)

    inline.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="monitor_menu"))

    pct_str = (
        f"≥ {int(cur_pct)}%" if cur_pct is not None
        else f"как общие (≥ {int(common['change_pct'])}%)"
    )
    int_str = (
        _fmt_hours(cur_interval) if cur_interval is not None
        else f"как общие ({_fmt_hours(common['interval_hours'])})"
    )
    text = (
        f"⚙️ *Настройки таблицы «{name.title()}»*\n\n"
        f"📊 Порог изменений: *{pct_str}*\n"
        f"⏱ Интервал проверки: *{int_str}*\n\n"
        "«как общие» — таблица берёт значение из общих настроек."
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=inline)


# ──────────────────────────────────────────────
# УТРЕННЯЯ СВОДКА И ПЛАНИРОВЩИК НАПОМИНАНИЙ
# ──────────────────────────────────────────────

MORNING_BRIEFING_HOUR = 8   # 08:00 по местному времени
MORNING_BRIEFING_FILE = "morning_briefing.json"

_morning_lock = threading.Lock()


def _load_last_briefing_date() -> str:
    if os.path.exists(MORNING_BRIEFING_FILE):
        try:
            with open(MORNING_BRIEFING_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return data.get("last_date", "")
        except Exception:
            pass
    return ""


def _save_last_briefing_date(date_str: str):
    with open(MORNING_BRIEFING_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_date": date_str}, f)


def _fetch_weather_kyiv() -> str:
    """Получает погоду для Киева через Open-Meteo (без API ключа)."""
    try:
        import urllib.request
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=50.45&longitude=30.52"
            "&current=temperature_2m,weathercode,windspeed_10m"
            "&hourly=temperature_2m,precipitation_probability"
            "&forecast_days=1"
            "&timezone=Europe%2FKiev"
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        cur = data.get("current", {})
        temp = cur.get("temperature_2m", "?")
        wind = cur.get("windspeed_10m", "?")
        code = cur.get("weathercode", 0)

        WMO = {
            0: "☀️ Ясно", 1: "🌤 Преимущественно ясно", 2: "⛅️ Переменная облачность",
            3: "☁️ Пасмурно", 45: "🌫 Туман", 48: "🌫 Изморозь",
            51: "🌦 Лёгкая морось", 53: "🌦 Морось", 55: "🌧 Плотная морось",
            61: "🌧 Небольшой дождь", 63: "🌧 Дождь", 65: "🌧 Сильный дождь",
            71: "🌨 Небольшой снег", 73: "🌨 Снег", 75: "❄️ Сильный снег",
            80: "🌦 Кратковременный дождь", 81: "🌧 Дождь", 82: "⛈ Ливень",
            95: "⛈ Гроза", 96: "⛈ Гроза с градом", 99: "⛈ Сильная гроза"
        }
        desc = WMO.get(code, f"код {code}")

        # Максимальный шанс осадков за день
        hourly = data.get("hourly", {})
        precip_probs = hourly.get("precipitation_probability", [])
        max_precip = max(precip_probs[:24]) if precip_probs else 0

        result = f"🌡 {temp}°C | {desc} | 💨 {wind} км/ч"
        if max_precip >= 30:
            result += f" | 🌧 Вероятность осадков до {max_precip}%"
        return result
    except Exception as e:
        return f"⚠️ Погода недоступна ({e})"


def _send_morning_briefing():
    """Отправляет утреннюю сводку Руслану."""
    now = _now_local()
    today = now.strftime("%Y-%m-%d")

    with _morning_lock:
        if _load_last_briefing_date() == today:
            return  # Уже отправляли сегодня
        # Не помечаем как отправленное до успешной отправки — см. ниже

    try:
        date_str = now.strftime("%d.%m.%Y")
        weather = _fetch_weather_kyiv()

        # Формируем утреннюю сводку
        lines = [
            f"☀️ *Доброе утро, Руслан!* ({date_str})",
            "",
            f"🌍 *Погода (Киев):* {weather}",
            "",
        ]

        # Краткая аналитика до 2 зарегистрированных таблиц
        saved = list_sheets()
        if saved:
            sheet_names = list(saved.keys())
            lines.append("📊 *Аналитика таблиц:*")
            lines.append("")
            for name in sheet_names[:2]:
                sheet_id = saved[name]
                try:
                    analysis = analyze_sheet_with_ai(sheet_id)
                    # Обрезаем до ~600 символов, чтобы сводка оставалась краткой
                    if len(analysis) > 600:
                        analysis = analysis[:600].rsplit("\n", 1)[0] + "\n..."
                    lines.append(f"*{name.title()}:*")
                    lines.append(analysis)
                    lines.append("")
                except Exception as e:
                    lines.append(f"*{name.title()}:* ⚠️ Не удалось получить аналитику ({e})")
                    lines.append("")
            if len(sheet_names) > 2:
                remaining = sheet_names[2:]
                lines.append(f"📋 Ещё таблиц: {', '.join(remaining)}")
                lines.append("")
        else:
            lines.append("📊 Таблиц пока нет. Добавь через меню 📗 Таблицы.")
            lines.append("")

        # Предстоящие напоминания на сегодня
        pending = list_pending(OWNER_ID)
        today_reminders = sorted(
            [r for r in pending if r.get("fire_at", "")[:10] == today],
            key=lambda r: r.get("fire_at", ""),
        )
        if today_reminders:
            lines.append("📅 *Напоминания на сегодня:*")
            for r in today_reminders:
                try:
                    fire_at = datetime.strptime(r["fire_at"], "%Y-%m-%dT%H:%M")
                    lines.append(f"  ⏰ {fire_at.strftime('%H:%M')} — {r['text']}")
                except Exception:
                    lines.append(f"  ⏰ {r['text']}")
            lines.append("")

        lines.append("Хорошего дня! 💪")

        safe_send(OWNER_ID, "\n".join(lines), main_menu())

        # Помечаем как отправленное только после успешной отправки
        with _morning_lock:
            _save_last_briefing_date(today)

    except Exception as e:
        print(f"Ошибка утренней сводки: {e}")


_last_sheet_monitor_run: datetime | None = None


def _maybe_run_sheet_monitor(now_local: datetime):
    """Запускает проверку таблиц не чаще раз в N часов.
    Интервал берётся из настроек мониторинга (Руслан меняет его из чата),
    env используется только как дефолт при первом запуске."""
    global _last_sheet_monitor_run
    # Тикaем с шагом самой «торопливой» таблицы: если у одной из них override
    # 1ч, а у остальных — общие 6ч, планировщик должен заходить раз в час,
    # а check_all сам отфильтрует, какие именно таблицы пора проверять.
    interval_hours = sheet_monitor.min_effective_interval_hours()
    if _last_sheet_monitor_run is not None:
        elapsed = (now_local - _last_sheet_monitor_run).total_seconds() / 3600.0
        if elapsed < interval_hours:
            return
    try:
        alerts = sheet_monitor.check_all(now_local)
    except Exception as e:
        print(f"Ошибка мониторинга таблиц: {e}")
        return
    # Помечаем как успешный запуск только после завершения, чтобы при сбое
    # повторить раньше, а не ждать целый интервал.
    _last_sheet_monitor_run = now_local
    if not alerts:
        return

    # Если в одном цикле сработало больше одной таблицы — схлопываем в одно
    # сводное сообщение со списком таблиц и кнопками по каждой, чтобы Руслан
    # не получал «лавину» отдельных уведомлений. По одному алерту шлём как
    # раньше — с полным текстом и кнопками действий.
    if len(alerts) == 1:
        alert = alerts[0]
        sheet_id = alert["sheet_id"]
        inline = types.InlineKeyboardMarkup(row_width=1)
        inline.add(types.InlineKeyboardButton(
            "📊 Подробная аналитика",
            callback_data=f"alert_analytics:{sheet_id}",
        ))
        inline.add(types.InlineKeyboardButton(
            "📄 Открыть таблицу",
            url=f"https://docs.google.com/spreadsheets/d/{sheet_id}",
        ))
        inline.add(types.InlineKeyboardButton(
            "😴 Тише до утра",
            callback_data=f"alert_snooze:{sheet_id}",
        ))
        inline.add(types.InlineKeyboardButton(
            "🔕 Выключить мониторинг",
            callback_data=f"alert_monitor_off:{sheet_id}",
        ))
        try:
            safe_send(OWNER_ID, alert["text"], inline)
        except Exception as e:
            print(f"Не удалось отправить алерт мониторинга: {e}")
        return

    lines = [f"🔔 *Изменения сразу в {len(alerts)} таблицах:*", ""]
    inline = types.InlineKeyboardMarkup(row_width=2)
    for alert in alerts:
        sheet_id = alert["sheet_id"]
        name = str(alert.get("name", "")).title()
        lines.append(f"• *{name}*")
        # Три компактные кнопки в ряд по каждой таблице: аналитика + snooze + выключить.
        # Полные кнопки (включая «Открыть таблицу») доступны через аналитику и
        # из меню — здесь экономим место, чтобы клавиатура не разрасталась.
        # Префиксы коротких callback_data (alert_*) подобраны так, чтобы вместе
        # с 44-символьным sheet_id вписаться в лимит Telegram (64 байта).
        inline.add(
            types.InlineKeyboardButton(
                f"📊 {name}",
                callback_data=f"alert_analytics:{sheet_id}",
            ),
            types.InlineKeyboardButton(
                f"😴 {name}",
                callback_data=f"alert_snooze:{sheet_id}",
            ),
            types.InlineKeyboardButton(
                f"🔕 {name}",
                callback_data=f"alert_monitor_off:{sheet_id}",
            ),
        )
    try:
        safe_send(OWNER_ID, "\n".join(lines), inline)
    except Exception as e:
        print(f"Не удалось отправить сводный алерт мониторинга: {e}")


def _scheduler_loop():
    """Фоновый поток: проверяет напоминания каждую минуту, отправляет утреннюю сводку в 8:00."""
    print("⏰ Планировщик напоминаний запущен.")
    while True:
        try:
            now = _now_local()

            # Утренняя сводка в MORNING_BRIEFING_HOUR:xx — окно целого часа,
            # чтобы не пропустить при перезапуске бота в HH:01+
            if now.hour == MORNING_BRIEFING_HOUR:
                _send_morning_briefing()

            # Периодический мониторинг изменений в Google Sheets
            _maybe_run_sheet_monitor(now)

            # Проверяем и отправляем просроченные напоминания
            due = get_due(now)
            for reminder in due:
                chat_id = reminder.get("chat_id")
                text = reminder.get("text", "")
                reminder_id = reminder.get("id", "")
                try:
                    # safe_send обрабатывает ошибки Markdown-разметки (fallback на plain text),
                    # поэтому пользовательский текст напоминания не может сломать отправку
                    safe_send(chat_id, f"⏰ *Напоминание!*\n\n{text}", main_menu())
                    mark_fired(reminder_id)
                except Exception as e:
                    print(f"Ошибка отправки напоминания {reminder_id}: {e}")
                    # Увеличиваем счётчик ошибок; после MAX_FAILURES — деактивируем
                    mark_failed(reminder_id)

        except Exception as e:
            print(f"Ошибка в планировщике: {e}")

        time.sleep(60)


if __name__ == "__main__":
    keep_alive()
    # Запускаем планировщик напоминаний в фоновом потоке
    scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="reminder-scheduler")
    scheduler_thread.start()
    # Засеваем налоговый календарь ФОП — идемпотентно, дубли не создаст
    _seed_tax_reminders_safe()
    print("🚀 Ruslan Personal Helper с SMS для Тохи!")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            err = str(e)
            if "409" in err or "Conflict" in err:
                print(
                    "⛔ 409 CONFLICT: бот уже запущен в другом месте!\n"
                    "   Telegram разрешает ОДИН активный polling на токен.\n"
                    "   Останови бот на ПК ИЛИ останови Replit-воркфлоу — "
                    "запускай только в одном месте.\n"
                    "   Повтор через 30 секунд..."
                )
                time.sleep(30)
            else:
                print(f"Ошибка polling: {e}. Перезапуск через 5 секунд...")
                time.sleep(5)
