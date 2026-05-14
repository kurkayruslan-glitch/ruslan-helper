import json
import os
import uuid
import threading
from datetime import datetime

REMINDERS_FILE = "reminders.json"
_lock = threading.Lock()

MAX_FAILURES = 3  # Drop a reminder after this many consecutive send failures


def _load() -> list:
    if os.path.exists(REMINDERS_FILE):
        try:
            with open(REMINDERS_FILE, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []


def _save(reminders: list):
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)


def add_reminder(chat_id: int, text: str, fire_at: datetime) -> dict:
    """Добавляет напоминание. Возвращает созданный объект."""
    with _lock:
        reminders = _load()
        reminder = {
            "id": str(uuid.uuid4())[:8],
            "chat_id": chat_id,
            "text": text,
            "fire_at": fire_at.strftime("%Y-%m-%dT%H:%M"),
            "created": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "fired": False,
            "failures": 0,
        }
        reminders.append(reminder)
        _save(reminders)
    return reminder


def get_due(now: datetime) -> list:
    """Возвращает напоминания, время которых наступило и которые ещё не отправлены."""
    with _lock:
        reminders = _load()
    due = []
    for r in reminders:
        if r.get("fired"):
            continue
        if r.get("failures", 0) >= MAX_FAILURES:
            continue
        try:
            fire_at = datetime.strptime(r["fire_at"], "%Y-%m-%dT%H:%M")
        except Exception:
            continue
        if fire_at <= now:
            due.append(r)
    return due


def mark_fired(reminder_id: str):
    """Помечает напоминание как успешно отправленное."""
    with _lock:
        reminders = _load()
        for r in reminders:
            if r.get("id") == reminder_id:
                r["fired"] = True
                r["failures"] = 0
        _save(reminders)


def mark_failed(reminder_id: str):
    """Увеличивает счётчик ошибок. После MAX_FAILURES напоминание деактивируется."""
    with _lock:
        reminders = _load()
        for r in reminders:
            if r.get("id") == reminder_id:
                r["failures"] = r.get("failures", 0) + 1
                if r["failures"] >= MAX_FAILURES:
                    r["fired"] = True  # Деактивируем после MAX_FAILURES неудачных попыток
        _save(reminders)


def list_pending(chat_id: int) -> list:
    """Возвращает список активных (не отправленных) напоминаний для чата."""
    with _lock:
        reminders = _load()
    return [r for r in reminders if r.get("chat_id") == chat_id and not r.get("fired")]


def cancel_reminder(reminder_id: str) -> bool:
    """Отменяет напоминание по id. Возвращает True если найдено."""
    with _lock:
        reminders = _load()
        for r in reminders:
            if r.get("id") == reminder_id:
                r["fired"] = True
                _save(reminders)
                return True
    return False
