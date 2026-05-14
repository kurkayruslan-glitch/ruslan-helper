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
from memory import get_facts, add_fact, delete_fact, clear_facts, format_for_prompt, format_for_display, clear_all as clear_memory
from zona import zona_search, zona_detail, build_index, index_exists, index_size
from tron import get_usdt_transactions, get_account_balance, build_tx_summary
from reminders import add_reminder, get_due, mark_fired, mark_failed, list_pending, cancel_reminder
import sheet_monitor

import threading
import json
import re
from datetime import datetime, timedelta

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# UTC offset for Ukraine (EET = UTC+2, adjust to +3 in summer if needed)
TZ_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "2"))

def _now_local() -> datetime:
    """Текущее местное время (UTC + TZ_HOURS)."""
    return datetime.utcnow() + timedelta(hours=TZ_HOURS)

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


def _tts_send_voice(chat_id: int, text: str):
    """Конвертирует текст в голос через gpt-audio-mini и отправляет как голосовое сообщение."""
    import re, base64
    try:
        clean = re.sub(r"\[ACTION:[^\]]*\]", "", text)
        clean = re.sub(r"[*_`#>]", "", clean).strip()
        if not clean:
            return
        response = openai_client.chat.completions.create(
            model="gpt-audio-mini",
            modalities=["text", "audio"],
            audio={"voice": "onyx", "format": "opus"},
            messages=[
                {"role": "system", "content": "Прочитай текст вслух естественно и живо, по-русски."},
                {"role": "user", "content": clean},
            ],
        )
        audio_data = base64.b64decode(response.choices[0].message.audio.data)
        audio_bytes = io.BytesIO(audio_data)
        audio_bytes.name = "reply.opus"
        bot.send_voice(chat_id, audio_bytes)
    except Exception as e:
        print(f"TTS ошибка: {e}")
        # Graceful fallback — текстовый ответ уже будет отправлен

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

# Чаты, ожидающие голосового ответа (запрос пришёл голосом)
voice_request_chats: set = set()

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
    markup.add("🛣️ Маршрут",     "📝 Анкета")
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
    match = re.match(r"^\[ACTION:([^\]]+)\]\s*", stripped)
    if not match:
        return None, None, reply
    # Возвращаем оригинальный текст без тега, сохраняя его начало
    reply = stripped
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

    elif action_type == "zona_search":
        name = action_param or ""
        if not name:
            bot.send_message(chat_id, "⚠️ Укажи фамилию для поиска. Пример: найди Иванов Иван")
            return
        if not index_exists():
            bot.send_message(chat_id, "⏳ База 200.zona.media не загружена. Напиши /zona_build чтобы загрузить (~2 мин).")
            return
        bot.send_chat_action(chat_id, "typing")
        result = zona_search(name)
        safe_send(chat_id, result, main_menu())

    elif action_type == "zona_detail":
        name = action_param or ""
        if not name:
            bot.send_message(chat_id, "⚠️ Укажи фамилию и имя.")
            return
        if not index_exists():
            bot.send_message(chat_id, "⏳ База 200.zona.media не загружена. Напиши /zona_build чтобы загрузить (~2 мин).")
            return
        bot.send_chat_action(chat_id, "typing")
        result, _ = zona_detail(name)
        safe_send(chat_id, result, main_menu())

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
    date_line = f"\nСейчас: {now.strftime('%Y-%m-%dT%H:%M')} (UTC+{TZ_HOURS}, Украина).\n"
    memory_block = date_line + memory_block
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
            waiting_for_owner_call.pop(chat_id)
            bot.send_message(chat_id, f"📞 Звоню на {number}...")
            ok, info = make_call(number, t)
            if ok:
                bot.send_message(chat_id, f"✅ Позвонил на *{number}*!\nГолосом скажет: _{t}_",
                                 parse_mode="Markdown", reply_markup=main_menu())
            else:
                bot.send_message(chat_id, f"❌ Ошибка звонка: {info}", reply_markup=main_menu())
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

    BUTTON_LABELS = {
        "📞 позвонить": lambda: _btn_call(chat_id),
        "🚕 тоха": lambda: bot.send_message(chat_id, "🚕 Что делаем с Тохой?", reply_markup=toha_menu()),
        "📍 геопозиция": lambda: bot.send_message(chat_id, "📍 Отправь геопозицию — нажми скрепку 📎 → Геопозиция"),
        "📗 таблицы": lambda: _btn_sheets(chat_id),
        "💰 usdt крипто": lambda: _btn_usdt(chat_id),
        "📊 я тигр": lambda: _ask_grok_and_route(chat_id, "Сделай полную статистику по бизнесу Я Тигр"),
        "📋 фоп": lambda: _ask_grok_and_route(chat_id, "Расскажи что нужно знать о ФОП 3 группы в Украине"),
        "🗑️ забыть": lambda: _btn_forget(chat_id),
        "🛣️ маршрут": lambda: _ask_grok_and_route(chat_id, "Помоги с маршрутом"),
        "📝 анкета": lambda: _start_anketa(chat_id),
        "анкета": lambda: _start_anketa(chat_id),
        "/анкета": lambda: _start_anketa(chat_id),
        "/anketa": lambda: _start_anketa(chat_id),
        "/profile": lambda: _start_anketa(chat_id),
        # Toha sub-menu
        "📍 отправить гео тохе": lambda: _btn_geo_toha(chat_id),
        "💬 написать тохе sms": lambda: _btn_sms_toha(chat_id),
        # Sheets sub-menu
        "📊 аналитика таблицы": lambda: _btn_analytics(chat_id),
        "📋 мои таблицы": lambda: _btn_my_sheets(chat_id),
        "➕ сохранить таблицу": lambda: _btn_save_sheet(chat_id),
        "📖 читать таблицу": lambda: _btn_read_sheet(chat_id),
        "✏️ записать в таблицу": lambda: _btn_write_sheet(chat_id),
        "ℹ️ инфо о таблице": lambda: _btn_info_sheet(chat_id),
        "🔙 назад": lambda: bot.send_message(chat_id, "Главное меню 👇", reply_markup=main_menu()),
    }

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


@bot.message_handler(commands=['zona_build'])
def cmd_zona_build(message):
    chat_id = message.chat.id
    if chat_id != OWNER_ID:
        return
    if index_exists():
        n = index_size()
        bot.send_message(chat_id, f"📊 База уже загружена: *{n:,}* записей.\n\nДля перезагрузки подожди и запусти снова.", parse_mode="Markdown")
        return
    bot.send_message(chat_id, "⏳ Загружаю индекс базы 200.zona.media... (~2-3 минуты, 220,000+ записей)")

    def _build():
        try:
            def progress(i, total, count):
                if i % 5 == 0:
                    bot.send_message(chat_id, f"📥 Загружено {i}/{total} файлов, {count:,} записей...")
            total = build_index(progress_cb=progress)
            bot.send_message(chat_id, f"✅ Индекс готов! *{total:,}* записей загружено.\n\nТеперь можешь искать: «найди Иванов Иван»", parse_mode="Markdown")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Ошибка загрузки: {e}")

    import threading
    threading.Thread(target=_build, daemon=True).start()


@bot.message_handler(commands=['zona_scan'])
def cmd_zona_scan(message):
    chat_id = message.chat.id
    if chat_id != OWNER_ID:
        return
    import scan_contacts
    import threading

    arg = ""
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        arg = parts[1].strip().lower()

    if arg == "stop":
        if scan_contacts.IS_RUNNING.is_set():
            scan_contacts.STOP_EVENT.set()
            bot.send_message(chat_id, "⏹ Останавливаю сканер... (завершит текущий батч)")
        else:
            bot.send_message(chat_id, "ℹ️ Сканер не запущен.")
        return

    if scan_contacts.IS_RUNNING.is_set():
        bot.send_message(chat_id, "⚠️ Сканер уже работает. Останови: /zona_scan stop")
        return

    if not index_exists():
        bot.send_message(chat_id, "❌ Индекс zona.media не загружен. Сначала /zona_build")
        return

    bot.send_message(chat_id,
        f"🚀 Запускаю сканер 200.zona.media\n"
        f"📅 Период: 14.10 – 14.12.2025 (5–7 мес назад)\n"
        f"🎯 Цель: 20 контактов родственников\n\n"
        f"Прогресс пойдёт сюда. Прервать: /zona_scan stop"
    )

    threading.Thread(target=scan_contacts.main, daemon=True).start()


@bot.message_handler(commands=['contacts'])
def cmd_contacts(message):
    import json
    chat_id = message.chat.id
    if chat_id != OWNER_ID:
        return
    DB_FILE = "contacts_db.json"
    if not os.path.exists(DB_FILE):
        bot.send_message(chat_id, "📭 База контактов пуста.\n\nПришли CSV файлы с данными zona.media — я их обработаю автоматически.")
        return
    with open(DB_FILE, encoding='utf-8') as f:
        found = json.load(f)
    if not found:
        bot.send_message(chat_id, "📭 База контактов пуста. Пришли CSV файлы с данными zona.media.")
        return
    bot.send_message(chat_id, f"📋 *База контактов: {len(found)}/20*\n\nОтправляю все записи...", parse_mode="Markdown")
    for i, rec in enumerate(found, 1):
        contacts = "\n".join(rec.get('vk', [])[:3] + rec.get('archive', [])[:3])
        msg = (f"[{i}/20] *{rec.get('name','—')}*\n"
               f"📍 {rec.get('region','—')}, {rec.get('location','—')}\n"
               f"⚔️ {rec.get('type','—')}"
               + (f" | {rec['rank']}" if rec.get('rank') else "") +
               f"\n💀 Погиб: {rec.get('death_date','—')}\n"
               f"🔗 Контакты:\n{contacts or '—'}")
        if rec.get('url'):
            msg += f"\n📄 {rec['url']}"
        bot.send_message(chat_id, msg, parse_mode="Markdown", disable_web_page_preview=True)


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
        voice_request_chats.add(chat_id)
        try:
            process_text(chat_id, text)
        finally:
            voice_request_chats.discard(chat_id)
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


@bot.message_handler(content_types=['document'])
def handle_document(message):
    """Принимает CSV/TXT файлы с данными zona.media, ищет погибших сен–ноя 2025 с контактами."""
    import csv
    import json
    from datetime import datetime
    from io import StringIO

    chat_id = message.chat.id
    if chat_id != OWNER_ID:
        return

    doc = message.document
    name = doc.file_name or ""
    bot.send_message(chat_id, f"📂 Получил файл: `{name}` ({doc.file_size // 1024} КБ)\n⏳ Обрабатываю...", parse_mode="Markdown")

    try:
        file_info = bot.get_file(doc.file_id)
        raw = bot.download_file(file_info.file_path)
        text = raw.decode('utf-8', errors='ignore')
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка загрузки: {e}")
        return

    DATE_FROM = datetime(2025, 9, 14)
    DATE_TO   = datetime(2025, 11, 14)
    DB_FILE   = "contacts_db.json"

    # Загружаем уже найденное
    found = []
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, encoding='utf-8') as f:
                found = json.load(f)
        except Exception:
            found = []
    found_urls = {r.get('url', r.get('name','')) for r in found}

    matched   = []
    total     = 0
    in_range  = 0

    # Пробуем разные разделители
    delimiter = '\t' if '\t' in text[:2000] else ','
    reader = csv.DictReader(StringIO(text), delimiter=delimiter)

    # Нормализуем заголовки (на случай разного регистра/пробелов)
    def norm(d, *keys):
        for k in keys:
            for dk in d.keys():
                if dk.strip().lower() == k.lower():
                    return d[dk].strip()
        return ""

    for row in reader:
        total += 1
        death_raw = norm(row, 'death_date','date_death','дата_гибели','дата гибели','Дата гибели','Died','died')
        if not death_raw:
            # Ищем дату в любом поле
            for v in row.values():
                m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', str(v))
                if m:
                    try:
                        dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                        if 2020 <= dt.year <= 2026:
                            death_raw = m.group(0)
                            break
                    except Exception:
                        pass

        if not death_raw:
            continue

        m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', death_raw)
        if not m:
            continue
        try:
            death_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except Exception:
            continue

        if not (DATE_FROM <= death_dt <= DATE_TO):
            continue
        in_range += 1

        # Контакты
        all_text = " ".join(str(v) for v in row.values())
        vk_links  = re.findall(r'https?://(?:vk\.com|vkontakte\.ru)/[^\s,;"\'<>]+', all_text)
        arc_links = re.findall(r'https?://(?:archive\.ph|web\.archive\.org)/[^\s,;"\'<>]+', all_text)
        if not vk_links and not arc_links:
            continue

        rec_id = norm(row,'url','URL','ссылка','link') or norm(row,'name','имя','ФИО','fio') or f"row_{total}"
        if rec_id in found_urls:
            continue

        rec = {
            "name":       norm(row,'name','имя','ФИО','fio','Имя','Name'),
            "region":     norm(row,'region','регион','Region'),
            "type":       norm(row,'type','род войск','тип','Type'),
            "location":   norm(row,'location','населённый пункт','город','Location'),
            "rank":       norm(row,'rank','звание','Rank'),
            "death_date": death_dt.strftime('%d.%m.%Y'),
            "vk":         vk_links[:5],
            "archive":    arc_links[:5],
            "url":        norm(row,'url','URL','ссылка','link') or "",
            "source_file": name,
        }
        matched.append(rec)
        found_urls.add(rec_id)

    # Добавляем новые к уже найденным
    new_count = 0
    for rec in matched:
        if len(found) >= 20:
            break
        found.append(rec)
        new_count += 1

    # Сохраняем
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(found, f, ensure_ascii=False, indent=2)

    # Отчёт
    bot.send_message(chat_id,
        f"📊 Файл `{name}` обработан:\n"
        f"• Всего строк: {total}\n"
        f"• Попали в диапазон 14.09–14.11.2025: {in_range}\n"
        f"• С контактами (VK/archive): {len(matched)}\n"
        f"• Добавлено в базу: {new_count}\n"
        f"• Итого в базе: {len(found)}/20",
        parse_mode="Markdown")

    # Отправляем каждую новую запись
    for i, rec in enumerate(matched[:new_count], 1):
        contacts = "\n".join(rec['vk'][:3] + rec['archive'][:3])
        msg = (f"✅ [{len(found)-new_count+i}/20] {rec['name']}\n"
               f"📍 {rec['region']}, {rec['location']}\n"
               f"⚔️ {rec['type']}{' | '+rec['rank'] if rec['rank'] else ''}\n"
               f"💀 Погиб: {rec['death_date']}\n"
               f"🔗 Контакты:\n{contacts}")
        if rec.get('url'):
            msg += f"\n📄 {rec['url']}"
        bot.send_message(chat_id, msg)

    if len(found) >= 20:
        bot.send_message(chat_id, "🎉 База контактов собрана! 20/20. Напиши /contacts чтобы посмотреть всё.")


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
    inline = types.InlineKeyboardMarkup(row_width=1)
    for sheet_id, info in monitored.items():
        mark = "✅" if info["enabled"] else "⬜️"
        inline.add(types.InlineKeyboardButton(
            f"{mark} {info['name'].title()}",
            callback_data=f"monitor_toggle:{sheet_id}",
        ))
    inline.add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="monitor_settings"))
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
    interval_hours = sheet_monitor.get_settings()["interval_hours"]
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
    for alert in alerts:
        try:
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
                "🔕 Выключить мониторинг",
                callback_data=f"alert_monitor_off:{sheet_id}",
            ))
            safe_send(OWNER_ID, alert["text"], inline)
        except Exception as e:
            print(f"Не удалось отправить алерт мониторинга: {e}")


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
    print("🚀 Ruslan Personal Helper с SMS для Тохи!")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Ошибка polling: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)
