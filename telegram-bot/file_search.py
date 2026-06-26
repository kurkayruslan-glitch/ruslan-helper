"""Поиск по файлам через Sauron.

Поддерживаемые форматы: txt, csv, xlsx/xls, docx, pdf
Извлекает ФИО и номера телефонов, нормализует, дедуплицирует,
выполняет пакетный поиск через Sauron с лимитами, прогрессом и обработкой ошибок.

Настройки пакетного поиска:
  SAURON_FILE_MAX_RECORDS  — максимум записей за файл (по умолчанию 50)
  SAURON_FILE_DELAY_SEC    — пауза между запросами в секундах (по умолчанию 2)
"""
import os
import re
import io
import csv
import time
import logging

logger = logging.getLogger(__name__)

# ── Лимиты по умолчанию ──────────────────────────────────────────────────────
DEFAULT_MAX_RECORDS = int(os.environ.get("SAURON_FILE_MAX_RECORDS", "50"))
DEFAULT_DELAY_SEC   = float(os.environ.get("SAURON_FILE_DELAY_SEC", "2"))

# ── Опциональные библиотеки ───────────────────────────────────────────────────
try:
    import openpyxl
    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False

try:
    import xlrd
    _HAS_XLRD = True
except ImportError:
    _HAS_XLRD = False

try:
    from docx import Document as DocxDocument
    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False

try:
    import pdfplumber
    _HAS_PDF = True
except ImportError:
    _HAS_PDF = False


# ═════════════════════════════════════════════════════════════════════════════
# Извлечение телефонов
# ═════════════════════════════════════════════════════════════════════════════

# Украинские и российские номера в разных форматах
_PHONE_RE = re.compile(
    r'(?<!\d)'
    r'(?:\+?(?:38|7|380|8))?'               # код страны (опционально)
    r'[\s\-\(]*'
    r'(?:0\d{2}|[3-9]\d{2})'               # код оператора / первые 3 цифры
    r'[\s\-\)]*'
    r'\d{3}'
    r'[\s\-]*'
    r'\d{2}'
    r'[\s\-]*'
    r'\d{2}'
    r'(?!\d)',
    re.ASCII,
)

def _normalize_phone(raw: str) -> str:
    """Нормализует номер телефона к формату +380XXXXXXXXX или +7XXXXXXXXXX."""
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 12 and digits.startswith('380'):
        return '+' + digits
    if len(digits) == 11 and digits.startswith('38'):
        return '+' + digits
    if len(digits) == 11 and digits.startswith('80'):
        return '+3' + digits
    if len(digits) == 11 and digits.startswith('8'):
        return '+7' + digits[1:]
    if len(digits) == 11 and digits.startswith('7'):
        return '+' + digits
    if len(digits) == 10 and digits.startswith('0'):
        return '+38' + digits
    if len(digits) == 10 and digits.startswith('9'):
        return '+7' + digits
    if len(digits) == 9:
        return '+380' + digits
    return '+' + digits if not digits.startswith('+') else raw


def extract_phones(text: str) -> list[str]:
    """Извлекает и нормализует все номера телефонов из текста."""
    found = []
    seen = set()
    for m in _PHONE_RE.finditer(text):
        raw = m.group(0).strip()
        normalized = _normalize_phone(raw)
        digits = re.sub(r'\D', '', normalized)
        if len(digits) < 9 or len(digits) > 15:
            continue
        if normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


# ═════════════════════════════════════════════════════════════════════════════
# Извлечение ФИО
# ═════════════════════════════════════════════════════════════════════════════

# Три кириллических слова с большой буквы подряд (фамилия имя отчество)
_FIO3_RE = re.compile(
    r'\b([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,25})\s+'
    r'([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,20})\s+'
    r'([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,20})\b'
)

# Два кириллических слова с большой буквы (фамилия имя)
_FIO2_RE = re.compile(
    r'\b([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,25})\s+'
    r'([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,20})\b'
)

# Стоп-слова — не имена
_NAME_STOPWORDS = {
    "Улица", "Проспект", "Бульвар", "Площадь", "Переулок",
    "Город", "Район", "Область", "Украина", "Россия", "Киев",
    "Харьков", "Одесса", "Днепр", "Запорожье",
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница",
    "Суббота", "Воскресенье",
}


def extract_names(text: str) -> list[str]:
    """Извлекает ФИО (3 слова) и имена-фамилии (2 слова) из текста."""
    found = []
    seen = set()

    # Сначала трёхсловные (ФИО — более надёжно)
    for m in _FIO3_RE.finditer(text):
        parts = [m.group(1), m.group(2), m.group(3)]
        if any(p in _NAME_STOPWORDS for p in parts):
            continue
        name = ' '.join(parts)
        if name not in seen:
            seen.add(name)
            found.append(name)

    # Затем двухсловные — только если они не входят в уже найденные трёхсловные
    for m in _FIO2_RE.finditer(text):
        p1, p2 = m.group(1), m.group(2)
        if p1 in _NAME_STOPWORDS or p2 in _NAME_STOPWORDS:
            continue
        name = f'{p1} {p2}'
        if name not in seen and not any(name in existing for existing in seen):
            seen.add(name)
            found.append(name)

    return found


# ═════════════════════════════════════════════════════════════════════════════
# Парсинг файлов
# ═════════════════════════════════════════════════════════════════════════════

def _parse_txt(data: bytes) -> str:
    for enc in ('utf-8', 'cp1251', 'utf-16', 'latin-1'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _parse_csv(data: bytes) -> str:
    text = _parse_txt(data)
    lines = []
    try:
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            lines.append(' '.join(row))
    except Exception:
        lines = text.splitlines()
    return '\n'.join(lines)


def _parse_xlsx(data: bytes) -> str:
    if not _HAS_OPENPYXL:
        return ""
    lines = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                parts = [str(c) for c in row if c is not None and str(c).strip()]
                if parts:
                    lines.append(' '.join(parts))
    except Exception as e:
        logger.warning(f"xlsx parse error: {e}")
    return '\n'.join(lines)


def _parse_xls(data: bytes) -> str:
    if not _HAS_XLRD:
        return ""
    lines = []
    try:
        wb = xlrd.open_workbook(file_contents=data)
        for sheet in wb.sheets():
            for row_idx in range(sheet.nrows):
                parts = [str(sheet.cell_value(row_idx, c)).strip()
                         for c in range(sheet.ncols)
                         if str(sheet.cell_value(row_idx, c)).strip()]
                if parts:
                    lines.append(' '.join(parts))
    except Exception as e:
        logger.warning(f"xls parse error: {e}")
    return '\n'.join(lines)


def _parse_docx(data: bytes) -> str:
    if not _HAS_DOCX:
        return ""
    lines = []
    try:
        doc = DocxDocument(io.BytesIO(data))
        for para in doc.paragraphs:
            if para.text.strip():
                lines.append(para.text.strip())
        for table in doc.tables:
            for row in table.rows:
                parts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if parts:
                    lines.append(' '.join(parts))
    except Exception as e:
        logger.warning(f"docx parse error: {e}")
    return '\n'.join(lines)


def _parse_pdf(data: bytes) -> str:
    if not _HAS_PDF:
        return ""
    lines = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:20]:  # ограничиваем 20 страниц
                text = page.extract_text()
                if text:
                    lines.append(text)
    except Exception as e:
        logger.warning(f"pdf parse error: {e}")
    return '\n'.join(lines)


def parse_file(data: bytes, filename: str) -> tuple[list[dict], str]:
    """
    Парсит файл и извлекает записи для поиска.

    Возвращает (records, error_msg).
    records — список {'type': 'phone'|'name', 'value': str}
    error_msg — непустая строка если файл не удалось обработать.
    """
    fname = filename.lower()

    if fname.endswith('.txt'):
        text = _parse_txt(data)
    elif fname.endswith('.csv'):
        text = _parse_csv(data)
    elif fname.endswith('.xlsx'):
        if not _HAS_OPENPYXL:
            return [], "Файл xlsx не поддерживается — библиотека openpyxl не установлена."
        text = _parse_xlsx(data)
    elif fname.endswith('.xls'):
        if not _HAS_XLRD:
            return [], "Файл xls не поддерживается — библиотека xlrd не установлена."
        text = _parse_xls(data)
    elif fname.endswith('.docx'):
        if not _HAS_DOCX:
            return [], "Файл docx не поддерживается — библиотека python-docx не установлена."
        text = _parse_docx(data)
    elif fname.endswith('.pdf'):
        if not _HAS_PDF:
            return [], "Файл pdf не поддерживается — библиотека pdfplumber не установлена."
        text = _parse_pdf(data)
    else:
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else '?'
        return [], (
            f"Формат .{ext} не поддерживается.\n"
            "Поддерживаю: txt, csv, xlsx, xls, docx, pdf"
        )

    if not text or not text.strip():
        return [], "Файл пустой или не удалось извлечь текст."

    # Извлекаем телефоны и ФИО
    phones = extract_phones(text)
    names  = extract_names(text)

    records: list[dict] = []
    seen: set[str] = set()

    for ph in phones:
        key = ph.lower()
        if key not in seen:
            seen.add(key)
            records.append({"type": "phone", "value": ph})

    for nm in names:
        key = nm.lower()
        if key not in seen:
            seen.add(key)
            records.append({"type": "name", "value": nm})

    if not records:
        return [], (
            "В файле не найдено ни номеров телефонов, ни ФИО.\n\n"
            "Убедись, что файл содержит данные в читаемом формате:\n"
            "• Телефоны: +380XXXXXXXXX или 0XXXXXXXXX\n"
            "• ФИО: три кириллических слова с большой буквы (Иванов Петр Сергеевич)"
        )

    return records, ""


def build_summary(records: list[dict], filename: str, max_records: int = DEFAULT_MAX_RECORDS) -> str:
    """Формирует краткое preview найденных записей."""
    phones = [r for r in records if r['type'] == 'phone']
    names  = [r for r in records if r['type'] == 'name']

    total = len(records)
    will_search = min(total, max_records)
    skipped = total - will_search

    lines = [f"📄 *Файл:* `{filename}`", ""]

    if phones:
        lines.append(f"📱 *Телефонов:* {len(phones)}")
        preview = phones[:5]
        for r in preview:
            lines.append(f"  • {r['value']}")
        if len(phones) > 5:
            lines.append(f"  _…ещё {len(phones) - 5}_")
        lines.append("")

    if names:
        lines.append(f"👤 *ФИО / Имён:* {len(names)}")
        preview = names[:5]
        for r in preview:
            lines.append(f"  • {r['value']}")
        if len(names) > 5:
            lines.append(f"  _…ещё {len(names) - 5}_")
        lines.append("")

    lines.append(f"━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔍 Будет проверено: *{will_search}* записей через Sauron")
    if skipped:
        lines.append(f"⚠️ Лимит {max_records} — пропущено: {skipped}")
    lines.append("")
    lines.append("Отправить данные на проверку в Sauron?")

    return '\n'.join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# Пакетный поиск
# ═════════════════════════════════════════════════════════════════════════════

def batch_search(
    records: list[dict],
    chat_id: int,
    bot,
    progress_msg_id: int,
    max_records: int = DEFAULT_MAX_RECORDS,
    delay_sec: float = DEFAULT_DELAY_SEC,
) -> tuple[list[dict], str]:
    """
    Выполняет пакетный поиск через Sauron.

    Возвращает (results, stop_reason).
    stop_reason — пустая строка если завершено нормально.

    results — список:
      {
        "query": str,
        "type": "phone"|"name",
        "found": bool,
        "result_text": str,
        "error": str|None,
      }
    """
    import sauron  # импортируем здесь чтобы избежать circular import

    to_search = records[:max_records]
    results = []
    stop_reason = ""
    auth_errors = 0

    for i, record in enumerate(to_search, 1):
        query   = record['value']
        rtype   = record['type']
        icon    = "📱" if rtype == 'phone' else "👤"

        # Обновляем прогресс
        try:
            progress_text = (
                f"🔍 *Поиск по файлу…*\n\n"
                f"Обработано: *{i-1}* / {len(to_search)}\n"
                f"{'▓' * ((i-1) * 10 // len(to_search))}{'░' * (10 - (i-1) * 10 // len(to_search))} "
                f"{(i-1) * 100 // len(to_search)}%\n\n"
                f"{icon} Ищу: `{query}`"
            )
            bot.edit_message_text(progress_text, chat_id, progress_msg_id, parse_mode="Markdown")
        except Exception:
            pass

        # Поиск
        try:
            success, result_text, stop_batch = sauron.search_for_batch(query)
        except Exception as e:
            result_text = f"Ошибка: {str(e)[:100]}"
            success     = False
            stop_batch  = False

        results.append({
            "query":       query,
            "type":        rtype,
            "found":       success,
            "result_text": result_text,
            "error":       None if success else result_text,
        })

        if stop_batch:
            stop_reason = result_text
            break

        # Считаем ошибки авторизации
        if not success and any(kw in result_text for kw in ("API-ключ", "Неверный", "401", "403")):
            auth_errors += 1
            if auth_errors >= 3:
                stop_reason = "Повторные ошибки авторизации Sauron — остановлено."
                break

        # Пауза между запросами
        if i < len(to_search):
            time.sleep(delay_sec)

    return results, stop_reason


# ═════════════════════════════════════════════════════════════════════════════
# Формирование отчёта
# ═════════════════════════════════════════════════════════════════════════════

def build_short_summary(results: list[dict], stop_reason: str = "") -> str:
    """Краткое итоговое сообщение."""
    total  = len(results)
    found  = sum(1 for r in results if r['found'])
    errors = sum(1 for r in results if r.get('error') and not r['found'])

    lines = ["🔍 *Поиск по файлу завершён*\n"]
    lines.append(f"✅ Найдено совпадений: *{found}*")
    lines.append(f"❌ Не найдено / ошибки: *{errors}*")
    lines.append(f"📊 Всего проверено: *{total}*")

    if stop_reason:
        lines.append(f"\n⚠️ Остановлено досрочно: _{stop_reason}_")

    if found > 0:
        lines.append("\n*Найденные записи:*")
        for r in results:
            if r['found']:
                icon = "📱" if r['type'] == 'phone' else "👤"
                lines.append(f"\n{icon} `{r['query']}`")
                # Первые 3 строки результата
                preview = '\n'.join(r['result_text'].splitlines()[:3])
                if len(preview) > 200:
                    preview = preview[:200] + "…"
                lines.append(preview)

    return '\n'.join(lines)


def build_csv_report(results: list[dict]) -> bytes:
    """Генерирует CSV-отчёт с полными результатами."""
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(["Тип", "Запрос", "Найдено", "Результат"])

    for r in results:
        rtype  = "Телефон" if r['type'] == 'phone' else "ФИО"
        found  = "Да" if r['found'] else "Нет"
        text   = r['result_text'] or r.get('error', '')
        # Убираем Markdown из текста
        clean  = re.sub(r'[*_`\[\]]', '', text)
        clean  = re.sub(r'\n{2,}', '\n', clean).strip()
        writer.writerow([rtype, r['query'], found, clean])

    return output.getvalue().encode('utf-8-sig')  # BOM для Excel


def build_txt_report(results: list[dict]) -> bytes:
    """Генерирует TXT-отчёт с полными результатами."""
    lines = ["ОТЧЁТ ПОИСКА SAURON", "=" * 50, ""]
    for i, r in enumerate(results, 1):
        icon  = "📱 Телефон" if r['type'] == 'phone' else "👤 ФИО"
        found = "✅ НАЙДЕНО" if r['found'] else "❌ Не найдено"
        lines.append(f"{i}. {icon}: {r['query']}")
        lines.append(f"   Статус: {found}")
        if r['result_text']:
            clean = re.sub(r'[*_`\[\]]', '', r['result_text'])
            lines.append(f"   {clean[:500]}")
        lines.append("")
    return '\n'.join(lines).encode('utf-8')


def supported_formats() -> str:
    """Перечисляет поддерживаемые форматы для отображения пользователю."""
    fmts = ["txt", "csv"]
    if _HAS_OPENPYXL:
        fmts.append("xlsx")
    if _HAS_XLRD:
        fmts.append("xls")
    if _HAS_DOCX:
        fmts.append("docx")
    if _HAS_PDF:
        fmts.append("pdf")
    return ", ".join(fmts)
