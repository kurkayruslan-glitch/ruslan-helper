"""
Parser for 200.zona.media — база погибших российских солдат.
Контакты родственников: поле "Источник" ведёт на архивы постов в ВКонтакте и др. соцсетях.
"""
import os
import re
import time
import threading
import requests
from urllib.parse import unquote
from html.parser import HTMLParser

INDEX_FILE = "zona_index.txt"
INDEX_LOCK = threading.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru,en;q=0.9",
}

SITEMAP_COUNT = 22  # sitemap-persons-0001..0022


# ──────────────────────────────────────────────────────────
# INDEX: кеш всех URL из сitemaps
# ──────────────────────────────────────────────────────────

def _download_sitemap(n: int) -> list[str]:
    url = f"https://200.zona.media/sitemap/sitemap-persons-{n:04d}.xml"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return re.findall(r"<loc>([^<]+\.html)</loc>", r.text)
    except Exception:
        return []


def build_index(progress_cb=None) -> int:
    """Скачивает все sitemaps и сохраняет URL в локальный файл. Возвращает количество записей."""
    all_urls = []
    for i in range(1, SITEMAP_COUNT + 1):
        urls = _download_sitemap(i)
        all_urls.extend(urls)
        if progress_cb:
            progress_cb(i, SITEMAP_COUNT, len(all_urls))
    with INDEX_LOCK:
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(all_urls))
    return len(all_urls)


def index_exists() -> bool:
    return os.path.exists(INDEX_FILE) and os.path.getsize(INDEX_FILE) > 1000


def index_size() -> int:
    if not index_exists():
        return 0
    with open(INDEX_FILE, encoding="utf-8") as f:
        return sum(1 for _ in f)


# ──────────────────────────────────────────────────────────
# SEARCH: поиск по фамилии/имени в индексе
# ──────────────────────────────────────────────────────────

def search_person(query: str, limit: int = 10) -> list[dict]:
    """
    Ищет человека в индексе URL по фамилии/имени.
    Возвращает список {"name": ..., "url": ..., "region": ..., "type": ...}
    """
    if not index_exists():
        return []

    # Нормализуем запрос: убираем лишние пробелы, разбиваем на слова
    words = [w.strip().lower() for w in query.split() if len(w) >= 2]
    if not words:
        return []

    results = []
    seen_urls = set()

    with INDEX_LOCK:
        with open(INDEX_FILE, encoding="utf-8") as f:
            for line in f:
                url = line.strip()
                if not url:
                    continue
                # Декодируем URL для поиска по-русски
                decoded = unquote(url).replace("_", " ").lower()
                # Проверяем что все слова запроса есть в URL
                if all(w in decoded for w in words):
                    if url not in seen_urls:
                        seen_urls.add(url)
                        info = _parse_url(url)
                        results.append(info)
                        if len(results) >= limit:
                            break

    return results


def _parse_url(url: str) -> dict:
    """Извлекает имя, регион, тип из URL страницы."""
    decoded = unquote(url)
    # URL: https://200.zona.media/{region}/{type}/{name}.html
    path = decoded.replace("https://200.zona.media/", "").rstrip(".html")
    parts = path.split("/")
    name = parts[-1].replace("_", " ") if parts else ""
    region = parts[0].replace("_", " ") if len(parts) > 0 else ""
    mil_type = parts[1].replace("_", " ") if len(parts) > 1 else ""
    return {"name": name, "region": region, "type": mil_type, "url": url}


# ──────────────────────────────────────────────────────────
# PAGE PARSER: извлекает всё с страницы человека
# ──────────────────────────────────────────────────────────

class _PersonPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.data = {}
        self._current_key = None
        self._current_val = []
        self._in_card = False
        self._key_next = False
        self._all_links = []
        self._depth = 0
        self._title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = attrs_dict.get("href", "")
            if href and href.startswith("http"):
                self._all_links.append(href)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title and not self._title:
            self._title = text


def fetch_person_info(url: str) -> dict | None:
    """Загружает страницу и извлекает все данные о человеке."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        return {"error": str(e)}

    result = {}

    # Заголовок
    title_m = re.search(r"<title>([^<]+)</title>", html)
    if title_m:
        result["title"] = title_m.group(1).strip()

    # Основные поля через паттерн "ключ / значение" из lite-html
    # Структура: <dt>Регион</dt><dd>...</dd> или просто последовательные теги
    dt_dd = re.findall(r"<dt[^>]*>([^<]+)</dt>\s*<dd[^>]*>([\s\S]*?)</dd>", html)
    for key, val in dt_dd:
        val_clean = re.sub(r"<[^>]+>", " ", val).strip()
        val_clean = re.sub(r"\s+", " ", val_clean)
        result[key.strip()] = val_clean

    # Если нет dt/dd — парсим структурированные блоки
    if not dt_dd:
        # Ищем паттерны "Ключ\n\nЗначение"
        text = re.sub(r"<[^>]+>", "\n", html)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        keys_map = {
            "Регион": "Регион",
            "Населенный пункт": "Нас. пункт",
            "Род войск": "Род войск",
            "Источник": "Источник",
            "Дата гибели": "Дата гибели",
        }
        for i, line in enumerate(lines):
            for k in keys_map:
                if line.strip() == k and i + 1 < len(lines):
                    result[k] = lines[i + 1]

    # Все внешние ссылки (особенно archive.ph → ВКонтакте и др.)
    all_links = re.findall(r'href="(https?://[^"]+)"', html)
    source_links = []
    vk_links = []
    archive_links = []

    for link in all_links:
        if "200.zona.media" in link or "zona.media" in link:
            continue
        if "google" in link or "gtm" in link or "favicon" in link:
            continue
        if "vk.com" in link or "vkontakte" in link:
            vk_links.append(link)
        elif "archive.ph" in link or "archive.org" in link or "web.archive" in link:
            archive_links.append(link)
        else:
            source_links.append(link)

    if vk_links:
        result["ВКонтакте"] = vk_links
    if archive_links:
        result["Архив (источник)"] = archive_links
    if source_links:
        result["Другие ссылки"] = [l for l in source_links if not any(
            skip in l for skip in ["airtable", "report", "googletagmanager"]
        )]

    result["url"] = url
    return result


# ──────────────────────────────────────────────────────────
# MAIN FUNCTION для бота
# ──────────────────────────────────────────────────────────

def zona_search(query: str, limit: int = 5) -> str:
    """Поиск человека, возвращает текстовый результат для Telegram."""
    if not index_exists():
        return "⚠️ Индекс базы ещё не загружен. Запусти команду «/zona_build» для загрузки (займёт 2-3 минуты)."

    results = search_person(query, limit=limit)
    if not results:
        return f"🔍 По запросу «{query}» ничего не найдено в базе 200.zona.media"

    lines = [f"🔍 *Найдено {len(results)} чел. по «{query}»:*\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"*{i}. {r['name']}*")
        if r["region"]:
            lines.append(f"   📍 {r['region']}")
        if r["type"]:
            lines.append(f"   🪖 {r['type']}")
        lines.append(f"   🔗 {r['url']}")
        lines.append("")

    lines.append("_Для подробностей и контактов родственников скажи номер записи_")
    return "\n".join(lines)


def zona_detail(query: str, limit: int = 5) -> tuple[str, list[dict]]:
    """Поиск + детали первого результата."""
    results = search_person(query, limit=limit)
    if not results:
        return f"🔍 По запросу «{query}» ничего не найдено.", []

    first = results[0]
    info = fetch_person_info(first["url"])
    if not info or "error" in info:
        return f"⚠️ Не удалось загрузить страницу: {info.get('error', '?')}", results

    lines = [f"👤 *{first['name']}*\n"]

    for key in ["Регион", "Нас. пункт", "Населенный пункт", "Род войск", "Дата гибели"]:
        if key in info:
            lines.append(f"• {key}: {info[key]}")

    if info.get("ВКонтакте"):
        lines.append("\n📱 *ВКонтакте (родственники):*")
        for link in info["ВКонтакте"][:5]:
            lines.append(f"  {link}")

    if info.get("Архив (источник)"):
        lines.append("\n🗄 *Источники (архивы постов):*")
        for link in info["Архив (источник)"][:3]:
            lines.append(f"  {link}")

    if info.get("Другие ссылки"):
        valid = [l for l in info["Другие ссылки"] if len(l) < 200][:3]
        if valid:
            lines.append("\n🔗 *Другие ссылки:*")
            for link in valid:
                lines.append(f"  {link}")

    if len(results) > 1:
        lines.append(f"\n_Ещё {len(results) - 1} совпадений — уточни имя для точного поиска_")

    return "\n".join(lines), results
