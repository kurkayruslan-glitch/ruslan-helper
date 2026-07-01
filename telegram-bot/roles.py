import json
import os

ROLES_FILE = "roles.json"

# Роли: owner — полный доступ, developer — код бота, driver — гео владельца, worker — связь с Русланом, guest — базовый доступ
ROLES = {
    "owner": "owner",
    "developer": "developer",
    "worker": "worker",
    "driver": "driver",
    "guest": "guest",
}


def _load() -> dict:
    if os.path.exists(ROLES_FILE):
        try:
            with open(ROLES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save(data: dict):
    with open(ROLES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def set_role(chat_id: int, role: str):
    data = _load()
    data[str(chat_id)] = role
    _save(data)


def get_role(chat_id: int) -> str:
    data = _load()
    return data.get(str(chat_id), "guest")


def list_roles() -> dict:
    return _load()
