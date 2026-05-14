import json
import os
import threading
import uuid
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

REMINDERS_FILE = "reminders.json"
MAX_FAILURES = 3  # Drop a reminder after this many consecutive send failures

_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized = False


def _conn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set; cannot store reminders")
    return psycopg.connect(dsn, row_factory=dict_row)


def _ensure_schema():
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id          TEXT PRIMARY KEY,
                    chat_id     BIGINT NOT NULL,
                    text        TEXT NOT NULL,
                    fire_at     TIMESTAMP NOT NULL,
                    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
                    fired       BOOLEAN NOT NULL DEFAULT FALSE,
                    failures    INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS reminders_due_idx "
                "ON reminders (fire_at) WHERE fired = FALSE"
            )
            c.commit()
        _migrate_json_if_present()
        _initialized = True


def _migrate_json_if_present():
    """One-time import of legacy reminders.json into the DB, then archive the file."""
    if not os.path.exists(REMINDERS_FILE):
        return
    try:
        with open(REMINDERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return
        with _conn() as c, c.cursor() as cur:
            for r in data:
                rid = r.get("id") or str(uuid.uuid4())[:8]
                chat_id = r.get("chat_id")
                text = r.get("text", "")
                fire_at_raw = r.get("fire_at", "")
                if chat_id is None or not fire_at_raw:
                    continue
                try:
                    fire_at = datetime.strptime(fire_at_raw, "%Y-%m-%dT%H:%M")
                except Exception:
                    continue
                created_raw = r.get("created", "")
                try:
                    created = datetime.strptime(created_raw, "%Y-%m-%dT%H:%M")
                except Exception:
                    created = datetime.now()
                cur.execute(
                    """
                    INSERT INTO reminders (id, chat_id, text, fire_at, created_at, fired, failures)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        rid,
                        int(chat_id),
                        text,
                        fire_at,
                        created,
                        bool(r.get("fired", False)),
                        int(r.get("failures", 0)),
                    ),
                )
            c.commit()
        os.rename(REMINDERS_FILE, REMINDERS_FILE + ".migrated")
    except Exception as e:
        print(f"reminders.json migration skipped: {e}")


def _row_to_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "text": row["text"],
        "fire_at": row["fire_at"].strftime("%Y-%m-%dT%H:%M"),
        "created": row["created_at"].strftime("%Y-%m-%dT%H:%M"),
        "fired": row["fired"],
        "failures": row["failures"],
    }


def add_reminder(chat_id: int, text: str, fire_at: datetime) -> dict:
    """Добавляет напоминание. Возвращает созданный объект."""
    _ensure_schema()
    rid = str(uuid.uuid4())[:8]
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reminders (id, chat_id, text, fire_at)
            VALUES (%s, %s, %s, %s)
            RETURNING id, chat_id, text, fire_at, created_at, fired, failures
            """,
            (rid, int(chat_id), text, fire_at.replace(second=0, microsecond=0)),
        )
        row = cur.fetchone()
        c.commit()
    return _row_to_dict(row)


def get_due(now: datetime) -> list:
    """Возвращает напоминания, время которых наступило и которые ещё не отправлены."""
    _ensure_schema()
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT id, chat_id, text, fire_at, created_at, fired, failures
            FROM reminders
            WHERE fired = FALSE AND failures < %s AND fire_at <= %s
            ORDER BY fire_at
            """,
            (MAX_FAILURES, now),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_fired(reminder_id: str):
    """Помечает напоминание как успешно отправленное."""
    _ensure_schema()
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE reminders SET fired = TRUE, failures = 0 WHERE id = %s",
            (reminder_id,),
        )
        c.commit()


def mark_failed(reminder_id: str):
    """Увеличивает счётчик ошибок. После MAX_FAILURES напоминание деактивируется."""
    _ensure_schema()
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE reminders
            SET failures = failures + 1,
                fired = CASE WHEN failures + 1 >= %s THEN TRUE ELSE fired END
            WHERE id = %s
            """,
            (MAX_FAILURES, reminder_id),
        )
        c.commit()


def list_pending(chat_id: int) -> list:
    """Возвращает список активных (не отправленных) напоминаний для чата."""
    _ensure_schema()
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT id, chat_id, text, fire_at, created_at, fired, failures
            FROM reminders
            WHERE chat_id = %s AND fired = FALSE
            ORDER BY fire_at
            """,
            (int(chat_id),),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def cancel_reminder(reminder_id: str) -> bool:
    """Отменяет напоминание по id. Возвращает True если найдено."""
    _ensure_schema()
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE reminders SET fired = TRUE WHERE id = %s AND fired = FALSE",
            (reminder_id,),
        )
        found = cur.rowcount > 0
        c.commit()
    return found
