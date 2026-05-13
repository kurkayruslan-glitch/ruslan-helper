import json
import os
from sheets import get_values, get_sheet_info

REGISTRY_FILE = "sheets_registry.json"


def load_registry() -> dict:
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_registry(registry: dict):
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def register_sheet(name: str, spreadsheet_id: str):
    registry = load_registry()
    registry[name.lower()] = spreadsheet_id
    save_registry(registry)


def find_sheet_id(name: str) -> str | None:
    registry = load_registry()
    return registry.get(name.lower())


def list_sheets() -> dict:
    return load_registry()


def _try_number(val: str):
    try:
        return float(str(val).replace(",", ".").replace(" ", "").replace("\u00a0", ""))
    except (ValueError, TypeError):
        return None


def _fmt(n: float) -> str:
    if n == int(n):
        return f"{int(n):,}".replace(",", " ")
    return f"{n:,.2f}".replace(",", " ").replace(".", ",")


def get_raw_data(spreadsheet_id: str, sheet_name: str = None) -> tuple:
    """
    Возвращает (title, headers, data_rows, sheet_names) или бросает Exception.
    """
    info = get_sheet_info(spreadsheet_id)
    title = info.get("properties", {}).get("title", "Без названия")
    sheets = info.get("sheets", [])
    sheet_names = [s["properties"]["title"] for s in sheets]

    target_sheet = sheet_name or sheet_names[0]
    range_name = f"{target_sheet}!A1:ZZ10000"
    values = get_values(spreadsheet_id, range_name)

    headers = values[0] if values else []
    data_rows = values[1:] if len(values) > 1 else []
    return title, headers, data_rows, sheet_names, target_sheet


def build_raw_stats(headers: list, data_rows: list) -> str:
    """Базовая статистика по столбцам (без AI)."""
    stats = []
    for col_idx, header in enumerate(headers):
        nums, text_vals = [], []
        for row in data_rows:
            if col_idx < len(row):
                cell = str(row[col_idx]).strip()
                if cell:
                    n = _try_number(cell)
                    if n is not None:
                        nums.append(n)
                    else:
                        text_vals.append(cell)
        if nums:
            stats.append(
                f"{header}: сумма={_fmt(sum(nums))}, "
                f"среднее={_fmt(sum(nums)/len(nums))}, "
                f"макс={_fmt(max(nums))}, мин={_fmt(min(nums))}, "
                f"записей={len(nums)}"
            )
        elif text_vals:
            unique = list(dict.fromkeys(text_vals))
            stats.append(f"{header}: {len(text_vals)} значений, топ: {', '.join(unique[:5])}")
    return "\n".join(stats) if stats else "нет числовых данных"


def analyze_sheet_data(spreadsheet_id: str, sheet_name: str = None) -> str:
    """Базовая текстовая аналитика (без AI) — используется как fallback."""
    try:
        title, headers, data_rows, sheet_names, target_sheet = get_raw_data(spreadsheet_id, sheet_name)

        if not headers:
            return f"📊 Таблица «{title}» пуста или нет данных."

        lines = [
            f"📊 *{title}* — {target_sheet}",
            f"Строк: {len(data_rows)}, Столбцов: {len(headers)}",
            "",
            "📈 *Статистика:*",
            build_raw_stats(headers, data_rows),
        ]
        if len(sheet_names) > 1:
            lines.append(f"\n📑 Листы: {', '.join(sheet_names)}")
        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ Ошибка при анализе: {e}"


def analyze_sheet_with_ai(spreadsheet_id: str, sheet_name: str = None) -> str:
    """
    Умный анализ через Grok — возвращает бизнес-разбор с выводами.
    Импортируем grok здесь чтобы избежать циклических импортов.
    """
    try:
        from grok import analyze_sheet_with_grok

        title, headers, data_rows, sheet_names, target_sheet = get_raw_data(spreadsheet_id, sheet_name)

        if not headers:
            return f"📊 Таблица «{title}» пуста."

        raw_stats = build_raw_stats(headers, data_rows)
        return analyze_sheet_with_grok(f"{title} / {target_sheet}", headers, data_rows, raw_stats)

    except Exception as e:
        return f"⚠️ Ошибка при анализе: {e}"
