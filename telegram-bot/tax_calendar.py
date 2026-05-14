"""Налоговый календарь ФОП 3 группы (5%, плательщик ЕСВ).

Граничні строки:
- Єдиний податок (ЄП): протягом 50 днів після кварталу → 19 травня, 19 серпня,
  19 листопада, 19 лютого.
- ЄСВ за квартал: до 19 числа місяця, наступного за кварталом → 19 квітня,
  19 липня, 19 жовтня, 19 січня.

Модуль повертає список найближчих дедлайнів і вміє ставити нагадування
через існуючу функцію add_reminder з reminders.py.
"""

import re
from datetime import date, datetime, timedelta
from typing import Callable

_TAG_RE = re.compile(r"\[tag:(fop-[^\]]+)\]")

# Структура: (місяць, день, тип, мітка-кварталу-зсув)
# зсув: "prev" = за попередній квартал, "prev-2" = за квартал перед минулим (для січня)
DEADLINES = [
    # ЄСВ — місяць після кварталу
    (4,  19, "ЕСВ", "I"),     # за Q1 поточного року
    (7,  19, "ЕСВ", "II"),    # за Q2
    (10, 19, "ЕСВ", "III"),   # за Q3
    (1,  19, "ЕСВ", "IV-prev"),  # за Q4 попереднього року
    # ЄП — два місяці після кварталу
    (5,  19, "ЕП",  "I"),
    (8,  19, "ЕП",  "II"),
    (11, 19, "ЕП",  "III"),
    (2,  19, "ЕП",  "IV-prev"),
]

REMIND_DAYS_BEFORE = 7   # За скільки днів до дедлайну попереджати
REMIND_HOUR = 9          # О котрій годині


def upcoming_deadlines(now: datetime, months_ahead: int = 12) -> list[dict]:
    """Повертає найближчі дедлайни в межах months_ahead місяців."""
    horizon = now + timedelta(days=months_ahead * 31)
    out = []
    # Перевіряємо поточний і наступний рік
    for year in (now.year, now.year + 1):
        for month, day, kind, q in DEADLINES:
            d = date(year, month, day)
            # Назва кварталу
            if q.endswith("-prev"):
                quarter_label = f"IV кв. {year - 1}"
            else:
                quarter_label = f"{q} кв. {year}"
            deadline_dt = datetime(year, month, day, REMIND_HOUR, 0)
            # Дедлайн діє весь день — не ховаємо його після REMIND_HOUR ранку
            if d < now.date():
                continue
            if deadline_dt > horizon:
                continue
            out.append({
                "deadline": deadline_dt,
                "kind": kind,             # "ЕСВ" or "ЕП"
                "quarter": quarter_label,
                "tag": f"fop-{kind}-{year}-{q}",  # унікальна мітка
            })
    out.sort(key=lambda x: x["deadline"])
    return out


def format_calendar(now: datetime, months_ahead: int = 6) -> str:
    """Формує текст календаря для виводу в Telegram."""
    items = upcoming_deadlines(now, months_ahead)
    if not items:
        return "📋 *Податковий календар ФОП*\n\nНа найближчі місяці дедлайнів немає."
    lines = ["📋 *Податковий календар ФОП 3 групи*", ""]
    for it in items:
        d = it["deadline"]
        days_left = (d.date() - now.date()).days
        when = d.strftime("%d.%m.%Y")
        if days_left == 0:
            tail = " ⚠️ *СЬОГОДНІ*"
        elif days_left < 0:
            tail = " ❌ прострочено"
        elif days_left <= 7:
            tail = f" 🔥 через {days_left} дн."
        else:
            tail = f" — через {days_left} дн."
        lines.append(f"• *{it['kind']}* за {it['quarter']}: до {when}{tail}")
    lines.append("")
    lines.append("_ЄП 3 групи = 5% обороту за квартал. ЄСВ = 22% мінімалки × 3 міс._")
    lines.append("_Сплата: e-cabinet (cabinet.tax.gov.ua) або через Дію._")
    return "\n".join(lines)


def seed_reminders(
    chat_id: int,
    now: datetime,
    add_reminder_fn: Callable,
    list_pending_fn: Callable,
    months_ahead: int = 12,
) -> int:
    """Створює нагадування для всіх найближчих дедлайнів.

    Дедуплікація за тегом у тексті нагадування — повторні виклики безпечні.
    Повертає кількість створених нових нагадувань.
    """
    items = upcoming_deadlines(now, months_ahead)
    if not items:
        return 0

    pending = list_pending_fn(chat_id) or []
    existing_tags = set()
    for r in pending:
        text = r.get("text", "") or ""
        m = _TAG_RE.search(text)
        if m:
            existing_tags.add(m.group(1))

    created = 0
    for it in items:
        # Два нагадування: за 7 днів і вранці у день дедлайну
        for offset_days, prefix in ((REMIND_DAYS_BEFORE, "за тиждень"), (0, "сьогодні")):
            fire_at = it["deadline"] - timedelta(days=offset_days)
            if fire_at <= now:
                continue
            tag = f"{it['tag']}-{'pre' if offset_days else 'day'}"
            if tag in existing_tags:
                continue
            kind_full = "Єдиний податок" if it["kind"] == "ЕП" else "ЄСВ"
            when = it["deadline"].strftime("%d.%m.%Y")
            text = (
                f"💰 ФОП: {kind_full} за {it['quarter']} — "
                f"{prefix} (граничний строк {when}). "
                f"Сплата через cabinet.tax.gov.ua або Дію.\n[tag:{tag}]"
            )
            add_reminder_fn(chat_id, text, fire_at)
            created += 1
    return created
