"""Поиск цен на товары.

Основной источник — Hotline.ua (украинский агрегатор цен по магазинам).
Если парсинг ломается (Hotline меняет вёрстку или ставит защиту) —
возвращаем прямые ссылки на поиск в популярных маркетплейсах.
"""
import re
import requests
from urllib.parse import quote_plus

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _fallback_links(query: str, q: str) -> str:
    return (
        f"🔍 Готовые поиски по «{query}» — открой и сравни сам:\n"
        f"• Hotline (агрегатор): https://hotline.ua/sr/?q={q}\n"
        f"• Rozetka: https://rozetka.com.ua/ua/search/?text={q}\n"
        f"• Prom: https://prom.ua/ua/search?search_term={q}\n"
        f"• OLX: https://www.olx.ua/list/q-{q}/"
    )


def search_prices(query: str, limit: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "❌ Не понял какой товар искать."
    q = quote_plus(query)

    if not _HAS_BS4:
        return (
            "⚠️ Парсер цен не установлен (нет beautifulsoup4). "
            f"Поставь через pip: `pip install beautifulsoup4 lxml`.\n\n"
            + _fallback_links(query, q)
        )

    try:
        r = requests.get(f"https://hotline.ua/sr/?q={q}", headers=HEADERS, timeout=15)
        if r.status_code != 200 or not r.text:
            return f"🔍 Hotline не ответил ({r.status_code}).\n\n" + _fallback_links(query, q)
        soup = BeautifulSoup(r.text, "html.parser")

        # Hotline периодически меняет селекторы — пробуем несколько паттернов
        cards = []
        for sel in [
            ".list-item",
            "div[data-product-id]",
            "div.list__item",
            "article",
        ]:
            found = soup.select(sel)
            if found:
                cards = found
                break

        results = []
        for card in cards:
            if len(results) >= limit:
                break
            # Имя товара
            name_el = (
                card.select_one(".item-title a")
                or card.select_one(".list-item__title a")
                or card.select_one("a.list-item__title-link")
                or card.select_one("h3 a")
                or card.select_one("a[href*='/sp']")
            )
            name = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue
            # Цена
            price_el = (
                card.select_one(".item-price")
                or card.select_one(".list-item__value-price")
                or card.select_one("[class*='price']")
            )
            price = "?"
            if price_el:
                m = re.search(r"(\d[\d\s]{1,8})", price_el.get_text(" ", strip=True))
                if m:
                    price = m.group(1).replace("\xa0", "").replace(" ", "") + " грн"
            # Ссылка
            link = name_el.get("href") if name_el else ""
            if link and link.startswith("/"):
                link = "https://hotline.ua" + link
            results.append((name[:70], price, link))

        if not results:
            return (
                f"🔍 На Hotline по «{query}» не вышло автоматически распарсить.\n\n"
                + _fallback_links(query, q)
            )

        lines = [f"💰 Топ-{len(results)} по запросу «{query}» (Hotline):\n"]
        for i, (name, price, link) in enumerate(results, 1):
            lines.append(f"{i}. {name} — {price}")
            if link:
                lines.append(f"   {link}")
        lines.append(f"\nВсе варианты: https://hotline.ua/sr/?q={q}")
        return "\n".join(lines)

    except requests.exceptions.Timeout:
        return f"⏳ Hotline думает слишком долго.\n\n" + _fallback_links(query, q)
    except Exception as e:
        return f"❌ Ошибка поиска: {str(e)[:120]}\n\n" + _fallback_links(query, q)
