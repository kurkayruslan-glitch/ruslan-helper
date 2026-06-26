"""Интеграция с sauron.info API.

Авторизация: query-параметр ?token={SAURON_API_KEY} (единственный рабочий формат).
Поиск:  POST /api/v1/search  — form-data  query={запрос}
Баланс: GET  /api/v1/balance

Все credentials только из переменных окружения. Не хранить и не выводить.

Fallback (если нет API-ключа): сессионная авторизация через форму
  SAURON_USERNAME + SAURON_PASSWORD
"""
import os
import re
import time
import requests
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

# ── Константы ────────────────────────────────────────────────────────────────
_SITE_BASE   = "https://sauron.info"
_API_DEFAULT = "https://sauron.info/api/v1"
_TIMEOUT     = 15

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,uk;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

_API_HEADERS = {
    "User-Agent": "RuslanHelperBot/1.0",
    "Accept": "application/json",
}

# Кеш сессии для fallback
_session_cache: dict = {}
_SESSION_TTL = 30 * 60


# ═════════════════════════════════════════════════════════════════════════════
# Конфигурация
# ═════════════════════════════════════════════════════════════════════════════

def _api_base() -> str:
    return os.environ.get("SAURON_API_URL", "").rstrip("/") or _API_DEFAULT


def _api_key() -> str:
    return os.environ.get("SAURON_API_KEY", "").strip()


def _credentials() -> tuple[str, str]:
    u = (os.environ.get("SAURON_USERNAME") or os.environ.get("SAURON_LOGIN") or "").strip()
    p = os.environ.get("SAURON_PASSWORD", "").strip()
    return u, p


def _is_configured() -> bool:
    if _api_key():
        return True
    u, p = _credentials()
    return bool(u and p)


# ═════════════════════════════════════════════════════════════════════════════
# Путь 1 — API-ключ  (?token=KEY, form-data)
# ═════════════════════════════════════════════════════════════════════════════

def _api_get(endpoint: str, extra_params: dict | None = None) -> dict:
    """GET запрос к API. Возвращает result-часть ответа или бросает RuntimeError."""
    key = _api_key()
    params = {"token": key, **(extra_params or {})}
    url = f"{_api_base()}/{endpoint.lstrip('/')}"
    try:
        resp = requests.get(url, headers=_API_HEADERS, params=params, timeout=_TIMEOUT)
    except requests.exceptions.Timeout:
        raise RuntimeError("sauron.info не ответил за 15 секунд.")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Нет соединения с sauron.info.")
    except Exception as e:
        raise RuntimeError(f"Ошибка сети: {str(e)[:100]}")
    return _parse_api_response(resp)


def _api_post_search(query: str) -> dict:
    """POST /search с form-data query=... Возвращает result или бросает RuntimeError."""
    key = _api_key()
    url = f"{_api_base()}/search"

    # Телефонные номера: убираем знак + в начале.
    # Sauron API возвращает ошибку 1002 ("Пустой запрос") если query начинается с '+'.
    q = query.strip()
    if re.match(r'^\+\d{7,15}$', q):
        q = q[1:]

    try:
        resp = requests.post(
            url,
            headers=_API_HEADERS,
            params={"token": key},
            data={"query": q},          # form-data, НЕ json
            timeout=_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("sauron.info не ответил за 15 секунд — попробуй позже.")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Нет соединения с sauron.info.")
    except Exception as e:
        raise RuntimeError(f"Ошибка сети: {str(e)[:100]}")
    return _parse_api_response(resp)


def _parse_api_response(resp: requests.Response) -> dict:
    """Разбирает JSON-ответ API и возвращает result. Бросает RuntimeError при ошибке."""
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"Сервер вернул не JSON (HTTP {resp.status_code}).")

    if data.get("ok"):
        return data.get("result", {})

    code = data.get("error_code", resp.status_code)
    desc = data.get("description", "Неизвестная ошибка")

    if code in (401, 403):
        raise RuntimeError(
            "Неверный SAURON_API_KEY или доступ запрещён — проверь ключ в Replit Secrets."
        )
    if code == 402:
        raise RuntimeError(
            "Недостаточно средств на балансе Sauron — пополни аккаунт на sauron.info."
        )
    if code == 1002:
        raise RuntimeError(
            "Пустой поисковый запрос — укажи что искать."
        )
    if code == 429:
        raise RuntimeError("Слишком много запросов — подожди немного.")
    if code == 404:
        raise RuntimeError(
            f"Метод не найден. Уточни SAURON_API_URL. Последняя ошибка: {desc}"
        )
    raise RuntimeError(f"API ошибка {code}: {desc}")


# ═════════════════════════════════════════════════════════════════════════════
# Форматирование результатов API
# ═════════════════════════════════════════════════════════════════════════════

# Порядок отображения полей в ответе
_FIELD_ORDER = [
    "ФИО", "Фамилия", "Имя", "Отчество",
    "Телефон", "Телефон2", "Телефон3",
    "День рождения", "Дата рождения",
    "Паспорт", "ИНН", "СНИЛС",
    "Адрес", "Город", "Регион", "Страна",
    "Email", "Организация", "Должность",
    "Связь с лицом", "Источник",
]

_FIELD_ICONS = {
    "ФИО": "👤", "Фамилия": "👤", "Имя": "👤", "Отчество": "👤",
    "Телефон": "📱", "Телефон2": "📱", "Телефон3": "📱",
    "День рождения": "🎂", "Дата рождения": "🎂",
    "Паспорт": "🪪", "ИНН": "🔢", "СНИЛС": "🔢",
    "Адрес": "🏠", "Город": "🏙️", "Регион": "📍", "Страна": "🌍",
    "Email": "📧", "Организация": "🏢", "Должность": "💼",
    "Связь с лицом": "🔗", "Источник": "📂",
}


def _format_record(record: dict, idx: int) -> str:
    """Форматирует одну запись из result.response."""
    lines = [f"*{idx}.*"]

    # Сначала поля в нашем порядке
    shown = set()
    for field in _FIELD_ORDER:
        val = record.get(field)
        if val and str(val).strip():
            icon = _FIELD_ICONS.get(field, "▪️")
            lines.append(f"  {icon} *{field}:* {val}")
            shown.add(field)

    # Остальные поля которых не было в нашем списке
    for field, val in record.items():
        if field not in shown and val and str(val).strip():
            lines.append(f"  ▪️ *{field}:* {val}")

    return "\n".join(lines)


def _format_api_result(result: dict, query: str) -> str:
    """Форматирует результат API в Markdown для Telegram."""
    records = result.get("response", [])
    balance = result.get("balance", "")

    if not records:
        return f"По запросу «{query}» ничего не найдено."

    parts = []
    for i, rec in enumerate(records[:20], 1):
        parts.append(_format_record(rec, i))

    text = "\n\n".join(parts)
    if len(records) > 20:
        text += f"\n\n_…ещё {len(records) - 20} записей (показаны первые 20)_"

    if balance:
        text += f"\n\n💰 _Остаток баланса: {balance}_"

    if len(text) > 4000:
        text = text[:4000] + "\n…_(обрезано)_"

    return text


# ═════════════════════════════════════════════════════════════════════════════
# Путь 2 — сессионная авторизация через форму (fallback)
# ═════════════════════════════════════════════════════════════════════════════

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_BROWSER_HEADERS)
    return s


def _login_session(username: str, password: str) -> tuple[requests.Session | None, str]:
    s = _make_session()
    try:
        s.get(_SITE_BASE + "/login", timeout=_TIMEOUT)
    except Exception as e:
        return None, f"Не удалось открыть sauron.info: {str(e)[:80]}"
    try:
        resp = s.post(
            _SITE_BASE + "/user/authenticateUser",
            data={"login": username, "password": password},
            timeout=_TIMEOUT, allow_redirects=True,
        )
    except requests.exceptions.Timeout:
        return None, "Сайт не ответил за 15 секунд."
    except Exception as e:
        return None, f"Ошибка сети: {str(e)[:80]}"

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
    username, password = _credentials()
    if not username or not password:
        return None, (
            "Sauron не настроен.\n\n"
            "Добавь в Replit Secrets:\n"
            "• SAURON_API_KEY — API-ключ _(рекомендуется)_\n"
            "• или SAURON_USERNAME + SAURON_PASSWORD"
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
    """Поиск через веб-сессию (fallback без API-ключа)."""
    for path, param, method in [
        ("/search", "q", "get"),
        ("/persons/search", "query", "post"),
    ]:
        try:
            url = _SITE_BASE + path
            if method == "post":
                resp = sess.post(url, data={param: query}, timeout=_TIMEOUT, allow_redirects=True)
            else:
                resp = sess.get(url, params={param: query}, timeout=_TIMEOUT, allow_redirects=True)
            if urlparse(resp.url).path.rstrip("/") == "/login":
                return ""
            if resp.status_code == 200 and len(resp.text) > 200:
                return _parse_html(resp.text, query)
        except Exception:
            continue
    return f"По запросу «{query}» ничего не найдено (web-fallback)."


def _parse_html(html: str, query: str) -> str:
    if not _HAS_BS4:
        clean = re.sub(r"<[^>]+>", " ", html)
        lines = [l.strip() for l in re.sub(r"\s{2,}", "\n", clean).splitlines() if len(l.strip()) > 3]
        return "\n".join(lines[:30])[:2000]
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "head", "nav", "footer"]):
        tag.decompose()
    block = None
    for sel in [".results", ".search-results", ".persons", ".items", "#results", "main", "article", "table"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(separator="\n", strip=True)
            if len(t) > 50:
                block = t
                break
    if not block:
        body = soup.find("body")
        block = body.get_text(separator="\n", strip=True) if body else ""
    nav_words = {"войти", "выйти", "главная", "меню", "login", "logout", "home", "search", "найти"}
    lines = [l.strip() for l in block.splitlines() if l.strip() and len(l.strip()) > 2 and l.strip().lower() not in nav_words]
    result = "\n".join(lines[:40])
    return result[:3000] + ("…" if len(result) > 3000 else "")


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
            "Добавь в Replit Secrets:\n"
            "• `SAURON_API_KEY` — API-ключ _(рекомендуется)_\n"
            "• `SAURON_USERNAME` + `SAURON_PASSWORD` — логин и пароль\n\n"
            "_Ключи в Telegram не присылай — только через Replit Secrets._"
        )

    header = f"🔍 *Sauron: «{query}»*\n{'━' * 22}\n"

    # ── Путь 1: API-ключ ──────────────────────────────────────────────────
    if _api_key():
        try:
            result = _api_post_search(query)
            formatted = _format_api_result(result, query)
            return header + formatted
        except RuntimeError as e:
            msg = str(e)
            return f"🔍 Sauron\n\n⚠️ {msg}"
        except Exception as e:
            return f"🔍 Sauron\n\n❌ Неожиданная ошибка: {str(e)[:150]}"

    # ── Путь 2: сессионная авторизация (fallback) ─────────────────────────
    sess, err = _get_session()
    if err:
        return f"🔍 *Sauron*\n\n⚠️ {err}"

    result_text = _session_search(sess, query)
    if not result_text:
        username, _ = _credentials()
        _session_cache.pop(username, None)
        sess, err = _get_session()
        if err:
            return f"🔍 *Sauron*\n\n⚠️ Сессия истекла, не удалось переавторизоваться: {err}"
        result_text = _session_search(sess, query)

    return header + (result_text or f"По запросу «{query}» ничего не найдено.")


def search_for_batch(query: str) -> tuple[bool, str, bool]:
    """
    Для пакетного поиска из файлов.
    Возвращает (success, result_text, stop_batch).
    stop_batch=True — нужно остановить всю пачку (нет баланса, нет ключа).
    """
    query = (query or "").strip()
    if not query:
        return False, "Пустой запрос", False

    if not _is_configured():
        return False, "Sauron не настроен — добавь SAURON_API_KEY в Replit Secrets.", True

    if _api_key():
        try:
            result = _api_post_search(query)
            records = result.get("response", [])
            balance = result.get("balance", "")

            # Проверяем баланс — если близко к нулю, предупреждаем
            try:
                bal_f = float(balance)
                if bal_f < 1.0:
                    stop = True
                else:
                    stop = False
            except Exception:
                stop = False

            if not records:
                return False, f"По запросу «{query}» ничего не найдено.", stop

            formatted = _format_api_result(result, query)
            return True, formatted, stop

        except RuntimeError as e:
            msg = str(e)
            stop = any(kw in msg for kw in ("баланс", "Неверный", "API-ключ", "Secrets"))
            return False, msg, stop
        except Exception as e:
            return False, str(e)[:150], False

    # Fallback — сессионный поиск
    try:
        result_text = search(query)
        found = "ничего не найдено" not in result_text and "⚠️" not in result_text
        stop = any(kw in result_text for kw in ("не настроен", "Secrets", "баланс"))
        return found, result_text, stop
    except Exception as e:
        return False, str(e)[:100], False


def get_balance() -> str:
    """Проверяет баланс через API."""
    if not _api_key():
        return "⚠️ Баланс доступен только через API-ключ (SAURON_API_KEY)."
    try:
        result = _api_get("balance")
        bal = result.get("balance")
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

    return "⚠️ Не настроен — добавь SAURON_API_KEY в Replit Secrets"
