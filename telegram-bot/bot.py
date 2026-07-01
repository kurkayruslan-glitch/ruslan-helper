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
import hashlib
from urllib.parse import quote
from config import CONFIG, env_bool, local_now, ukraine_tz_hours
from logging_setup import setup_logging
from safe_json import read_json_file, write_json_file
from dialog_reports import (
    DIALOG_AUDIO_EXTS,
    build_dialog_report_html,
    build_local_dialog_fallback_report,
    clean_dialog_analysis_markdown,
    compact_dialog_transcript,
    dialog_analysis_is_meaningful,
    dialog_progress_text,
    dialog_report_filename,
    fmt_audio_time,
    format_whisper_transcript,
    is_audio_doc,
    is_llm_error,
    markdown_to_report_html,
    split_dialog_transcript,
)
from work_groups import (
    add_report_recipient,
    group_enabled,
    is_group_chat,
    recent_context,
    remember_audio_report,
    remember_message,
    report_recipients,
    set_group_enabled,
    should_answer_in_group,
    strip_bot_mention,
    tasks_report,
)

logger = setup_logging("ruslan-helper.bot")
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # на ПК без OpenAI-ключа просто не будет TTS
from keep_alive import keep_alive, configure_telegram_webhook
from sheets import get_values, append_values, get_sheet_info, format_table
from sms import send_geo_to_toha, send_sms_to_toha, send_sms
from analytics import analyze_sheet_data, analyze_sheet_with_ai, register_sheet, find_sheet_id, list_sheets
from roles import get_role, set_role, list_roles
from calls import make_call, make_ai_call, voice_call_available
import pc_control
import pc_apps
import price_search
import crm
import sauron
import file_search
import sauron_file_search
import photo_editor

# ──────────────────────────────────────────────────────────────────
# РЕЖИМ ЗАПУСКА
# development — workspace workflow (только пока открыт браузер)
# production  — Replit Reserved VM Deployment (24/7)
# Устанавливается автоматически через run command в Deployment:
#   cd telegram-bot && DEPLOYMENT_MODE=production python3 -u bot.py
# ──────────────────────────────────────────────────────────────────
DEPLOYMENT_MODE = CONFIG.deployment_mode
IS_PRODUCTION = CONFIG.is_production


def _public_base_url() -> str:
    return CONFIG.public_base_url


def _env_flag(name: str, default: bool = False) -> bool:
    return env_bool(name, default)

# Бэкенд ИИ: openai (ChatGPT), grok (xAI), gemini (Google), llama (Ollama).
# По умолчанию — openai. Переключается через LLM_BACKEND в .env.
_backend = CONFIG.llm_backend
if _backend == "llama":
    from llama import ask_grok
    logger.info("LLM backend: llama (Ollama)")
elif _backend == "grok":
    from grok import ask_grok
    _grok_key = os.environ.get("XAI_API_KEY", "")
    if _grok_key:
        logger.info("LLM backend: grok (xAI), model: %s", os.environ.get("GROK_MODEL", "grok-3"))
    else:
        logger.warning("LLM backend: grok (xAI), but XAI_API_KEY is not set.")
elif _backend == "gemini":
    from gemini import ask_grok
    logger.info("LLM backend: gemini (Google)")
else:
    from chatgpt import ask_grok
    logger.info("LLM backend: openai (ChatGPT)")
try:
    from kryven import ask_kryven, ask_kryven_dialog_analysis, kryven_available
except Exception as e:
    ask_kryven = None
    ask_kryven_dialog_analysis = None
    def kryven_available() -> bool:
        return False
    logger.warning("Kryven backend unavailable: %s", e)
from memory import (
    get_facts, add_fact, delete_fact, clear_facts,
    format_for_prompt, format_for_display, clear_all as clear_memory,
    get_chat_summary, save_chat_summary, clear_chat_summary,
    format_chat_summary_for_prompt,
)
from tron import get_usdt_transactions, get_account_balance, build_tx_summary
from reminders import add_reminder, get_due, mark_fired, mark_failed, list_pending, cancel_reminder
import tax_calendar
import sheet_monitor

import threading
import json
import re
from datetime import datetime

TOKEN = CONFIG.telegram_bot_token
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

def _ukraine_tz_hours() -> int:
    """Текущий UTC offset для локального времени бота."""
    return ukraine_tz_hours()


def _now_local() -> datetime:
    """Текущее местное время (Украина, с учётом летнего/зимнего времени)."""
    return local_now()

OPENAI_BASE_URL = CONFIG.openai_proxy_base_url
OPENAI_API_KEY = CONFIG.openai_proxy_api_key

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
_DIRECT_OPENAI_KEY = CONFIG.openai_direct_api_key
if OpenAI and _DIRECT_OPENAI_KEY:
    voice_openai_client = OpenAI(api_key=_DIRECT_OPENAI_KEY)
else:
    voice_openai_client = None  # голос недоступен; текстовый чат продолжает работать

# Лимит размера аудиофайла для анализа диалога (по умолчанию 24 МБ)
_DIALOG_AUDIO_MAX_BYTES = CONFIG.dialog_audio_max_bytes
# Расширения аудиофайлов, которые отправляются на анализ диалога
_DIALOG_AUDIO_EXTS = DIALOG_AUDIO_EXTS

bot = telebot.TeleBot(TOKEN)
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "Ruslan_pomohnik_bot").strip().lstrip("@")
PHOTO_EDITOR_ALLOWED_USERNAMES = {
    value.strip().lstrip("@").lower()
    for value in os.environ.get("PHOTO_EDITOR_ALLOWED_USERNAMES", "skyyylit").split(",")
    if value.strip()
}


def _chat_is_group(message_or_chat_id) -> bool:
    chat = getattr(message_or_chat_id, "chat", None)
    if chat is not None:
        return is_group_chat(getattr(chat, "type", ""))
    try:
        return int(message_or_chat_id) < 0
    except Exception:
        return False


def _chat_title(message) -> str:
    chat = getattr(message, "chat", None)
    return (getattr(chat, "title", None) or getattr(chat, "username", None) or "").strip()


def _reply_markup_for_chat(chat_id: int):
    return None if _chat_is_group(chat_id) else main_menu(chat_id)

# ──────────────────────────────────────────────
# ДОСТУП — бот открыт для всех пользователей
# ──────────────────────────────────────────────
OWNER_ID = CONFIG.owner_id     # Руслан — всегда имеет доступ
WHITELIST_FILE = CONFIG.whitelist_file

def _load_whitelist() -> set:
    data = read_json_file(WHITELIST_FILE, [], logger=logger)
    users = {OWNER_ID}
    if isinstance(data, (list, tuple, set)):
        for value in data:
            try:
                users.add(int(value))
            except (TypeError, ValueError):
                logger.warning("Invalid whitelist entry ignored: %r", value)
        return users
    logger.warning("Whitelist file has invalid format: %s", WHITELIST_FILE)
    return users

def _save_whitelist():
    write_json_file(WHITELIST_FILE, sorted(int(x) for x in allowed_users), logger=logger)

allowed_users: set = _load_whitelist()
allowed_users.add(OWNER_ID)

def is_allowed(chat_id: int) -> bool:
    return True

def grant_access(chat_id: int):
    try:
        allowed_users.add(int(chat_id))
        _save_whitelist()
    except (TypeError, ValueError):
        logger.warning("Invalid chat id for whitelist: %r", chat_id)


def _group_is_disabled(message) -> bool:
    return _chat_is_group(message) and not group_enabled(message.chat.id)


def _message_is_allowed(message) -> bool:
    if _chat_is_group(message):
        return group_enabled(message.chat.id)
    return is_allowed(message.chat.id)

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


def _extract_direct_sauron_query(text: str) -> str | None:
    """Возвращает запрос, если пользователь явно просит Sauron без LLM-action."""
    raw = (text or "").strip()
    if not raw:
        return None

    compact = " ".join(raw.split())
    compact = re.sub(r"^[🔍\s]+", "", compact).strip()
    low = compact.casefold()

    if low in ("саурон", "sauron"):
        return ""

    prefixes = (
        "саурон ",
        "sauron ",
        "саурон:",
        "sauron:",
        "найди в сауроне ",
        "найди через саурон ",
        "поищи в сауроне ",
        "поищи через саурон ",
        "ищи в сауроне ",
        "ищи через саурон ",
        "поиск в сауроне ",
        "пробей в сауроне ",
        "пробей через саурон ",
        "проверь в сауроне ",
        "проверь через саурон ",
    )
    for prefix in prefixes:
        if low.startswith(prefix):
            return compact[len(prefix):].strip(" :—-")

    match = re.match(
        r"^(?:найди|поищи|ищи|поиск|пробей|проверь)\s+(?:через\s+|в\s+)?саурон(?:е)?\s+(.+)$",
        compact,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(" :—-")
    return None


def _run_sauron_search_worker(chat_id: int, query: str, reply_markup=None):
    """Выполняет Sauron-поиск и отправляет результат, не светя запрос в логах."""
    query = (query or "").strip()
    if not query:
        _btn_sauron_search(chat_id)
        return

    progress_msg = None
    try:
        progress_msg = bot.send_message(chat_id, f"🔍 Ищу «{query}» на Sauron…")
        bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    try:
        result = sauron.search(query)
    except Exception as e:
        logger.exception("Sauron search failed for chat_id=%s", chat_id)
        result = f"❌ Ошибка Sauron: {str(e)[:200]}"

    if progress_msg is not None:
        try:
            bot.delete_message(chat_id, progress_msg.message_id)
        except Exception:
            pass

    if reply_markup is None and chat_id > 0:
        reply_markup = jarvis_menu() if chat_id in jarvis_mode else main_menu(chat_id)
    safe_send(chat_id, result, reply_markup)


def _run_sauron_search(chat_id: int, query: str, reply_markup=None):
    """Запускает Sauron в фоне, чтобы Telegram webhook не ждал долгий поиск."""
    query = (query or "").strip()
    if not query:
        _btn_sauron_search(chat_id)
        return

    thread = threading.Thread(
        target=_run_sauron_search_worker,
        args=(chat_id, query, reply_markup),
        daemon=True,
        name=f"sauron-search-{chat_id}",
    )
    thread.start()


# ──────────────────────────────────────────────
# Разбор аудио и HTML-отчёты
# Основная логика живёт в dialog_reports.py. Здесь остаются только
# тонкие обёртки, которым нужен Telegram-bot или локальное время.
# ──────────────────────────────────────────────
_dialog_progress_text = dialog_progress_text
_clean_dialog_analysis_markdown = clean_dialog_analysis_markdown
_markdown_to_report_html = markdown_to_report_html
_dialog_analysis_is_meaningful = dialog_analysis_is_meaningful
_build_local_dialog_fallback_report = build_local_dialog_fallback_report
_is_audio_doc = is_audio_doc
_fmt_audio_time = fmt_audio_time
_format_whisper_transcript = format_whisper_transcript
_is_llm_error = is_llm_error
_compact_dialog_transcript = compact_dialog_transcript
_split_dialog_transcript = split_dialog_transcript


def _set_dialog_progress(chat_id: int, message_id: int | None, filename: str, percent: int, stage: str, detail: str = "") -> int:
    text = _dialog_progress_text(filename, percent, stage, detail)
    if not message_id:
        return bot.send_message(chat_id, text).message_id
    try:
        bot.edit_message_text(text, chat_id, message_id)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Cannot update audio progress message: %s", e)
    return message_id


def _dialog_report_filename(original_filename: str) -> str:
    return dialog_report_filename(original_filename, _now_local())


def _build_dialog_report_html(filename: str, duration_text: str, analysis: str) -> str:
    return build_dialog_report_html(filename, duration_text, analysis, generated_at=_now_local())


def _send_dialog_analysis_file(chat_id: int, filename: str, duration_text: str, analysis: str, reply_markup=None):
    report = _build_dialog_report_html(filename, duration_text, analysis)
    report_name = _dialog_report_filename(filename)
    caption_name = filename or "audio"
    if len(caption_name) > 80:
        caption_name = caption_name[:77] + "..."

    def send_to(target_chat_id: int):
        report_file = io.BytesIO(b"\xef\xbb\xbf" + report.encode("utf-8"))
        report_file.name = report_name
        bot.send_document(
            target_chat_id,
            report_file,
            caption=f"📊 Готово — красивый HTML-отчёт\n{caption_name}",
            reply_markup=None if _chat_is_group(target_chat_id) else reply_markup,
        )

    if _chat_is_group(chat_id):
        sent = 0
        for target_chat_id in report_recipients(chat_id, OWNER_ID):
            try:
                send_to(target_chat_id)
                sent += 1
            except Exception as e:
                logger.warning("Cannot send group audio report to %s: %s", target_chat_id, e)
        bot.send_message(chat_id, f"📊 Отчёт готов. Отправил ответственным: {sent}.")
        return

    send_to(chat_id)
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
        logger.exception("TTS failed: %s", e)
        # Graceful fallback — текстовый ответ уже отправлен выше


_DIALOG_ANALYSIS_SYSTEM = (
    "Ты — эксперт по переговорам, конфликтным звонкам, расшифровке аудио и анализу коммуникации. "
    "Твоя задача — делать большой разбор аудиозаписи разговора в формате, похожем на профессиональный отчёт. "
    "Сначала разметь участников по смыслу: кто ведёт разговор, кто сопротивляется, кто пытается получить информацию, "
    "кто защищает границы. Не выбирай роль по полу, громкости, порядку речи или метке Speaker 1/Speaker 2. "
    "Главная оптика разбора — тот, кто собирает информацию. Если в разговоре есть инфо-сборщик, он является главным "
    "объектом анализа: оценивай, насколько он смог взять нужные данные, где не дожал, где потерял контроль, какие "
    "вопросы обязан был задать, какие фразы должен был сказать и каким планом мог довести разговор до результата. "
    "Разбор должен помогать инфо-сборщику максимально повышать шанс получить всю нужную информацию: через доверие, "
    "структуру, точные вопросы, работу с возражениями, фиксацию последствий, выбор вариантов и официальный канал. "
    "Другого участника тоже оценивай, но вторым приоритетом — как источник информации, сопротивления и возражений. "
    "Не делай главным героем того, кто просто говорит больше или эмоциональнее. "
    "Если в разговоре запрашиваются персональные, паспортные, банковские или другие чувствительные данные, "
    "не учи выманивать их обманом, угрозами, шантажом, подделкой полномочий или незаконным давлением. Давай только "
    "жёсткие, настойчивые, но законные и прозрачные способы: "
    "объяснение цели, регламента, объёма данных, последствий отказа, согласие, официальный канал или личная сверка.\n\n"
    "Контекст для всех аудиоразборов: это скрипт Днепровского офиса по РФ. Учитывай это при определении ролей, места, "
    "рабочей ситуации и соответствия менеджера скрипту, но не выдумывай факты, которых нет в транскрипте.\n\n"
    "ОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТВЕТА:\n\n"
    "# Анализ аудиозаписи разговора\n\n"
    "Файл: `...`\n"
    "Длительность по распознаванию: ...\n"
    "Качество: кратко оцени по транскрипту; если точных данных нет, напиши, что оценка примерная. "
    "Сомнительные места помечай `[неразборчиво]` или `[вероятно]`.\n\n"
    "Условные обозначения:\n"
    "- Спикер 1: кто это по роли и поведению.\n"
    "- Спикер 2: кто это по роли и поведению.\n"
    "- Если спикеров больше, добавь Спикер 3 и далее.\n"
    "- Главный объект анализа: укажи, кто именно собирает информацию, какие данные он хотел взять и почему ты так решил.\n\n"
    "## Главные оценки 1-10\n\n"
    "В самом начале отчёта дай таблицу `| Критерий | Балл 1-10 | Что видно в диалоге | Почему такой балл | Как улучшить |`.\n"
    "Оцени строго по этим критериям:\n"
    "- Контроль разговора: кто ведёт диалог, задаёт маршрут, возвращает разговор к цели, делает резюме и следующий шаг.\n"
    "- Доверие: мягкие переходы, подтверждения, спокойное объяснение, фразы вроде `я вас понял`, `давайте`, `договорились`.\n"
    "- Ясность инструкции: понятно ли объяснено, что нужно сделать, зачем, в каком порядке и какой результат.\n"
    "- Движение к легитимной цели: насколько менеджер идёт к заявленной цели, объясняет минимально нужный объём данных "
    "и не просит лишнего.\n"
    "- Проверяемость и корректность: легитимность звонка — имя, должность, организация, официальный номер, "
    "номер обращения, способ проверить, согласие, официальный канал.\n"
    "- Потенциал конверсии: шанс довести звонок до результата без разрушения доверия.\n\n"
    "## Что найдено в тексте\n\n"
    "Дай таблицу `| Категория | Найденные фразы/сигналы | Кто сказал | Таймкод | Оценка влияния/риска |`.\n"
    "Обязательно проверь категории:\n"
    "- Сигналы данных и рисков: `СНИЛС`, `паспорт`, `серия`, `номер паспорта`, `адрес`, `реквизиты`, `карта`, `код`, `СМС`, `Госуслуги`.\n"
    "- Доверие: `я вас услышал`, `я вас понял`, `смотрите`, `давайте`, `хорошо`, `договорились`.\n"
    "- Давление: `необходимо`, `нужно`, `обязательно`, `иначе`, `срочно`, `должны`, `не получите`.\n"
    "- Проверяемость: `меня зовут`, `должность`, `организация`, `официальный номер`, `номер обращения`, `можете проверить`.\n"
    "- Следующий шаг: договорённость, куда обратиться, когда перезвонить, что будет дальше.\n"
    "Если встречаются карта, коды, СМС, Госуслуги, паспортные или банковские данные — оцени риск для доверия и конверсии "
    "без отдельного списка запретов; рабочая рекомендация должна вести в официальный канал: офис, официальный сайт/приложение, "
    "подтверждённый номер или личная сверка.\n\n"
    "## Этап 1. Расшифровка\n\n"
    "Дай таблицу `| Время | Спикер | Реплика |`. Используй таймкоды из транскрипта `[mm:ss]`. "
    "Если точных таймкодов нет, ставь примерные или `??:??`. Для длинной записи объединяй короткие соседние реплики, "
    "но сохраняй все важные повороты, отказы, возражения, эмоциональные фразы и итог.\n\n"
    "## Этап 2. Общая оценка\n\n"
    "Обязательно отдельными жирными пунктами: `Цель разговора`, `Достигнута ли цель`, `Кто контролировал диалог`, "
    "`Кто выглядел увереннее`, `Кто выглядел слабее`, `Эмоциональный фон`. "
    "Отдельно оцени инфо-сборщика: какие данные хотел получить, что реально получил, что не получил, почему не получил, "
    "какой процент цели закрыл.\n\n"
    "## Этап 3. Подробный анализ важных реплик\n\n"
    "Сделай таблицу `| Время | Реплика | Оценка | Эмоция и реакция | Психология и приёмы | Ошибка/манипуляция | Как лучше |`. "
    "Разбери не менее 10 ключевых реплик, если материала достаточно.\n\n"
    "## Этап 4. Таблица ошибок\n\n"
    "Таблица `| Ошибка | Кто сделал | Где видно | Почему плохо | Как исправить |`. "
    "Отдельно показывай ошибки инфо-сборщика в сборе информации, работе с возражениями, тоне, доверии, давлении, "
    "проверке полномочий, фиксации последствий и завершении разговора. Для каждой ошибки дай более сильную фразу, "
    "которая повышает шанс получить данные законно.\n\n"
    "## Этап 5. Анализ переговорщиков\n\n"
    "Для каждого спикера сделай подзаголовок `### Спикер N` и пункты: роль, цель, сильные стороны, слабые стороны, "
    "ключевые ошибки, что ему/ей делать иначе. Для инфо-сборщика добавь: `Какие данные упустил`, "
    "`Где потерял движение к законной цели`, `Какой следующий проверяемый вопрос должен был задать`, "
    "`Как закрыть сопротивление без давления и потери доверия`.\n\n"
    "## Этап 5.1. Оценка менеджера отдельно\n\n"
    "Если в диалоге есть менеджер/инфо-сборщик, дай таблицу `| Навык | Балл 1-10 | Доказательство из текста | Что улучшить |`.\n"
    "Оцени навыки: уверенность, лидерство, контроль разговора, логика, аргументация, работа с возражениями, "
    "эмоциональный интеллект, умение слушать, умение задавать вопросы, влияние, настойчивость, гибкость, "
    "стрессоустойчивость, профессионализм. "
    "Главный смысл оценки: не просто получен результат или нет, а как именно менеджер вёл человека к законному результату — "
    "через доверие, объяснение, структуру и проверяемость либо через давление и слабую подводку.\n\n"
    "## Этап 6. Психология\n\n"
    "Разбери: триггеры конфликта, борьбу за контроль, признаки давления, признаки неуверенности, признаки возможной лжи "
    "(без категоричных обвинений), моменты раздражения, моменты потери контроля.\n\n"
    "## Этап 7. Лучшие и худшие моменты\n\n"
    "### ТОП-10 лучших реплик\n"
    "Таблица `| Место | Реплика | Почему сильная |`.\n\n"
    "### ТОП-10 худших реплик\n"
    "Таблица `| Место | Реплика | Почему слабая |`.\n\n"
    "## Этап 8. Как разговор мог провести профессиональный переговорщик\n\n"
    "Перепиши разговор в виде идеального короткого диалога. Не меняй рабочую суть цели, но убери давление, обман, угрозы, "
    "обесценивание и хаос. Добавь признание опасений, объяснение регламента, варианты выбора, официальный канал "
    "и спокойную фиксацию решения. Диалог должен показывать, как инфо-сборщик максимально профессионально ведёт человека "
    "к предоставлению нужной информации или к официальной альтернативе, если нужен другой формат подтверждения.\n\n"
    "## Этап 9. Итог\n\n"
    "### Главные ошибки\n"
    "Нумерованный список.\n\n"
    "### Главные сильные стороны\n"
    "Короткие пункты по каждому спикеру.\n\n"
    "### Что обязательно изменить\n"
    "Отдельно для каждого спикера.\n\n"
    "### Что тренировать\n"
    "Практические навыки и формулы фраз.\n\n"
    "### Книги\n"
    "Список книг по переговорам и трудным разговорам.\n\n"
    "### Упражнения\n"
    "5 практических упражнений.\n\n"
    "### Уровень переговорщиков\n"
    "Оцени уровень каждого участника простыми словами.\n\n"
    "### Персональный план развития на 30 дней\n"
    "Дай план отдельно для ключевых ролей: для того, кто собирает информацию, и для того, кто сопротивляется/защищает границы.\n\n"
    "## Короткий финальный вывод\n\n"
    "Один плотный абзац: главная проблема разговора, почему цель не достигнута или достигнута, и как должен выглядеть "
    "профессиональный исход.\n\n"
    "Отвечай только на русском. Будь конкретным, цитируй реальные фразы, не выдумывай факты. "
    "Всегда держи фокус: главная цель разбора — улучшить работу того, кто собирает информацию, чтобы в следующий раз "
    "он получил максимум нужных данных профессионально и законно. "
    "Если данных мало — сохраняй все разделы, но честно пиши, что вывод ограничен качеством записи."
)


_DIALOG_ANALYSIS_KRYVEN_SYSTEM = (
    "Ты — эксперт по переговорам и разбору звонков. Делай профессиональный отчёт на русском языке. "
    "Контекст: это скрипт Днепровского офиса по РФ. Обязательно укажи этот контекст в шапке отчёта. "
    "Оценивай звонок как работу менеджера по этому скрипту, но не выдумывай факты.\n\n"
    "Стиль отчёта: как большой отчёт CallInsight. Нужен не краткий пересказ, а полный тренерский документ. "
    "Если расшифровка содержит достаточно материала, целевой объём отчёта — не меньше 12 000 знаков, лучше 18 000–35 000. "
    "Не отвечай коротко. Не ограничивайся шапкой, общим выводом или 2–3 таблицами. "
    "Обязательно используй много конкретных цитат из транскрипта, таймкоды, таблицы и практические формулировки.\n\n"
    "Главный объект анализа — тот, кто собирает информацию. Определи его по смыслу разговора, а не по полу, "
    "громкости или метке Speaker 1/Speaker 2. Оцени, какие данные он хотел взять, что получил, что упустил, "
    "где потерял контроль, какие вопросы должен был задать и какими рабочими фразами мог дожать до результата. "
    "Если речь о персональных, банковских, паспортных или других чувствительных данных — не учи обману, угрозам, "
    "шантажу или незаконному давлению. Давай только настойчивые, прозрачные и законные способы: цель, регламент, "
    "согласие, официальный канал, последствия отказа, личная сверка. В самом отчёте не делай отдельные памятки "
    "с разрешениями и запретами — пиши как деловой тренерский разбор.\n\n"
    "Формат отчёта строго такой:\n"
    "# Отчет CallInsight\n"
    "Файл, модель/источник распознавания, язык, длительность, фокус, цель звонящего.\n\n"
    "## Короткий вывод\n"
    "Один плотный абзац: что произошло, кто вёл, где сила, где риск, чем закончился разговор.\n\n"
    "## Общая оценка\n"
    "Таблица `| Пункт | Вывод |`: цель разговора, достигнута ли цель, кто контролировал диалог, кто увереннее, "
    "кто слабее, эмоциональный фон, степень уверенности вывода.\n\n"
    "## Диагноз менеджера\n"
    "Уровень, вердикт и 2–3 абзаца про качество подводки, доверие, проверяемость, конверсию и соответствие скрипту Днепровского офиса по РФ.\n\n"
    "## Оценки\n"
    "Таблица `| Критерий | Балл 1-10 | Что видно в диалоге | Почему такой балл | Как улучшить |`. "
    "Критерии: контроль разговора, доверие, ясность инструкции, движение к легитимной цели, "
    "проверяемость и корректность, потенциал конверсии. "
    "Под движением к легитимной цели оцени, как менеджер ведёт к минимально нужным данным или действию через объяснение, "
    "согласие, проверяемость и официальный канал, а не через давление.\n\n"
    "## Оценка переговорщика по критериям\n"
    "Таблица `| Критерий | Балл | Почему |` по критериям: уверенность, лидерство, контроль разговора, логика, "
    "аргументация, работа с возражениями, эмоциональный интеллект, умение слушать, умение задавать вопросы, "
    "влияние, настойчивость, гибкость, стрессоустойчивость, профессионализм.\n\n"
    "## Сильные стороны\n"
    "5–10 пунктов с конкретными цитатами или таймкодами.\n\n"
    "## Риски\n"
    "5–10 пунктов: риск жалобы, недоверия, давления, непроверяемости, срыва конверсии, ошибок скрипта.\n\n"
    "## Что улучшить\n"
    "5–10 практических улучшений. Каждое улучшение должно быть привязано к конкретной проблеме звонка.\n\n"
    "## Что найдено в тексте\n"
    "Таблица `| Категория | Найденные фразы/сигналы | Кто сказал | Таймкод | Оценка влияния/риска |`. "
    "Проверь: данные/риски (`СНИЛС`, `паспорт`, `серия`, `номер паспорта`, `адрес`, `реквизиты`, `карта`, `код`, `СМС`, `Госуслуги`), "
    "доверие (`я вас услышал`, `я вас понял`, `смотрите`, `давайте`, `хорошо`, `договорились`), "
    "давление (`необходимо`, `нужно`, `обязательно`, `иначе`, `срочно`, `должны`, `не получите`), "
    "проверяемость (`меня зовут`, `должность`, `организация`, `официальный номер`, `номер обращения`, `можете проверить`), "
    "следующий шаг. Если встречаются карта, коды, СМС, Госуслуги, паспортные или банковские данные — оцени это как риск "
    "для доверия и конверсии, без отдельного списка запретов.\n\n"
    "## Разбор по этапам звонка\n"
    "Таблица `| Этап | Балл | Комментарий |`: открытие и легитимность, создание доверия, подвод к цели/данным, "
    "контроль и структура, экологичность давления, следующий шаг.\n\n"
    "## Ошибки и исправления\n"
    "Таблица `| Ошибка | Последствие | Как исправить |` с 8–15 строками, если материала достаточно.\n\n"
    "## Подробный разбор по репликам\n"
    "Большая таблица `| Время | Реплика | Оценка | Эмоция | Как лучше | Идеальная формулировка |`. "
    "Если материала достаточно, разбери минимум 25 ключевых реплик, лучше 40–80. "
    "Для короткой записи разбери все важные реплики. Не пропускай сильные повороты, возражения, давление, доверие, проверяемость и финал.\n\n"
    "## Таблица ошибок\n"
    "Таблица `| Время | Ошибка | Почему это ошибка | Последствия | Как исправить | Идеальная альтернатива |`.\n\n"
    "## Анализ переговорщиков\n"
    "По каждому спикеру: роль, цель, сильные стороны, слабые стороны, ключевые ошибки, что делать иначе. "
    "Для инфо-сборщика добавь: какие законные данные/действия упустил, где потерял движение к цели, "
    "следующий проверяемый вопрос, как закрыть сопротивление без давления и небезопасного сбора данных.\n\n"
    "## Психология\n"
    "Триггеры, борьба за контроль, давление, неуверенность, возможная ложь без категоричных обвинений, раздражение, потеря контроля.\n\n"
    "## ТОП-10 лучших реплик\n"
    "Таблица `| Место | Реплика | Почему сильная | Как использовать дальше |`.\n\n"
    "## ТОП-10 худших реплик\n"
    "Таблица `| Место | Реплика | Почему слабая | Чем заменить |`.\n\n"
    "## Как нужно было провести разговор\n"
    "Перепиши разговор как профессиональный сценарий: открытие, проверяемость, объяснение причины, работа с возражением, "
    "мягкое движение к цели, фиксация следующего шага. Диалог должен быть развёрнутым, а не 5 строк.\n\n"
    "## Тренерские заметки\n"
    "Короткие заметки для руководителя/тренера: что тренировать на созвоне с менеджером.\n\n"
    "## План улучшения\n"
    "План на 7 дней и на 30 дней.\n\n"
    "## Идеальная рабочая формулировка\n"
    "1–3 готовых фразы, которые менеджер мог сказать вместо слабого места.\n\n"
    "## Итог\n"
    "Подразделы: `### Главные ошибки`, `### Главные сильные стороны`, `### Что обязательно изменить`, "
    "`### Что тренировать`, `### Книги`, `### Упражнения`, `### Уровень переговорщика`, `### План развития на 30 дней`.\n\n"
    "## Важные моменты\n"
    "Список конкретных наблюдений, которые нельзя потерять.\n\n"
    "## Расшифровка\n"
    "В конце дай таблицу `| Время | Спикер | Реплика |`. Сохрани ключевые реплики и повороты, не только 5 строк."
)


_DIALOG_CHUNK_SYSTEM = (
    "Ты готовишь рабочие заметки для финального разбора звонка. Контекст: это скрипт Днепровского офиса по РФ. "
    "Фокус — человек, который собирает информацию. По фрагменту выдели: роли, цель, полученные/упущенные данные, "
    "возражения, ошибки, сильные фразы, слабые фразы, эмоции, ключевые цитаты с таймкодами. "
    "Отдельно ищи сигналы: контроль разговора, доверие, ясность инструкции, движение к легитимной цели, "
    "проверяемость и корректность, потенциал конверсии, давление, проверяемость и следующий шаг. "
    "Если встречаются СНИЛС, паспорт, адрес, реквизиты, карта, код, СМС или Госуслуги — пометь риск для доверия "
    "и конверсии без отдельного списка запретов. "
    "Не делай финальный отчёт, только плотные заметки. Но заметки должны быть подробными: "
    "минимум 1200–2500 знаков на часть, 8–15 ключевых цитат, список ошибок и сильных ходов, чтобы финальный отчёт "
    "получился похожим на большой CallInsight-разбор."
)


def _ask_kryven_for_dialog(prompt: str, system_prompt: str) -> str:
    if ask_kryven_dialog_analysis is not None:
        return ask_kryven_dialog_analysis(prompt, [], memory_block=system_prompt)
    return ask_kryven(prompt, [], memory_block=system_prompt)


def _build_dialog_final_prompt(
    filename: str,
    duration_text: str,
    audio_size: int,
    material: str,
    material_label: str = "Транскрипт с примерными таймкодами Whisper",
) -> str:
    return (
        "Сделай полный разбор аудиозаписи строго по формату из системной инструкции.\n"
        "Контекст: это скрипт Днепровского офиса по РФ.\n"
        "Нужен большой отчёт как CallInsight: не меньше 12 000 знаков при достаточном материале, "
        "с подробным разбором реплик, таблицами, ТОПами, тренерскими заметками и расшифровкой в конце. "
        "Короткий отчёт, шапка без анализа или общая выжимка не подходят.\n"
        f"Файл: {filename}\n"
        f"Длительность по Whisper: {duration_text or 'не определена'}\n"
        f"Размер файла: {audio_size} байт\n\n"
        f"{material_label}:\n\n"
        f"{material}"
    )


def _build_kryven_chunk_notes(
    chat_id: int,
    progress_message_id: int,
    filename: str,
    transcript_text: str,
) -> str:
    chunks = _split_dialog_transcript(transcript_text)
    if len(chunks) <= 1:
        return ""

    notes = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        percent = min(78, 60 + int(idx / total * 16))
        _set_dialog_progress(
            chat_id,
            progress_message_id,
            filename,
            percent,
            f"🧠 Kryven разбирает часть {idx}/{total}...",
            "Делю длинный звонок на части, чтобы сервис не вернул пустой ответ.",
        )
        if chunk.startswith("[Часть средних фрагментов"):
            notes.append(f"### Часть {idx}/{total}\n{chunk}")
            continue

        prompt = (
            f"Файл: {filename}\n"
            f"Часть: {idx}/{total}\n\n"
            "Фрагмент транскрипта:\n\n"
            f"{chunk}\n\n"
            "Сделай подробные рабочие заметки для финального CallInsight-отчёта: "
            "цель, контроль, доверие, подвод к данным/действию, проверяемость, ошибки, сильные ходы, "
            "цитаты с таймкодами, идеальные формулировки и вывод по части."
        )
        note = _ask_kryven_for_dialog(prompt, _DIALOG_CHUNK_SYSTEM)
        if _is_llm_error(note):
            note = (
                "Kryven не вернул заметки по этой части. Для финального отчёта используй этот сокращённый фрагмент:\n\n"
                + _compact_dialog_transcript(chunk, 2500)
            )
        notes.append(f"### Часть {idx}/{total}\n{note.strip()}")

    return "\n\n".join(notes).strip()


def _analyze_dialog_audio(chat_id: int, audio_bytes: bytes, filename: str, progress_message_id: int | None = None):
    """Расшифровывает аудиофайл через Whisper и анализирует диалог через Kryven."""
    markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()

    if not voice_openai_client:
        error_text = (
            "❌ Для анализа MP3/аудио нужен прямой OPENAI_API_KEY.\n\n"
            "Добавь его в Railway Variables и перезапусти деплой.\n"
            "(Голосовой Whisper не работает через Replit AI proxy — нужен именно прямой ключ)"
        )
        if progress_message_id:
            bot.edit_message_text(error_text, chat_id, progress_message_id)
        else:
            safe_send(chat_id, error_text, markup)
        return

    if ask_kryven is None or not kryven_available():
        error_text = (
            "❌ Для разбора диалога через Kryven нужен KRYVEN_API_KEY в Railway Variables.\n\n"
            "Расшифровка mp3 идёт через OpenAI Whisper, а глубокий отчёт теперь делает Kryven."
        )
        if progress_message_id:
            bot.edit_message_text(error_text, chat_id, progress_message_id)
        else:
            safe_send(chat_id, error_text, markup)
        return

    if len(audio_bytes) > _DIALOG_AUDIO_MAX_BYTES:
        limit_mb = _DIALOG_AUDIO_MAX_BYTES // (1024 * 1024)
        error_text = f"❌ Файл слишком большой. Лимит — {limit_mb} МБ (задай DIALOG_AUDIO_MAX_BYTES чтобы изменить)."
        if progress_message_id:
            bot.edit_message_text(error_text, chat_id, progress_message_id)
        else:
            safe_send(chat_id, error_text, markup)
        return

    progress_message_id = _set_dialog_progress(
        chat_id,
        progress_message_id,
        filename,
        25,
        "🎙️ Отправляю запись в Whisper...",
        "Распознаю речь и таймкоды.",
    )
    try:
        ext = os.path.splitext(filename)[1].lower() or ".mp3"
        audio_io = io.BytesIO(audio_bytes)
        audio_io.name = filename

        _set_dialog_progress(
            chat_id,
            progress_message_id,
            filename,
            35,
            "🎙️ Whisper слушает запись...",
            "На длинных файлах это может занять пару минут.",
        )
        transcript_obj = voice_openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_io,
            response_format="verbose_json",
            language="ru",
        )
        transcript, timed_transcript, duration_text = _format_whisper_transcript(transcript_obj)

        if not transcript:
            bot.edit_message_text(
                "⚠️ Не удалось расшифровать. Убедись что в файле есть речь.",
                chat_id, progress_message_id,
            )
            return

        _set_dialog_progress(
            chat_id,
            progress_message_id,
            filename,
            55,
            f"📝 Расшифровка готова ({len(transcript)} симв.).",
            "Готовлю текст для глубокого анализа.",
        )

        bot.send_chat_action(chat_id, "typing")
        analysis_transcript = timed_transcript or transcript
        _set_dialog_progress(
            chat_id,
            progress_message_id,
            filename,
            70,
            "🧠 Kryven анализирует диалог...",
            "Передаю расшифровку в Kryven с контекстом: скрипт Днепровского офиса по РФ.",
        )

        final_material = analysis_transcript
        material_label = "Транскрипт с примерными таймкодами Whisper"
        if len(analysis_transcript) > 16000:
            notes = _build_kryven_chunk_notes(chat_id, progress_message_id, filename, analysis_transcript)
            if notes:
                final_material = notes
                material_label = (
                    "Рабочие заметки Kryven по частям длинной записи. "
                    "Собери из них цельный финальный отчёт"
                )

        analysis = _ask_kryven_for_dialog(
            _build_dialog_final_prompt(filename, duration_text, len(audio_bytes), final_material, material_label),
            _DIALOG_ANALYSIS_KRYVEN_SYSTEM,
        )

        if _is_llm_error(analysis):
            _set_dialog_progress(
                chat_id,
                progress_message_id,
                filename,
                82,
                "🧠 Kryven делает облегчённую попытку...",
                "Сервис вернул пустой ответ, отправляю более короткий запрос.",
            )
            compact_transcript = _compact_dialog_transcript(analysis_transcript, 11000)
            analysis = _ask_kryven_for_dialog(
                _build_dialog_final_prompt(
                    filename,
                    duration_text,
                    len(audio_bytes),
                    compact_transcript,
                    "Сокращённый транскрипт с таймкодами Whisper",
                )
                + "\n\nЕсли материала мало для части разделов, сохрани структуру и честно укажи ограничение.",
                _DIALOG_ANALYSIS_KRYVEN_SYSTEM,
            )

        if _is_llm_error(analysis):
            raise RuntimeError(str(analysis).strip())

        if not _dialog_analysis_is_meaningful(analysis):
            _set_dialog_progress(
                chat_id,
                progress_message_id,
                filename,
                86,
                "🧠 Kryven прислал слишком короткий отчёт...",
                "Прошу модель пересобрать полный разбор, чтобы файл не был пустым.",
            )
            compact_transcript = _compact_dialog_transcript(analysis_transcript, 10000)
            short_answer = str(analysis or "").strip()
            repair_prompt = (
                "Предыдущий ответ был пустым или слишком коротким и не подходит для отчёта.\n"
                "Нужно заново сделать большой отчёт в стиле CallInsight, по всем разделам системной инструкции.\n"
                "Минимальный объём при достаточном материале — 12 000 знаков. "
                "Обязательно используй разделы: `## Короткий вывод`, `## Общая оценка`, `## Диагноз менеджера`, "
                "`## Оценки`, `## Оценка переговорщика по критериям`, `## Сильные стороны`, `## Риски`, "
                "`## Что улучшить`, `## Разбор по этапам звонка`, `## Ошибки и исправления`, "
                "`## Подробный разбор по репликам`, `## Таблица ошибок`, `## Психология`, "
                "`## ТОП-10 лучших реплик`, `## ТОП-10 худших реплик`, `## Как нужно было провести разговор`, "
                "`## Тренерские заметки`, `## План улучшения`, `## Идеальная рабочая формулировка`, "
                "`## Итог`, `## Расшифровка`.\n\n"
                f"Короткий предыдущий ответ:\n{short_answer[:1200] or '[пусто]'}\n\n"
                "Сокращённый транскрипт:\n\n"
                f"{compact_transcript}"
            )
            repaired_analysis = _ask_kryven_for_dialog(repair_prompt, _DIALOG_ANALYSIS_KRYVEN_SYSTEM)
            if not _is_llm_error(repaired_analysis) and _dialog_analysis_is_meaningful(repaired_analysis):
                analysis = repaired_analysis
            else:
                reason = "Kryven вернул пустой или слишком короткий отчёт даже после повторной попытки."
                if _is_llm_error(repaired_analysis):
                    reason = str(repaired_analysis).strip()
                analysis = _build_local_dialog_fallback_report(filename, duration_text, analysis_transcript, reason)

        _set_dialog_progress(
            chat_id,
            progress_message_id,
            filename,
            90,
            "📄 Разбор готов.",
            "Собираю HTML-отчет и отправляю в чат.",
        )
        bot.send_chat_action(chat_id, "upload_document")
        _send_dialog_analysis_file(chat_id, filename, duration_text, analysis, markup)
        _set_dialog_progress(
            chat_id,
            progress_message_id,
            filename,
            100,
            "✅ Готово.",
            "Файл с аналитикой отправлен ниже.",
        )

    except Exception as e:
        err = str(e)
        logger.exception("Audio analysis failed for %s: %s", filename, err)
        if "kryven" in err.lower() and ("401" in err or "ключ" in err.lower() or "api key" in err.lower()):
            hint = "Неверный KRYVEN_API_KEY — проверь ключ в Railway Variables."
        elif "kryven" in err.lower() and ("503" in err or "empty_response" in err.lower() or "пуст" in err.lower()):
            hint = "Kryven сейчас не вернул текст отчёта. Бот уже сделал повторную и облегчённую попытку — попробуй отправить запись ещё раз через минуту."
        elif "401" in err or "incorrect api key" in err.lower() or ("auth" in err.lower() and "key" in err.lower()):
            hint = "Неверный OPENAI_API_KEY — проверь ключ в Railway Variables."
        elif "413" in err or "too large" in err.lower() or "maximum" in err.lower():
            hint = "Файл слишком большой для Whisper API. Попробуй обрезать запись."
        elif "timeout" in err.lower():
            hint = "Сервер не ответил вовремя — попробуй ещё раз."
        elif "rate" in err.lower():
            hint = "Слишком много запросов — подожди минуту и повтори."
        else:
            hint = f"Ошибка обработки: {err[:200]}"
        try:
            bot.edit_message_text(f"❌ {hint}", chat_id, progress_message_id)
        except Exception:
            safe_send(chat_id, f"❌ {hint}", markup)


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

# Фоторедактор: chat_id → {"prompt": str, "actor_id": int}
waiting_for_photo_edit: dict = {}

# Чаты в режиме свободного ИИ-диалога («🤖 ИИ чат»)
ai_chat_mode: set = set()

# Режим ИИ по чатам: default — штатный backend, kryven — Kryven API.
AI_BACKEND_MODES_FILE = "ai_backend_modes.json"

def _load_ai_backend_modes() -> dict:
    if os.path.exists(AI_BACKEND_MODES_FILE):
        try:
            import json
            with open(AI_BACKEND_MODES_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return {
                int(chat_id): str(mode).lower()
                for chat_id, mode in data.items()
                if str(mode).lower() in ("default", "kryven")
            }
        except Exception:
            pass
    return {}


def _save_ai_backend_modes():
    import json
    with open(AI_BACKEND_MODES_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {str(chat_id): mode for chat_id, mode in chat_ai_backend_modes.items()},
            f,
            ensure_ascii=False,
            indent=2,
        )


chat_ai_backend_modes: dict = _load_ai_backend_modes()


def _get_chat_ai_backend(chat_id: int) -> str:
    return chat_ai_backend_modes.get(chat_id, "default")


def _set_chat_ai_backend(chat_id: int, mode: str):
    mode = (mode or "default").lower()
    if mode == "kryven":
        chat_ai_backend_modes[chat_id] = "kryven"
    else:
        chat_ai_backend_modes.pop(chat_id, None)
    _save_ai_backend_modes()


def _chat_ai_backend_label(chat_id: int) -> str:
    if _get_chat_ai_backend(chat_id) == "kryven":
        return "Kryven"
    return os.environ.get("LLM_BACKEND", "openai").lower()

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

# История чата — сохраняется на диск, максимум HISTORY_MAX сообщений на пользователя
GROK_HISTORY_FILE = CONFIG.grok_history_file
HISTORY_MAX = CONFIG.grok_history_max  # 80 сообщений = примерно 40 обменов
CHAT_MEMORY_MAX_CHARS = CONFIG.chat_memory_max_chars
CHAT_MEMORY_FOR_ALL = CONFIG.chat_memory_for_all

def _load_grok_history() -> dict:
    data = read_json_file(GROK_HISTORY_FILE, {}, logger=logger)
    try:
        return {int(k): v for k, v in data.items() if isinstance(v, list)}
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("Chat history file has invalid format: %s (%s)", GROK_HISTORY_FILE, exc)
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
    # Обрезаем перед записью — так файл не растёт бесконечно
    trimmed = {str(k): _trim_history(v) for k, v in grok_history.items()}
    write_json_file(GROK_HISTORY_FILE, trimmed, logger=logger)

grok_history: dict = _load_grok_history()
_chat_memory_lock = threading.Lock()

# Известные пользователи: username (без @) → chat_id (сохраняем на диск)
KNOWN_USERS_FILE = CONFIG.known_users_file
USER_PROFILES_FILE = CONFIG.user_profiles_file
PENDING_USER_MESSAGES_FILE = CONFIG.pending_user_messages_file
START_EVENTS_FILE = os.environ.get("START_EVENTS_FILE", "start_events.json")
PINNED_PROFILE_NAMES = {
    "korablikkkkkkk": "мото моточка",
    "nesss31": "Гуцульский комерс",
    "skyyylit": "инопланетянин",
}
PENDING_DEVELOPER_USERNAMES = {
    "nesss31",
    "skyyylit",
}
DIRECT_MESSAGE_ALIASES = {
    "skyyylit": "skyyylit",
    "инопланетянин": "skyyylit",
    "инопланетянину": "skyyylit",
    "инопланетяну": "skyyylit",
    "nesss31": "nesss31",
    "гуцульский комерс": "nesss31",
    "гуцульскому комерсу": "nesss31",
    "гуцульский коммерс": "nesss31",
    "гуцульскому коммерсу": "nesss31",
    "комерс": "nesss31",
    "комерсу": "nesss31",
    "коммерс": "nesss31",
    "коммерсу": "nesss31",
}
PINNED_PROFILE_RULES = {
    "nesss31": (
        "Обращайся к этому пользователю как к Гуцульскому комерсу. "
        "Стиль — как свой с района: грубо, прямо, с матом, дружескими оскорблениями, "
        "подколами и пацанским вайбом. Можно говорить жёстко и не церемониться, "
        "но без реальных угроз, злой травли, призывов к насилию и унижения по личным признакам."
    ),
    "skyyylit": (
        "Обращайся к этому пользователю как к инопланетянину, человеку из другой Вселенной, "
        "гостю нашей планеты или похожими дружескими вариантами. Варьируй формулировки, "
        "можно с лёгким стёбом, но без злой травли."
    ),
}


def _apply_pinned_profile_rule(profile: dict, username: str) -> dict:
    uname = str(username or "").lower().strip()
    pinned_name = PINNED_PROFILE_NAMES.get(uname)
    if pinned_name:
        profile["custom_name"] = pinned_name
        profile["display_rule"] = PINNED_PROFILE_RULES.get(
            uname,
            f'Обращаться к пользователю только как "{pinned_name}".',
        )
    return profile


def _apply_pending_username_access(username: str, chat_id: int) -> str | None:
    """Grant pre-approved roles when a known username first presses /start."""
    uname = str(username or "").lower().strip().lstrip("@")
    if not uname:
        return None
    if uname in PENDING_DEVELOPER_USERNAMES and chat_id != OWNER_ID:
        grant_access(chat_id)
        set_role(chat_id, "developer")
        logger.info("Pending developer access granted: @%s -> %s", uname, chat_id)
        return "developer"
    return None

def _load_known_users() -> dict:
    data = read_json_file(KNOWN_USERS_FILE, {}, logger=logger)
    try:
        return {k: int(v) for k, v in data.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("Known users file has invalid format: %s (%s)", KNOWN_USERS_FILE, exc)
    return {}

def _save_known_users():
    write_json_file(KNOWN_USERS_FILE, known_users, logger=logger)

known_users: dict = _load_known_users()


def _load_user_profiles() -> dict:
    data = read_json_file(USER_PROFILES_FILE, {}, logger=logger)
    try:
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except AttributeError as exc:
        logger.warning("User profiles file has invalid format: %s (%s)", USER_PROFILES_FILE, exc)
    return {}


def _save_user_profiles():
    write_json_file(USER_PROFILES_FILE, user_profiles, logger=logger)


def _load_pending_user_messages() -> dict:
    data = read_json_file(PENDING_USER_MESSAGES_FILE, {}, logger=logger)
    try:
        return {
            str(k).lower(): [str(item) for item in v if str(item).strip()]
            for k, v in data.items()
            if isinstance(v, list)
        }
    except AttributeError as exc:
        logger.warning("Pending messages file has invalid format: %s (%s)", PENDING_USER_MESSAGES_FILE, exc)
    return {}


def _save_pending_user_messages(data: dict):
    write_json_file(PENDING_USER_MESSAGES_FILE, data, logger=logger)


def _load_start_events() -> list:
    data = read_json_file(START_EVENTS_FILE, [], logger=logger)
    return data if isinstance(data, list) else []


def _save_start_events(events: list):
    write_json_file(START_EVENTS_FILE, events[-1000:], logger=logger)


def _record_start_event(message, role: str):
    """Persist and notify Ruslan about every private /start."""
    try:
        user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        if not user or not chat:
            return
        user_id = int(getattr(user, "id", 0) or getattr(chat, "id", 0) or 0)
        chat_id = int(getattr(chat, "id", user_id) or user_id)
        if not user_id:
            return

        username = (getattr(user, "username", None) or "").strip()
        first_name = (getattr(user, "first_name", None) or "").strip()
        last_name = (getattr(user, "last_name", None) or "").strip()
        full_name = " ".join(p for p in (first_name, last_name) if p).strip()
        now = _now_local().strftime("%Y-%m-%d %H:%M")

        events = _load_start_events()
        previous_count = sum(
            1 for item in events
            if str(item.get("user_id", "")) == str(user_id)
            or (username and str(item.get("username", "")).lower() == username.lower())
        )
        entry = {
            "time": now,
            "user_id": user_id,
            "chat_id": chat_id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "language_code": getattr(user, "language_code", "") or "",
            "role": role,
            "start_count_for_user": previous_count + 1,
        }
        events.append(entry)
        _save_start_events(events)

        if user_id == OWNER_ID:
            return

        profile = {
            "user_id": user_id,
            "chat_id": chat_id,
            "username": username,
            "full_name": full_name,
            "first_name": first_name,
            "last_name": last_name,
        }
        if username:
            _apply_pinned_profile_rule(profile, username)
        display_name = _profile_display_name(profile, f"id {user_id}")
        username_label = f"@{username}" if username else "без username"
        text = (
            "🆕 */start в боте*\n\n"
            f"Кто: *{_md_escape(display_name)}*\n"
            f"Username: `{_md_escape(username_label)}`\n"
            f"ID: `{user_id}`\n"
            f"Роль: `{_md_escape(role or 'guest')}`\n"
            f"Стартов от него: `{previous_count + 1}`\n"
            f"Время: `{now}`"
        )
        safe_send(OWNER_ID, text, main_menu(OWNER_ID))
    except Exception as e:
        logger.exception("Start event tracking failed: %s", e)


def _queue_pending_user_message(username: str, text: str):
    username = str(username or "").strip().lstrip("@").lower()
    text = str(text or "").strip()
    if not username or not text:
        return
    pending = _load_pending_user_messages()
    messages = pending.setdefault(username, [])
    messages.append(text)
    pending[username] = messages[-10:]
    _save_pending_user_messages(pending)


def _send_owner_message_to_user(owner_chat_id: int, username: str, text: str) -> bool:
    username = str(username or "").strip().lstrip("@").lower()
    text = str(text or "").strip()
    if owner_chat_id != OWNER_ID:
        bot.send_message(owner_chat_id, "🔒 Писать пользователям через бота может только Руслан.")
        return True
    if not username or not text:
        bot.send_message(owner_chat_id, "⚠️ Формат: напиши @username: текст")
        return True

    outgoing = f"Сообщение от Руслана:\n{text}"
    target_chat_id = known_users.get(username)
    if target_chat_id:
        try:
            bot.send_message(int(target_chat_id), outgoing)
            bot.send_message(owner_chat_id, f"✅ Отправил @{username}.")
            return True
        except Exception as e:
            logger.warning("Cannot send direct message to @%s: %s", username, e)
            _queue_pending_user_message(username, outgoing)
            bot.send_message(
                owner_chat_id,
                f"⚠️ Сейчас не смог отправить @{username}. Сохранил в очередь — доставлю после его /start.",
            )
            return True

    _queue_pending_user_message(username, outgoing)
    bot.send_message(
        owner_chat_id,
        f"🕓 @{username} ещё не найден в users. Сохранил сообщение — доставлю, когда он нажмёт /start.",
    )
    return True


def _normalize_direct_message_target(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().lstrip("@"))


def _resolve_direct_message_alias(value: str) -> str | None:
    return DIRECT_MESSAGE_ALIASES.get(_normalize_direct_message_target(value))


def _direct_message_alias_pattern() -> str:
    aliases = sorted(DIRECT_MESSAGE_ALIASES, key=len, reverse=True)
    return "|".join(re.escape(alias).replace(r"\ ", r"\s+") for alias in aliases)


def _handle_owner_direct_message_command(chat_id: int, text: str) -> bool:
    raw = str(text or "").strip()
    match = re.match(
        r"^(?:(?:напиши|передай|отправь)\s+)?@([A-Za-z0-9_]{3,32})\s*[:：,-]\s*(.+)$",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        username, message_text = match.group(1), match.group(2)
        return _send_owner_message_to_user(chat_id, username, message_text)

    match = re.match(
        r"^(?:напиши|передай|отправь)\s+@([A-Za-z0-9_]{3,32})\s+(.+)$",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        username, message_text = match.group(1), match.group(2)
        return _send_owner_message_to_user(chat_id, username, message_text)

    alias_pattern = _direct_message_alias_pattern()
    match = re.match(
        rf"^(?:(?:напиши|передай|отправь)\s+)?({alias_pattern})\s*[:：,-]?\s+(.+)$",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return False

    username = _resolve_direct_message_alias(match.group(1))
    if not username:
        return False
    return _send_owner_message_to_user(chat_id, username, match.group(2))


user_profiles: dict = _load_user_profiles()


def _track_user(message, event: str = "message", from_user=None):
    """Запоминает, кто пользуется ботом, без сохранения текста сообщений."""
    try:
        user = from_user or getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        if not user or not chat:
            return
        user_id = int(getattr(user, "id", 0) or getattr(chat, "id", 0))
        chat_id = int(getattr(chat, "id", user_id) or user_id)
        if not user_id:
            return

        now = _now_local().strftime("%Y-%m-%d %H:%M")
        key = str(user_id)
        profile = user_profiles.get(key, {})
        username = (getattr(user, "username", None) or "").strip()
        first_name = (getattr(user, "first_name", None) or "").strip()
        last_name = (getattr(user, "last_name", None) or "").strip()
        full_name = " ".join(p for p in (first_name, last_name) if p).strip()

        profile.update({
            "user_id": user_id,
            "chat_id": user_id,
            "last_chat_id": chat_id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "language_code": getattr(user, "language_code", "") or "",
            "is_bot": bool(getattr(user, "is_bot", False)),
            "chat_type": getattr(chat, "type", "") or "",
            "last_seen": now,
            "last_event": event,
            "message_count": int(profile.get("message_count", 0)) + 1,
        })
        _apply_pinned_profile_rule(profile, username)
        profile.setdefault("first_seen", now)
        user_profiles[key] = profile

        if username:
            uname = username.lower()
            for old_name, old_chat_id in list(known_users.items()):
                if old_chat_id == user_id and old_name != uname:
                    known_users.pop(old_name, None)
            known_users[uname] = user_id
            _save_known_users()
            pending = _load_pending_user_messages()
            messages = pending.pop(uname, [])
            if messages:
                _save_pending_user_messages(pending)
                for pending_text in messages[:5]:
                    try:
                        bot.send_message(chat_id, pending_text)
                    except Exception as send_error:
                        logger.warning("Cannot send pending message to @%s: %s", uname, send_error)
        _save_user_profiles()
    except Exception as e:
        logger.exception("User tracking failed: %s", e)


def _md_escape(value) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("*", "\\*").replace("_", "\\_")


def _profile_display_name(profile: dict, fallback: str = "Без имени") -> str:
    if not isinstance(profile, dict):
        return fallback
    custom_name = str(profile.get("custom_name") or "").strip()
    if custom_name:
        return custom_name
    full_name = str(profile.get("full_name") or "").strip()
    if full_name:
        return full_name
    first_name = str(profile.get("first_name") or "").strip()
    last_name = str(profile.get("last_name") or "").strip()
    name = " ".join(p for p in (first_name, last_name) if p).strip()
    return name or fallback


def _profile_for_chat_id(chat_id: int) -> dict:
    try:
        target_id = int(chat_id)
    except Exception:
        return {}
    for profile in user_profiles.values():
        try:
            profile_chat_id = int(profile.get("chat_id") or profile.get("user_id") or 0)
        except Exception:
            continue
        if profile_chat_id == target_id:
            username = str(profile.get("username") or "").strip()
            if username:
                _apply_pinned_profile_rule(profile, username)
            return profile
    for username, known_chat_id in known_users.items():
        try:
            if int(known_chat_id) != target_id:
                continue
        except Exception:
            continue
        profile = {
            "user_id": target_id,
            "chat_id": target_id,
            "username": username,
        }
        return _apply_pinned_profile_rule(profile, username)
    return {}


def _actor_id_for_request(chat_id: int, from_user=None) -> int:
    try:
        user_id = int(getattr(from_user, "id", 0) or 0)
        if user_id:
            return user_id
    except Exception:
        pass
    try:
        return int(chat_id)
    except Exception:
        return 0


def _profile_from_user(from_user=None, fallback_chat_id: int | None = None) -> dict:
    actor_id = _actor_id_for_request(fallback_chat_id or 0, from_user)
    profile = dict(_profile_for_chat_id(actor_id)) if actor_id else {}
    if from_user:
        username = (getattr(from_user, "username", None) or "").strip()
        first_name = (getattr(from_user, "first_name", None) or "").strip()
        last_name = (getattr(from_user, "last_name", None) or "").strip()
        full_name = " ".join(p for p in (first_name, last_name) if p).strip()
        profile.update({
            "user_id": actor_id,
            "chat_id": actor_id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "language_code": getattr(from_user, "language_code", "") or "",
            "is_bot": bool(getattr(from_user, "is_bot", False)),
        })
        if username:
            _apply_pinned_profile_rule(profile, username)
    return profile


def _actor_identity_block(chat_id: int, from_user=None) -> str:
    actor_id = _actor_id_for_request(chat_id, from_user)
    profile = _profile_from_user(from_user, actor_id)
    username = str(profile.get("username") or "").strip()
    display_name = _profile_display_name(profile, "").strip()
    role = "owner" if actor_id == OWNER_ID else get_role(actor_id)
    role_label = {
        "owner": "владелец",
        "developer": "разработчик",
        "worker": "работник",
        "driver": "водитель",
        "guest": "гость",
    }.get(role, role or "guest")

    handle = f"@{username}" if username else ""
    name_part = " / ".join(p for p in (display_name, handle) if p)
    if not name_part:
        name_part = f"id {actor_id}" if actor_id else "неизвестный пользователь"

    lines = ["Текущий собеседник:"]
    if actor_id == OWNER_ID:
        lines.append("- Это Руслан, владелец бота. Можно обращаться к нему как к Руслану.")
        lines.append("- Его личные факты, семья, бизнес и задачи относятся к текущему собеседнику.")
    else:
        lines.append(f"- Это НЕ Руслан. Это другой пользователь бота: {name_part}.")
        lines.append(f"- Роль пользователя: {role_label}.")
        lines.append("- Руслан — владелец бота и отдельный человек. Говори о Руслане в третьем лице.")
        lines.append("- Не называй текущего пользователя Русланом и не применяй к нему факты Руслана: жена, дочь, бизнес, водитель, ФОП, кошельки, ПК.")
        if role == "developer":
            lines.append("- Ему разрешён технический доступ к коду бота: просмотр разрешённых файлов и загрузка правок.")
            lines.append("- Не выполняй личные действия от имени Руслана: SMS, звонки, CRM, ПК, роли и память владельца.")
        else:
            lines.append("- Не выполняй действия от имени Руслана для этого пользователя: SMS, звонки, CRM, ПК, код, роли, память владельца.")
        lines.append("- Если пользователь хочет связаться с Русланом, помоги ему написать/позвонить Руслану доступным способом.")

    try:
        if actor_id and int(chat_id) != int(actor_id):
            lines.append("- Сообщение пришло из группы: отвечай автору сообщения, а не группе как будто это Руслан.")
    except Exception:
        pass

    display_rule = str(profile.get("display_rule") or "").strip()
    if display_rule:
        lines.append(f"- Особое правило обращения: {display_rule}")

    return "\n".join(lines) + "\n"


PHOTO_EDIT_DEFAULT_PROMPT = "Улучши фото естественно: свет, резкость, цвета. Не меняй смысл сцены."
PHOTO_EDIT_PREFIXES = (
    "редактируй",
    "отредактируй",
    "фоторедактор",
    "обработай фото",
    "обработай",
    "измени фото",
    "измени",
    "улучши фото",
    "улучши",
)


def _photo_editor_allowed(chat_id: int, from_user=None) -> bool:
    if _env_flag("PHOTO_EDITOR_FOR_ALL", False):
        return True
    actor_id = _actor_id_for_request(chat_id, from_user)
    if actor_id == OWNER_ID:
        return True
    username = (getattr(from_user, "username", None) or "").strip().lstrip("@").lower()
    if username and username in PHOTO_EDITOR_ALLOWED_USERNAMES:
        return True
    profile = _profile_from_user(from_user, actor_id)
    profile_username = str(profile.get("username") or "").strip().lstrip("@").lower()
    return bool(profile_username and profile_username in PHOTO_EDITOR_ALLOWED_USERNAMES)


def _extract_photo_edit_prompt(text: str, bot_username: str | None = None) -> str | None:
    raw = strip_bot_mention(str(text or ""), bot_username).strip()
    if not raw:
        return None

    parts = raw.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if command in ("/photo", "/foto", "/editphoto", "/image"):
        return parts[1].strip() if len(parts) > 1 and parts[1].strip() else PHOTO_EDIT_DEFAULT_PROMPT

    low = raw.casefold()
    for prefix in PHOTO_EDIT_PREFIXES:
        if low == prefix:
            return PHOTO_EDIT_DEFAULT_PROMPT
        if low.startswith(prefix + " "):
            prompt = raw[len(prefix):].strip(" :—-\n\t")
            return prompt or PHOTO_EDIT_DEFAULT_PROMPT
    return None


def _btn_photo_editor(chat_id: int, prompt: str | None = None, from_user=None):
    if not _photo_editor_allowed(chat_id, from_user):
        bot.send_message(chat_id, "🖼 Фоторедактор пока доступен только Руслану и @skyyylit.")
        return
    if not photo_editor.image_editor_available(CONFIG.openai_direct_api_key):
        bot.send_message(chat_id, "⚠️ Фоторедактору нужен прямой OPENAI_API_KEY в Railway Variables.")
        return

    clean_prompt = (prompt or PHOTO_EDIT_DEFAULT_PROMPT).strip()
    waiting_for_photo_edit[chat_id] = {
        "prompt": clean_prompt,
        "actor_id": _actor_id_for_request(chat_id, from_user),
    }
    bot.send_message(
        chat_id,
        "🖼 Кидай фото одним сообщением.\n"
        f"Задача: {clean_prompt}",
        reply_markup=None if _chat_is_group(chat_id) else main_menu(chat_id),
    )


def _display_name_for_chat_id(chat_id: int, fallback: str = "работник") -> str:
    profile = _profile_for_chat_id(chat_id)
    display_name = _profile_display_name(profile, "")
    if display_name:
        return display_name
    try:
        target_id = int(chat_id)
    except Exception:
        return fallback
    for username, known_chat_id in known_users.items():
        try:
            if int(known_chat_id) == target_id:
                return f"@{username}"
        except Exception:
            continue
    return fallback


def _format_users_report(limit: int = 50) -> str:
    """Формирует отчёт владельцу: кто пользовался ботом и когда."""
    role_names = {"owner": "Владелец", "developer": "Разработчик", "driver": "Водитель", "worker": "Работник", "guest": "Гость"}
    roles_data = list_roles()

    profiles = {str(k): dict(v) for k, v in user_profiles.items()}
    for username, chat_id in known_users.items():
        key = str(chat_id)
        profile = profiles.setdefault(key, {
            "user_id": chat_id,
            "chat_id": chat_id,
            "username": username,
            "first_seen": "",
            "last_seen": "",
            "message_count": 0,
        })
        _apply_pinned_profile_rule(profile, username)
    existing_usernames = {
        str(profile.get("username") or "").lower()
        for profile in profiles.values()
        if isinstance(profile, dict)
    }
    for username, custom_name in PINNED_PROFILE_NAMES.items():
        if username in existing_usernames:
            continue
        profiles[f"pinned:{username}"] = {
            "user_id": 0,
            "chat_id": 0,
            "username": username,
            "custom_name": custom_name,
            "first_seen": "ждет сообщения боту",
            "last_seen": "ждет сообщения боту",
            "last_event": "закрепленное имя",
            "message_count": 0,
            "pending": True,
        }

    if not profiles:
        return "👥 Пока никто не запускал бота."

    items = list(profiles.values())
    items.sort(key=lambda p: (p.get("last_seen") or p.get("first_seen") or ""), reverse=True)
    shown = items[:limit]

    lines = [
        "👥 *Пользователи бота*",
        "",
        f"Всего известно: *{len(items)}*",
        f"Показываю последних: *{len(shown)}*",
        "",
    ]
    for i, profile in enumerate(shown, 1):
        chat_id = int(profile.get("chat_id") or profile.get("user_id") or 0)
        username = profile.get("username") or ""
        name = _profile_display_name(profile)
        role = role_names.get(roles_data.get(str(chat_id), "guest"), "Гость")
        username_label = f"@{_md_escape(username)}" if username else "_без username_"
        first_seen = profile.get("first_seen") or "?"
        last_seen = profile.get("last_seen") or "?"
        count = int(profile.get("message_count", 0) or 0)
        last_event = profile.get("last_event") or "?"
        lines.extend([
            f"{i}. *{_md_escape(name)}* · {username_label}",
            f"   ID: `{chat_id}` · роль: *{role}*" if chat_id else "   ID: _появится после сообщения боту_ · роль: *Гость*",
            f"   Первый вход: `{first_seen}`",
            f"   Последний раз: `{last_seen}` · действий: `{count}` · `{_md_escape(last_event)}`",
            "",
        ])
    if len(items) > limit:
        lines.append(f"Ещё скрыто: {len(items) - limit}.")
    return "\n".join(lines).strip()


def main_menu(chat_id: int | None = None):
    """Главное меню — только нужные рабочие команды."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("🩺 Статус", "📞 Звонок")
    markup.add("📋 Задачи")
    markup.add("🔍 Саурон", "📁 Файл → Саурон")
    if chat_id is not None and _photo_editor_allowed(chat_id):
        markup.add("🖼 Фоторедактор")
    if chat_id is not None and _can_access_code(chat_id):
        markup.add("📂 Код")
    if chat_id == OWNER_ID:
        markup.add("👥 Пользователи")
    return markup


BOT_COMMANDS = (
    ("start", "главное меню"),
    ("help", "что умеет бот"),
    ("status", "статус бота"),
    ("call", "звонок или ИИ-звонок"),
    ("tasks", "задачи и напоминания"),
    ("sauron", "поиск через Sauron"),
    ("file_sauron", "поиск по файлу через Sauron"),
    ("photo", "фоторедактор"),
    ("files", "файлы кода"),
    ("code", "показать файл кода"),
    ("ai_mode", "текущий режим ИИ"),
    ("kryven", "включить Kryven"),
    ("openai", "вернуть обычный ИИ"),
    ("memory", "память бота"),
    ("forget", "очистить историю диалога"),
    ("users", "кто пользуется ботом"),
)


def _sync_bot_commands():
    """Обновляет меню быстрых команд Telegram."""
    try:
        commands = [types.BotCommand(command, description) for command, description in BOT_COMMANDS]
        bot.set_my_commands(commands)
        logger.info("Telegram quick commands synced: %s", len(commands))
    except Exception as e:
        logger.warning("Telegram quick commands sync failed: %s", e)


def ai_chat_menu():
    """Клавиатура внутри режима разговора — только выход."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("🔙 Выйти из чата")
    return markup


def jarvis_menu():
    """Отдельный Jarvis-раздел отключён; используем обычное меню."""
    return main_menu()


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
    """Меню для рабочего — только связь с Русланом."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("📞 Позвонить Руслану")
    return markup


def developer_menu():
    """Меню для разработчика — код бота без личных owner-действий."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("📂 Код", "🩺 Статус")
    markup.add("🔍 Саурон", "📁 Файл → Саурон")
    return markup


def get_menu_for_role(role: str):
    if role == "developer":
        return developer_menu()
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


def _handle_grok_action(chat_id: int, action_type: str, action_param: str | None, actor_id: int | None = None):
    """Выполняет действие из ACTION-тега Grok."""
    try:
        actual_actor_id = int(actor_id if actor_id is not None else chat_id)
    except Exception:
        actual_actor_id = int(chat_id) if str(chat_id).lstrip("-").isdigit() else 0
    owner_only_actions = {
        "send_sms", "call_wife", "call_toha", "sms_toha",
        "assign_role", "remember", "recall", "forget_fact", "forget_all_facts",
        "open_url", "search_files", "search_content",
        "screenshot", "screenshot_site", "open_folder", "launch_app", "close_app",
        "list_apps", "crm_expense", "call_restaurant", "sheet_analytics", "sheets_list",
    }
    if actual_actor_id != OWNER_ID and action_type in owner_only_actions:
        safe_send(chat_id, "Это действие только для Руслана.", _reply_markup_for_chat(chat_id))
        return

    disabled_actions = {
        "call_toha", "sms_toha", "sheet_analytics", "sheets_list",
        "open_url", "search_files", "search_content", "screenshot",
        "screenshot_site", "open_folder", "launch_app", "close_app",
        "list_apps", "crm_expense", "call_restaurant",
    }
    if action_type in disabled_actions:
        safe_send(chat_id, "Этот раздел отключён.", main_menu())
        return
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
        _btn_users(chat_id)

    elif action_type == "assign_role":
        if not action_param or ":" not in action_param:
            bot.send_message(chat_id, "⚠️ Укажи @username и роль. Пример: назначь @toha водителем")
            return
        username_raw, role = action_param.split(":", 1)
        username = username_raw.lstrip("@").lower().strip()
        role = role.strip()
        valid_roles = {"driver", "worker", "guest", "developer", "owner"}
        if role not in valid_roles:
            bot.send_message(chat_id, f"⚠️ Неверная роль «{role}». Допустимые: developer, driver, worker, guest.")
            return
        if username not in known_users:
            bot.send_message(chat_id, f"⚠️ @{username} ещё не запускал бота. Попроси его написать /start.")
            return
        target_id = known_users[username]
        grant_access(target_id)
        set_role(target_id, role)
        role_labels = {"driver": "Водитель", "worker": "Работник", "guest": "Гость", "developer": "Разработчик", "owner": "Владелец"}
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
        clear_chat_summary(chat_id)
        _save_grok_history()
        bot.send_message(chat_id, "🗑️ История и краткая память диалога очищены. Начинаем с чистого листа!", reply_markup=main_menu())

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
        _run_sauron_search(chat_id, action_param or "")

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


_CHAT_MEMORY_SYSTEM = (
    "Ты — модуль памяти Telegram-бота. Обновляй краткую долгосрочную память диалога. "
    "Сохраняй только то, что пригодится в будущих разговорах: устойчивые факты о пользователе, "
    "его проектах, целях, предпочтениях, настройках бота, текущих задачах, принятых решениях и важных контекстах. "
    "Не сохраняй одноразовый шум, эмоции момента, случайные фразы, длинные тексты, секреты, пароли, API-ключи, "
    "коды, номера карт и другие чувствительные данные. "
    "Если новой полезной информации нет — сохрани прежнюю память без изменений. "
    "Верни только обновлённую память на русском языке, короткими пунктами, без приветствий и без markdown-заголовка."
)


def _clean_chat_memory_summary(text: str, previous: str) -> str:
    """Очищает ответ LLM-памяти от служебного мусора и ограничивает размер."""
    import re
    summary = (text or "").strip()
    summary = re.sub(r"^```[a-zA-Zа-яА-Я0-9_-]*\s*", "", summary)
    summary = re.sub(r"\s*```$", "", summary).strip()
    summary = re.sub(r"\[ACTION:[^\]]+\]\s*", "", summary)
    summary = re.sub(r"\[REMEMBER:[^\]]+\]\s*", "", summary, flags=re.IGNORECASE)
    low = summary.lower().strip(" .!—-")
    if low in ("без изменений", "нет новой информации", "ничего не добавлять", "память без изменений"):
        summary = previous
    if len(summary) > CHAT_MEMORY_MAX_CHARS:
        cut = summary[:CHAT_MEMORY_MAX_CHARS]
        summary = cut.rsplit("\n", 1)[0].strip() or cut.strip()
    return summary


def _update_chat_memory(chat_id: int, user_text: str, assistant_text: str):
    """Фоново обновляет краткую память диалога через LLM."""
    if chat_id != OWNER_ID and not CHAT_MEMORY_FOR_ALL:
        return
    user_text = (user_text or "").strip()
    assistant_text = (assistant_text or "").strip()
    if not user_text or not assistant_text:
        return
    if user_text.startswith("/") and not user_text.lower().startswith(("/memory", "/start")):
        return

    user_clip = user_text[:5000] + ("…" if len(user_text) > 5000 else "")
    assistant_clip = assistant_text[:5000] + ("…" if len(assistant_text) > 5000 else "")

    try:
        with _chat_memory_lock:
            previous = get_chat_summary(chat_id)
            prompt = (
                "Предыдущая память диалога:\n"
                f"{previous or '(пока пусто)'}\n\n"
                "Последний обмен:\n"
                f"Пользователь: {user_clip}\n\n"
                f"Бот: {assistant_clip}\n\n"
                "Обнови память так, чтобы в следующем разговоре бот понимал контекст."
            )
            updated = ask_grok(prompt, [], memory_block=_CHAT_MEMORY_SYSTEM)
            summary = _clean_chat_memory_summary(updated, previous)
            if summary:
                save_chat_summary(chat_id, summary)
    except Exception as e:
        logger.exception("Chat memory update failed: %s", e)


def _schedule_chat_memory_update(chat_id: int, user_text: str, assistant_text: str):
    """Запускает обновление памяти не блокируя ответ пользователю."""
    try:
        threading.Thread(
            target=_update_chat_memory,
            args=(chat_id, user_text, assistant_text),
            daemon=True,
            name=f"chat-memory-{chat_id}",
        ).start()
    except Exception as e:
        logger.exception("Chat memory worker start failed: %s", e)


def _ask_grok_and_route(chat_id: int, text: str, extra_memory: str = "", from_user=None):
    """Отправляет сообщение в Grok, разбирает ACTION-теги и REMEMBER-теги, показывает ответ."""
    history = grok_history.get(chat_id, [])
    actor_id = _actor_id_for_request(chat_id, from_user)
    memory_block = format_for_prompt() + format_chat_summary_for_prompt(chat_id)
    if extra_memory:
        memory_block += "\n\nКонтекст рабочей группы:\n" + extra_memory.strip() + "\n"
    memory_block = _actor_identity_block(chat_id, from_user) + memory_block
    # Добавляем текущую дату и время, чтобы Grok правильно рассчитывал относительные сроки
    now = _now_local()
    tz = _ukraine_tz_hours()
    date_line = f"\nСейчас: {now.strftime('%Y-%m-%dT%H:%M')} (UTC+{tz}, Украина).\n"
    memory_block = date_line + memory_block
    # Базовая личность — умный помощник без лишних разделов меню.
    personality = (
        "Ты — Ruslan Helper, умный персональный ассистент Руслана. "
        "Думай как сильный ChatGPT/Codex: внимательно понимай запрос, рассуждай, "
        "предлагай следующий шаг и помогай доводить дело до результата. "
        "Общайся на русском языке: дерзко, живо, как умный кент в Telegram, без воды. "
        "Можно использовать мат, жёсткие подколы и грубоватые фразы, если это уместно по контексту. "
        "Не превращайся в токсичную помойку: без угроз, травли, унижения по национальности, религии, полу, болезни или другим личным признакам. "
        "Если не хватает данных — задай один короткий уточняющий вопрос. "
        "Если можно помочь сразу — помогай сразу. "
        "Никогда не проси пароли, коды SMS, токены или личные данные. "
        "Если пользователь сообщает важный устойчивый факт о себе, проекте, настройке или задаче, "
        "можешь добавить в конец ответа служебный тег [REMEMBER:короткий факт]. "
        "Не добавляй туда секреты, пароли, токены, номера карт и одноразовые мелочи. "
        "Для поиска информации через Sauron используй [ACTION:search_sauron:запрос]. "
        "Все отключённые разделы не предлагай: таблицы, Тоха, Dota, Jarvis, Мой ПК, Я Тигр, ФОП, бронирование. "
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
    try:
        if _get_chat_ai_backend(chat_id) == "kryven":
            if ask_kryven is None:
                reply = "❌ Kryven backend не загрузился. Переключись обратно командой /openai."
            else:
                reply = ask_kryven(text, history, memory_block=memory_block)
        else:
            reply = ask_grok(text, history, memory_block=memory_block)
    except Exception as e:
        logger.exception("AI reply failed: %s", e)
        safe_send(
            chat_id,
            "⚠️ Я получил сообщение, но ИИ-ответ сейчас не собрался. "
            "Попробуй ещё раз или переключи режим командой /openai.",
            _reply_markup_for_chat(chat_id),
        )
        return

    # Извлекаем оба формата тегов памяти: [REMEMBER:] и [ACTION:remember:] (только для владельца)
    new_facts, reply_without_remember = _extract_remember_tags(reply)
    remember_matches = []  # обработано ниже — post-conflict код становится no-op
    saved_count = 0
    if actor_id == OWNER_ID:
        for fact in new_facts:
            if add_fact(fact.strip()):
                saved_count += 1

    # Разбираем основной ACTION тег
    action_type, action_param, clean_reply = _parse_action(reply_without_remember)

    # Сохраняем в историю (без служебных тегов)
    assistant_memory_text = clean_reply or reply_without_remember
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": assistant_memory_text})
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
        _handle_grok_action(chat_id, action_type, action_param, actor_id=actor_id)
        return  # forget сам выводит сообщение
    if action_type:
        _handle_grok_action(chat_id, action_type, action_param, actor_id=actor_id)

    _schedule_chat_memory_update(chat_id, text, assistant_memory_text)

    # Показываем текстовый ответ Grok (если не пустой)
    if clean_reply and clean_reply.strip():
        if is_voice:
            _tts_send_voice(chat_id, clean_reply)
        safe_send(chat_id, clean_reply, _reply_markup_for_chat(chat_id))

    # Тихо уведомляем если что-то запомнено
    if saved_count > 0:
        noun = "факт" if saved_count == 1 else ("факта" if saved_count < 5 else "фактов")
        bot.send_message(chat_id, f"🧠 Запомнил {saved_count} {noun}.")


def process_text(chat_id, text, from_user=None):
    import re
    t = text.strip()
    tl = t.lower()

    if _handle_owner_direct_message_command(chat_id, t):
        return

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
            waiting_for_owner_call[chat_id] = {"step": "message_or_mode", "number": number}
            bot.send_message(
                chat_id,
                f"✅ Номер: *{number}*\n\n"
                "Напиши текст — я позвоню и скажу его более живым голосом.\n\n"
                "Или напиши *диалог* — тогда ИИ позвонит, послушает человека и попробует пообщаться.",
                parse_mode="Markdown",
            )
            return
        elif state["step"] == "message_or_mode":
            number = state["number"]
            if tl.strip() in ("диалог", "живой диалог", "ии диалог", "общение", "поговорить", "разговор"):
                if not voice_call_available():
                    waiting_for_owner_call[chat_id] = {"step": "message_or_mode", "number": number}
                    bot.send_message(
                        chat_id,
                        "⚠️ Живой ИИ-звонок уже встроен, но ему нужен публичный URL сервера, "
                        "чтобы Twilio мог присылать ответы человека обратно боту.\n\n"
                        "Пока можешь написать обычный текст — я позвоню и озвучу его живее.",
                    )
                    return
                waiting_for_owner_call[chat_id] = {"step": "dialog_goal", "number": number}
                bot.send_message(
                    chat_id,
                    "🎙️ *Живой ИИ-звонок*\n\n"
                    "Напиши цель разговора: что нужно сказать, выяснить или к какому итогу привести человека.",
                    parse_mode="Markdown",
                )
                return
            msg = t
            waiting_for_owner_call[chat_id] = {
                "step": "confirm",
                "number": number,
                "message": msg,
                "mode": "static",
            }
            bot.send_message(
                chat_id,
                f"📞 *Готов позвонить*\n\n"
                f"Номер: `{number}`\n"
                f"Скажу живым голосом: _{msg}_\n\n"
                f"⚠️ Это *реальный звонок*. Подтвердить?\n"
                f"Напиши *да* чтобы позвонить, *нет* чтобы отменить.",
                parse_mode="Markdown",
            )
            return
        elif state["step"] == "message":
            number = state["number"]
            msg = t
            # Показываем план и просим подтверждение ПЕРЕД реальным звонком
            waiting_for_owner_call[chat_id] = {"step": "confirm", "number": number, "message": msg, "mode": "static"}
            bot.send_message(
                chat_id,
                f"📞 *Готов позвонить*\n\n"
                f"Номер: `{number}`\n"
                f"Скажу живым голосом: _{msg}_\n\n"
                f"⚠️ Это *реальный звонок*. Подтвердить?\n"
                f"Напиши *да* чтобы позвонить, *нет* чтобы отменить.",
                parse_mode="Markdown",
            )
            return
        elif state["step"] == "dialog_goal":
            number = state["number"]
            goal = t
            waiting_for_owner_call[chat_id] = {
                "step": "confirm",
                "number": number,
                "message": goal,
                "mode": "dialog",
            }
            bot.send_message(
                chat_id,
                f"🎙️ *Готов запустить живой ИИ-звонок*\n\n"
                f"Номер: `{number}`\n"
                f"Цель разговора: _{goal}_\n\n"
                "ИИ представится голосовым помощником Руслана, будет слушать ответы и говорить короткими репликами.\n\n"
                f"⚠️ Это *реальный звонок*. Подтвердить?\n"
                f"Напиши *да* чтобы позвонить, *нет* чтобы отменить.",
                parse_mode="Markdown",
            )
            return
        elif state["step"] == "confirm":
            number = state["number"]
            message = state["message"]
            mode = state.get("mode", "static")
            waiting_for_owner_call.pop(chat_id)
            if tl.strip() in ("да", "yes", "давай", "подтверждаю", "звони", "ok", "ок"):
                bot.send_message(chat_id, f"📞 Звоню на {number}...")
                if mode == "dialog":
                    ok, info = make_ai_call(number, message)
                else:
                    ok, info = make_call(number, message)
                if ok:
                    if mode == "dialog":
                        bot.send_message(
                            chat_id,
                            f"✅ ИИ-звонок запущен на *{number}*\nЦель: _{message}_",
                            parse_mode="Markdown", reply_markup=main_menu(),
                        )
                    else:
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
        markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
        _run_sauron_search(chat_id, query, markup)
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
        _handle_grok_action(chat_id, "usdt", address, actor_id=_actor_id_for_request(chat_id, from_user))
        return

    # ══════════════════════════════════════════════════════════════════
    # 1.5 ЯВНЫЕ КОМАНДЫ ПАМЯТИ — «запомни что...» (только для владельца)
    # ══════════════════════════════════════════════════════════════════

    if tl.strip() in ("kryven", "кривен", "включи kryven", "включи кривен", "режим kryven", "режим кривен"):
        if not kryven_available():
            bot.send_message(
                chat_id,
                "❌ Kryven пока не настроен: нужен KRYVEN_API_KEY в Railway Variables.",
                reply_markup=main_menu(chat_id),
            )
            return
        _set_chat_ai_backend(chat_id, "kryven")
        bot.send_message(
            chat_id,
            "🧠 Kryven включён. Следующие обычные ответы пойдут через Kryven.\n\n"
            "Вернуть обычный режим: /openai",
            reply_markup=main_menu(chat_id),
        )
        return

    if tl.strip() in ("openai", "chatgpt", "чатгпт", "обычный ии", "выключи kryven", "выключи кривен"):
        _set_chat_ai_backend(chat_id, "default")
        bot.send_message(
            chat_id,
            "✅ Обычный ИИ-режим включён. Следующие ответы снова пойдут через основной backend.",
            reply_markup=main_menu(chat_id),
        )
        return

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
        _ask_grok_and_route(chat_id, t, from_user=from_user)
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
            "👥 пользователи": lambda: _btn_users(chat_id),
            "пользователи": lambda: _btn_users(chat_id),
            "кто пользуется ботом": lambda: _btn_users(chat_id),
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
        _ask_grok_and_route(chat_id, t, from_user=from_user)
        return

    BUTTON_LABELS = {
        # Оставленные команды
        "🩺 статус":             lambda: _btn_status(chat_id),
        "статус":                lambda: _btn_status(chat_id),
        "📞 звонок":             lambda: _btn_call(chat_id),
        "📞 позвонить":          lambda: _btn_call(chat_id),
        "позвонить":             lambda: _btn_call(chat_id),
        "📂 код":                lambda: _send_code_list(chat_id),
        "код":                   lambda: _send_code_list(chat_id),
        "файлы кода":            lambda: _send_code_list(chat_id),
        "покажи файлы кода":     lambda: _send_code_list(chat_id),
        "📋 задачи":             lambda: _btn_tasks(chat_id),
        "задачи":                lambda: _btn_tasks(chat_id),
        "👥 пользователи":       lambda: _btn_users(chat_id),
        "пользователи":          lambda: _btn_users(chat_id),
        "кто пользуется ботом":  lambda: _btn_users(chat_id),
        "кто пользуется":        lambda: _btn_users(chat_id),
        "🔍 саурон":             lambda: _btn_sauron_search(chat_id),
        "саурон":                lambda: _btn_sauron_search(chat_id),
        "📁 файл → саурон":     lambda: _btn_file_sauron(chat_id),
        "файл саурон":           lambda: _btn_file_sauron(chat_id),
        "🖼 фоторедактор":       lambda: _btn_photo_editor(chat_id, from_user=from_user),
        "фоторедактор":          lambda: _btn_photo_editor(chat_id, from_user=from_user),
        "🔙 назад":              lambda: bot.send_message(chat_id, "Главное меню 👇", reply_markup=main_menu()),
    }

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

    user_report_triggers = [
        "кто пользуется ботом", "кто пользовался ботом", "список пользователей",
        "покажи пользователей", "пользователи бота", "активность пользователей",
    ]
    if any(trigger in tl for trigger in user_report_triggers):
        _btn_users(chat_id)
        return

    code_show_prefixes = ("покажи код ", "открой код ", "/code ")
    for prefix in code_show_prefixes:
        if tl.startswith(prefix):
            _send_code_file(chat_id, t[len(prefix):].strip())
            return

    photo_prompt = _extract_photo_edit_prompt(t)
    if photo_prompt is not None:
        _btn_photo_editor(chat_id, photo_prompt, from_user=from_user)
        return

    label_key = tl.strip()
    if label_key in BUTTON_LABELS:
        BUTTON_LABELS[label_key]()
        return

    # ══════════════════════════════════════════════════════════════════
    # 3. ВСЁ ОСТАЛЬНОЕ → GROK (основной AI-мозг)
    # ══════════════════════════════════════════════════════════════════
    _ask_grok_and_route(chat_id, t, from_user=from_user)


# ── Вспомогательные функции для кнопок ───────────────────────────────────────

def _btn_call(chat_id):
    bot.send_message(
        chat_id,
        "📞 *Звонок*\n\n"
        "На какой номер? Напиши в формате +380XXXXXXXXX.\n\n"
        "После номера можно:\n"
        "• написать текст — бот позвонит и озвучит его живее;\n"
        "• написать *диалог* — ИИ попробует поговорить с человеком.",
        parse_mode="Markdown",
    )
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

def _jarvis_system_status(chat_id: int) -> str:
    """Генерирует компактный статус-блок всех подсистем."""
    now = _now_local()
    tz = _ukraine_tz_hours()
    lines = []

    # ИИ backend
    backend = _chat_ai_backend_label(chat_id)
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
    tw_from_ok = bool(os.environ.get("TWILIO_FROM_NUMBER") or os.environ.get("TWILIO_PHONE_NUMBER"))
    tw_ok = bool(os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN") and tw_from_ok)
    tx_ok = all(os.environ.get(k) for k in ("TELNYX_API_KEY", "TELNYX_FROM_NUMBER"))
    calls_str = " + ".join(filter(None, [
        "Twilio" if tw_ok else None,
        "Telnyx" if tx_ok else None,
    ]))
    lines.append(f"📞 Звонки: {('✅ ' + calls_str) if calls_str else '⚠️ не настроены'}")
    if tw_ok:
        lines.append(f"🎙️ ИИ-звонок с диалогом: {'✅ готов' if voice_call_available() else '⚠️ нужен PUBLIC_BASE_URL / Public Networking'}")

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

    status = _jarvis_system_status(chat_id)
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
    """Короткий статус оставленных функций."""
    lines = ["🩺 *Статус бота*", "━━━━━━━━━━━━━━━━━━━━━━"]
    lines.append("✅ Бот онлайн 24/7 на Railway")
    try:
        lines.append(f"🔍 Sauron: {sauron.status()}")
    except Exception:
        lines.append("🔍 Sauron: ⚠️ ошибка модуля")
    active_ai = _chat_ai_backend_label(chat_id)
    lines.append(f"🧠 ИИ-ответы: {active_ai}")
    lines.append(f"🖼 Фоторедактор: {'✅ готов' if photo_editor.image_editor_available(CONFIG.openai_direct_api_key) else '⚠️ нужен OPENAI_API_KEY'}")
    calls_ok = any(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TELNYX_API_KEY"))
    lines.append(f"📞 Звонки: {'✅ настроены' if calls_ok else '⚠️ не настроены'}")
    if os.environ.get("TWILIO_ACCOUNT_SID"):
        lines.append(f"🎙️ Живой ИИ-звонок: {'✅ готов' if voice_call_available() else '⚠️ нужен PUBLIC_BASE_URL'}")
    lines.append("💬 Умный режим: ✅ пиши обычным текстом")
    safe_send(chat_id, "\n".join(lines), main_menu())


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
    image_ok = photo_editor.image_editor_available(CONFIG.openai_direct_api_key)
    lines.append(f"  {'✅' if image_ok else '⚠️'} Фоторедактор по команде /photo{' ' if image_ok else ' — нужен OPENAI_API_KEY'}")
    lines.append("  ✅ Доступ к фоторедактору: Руслан и @skyyylit")

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
    tw_from = bool(os.environ.get("TWILIO_FROM_NUMBER") or os.environ.get("TWILIO_PHONE_NUMBER"))
    tw = bool(os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN") and tw_from)
    tx = all(os.environ.get(k) for k in ("TELNYX_API_KEY", "TELNYX_FROM_NUMBER"))
    if tw or tx:
        prov = "/".join(filter(None, ["Twilio" if tw else None, "Telnyx" if tx else None]))
        lines.append(f"  ✅ Звонки через {prov} — живой голос + подтверждение")
        if tw:
            lines.append(f"  {'✅' if voice_call_available() else '⚠️'} ИИ может общаться в звонке{' ' if voice_call_available() else ' — нужен PUBLIC_BASE_URL/Public Networking'}")
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
        "Kryven":  "kryven-flash ≈ $0.50/1M входящих · $4/1M исходящих",
        "kryven":  "kryven-flash ≈ $0.50/1M входящих · $4/1M исходящих",
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
        chat_summary = get_chat_summary(chat_id)
        if chat_summary:
            text += "\n\n🗂️ *Краткая память нашего диалога:*\n\n" + chat_summary
        if not text or not text.strip():
            text = "💾 Память пуста. Скажи «запомни что...» чтобы добавить факт."
        safe_send(chat_id, text, markup)
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Ошибка чтения памяти: {e}", reply_markup=markup)


def _btn_users(chat_id: int):
    """Показывает владельцу список пользователей и последнюю активность."""
    if chat_id != OWNER_ID:
        bot.send_message(chat_id, "🔒 Список пользователей доступен только Руслану.", reply_markup=main_menu())
        return
    safe_send(chat_id, _format_users_report(), main_menu(chat_id))


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
        lines.append("  → Добавь `ANYDESK_ID` в Railway Variables")
    if tv_id:
        lines.append(f"🟢 TeamViewer ID: `{tv_id}`")
    else:
        lines.append("⚠️ TeamViewer ID: не сохранён")
        lines.append("  → Добавь `TEAMVIEWER_ID` в Railway Variables")
    if pc_name:
        lines.append(f"\n🖥 ПК: {pc_name}")
    lines.append(
        "\n🔒 _ID хранятся в Railway Variables — бот не просит пароли._\n"
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
        text += "_Ссылка из Railway Variable `CRD_SHARE_URL`. Обновляй при каждой новой сессии._\n\n"
    else:
        text += "⚠️ Ссылка не сохранена. Добавь `CRD_SHARE_URL` в Railway Variables после создания сессии.\n\n"
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
            "3. Сохрани ID: добавь `ANYDESK_ID=123456789` в Railway Variables\n"
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
            "4. Сохрани ID: добавь `TEAMVIEWER_ID=987654321` в Railway Variables\n\n"
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
            "2. Скопируй код → сохрани в Railway Variables как `CRD_SHARE_URL`\n\n"
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
            "Для голосовых ответов добавь в Railway Variables:\n"
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
            "⚠️ Не настроено. Добавь в Railway Variables хотя бы одно:\n\n"
            "*Вариант 1 — API-ключ (рекомендуется):*\n"
            "• `SAURON_API_KEY` — ключ от sauron.info\n\n"
            "*Вариант 2 — логин/пароль:*\n"
            "• `SAURON_USERNAME` — твой логин\n"
            "• `SAURON_PASSWORD` — твой пароль\n\n"
            "После добавления перезапусти бота.\n"
            "_Ключи и пароли в Telegram не присылай — только через Railway Variables._",
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
            "Добавь в Railway Variables:\n"
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
    clear_chat_summary(chat_id)
    _save_grok_history()
    bot.send_message(chat_id, "🗑️ Готово — забыл историю и краткую память диалога. Начинаем с нуля!", reply_markup=main_menu())


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
    _track_user(message, "command:/forget")
    if not _message_is_allowed(message):
        return
    grok_history.pop(chat_id, None)
    clear_chat_summary(chat_id)
    _save_grok_history()
    bot.send_message(chat_id, "🗑️ История и краткая память диалога очищены. Начинаем с чистого листа!", reply_markup=main_menu())


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
            logger.info("FOP tax calendar seeded: %s new reminders.", created)
    except Exception as e:
        logger.exception("FOP tax calendar seeding failed: %s", e)


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
    if not _message_is_allowed(message):
        return
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
CODE_DENYLIST = {
    "whitelist.json",
    "known_users.json",
    "user_profiles.json",
    "pending_user_messages.json",
    "start_events.json",
    "memory.json",
}  # содержат приватные данные

def _can_access_code(chat_id: int | None) -> bool:
    try:
        actor_id = int(chat_id or 0)
    except Exception:
        return False
    return actor_id == OWNER_ID or get_role(actor_id) == "developer"

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
    if not _can_access_code(chat_id):
        bot.send_message(chat_id, "🔒 Доступ к коду есть только у Руслана и роли developer.")
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
    if not _can_access_code(chat_id):
        bot.send_message(chat_id, "🔒 Доступ к коду есть только у Руслана и роли developer.")
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
    if not _message_is_allowed(message):
        return
    _send_code_list(message.chat.id)

@bot.message_handler(commands=['code'])
def cmd_code(message):
    chat_id = message.chat.id
    if not _can_access_code(chat_id):
        bot.send_message(chat_id, "🔒 Доступ к коду есть только у Руслана и роли developer.")
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        _send_code_list(chat_id)
        return
    _send_code_file(chat_id, parts[1].strip())


@bot.message_handler(commands=['kryven', 'kryven_on'])
def cmd_kryven(message):
    chat_id = message.chat.id
    _track_user(message, "command:/kryven")
    if not _message_is_allowed(message):
        return
    if not kryven_available():
        bot.send_message(
            chat_id,
            "❌ Kryven пока не настроен: нужен KRYVEN_API_KEY в Railway Variables.",
            reply_markup=main_menu(chat_id),
        )
        return
    _set_chat_ai_backend(chat_id, "kryven")
    bot.send_message(
        chat_id,
        "🧠 Kryven включён. Следующие обычные ответы пойдут через Kryven.\n\n"
        "Вернуть обычный режим: /openai",
        reply_markup=main_menu(chat_id),
    )


@bot.message_handler(commands=['help', 'menu'])
def cmd_help(message):
    chat_id = message.chat.id
    _track_user(message, "command:/help")
    if not _message_is_allowed(message):
        return
    _btn_skills(chat_id)


@bot.message_handler(commands=['status'])
def cmd_status(message):
    chat_id = message.chat.id
    _track_user(message, "command:/status")
    if not _message_is_allowed(message):
        return
    _btn_status(chat_id)


@bot.message_handler(commands=['call'])
def cmd_call(message):
    chat_id = message.chat.id
    _track_user(message, "command:/call")
    if not _message_is_allowed(message):
        return
    _btn_call(chat_id)


@bot.message_handler(commands=['tasks'])
def cmd_tasks(message):
    chat_id = message.chat.id
    _track_user(message, "command:/tasks")
    if not _message_is_allowed(message):
        return
    if _chat_is_group(message):
        safe_send(chat_id, tasks_report(chat_id))
        return
    _btn_tasks(chat_id)


@bot.message_handler(commands=['group_on', 'group_off', 'group_tasks', 'group_access', 'group_status'])
def cmd_work_group(message):
    chat_id = message.chat.id
    _track_user(message, "command:/work_group")
    if not _chat_is_group(message):
        bot.send_message(chat_id, "Эти команды работают внутри рабочей группы.")
        return

    text = message.text or ""
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    args = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    user_id = int(getattr(message.from_user, "id", 0) or 0)

    if command != "/group_on" and not group_enabled(chat_id):
        if user_id == OWNER_ID and command == "/group_status":
            bot.send_message(chat_id, "Group bot: disabled")
        return

    if command == "/group_tasks":
        safe_send(chat_id, tasks_report(chat_id))
        return

    if user_id != OWNER_ID:
        bot.send_message(chat_id, "Эту настройку может менять только Руслан.")
        return

    if command == "/group_on":
        set_group_enabled(chat_id, True)
        bot.send_message(chat_id, "✅ Память рабочей группы включена.")
        return
    if command == "/group_off":
        set_group_enabled(chat_id, False)
        bot.send_message(chat_id, "✅ Память рабочей группы выключена.")
        return
    if command == "/group_status":
        enabled = "включена" if group_enabled(chat_id) else "выключена"
        recipients = report_recipients(chat_id, OWNER_ID)
        bot.send_message(chat_id, f"Память группы: {enabled}\nПолучателей аудиоотчётов: {len(recipients)}")
        return
    if command == "/group_access":
        if not args:
            add_report_recipient(chat_id, user_id)
            bot.send_message(chat_id, "✅ Добавил тебя в получатели аудиоотчётов этой группы.")
            return
        username = args.strip().lstrip("@").split()[0].lower()
        target_chat_id = known_users.get(username)
        if not target_chat_id:
            bot.send_message(chat_id, f"Не знаю @{username}. Пусть он напишет боту /start в личку, потом повтори команду.")
            return
        add_report_recipient(chat_id, target_chat_id)
        bot.send_message(chat_id, f"✅ @{username} добавлен в получатели аудиоотчётов этой группы.")
        return


@bot.message_handler(commands=['sauron'])
def cmd_sauron(message):
    chat_id = message.chat.id
    _track_user(message, "command:/sauron")
    if not _message_is_allowed(message):
        return
    _btn_sauron_search(chat_id)


@bot.message_handler(commands=['file_sauron', 'filesauron'])
def cmd_file_sauron(message):
    chat_id = message.chat.id
    _track_user(message, "command:/file_sauron")
    if not _message_is_allowed(message):
        return
    _btn_file_sauron(chat_id)


@bot.message_handler(commands=['photo', 'foto', 'editphoto', 'image'])
def cmd_photo_editor(message):
    chat_id = message.chat.id
    _track_user(message, "command:/photo")
    if not _message_is_allowed(message):
        return
    text = message.text or ""
    prompt = _extract_photo_edit_prompt(text, BOT_USERNAME) or PHOTO_EDIT_DEFAULT_PROMPT
    _btn_photo_editor(chat_id, prompt, from_user=message.from_user)


@bot.message_handler(commands=['openai', 'chatgpt', 'kryven_off'])
def cmd_openai_backend(message):
    chat_id = message.chat.id
    _track_user(message, "command:/openai")
    if not _message_is_allowed(message):
        return
    _set_chat_ai_backend(chat_id, "default")
    bot.send_message(
        chat_id,
        "✅ Обычный ИИ-режим включён. Следующие ответы снова пойдут через основной backend.",
        reply_markup=main_menu(chat_id),
    )


@bot.message_handler(commands=['ai_mode', 'aimode'])
def cmd_ai_mode(message):
    chat_id = message.chat.id
    _track_user(message, "command:/ai_mode")
    if not _message_is_allowed(message):
        return
    active = _chat_ai_backend_label(chat_id)
    kryven_state = "✅ настроен" if kryven_available() else "⚠️ нет KRYVEN_API_KEY"
    bot.send_message(
        chat_id,
        "🧠 *Режим ИИ*\n\n"
        f"Сейчас: *{active}*\n"
        f"Kryven: {kryven_state}\n\n"
        "/kryven — отвечать через Kryven\n"
        "/openai — вернуть обычный режим",
        parse_mode="Markdown",
        reply_markup=main_menu(chat_id),
    )


@bot.message_handler(commands=['memory'])
def cmd_memory(message):
    chat_id = message.chat.id
    _track_user(message, "command:/memory")
    if chat_id != OWNER_ID:
        return
    args = message.text.strip().split(maxsplit=1)
    # /memory clear — очистить память
    if len(args) > 1 and args[1].strip().lower() in ("clear", "очистить", "сбросить"):
        clear_memory()
        clear_chat_summary(chat_id)
        grok_history.pop(chat_id, None)
        _save_grok_history()
        bot.send_message(chat_id, "🗑️ Долгосрочная память и история диалога очищены.", reply_markup=main_menu())
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
    text = format_for_display()
    chat_summary = get_chat_summary(chat_id)
    if chat_summary:
        text += "\n\n🗂️ *Краткая память нашего диалога:*\n\n" + chat_summary
    safe_send(chat_id, text, main_menu())


@bot.message_handler(commands=['users', 'userlist'])
def cmd_users(message):
    chat_id = message.chat.id
    _track_user(message, "command:/users")
    if chat_id != OWNER_ID:
        return
    _btn_users(chat_id)


@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    _track_user(message, "command:/start")
    if _chat_is_group(message):
        return
    auto_role = None
    if message.from_user and message.from_user.username:
        username = message.from_user.username.lower()
        known_users[username] = chat_id
        _save_known_users()
        logger.info("Known user saved: @%s -> %s", message.from_user.username, chat_id)
        auto_role = _apply_pending_username_access(username, chat_id)
    if not is_allowed(chat_id):
        bot.send_message(chat_id, "🔒 Нет доступа. Попроси Руслана добавить твой Telegram ID в whitelist.",
                         reply_markup=types.ReplyKeyboardRemove())
        return
    role = auto_role or get_role(chat_id)
    _record_start_event(message, role)
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
            reply_markup=main_menu(chat_id)
        )
    elif role == "developer":
        bot.send_message(
            chat_id,
            "👋 Привет. У тебя роль разработчика: можешь смотреть файлы через /files и присылать правки файлом.",
            reply_markup=developer_menu(),
        )
    elif role == "worker":
        bot.send_message(chat_id, "👋 Привет! Можешь написать Руслану или просто задать вопрос.", reply_markup=worker_menu())
    else:
        bot.send_message(chat_id, "👋 Привет! Пиши обычным текстом или выбери кнопку ниже.", reply_markup=main_menu(chat_id))


@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    chat_id = message.chat.id
    _track_user(message, "voice")
    if not _message_is_allowed(message):
        return

    # Голос работает ТОЛЬКО через прямой OPENAI_API_KEY — Replit proxy не поддерживает audio.
    if not voice_openai_client:
        markup = jarvis_menu() if chat_id in jarvis_mode else main_menu()
        bot.send_message(
            chat_id,
            "🎤 Голос пока не включён: нужно добавить OPENAI_API_KEY в Railway Variables "
            "и перезапустить бота.\n\n"
            "Ключи и пароли в Telegram не присылай — только через Railway Variables.",
            reply_markup=markup,
        )
        return

    msg = bot.send_message(chat_id, "🎤 Слушаю…")
    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)
        voice_duration = int(getattr(message.voice, "duration", 0) or 0)

        if chat_id != OWNER_ID:
            filename = f"voice_{getattr(message, 'message_id', int(time.time()))}.ogg"
            if _chat_is_group(message):
                if voice_duration < 25 and not should_answer_in_group(message, BOT_USERNAME):
                    # Короткие голосовые в группе считаем обычной рабочей репликой.
                    pass
                else:
                    remember_audio_report(chat_id, _chat_title(message), message.from_user, filename, _fmt_audio_time(voice_duration))
                    _analyze_dialog_audio(chat_id, downloaded, filename, msg.message_id)
                    return
            else:
                _analyze_dialog_audio(chat_id, downloaded, filename, msg.message_id)
                return

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

        if _chat_is_group(message):
            remember_message(
                chat_id,
                _chat_title(message),
                message.from_user,
                f"Голосовое сообщение: {text}",
                kind="voice",
                message_id=getattr(message, "message_id", 0),
            )
            if not should_answer_in_group(message, BOT_USERNAME):
                bot.edit_message_text("🎙️ Голосовое расшифровано и добавлено в память группы.", chat_id, msg.message_id)
                return
            _ask_grok_and_route(chat_id, text, extra_memory=recent_context(chat_id), from_user=message.from_user)
            return

        voice_request_chats.add(chat_id)
        try:
            process_text(chat_id, text, from_user=message.from_user)
        finally:
            voice_request_chats.discard(chat_id)

    except Exception as e:
        err_str = str(e)
        logger.exception("Voice processing failed: %s", err_str)
        if "timeout" in err_str.lower():
            hint = "Сервер не ответил вовремя — попробуй ещё раз."
        elif "rate" in err_str.lower():
            hint = "Слишком много запросов — подожди секунду."
        elif "auth" in err_str.lower() or "401" in err_str or "403" in err_str:
            hint = "Неверный OPENAI_API_KEY — проверь ключ в Railway Variables."
        else:
            hint = "Попробуй ещё раз или напиши текстом."
        try:
            bot.edit_message_text(f"⚠️ {hint}", chat_id, msg.message_id)
        except Exception:
            bot.send_message(chat_id, f"⚠️ {hint}")


@bot.message_handler(content_types=['audio'])
def handle_audio(message):
    """Принимает аудиофайлы (MP3 и др.) и анализирует диалог."""
    chat_id = message.chat.id
    _track_user(message, "audio")
    if not _message_is_allowed(message):
        return

    audio = message.audio
    filename = (audio.file_name or f"audio.mp3").strip()
    if _chat_is_group(message):
        remember_audio_report(chat_id, _chat_title(message), message.from_user, filename, "")

    msg_dl = bot.send_message(chat_id, _dialog_progress_text(filename, 5, "📥 Получаю файл из Telegram..."))
    try:
        _set_dialog_progress(chat_id, msg_dl.message_id, filename, 10, "📥 Скачиваю файл...")
        file_info = bot.get_file(audio.file_id)
        audio_bytes = bot.download_file(file_info.file_path)
        size_mb = len(audio_bytes) / (1024 * 1024)
        _set_dialog_progress(chat_id, msg_dl.message_id, filename, 20, "✅ Файл скачан.", f"Размер: {size_mb:.1f} МБ")
    except Exception as e:
        bot.edit_message_text(f"❌ Не смог скачать файл: {e}", chat_id, msg_dl.message_id)
        return

    _analyze_dialog_audio(chat_id, audio_bytes, filename, msg_dl.message_id)


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    _track_user(message, "photo")
    if not _message_is_allowed(message):
        return

    actor_id = _actor_id_for_request(chat_id, message.from_user)
    pending = waiting_for_photo_edit.get(chat_id)
    pending_for_actor = bool(pending and int(pending.get("actor_id") or 0) == int(actor_id))
    caption = message.caption or ""
    caption_prompt = _extract_photo_edit_prompt(caption, BOT_USERNAME)

    if _chat_is_group(message):
        if group_enabled(chat_id):
            remember_message(
                chat_id,
                _chat_title(message),
                message.from_user,
                f"Отправлено фото. Подпись: {caption}" if caption else "Отправлено фото.",
                kind="photo",
                message_id=getattr(message, "message_id", 0),
            )
        if not pending_for_actor and not should_answer_in_group(message, BOT_USERNAME):
            return

    if caption_prompt is None and not pending_for_actor:
        return

    prompt = (caption_prompt or str(pending.get("prompt") or PHOTO_EDIT_DEFAULT_PROMPT)).strip()
    if pending_for_actor:
        waiting_for_photo_edit.pop(chat_id, None)

    if not _photo_editor_allowed(chat_id, message.from_user):
        bot.send_message(chat_id, "🖼 Фоторедактор пока доступен только Руслану и @skyyylit.")
        return
    if not photo_editor.image_editor_available(CONFIG.openai_direct_api_key):
        bot.send_message(chat_id, "⚠️ Фоторедактору нужен прямой OPENAI_API_KEY в Railway Variables.")
        return

    progress = bot.send_message(chat_id, "🖼 Принял фото. Редактирую, подожди немного…")
    try:
        bot.send_chat_action(chat_id, "upload_photo")
        source_photo = message.photo[-1]
        file_info = bot.get_file(source_photo.file_id)
        image_bytes = bot.download_file(file_info.file_path)
        edited = photo_editor.edit_photo(
            image_bytes,
            prompt,
            api_key=CONFIG.openai_direct_api_key,
        )
        output = io.BytesIO(edited)
        output.name = "edited_photo.png"
        try:
            bot.delete_message(chat_id, progress.message_id)
        except Exception:
            pass
        try:
            bot.send_photo(chat_id, output, caption=f"Готово: {prompt[:180]}", reply_markup=_reply_markup_for_chat(chat_id))
        except Exception:
            output.seek(0)
            bot.send_document(chat_id, output, caption=f"Готово: {prompt[:180]}", reply_markup=_reply_markup_for_chat(chat_id))
    except Exception as e:
        logger.exception("Photo edit failed: %s", e)
        hint = str(e)
        if "OPENAI_API_KEY" in hint:
            hint = "Нужен прямой OPENAI_API_KEY в Railway Variables."
        elif "timeout" in hint.lower() or "долго" in hint.lower():
            hint = "OpenAI долго не отвечает. Попробуй ещё раз или фото попроще."
        elif "401" in hint or "auth" in hint.lower() or "api key" in hint.lower():
            hint = "Проверь OPENAI_API_KEY в Railway Variables."
        try:
            bot.edit_message_text(f"❌ Не смог отредактировать фото: {hint[:500]}", chat_id, progress.message_id)
        except Exception:
            bot.send_message(chat_id, f"❌ Не смог отредактировать фото: {hint[:500]}")


def _run_file_sauron_worker(chat_id: int, doc, filename: str):
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
        found_cnt = sum(1 for p in persons if p.found)
        rel_cnt   = len(relatives)
        caption_base = (
            f"✅ Найдено: {found_cnt} / {len(persons)} чел.\n"
            f"👨‍👩‍👧 Родственников: {rel_cnt}"
            + (f"\n⚠️ Пропущено: {skipped}" if skipped else "")
        )

        # ── Единственный результат: FINAL_MERGED.xlsx ─────────────────────
        xlsx_bytes = sauron_file_search.build_final_merged_xlsx(
            persons, relatives, phone_checks,
        )
        if xlsx_bytes:
            xlsx_bio = io.BytesIO(xlsx_bytes)
            xlsx_bio.name = "FINAL_MERGED.xlsx"
            bot.send_document(
                chat_id, xlsx_bio,
                caption=(
                    "📊 FINAL_MERGED.xlsx — итоговый отчёт\n"
                    "(1 строка = 1 родственник)\n" + caption_base
                ),
                reply_markup=markup,
            )
        else:
            bot.send_message(
                chat_id,
                "⚠️ Не удалось собрать XLSX (нет openpyxl).",
                reply_markup=markup,
            )
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Отчёт не отправлен: {str(e)[:100]}", reply_markup=markup)


def _run_file_sauron(chat_id: int, doc, filename: str):
    """Запускает файловый Sauron в фоне, чтобы webhook не обрывался на долгом поиске."""
    thread = threading.Thread(
        target=_run_file_sauron_worker,
        args=(chat_id, doc, filename),
        daemon=True,
        name=f"file-sauron-{chat_id}",
    )
    thread.start()


def make_sms_link(phone: str, text: str) -> str:
    """Создать https ссылку на страницу-редирект которая откроет SMS приложение"""
    domain = os.environ.get("REPLIT_DEV_DOMAIN", "localhost")
    clean_phone = phone.replace(" ", "").replace("-", "")
    return f"https://{domain}/api/sms?to={quote(clean_phone)}&body={quote(text)}"


def build_location_markup(lat, lon, is_live=False):
    maps_link = f"https://maps.google.com/?q={lat},{lon}"
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("🗺️ Открыть в Google картах", url=maps_link))
    return markup, maps_link


@bot.message_handler(content_types=['location'])
def handle_location(message):
    chat_id = message.chat.id
    _track_user(message, "location")
    if not _message_is_allowed(message):
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
    _track_user(message, "live_location")
    if not _message_is_allowed(message):
        return
    if message.location is None:
        return

    lat = message.location.latitude
    lon = message.location.longitude
    last_location[chat_id] = (lat, lon)

    # Тихо обновляем координаты, без спама сообщениями
    logger.info("Live location update from %s: %s, %s", chat_id, lat, lon)


@bot.callback_query_handler(func=lambda call: call.data == "send_geo_toha")
def callback_send_geo(call):
    _track_user(call.message, "callback:send_geo_toha", from_user=call.from_user)
    bot.answer_callback_query(call.id, "Раздел Тоха отключён.")
    bot.send_message(call.message.chat.id, "🚕 Раздел Тоха отключён.", reply_markup=main_menu())


def process_worker(chat_id: int, text: str, from_user=None):
    """Обработка команд для рабочего — аналитика + звонок Руслану"""
    raw_text = str(text or "").strip()
    t = raw_text.lower()
    # ── Ожидаем текст для звонка ──────────────────────
    if chat_id in waiting_for_call_msg:
        waiting_for_call_msg.pop(chat_id)
        owner_phone = os.environ.get("RUSLAN_PHONE_NUMBER", "")
        if not owner_phone:
            bot.send_message(chat_id, "⚠️ Номер Руслана не настроен. Обратись напрямую.", reply_markup=worker_menu())
            return
        display_name = _display_name_for_chat_id(chat_id)
        say_text = f"Привет Руслан, твой рабочий {display_name} говорит: {text}"
        bot.send_message(chat_id, "📞 Звоню Руслану...")
        ok, info = make_call(owner_phone, say_text)
        if ok:
            bot.send_message(chat_id, "✅ Позвонил! Руслан услышит твоё сообщение.", reply_markup=worker_menu())
            # Уведомление Руслану в Telegram
            bot.send_message(OWNER_ID,
                             f"📞 *Рабочий {_md_escape(display_name)} звонит тебе!*\n\nСообщение: _{_md_escape(text)}_",
                             parse_mode="Markdown")
        else:
            bot.send_message(chat_id, f"❌ Не удалось позвонить: {info}", reply_markup=worker_menu())
        return
    # ── Кнопка звонка ────────────────────────────────
    if "позвонить руслану" in t or "📞" in t:
        bot.send_message(chat_id, "✍️ Напиши сообщение — я позвоню Руслану и скажу его голосом:")
        waiting_for_call_msg[chat_id] = True
        return
    # ── Таблицы отключены ─────────────────────────────
    if "аналитика" in t or "📊" in t or "статистика таблиц" in t or "сводк" in t or "мои таблицы" in t or "📋" in t:
        bot.send_message(chat_id, "📊 Раздел таблиц отключён.", reply_markup=worker_menu())
        return
    if chat_id in waiting_for_sheet_id and waiting_for_sheet_id[chat_id] == "analytics":
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
        if not raw_text:
            bot.send_message(chat_id, "Нажми кнопку 👇", reply_markup=worker_menu())
            return
        _ask_grok_and_route(
            chat_id,
            raw_text,
            extra_memory=(
                "Пользователь имеет роль worker. "
                "На обычные сообщения отвечай по-русски дерзко, живо, как умный кент, без стерильного тона. "
                "Можно использовать мат и грубоватые дружеские подколы, если это уместно. "
                "Не отправляй его к кнопкам, если он задал обычный вопрос. "
                "Если он явно хочет связаться с Русланом, подскажи кнопку 'Позвонить Руслану'."
            ),
            from_user=from_user,
        )


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
    _track_user(message, "text")
    text = message.text or ""

    if _chat_is_group(message):
        group_cmd = (text or "").strip().lower()
        if group_cmd in (
            "выключи чат в группе",
            "выключи бота в группе",
            "убери бота с группы",
            "убери бота из группы",
            "бот молчи",
            "бот не отвечай",
        ):
            if getattr(message.from_user, "id", None) == OWNER_ID:
                set_group_enabled(chat_id, False)
                bot.send_message(chat_id, "✅ Бот выключен в этой группе. Включить обратно: /group_on")
            return
        if not group_enabled(chat_id):
            return
        if group_enabled(chat_id):
            remember_message(
                chat_id,
                _chat_title(message),
                message.from_user,
                text,
                kind="text",
                message_id=getattr(message, "message_id", 0),
            )
        if not should_answer_in_group(message, BOT_USERNAME):
            return
        group_question = strip_bot_mention(text, BOT_USERNAME)
        if not group_question:
            group_question = "Ответь по контексту этой рабочей группы."
        direct_sauron_query = _extract_direct_sauron_query(group_question)
        if direct_sauron_query is not None:
            _run_sauron_search(chat_id, direct_sauron_query, reply_markup=None)
            return
        _ask_grok_and_route(chat_id, group_question, extra_memory=recent_context(chat_id), from_user=message.from_user)
        return

    if not is_allowed(chat_id):
        bot.send_message(chat_id, "🔒 Нет доступа. Попроси Руслана добавить твой Telegram ID в whitelist.")
        return
    direct_sauron_query = _extract_direct_sauron_query(text)
    if direct_sauron_query is not None:
        _run_sauron_search(chat_id, direct_sauron_query)
        return
    role = get_role(chat_id)
    if role == "worker":
        process_worker(chat_id, text, from_user=message.from_user)
    else:
        process_text(chat_id, text, from_user=message.from_user)


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
    • py / json / md  → загрузка кода (Руслан или developer)
    """
    chat_id = message.chat.id
    _track_user(message, "document")
    if not _message_is_allowed(message):
        return

    doc      = message.document
    filename = (doc.file_name or "file").strip()
    ext      = os.path.splitext(filename)[1].lower()

    # ── Роутинг: аудиофайлы → анализ диалога ─────────────────────────────
    if _is_audio_doc(filename, getattr(doc, 'mime_type', None)):
        if _chat_is_group(message):
            remember_audio_report(chat_id, _chat_title(message), message.from_user, filename, "")
        msg_dl = bot.send_message(chat_id, _dialog_progress_text(filename, 5, "📥 Получаю файл из Telegram..."))
        try:
            _set_dialog_progress(chat_id, msg_dl.message_id, filename, 10, "📥 Скачиваю файл...")
            file_info = bot.get_file(doc.file_id)
            audio_bytes = bot.download_file(file_info.file_path)
            size_mb = len(audio_bytes) / (1024 * 1024)
            _set_dialog_progress(chat_id, msg_dl.message_id, filename, 20, "✅ Файл скачан.", f"Размер: {size_mb:.1f} МБ")
        except Exception as e:
            bot.edit_message_text(f"❌ Не смог скачать файл: {e}", chat_id, msg_dl.message_id)
            return
        _analyze_dialog_audio(chat_id, audio_bytes, filename, msg_dl.message_id)
        return

    # ── Роутинг: Sauron ───────────────────────────────────────────────────
    # .txt включён: список ФИО чаще всего приходит именно в txt/csv/xlsx
    SAURON_EXTS = {'.csv', '.xlsx', '.xls', '.docx', '.pdf', '.txt'}
    is_sauron_mode = chat_id in waiting_for_file_sauron
    is_sauron_ext  = ext in SAURON_EXTS
    if is_sauron_ext or is_sauron_mode:
        waiting_for_file_sauron.discard(chat_id)
        _run_file_sauron(chat_id, doc, filename)
        return

    # ── Роутинг: загрузка кода (Руслан или developer) ────────────────────
    if not _can_access_code(chat_id):
        # Нет технического доступа, не Sauron-файл — молча игнорируем
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
    _track_user(call.message, f"callback:{call.data}", from_user=call.from_user)
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

MORNING_BRIEFING_HOUR = CONFIG.morning_briefing_hour   # 08:00 по местному времени
MORNING_BRIEFING_FILE = CONFIG.morning_briefing_file

_morning_lock = threading.Lock()


def _load_last_briefing_date() -> str:
    data = read_json_file(MORNING_BRIEFING_FILE, {}, logger=logger)
    return str(data.get("last_date", "")) if isinstance(data, dict) else ""


def _save_last_briefing_date(date_str: str):
    write_json_file(MORNING_BRIEFING_FILE, {"last_date": date_str}, logger=logger)


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
        logger.exception("Morning briefing failed: %s", e)


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
        logger.exception("Sheet monitoring failed: %s", e)
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
            logger.exception("Cannot send sheet monitoring alert: %s", e)
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
        logger.exception("Cannot send grouped sheet monitoring alert: %s", e)


def _scheduler_loop():
    """Фоновый поток: проверяет напоминания каждую минуту, отправляет утреннюю сводку в 8:00."""
    logger.info("Reminder scheduler started.")
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
                    logger.exception("Reminder send failed: %s (%s)", reminder_id, e)
                    # Увеличиваем счётчик ошибок; после MAX_FAILURES — деактивируем
                    mark_failed(reminder_id)

        except Exception as e:
            logger.exception("Scheduler loop failed: %s", e)

        time.sleep(60)


if __name__ == "__main__":
    keep_alive()
    _sync_bot_commands()
    # Запускаем планировщик напоминаний в фоновом потоке
    scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="reminder-scheduler")
    scheduler_thread.start()
    # Засеваем налоговый календарь ФОП — идемпотентно, дубли не создаст
    _seed_tax_reminders_safe()
    logger.info("Ruslan Personal Helper started.")

    _mode_label = "PRODUCTION 🟢 (24/7)" if IS_PRODUCTION else "DEVELOPMENT 🟡 (только пока открыт Replit)"
    public_base = _public_base_url()
    force_polling = _env_flag("TELEGRAM_FORCE_POLLING", False)

    if public_base and not force_polling:
        webhook_secret = (
            os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
            or hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:48]
        )
        configure_telegram_webhook(bot, webhook_secret)
        webhook_url = f"{public_base}/api/telegram/webhook/{webhook_secret}"

        try:
            bot.remove_webhook()
            time.sleep(0.5)
            try:
                bot.set_webhook(url=webhook_url, drop_pending_updates=False)
            except TypeError:
                bot.set_webhook(url=webhook_url)
            logger.info("Telegram webhook ready: %s/api/telegram/webhook/<secret>", public_base)
            logger.info("Webhook start | mode: %s | %s Kyiv", _mode_label, _now_local().strftime("%Y-%m-%d %H:%M"))
        except Exception as _we:
            logger.exception("Webhook setup failed, falling back to polling: %s", _we)
            public_base = ""

        if public_base:
            while True:
                time.sleep(3600)

    # Fallback: local/dev mode without a public URL.
    # Railway should normally use webhook mode to avoid Telegram 409 polling conflicts.
    try:
        bot.remove_webhook()
        logger.info("Webhook removed, polling is ready.")
    except Exception as _we:
        logger.warning("Webhook removal failed, continuing: %s", _we)

    logger.info("Polling start | mode: %s | %s Kyiv", _mode_label, _now_local().strftime("%Y-%m-%d %H:%M"))

    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            err = str(e)
            if "409" in err or "Conflict" in err:
                logger.error(
                    "409 CONFLICT: bot is already running in another place. "
                    "Only one active polling instance is allowed. Retry in 30 seconds."
                )
                time.sleep(30)
            else:
                logger.exception("Polling failed, restart in 5 seconds: %s", e)
                time.sleep(5)
