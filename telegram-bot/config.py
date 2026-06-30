import os
from dataclasses import dataclass
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - old Python fallback
    ZoneInfo = None


TRUE_VALUES = {"1", "true", "yes", "on", "\u0434\u0430"}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _url_with_scheme(value: str) -> str:
    url = (value or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def _public_base_url() -> str:
    return _url_with_scheme(
        os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("TWILIO_PUBLIC_BASE_URL")
        or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        or ""
    )


@dataclass(frozen=True)
class BotConfig:
    deployment_mode: str = os.environ.get("DEPLOYMENT_MODE", "development").lower()
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    owner_id: int = env_int("OWNER_ID", 7959647798)
    bot_secret_code: str = os.environ.get("BOT_SECRET_CODE", "ruslan2024vip")
    llm_backend: str = os.environ.get("LLM_BACKEND", "openai").lower()
    public_base_url: str = _public_base_url()
    timezone_name: str = os.environ.get("BOT_TIMEZONE", "Europe/Kiev")
    tz_offset_hours: str = os.environ.get("TZ_OFFSET_HOURS", "").strip()

    openai_proxy_base_url: str = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", "")
    openai_proxy_api_key: str = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY", "")
    openai_direct_api_key: str = os.environ.get("OPENAI_API_KEY", "")

    dialog_audio_max_bytes: int = env_int("DIALOG_AUDIO_MAX_BYTES", 24 * 1024 * 1024)
    grok_history_max: int = env_int("GROK_HISTORY_MAX", 80)
    chat_memory_max_chars: int = env_int("CHAT_MEMORY_MAX_CHARS", 3500)
    chat_memory_for_all: bool = env_bool("CHAT_MEMORY_FOR_ALL", False)
    morning_briefing_hour: int = env_int("MORNING_BRIEFING_HOUR", 8)

    whitelist_file: str = os.environ.get("WHITELIST_FILE", "whitelist.json")
    grok_history_file: str = os.environ.get("GROK_HISTORY_FILE", "grok_history.json")
    known_users_file: str = os.environ.get("KNOWN_USERS_FILE", "known_users.json")
    user_profiles_file: str = os.environ.get("USER_PROFILES_FILE", "user_profiles.json")
    pending_user_messages_file: str = os.environ.get("PENDING_USER_MESSAGES_FILE", "pending_user_messages.json")
    morning_briefing_file: str = os.environ.get("MORNING_BRIEFING_FILE", "morning_briefing.json")

    @property
    def is_production(self) -> bool:
        return self.deployment_mode == "production"


CONFIG = BotConfig()


def ukraine_tz_hours(config: BotConfig = CONFIG) -> int:
    if config.tz_offset_hours.lstrip("-").isdigit():
        return int(config.tz_offset_hours)
    if ZoneInfo is not None:
        try:
            now = datetime.now(ZoneInfo(config.timezone_name))
            offset = now.utcoffset()
            if offset is not None:
                return int(offset.total_seconds() // 3600)
        except Exception:
            pass

    now_utc = datetime.utcnow()
    year = now_utc.year
    dst_start = max(
        datetime(year, 3, day) for day in range(25, 32)
        if datetime(year, 3, day).weekday() == 6
    ).replace(hour=1)
    dst_end = max(
        datetime(year, 10, day) for day in range(25, 32)
        if datetime(year, 10, day).weekday() == 6
    ).replace(hour=1)
    return 3 if dst_start <= now_utc < dst_end else 2


def local_now(config: BotConfig = CONFIG) -> datetime:
    if ZoneInfo is not None and not config.tz_offset_hours:
        try:
            return datetime.now(ZoneInfo(config.timezone_name)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.utcnow() + timedelta(hours=ukraine_tz_hours(config))
