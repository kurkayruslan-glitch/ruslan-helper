"""Унифицированный слой БД — Postgres (Replit) или SQLite (локально).

Если DATABASE_URL не задан или начинается с 'sqlite:' — используем SQLite,
файл по умолчанию data.db рядом с ботом. Иначе — psycopg + Postgres.
"""
import os
import sqlite3
import threading
from datetime import datetime

_DSN = (os.environ.get("DATABASE_URL") or "").strip()
USE_SQLITE = (not _DSN) or _DSN.startswith("sqlite:")

if USE_SQLITE:
    if _DSN.startswith("sqlite:///"):
        SQLITE_PATH = _DSN.replace("sqlite:///", "", 1)
    elif _DSN.startswith("sqlite://"):
        SQLITE_PATH = _DSN.replace("sqlite://", "", 1) or "data.db"
    else:
        SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
else:
    import psycopg
    from psycopg.rows import dict_row

# В sqlite3 у нас сериализованный доступ через один lock
_sqlite_lock = threading.Lock()


def is_sqlite() -> bool:
    return USE_SQLITE


def placeholder() -> str:
    """Плейсхолдер параметров: '?' для SQLite, '%s' для psycopg."""
    return "?" if USE_SQLITE else "%s"


def sql(query: str) -> str:
    """Подставляет правильный плейсхолдер. В коде пиши '?' — будет заменено."""
    if USE_SQLITE:
        return query
    return query.replace("?", "%s")


def _sqlite_connect():
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class _SqliteConnCtx:
    """Контекстный менеджер с интерфейсом, похожим на psycopg.connect()."""
    def __init__(self):
        _sqlite_lock.acquire()
        self.conn = _sqlite_connect()
    def __enter__(self):
        return self
    def cursor(self):
        return _SqliteCursorCtx(self.conn)
    def commit(self):
        self.conn.commit()
    def close(self):
        try:
            self.conn.close()
        finally:
            try:
                _sqlite_lock.release()
            except RuntimeError:
                pass
    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.close()
        finally:
            try:
                _sqlite_lock.release()
            except RuntimeError:
                pass


class _SqliteCursorCtx:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        self.rowcount = 0
    def __enter__(self):
        return self
    def execute(self, query, params=()):
        self.cur.execute(query, params)
        self.rowcount = self.cur.rowcount
        return self
    def fetchall(self):
        rows = self.cur.fetchall()
        return [dict(r) for r in rows]
    def fetchone(self):
        row = self.cur.fetchone()
        return dict(row) if row else None
    def __exit__(self, exc_type, exc, tb):
        self.cur.close()


def connect():
    """Открывает соединение. Используй в `with connect() as c, c.cursor() as cur:`"""
    if USE_SQLITE:
        return _SqliteConnCtx()
    return psycopg.connect(_DSN, row_factory=dict_row)


def now_default_sql() -> str:
    """Серверный default для timestamp NOW()/CURRENT_TIMESTAMP."""
    return "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"


def to_datetime(value) -> datetime | None:
    """Нормализует значение из БД в datetime (sqlite возвращает строки)."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                continue
    return None
