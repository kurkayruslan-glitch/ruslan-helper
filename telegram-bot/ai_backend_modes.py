"""Persistent AI backend mode storage for chat-level Kryven/OpenAI switching."""

import os
import json
import threading

MODES_FILE = "ai_backend_modes.json"
_lock = threading.Lock()


def _load_modes() -> dict:
    """Load AI backend modes from file. Returns {chat_id: 'kryven' or 'default'}."""
    if os.path.exists(MODES_FILE):
        try:
            with open(MODES_FILE, "r") as f:
                data = json.load(f)
                # Ensure all keys are strings (JSON keys are always strings)
                return {str(k): v for k, v in data.items()}
        except Exception as e:
            print(f"⚠️  Ошибка загрузки {MODES_FILE}: {e}")
    return {}


def _save_modes(modes: dict):
    """Save AI backend modes to file."""
    with _lock:
        try:
            with open(MODES_FILE, "w") as f:
                json.dump(modes, f, indent=2)
        except Exception as e:
            print(f"⚠️  Ошибка сохранения {MODES_FILE}: {e}")


_modes = _load_modes()


def get_mode(chat_id: int) -> str:
    """Get AI backend mode for a chat. Returns 'kryven' or 'default'."""
    return _modes.get(str(chat_id), "default")


def set_mode(chat_id: int, mode: str):
    """Set AI backend mode for a chat. mode should be 'kryven' or 'default'."""
    if mode not in ("kryven", "default"):
        raise ValueError(f"Invalid mode: {mode}")
    _modes[str(chat_id)] = mode
    _save_modes(_modes)


def is_kryven_enabled(chat_id: int) -> bool:
    """Check if Kryven mode is enabled for this chat."""
    return get_mode(chat_id) == "kryven"


def enable_kryven(chat_id: int):
    """Enable Kryven mode for this chat."""
    set_mode(chat_id, "kryven")


def disable_kryven(chat_id: int):
    """Disable Kryven mode (switch to default backend)."""
    set_mode(chat_id, "default")

