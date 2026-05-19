"""Управление локальным ПК (только при локальном запуске на Windows/Mac/Linux).

Возможности:
- open_url(url): открыть сайт в дефолтном браузере.
- search_files(query, by_content=False, limit=15): найти файлы по имени или по содержимому
  в списке папок из PC_SEARCH_DIRS (по умолчанию Desktop, Documents, Downloads).
- open_folder(path): открыть папку в проводнике.

Все функции возвращают строку, готовую к отправке пользователю в Telegram.
На сервере (Replit) функции работают, но искать ничего не найдут — это окей.
"""
import os
import platform
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import urlparse


def _default_search_dirs() -> list[str]:
    custom = os.environ.get("PC_SEARCH_DIRS", "").strip()
    if custom:
        return [d.strip() for d in custom.split(os.pathsep) if d.strip()]
    home = Path.home()
    candidates = [home / "Desktop", home / "Documents", home / "Downloads"]
    return [str(p) for p in candidates if p.exists()]


# Папки, которые пропускаем при обходе — мусор и системные
_SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".idea", ".vscode",
    "AppData", "Library", ".cache", ".npm", ".cargo", "dist", "build",
    "$RECYCLE.BIN", "System Volume Information",
}
_MAX_FILES_SCANNED = 200_000
_MAX_CONTENT_FILE_BYTES = 2 * 1024 * 1024
_CONTENT_EXTS = {".txt", ".md", ".log", ".csv", ".json", ".py", ".js", ".ts",
                 ".html", ".css", ".yml", ".yaml", ".ini", ".cfg", ".rtf"}


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not urlparse(url).scheme:
        # Голый запрос без http/https — открываем в поиске Google
        if "." in url and " " not in url:
            return "https://" + url
        from urllib.parse import quote_plus
        return "https://www.google.com/search?q=" + quote_plus(url)
    return url


def open_url(url: str) -> str:
    """Открывает URL в браузере по умолчанию. Возвращает текст для пользователя."""
    norm = _normalize_url(url)
    if not norm:
        return "❌ Не понял какой сайт открыть."
    try:
        webbrowser.open(norm, new=2)
        return f"🌐 Открываю: {norm}"
    except Exception as e:
        return f"❌ Не смог открыть браузер: {e}"


def _iter_files(roots: list[str]):
    scanned = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                if scanned >= _MAX_FILES_SCANNED:
                    return
                scanned += 1
                yield os.path.join(dirpath, fname)


def search_files(query: str, by_content: bool = False, limit: int = 15) -> str:
    query = (query or "").strip()
    if not query:
        return "❌ Не понял что искать."
    roots = _default_search_dirs()
    if not roots:
        return "❌ Не задано где искать (нет Desktop/Documents/Downloads и не задан PC_SEARCH_DIRS)."

    needle = query.lower()
    matches: list[str] = []

    try:
        if by_content:
            for path in _iter_files(roots):
                ext = os.path.splitext(path)[1].lower()
                if ext not in _CONTENT_EXTS:
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size == 0 or size > _MAX_CONTENT_FILE_BYTES:
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    continue
                if needle in content.lower():
                    matches.append(path)
                    if len(matches) >= limit:
                        break
        else:
            for path in _iter_files(roots):
                if needle in os.path.basename(path).lower():
                    matches.append(path)
                    if len(matches) >= limit:
                        break
    except Exception as e:
        return f"❌ Ошибка поиска: {e}"

    mode = "по содержимому" if by_content else "по имени"
    if not matches:
        return f"🔍 Ничего не нашёл {mode} по запросу «{query}» в:\n" + "\n".join(f"• `{r}`" for r in roots)

    lines = [f"🔍 Нашёл {len(matches)} файл(ов) {mode} «{query}»:\n"]
    for p in matches:
        try:
            sz = os.path.getsize(p)
            sz_h = f"{sz // 1024} КБ" if sz >= 1024 else f"{sz} Б"
        except OSError:
            sz_h = "?"
        lines.append(f"• `{p}`  _({sz_h})_")
    return "\n".join(lines)


def open_folder(path: str) -> str:
    """Открывает папку в проводнике (или папку файла, если передан файл)."""
    path = (path or "").strip().strip('"').strip("'")
    if not path or not os.path.exists(path):
        return f"❌ Нет такого пути: `{path}`"
    if os.path.isfile(path):
        target = os.path.dirname(path) or "."
    else:
        target = path
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(target)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return f"📂 Открыл: `{target}`"
    except Exception as e:
        return f"❌ Не смог открыть проводник: {e}"
