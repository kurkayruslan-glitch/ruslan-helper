"""Запуск и закрытие программ на ПК Руслана по человеческим названиям.

Сканирует ярлыки (.lnk) в меню Пуск и на рабочем столе, чтобы найти
программу по нечёткому совпадению («фотошоп» -> «Adobe Photoshop 2024»).
Для приложений удалённого доступа (TeamViewer, AnyDesk) — проверяет
стандартные пути установки напрямую, без ярлыков.
Работает на Windows; на Mac/Linux — упрощённый fallback.
"""

import os
import sys
import subprocess
import glob
import difflib

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# ──────────────────────────────────────────────────────────────────
# Прямые пути приложений удалённого доступа (не всегда есть ярлык)
# ──────────────────────────────────────────────────────────────────

def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))

_REMOTE_APPS_DIRECT: dict[str, list[str]] = {
    "teamviewer": [
        r"C:\Program Files\TeamViewer\TeamViewer.exe",
        r"C:\Program Files (x86)\TeamViewer\TeamViewer.exe",
        _expand(r"%LOCALAPPDATA%\TeamViewer\TeamViewer.exe"),
    ],
    "anydesk": [
        r"C:\Program Files (x86)\AnyDesk\AnyDesk.exe",
        r"C:\Program Files\AnyDesk\AnyDesk.exe",
        _expand(r"%APPDATA%\AnyDesk\AnyDesk.exe"),
        _expand(r"%LOCALAPPDATA%\AnyDesk\AnyDesk.exe"),
    ],
}

_REMOTE_APP_EXE: dict[str, str] = {
    "teamviewer": "TeamViewer.exe",
    "anydesk": "AnyDesk.exe",
}

# Нормализатор русских/вариантных названий → ключ _REMOTE_APPS_DIRECT
_REMOTE_NAME_MAP: dict[str, str] = {
    "teamviewer": "teamviewer",
    "team viewer": "teamviewer",
    "тимвьюер": "teamviewer",
    "тим вьюер": "teamviewer",
    "тим": "teamviewer",
    "anydesk": "anydesk",
    "any desk": "anydesk",
    "аньдеск": "anydesk",
    "ани деск": "anydesk",
    "аниdesc": "anydesk",
}


def find_remote_app_path(name: str) -> str | None:
    """Ищет исполняемый файл приложения удалённого доступа по прямым путям.
    Возвращает путь к .exe если найден, иначе None.
    Работает только на Windows; на других ОС всегда None."""
    if not IS_WINDOWS:
        return None
    key = _REMOTE_NAME_MAP.get(name.strip().lower(), name.strip().lower())
    for path in _REMOTE_APPS_DIRECT.get(key, []):
        if os.path.isfile(path):
            return path
    return None


def is_remote_app_running(name: str) -> bool:
    """Проверяет запущен ли процесс удалённого доступа (только Windows)."""
    if not IS_WINDOWS:
        return False
    key = _REMOTE_NAME_MAP.get(name.strip().lower(), name.strip().lower())
    exe = _REMOTE_APP_EXE.get(key, "")
    if not exe:
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
            text=True, timeout=5, stderr=subprocess.DEVNULL,
        )
        return exe.lower() in out.lower()
    except Exception:
        return False


# ---------- поиск ярлыков ----------

def _shortcut_dirs() -> list[str]:
    dirs: list[str] = []
    if IS_WINDOWS:
        candidates = [
            os.environ.get("ProgramData", r"C:\ProgramData") +
                r"\Microsoft\Windows\Start Menu\Programs",
            os.environ.get("APPDATA", "") +
                r"\Microsoft\Windows\Start Menu\Programs",
            os.path.join(os.path.expanduser("~"), "Desktop"),
            os.path.join(os.path.expanduser("~"), "Рабочий стол"),
            r"C:\Users\Public\Desktop",
        ]
        dirs = [d for d in candidates if d and os.path.isdir(d)]
    elif IS_MAC:
        dirs = ["/Applications", os.path.expanduser("~/Applications")]
        dirs = [d for d in dirs if os.path.isdir(d)]
    return dirs


def _scan_apps() -> dict[str, str]:
    """Вернуть {имя_без_расширения_lowercase: полный_путь_к_ярлыку}."""
    result: dict[str, str] = {}
    exts = (".lnk", ".url") if IS_WINDOWS else (".app",)
    for d in _shortcut_dirs():
        for ext in exts:
            for path in glob.glob(os.path.join(d, "**", "*" + ext),
                                  recursive=True):
                name = os.path.splitext(os.path.basename(path))[0].lower()
                # сохраняем первый найденный (Start Menu приоритетнее Desktop)
                result.setdefault(name, path)
    return result


# Простые алиасы (русский -> часть английского названия)
_ALIASES = {
    "хром": "chrome",
    "хром браузер": "chrome",
    "гугл хром": "chrome",
    "фотошоп": "photoshop",
    "опен": "obs",
    "обс": "obs",
    "телеграм": "telegram",
    "вайбер": "viber",
    "ватсап": "whatsapp",
    "ворд": "word",
    "эксель": "excel",
    "блокнот": "notepad",
    "калькулятор": "calculator",
    "проводник": "explorer",
    "ютуб": "youtube",
    "вс код": "visual studio code",
    "вскод": "visual studio code",
    "терминал": "terminal",
    "повершелл": "powershell",
    "командная строка": "cmd",
    "стим": "steam",
    "дискорд": "discord",
    "спотифай": "spotify",
    "зум": "zoom",
    "скайп": "skype",
    "фигма": "figma",
    "ноушн": "notion",
    "брейв": "brave",
    "файрфокс": "firefox",
    "опера": "opera",
    "эдж": "edge",
    # удалённый доступ
    "тимвьюер": "teamviewer",
    "тим вьюер": "teamviewer",
    "тим": "teamviewer",
    "team viewer": "teamviewer",
    "аньдеск": "anydesk",
    "ани деск": "anydesk",
    "any desk": "anydesk",
}


def _find_best(query: str, apps: dict[str, str]) -> tuple[str, str] | None:
    q = (query or "").strip().lower()
    if not q:
        return None
    q = _ALIASES.get(q, q)

    # 1) точное совпадение
    if q in apps:
        return q, apps[q]
    # 2) подстрока
    matches = [n for n in apps if q in n]
    if matches:
        matches.sort(key=len)  # короткое имя обычно «основное»
        return matches[0], apps[matches[0]]
    # 3) нечёткое
    close = difflib.get_close_matches(q, list(apps.keys()), n=1, cutoff=0.6)
    if close:
        return close[0], apps[close[0]]
    return None


# ---------- запуск ----------

def launch_app(name: str) -> str:
    """Открыть программу по человеческому имени. Возвращает сообщение пользователю."""
    name = (name or "").strip()
    if not name:
        return "❌ Не понял какую программу открыть."

    # Шаг 1: для приложений удалённого доступа сначала ищем по прямым путям Windows
    if IS_WINDOWS:
        direct_path = find_remote_app_path(name)
        if direct_path:
            try:
                os.startfile(direct_path)  # type: ignore[attr-defined]
                app_display = os.path.splitext(os.path.basename(direct_path))[0]
                return f"🚀 Запускаю {app_display}.\n_Путь: {direct_path}_"
            except Exception as e:
                return (
                    f"❌ Нашёл {name} по пути {direct_path}, "
                    f"но не смог запустить: {e}"
                )

    # Шаг 2: поиск по ярлыкам Start Menu / Desktop
    apps = _scan_apps()
    hit = _find_best(name, apps)

    if hit:
        title, path = hit
        try:
            if IS_WINDOWS:
                os.startfile(path)  # type: ignore[attr-defined]
            elif IS_MAC:
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return f"🚀 Открываю «{title}»."
        except Exception as e:
            return f"❌ Не получилось открыть «{title}»: {e}"

    # Шаг 3 (fallback Windows): cmd /c start
    if IS_WINDOWS:
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", _ALIASES.get(name.lower(), name)],
                shell=False,
            )
            return f"🚀 Пробую открыть «{name}» через системный поиск."
        except Exception as e:
            # Если это приложение удалённого доступа — даём понятную подсказку
            norm = _REMOTE_NAME_MAP.get(name.strip().lower(), "")
            if norm in _REMOTE_APPS_DIRECT:
                known_paths = "\n".join(
                    f"• {p}" for p in _REMOTE_APPS_DIRECT[norm]
                )
                return (
                    f"⚠️ {name} не найден ни в одном стандартном месте.\n\n"
                    f"Проверил пути:\n{known_paths}\n\n"
                    f"Что сделать:\n"
                    f"1. Убедись что {name} установлен\n"
                    f"2. Если установлен нестандартно — укажи путь через настройки бота\n"
                    f"3. Скачать: teamviewer.com / anydesk.com"
                )
            return f"❌ Не нашёл «{name}» среди установленных программ ({e})."

    return f"❌ Не нашёл «{name}» среди установленных программ."


# ---------- закрытие ----------

# Маппинг русских/коротких имён -> имя процесса .exe (для Windows taskkill)
_PROC_MAP = {
    "хром": "chrome.exe",
    "гугл хром": "chrome.exe",
    "chrome": "chrome.exe",
    "фотошоп": "photoshop.exe",
    "photoshop": "photoshop.exe",
    "опен": "obs64.exe",
    "обс": "obs64.exe",
    "obs": "obs64.exe",
    "телеграм": "telegram.exe",
    "telegram": "telegram.exe",
    "вайбер": "viber.exe",
    "ватсап": "whatsapp.exe",
    "ворд": "winword.exe",
    "word": "winword.exe",
    "эксель": "excel.exe",
    "excel": "excel.exe",
    "блокнот": "notepad.exe",
    "notepad": "notepad.exe",
    "калькулятор": "calculator.exe",
    "стим": "steam.exe",
    "steam": "steam.exe",
    "дискорд": "discord.exe",
    "discord": "discord.exe",
    "спотифай": "spotify.exe",
    "spotify": "spotify.exe",
    "зум": "zoom.exe",
    "zoom": "zoom.exe",
    "скайп": "skype.exe",
    "файрфокс": "firefox.exe",
    "firefox": "firefox.exe",
    "эдж": "msedge.exe",
    "edge": "msedge.exe",
    "брейв": "brave.exe",
    "опера": "opera.exe",
    "вс код": "code.exe",
    "вскод": "code.exe",
    "vscode": "code.exe",
    "фигма": "figma.exe",
    "ноушн": "notion.exe",
}


def close_app(name: str) -> str:
    name = (name or "").strip().lower()
    if not name:
        return "❌ Не понял какую программу закрыть."

    exe = _PROC_MAP.get(name)
    if not exe:
        # если пользователь сразу написал имя процесса (например, "chrome.exe")
        if name.endswith(".exe"):
            exe = name
        else:
            exe = name + ".exe"

    if IS_WINDOWS:
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/IM", exe],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                return f"✅ Закрыл «{name}» ({exe})."
            return (f"❌ Не получилось закрыть «{name}». "
                    f"taskkill: {(r.stderr or r.stdout).strip()[:200]}")
        except Exception as e:
            return f"❌ Ошибка при закрытии: {e}"

    try:
        subprocess.run(["pkill", "-f", name], timeout=10)
        return f"✅ Послал сигнал закрытия «{name}»."
    except Exception as e:
        return f"❌ Ошибка: {e}"


def list_apps(limit: int = 50) -> str:
    apps = _scan_apps()
    if not apps:
        return "Не нашёл ни одной программы (нет ярлыков в меню Пуск/Desktop?)."
    names = sorted(apps.keys())
    head = names[:limit]
    extra = f"\n…и ещё {len(names) - limit}" if len(names) > limit else ""
    return ("📋 Программы, которые я вижу на твоём ПК "
            f"({len(names)} шт.):\n• " + "\n• ".join(head) + extra)
