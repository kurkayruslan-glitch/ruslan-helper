import os
from urllib.parse import quote
from twilio.rest import Client

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

    clean_to = to_number.replace(" ", "").replace("-", "")
    twiml_url = f"https://{domain}/api/twiml?message={quote(message)}"

    try:
        client = Client(account_sid, auth_token)
        call = client.calls.create(
            to=clean_to,
            from_=from_number,
            url=twiml_url,
        )
        return True, call.sid
    except Exception as e:
        return False, str(e)
