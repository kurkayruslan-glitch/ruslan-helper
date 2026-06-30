import os
import time
import json
import re
import telebot
from flask import Flask, request, Response
from threading import Thread

from calls import _escape_xml, _say_xml, _call_language
from logging_setup import setup_logging

app = Flask(__name__)
logger = setup_logging("ruslan-helper.keep_alive")

_START_TIME = time.time()
_VOICE_CALL_SESSIONS = {}
_TELEGRAM_BOT = None
_TELEGRAM_WEBHOOK_SECRET = ""


def configure_telegram_webhook(bot, secret: str):
    """Attach the Telegram bot instance to the Flask webhook route."""
    global _TELEGRAM_BOT, _TELEGRAM_WEBHOOK_SECRET
    _TELEGRAM_BOT = bot
    _TELEGRAM_WEBHOOK_SECRET = (secret or "").strip()


@app.route('/')
def home():
    return "Ruslan Helper — Bot is alive!"


@app.route('/health')
def health():
    uptime = int(time.time() - _START_TIME)
    mode = os.environ.get("DEPLOYMENT_MODE", "development")
    data = {
        "status": "ok",
        "bot": "Ruslan Helper (@Ruslan_pomohnik_bot)",
        "mode": mode,
        "uptime_seconds": uptime,
        "uptime_human": _fmt_uptime(uptime),
        "voice_call": "ready",
        "telegram_updates": "webhook" if _TELEGRAM_BOT else "polling_or_not_configured",
        "note": (
            "PRODUCTION — работает 24/7 на Railway/Reserved VM"
            if mode == "production"
            else "DEVELOPMENT — остановится при закрытии браузера"
        ),
    }
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')


@app.route('/status')
def status():
    uptime = int(time.time() - _START_TIME)
    mode = os.environ.get("DEPLOYMENT_MODE", "development")
    lines = [
        f"✅ Ruslan Helper — работает",
        f"Режим: {'🚀 PRODUCTION (24/7)' if mode == 'production' else '⚠️ DEVELOPMENT'}",
        f"Аптайм: {_fmt_uptime(uptime)}",
        f"ИИ: {os.environ.get('LLM_BACKEND', 'openai')}",
        "Голосовые ИИ-звонки: endpoint готов",
    ]
    return Response("\n".join(lines), mimetype='text/plain; charset=utf-8')


@app.route('/api/twiml', methods=['GET', 'POST'])
def twiml():
    message = request.values.get('message', 'Привет, это сообщение от Руслана.')
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'{_say_xml(message, repeat=True)}\n'
        '</Response>'
    )
    return Response(xml, mimetype='text/xml; charset=utf-8')


@app.route('/api/telegram/webhook/<secret>', methods=['POST'])
def telegram_webhook(secret):
    """Receive Telegram updates on Railway so polling conflicts cannot silence the bot."""
    if not _TELEGRAM_WEBHOOK_SECRET or secret != _TELEGRAM_WEBHOOK_SECRET:
        return Response("forbidden", status=403, mimetype='text/plain')
    if _TELEGRAM_BOT is None:
        return Response("telegram bot is not configured", status=503, mimetype='text/plain')

    try:
        raw_body = request.get_data(cache=False).decode("utf-8")
        update = telebot.types.Update.de_json(raw_body)
        if update:
            _TELEGRAM_BOT.process_new_updates([update])
        return Response("ok", mimetype='text/plain')
    except Exception as e:
        logger.exception("Telegram webhook error: %s", e)
        return Response("error", status=500, mimetype='text/plain')


@app.route('/api/voice/start', methods=['GET', 'POST'])
def voice_start():
    """Старт интерактивного ИИ-звонка Twilio."""
    _cleanup_voice_sessions()
    call_sid = request.values.get("CallSid", f"dev-{int(time.time())}")
    goal = request.values.get("goal", "").strip() or "коротко поговорить с человеком по просьбе Руслана"
    goal = goal[:900]

    _VOICE_CALL_SESSIONS[call_sid] = {
        "goal": goal,
        "history": [],
        "turns": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    intro = (
        "Здравствуйте. Это голосовой помощник Руслана. "
        f"Звоню по вопросу: {goal}. "
        "Удобно сейчас коротко поговорить?"
    )
    return _voice_gather_response(call_sid, intro)


@app.route('/api/voice/respond', methods=['GET', 'POST'])
def voice_respond():
    """Следующая реплика ИИ после речи собеседника."""
    _cleanup_voice_sessions()
    call_sid = request.values.get("CallSid", "")
    speech = (request.values.get("SpeechResult", "") or "").strip()

    session = _VOICE_CALL_SESSIONS.get(call_sid)
    if not session:
        session = {
            "goal": "продолжить разговор по просьбе Руслана",
            "history": [],
            "turns": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        _VOICE_CALL_SESSIONS[call_sid] = session

    if not speech:
        session["turns"] += 1
        if session["turns"] >= 3:
            return _voice_say_response(
                "Не расслышал ответ. Тогда закончим звонок. Спасибо, до свидания.",
                hangup=True,
            )
        return _voice_gather_response(call_sid, "Не расслышал. Повторите, пожалуйста, коротко.")

    session["updated_at"] = time.time()
    session["turns"] = int(session.get("turns", 0)) + 1

    goal = session.get("goal", "")
    history = session.setdefault("history", [])
    reply = _ask_voice_ai(goal, speech, history)
    reply = _clean_voice_reply(reply)

    should_end = "[END_CALL]" in reply
    reply = reply.replace("[END_CALL]", "").strip()
    if not reply:
        reply = "Понял. Спасибо, я передам Руслану. До свидания."
        should_end = True

    history.append({"role": "user", "content": speech})
    history.append({"role": "assistant", "content": reply})
    session["history"] = history[-12:]

    max_turns = int(os.environ.get("VOICE_CALL_MAX_TURNS", "8"))
    if should_end or session["turns"] >= max_turns:
        return _voice_say_response(reply + " Спасибо, до свидания.", hangup=True)

    return _voice_gather_response(call_sid, reply)


def _ask_voice_ai(goal: str, speech: str, history: list) -> str:
    try:
        backend = os.environ.get("LLM_BACKEND", "openai").lower()
        if backend == "grok":
            from grok import ask_grok
        elif backend == "gemini":
            from gemini import ask_grok
        else:
            from chatgpt import ask_grok

        memory_block = (
            "Ты голосовой помощник Руслана в телефонном звонке. "
            "Ты не притворяешься человеком: если нужно, говоришь, что ты голосовой помощник. "
            f"Цель звонка: {goal}. "
            "Говори естественно, коротко, по телефону: максимум 1-2 предложения и один вопрос за раз. "
            "Не читай длинные списки. Не используй Markdown. "
            "Главная задача — спокойно продвинуть разговор к цели: уточнить нужное, ответить на возражение, "
            "зафиксировать итог или договориться о следующем шаге. "
            "Если нужны чувствительные данные, не выманивай их обманом: предложи официальный канал, личную сверку "
            "или объясни цель и последствия отказа. "
            "Если человек просит закончить, злится, отказывается говорить или цель достигнута — заверши вежливо "
            "и добавь в конец [END_CALL]."
        )
        return ask_grok(speech, history, memory_block=memory_block)
    except Exception as e:
        logger.exception("Voice AI error: %s", e)
        return "Сейчас не получается обработать ответ. Я передам Руслану, что вы ответили. [END_CALL]"


def _clean_voice_reply(text: str) -> str:
    text = re.sub(r"\[ACTION:[^\]]*\]", "", str(text or ""))
    text = re.sub(r"[*_`#>~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 650:
        text = text[:650].rsplit(" ", 1)[0] + "."
    return text


def _voice_gather_response(call_sid: str, text: str) -> Response:
    base = _request_base_url()
    action = f"{base}/api/voice/respond"
    language = _call_language()
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'  <Gather input="speech" action="{_escape_xml(action)}" method="POST" '
        f'language="{_escape_xml(language)}" speechTimeout="auto" timeout="6">\n'
        f'{_say_xml(text, repeat=False)}\n'
        '  </Gather>\n'
        f'{_say_xml("Не расслышал ответ. Повторите, пожалуйста.", repeat=False)}\n'
        f'  <Redirect method="POST">{_escape_xml(action)}</Redirect>\n'
        '</Response>'
    )
    return Response(xml, mimetype='text/xml; charset=utf-8')


def _voice_say_response(text: str, hangup: bool = False) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'{_say_xml(text, repeat=False)}\n'
        f'{"<Hangup/>" if hangup else ""}\n'
        '</Response>'
    )
    return Response(xml, mimetype='text/xml; charset=utf-8')


def _request_base_url() -> str:
    env_url = (
        os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("TWILIO_PUBLIC_BASE_URL")
        or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        or ""
    ).strip()
    if env_url:
        if not env_url.startswith(("http://", "https://")):
            env_url = "https://" + env_url
        return env_url.rstrip("/")
    return request.url_root.rstrip("/")


def _cleanup_voice_sessions():
    ttl = int(os.environ.get("VOICE_CALL_SESSION_TTL_SECONDS", "3600"))
    now = time.time()
    for sid, session in list(_VOICE_CALL_SESSIONS.items()):
        if now - float(session.get("updated_at", session.get("created_at", now))) > ttl:
            _VOICE_CALL_SESSIONS.pop(sid, None)


def _fmt_uptime(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def run():
    # PORT — стандартная переменная Railway/Render/Heroku.
    # PORT_KEEPALIVE — для обратной совместимости с Replit.
    # Если ничего не задано — слушаем 8000.
    port = int(os.environ.get("PORT") or os.environ.get("PORT_KEEPALIVE") or 8000)
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
