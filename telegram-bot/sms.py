import os
import re
from twilio.rest import Client

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\[\d+m")


def _clean_err(s: str) -> str:
    return _ANSI_RE.sub("", str(s)).replace("  ", " ").strip()


def normalize_phone(raw: str) -> str:
    """Приводит номер к международному формату. Понимает украинские варианты.
    Примеры:
      '+380 93 420 99 99' -> '+380934209999'
      '380934209999'      -> '+380934209999'
      '0934209999'        -> '+380934209999'
      '934209999'         -> '+380934209999'
    """
    if not raw:
        return ""
    s = str(raw).strip()
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if has_plus:
        return "+" + digits
    if digits.startswith("380") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+38" + digits
    if len(digits) == 9 and digits[0] in "3456789":
        return "+380" + digits
    return "+" + digits

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
    to = normalize_phone(to_number)
    if not to or len(re.sub(r"\D", "", to)) < 10:
        return False, f"Не похоже на телефонный номер: {to_number!r}"
    if not (text or "").strip():
        return False, "Пустой текст SMS"
    try:
        msg = Client(ACCOUNT_SID, AUTH_TOKEN).messages.create(
            body=text, from_=FROM_NUMBER, to=to
        )
        return True, msg.sid
    except Exception as e:
        return False, _clean_err(e)


def send_geo_to_toha(lat: float, lon: float) -> bool:
    """Отправить геопозицию Тохе по SMS"""
    maps_link = f"https://maps.google.com/?q={lat},{lon}"
    text = f"📍 Руслан ждёт тебя здесь:\n{maps_link}"
    return send_sms_to_toha(text)
