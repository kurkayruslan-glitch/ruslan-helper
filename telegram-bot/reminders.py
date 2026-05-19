import json
import os
import threading
import uuid
from datetime import datetime

from db import connect, is_sqlite, now_default_sql, sql, to_datetime

REMINDERS_FILE = "reminders.json"
MAX_FAILURES = 3

_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized = False


def _ensure_schema():
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        bigint = "INTEGER" if is_sqlite() else "BIGINT"
        boolean = "INTEGER" if is_sqlite() else "BOOLEAN"
        with connect() as c, c.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS reminders (
                    id          TEXT PRIMARY KEY,
                    chat_id     {bigint} NOT NULL,
                    text        TEXT NOT NULL,
                    fire_at     TIMESTAMP NOT NULL,
                    created_at  TIMESTAMP NOT NULL DEFAULT {now_default_sql()},
                    fired       {boolean} NOT NULL DEFAULT 0,
                    failures    INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            if is_sqlite():
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders (fire_at) WHERE fired = 0"
                )
            else:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders (fire_at) WHERE fired = FALSE"
                )
            c.commit()
        _migrate_json_if_present()
        _initialized = True


def _migrate_json_if_present():
    if not os.path.exists(REMINDERS_FILE):
        return
    try:
        with open(REMINDERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return
        with connect() as c, c.cursor() as cur:
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
                fired = 1 if r.get("fired", False) else 0
                if is_sqlite():
                    cur.execute(
                        "INSERT OR IGNORE INTO reminders "
                        "(id, chat_id, text, fire_at, created_at, fired, failures) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (rid, int(chat_id), text, fire_at, created, fired, int(r.get("failures", 0))),
                    )
                else:
                    cur.execute(
                        "INSERT INTO reminders "
                        "(id, chat_id, text, fire_at, created_at, fired, failures) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                        (rid, int(chat_id), text, fire_at, created, bool(fired), int(r.get("failures", 0))),
                    )
            c.commit()
        os.rename(REMINDERS_FILE, REMINDERS_FILE + ".migrated")
    except Exception as e:
        print(f"reminders.json migration skipped: {e}")


def _row_to_dict(row: dict) -> dict:
    fire_at = to_datetime(row["fire_at"]) or datetime.now()
    created = to_datetime(row["created_at"]) or datetime.now()
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "text": row["text"],
        "fire_at": fire_at.strftime("%Y-%m-%dT%H:%M"),
        "created": created.strftime("%Y-%m-%dT%H:%M"),
        "fired": bool(row["fired"]),
        "failures": row["failures"],
    }


def add_reminder(chat_id: int, text: str, fire_at: datetime) -> dict:
    _ensure_schema()
    rid = str(uuid.uuid4())[:8]
    fa = fire_at.replace(second=0, microsecond=0)
    with _lock, connect() as c, c.cursor() as cur:
        cur.execute(
            sql("INSERT INTO reminders (id, chat_id, text, fire_at) VALUES (?, ?, ?, ?)"),
            (rid, int(chat_id), text, fa),
        )
        cur.execute(
            sql("SELECT id, chat_id, text, fire_at, created_at, fired, failures FROM reminders WHERE id = ?"),
            (rid,),
        )
        row = cur.fetchone()
        c.commit()
    return _row_to_dict(row)


def get_due(now: datetime) -> list:
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        if is_sqlite():
            cur.execute(
                "SELECT id, chat_id, text, fire_at, created_at, fired, failures FROM reminders "
                "WHERE fired = 0 AND failures < ? AND fire_at <= ? ORDER BY fire_at",
                (MAX_FAILURES, now),
            )
        else:
            cur.execute(
                "SELECT id, chat_id, text, fire_at, created_at, fired, failures FROM reminders "
                "WHERE fired = FALSE AND failures < %s AND fire_at <= %s ORDER BY fire_at",
                (MAX_FAILURES, now),
            )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_fired(reminder_id: str):
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        if is_sqlite():
            cur.execute("UPDATE reminders SET fired = 1, failures = 0 WHERE id = ?", (reminder_id,))
        else:
            cur.execute("UPDATE reminders SET fired = TRUE, failures = 0 WHERE id = %s", (reminder_id,))
        c.commit()


def mark_failed(reminder_id: str):
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        if is_sqlite():
            cur.execute(
                "UPDATE reminders SET failures = failures + 1, "
                "fired = CASE WHEN failures + 1 >= ? THEN 1 ELSE fired END "
                "WHERE id = ?",
                (MAX_FAILURES, reminder_id),
            )
        else:
            cur.execute(
                "UPDATE reminders SET failures = failures + 1, "
                "fired = CASE WHEN failures + 1 >= %s THEN TRUE ELSE fired END "
                "WHERE id = %s",
                (MAX_FAILURES, reminder_id),
            )
        c.commit()


def list_pending(chat_id: int) -> list:
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        if is_sqlite():
            cur.execute(
                "SELECT id, chat_id, text, fire_at, created_at, fired, failures FROM reminders "
                "WHERE chat_id = ? AND fired = 0 ORDER BY fire_at",
                (int(chat_id),),
            )
        else:
            cur.execute(
                "SELECT id, chat_id, text, fire_at, created_at, fired, failures FROM reminders "
                "WHERE chat_id = %s AND fired = FALSE ORDER BY fire_at",
                (int(chat_id),),
            )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def cancel_reminder(reminder_id: str) -> bool:
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        if is_sqlite():
            cur.execute("UPDATE reminders SET fired = 1 WHERE id = ? AND fired = 0", (reminder_id,))
        else:
            cur.execute("UPDATE reminders SET fired = TRUE WHERE id = %s AND fired = FALSE", (reminder_id,))
        found = cur.rowcount > 0
        c.commit()
    return found
