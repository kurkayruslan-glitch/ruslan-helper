import json
import os

MEMORY_FILE = "memory.json"


def _load() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"facts": []}


def _save(data: dict):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_facts() -> list[str]:
    return _load().get("facts", [])


def add_fact(fact: str) -> bool:
    fact = fact.strip()
    if not fact:
        return False
    data = _load()
    facts = data.get("facts", [])
    if fact.lower() in [f.lower() for f in facts]:
        return False
    facts.append(fact)
    data["facts"] = facts
    _save(data)
    return True


def delete_fact(index: int) -> str | None:
    data = _load()
    facts = data.get("facts", [])
    if 0 <= index < len(facts):
        removed = facts.pop(index)
        data["facts"] = facts
        _save(data)
        return removed
    return None


def clear_facts():
    _save({"facts": []})


def facts_for_prompt() -> str:
    facts = get_facts()
    if not facts:
        return ""
    lines = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(facts))
    return f"\n\nДолгосрочная память (запомнено навсегда):\n{lines}"
