import os
import re
from urllib.parse import quote
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException


def _strip_ansi(text: str) -> str:
    """Убирает ANSI escape-коды из строки."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def make_call(to_number: str, message: str) -> tuple[bool, str]:
    """
    Звонит на номер и голосом произносит сообщение.
    Возвращает (успех, описание).
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    domain      = os.environ.get("REPLIT_DEV_DOMAIN", "localhost")

    if not all([account_sid, auth_token, from_number]):
        return False, "Twilio не настроен"

    clean_to = re.sub(r"[\s\-\(\)]", "", to_number)
    twiml_url = f"https://{domain}/api/twiml?message={quote(message)}"

    try:
        client = Client(account_sid, auth_token)
        call = client.calls.create(
            to=clean_to,
            from_=from_number,
            url=twiml_url,
        )
        return True, call.sid
    except TwilioRestException as e:
        # Понятные сообщения для типичных ошибок
        if e.code == 21219 or "unverified" in str(e).lower():
            return False, (
                f"⚠️ Trial-аккаунт Twilio может звонить только на подтверждённые номера.\n\n"
                f"Подтверди {clean_to} здесь:\n"
                f"twilio.com/console/phone-numbers/verified\n\n"
                f"Или пополни баланс Twilio чтобы снять ограничения."
            )
        if e.code == 21211:
            return False, f"⚠️ Неверный формат номера: {clean_to}"
        # Остальные ошибки — чисто без ANSI
        return False, _strip_ansi(e.msg or str(e))
    except Exception as e:
        return False, _strip_ansi(str(e))
