import os
from twilio.rest import Client

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
TOHA_NUMBER = os.environ.get("TOHA_PHONE_NUMBER")


def send_sms_to_toha(text: str) -> bool:
    """Отправить SMS Тохе"""
    try:
        client = Client(ACCOUNT_SID, AUTH_TOKEN)
        client.messages.create(
            body=text,
            from_=FROM_NUMBER,
            to=TOHA_NUMBER
        )
        return True
    except Exception as e:
        print(f"Ошибка отправки SMS: {e}")
        return False


def send_sms(to_number: str, text: str) -> tuple[bool, str]:
    """Отправить SMS на любой номер через Twilio. Возвращает (успех, SID или текст ошибки)."""
    if not ACCOUNT_SID or not AUTH_TOKEN or not FROM_NUMBER:
        return False, "Не настроены TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER в .env"
    to = (to_number or "").strip().replace(" ", "").replace("-", "")
    if not to:
        return False, "Не указан номер получателя"
    if not to.startswith("+"):
        return False, f"Номер должен быть в международном формате с +, а пришло: {to}"
    if not (text or "").strip():
        return False, "Пустой текст SMS"
    try:
        msg = Client(ACCOUNT_SID, AUTH_TOKEN).messages.create(
            body=text, from_=FROM_NUMBER, to=to
        )
        return True, msg.sid
    except Exception as e:
        return False, str(e)


def send_geo_to_toha(lat: float, lon: float) -> bool:
    """Отправить геопозицию Тохе по SMS"""
    maps_link = f"https://maps.google.com/?q={lat},{lon}"
    text = f"📍 Руслан ждёт тебя здесь:\n{maps_link}"
    return send_sms_to_toha(text)
