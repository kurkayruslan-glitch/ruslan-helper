from flask import Flask, request, Response
from threading import Thread

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

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

def run():
    app.run(host='0.0.0.0', port=8000)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
