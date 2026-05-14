import json
import os
import threading
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

MEMORY_FILE = "memory.json"

_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized = False


def _conn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set; cannot store memory facts")
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
                CREATE TABLE IF NOT EXISTS memory_facts (
                    id        SERIAL PRIMARY KEY,
                    fact      TEXT NOT NULL UNIQUE,
                    added_at  TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            c.commit()
        _migrate_json_if_present()
        _initialized = True


def _migrate_json_if_present():
    """One-time import of legacy memory.json into the DB, then archive the file."""
    if not os.path.exists(MEMORY_FILE):
        return
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return
        with _conn() as c, c.cursor() as cur:
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
                    """
                    INSERT INTO memory_facts (fact, added_at)
                    VALUES (%s, %s)
                    ON CONFLICT (fact) DO NOTHING
                    """,
                    (fact, added_at),
                )
            c.commit()
        os.rename(MEMORY_FILE, MEMORY_FILE + ".migrated")
    except Exception as e:
        print(f"memory.json migration skipped: {e}")


def _load_raw() -> list:
    """Возвращает список записей в виде словарей {fact, added}."""
    _ensure_schema()
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, fact, added_at FROM memory_facts ORDER BY id"
        )
        rows = cur.fetchall()
    return [
        {"fact": r["fact"], "added": r["added_at"].strftime("%Y-%m-%d %H:%M")}
        for r in rows
    ]


def get_all_facts() -> list:
    """Возвращает список фактов (строки)."""
    return [item["fact"] for item in _load_raw()]


def add_fact(fact: str) -> bool:
    """Добавляет факт, если он ещё не сохранён. Возвращает True если добавлен новый."""
    fact = fact.strip()
    if not fact:
        return False
    _ensure_schema()
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_facts (fact)
            VALUES (%s)
            ON CONFLICT (fact) DO NOTHING
            """,
            (fact,),
        )
        added = cur.rowcount > 0
        c.commit()
    return added


def _delete_by_position(index: int) -> str | None:
    """Удаляет факт на позиции index (0-based в порядке id ASC). Возвращает текст или None."""
    _ensure_schema()
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute("SELECT id, fact FROM memory_facts ORDER BY id")
        rows = cur.fetchall()
        if not (0 <= index < len(rows)):
            return None
        target = rows[index]
        cur.execute("DELETE FROM memory_facts WHERE id = %s", (target["id"],))
        c.commit()
        return target["fact"]


def remove_fact(index: int) -> bool:
    """Удаляет факт по индексу (0-based). Возвращает True если удалён."""
    return _delete_by_position(index) is not None


def clear_all():
    """Очищает всю память."""
    _ensure_schema()
    with _lock, _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM memory_facts")
        c.commit()


def format_for_prompt() -> str:
    """Форматирует факты для вставки в системный промпт."""
    facts = get_all_facts()
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts)
    return f"\nЧто я знаю о Руслане (долгосрочная память):\n{lines}\n"


def format_for_display() -> str:
    """Форматирует факты для показа пользователю."""
    raw = _load_raw()
    if not raw:
        return "🧠 Память пуста — я пока ничего не запомнил."
    lines = []
    for i, item in enumerate(raw, 1):
        fact = item.get("fact", "")
        added = item.get("added", "")
        lines.append(f"{i}. {fact}" + (f"  _{added}_" if added else ""))
    return "🧠 *Что я помню о тебе:*\n\n" + "\n".join(lines)


# --- Aliases for backward compatibility ---

def get_facts() -> list[str]:
    """Alias for get_all_facts (returning plain list of strings)."""
    return get_all_facts()


def delete_fact(index: int) -> str | None:
    """Alias for remove_fact (returning the removed string or None)."""
    return _delete_by_position(index)


def clear_facts():
    """Alias for clear_all."""
    clear_all()
