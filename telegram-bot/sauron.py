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


def _vk_status_line() -> str:
    """Статус VK API для отчёта status() — без показа токена и без сетевых вызовов."""
    if not _rel_vk:
        return "VK API: ⚠️ модуль vk_api_client недоступен"
    try:
        if _rel_vk.is_available():
            return "VK API: ✅ подключён (расширенный поиск родственников активен)"
        return "VK API: ⚠️ не настроен (добавь VK_API_TOKEN для поиска родственников)"
    except Exception:
        return "VK API: ⚠️ ошибка проверки"


def status() -> str:
    """Статус интеграции — без показа значений secrets. Включает статус VK API."""
    key = _api_key()
    username, password = _credentials()
    api_url = os.environ.get("SAURON_API_URL", "")

    if key:
        url_note = f" · URL: {api_url}" if api_url else ""
        sauron_part = f"✅ API-ключ настроен{url_note}"
    elif username and password:
        cached = _session_cache.get(username)
        sauron_part = "✅ Логин/пароль настроены · сессия не активна"
        if cached:
            _, ts = cached
            age = int(time.time() - ts)
            if age < _SESSION_TTL:
                sauron_part = f"✅ Логин/пароль настроены · сессия {age // 60}м {age % 60}с назад"
    elif username and not password:
        sauron_part = "⚠️ SAURON_USERNAME задан, но SAURON_PASSWORD отсутствует"
    else:
        sauron_part = "⚠️ Не настроен — добавь SAURON_API_KEY в Replit Secrets"

    return f"{sauron_part}\n   {_vk_status_line()}"

# GLOBAL RELATIVE SEARCH OVERRIDE
_REL_MAX_PRIMARY = int(os.environ.get("REL_SEARCH_PRIMARY_RECORDS", "8"))
_REL_MAX_SECONDARY = int(os.environ.get("REL_SEARCH_SECONDARY_QUERIES", "8"))
_REL_MAX_VK = int(os.environ.get("REL_SEARCH_VK_PROFILES", "6"))
_REL_MAX_OUT = int(os.environ.get("REL_SEARCH_MAX_RELATIVES", "14"))

_REL_FAMILY_KWS = (
    "родствен", "родств", "связь с лицом", "связи", "семья", "семей",
    "мать", "отец", "родител", "сын", "дочь", "дети", "ребен", "ребён",
    "брат", "сестр", "жена", "муж", "супруг", "супруга", "брак",
    "дед", "баб", "внук", "внуч", "племян", "дядя", "тетя", "тётя",
    "зять", "сноха", "свек", "тещ", "тёщ", "тесть", "опек",
)
_REL_NON_FAMILY_KWS = (
    "клиент", "водитель", "работодатель", "сотрудник", "коллега",
    "учредитель", "директор", "акционер", "владелец", "контрагент",
)
_REL_FIO_RE = re.compile(
    r"\b([А-ЯЁІЇЄ][а-яёіїє'\-]{1,25})\s+"
    r"([А-ЯЁІЇЄ][а-яёіїє'\-]{1,25})"
    r"(?:\s+([А-ЯЁІЇЄ][а-яёіїє'\-]{1,25}))?\b"
)
_REL_PHONE_RE = re.compile(r"(?:\+?\d[\d\s()\-.]{7,}\d)")
_REL_VK_RE = re.compile(
    r"(?:https?://)?(?:m\.)?(?:vk\.com|vkontakte\.ru)/[A-Za-z0-9_.\-/]+|\b(?:id|club|public)\d{4,}\b",
    re.I,
)
_REL_NAME_STOPS = {
    "Республика", "Область", "Край", "Округ", "Район", "Город", "Улица",
    "Проспект", "Переулок", "Украина", "Россия", "Российская", "Москва",
    "Телефон", "Адрес", "Источник", "СНИЛС", "Паспорт", "Email", "Почта",
}

try:
    import vk_api_client as _rel_vk
except Exception:
    _rel_vk = None


def _rel_limit(text: str, n: int = 3900) -> str:
    return text if len(text) <= n else text[:n] + "\n…_(обрезано)_"


def _rel_clean(v) -> str:
    return str(v or "").replace("\n", " ").strip()


def _rel_record_fio(rec: dict) -> str:
    direct = _rel_clean(rec.get("ФИО"))
    if direct:
        return direct
    parts = [_rel_clean(rec.get(k)) for k in ("Фамилия", "Имя", "Отчество")]
    return " ".join(p for p in parts if p)


def _rel_digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def _rel_norm_phone(raw: str) -> str:
    d = _rel_digits(raw)
    if len(d) == 11 and d.startswith("8"):
        return "7" + d[1:]
    if len(d) == 10 and d.startswith("0"):
        return "380" + d[1:]
    return d


def _rel_extract_phones(text: str) -> list[str]:
    out, seen = [], set()
    for m in _REL_PHONE_RE.finditer(text or ""):
        p = _rel_norm_phone(m.group(0))
        if 9 <= len(p) <= 15 and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _rel_extract_vk_refs(text: str) -> list[str]:
    out, seen = [], set()
    for m in _REL_VK_RE.finditer(text or ""):
        ref = m.group(0).strip().rstrip(".,;)")
        if ref and ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _rel_extract_fios(text: str) -> list[str]:
    out, seen = [], set()
    for m in _REL_FIO_RE.finditer(text or ""):
        parts = [p for p in m.groups() if p]
        if len(parts) < 2 or any(p in _REL_NAME_STOPS for p in parts):
            continue
        fio = " ".join(parts)
        key = fio.lower()
        if key not in seen:
            seen.add(key)
            out.append(fio)
    return out


def _rel_is_family_context(text: str) -> bool:
    t = (text or "").lower()
    if any(k in t for k in _REL_NON_FAMILY_KWS) and not any(k in t for k in _REL_FAMILY_KWS):
        return False
    return any(k in t for k in _REL_FAMILY_KWS)


def _rel_same_person(a: str, b: str) -> bool:
    aa = {x.lower() for x in (a or "").split() if len(x) > 2}
    bb = {x.lower() for x in (b or "").split() if len(x) > 2}
    return bool(aa and bb and len(aa & bb) >= min(2, len(aa), len(bb)))


def _rel_dedupe(items: list[str]) -> list[str]:
    out, seen = [], set()
    for item in items:
        item = _rel_clean(item)
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _rel_add_candidate(candidates: dict, name: str, evidence: str, source: str,
                       confidence: str = "средняя", phone: str = "", vk: str = ""):
    name = _rel_clean(name)
    if not name or len(name) < 5:
        return
    key = re.sub(r"\s+", " ", name.lower())
    cur = candidates.setdefault(key, {
        "name": name,
        "evidence": [],
        "sources": set(),
        "phones": set(),
        "vk": set(),
        "confidence": "низкая",
        "score": 0,
    })
    if evidence and evidence not in cur["evidence"]:
        cur["evidence"].append(evidence)
    if source:
        cur["sources"].add(source)
    if phone:
        cur["phones"].add(phone)
    if vk:
        cur["vk"].add(vk)
    weights = {"низкая": 1, "средняя": 2, "высокая": 3}
    if weights.get(confidence, 1) > weights.get(cur["confidence"], 1):
        cur["confidence"] = confidence
    cur["score"] += weights.get(confidence, 1)


def _rel_scan_sauron_records(records: list[dict], query: str) -> tuple[dict, list[str], list[str]]:
    candidates, secondary, vk_refs = {}, [], []
    for rec in records[:_REL_MAX_PRIMARY]:
        main_fio = _rel_record_fio(rec) or query
        blob = " ".join(f"{k}: {v}" for k, v in rec.items() if v)
        vk_refs.extend(_rel_extract_vk_refs(blob))
        secondary.extend(_rel_extract_phones(blob))
        for key, val in rec.items():
            text = f"{key}: {val}"
            if not _rel_is_family_context(text):
                continue
            for fio in _rel_extract_fios(text):
                if _rel_same_person(fio, main_fio) or _rel_same_person(fio, query):
                    continue
                conf = "высокая" if ("связ" in key.lower() or "род" in key.lower()) else "средняя"
                _rel_add_candidate(candidates, fio, f"Sauron: семейное поле «{key}»", "Sauron", conf)
                secondary.append(fio)
    return candidates, _rel_dedupe(secondary), _rel_dedupe(vk_refs)


def _rel_vk_profiles_from_refs(refs: list[str]) -> list[dict]:
    if not _rel_vk or not getattr(_rel_vk, "is_available", lambda: False)():
        return []
    out = []
    for ref in refs[:_REL_MAX_VK]:
        try:
            prof = _rel_vk.get_profile(ref)
            if prof:
                out.append(prof)
        except Exception:
            continue
    return out


def _rel_vk_search(query: str) -> tuple[list[dict], list[str]]:
    profiles, notes = [], []
    if not _rel_vk or not getattr(_rel_vk, "is_available", lambda: False)():
        return profiles, ["VK API: токен не настроен"]
    try:
        ok, msg = _rel_vk.check_token()
        notes.append(f"VK API: {'OK' if ok else 'ошибка'} ({msg})")
        if not ok:
            return profiles, notes
    except Exception as e:
        notes.append(f"VK API: проверка не удалась ({str(e)[:80]})")
        return profiles, notes
    try:
        api_call = getattr(_rel_vk, "_api_call", None)
        if not api_call:
            return profiles, notes
        resp = api_call("users.search", {
            "q": query,
            "count": str(_REL_MAX_VK),
            "fields": "bdate,city,home_town,maiden_name,relatives,domain,contacts,is_closed,can_access_closed,country",
            "name_case": "nom",
        })
        raw_items = resp.get("items", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
        for item in raw_items[:_REL_MAX_VK]:
            uid = item.get("id") if isinstance(item, dict) else None
            if not uid:
                continue
            prof = _rel_vk.get_profile(str(uid))
            if prof:
                profiles.append(prof)
    except Exception as e:
        notes.append(f"VK search: {str(e)[:80]}")
    return profiles, notes


def _rel_scan_vk_profiles(profiles: list[dict], candidates: dict, query: str) -> list[str]:
    notes = []
    for prof in profiles[:_REL_MAX_VK]:
        full = f"{prof.get('last_name','')} {prof.get('first_name','')}".strip()
        url = prof.get("profile_url", "")
        if not full:
            continue
        if prof.get("is_closed") and not prof.get("can_access_closed"):
            notes.append(f"VK: {full} закрыт/нет доступа ({url})")
        else:
            bits = [full]
            if prof.get("bdate"):
                bits.append(str(prof["bdate"]))
            if prof.get("city") or prof.get("home_town"):
                bits.append(str(prof.get("city") or prof.get("home_town")))
            if url:
                bits.append(url)
            notes.append("VK профиль: " + " · ".join(bits))
        for rel in prof.get("relatives_resolved", []) or []:
            rname = _rel_clean(rel.get("name"))
            rtype = _rel_clean(rel.get("type")) or "relative"
            if rname and not _rel_same_person(rname, query):
                _rel_add_candidate(candidates, rname, f"VK: указан как {rtype} у {full}", "VK relatives", "высокая", vk=url)
    return notes


def _rel_secondary_sauron(candidates: dict, secondary_queries: list[str]):
    used = 0
    for q in secondary_queries:
        if used >= _REL_MAX_SECONDARY:
            break
        if not q or len(q) < 5:
            continue
        used += 1
        try:
            res = _api_post_search(q)
        except Exception:
            continue
        recs = res.get("response", []) or []
        if not recs:
            continue
        blob = " ".join(" ".join(str(v) for v in rec.values() if v) for rec in recs[:5])
        phones = _rel_extract_phones(blob)
        vk_refs = _rel_extract_vk_refs(blob)
        target_names = [q] if _REL_FIO_RE.search(q) else _rel_extract_fios(blob)[:3]
        for name in target_names:
            _rel_add_candidate(
                candidates,
                name,
                f"Sauron: вторичная проверка по «{q}» дала {len(recs)} записей",
                "Sauron secondary",
                "средняя",
                phone=phones[0] if phones else "",
                vk=vk_refs[0] if vk_refs else "",
            )


def _rel_format_global_result(result: dict, query: str) -> str:
    records = result.get("response", []) or []
    balance = result.get("balance", "")
    if not records:
        return f"По запросу «{query}» ничего не найдено."
    candidates, secondary, vk_refs = _rel_scan_sauron_records(records, query)
    vk_profiles = _rel_vk_profiles_from_refs(vk_refs)
    vk_search_profiles, vk_notes = _rel_vk_search(query)
    vk_notes.extend(_rel_scan_vk_profiles(vk_profiles + vk_search_profiles, candidates, query))
    cand_names = [c["name"] for c in candidates.values()]
    _rel_secondary_sauron(candidates, _rel_dedupe(secondary + cand_names))
    lines = [
        f"🌐 *Глобальный поиск родственников: «{query}»*",
        "━" * 22,
        f"Найдено базовых записей Sauron: *{len(records)}*",
    ]
    if balance:
        lines.append(f"Баланс Sauron: _{balance}_")
    lines.append("")
    lines.append("*1. Основные совпадения Sauron*")
    for i, rec in enumerate(records[:min(3, _REL_MAX_PRIMARY)], 1):
        lines.append(_format_record(rec, i))
        lines.append("")
    cand_list = sorted(candidates.values(), key=lambda x: (-x["score"], x["name"]))
    lines.append("*2. Кандидаты родственников / связей*")
    if not cand_list:
        lines.append("Явных родственников в найденных данных не выделено. Попробуй ФИО + дата рождения или телефон.")
    else:
        for i, c in enumerate(cand_list[:_REL_MAX_OUT], 1):
            src = ", ".join(sorted(c["sources"]))
            ev = "; ".join(c["evidence"][:3])
            phones = "; ".join(sorted(c["phones"])[:3])
            vk = "; ".join(sorted(c["vk"])[:2])
            line = f"{i}. *{c['name']}* — уверенность: *{c['confidence']}*"
            line += f"\n   Источник: {src}"
            if ev:
                line += f"\n   Доказательства: {ev}"
            if phones:
                line += f"\n   Телефоны: {phones}"
            if vk:
                line += f"\n   VK: {vk}"
            lines.append(line)
    lines.append("")
    lines.append("*3. VK API*")
    if vk_notes:
        for note in vk_notes[:8]:
            lines.append(f"• {note}")
    else:
        lines.append("• VK-сигналов нет или VK_API_TOKEN не дал данных по этому запросу.")
    next_q = _rel_dedupe(secondary + [c["name"] for c in cand_list])[:10]
    if next_q:
        lines.append("")
        lines.append("*4. Что ещё проверял / можно добить вручную*")
        lines.append(", ".join(next_q))
    lines.append("")
    lines.append("_Важно: это поиск кандидатов и связей по базам/публичным VK-сигналам. Родство считается уверенным только когда есть семейное поле, VK relatives или несколько независимых совпадений._")
    return _rel_limit("\n".join(lines))


def search(query: str) -> str:
    """Глобальный поиск человека и кандидатов родственников: Sauron + VK API."""
    query = (query or "").strip()
    if not query:
        return "❓ Что искать? Укажи ФИО, номер телефона, адрес или ИНН."
    if not _is_configured():
        return (
            "🔍 *Sauron не настроен*\n\n"
            "Добавь в Railway/Replit Secrets:\n"
            "• SAURON_API_KEY — API-ключ\n"
            "• VK_API_TOKEN — для расширения через VK API"
        )
    if _api_key():
        try:
            result = _api_post_search(query)
            return _rel_format_global_result(result, query)
        except RuntimeError as e:
            return f"🔍 Sauron\n\n⚠️ {str(e)}"
        except Exception as e:
            return f"🔍 Sauron\n\n❌ Неожиданная ошибка: {str(e)[:150]}"
    sess, err = _get_session()
    if err:
        return f"🔍 *Sauron*\n\n⚠️ {err}"
    result_text = _session_search(sess, query)
    return f"🔍 *Sauron: «{query}»*\n{'━' * 22}\n" + (result_text or f"По запросу «{query}» ничего не найдено.")
