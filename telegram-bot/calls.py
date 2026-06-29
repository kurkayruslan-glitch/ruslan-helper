import os
import re
from urllib.parse import urlencode

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException


def _strip_ansi(text: str) -> str:
    """Убирает ANSI escape-коды из строки."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _escape_xml(text: str) -> str:
    return (str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def _twilio_client():
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = (
        os.environ.get("TWILIO_FROM_NUMBER")
        or os.environ.get("TWILIO_PHONE_NUMBER")
    )
    if not all([account_sid, auth_token, from_number]):
        return None, None, "Twilio не настроен"
    return Client(account_sid, auth_token), from_number, ""


def _clean_phone(to_number: str) -> str:
    return re.sub(r"[\s\-\(\)]", "", str(to_number or ""))


def _call_voice() -> str:
    return os.environ.get("CALL_VOICE", "Polly.Tatyana")


def _call_language() -> str:
    return os.environ.get("CALL_LANGUAGE", "ru-RU")


def _public_base_url() -> str:
    url = (
        os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("TWILIO_PUBLIC_BASE_URL")
        or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        or ""
    ).strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def voice_call_available() -> bool:
    """True, если можно делать интерактивный ИИ-звонок через webhook."""
    return bool(_public_base_url())


def _split_for_voice(message: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    if not text:
        return ["Здравствуйте. Это голосовой помощник Руслана."]
    parts = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    current = ""
    for part in parts:
        if not part:
            continue
        if len(current) + len(part) > 360:
            if current:
                chunks.append(current.strip())
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        chunks.append(current.strip())
    return chunks or [text[:360]]


def _say_xml(message: str, repeat: bool = False) -> str:
    """Более живое чтение: короткие куски, паузы и SSML-prosody."""
    voice = _call_voice()
    lang = _call_language()
    rate = os.environ.get("CALL_VOICE_RATE", "92%")
    pitch = os.environ.get("CALL_VOICE_PITCH", "+0%")
    chunks = _split_for_voice(message)
    lines = []
    for chunk in chunks:
        safe = _escape_xml(chunk)
        lines.append(
            f'<Say language="{lang}" voice="{voice}">'
            f'<prosody rate="{rate}" pitch="{pitch}">{safe}</prosody>'
            '</Say>'
        )
        lines.append('<Pause length="0.45"/>')
    if repeat:
        lines.append('<Pause length="1"/>')
        lines.append(
            f'<Say language="{lang}" voice="{voice}">'
            '<prosody rate="90%">Повторю коротко.</prosody>'
            '</Say>'
        )
        lines.append('<Pause length="0.4"/>')
        for chunk in chunks:
            safe = _escape_xml(chunk)
            lines.append(
                f'<Say language="{lang}" voice="{voice}">'
                f'<prosody rate="{rate}" pitch="{pitch}">{safe}</prosody>'
                '</Say>'
            )
            lines.append('<Pause length="0.45"/>')
    return "\n".join(lines)


def _twilio_error(e: TwilioRestException, clean_to: str) -> str:
    if e.code == 21219 or "unverified" in str(e).lower():
        return (
            f"⚠️ Trial-аккаунт Twilio может звонить только на подтверждённые номера.\n\n"
            f"Подтверди {clean_to} здесь:\n"
            f"twilio.com/console/phone-numbers/verified\n\n"
            f"Или пополни баланс Twilio чтобы снять ограничения."
        )
    if e.code == 21211:
        return f"⚠️ Неверный формат номера: {clean_to}"
    return _strip_ansi(e.msg or str(e))


def make_call(to_number: str, message: str) -> tuple[bool, str]:
    """
    Звонит на номер и голосом произносит сообщение.
    TwiML передаётся напрямую в Twilio (без внешнего URL/туннеля).
    Возвращает (успех, описание).
    """
    client, from_number, error = _twilio_client()
    if error:
        return False, error

    clean_to = _clean_phone(to_number)
    repeat = os.environ.get("CALL_REPEAT", "1").lower() in ("1", "true", "yes", "on")
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'{_say_xml(message, repeat=repeat)}'
        '</Response>'
    )

    try:
        call = client.calls.create(
            to=clean_to,
            from_=from_number,
            twiml=twiml,
        )
        return True, call.sid
    except TwilioRestException as e:
        return False, _twilio_error(e, clean_to)
    except Exception as e:
        return False, _strip_ansi(str(e))


def make_ai_call(to_number: str, goal: str) -> tuple[bool, str]:
    """
    Интерактивный звонок: Twilio открывает webhook, слушает речь человека
    и получает следующую реплику ИИ.
    """
    client, from_number, error = _twilio_client()
    if error:
        return False, error

    base_url = _public_base_url()
    if not base_url:
        return False, (
            "Для живого ИИ-звонка нужен публичный URL сервера.\n"
            "В Railway включи Public Networking или задай переменную PUBLIC_BASE_URL."
        )

    clean_to = _clean_phone(to_number)
    params = urlencode({"goal": str(goal or "")[:900]})
    callback_url = f"{base_url}/api/voice/start?{params}"

    try:
        call = client.calls.create(
            to=clean_to,
            from_=from_number,
            url=callback_url,
            method="POST",
        )
        return True, call.sid
    except TwilioRestException as e:
        return False, _twilio_error(e, clean_to)
    except Exception as e:
        return False, _strip_ansi(str(e))
