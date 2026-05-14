import os
import json
from datetime import datetime

MEMORY_FILE = "memory.json"


def _load_raw() -> list:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []


def _save_raw(facts: list):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(facts, f, ensure_ascii=False, indent=2)


def get_all_facts() -> list:
    """Возвращает список фактов (строки)."""
    return [item["fact"] if isinstance(item, dict) else str(item) for item in _load_raw()]


def add_fact(fact: str) -> bool:
    """Добавляет факт, если он ещё не сохранён. Возвращает True если добавлен новый."""
    fact = fact.strip()
    if not fact:
        return False
    facts = _load_raw()
    existing = [item["fact"] if isinstance(item, dict) else str(item) for item in facts]
    # Дедупликация — не добавляем точные дубли
    if fact in existing:
        return False
    facts.append({"fact": fact, "added": datetime.now().strftime("%Y-%m-%d %H:%M")})
    _save_raw(facts)
    return True


def remove_fact(index: int) -> bool:
    """Удаляет факт по индексу (0-based). Возвращает True если удалён."""
    facts = _load_raw()
    if 0 <= index < len(facts):
        facts.pop(index)
        _save_raw(facts)
        return True
    return False


def clear_all():
    """Очищает всю память."""
    _save_raw([])


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
        if isinstance(item, dict):
            fact = item.get("fact", "")
            added = item.get("added", "")
            lines.append(f"{i}. {fact}" + (f"  _{added}_" if added else ""))
        else:
            lines.append(f"{i}. {item}")
    return "🧠 *Что я помню о тебе:*\n\n" + "\n".join(lines)


# --- Aliases for backward compatibility ---

def get_facts() -> list[str]:
    """Alias for get_all_facts (returning plain list of strings)."""
    return get_all_facts()


def delete_fact(index: int) -> str | None:
    """Alias for remove_fact (returning the removed string or None)."""
    facts = _load_raw()
    if 0 <= index < len(facts):
        item = facts.pop(index)
        _save_raw(facts)
        return item["fact"] if isinstance(item, dict) else str(item)
    return None


def clear_facts():
    """Alias for clear_all."""
    clear_all()
