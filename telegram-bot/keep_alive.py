import os
import time
import json
from flask import Flask, request, Response
from threading import Thread

app = Flask(__name__)

_START_TIME = time.time()


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
        "note": (
            "PRODUCTION — работает 24/7 на Replit Reserved VM"
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
        f"Режим: {'🚀 PRODUCTION (Reserved VM, 24/7)' if mode == 'production' else '⚠️ DEVELOPMENT (только пока открыт браузер)'}",
        f"Аптайм: {_fmt_uptime(uptime)}",
        f"ИИ: {os.environ.get('LLM_BACKEND', 'openai')}",
    ]
    return Response("\n".join(lines), mimetype='text/plain; charset=utf-8')


@app.route('/api/twiml', methods=['GET', 'POST'])
def twiml():
    message = request.values.get('message', 'Привет, это сообщение от Руслана.')
    safe = (message
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'  <Say language="ru-RU" voice="Polly.Tatyana">{safe}</Say>\n'
        '  <Pause length="1"/>\n'
        f'  <Say language="ru-RU" voice="Polly.Tatyana">{safe}</Say>\n'
        '</Response>'
    )
    return Response(xml, mimetype='text/xml; charset=utf-8')


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
