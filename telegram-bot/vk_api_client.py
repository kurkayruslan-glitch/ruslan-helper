"""vk_api_client.py — VK API клиент для Ruslan Helper.

Используется ТОЛЬКО официальный VK API (api.vk.com/method/).
Токен хранится только в Replit Secrets / env → VK_API_TOKEN.
При отсутствии токена все методы возвращают {} / None без исключений.

Что делает:
  • users.get  — публичные поля профиля (имя, maiden_name, bdate, city,
                 relatives, domain) для обогащения данных родственников
  • Сравнивает полученные данные с известными ФИО для подтверждения родства

Что НЕ делает:
  • обход капчи
  • вход в чужие аккаунты
  • скрапинг закрытых страниц / friends / messages
  • любые неофициальные endpoint-ы
"""
from __future__ import annotations

import os
import re
import time
import logging
import urllib.request
import urllib.parse
import json
from typing import Optional

logger = logging.getLogger(__name__)

_TOKEN: Optional[str] = os.environ.get('VK_API_TOKEN') or None
_VK_API_BASE = 'https://api.vk.com/method/'
_VK_API_VER  = '5.199'
_RATE_DELAY  = 0.40          # ~2.5 req/sec — с запасом от лимита VK
_TIMEOUT     = 8             # секунды ожидания ответа
_RETRY_SLEEP = 1.2           # пауза при rate limit (error 29)

_last_req_time: float = 0.0


# ═════════════════════════════════════════════════════════════════════════════
# Публичный интерфейс
# ═════════════════════════════════════════════════════════════════════════════

def is_available() -> bool:
    """True если VK_API_TOKEN настроен в env."""
    return bool(_TOKEN)


def check_token() -> tuple[bool, str]:
    """
    Мягкая проверка токена через users.get (свой профиль).
    Не логирует токен. Возвращает (ok, описание).
    """
    if not _TOKEN:
        return False, "VK_API_TOKEN не настроен в Secrets"

    resp = _api_call('users.get', {'fields': 'domain'})
    if resp is None:
        return False, "Токен невалиден или нет прав (проверь VK_API_TOKEN в Secrets)"
    if isinstance(resp, list) and resp:
        user = resp[0]
        name = f"{user.get('first_name','')} {user.get('last_name','')}".strip()
        uid  = user.get('id', '?')
        return True, f"Авторизован как {name} (id{uid})"
    return False, "Неожиданный ответ VK API"


def get_profile(vk_ref: str) -> Optional[dict]:
    """
    Получает публичные поля профиля VK по ссылке / id / screen_name.

    Возвращает dict:
        id, first_name, last_name, maiden_name, bdate,
        city, home_town, relatives (list), domain, photo_100, profile_url
    или None если нет токена / недоступно / ошибка.

    Только публичные данные. Если профиль закрыт — возвращает None.
    """
    if not _TOKEN:
        return None
    uid = _extract_vk_id(vk_ref)
    if not uid:
        return None

    resp = _api_call('users.get', {
        'user_ids': uid,
        'fields': 'bdate,city,home_town,maiden_name,relatives,domain,photo_100',
        'name_case': 'nom',
    })
    if not resp or not isinstance(resp, list) or not resp:
        return None

    user = resp[0]
    if user.get('deactivated'):
        return None

    city_obj = user.get('city') or {}
    return {
        'id':          user.get('id'),
        'first_name':  user.get('first_name', ''),
        'last_name':   user.get('last_name', ''),
        'maiden_name': user.get('maiden_name', ''),
        'bdate':       user.get('bdate', ''),
        'city':        city_obj.get('title', '') if isinstance(city_obj, dict) else '',
        'home_town':   user.get('home_town', ''),
        'domain':      user.get('domain', ''),
        'relatives':   user.get('relatives', []),
        'photo_100':   user.get('photo_100', ''),
        'profile_url': (
            f"https://vk.com/{user.get('domain')}"
            if user.get('domain')
            else f"https://vk.com/id{user.get('id', 0)}"
        ),
    }


def enrich_relative(
    vk_links:  str,
    main_fio:  str,
    rel_fio:   str,
    rel_dob:   str = '',
) -> dict:
    """
    Обогащает данные родственника через VK API.

    vk_links  — строка с vk-ссылками/id (из Sauron, разделённые ';')
    main_fio  — ФИО основного человека из файла
    rel_fio   — ФИО кандидата в родственники
    rel_dob   — дата рождения кандидата (DD.MM.YYYY) если известна

    Возвращает dict:
        vk_profile_url, vk_full_name, vk_maiden_name, vk_bdate,
        vk_city, vk_relatives (str), vk_evidence (str), vk_score_bonus (float)
    или {} если VK недоступен / профиль не найден.
    """
    if not _TOKEN or not vk_links or not vk_links.strip():
        return {}

    main_parts  = [p.lower() for p in main_fio.split() if len(p) > 2]
    rel_last    = rel_fio.strip().split()[0].lower() if rel_fio.strip() else ''
    main_last   = main_fio.strip().split()[0].lower() if main_fio.strip() else ''

    for ref in re.split(r'[;,\s]+', vk_links):
        ref = ref.strip()
        if not ref or len(ref) < 2:
            continue

        profile = get_profile(ref)
        if not profile:
            continue

        vk_last   = profile['last_name'].lower()
        vk_maiden = profile['maiden_name'].lower()
        vk_bdate  = profile['bdate']
        vk_city   = profile['city'] or profile['home_town']
        vk_rels   = profile['relatives'] or []
        vk_full   = f"{profile['last_name']} {profile['first_name']}".strip()

        ev: list[str] = []
        score_bonus = 0.0

        # ── Совпадение фамилии ──────────────────────────────────────────
        if rel_last and vk_last and rel_last == vk_last:
            ev.append(f"VK: совпадает фамилия ({vk_full})")
            score_bonus += 4.0

        # ── Девичья фамилия = фамилия основного ────────────────────────
        if vk_maiden and main_last and vk_maiden == main_last:
            ev.append(f"VK: девичья фамилия «{profile['maiden_name']}» = фамилия основного")
            score_bonus += 7.0

        # ── Фамилия основного встречается как текущая фамилия VK ───────
        if main_last and vk_last and main_last == vk_last and rel_last != vk_last:
            ev.append(f"VK: текущая фамилия VK = фамилия основного")
            score_bonus += 5.0

        # ── Дата рождения совпадает ─────────────────────────────────────
        if rel_dob and vk_bdate:
            # Сравниваем DD.MM или DD.MM.YYYY
            rel_short = rel_dob[:5]   # "DD.MM"
            vk_short  = vk_bdate[:5]  # "DD.MM"
            if rel_short == vk_short:
                ev.append(f"VK: дата рождения совпадает ({vk_bdate})")
                score_bonus += 5.0

        # ── Родственники VK упоминают основного ────────────────────────
        rel_names_vk: list[str] = []
        for r in vk_rels[:15]:
            rname = (r.get('name') or '').strip()
            if not rname:
                continue
            rel_names_vk.append(rname)
            rname_lower = rname.lower()
            if any(p in rname_lower for p in main_parts):
                ev.append(f"VK: родственник «{rname}» упоминает основного")
                score_bonus += 8.0
                break

        result = {
            'vk_profile_url': profile['profile_url'],
            'vk_full_name':   vk_full,
            'vk_maiden_name': profile['maiden_name'],
            'vk_bdate':       vk_bdate,
            'vk_city':        vk_city,
            'vk_relatives':   '; '.join(rel_names_vk[:6]),
            'vk_evidence':    '; '.join(ev),
            'vk_score_bonus': round(score_bonus, 1),
        }
        return result   # берём первый валидный профиль

    return {}


# ═════════════════════════════════════════════════════════════════════════════
# Внутренние утилиты
# ═════════════════════════════════════════════════════════════════════════════

def _extract_vk_id(vk_ref: str) -> Optional[str]:
    """Из ссылки / строки извлекает id или screen_name для users.get."""
    vk_ref = (vk_ref or '').strip()
    if not vk_ref:
        return None

    # vk.com/id12345 → '12345'
    m = re.search(r'vk\.com/id(\d+)', vk_ref, re.I)
    if m:
        return m.group(1)

    # vk.com/screen_name → 'screen_name'
    m = re.search(r'vk\.com/([a-zA-Z][a-zA-Z0-9._]{2,})', vk_ref, re.I)
    if m:
        return m.group(1)

    # Просто числовой id
    if re.match(r'^\d{5,}$', vk_ref):
        return vk_ref

    # id123 без домена
    m = re.match(r'^id(\d+)$', vk_ref, re.I)
    if m:
        return m.group(1)

    # screen_name без домена (только латиница)
    if re.match(r'^[a-zA-Z][a-zA-Z0-9._]{2,}$', vk_ref):
        return vk_ref

    return None


def _api_call(method: str, params: dict) -> Optional[object]:
    """Выполняет HTTP запрос к VK API. Возвращает response или None."""
    if not _TOKEN:
        return None

    global _last_req_time
    elapsed = time.time() - _last_req_time
    if elapsed < _RATE_DELAY:
        time.sleep(_RATE_DELAY - elapsed)
    _last_req_time = time.time()

    all_params = {**params, 'access_token': _TOKEN, 'v': _VK_API_VER}
    url = _VK_API_BASE + method + '?' + urllib.parse.urlencode(all_params)

    try:
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'RuslanHelper/2.0 (personal bot)'},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
    except Exception as exc:
        logger.warning(f"VK API network error ({method}): {exc}")
        return None

    try:
        data = json.loads(raw)
    except Exception:
        logger.warning(f"VK API bad JSON ({method})")
        return None

    if 'error' in data:
        err  = data['error']
        code = err.get('error_code', 0)
        msg  = err.get('error_msg', '')
        if code == 5:
            logger.error("VK API: невалидный токен (error 5). Проверь VK_API_TOKEN в Secrets.")
        elif code == 14:
            logger.warning("VK API: требуется капча (error 14) — пропускаем запрос.")
        elif code == 29:
            logger.warning("VK API: rate limit (error 29) — пауза.")
            time.sleep(_RETRY_SLEEP)
        elif code in (7, 15, 200):
            logger.debug(f"VK API access denied ({code}): {msg}")
        else:
            logger.debug(f"VK API error {code}: {msg}")
        return None

    return data.get('response')
