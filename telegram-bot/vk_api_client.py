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

    # Пустой список → это сервисный токен (нет «своего профиля»).
    # Проверяем реальную задачу: чтение чужого публичного профиля.
    probe = _api_call('users.get', {'user_ids': '1', 'fields': 'domain'})
    if isinstance(probe, list) and probe:
        return True, "Сервисный токен валиден (чтение публичных профилей работает)"
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
        'fields': ('bdate,city,home_town,maiden_name,relatives,domain,photo_100,'
                   'contacts,is_closed,can_access_closed,country'),
        'name_case': 'nom',
    })
    if not resp or not isinstance(resp, list) or not resp:
        return None

    user = resp[0]
    if user.get('deactivated'):
        return None

    city_obj = user.get('city') or {}
    is_closed = bool(user.get('is_closed'))
    can_access = bool(user.get('can_access_closed'))
    # Имена родственников-пользователей VK разрешаем доп. запросом
    rels_raw = user.get('relatives', []) or []
    rels_resolved = _resolve_relatives(rels_raw) if (not is_closed or can_access) else []

    return {
        'id':            user.get('id'),
        'first_name':    user.get('first_name', ''),
        'last_name':     user.get('last_name', ''),
        'maiden_name':   user.get('maiden_name', ''),
        'bdate':         user.get('bdate', ''),
        'city':          city_obj.get('title', '') if isinstance(city_obj, dict) else '',
        'home_town':     user.get('home_town', ''),
        'domain':        user.get('domain', ''),
        'relatives':     rels_raw,
        'relatives_resolved': rels_resolved,
        'mobile_phone':  user.get('mobile_phone', ''),
        'home_phone':    user.get('home_phone', ''),
        'is_closed':     is_closed,
        'can_access_closed': can_access,
        'photo_100':     user.get('photo_100', ''),
        'profile_url': (
            f"https://vk.com/{user.get('domain')}"
            if user.get('domain')
            else f"https://vk.com/id{user.get('id', 0)}"
        ),
    }


def _resolve_relatives(rels: list) -> list[dict]:
    """
    Превращает VK-поле relatives в список {type, name}.
    Для родственников-пользователей VK (есть только id) дозапрашивает имена
    через users.get одним батчем. Возвращает только записи с именем.
    """
    out: list[dict] = []
    ids_to_fetch: list[str] = []
    for r in rels:
        if not isinstance(r, dict):
            continue
        rtype = str(r.get('type', '') or '')
        if r.get('name'):
            out.append({'type': rtype, 'name': str(r['name']).strip()})
        elif r.get('id'):
            ids_to_fetch.append(str(r['id']))
            out.append({'type': rtype, 'id': r['id'], 'name': ''})

    if ids_to_fetch:
        resp = _api_call('users.get', {'user_ids': ','.join(ids_to_fetch),
                                       'fields': 'maiden_name'})
        if isinstance(resp, list):
            name_by_id = {
                u.get('id'): f"{u.get('last_name','')} {u.get('first_name','')}".strip()
                for u in resp if isinstance(u, dict)
            }
            for item in out:
                if not item.get('name') and item.get('id') in name_by_id:
                    item['name'] = name_by_id[item['id']]

    return [x for x in out if x.get('name')]


def _digits(s: str) -> str:
    """Только цифры из строки (для сравнения телефонов)."""
    return re.sub(r'\D+', '', s or '')


def enrich_relative(
    vk_links:     str,
    main_fio:     str,
    rel_fio:      str,
    rel_dob:      str = '',
    rel_phones:   Optional[list] = None,
    main_address: str = '',
    rel_address:  str = '',
) -> dict:
    """
    Обогащает данные родственника через ОФИЦИАЛЬНЫЙ VK API и формирует
    доказательства родства.

    vk_links     — строка с vk-ссылками/id (из Sauron, разделённые ';')
    main_fio     — ФИО основного человека из файла
    rel_fio      — ФИО кандидата в родственники
    rel_dob      — дата рождения кандидата (DD.MM.YYYY) если известна
    rel_phones   — телефоны кандидата (для сверки с публичным контактом VK)
    main_address — адрес основного (для сверки города)
    rel_address  — адрес кандидата (для сверки города)

    Возвращает dict:
        vk_profile_url, vk_full_name, vk_maiden_name, vk_bdate, vk_city,
        vk_relatives (str), vk_evidence (str), vk_score_bonus (float),
        vk_closed (bool), vk_high_confidence (bool)
    или {} если VK недоступен / профиль не найден.

    Правила уверенности:
      • высокая ТОЛЬКО при явном совпадении relatives ИЛИ ≥2 независимых
        признаках;
      • одно слабое совпадение (например только общий город) родством не
        считается;
      • закрытые профили не обходятся — отдаётся «VK закрыт/нет доступа».
    """
    if not _TOKEN or not vk_links or not vk_links.strip():
        return {}

    rel_phones = rel_phones or []
    rel_ph_norm = {_digits(p) for p in rel_phones if _digits(p)}

    main_tokens = [p.lower() for p in main_fio.split() if len(p) > 2]
    main_last   = main_fio.strip().split()[0].lower() if main_fio.strip() else ''
    rel_split   = rel_fio.strip().split()
    rel_last    = rel_split[0].lower() if rel_split else ''
    rel_first   = rel_split[1].lower() if len(rel_split) > 1 else ''
    addr_blob   = f"{main_address} {rel_address}".lower()

    closed_result: Optional[dict] = None

    for ref in re.split(r'[;,\s]+', vk_links):
        ref = ref.strip()
        if not ref or len(ref) < 2:
            continue

        profile = get_profile(ref)
        if not profile:
            continue

        vk_full = f"{profile['last_name']} {profile['first_name']}".strip()

        # ── Закрытый профиль — не обходим, фиксируем как нет доступа ──────
        # Не прерываем перебор: вдруг в списке есть открытый профиль с
        # доказательствами — он имеет приоритет.
        if profile.get('is_closed') and not profile.get('can_access_closed'):
            if closed_result is None:
                closed_result = {
                    'vk_profile_url':     profile['profile_url'],
                    'vk_full_name':       vk_full,
                    'vk_maiden_name':     '',
                    'vk_bdate':           '',
                    'vk_city':            '',
                    'vk_relatives':       '',
                    'vk_evidence':        'VK закрыт/нет доступа',
                    'vk_score_bonus':     0.0,
                    'vk_closed':          True,
                    'vk_high_confidence': False,
                }
            continue

        vk_last   = profile['last_name'].lower()
        vk_first  = profile['first_name'].lower()
        vk_maiden = (profile['maiden_name'] or '').lower()
        vk_bdate  = profile['bdate']
        vk_city   = profile['city'] or profile['home_town']
        vk_city_l = vk_city.lower()
        vk_rels   = profile.get('relatives_resolved', []) or []

        ev: list[str] = []
        score_bonus  = 0.0
        signals      = 0          # независимые признаки
        explicit_kin = False      # явное совпадение relatives

        # ── 1. Совпадение фамилии ───────────────────────────────────────
        surname_match = bool(rel_last and vk_last and rel_last == vk_last)
        if surname_match:
            ev.append(f"VK: совпадает фамилия ({vk_full})")
            score_bonus += 4.0
            signals += 1

        # ── 2. Девичья фамилия = фамилия основного (смена фамилии) ──────
        maiden_match = bool(vk_maiden and main_last and vk_maiden == main_last)
        if maiden_match:
            ev.append(f"VK: совпадает девичья фамилия «{profile['maiden_name']}»")
            score_bonus += 7.0
            signals += 1

        # ── 3. Текущая фамилия VK = фамилия основного (а у кандидата иная)
        if main_last and vk_last and main_last == vk_last and rel_last != vk_last:
            ev.append("VK: текущая фамилия = фамилия основного")
            score_bonus += 5.0
            signals += 1

        # ── 4. Дата рождения совпадает ─────────────────────────────────
        dob_match = False
        if rel_dob and vk_bdate and len(vk_bdate) >= 5:
            if rel_dob[:5] == vk_bdate[:5]:
                dob_match = True
                ev.append(f"VK: совпадает дата рождения ({vk_bdate})")
                score_bonus += 5.0
                signals += 1

        # ── 5. Город ───────────────────────────────────────────────────
        if vk_city_l and len(vk_city_l) > 2 and vk_city_l in addr_blob:
            if surname_match or maiden_match:
                ev.append(f"VK: общий город ({vk_city}) + фамилия")
                score_bonus += 3.0
                signals += 1
            else:
                # город сам по себе — слабый признак, не считаем сигналом
                ev.append(f"VK: общий город ({vk_city})")
                score_bonus += 1.0

        # ── 6. Телефон в публичном контакте VK совпадает ───────────────
        vk_phones = {_digits(profile.get('mobile_phone', '')),
                     _digits(profile.get('home_phone', ''))}
        vk_phones.discard('')
        if rel_ph_norm and (vk_phones & rel_ph_norm):
            ev.append("VK: телефон в профиле совпадает")
            score_bonus += 6.0
            signals += 1

        # ── 7. Имя кандидата + дата (подтверждение личности при смене фам.)
        # Зависит от уже учтённой даты — это НЕ отдельный независимый сигнал,
        # только дополнительный балл к скорингу.
        if rel_first and vk_first and rel_first == vk_first and dob_match:
            ev.append("VK: совпадает имя + дата рождения")
            score_bonus += 2.0

        # ── 8. Явные родственники VK упоминают основного ───────────────
        rel_names_vk: list[str] = []
        for r in vk_rels[:15]:
            rname = (r.get('name') or '').strip()
            if not rname:
                continue
            rel_names_vk.append(rname)
            rname_lower = rname.lower()
            if main_tokens and sum(1 for p in main_tokens if p in rname_lower) >= 2:
                ev.append(f"VK: указан родственник «{rname}» (совпадает с основным)")
                score_bonus += 8.0
                explicit_kin = True
                break
        else:
            if rel_names_vk:
                ev.append(f"VK: в профиле указаны родственники ({', '.join(rel_names_vk[:3])})")

        high_conf = explicit_kin or signals >= 2

        return {
            'vk_profile_url':     profile['profile_url'],
            'vk_full_name':       vk_full,
            'vk_maiden_name':     profile['maiden_name'],
            'vk_bdate':           vk_bdate,
            'vk_city':            vk_city,
            'vk_relatives':       '; '.join(rel_names_vk[:6]),
            'vk_evidence':        '; '.join(ev),
            'vk_score_bonus':     round(score_bonus, 1),
            'vk_closed':          False,
            'vk_high_confidence': high_conf,
        }   # берём первый открытый профиль с данными

    # Открытых профилей не нашлось — если был закрытый, отдаём его
    return closed_result or {}


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

    all_params = {**params, 'access_token': _TOKEN, 'v': _VK_API_VER, 'lang': 0}
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
