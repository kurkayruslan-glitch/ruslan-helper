"""Интеграция с sauron.info — авторизация и поиск.

Логин/пароль берутся ТОЛЬКО из переменных окружения SAURON_LOGIN и SAURON_PASSWORD.
Ключи не хранятся в коде. Сессия кешируется в памяти процесса.
"""
import os
import re
import time
import requests
from urllib.parse import quote_plus, urljoin, urlparse

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False

BASE_URL = "https://sauron.info"
LOGIN_URL = f"{BASE_URL}/login"
AUTH_URL  = f"{BASE_URL}/user/authenticateUser"
TIMEOUT   = 15

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,uk;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Origin": BASE_URL,
    "Referer": LOGIN_URL,
}

# Кеш сессии: {login_key: (session, ts)}  — живёт 30 минут
_session_cache: dict = {}
_SESSION_TTL = 30 * 60  # секунд


def _get_credentials() -> tuple[str, str]:
    """Возвращает (login, password) из Replit Secrets или ('', '')."""
    login = os.environ.get("SAURON_LOGIN", "").strip()
    pwd   = os.environ.get("SAURON_PASSWORD", "").strip()
    return login, pwd


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _login(login: str, password: str) -> tuple[requests.Session | None, str]:
    """Создаёт авторизованную сессию. Возвращает (session, error_msg)."""
    s = _make_session()
    try:
        # Сначала загружаем страницу логина, чтобы получить cookies если есть
        s.get(LOGIN_URL, timeout=TIMEOUT)
    except Exception as e:
        return None, f"Не удалось открыть sauron.info: {e}"

    try:
        resp = s.post(
            AUTH_URL,
            data={"login": login, "password": password},
            timeout=TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.Timeout:
        return None, "Сайт не ответил за 15 секунд — попробуй позже."
    except requests.exceptions.ConnectionError:
        return None, "Нет соединения с sauron.info — проверь интернет."
    except Exception as e:
        return None, f"Ошибка сети: {str(e)[:120]}"

    # Если вернулись на /login — авторизация не прошла
    final_path = urlparse(resp.url).path.rstrip("/")
    if final_path in ("/login", "/user/authenticateUser", ""):
        # Попробуем извлечь сообщение об ошибке из HTML
        err_text = ""
        if _HAS_BS4:
            soup = BeautifulSoup(resp.text, "html.parser")
            for sel in [".auth-modal-error", ".error", ".alert", ".toast-card--error"]:
                el = soup.select_one(sel)
                if el and el.get_text(strip=True):
                    err_text = el.get_text(strip=True)
                    break
        if not err_text:
            m = re.search(r"(Неверн[а-яё]+\s+\w+|Invalid\s+\w+|incorrect\s+\w+|wrong\s+\w+)", resp.text, re.I)
            err_text = m.group(0) if m else "Неверный логин или пароль"
        return None, err_text

    return s, ""


def _get_session() -> tuple[requests.Session | None, str]:
    """Возвращает кешированную сессию или создаёт новую."""
    login, pwd = _get_credentials()
    if not login or not pwd:
        return None, (
            "Поиск через Sauron не настроен.\n\n"
            "Добавь в Replit Secrets:\n"
            "  SAURON_LOGIN — твой логин на sauron.info\n"
            "  SAURON_PASSWORD — твой пароль\n\n"
            "После добавления перезапусти бота."
        )

    cache_key = login
    cached = _session_cache.get(cache_key)
    if cached:
        sess, ts = cached
        if time.time() - ts < _SESSION_TTL:
            return sess, ""

    sess, err = _login(login, pwd)
    if err:
        return None, f"Не удалось войти на sauron.info: {err}"

    _session_cache[cache_key] = (sess, time.time())
    return sess, ""


def _discover_search_url(sess: requests.Session) -> tuple[str, str, str]:
    """
    Пытается найти поисковый endpoint на главной странице после авторизации.
    Возвращает (method, url, query_param).
    """
    try:
        r = sess.get(BASE_URL + "/", timeout=TIMEOUT)
        home_url = r.url

        if _HAS_BS4:
            soup = BeautifulSoup(r.text, "html.parser")
            # Ищем форму с поиском
            for form in soup.find_all("form"):
                inputs = form.find_all("input")
                param_names = [i.get("name", "") for i in inputs]
                # Ищем поле типа text/search с поисковым названием
                search_fields = [
                    n for n in param_names
                    if n and any(kw in n.lower() for kw in ("q", "query", "search", "find", "term", "s"))
                ]
                if search_fields:
                    action = form.get("action", "")
                    method = form.get("method", "get").lower()
                    full_url = urljoin(home_url, action) if action else home_url
                    return method, full_url, search_fields[0]
        else:
            # Regex fallback
            forms = re.findall(
                r'<form[^>]*action=["\']([^"\']*)["\'][^>]*method=["\'](\w+)["\']',
                r.text, re.I
            )
            for action, method in forms:
                full_url = urljoin(home_url, action)
                return method, full_url, "q"

    except Exception:
        pass

    # Fallback — пробуем стандартные эндпоинты
    for candidate in ["/search", "/find", "/persons/search", "/query"]:
        try:
            test_r = sess.get(
                BASE_URL + candidate + "?q=test",
                timeout=8, allow_redirects=False
            )
            if test_r.status_code in (200, 301, 302) and test_r.status_code != 404:
                return "get", BASE_URL + candidate, "q"
        except Exception:
            continue

    return "get", BASE_URL + "/search", "q"


def _parse_results(html: str, query: str) -> str:
    """Парсит HTML страницы результатов и возвращает форматированный текст."""
    if not html.strip():
        return f"🔍 Пустой ответ по запросу «{query}»."

    if _HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")

        # Убираем скрипты и стили
        for tag in soup(["script", "style", "noscript", "head"]):
            tag.decompose()

        # Ищем секцию результатов по типичным классам/тегам
        results_block = None
        for sel in [
            ".results", ".search-results", ".persons", ".items",
            "#results", "#search-results", "main", "article",
            ".content", ".list", ".data-table", "table",
        ]:
            block = soup.select_one(sel)
            if block:
                text = block.get_text(separator="\n", strip=True)
                if len(text) > 50:
                    results_block = text
                    break

        if not results_block:
            # Берём весь body
            body = soup.find("body")
            results_block = body.get_text(separator="\n", strip=True) if body else soup.get_text(separator="\n", strip=True)

        # Чистим многократные пустые строки
        lines = [l.strip() for l in results_block.splitlines() if l.strip()]
        # Убираем навигационный мусор (короткие строки с типичными словами)
        nav_words = {"войти", "выйти", "главная", "меню", "login", "logout", "home", "search", "найти", "назад"}
        lines = [l for l in lines if len(l) > 2 and l.lower() not in nav_words]

        if not lines:
            return f"🔍 По запросу «{query}» ничего не найдено."

        # Ограничиваем длину
        result_text = "\n".join(lines[:40])
        if len(result_text) > 3000:
            result_text = result_text[:3000] + "\n…(обрезано)"
        return result_text

    else:
        # Без BS4 — грубая очистка тегов
        clean = re.sub(r"<[^>]+>", " ", html)
        clean = re.sub(r"\s{2,}", "\n", clean).strip()
        lines = [l.strip() for l in clean.splitlines() if len(l.strip()) > 3]
        if not lines:
            return f"🔍 По запросу «{query}» ничего не найдено."
        return "\n".join(lines[:30])[:2500]


def search(query: str) -> str:
    """
    Главная функция: авторизуется (или использует кеш) и ищет по запросу.
    Возвращает отформатированный текст для отправки в Telegram.
    """
    query = (query or "").strip()
    if not query:
        return "❓ Что искать? Укажи запрос — ФИО, номер телефона, адрес."

    # Получаем сессию
    sess, err = _get_session()
    if err:
        return f"🔍 Sauron\n\n{err}"

    # Находим поисковый URL
    try:
        method, search_url, param = _discover_search_url(sess)
    except Exception as e:
        return f"⚠️ Не удалось определить поисковый URL: {e}"

    # Выполняем поиск
    try:
        if method == "post":
            resp = sess.post(search_url, data={param: query}, timeout=TIMEOUT)
        else:
            resp = sess.get(search_url, params={param: query}, timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        return "⏳ sauron.info не ответил за 15 секунд — попробуй позже."
    except requests.exceptions.ConnectionError:
        return "❌ Нет соединения с sauron.info."
    except Exception as e:
        return f"❌ Ошибка при поиске: {str(e)[:120]}"

    # Если редирект на логин — сессия протухла, чистим кеш и пробуем ещё раз
    final_path = urlparse(resp.url).path.rstrip("/")
    if final_path == "/login":
        login, _ = _get_credentials()
        _session_cache.pop(login, None)
        sess, err = _get_session()
        if err:
            return f"🔍 Сессия истекла, не удалось переавторизоваться: {err}"
        try:
            if method == "post":
                resp = sess.post(search_url, data={param: query}, timeout=TIMEOUT)
            else:
                resp = sess.get(search_url, params={param: query}, timeout=TIMEOUT)
        except Exception as e:
            return f"❌ Ошибка после переавторизации: {str(e)[:120]}"

    # Парсим результаты
    parsed = _parse_results(resp.text, query)

    header = f"🔍 *Sauron: «{query}»*\n{'━' * 20}\n"
    footer = f"\n\n🌐 [Открыть на сайте]({resp.url})"
    return header + parsed + footer


def status() -> str:
    """Проверяет настройку без показа credentials."""
    login, pwd = _get_credentials()
    if not login:
        return "⚠️ SAURON_LOGIN не задан в Replit Secrets"
    if not pwd:
        return "⚠️ SAURON_PASSWORD не задан в Replit Secrets"

    cache_key = login
    cached = _session_cache.get(cache_key)
    if cached:
        sess, ts = cached
        age = int(time.time() - ts)
        if age < _SESSION_TTL:
            return f"✅ Авторизован (сессия {age//60}м {age%60}с назад)"

    return f"✅ Логин настроен · сессия не активна (авторизация при первом поиске)"
