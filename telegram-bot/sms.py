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


def send_geo_to_toha(lat: float, lon: float) -> bool:
    """Отправить геопозицию Тохе по SMS"""
    maps_link = f"https://maps.google.com/?q={lat},{lon}"
    text = f"📍 Руслан ждёт тебя здесь:\n{maps_link}"
    return send_sms_to_toha(text)
