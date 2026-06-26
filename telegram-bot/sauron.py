"""Интеграция с sauron.info.

Приоритет авторизации:
  1. SAURON_API_KEY  — API-ключ → Authorization: Bearer {key}  (основной путь)
  2. SAURON_USERNAME + SAURON_PASSWORD — сессионная авторизация через форму (fallback)

SAURON_API_URL — переопределяет базовый URL API (по умолчанию https://sauron.info/api/v1).

Секреты берутся ТОЛЬКО из переменных окружения.
Никакие credentials не попадают в логи, сообщения или код.
"""
import os
import re
import time
import requests
from urllib.parse import urljoin, urlparse

# ── Опциональный HTML-парсер ──────────────────────────────────────────────────
try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False

# ── Константы ────────────────────────────────────────────────────────────────
_SITE_BASE   = "https://sauron.info"
_API_V1      = "https://sauron.info/api/v1"   # переопределяется через SAURON_API_URL
_TIMEOUT     = 15  # секунд

_SITE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,uk;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Origin": _SITE_BASE,
    "Referer": _SITE_BASE + "/login",
}

_API_HEADERS = {
    "User-Agent": "RuslanHelperBot/1.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# ── Кеш сессии (fallback-режим) ───────────────────────────────────────────────
_session_cache: dict = {}   # {username: (session, timestamp)}
_SESSION_TTL = 30 * 60      # 30 минут


# ═════════════════════════════════════════════════════════════════════════════
# Helpers — чтение конфигурации
# ═════════════════════════════════════════════════════════════════════════════

def _api_base() -> str:
    """Возвращает базовый URL API (из SAURON_API_URL или дефолт)."""
    return os.environ.get("SAURON_API_URL", "").rstrip("/") or _API_V1


def _api_key() -> str:
    return os.environ.get("SAURON_API_KEY", "").strip()


def _credentials() -> tuple[str, str]:
    """Возвращает (username, password) из Secrets или ('', '')."""
    u = (os.environ.get("SAURON_USERNAME") or os.environ.get("SAURON_LOGIN") or "").strip()
    p = os.environ.get("SAURON_PASSWORD", "").strip()
    return u, p


def _is_configured() -> bool:
    """True если хотя бы один способ авторизации настроен."""
    if _api_key():
        return True
    u, p = _credentials()
    return bool(u and p)


# ═════════════════════════════════════════════════════════════════════════════
# Путь 1 — API-ключ (Authorization: Bearer)
# ═════════════════════════════════════════════════════════════════════════════

def _api_request(method: str, endpoint: str, **kwargs) -> dict:
    """
    Выполняет запрос к API с Bearer-авторизацией.
    Возвращает распакованный JSON или выбрасывает RuntimeError с человеческим сообщением.
    Секреты не попадают в исключение.
    """
    key = _api_key()
    headers = {
        **_API_HEADERS,
        "Authorization": f"Bearer {key}",
    }
    url = f"{_api_base()}/{endpoint.lstrip('/')}"

    try:
        resp = requests.request(method, url, headers=headers, timeout=_TIMEOUT, **kwargs)
    except requests.exceptions.Timeout:
        raise RuntimeError("sauron.info не ответил за 15 секунд — попробуй позже.")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Нет соединения с sauron.info — проверь интернет.")
    except Exception as e:
        raise RuntimeError(f"Ошибка сети: {str(e)[:120]}")

    # Парсим JSON-ответ в стиле Telegram Bot API
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"Сервер вернул не JSON (HTTP {resp.status_code}).")

    if data.get("ok"):
        return data.get("result", data)

    code = data.get("error_code", resp.status_code)
    desc = data.get("description", "Неизвестная ошибка")

    # Человеческие сообщения по кодам
    if code in (401, 403):
        raise RuntimeError(
            "Неверный SAURON_API_KEY или доступ запрещён. "
            "Проверь ключ в Replit Secrets."
        )
    if code == 402:
        raise RuntimeError(
            "Недостаточно средств на балансе Sauron. "
            "Пополни аккаунт на sauron.info."
        )
    if code == 429:
        raise RuntimeError("Слишком много запросов — подожди немного и попробуй снова.")
    if code == 404:
        raise RuntimeError(
            f"Метод '{endpoint}' не найден на API. "
            "Уточни SAURON_API_URL или документацию."
        )
    raise RuntimeError(f"API ошибка {code}: {desc}")


# ═════════════════════════════════════════════════════════════════════════════
# Путь 2 — сессионная авторизация через форму (fallback)
# ═════════════════════════════════════════════════════════════════════════════

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_SITE_HEADERS)
    return s


def _login_session(username: str, password: str) -> tuple[requests.Session | None, str]:
    """Авторизуется через веб-форму. Возвращает (session, error_msg)."""
    s = _make_session()
    try:
        s.get(_SITE_BASE + "/login", timeout=_TIMEOUT)
    except Exception as e:
        return None, f"Не удалось открыть sauron.info: {str(e)[:100]}"

    try:
        resp = s.post(
            _SITE_BASE + "/user/authenticateUser",
            data={"login": username, "password": password},
            timeout=_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.Timeout:
        return None, "Сайт не ответил за 15 секунд."
    except requests.exceptions.ConnectionError:
        return None, "Нет соединения с sauron.info."
    except Exception as e:
        return None, f"Ошибка сети: {str(e)[:100]}"

    final_path = urlparse(resp.url).path.rstrip("/")
    if final_path in ("/login", "/user/authenticateUser", ""):
        err = "Неверный логин или пароль"
        if _HAS_BS4:
            soup = BeautifulSoup(resp.text, "html.parser")
            for sel in [".auth-modal-error", ".error", ".alert"]:
                el = soup.select_one(sel)
                if el and el.get_text(strip=True):
                    err = el.get_text(strip=True)
                    break
        return None, err

    return s, ""


def _get_session() -> tuple[requests.Session | None, str]:
    """Возвращает кешированную сессию или создаёт новую."""
    username, password = _credentials()
    if not username or not password:
        return None, (
            "Не настроен ни API-ключ, ни логин/пароль.\n\n"
            "Добавь в Replit Secrets одно из:\n"
            "• SAURON_API_KEY — API-ключ (рекомендуется)\n"
            "• SAURON_USERNAME + SAURON_PASSWORD — логин и пароль"
        )
    cached = _session_cache.get(username)
    if cached:
        sess, ts = cached
        if time.time() - ts < _SESSION_TTL:
            return sess, ""
    sess, err = _login_session(username, password)
    if err:
        return None, f"Не удалось войти: {err}"
    _session_cache[username] = (sess, time.time())
    return sess, ""


def _session_search(sess: requests.Session, query: str) -> str:
    """Поиск через веб-сессию (fallback). Возвращает текст результата."""
    # Сначала пробуем API v1 через сессионные cookies
    try:
        url = f"{_api_base()}/search"
        resp = sess.post(url, json={"query": query}, timeout=_TIMEOUT)
        try:
            data = resp.json()
            if data.get("ok") and data.get("result"):
                return _format_api_result(data["result"], query)
        except Exception:
            pass
    except Exception:
        pass

    # Fallback — HTML-поиск на сайте
    for candidate_path, param, method in [
        ("/search",          "q",     "get"),
        ("/persons/search",  "query", "post"),
        ("/find",            "q",     "get"),
    ]:
        try:
            url = _SITE_BASE + candidate_path
            if method == "post":
                resp = sess.post(url, data={param: query}, timeout=_TIMEOUT, allow_redirects=True)
            else:
                resp = sess.get(url, params={param: query}, timeout=_TIMEOUT, allow_redirects=True)

            # Если редирект на логин — сессия протухла
            if urlparse(resp.url).path.rstrip("/") == "/login":
                return ""   # вызывающий код переавторизует

            if resp.status_code == 200 and len(resp.text) > 200:
                return _parse_html(resp.text, query)
        except Exception:
            continue

    return f"🔍 По запросу «{query}» ничего не найдено (HTML-fallback)."


# ═════════════════════════════════════════════════════════════════════════════
# Форматирование результатов
# ═════════════════════════════════════════════════════════════════════════════

def _format_api_result(result, query: str) -> str:
    """Форматирует результат из API (dict/list) в читаемый текст."""
    if not result:
        return f"По запросу «{query}» ничего не найдено."

    if isinstance(result, list):
        lines = []
        for i, item in enumerate(result[:15], 1):
            if isinstance(item, dict):
                parts = []
                for key in ("name", "full_name", "phone", "address", "inn", "email", "info"):
                    val = item.get(key)
                    if val:
                        parts.append(f"{key}: {val}")
                lines.append(f"{i}. " + " | ".join(parts) if parts else f"{i}. {item}")
            else:
                lines.append(f"{i}. {item}")
        text = "\n".join(lines)
        if len(result) > 15:
            text += f"\n…ещё {len(result) - 15} результатов"
        return text

    if isinstance(result, dict):
        lines = []
        for k, v in result.items():
            if v and k not in ("id", "_id"):
                lines.append(f"*{k}*: {v}")
        return "\n".join(lines) or str(result)

    return str(result)[:2000]


def _parse_html(html: str, query: str) -> str:
    """Парсит HTML страницы результатов."""
    if not html.strip():
        return f"По запросу «{query}» пустой ответ."

    if _HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "head", "nav", "footer"]):
            tag.decompose()
        block = None
        for sel in [".results", ".search-results", ".persons", ".items",
                    "#results", "main", "article", ".content", ".list", "table"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(separator="\n", strip=True)
                if len(t) > 50:
                    block = t
                    break
        if not block:
            body = soup.find("body")
            block = body.get_text(separator="\n", strip=True) if body else soup.get_text(separator="\n", strip=True)
        nav_words = {"войти", "выйти", "главная", "меню", "login", "logout", "home", "search", "найти", "назад"}
        lines = [l.strip() for l in block.splitlines()
                 if l.strip() and len(l.strip()) > 2 and l.strip().lower() not in nav_words]
        if not lines:
            return f"По запросу «{query}» ничего не найдено."
        result = "\n".join(lines[:40])
        return result[:3000] + ("…" if len(result) > 3000 else "")
    else:
        clean = re.sub(r"<[^>]+>", " ", html)
        clean = re.sub(r"\s{2,}", "\n", clean).strip()
        lines = [l.strip() for l in clean.splitlines() if len(l.strip()) > 3]
        return "\n".join(lines[:30])[:2500] or f"По запросу «{query}» ничего не найдено."


# ═════════════════════════════════════════════════════════════════════════════
# Публичные функции
# ═════════════════════════════════════════════════════════════════════════════

def search(query: str) -> str:
    """
    Ищет запрос через Sauron. Приоритет: API-ключ → логин/пароль.
    Возвращает Markdown-текст для Telegram.
    """
    query = (query or "").strip()
    if not query:
        return "❓ Что искать? Укажи ФИО, номер телефона, адрес или ИНН."

    if not _is_configured():
        return (
            "🔍 *Sauron не настроен*\n\n"
            "Добавь в Replit Secrets хотя бы одно из:\n"
            "• `SAURON_API_KEY` — API-ключ _(рекомендуется)_\n"
            "• `SAURON_USERNAME` + `SAURON_PASSWORD` — логин и пароль\n\n"
            "После добавления перезапусти бота.\n"
            "_Ключи и пароли в Telegram не присылай._"
        )

    header = f"🔍 *Sauron: «{query}»*\n{'━' * 22}\n"

    # ── Путь 1: API-ключ ──────────────────────────────────────────────────
    key = _api_key()
    if key:
        # Пробуем наиболее вероятные endpoint-ы для поиска
        search_endpoints = [
            ("POST", "search",         {"query": query}),
            ("POST", "search",         {"q": query}),
            ("GET",  "search",         {"query": query}),
            ("GET",  "search",         {"q": query}),
            ("POST", "persons/search", {"query": query}),
            ("POST", "find",           {"query": query}),
        ]
        last_err = ""
        for method, ep, body in search_endpoints:
            try:
                if method == "POST":
                    result = _api_request("POST", ep, json=body)
                else:
                    result = _api_request("GET", ep, params=body)
                formatted = _format_api_result(result, query)
                return header + formatted
            except RuntimeError as e:
                msg = str(e)
                last_err = msg
                # Если ошибка авторизации или баланс — не перебираем другие endpoints
                if any(kw in msg for kw in ("API-ключ", "баланс", "запросов", "Secrets")):
                    return f"🔍 Sauron\n\n⚠️ {msg}"
                # 404 = endpoint не тот, пробуем следующий
                continue
            except Exception as e:
                last_err = str(e)[:120]
                continue

        # Все стандартные endpoint-ы дали 404 — ключ рабочий, но endpoint неизвестен
        return (
            f"🔍 *Sauron: «{query}»*\n\n"
            "⚠️ API-ключ настроен, но поисковый endpoint неизвестен.\n\n"
            "Уточни документацию sauron.info и задай правильный URL через:\n"
            "`SAURON_API_URL` в Replit Secrets\n\n"
            f"_Последняя ошибка: {last_err}_"
        )

    # ── Путь 2: сессионная авторизация (fallback) ─────────────────────────
    sess, err = _get_session()
    if err:
        return f"🔍 *Sauron*\n\n⚠️ {err}"

    result_text = _session_search(sess, query)

    # Сессия протухла — одна попытка переавторизоваться
    if not result_text:
        username, _ = _credentials()
        _session_cache.pop(username, None)
        sess, err = _get_session()
        if err:
            return f"🔍 *Sauron*\n\n⚠️ Сессия истекла, переавторизация не удалась: {err}"
        result_text = _session_search(sess, query)

    if not result_text:
        return header + f"По запросу «{query}» ничего не найдено."

    return header + result_text


def get_balance() -> str:
    """Проверяет баланс через API (только если настроен API-ключ)."""
    if not _api_key():
        return "⚠️ Баланс доступен только через API-ключ (SAURON_API_KEY)."
    try:
        result = _api_request("GET", "balance")
        if isinstance(result, dict):
            bal = result.get("balance") or result.get("amount") or result.get("credits")
            if bal is not None:
                return f"💰 Баланс Sauron: *{bal}*"
        return f"💰 Баланс: {result}"
    except RuntimeError as e:
        return f"⚠️ {e}"
    except Exception as e:
        return f"⚠️ Ошибка баланса: {str(e)[:100]}"


def status() -> str:
    """Статус интеграции — без показа значений secrets."""
    key = _api_key()
    username, password = _credentials()
    api_url = os.environ.get("SAURON_API_URL", "")

    if key:
        url_note = f" · URL: {api_url}" if api_url else ""
        # Статус сессии-кеша не применим для API-режима
        return f"✅ API-ключ настроен{url_note}"

    if username and password:
        cached = _session_cache.get(username)
        if cached:
            _, ts = cached
            age = int(time.time() - ts)
            if age < _SESSION_TTL:
                return f"✅ Логин/пароль настроены · сессия {age // 60}м {age % 60}с назад"
        return "✅ Логин/пароль настроены · сессия не активна"

    if username and not password:
        return "⚠️ SAURON_USERNAME задан, но SAURON_PASSWORD отсутствует"
    if not username and password:
        return "⚠️ SAURON_PASSWORD задан, но SAURON_USERNAME отсутствует"

    return "⚠️ Не настроен — добавь SAURON_API_KEY в Replit Secrets"
