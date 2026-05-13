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
    """Попытка распарсить как число (поддержка запятой как разделителя)"""
    try:
        return float(str(val).replace(",", ".").replace(" ", "").replace("\u00a0", ""))
    except (ValueError, TypeError):
        return None


def analyze_sheet_data(spreadsheet_id: str, sheet_name: str = None) -> str:
    """Читает таблицу и возвращает текстовую аналитику"""
    try:
        info = get_sheet_info(spreadsheet_id)
        title = info.get("properties", {}).get("title", "Без названия")
        sheets = info.get("sheets", [])
        sheet_names = [s["properties"]["title"] for s in sheets]

        target_sheet = sheet_name or sheet_names[0]
        range_name = f"{target_sheet}!A1:ZZ10000"
        values = get_values(spreadsheet_id, range_name)

        if not values:
            return f"📊 Таблица «{title}» пуста или нет данных."

        headers = values[0] if values else []
        data_rows = values[1:] if len(values) > 1 else []

        total_rows = len(data_rows)
        total_cols = len(headers)

        lines = [
            f"📊 *Аналитика таблицы: {title}*",
            f"📄 Лист: {target_sheet}",
            f"📏 Строк данных: {total_rows}",
            f"📐 Столбцов: {total_cols}",
            "",
        ]

        if sheet_names and len(sheet_names) > 1:
            lines.append(f"📑 Все листы: {', '.join(sheet_names)}")
            lines.append("")

        # Анализ по столбцам
        col_stats = []
        for col_idx, header in enumerate(headers):
            nums = []
            text_vals = []
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
                total = sum(nums)
                avg = total / len(nums)
                col_stats.append(
                    f"*{header}* — "
                    f"Сумма: {_fmt(total)} | "
                    f"Среднее: {_fmt(avg)} | "
                    f"Макс: {_fmt(max(nums))} | "
                    f"Мин: {_fmt(min(nums))} | "
                    f"Записей: {len(nums)}"
                )
            elif text_vals:
                unique = list(dict.fromkeys(text_vals))
                top = unique[:5]
                preview = ", ".join(top) + ("..." if len(unique) > 5 else "")
                col_stats.append(f"*{header}* — Текст, {len(text_vals)} значений. Топ: {preview}")

        if col_stats:
            lines.append("📈 *Статистика по столбцам:*")
            lines.extend(col_stats)

        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ Ошибка при анализе таблицы: {e}"


def _fmt(n: float) -> str:
    """Красивый формат числа"""
    if n == int(n):
        return f"{int(n):,}".replace(",", " ")
    return f"{n:,.2f}".replace(",", " ").replace(".", ",")
