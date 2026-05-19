import os
import re
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException


def _strip_ansi(text: str) -> str:
    """Убирает ANSI escape-коды из строки."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _escape_xml(text: str) -> str:
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&apos;"))


def make_call(to_number: str, message: str) -> tuple[bool, str]:
    """
    Звонит на номер и голосом произносит сообщение.
    TwiML передаётся напрямую в Twilio (без внешнего URL/туннеля).
    Возвращает (успех, описание).
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")

    if not all([account_sid, auth_token, from_number]):
        return False, "Twilio не настроен"

    clean_to = re.sub(r"[\s\-\(\)]", "", to_number)
    safe = _escape_xml(message)

    twiml = (
        '<Response>'
        f'<Say language="ru-RU" voice="Polly.Tatyana">{safe}</Say>'
        '<Pause length="1"/>'
        f'<Say language="ru-RU" voice="Polly.Tatyana">{safe}</Say>'
        '</Response>'
    )

    try:
        client = Client(account_sid, auth_token)
        call = client.calls.create(
            to=clean_to,
            from_=from_number,
            twiml=twiml,
        )
        return True, call.sid
    except TwilioRestException as e:
        if e.code == 21219 or "unverified" in str(e).lower():
            return False, (
                f"⚠️ Trial-аккаунт Twilio может звонить только на подтверждённые номера.\n\n"
                f"Подтверди {clean_to} здесь:\n"
                f"twilio.com/console/phone-numbers/verified\n\n"
                f"Или пополни баланс Twilio чтобы снять ограничения."
            )
        if e.code == 21211:
            return False, f"⚠️ Неверный формат номера: {clean_to}"
        return False, _strip_ansi(e.msg or str(e))
    except Exception as e:
        return False, _strip_ansi(str(e))
