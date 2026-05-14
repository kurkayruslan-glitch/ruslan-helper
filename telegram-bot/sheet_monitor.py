"""
Мониторинг Google Sheets: периодически опрашивает зарегистрированные таблицы
и формирует короткие алерты при значимых изменениях (новые строки, резкие
изменения числовых сумм, новые строки вне рабочего времени).
"""
import json
import os
import threading
from datetime import datetime
from typing import Optional

from analytics import list_sheets, get_raw_data, _try_number, _fmt

STATE_FILE = "sheet_monitor.json"
_state_lock = threading.Lock()

SIGNIFICANT_CHANGE_PCT = float(os.environ.get("SHEET_MONITOR_CHANGE_PCT", "20"))
BUSINESS_HOUR_START = int(os.environ.get("SHEET_BUSINESS_HOUR_START", "9"))
BUSINESS_HOUR_END = int(os.environ.get("SHEET_BUSINESS_HOUR_END", "20"))


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    """Атомарная запись через временный файл, чтобы избежать порчи при
    параллельной записи из планировщика и из обработчиков callback."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _entry(state: dict, sheet_id: str) -> dict:
    return state.setdefault(sheet_id, {"enabled": False, "snapshot": None, "last_check": None})


def is_enabled(sheet_id: str) -> bool:
    with _state_lock:
        return bool(_load_state().get(sheet_id, {}).get("enabled"))


def set_enabled(sheet_id: str, enabled: bool):
    """Включает/выключает мониторинг.
    При включении строит свежий baseline-snapshot; если таблица недоступна —
    выкидывает исключение и НЕ включает мониторинг (чтобы не сыпать ошибками)."""
    if enabled:
        # Защита от случайного/подменённого sheet_id из callback payload —
        # включать мониторинг можно только для зарегистрированных таблиц.
        if sheet_id not in set(list_sheets().values()):
            raise ValueError("Эта таблица не зарегистрирована.")
        # Стартовый снимок строим вне lock-а (сетевой вызов может быть медленным)
        snap = _build_snapshot(sheet_id)  # пусть исключение пробросится наверх
        with _state_lock:
            state = _load_state()
            e = _entry(state, sheet_id)
            e["enabled"] = True
            # Всегда обновляем baseline, чтобы не алертить «догоняющими»
            # изменениями за период, пока мониторинг был выключен.
            e["snapshot"] = snap
            e["last_check"] = datetime.utcnow().isoformat()
            _save_state(state)
    else:
        with _state_lock:
            state = _load_state()
            _entry(state, sheet_id)["enabled"] = False
            _save_state(state)


def list_monitored() -> dict:
    """{sheet_id: enabled_bool} для всех зарегистрированных таблиц."""
    with _state_lock:
        state = _load_state()
    result = {}
    for name, sheet_id in list_sheets().items():
        result[sheet_id] = {
            "name": name,
            "enabled": bool(state.get(sheet_id, {}).get("enabled")),
        }
    return result


def _column_sums(headers: list, data_rows: list) -> dict:
    sums = {}
    for col_idx, header in enumerate(headers):
        nums = []
        for row in data_rows:
            if col_idx < len(row):
                n = _try_number(row[col_idx])
                if n is not None:
                    nums.append(n)
        if nums:
            sums[str(header)] = sum(nums)
    return sums


def _build_snapshot(sheet_id: str) -> dict:
    title, headers, data_rows, _sheet_names, target_sheet = get_raw_data(sheet_id)
    return {
        "title": title,
        "sheet": target_sheet,
        "row_count": len(data_rows),
        "sums": _column_sums(headers, data_rows),
    }


def _is_outside_business_hours(now: datetime) -> bool:
    return now.hour < BUSINESS_HOUR_START or now.hour >= BUSINESS_HOUR_END


def check_sheet(sheet_id: str, name: str, now_local: datetime) -> Optional[str]:
    """
    Сравнивает текущий снимок с прошлым; возвращает текст алерта или None.
    Обновляет состояние в файле.
    """
    with _state_lock:
        state = _load_state()
        entry = _entry(state, sheet_id)
        if not entry.get("enabled"):
            return None
        prev = entry.get("snapshot")

    try:
        snap = _build_snapshot(sheet_id)
    except Exception as e:
        # Дедуп ошибок: одну и ту же ошибку шлём не чаще раза в 6 часов,
        # чтобы при длительной недоступности таблицы не сыпать алерты.
        err_text = str(e)
        now_iso = datetime.utcnow().isoformat()
        suppress = False
        with _state_lock:
            state = _load_state()
            entry = _entry(state, sheet_id)
            last_err = entry.get("last_error") or {}
            if last_err.get("text") == err_text:
                try:
                    last_at = datetime.fromisoformat(last_err.get("at", ""))
                    if (datetime.utcnow() - last_at).total_seconds() < 6 * 3600:
                        suppress = True
                except Exception:
                    pass
            entry["last_check"] = now_iso
            entry["last_error"] = {"text": err_text, "at": now_iso}
            _save_state(state)
        if suppress:
            return None
        return f"⚠️ *{name.title()}* — не удалось проверить таблицу: {err_text}"

    with _state_lock:
        state = _load_state()
        entry = _entry(state, sheet_id)
        if not entry.get("enabled"):
            return None  # выключили во время проверки
        entry["snapshot"] = snap
        entry["last_check"] = datetime.utcnow().isoformat()
        _save_state(state)

    if not prev:
        return None  # первый снимок — без алерта

    alerts = []
    new_rows = snap["row_count"] - prev.get("row_count", 0)
    if new_rows > 0:
        msg = f"➕ Новых строк: {new_rows}"
        if _is_outside_business_hours(now_local):
            msg += f" (вне рабочих часов, {now_local.strftime('%H:%M')})"
        alerts.append(msg)

    prev_sums = prev.get("sums", {})
    for col, val in snap.get("sums", {}).items():
        old = prev_sums.get(col)
        if old is None:
            continue
        if old == 0:
            if val != 0:
                alerts.append(f"📈 «{col}»: было 0, стало {_fmt(val)}")
            continue
        delta_pct = (val - old) / abs(old) * 100
        if abs(delta_pct) >= SIGNIFICANT_CHANGE_PCT:
            arrow = "📉" if delta_pct < 0 else "📈"
            alerts.append(
                f"{arrow} «{col}»: {_fmt(old)} → {_fmt(val)} "
                f"({'+' if delta_pct >= 0 else ''}{delta_pct:.0f}%)"
            )

    if not alerts:
        return None

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    header = f"🔔 *{name.title()}* ({snap.get('sheet','')})\n[Открыть таблицу]({url})"
    return header + "\n" + "\n".join(alerts)


def check_all(now_local: datetime) -> list:
    """Проверяет все включённые таблицы, возвращает список текстов алертов."""
    alerts = []
    for name, sheet_id in list_sheets().items():
        if not is_enabled(sheet_id):
            continue
        try:
            msg = check_sheet(sheet_id, name, now_local)
            if msg:
                alerts.append(msg)
        except Exception as e:
            alerts.append(f"⚠️ *{name.title()}* — ошибка мониторинга: {e}")
    return alerts
