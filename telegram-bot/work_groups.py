import os
import re
from datetime import datetime

from safe_json import read_json_file, write_json_file


GROUP_MEMORY_FILE = os.environ.get("WORK_GROUP_MEMORY_FILE", "work_group_memory.json")
GROUP_SETTINGS_FILE = os.environ.get("WORK_GROUP_SETTINGS_FILE", "work_group_settings.json")
MAX_MESSAGES_PER_GROUP = int(os.environ.get("WORK_GROUP_MAX_MESSAGES", "350"))
MAX_TASKS_PER_GROUP = int(os.environ.get("WORK_GROUP_MAX_TASKS", "120"))


def _now_text(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M")


def _chat_key(chat_id: int) -> str:
    return str(int(chat_id))


def is_group_chat(chat_type: str | None) -> bool:
    return str(chat_type or "").lower() in {"group", "supergroup"}


def load_settings() -> dict:
    data = read_json_file(GROUP_SETTINGS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_settings(settings: dict) -> None:
    write_json_file(GROUP_SETTINGS_FILE, settings if isinstance(settings, dict) else {})


def load_memory() -> dict:
    data = read_json_file(GROUP_MEMORY_FILE, {})
    return data if isinstance(data, dict) else {}


def save_memory(memory: dict) -> None:
    write_json_file(GROUP_MEMORY_FILE, memory if isinstance(memory, dict) else {})


def _group_settings(chat_id: int, settings: dict | None = None) -> dict:
    settings = settings if isinstance(settings, dict) else load_settings()
    key = _chat_key(chat_id)
    group = settings.get(key)
    if not isinstance(group, dict):
        group = {}
    group.setdefault("enabled", True)
    group.setdefault("report_chat_ids", [])
    settings[key] = group
    return group


def group_enabled(chat_id: int) -> bool:
    return bool(_group_settings(chat_id).get("enabled", True))


def set_group_enabled(chat_id: int, enabled: bool) -> None:
    settings = load_settings()
    group = _group_settings(chat_id, settings)
    group["enabled"] = bool(enabled)
    save_settings(settings)


def add_report_recipient(chat_id: int, recipient_chat_id: int) -> None:
    settings = load_settings()
    group = _group_settings(chat_id, settings)
    recipients = [int(x) for x in group.get("report_chat_ids", []) if str(x).lstrip("-").isdigit()]
    recipient_chat_id = int(recipient_chat_id)
    if recipient_chat_id not in recipients:
        recipients.append(recipient_chat_id)
    group["report_chat_ids"] = recipients
    save_settings(settings)


def report_recipients(chat_id: int, owner_id: int) -> list[int]:
    group = _group_settings(chat_id)
    recipients = [int(owner_id)]
    for value in group.get("report_chat_ids", []):
        try:
            recipient = int(value)
        except Exception:
            continue
        if recipient not in recipients:
            recipients.append(recipient)
    return recipients


def _sender_name(user) -> str:
    if not user:
        return "unknown"
    username = (getattr(user, "username", None) or "").strip()
    first_name = (getattr(user, "first_name", None) or "").strip()
    last_name = (getattr(user, "last_name", None) or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if username:
        return f"@{username}" + (f" ({full_name})" if full_name else "")
    return full_name or str(getattr(user, "id", "") or "unknown")


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:1800]


TASK_PATTERNS = (
    r"(?:надо|нужно|нужн[аоы]|сделай|сделать|проверь|проверить|подготовь|подготовить|скинь|отправь|создай|найди|узнай|перезвони|позвони|напомни|договорись|разбери|посмотри)",
    r"(?:должен|должна|должны|обязан|обязана)",
    r"(?:задача|ответственный|срок|дедлайн)",
)


def extract_task(text: str, sender_name: str) -> dict | None:
    clean = _clean_text(text)
    if len(clean) < 5:
        return None
    low = clean.lower()
    if not any(re.search(pattern, low, re.IGNORECASE) for pattern in TASK_PATTERNS):
        return None

    assignee = ""
    mention = re.search(r"@[\w_]{3,}", clean)
    if mention:
        assignee = mention.group(0)
    else:
        lead_name = re.search(r"^\s*([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_.-]{1,40})\s*[,:\-]", clean)
        if lead_name:
            assignee = lead_name.group(1)
    if not assignee:
        match = re.search(
            r"(?:для|пусть|пускай|задача для|ответственный)\s+([A-Za-zА-Яа-яЁё0-9_@.-]{2,40})",
            clean,
            re.IGNORECASE,
        )
        if match:
            assignee = match.group(1)

    return {
        "created_at": _now_text(),
        "from": sender_name,
        "assignee": assignee or "не определён",
        "text": clean,
        "status": "open",
    }


def remember_message(
    chat_id: int,
    chat_title: str,
    user,
    text: str,
    *,
    kind: str = "text",
    message_id: int | None = None,
) -> dict | None:
    if not group_enabled(chat_id):
        return None
    clean = _clean_text(text)
    if not clean:
        return None

    memory = load_memory()
    key = _chat_key(chat_id)
    group = memory.get(key)
    if not isinstance(group, dict):
        group = {}
    group["chat_id"] = int(chat_id)
    group["title"] = chat_title or group.get("title") or ""
    group["updated_at"] = _now_text()

    sender = _sender_name(user)
    item = {
        "time": _now_text(),
        "message_id": int(message_id or 0),
        "from": sender,
        "user_id": int(getattr(user, "id", 0) or 0) if user else 0,
        "kind": kind,
        "text": clean,
    }
    messages = group.get("messages")
    if not isinstance(messages, list):
        messages = []
    messages.append(item)
    group["messages"] = messages[-MAX_MESSAGES_PER_GROUP:]

    task = extract_task(clean, sender)
    if task:
        tasks = group.get("tasks")
        if not isinstance(tasks, list):
            tasks = []
        task["message_id"] = int(message_id or 0)
        tasks.append(task)
        group["tasks"] = tasks[-MAX_TASKS_PER_GROUP:]

    memory[key] = group
    save_memory(memory)
    return task


def remember_audio_report(chat_id: int, chat_title: str, user, filename: str, duration_text: str) -> None:
    text = f"Отправлена аудиозапись для разбора: {filename or 'audio'}, длительность: {duration_text or 'не определена'}."
    remember_message(chat_id, chat_title, user, text, kind="audio")


def recent_context(chat_id: int, limit: int = 80) -> str:
    group = load_memory().get(_chat_key(chat_id), {})
    if not isinstance(group, dict):
        return ""
    messages = group.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    tasks = group.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []

    lines = []
    if group.get("title"):
        lines.append(f"Рабочая группа: {group.get('title')}")
    if messages:
        lines.append("Последние сообщения группы:")
        for item in messages[-limit:]:
            lines.append(f"- {item.get('time', '')} | {item.get('from', '')}: {item.get('text', '')}")
    open_tasks = [t for t in tasks if t.get("status") == "open"][-40:]
    if open_tasks:
        lines.append("Открытые задачи и поручения:")
        for item in open_tasks:
            assignee = item.get("assignee") or "не определён"
            lines.append(f"- {item.get('created_at', '')} | {assignee}: {item.get('text', '')}")
    return "\n".join(lines[-220:])


def tasks_report(chat_id: int) -> str:
    group = load_memory().get(_chat_key(chat_id), {})
    if not isinstance(group, dict):
        return "По этой группе пока нет сохранённых задач."
    tasks = [t for t in group.get("tasks", []) if isinstance(t, dict) and t.get("status") == "open"]
    if not tasks:
        return "По этой группе пока нет открытых задач."
    lines = ["Открытые задачи группы:"]
    for i, task in enumerate(tasks[-60:], 1):
        assignee = task.get("assignee") or "не определён"
        lines.append(f"{i}. {assignee}: {task.get('text', '')} ({task.get('created_at', '')})")
    return "\n".join(lines)


def should_answer_in_group(message, bot_username: str | None = None) -> bool:
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if text.startswith("/"):
        return True
    if bot_username and f"@{bot_username.lower()}" in text.lower():
        return True
    reply_to = getattr(message, "reply_to_message", None)
    reply_user = getattr(reply_to, "from_user", None) if reply_to else None
    return bool(reply_user and getattr(reply_user, "is_bot", False))


def strip_bot_mention(text: str, bot_username: str | None) -> str:
    value = str(text or "")
    if bot_username:
        value = re.sub(rf"@{re.escape(bot_username)}\b", "", value, flags=re.IGNORECASE)
    return value.strip()
