"""Интеграция с CRM melog55.mamoth.club (Django REST).
Авторизация — по cookie сессии из браузера.

Требуется в .env:
  CRM_BASE_URL = https://melog55.mamoth.club/api
  CRM_COOKIE   = csrftoken=xxx; sessionid=yyy

Как получить CRM_COOKIE:
  1. Залогинься в CRM в Chrome.
  2. F12 -> Application -> Cookies -> melog55.mamoth.club
  3. Скопируй значения csrftoken и sessionid в одну строку через "; ".
  4. Вставь в .env. Перезапусти бота.
"""

import os
import re
import datetime as dt
import requests

BASE_URL = os.environ.get("CRM_BASE_URL", "https://melog55.mamoth.club/api").rstrip("/")
COOKIE = os.environ.get("CRM_COOKIE", "").strip()


def _csrf_from_cookie(cookie: str) -> str:
    m = re.search(r"csrftoken=([^;\s]+)", cookie or "")
    return m.group(1) if m else ""


def _headers() -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://melog55.mamoth.club",
        "Referer": "https://melog55.mamoth.club/app/",
        "User-Agent": "RuslanHelperBot/1.0",
    }
    if COOKIE:
        h["Cookie"] = COOKIE
    csrf = _csrf_from_cookie(COOKIE)
    if csrf:
        h["X-CSRFToken"] = csrf
    return h


def _today() -> str:
    return dt.date.today().isoformat()


def add_expense(amount: float, description: str, currency: str = "USDT",
                date: str = "", amount_usd: float | None = None) -> tuple[bool, str]:
    """Добавить расход в CRM. Возвращает (успех, сообщение)."""
    if not COOKIE:
        return False, ("❌ CRM не настроена. Добавь в .env строку CRM_COOKIE "
                       "(см. инструкцию в crm.py).")
    try:
        amt = float(str(amount).replace(",", "."))
    except Exception:
        return False, f"❌ Не понял сумму: {amount!r}"
    if amt <= 0:
        return False, "❌ Сумма должна быть больше 0."
    cur = (currency or "USDT").upper().strip()
    desc = (description or "").strip() or "без названия"
    payload = {
        "date": date or _today(),
        "description": desc,
        "amount": amt,
        "amount_usd": float(amount_usd) if amount_usd is not None else amt,
        "currency": cur,
    }
    try:
        r = requests.post(f"{BASE_URL}/expense/", json=payload,
                          headers=_headers(), timeout=20)
    except Exception as e:
        return False, f"❌ Сеть упала: {e}"

    if r.status_code in (200, 201):
        try:
            j = r.json()
            return True, (f"✅ Записал в CRM: «{j.get('description','?')}» — "
                          f"{j.get('amount','?')} {j.get('currency','?')} "
                          f"за {j.get('date','?')} (id {j.get('id','?')})")
        except Exception:
            return True, "✅ Записал расход в CRM."
    if r.status_code in (401, 403):
        return False, ("❌ CRM не пускает (401/403). Cookie протухла — залогинься "
                       "в CRM заново и обнови CRM_COOKIE в .env.")
    return False, f"❌ CRM вернула {r.status_code}: {r.text[:300]}"
