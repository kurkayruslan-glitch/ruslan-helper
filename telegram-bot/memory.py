import json
import os
import threading
from datetime import datetime

from db import connect, is_sqlite, now_default_sql, sql, to_datetime
from logging_setup import setup_logging

MEMORY_FILE = "memory.json"
logger = setup_logging("ruslan-helper.memory")

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
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if is_sqlite() else "SERIAL PRIMARY KEY"
        with connect() as c, c.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS memory_facts (
                    id        {pk},
                    fact      TEXT NOT NULL UNIQUE,
                    added_at  TIMESTAMP NOT NULL DEFAULT {now_default_sql()}
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS chat_memory (
                    chat_id    TEXT PRIMARY KEY,
                    summary    TEXT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT {now_default_sql()}
                )
                """
            )
            c.commit()
        _migrate_json_if_present()
        _initialized = True


def _migrate_json_if_present():
    if not os.path.exists(MEMORY_FILE):
        return
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return
        with connect() as c, c.cursor() as cur:
            for item in data:
                if isinstance(item, dict):
                    fact = (item.get("fact") or "").strip()
                    added_raw = item.get("added", "")
                else:
                    fact = str(item).strip()
                    added_raw = ""
                if not fact:
                    continue
                added_at = None
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                    try:
                        added_at = datetime.strptime(added_raw, fmt)
                        break
                    except Exception:
                        continue
                if added_at is None:
                    added_at = datetime.now()
                cur.execute(
                    sql("INSERT OR IGNORE INTO memory_facts (fact, added_at) VALUES (?, ?)")
                    if is_sqlite()
                    else "INSERT INTO memory_facts (fact, added_at) VALUES (%s, %s) ON CONFLICT (fact) DO NOTHING",
                    (fact, added_at),
                )
            c.commit()
        os.rename(MEMORY_FILE, MEMORY_FILE + ".migrated")
    except Exception as e:
        logger.exception("memory.json migration skipped: %s", e)


def _load_raw() -> list:
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        cur.execute("SELECT id, fact, added_at FROM memory_facts ORDER BY id")
        rows = cur.fetchall()
    out = []
    for r in rows:
        dt = to_datetime(r["added_at"]) or datetime.now()
        out.append({"fact": r["fact"], "added": dt.strftime("%Y-%m-%d %H:%M")})
    return out


def get_all_facts() -> list:
    return [item["fact"] for item in _load_raw()]


def add_fact(fact: str) -> bool:
    fact = fact.strip()
    if not fact:
        return False
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        if is_sqlite():
            cur.execute("INSERT OR IGNORE INTO memory_facts (fact) VALUES (?)", (fact,))
        else:
            cur.execute(
                "INSERT INTO memory_facts (fact) VALUES (%s) ON CONFLICT (fact) DO NOTHING",
                (fact,),
            )
        added = cur.rowcount > 0
        c.commit()
    return added


def _delete_by_position(index: int) -> str | None:
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        cur.execute("SELECT id, fact FROM memory_facts ORDER BY id")
        rows = cur.fetchall()
        if not (0 <= index < len(rows)):
            return None
        target = rows[index]
        cur.execute(sql("DELETE FROM memory_facts WHERE id = ?"), (target["id"],))
        c.commit()
        return target["fact"]


def remove_fact(index: int) -> bool:
    return _delete_by_position(index) is not None


def clear_all():
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        cur.execute("DELETE FROM memory_facts")
        cur.execute("DELETE FROM chat_memory")
        c.commit()


def get_chat_summary(chat_id: int) -> str:
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        cur.execute(sql("SELECT summary FROM chat_memory WHERE chat_id = ?"), (str(chat_id),))
        row = cur.fetchone()
    return (row.get("summary") or "").strip() if row else ""


def save_chat_summary(chat_id: int, summary: str) -> bool:
    summary = (summary or "").strip()
    _ensure_schema()
    if not summary:
        clear_chat_summary(chat_id)
        return False
    with _lock, connect() as c, c.cursor() as cur:
        if is_sqlite():
            cur.execute(
                """
                INSERT INTO chat_memory (chat_id, summary)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    summary = excluded.summary,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (str(chat_id), summary),
            )
        else:
            cur.execute(
                """
                INSERT INTO chat_memory (chat_id, summary)
                VALUES (%s, %s)
                ON CONFLICT (chat_id) DO UPDATE SET
                    summary = EXCLUDED.summary,
                    updated_at = NOW()
                """,
                (str(chat_id), summary),
            )
        c.commit()
    return True


def clear_chat_summary(chat_id: int | None = None):
    _ensure_schema()
    with _lock, connect() as c, c.cursor() as cur:
        if chat_id is None:
            cur.execute("DELETE FROM chat_memory")
        else:
            cur.execute(sql("DELETE FROM chat_memory WHERE chat_id = ?"), (str(chat_id),))
        c.commit()


def format_chat_summary_for_prompt(chat_id: int) -> str:
    summary = get_chat_summary(chat_id)
    if not summary:
        return ""
    return f"\nКраткая память нашего диалога:\n{summary}\n"


def format_for_prompt() -> str:
    facts = get_all_facts()
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts)
    return f"\nЧто я знаю о Руслане (долгосрочная память):\n{lines}\n"


def format_for_display() -> str:
    raw = _load_raw()
    if not raw:
        return "🧠 Память фактов пуста — я пока ничего не запомнил."
    lines = []
    for i, item in enumerate(raw, 1):
        fact = item.get("fact", "")
        added = item.get("added", "")
        lines.append(f"{i}. {fact}" + (f"  _{added}_" if added else ""))
    return "🧠 *Что я помню о тебе:*\n\n" + "\n".join(lines)


def get_facts() -> list[str]:
    return get_all_facts()


def delete_fact(index: int) -> str | None:
    return _delete_by_position(index)


def clear_facts():
    clear_all()
