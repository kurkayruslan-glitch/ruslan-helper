import requests
from urllib.parse import quote

# API сервер доступен локально
API_BASE = "http://localhost:80/api"


def _encode_range(range_name: str) -> str:
    """Экранирует имя диапазона для безопасной передачи в URL."""
    return quote(range_name, safe="!")


def get_values(spreadsheet_id: str, range_name: str) -> list:
    """Получить данные из таблицы"""
    url = f"{API_BASE}/sheets/{spreadsheet_id}/values/{_encode_range(range_name)}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data.get("values", [])


def append_values(spreadsheet_id: str, range_name: str, values: list) -> dict:
    """Добавить строку в таблицу"""
    url = f"{API_BASE}/sheets/{spreadsheet_id}/values/{_encode_range(range_name)}/append"
    body = {"values": values}
    resp = requests.post(url, json=body, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_sheet_info(spreadsheet_id: str) -> dict:
    """Получить информацию о таблице"""
    url = f"{API_BASE}/sheets/{spreadsheet_id}/info"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def format_table(values: list, max_rows: int = 20) -> str:
    """Форматировать данные таблицы для отображения в Telegram"""
    if not values:
        return "Таблица пуста"
    rows = values[:max_rows]
    lines = []
    for row in rows:
        lines.append(" | ".join(str(cell) for cell in row))
    result = "\n".join(lines)
    if len(values) > max_rows:
        result += f"\n... и ещё {len(values) - max_rows} строк"
    return result
