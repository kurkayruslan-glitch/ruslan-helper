"""Запуск и закрытие программ на ПК Руслана по человеческим названиям.

Сканирует ярлыки (.lnk) в меню Пуск и на рабочем столе, чтобы найти
программу по нечёткому совпадению («фотошоп» -> «Adobe Photoshop 2024»).
Работает на Windows; на Mac/Linux — упрощённый fallback.
"""

import os
import sys
import subprocess
import glob
import difflib

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


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

    # Fallback: попробовать как команду shell (chrome, notepad, calc и т.д.)
    if IS_WINDOWS:
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", _ALIASES.get(name.lower(), name)],
                shell=False,
            )
            return f"🚀 Пробую открыть «{name}» через системный поиск."
        except Exception as e:
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
