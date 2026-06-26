"""Поиск ФИО из файлов через Sauron.

Сценарий: пользователь присылает файл со списком ФИО → бот извлекает ФИО,
для каждого ищет в Sauron, собирает адреса/телефоны/связи, по связанным
людям делает один дополнительный запрос (глубина 1, лимит 3 чел./запрос).

Форматы: txt, csv, xlsx, xls, docx, pdf.
Для csv/xlsx — приоритет именованным колонкам (ФИО/Фамилия/Имя/…).
Regex — запасной вариант для неструктурированных файлов.

Env (переопределяют умолчания):
  SAURON_FILE_MAX_FIO      — макс. ФИО за файл (умолч. 30)
  SAURON_FILE_DELAY_SEC    — пауза между запросами, сек (умолч. 2)
  SAURON_FILE_MAX_RELATED  — макс. связанных лиц на одно ФИО (умолч. 3)
"""
import os
import re
import io
import csv
import time
import logging

logger = logging.getLogger(__name__)

# ── Лимиты ───────────────────────────────────────────────────────────────────
DEFAULT_MAX_FIO     = int(os.environ.get("SAURON_FILE_MAX_FIO",     "30"))
DEFAULT_DELAY_SEC   = float(os.environ.get("SAURON_FILE_DELAY_SEC", "2"))
DEFAULT_MAX_RELATED = int(os.environ.get("SAURON_FILE_MAX_RELATED", "3"))

# Обратная совместимость с bot.py который читает DEFAULT_MAX_RECORDS
DEFAULT_MAX_RECORDS = DEFAULT_MAX_FIO

# ── Опциональные библиотеки ───────────────────────────────────────────────────
try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
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
# Стоп-слова для фильтрации ложных ФИО
# ═════════════════════════════════════════════════════════════════════════════

_NAME_STOPWORDS: frozenset[str] = frozenset({
    # Административные единицы
    "Республика", "Область", "Край", "Округ", "Район", "Автономная",
    "Федерация", "Федеральный", "Федеральная", "Муниципальный", "Муниципальная",
    "Поселение", "Поселок", "Деревня", "Хутор", "Аул", "Станица",
    # Названия регионов РФ
    "Дагестан", "Башкортостан", "Татарстан", "Чечня", "Ингушетия",
    "Калмыкия", "Черкесия", "Адыгея", "Мордовия", "Удмуртия",
    "Чувашия", "Якутия", "Бурятия", "Хакасия", "Тыва", "Тува",
    "Карачаево", "Кабардино", "Балкария", "Коми", "Крым", "Алтай", "Марий",
    # Прилагательные формы регионов
    "Чувашская", "Чеченская", "Дагестанская", "Башкирская",
    "Татарская", "Ингушская", "Кабардинская", "Балкарская",
    "Калмыцкая", "Карачаевская", "Черкесская", "Адыгейская",
    "Мордовская", "Удмуртская", "Бурятская", "Тувинская",
    "Хакасская", "Якутская", "Алтайская",
    "Московская", "Ленинградская", "Нижегородская", "Самарская",
    "Саратовская", "Волгоградская", "Ростовская", "Краснодарская",
    "Ставропольская", "Свердловская", "Челябинская", "Тюменская",
    "Омская", "Новосибирская", "Иркутская", "Красноярская",
    "Хабаровская", "Воронежская", "Кемеровская", "Пермская",
    "Оренбургская", "Тульская", "Рязанская", "Ярославская",
    "Владимирская", "Ивановская", "Костромская", "Смоленская",
    "Тверская", "Брянская", "Орловская", "Курская", "Белгородская",
    "Липецкая", "Тамбовская", "Пензенская", "Ульяновская",
    "Кировская", "Вологодская", "Архангельская", "Мурманская",
    "Псковская", "Астраханская", "Калужская", "Калининградская",
    "Российская", "Советская", "Украинская", "Белорусская",
    # Адресные ориентиры
    "Улица", "Проспект", "Бульвар", "Площадь", "Переулок",
    "Набережная", "Шоссе", "Тракт", "Квартал", "Микрорайон",
    "Город", "Москва", "Санкт-Петербург",
    # Месяцы и дни недели
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница",
    "Суббота", "Воскресенье",
    # Украинские регионы
    "Украина", "Харків", "Одеса", "Дніпро",
})

# Прилагательные-топонимы: *ская/*ский/*ское/*ской
_GEO_ADJ_RE = re.compile(r'^[А-ЯЁ][а-яё]{3,}(ская|ский|ское|ской|ских|ском)$')


def _is_valid_fio_word(word: str) -> bool:
    if word in _NAME_STOPWORDS:
        return False
    if _GEO_ADJ_RE.match(word):
        return False
    # Слишком короткое (1 символ) или слишком длинное (>30)
    if len(word) < 2 or len(word) > 30:
        return False
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Извлечение ФИО
# ═════════════════════════════════════════════════════════════════════════════

# Три кириллических слова с заглавной буквы
_FIO3_RE = re.compile(
    r'\b([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,25})\s+'
    r'([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,20})\s+'
    r'([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,20})\b'
)

# Два кириллических слова с заглавной буквы
_FIO2_RE = re.compile(
    r'\b([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,25})\s+'
    r'([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,20})\b'
)


def extract_names(text: str) -> list[str]:
    """Извлекает ФИО из текста. Сначала 3-словные, потом 2-словные (fallback)."""
    found: list[str] = []
    seen: set[str] = set()

    for m in _FIO3_RE.finditer(text):
        parts = [m.group(1), m.group(2), m.group(3)]
        if not all(_is_valid_fio_word(p) for p in parts):
            continue
        name = ' '.join(parts)
        if name not in seen:
            seen.add(name)
            found.append(name)

    for m in _FIO2_RE.finditer(text):
        p1, p2 = m.group(1), m.group(2)
        if not _is_valid_fio_word(p1) or not _is_valid_fio_word(p2):
            continue
        name = f'{p1} {p2}'
        # Не добавлять если уже входит в 3-словное
        if name not in seen and not any(name in ex for ex in seen):
            seen.add(name)
            found.append(name)

    return found


# ═════════════════════════════════════════════════════════════════════════════
# Детектор колонок для структурированных файлов
# ═════════════════════════════════════════════════════════════════════════════

_FIO_COLS     = frozenset({'фио', 'fullname', 'full_name', 'ф.и.о', 'ф.и.о.', 'полное имя', 'наименование', 'person', 'человек'})
_LAST_COLS    = frozenset({'фамилия', 'lastname', 'last_name', 'surname', 'фам', 'last'})
_FIRST_COLS   = frozenset({'имя', 'firstname', 'first_name', 'name', 'first'})
_PATRON_COLS  = frozenset({'отчество', 'patronymic', 'patronym', 'middlename', 'middle_name', 'отч', 'middle'})
_PERSON_COLS  = frozenset({'клиент', 'client', 'абонент', 'subscriber', 'пассажир', 'passenger',
                            'сотрудник', 'employee', 'владелец', 'owner', 'заказчик'})


def _norm_header(s: str) -> str:
    return re.sub(r'[\s_\-\.]+', '', str(s).lower().strip())


def _detect_fio_cols(headers: list[str]) -> dict[str, int]:
    """
    Возвращает {'fio': idx, 'last': idx, 'first': idx, 'patron': idx}
    для обнаруженных колонок. Значения -1 если не найдено.
    """
    result = {'fio': -1, 'last': -1, 'first': -1, 'patron': -1}
    for i, h in enumerate(headers):
        n = _norm_header(h)
        if n in _FIO_COLS or n in _PERSON_COLS:
            result['fio'] = i
        elif n in _LAST_COLS and result['last'] == -1:
            result['last'] = i
        elif n in _FIRST_COLS and result['first'] == -1:
            result['first'] = i
        elif n in _PATRON_COLS and result['patron'] == -1:
            result['patron'] = i
    return result


def _build_fio_from_cols(row: list[str], col_map: dict[str, int]) -> str:
    """Собирает ФИО из колонок строки."""
    def get(key: str) -> str:
        idx = col_map.get(key, -1)
        if idx < 0 or idx >= len(row):
            return ''
        return str(row[idx]).strip()

    if col_map.get('fio', -1) >= 0:
        val = get('fio')
        if val:
            return val

    parts = [get('last'), get('first'), get('patron')]
    combined = ' '.join(p for p in parts if p)
    return combined


# ═════════════════════════════════════════════════════════════════════════════
# Парсинг файлов
# ═════════════════════════════════════════════════════════════════════════════

def _decode(data: bytes) -> str:
    for enc in ('utf-8', 'cp1251', 'utf-16', 'latin-1'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _parse_csv_structured(data: bytes) -> tuple[list[str], list[list[str]]]:
    """Возвращает (headers, rows). headers — список заголовков, rows — строки данных."""
    text = _decode(data)
    rows: list[list[str]] = []
    # Пробуем разные разделители
    for sep in (',', ';', '\t', '|'):
        try:
            reader = csv.reader(io.StringIO(text), delimiter=sep)
            all_rows = [r for r in reader if any(c.strip() for c in r)]
            if len(all_rows) >= 2 and len(all_rows[0]) >= 2:
                return all_rows[0], all_rows[1:]
        except Exception:
            continue
    # Fallback — просто строки
    lines = [l for l in text.splitlines() if l.strip()]
    return [], [[l] for l in lines]


def _parse_xlsx_structured(data: bytes) -> tuple[list[str], list[list[str]]]:
    if not _HAS_OPENPYXL:
        return [], []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = [[str(c.value or '').strip() for c in row] for row in ws.iter_rows()]
        wb.close()
        if len(rows) >= 2:
            return rows[0], rows[1:]
        return [], rows
    except Exception as e:
        logger.warning(f"xlsx parse error: {e}")
        return [], []


def _parse_xls_structured(data: bytes) -> tuple[list[str], list[list[str]]]:
    if not _HAS_XLRD:
        return [], []
    try:
        wb = xlrd.open_workbook(file_contents=data)
        ws = wb.sheet_by_index(0)
        rows = [[str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)] for r in range(ws.nrows)]
        if len(rows) >= 2:
            return rows[0], rows[1:]
        return [], rows
    except Exception as e:
        logger.warning(f"xls parse error: {e}")
        return [], []


def _parse_docx_text(data: bytes) -> str:
    if not _HAS_DOCX:
        return ''
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
                    lines.append('\t'.join(parts))
    except Exception as e:
        logger.warning(f"docx parse error: {e}")
    return '\n'.join(lines)


def _parse_pdf_text(data: bytes) -> str:
    if not _HAS_PDF:
        return ''
    lines = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:20]:
                text = page.extract_text()
                if text:
                    lines.append(text)
    except Exception as e:
        logger.warning(f"pdf parse error: {e}")
    return '\n'.join(lines)


def parse_file(data: bytes, filename: str) -> tuple[list[dict], str]:
    """
    Парсит файл и возвращает список PersonRecord + строку ошибки.

    PersonRecord: {
        'row_num'     : int   — номер строки в исходном файле (1-based)
        'source_line' : str   — исходная строка/запись
        'fio'         : str   — извлечённое ФИО (основной ключ поиска)
    }
    """
    fname = filename.lower()
    records: list[dict] = []
    seen_fio: set[str] = set()

    def _add(row_num: int, source_line: str, fio: str):
        key = fio.strip().lower()
        if not key or key in seen_fio:
            return
        seen_fio.add(key)
        records.append({'row_num': row_num, 'source_line': source_line, 'fio': fio.strip()})

    # ── Структурированные форматы (csv / xlsx / xls) ──────────────────────
    if fname.endswith('.csv'):
        headers, rows = _parse_csv_structured(data)
        col_map = _detect_fio_cols(headers)
        has_struct = any(v >= 0 for v in col_map.values())

        if has_struct:
            for i, row in enumerate(rows, 2):
                fio = _build_fio_from_cols(row, col_map)
                if fio:
                    _add(i, ' | '.join(str(c) for c in row if str(c).strip()), fio)
        else:
            # Regex по тексту
            for i, row in enumerate(rows, 2):
                line = ' '.join(str(c) for c in row if str(c).strip())
                for name in extract_names(line):
                    _add(i, line, name)

    elif fname.endswith('.xlsx'):
        if not _HAS_OPENPYXL:
            return [], "xlsx не поддерживается — библиотека openpyxl не установлена."
        headers, rows = _parse_xlsx_structured(data)
        col_map = _detect_fio_cols(headers)
        has_struct = any(v >= 0 for v in col_map.values())

        if has_struct:
            for i, row in enumerate(rows, 2):
                fio = _build_fio_from_cols(row, col_map)
                if fio:
                    _add(i, ' | '.join(str(c) for c in row if str(c).strip()), fio)
        else:
            for i, row in enumerate(rows, 2):
                line = ' '.join(str(c) for c in row if str(c).strip())
                for name in extract_names(line):
                    _add(i, line, name)

    elif fname.endswith('.xls'):
        if not _HAS_XLRD:
            return [], "xls не поддерживается — библиотека xlrd не установлена."
        headers, rows = _parse_xls_structured(data)
        col_map = _detect_fio_cols(headers)
        has_struct = any(v >= 0 for v in col_map.values())

        if has_struct:
            for i, row in enumerate(rows, 2):
                fio = _build_fio_from_cols(row, col_map)
                if fio:
                    _add(i, ' | '.join(str(c) for c in row if str(c).strip()), fio)
        else:
            for i, row in enumerate(rows, 2):
                line = ' '.join(str(c) for c in row if str(c).strip())
                for name in extract_names(line):
                    _add(i, line, name)

    # ── Неструктурированные форматы (txt / docx / pdf) ───────────────────
    elif fname.endswith('.txt'):
        text = _decode(data)
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            # Если строка — одно-два-три слова с заглавной → возможно ФИО
            words = stripped.split()
            if 2 <= len(words) <= 4:
                candidate = ' '.join(words)
                names = extract_names(candidate)
                if names:
                    for name in names:
                        _add(i, stripped, name)
                    continue
            # Иначе regex по всей строке
            for name in extract_names(stripped):
                _add(i, stripped, name)

    elif fname.endswith('.docx'):
        if not _HAS_DOCX:
            return [], "docx не поддерживается — библиотека python-docx не установлена."
        text = _parse_docx_text(data)
        for i, line in enumerate(text.splitlines(), 1):
            for name in extract_names(line.strip()):
                _add(i, line.strip(), name)

    elif fname.endswith('.pdf'):
        if not _HAS_PDF:
            return [], "pdf не поддерживается — библиотека pdfplumber не установлена."
        text = _parse_pdf_text(data)
        for i, line in enumerate(text.splitlines(), 1):
            for name in extract_names(line.strip()):
                _add(i, line.strip(), name)

    else:
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else '?'
        return [], f"Формат .{ext} не поддерживается.\nПоддерживаю: txt, csv, xlsx, xls, docx, pdf"

    if not records:
        return [], (
            "В файле не найдено ни одного ФИО.\n\n"
            "Убедись, что:\n"
            "• Для csv/xlsx — есть колонка «ФИО», «Фамилия» или «Имя»\n"
            "• Для txt — каждая строка содержит имя (2–3 слова с заглавной буквы на кириллице)"
        )

    return records, ''


# ═════════════════════════════════════════════════════════════════════════════
# Извлечение полей из ответа Sauron API
# ═════════════════════════════════════════════════════════════════════════════

def _val(rec: dict, *keys: str) -> str:
    for k in keys:
        v = rec.get(k)
        if v and str(v).strip() and str(v).strip().lower() not in ('none', 'null', '-'):
            return str(v).strip()
    return ''


def _extract_person_fields(api_result: dict) -> dict:
    """
    Из ответа Sauron (result dict) извлекает структурированные поля.

    Возвращает dict:
        found_fio, phones, emails, addresses, connections, sources, raw_count
    """
    records = api_result.get("response", [])
    if not records:
        return {
            "found_fio": "", "phones": "", "emails": "",
            "addresses": "", "connections": "", "sources": "",
            "raw_count": 0, "related_fios": [],
        }

    all_fios: list[str]    = []
    all_phones: list[str]  = []
    all_emails: list[str]  = []
    all_addrs: list[str]   = []
    all_conns: list[str]   = []
    all_sources: list[str] = []
    related_fios: list[str] = []

    for rec in records:
        # ФИО
        fio = _val(rec, 'ФИО', 'Фамилия', 'Имя')
        if fio and fio not in all_fios:
            all_fios.append(fio)

        # Телефоны
        for pf in ('Телефон', 'Телефон2', 'Телефон3', 'Phone', 'Моб', 'Mob'):
            ph = _val(rec, pf)
            if ph and ph not in all_phones:
                all_phones.append(ph)

        # Email
        for ef in ('Email', 'E-mail', 'Эл. почта', 'Почта'):
            em = _val(rec, ef)
            if em and em not in all_emails:
                all_emails.append(em)

        # Адрес
        addr_parts = []
        for af in ('Страна', 'Регион', 'Город', 'Населенный пункт', 'Адрес', 'Улица', 'Дом', 'Квартира'):
            v = _val(rec, af)
            if v:
                addr_parts.append(v)
        if addr_parts:
            addr = ', '.join(addr_parts)
            if addr not in all_addrs:
                all_addrs.append(addr)

        # Связи с людьми
        conn = _val(rec, 'Связь с лицом', 'Связанные лица', 'Связи', 'Родственники')
        if conn and conn not in all_conns:
            all_conns.append(conn)
            # Запоминаем ФИО из связи для дополнительного запроса
            if len(conn) > 3 and conn not in related_fios:
                # Берём только ФИО-часть (до цифр/дат)
                fio_part = re.split(r'[\d(]', conn)[0].strip()
                if fio_part and len(fio_part.split()) >= 2:
                    related_fios.append(fio_part)

        # Источник
        src = _val(rec, 'Источник', 'База', 'Источники')
        if src and src not in all_sources:
            all_sources.append(src)

    def join(lst: list[str], max_items: int = 10, sep: str = '; ') -> str:
        return sep.join(lst[:max_items])

    return {
        "found_fio":   join(all_fios, 5),
        "phones":      join(all_phones, 10),
        "emails":      join(all_emails, 5),
        "addresses":   join(all_addrs, 5),
        "connections": join(all_conns, 10),
        "sources":     join(all_sources, 10),
        "raw_count":   len(records),
        "related_fios": related_fios[:DEFAULT_MAX_RELATED],
    }


# ═════════════════════════════════════════════════════════════════════════════
# Пакетный поиск (entity-ориентированный)
# ═════════════════════════════════════════════════════════════════════════════

def batch_search(
    records: list[dict],
    chat_id: int,
    bot,
    progress_msg_id: int,
    max_records: int = DEFAULT_MAX_FIO,
    delay_sec: float = DEFAULT_DELAY_SEC,
) -> tuple[list[dict], str]:
    """
    Пакетный поиск по списку ФИО.

    Для каждого ФИО:
    1. Ищет в Sauron
    2. Если найдены связанные лица — делает доп. запросы (глубина 1, лимит MAX_RELATED)
    3. Собирает адреса/телефоны/связи/источники связанных лиц

    Возвращает (results, stop_reason).

    Каждый result: {
        row_num, source_fio, found, found_fio,
        phones, emails, addresses, connections,
        related_contacts, related_addresses, related_fios_found,
        sources, raw_count, confidence, error
    }
    """
    import sauron as _sauron

    to_search = records[:max_records]
    skipped   = len(records) - len(to_search)
    results: list[dict] = []
    stop_reason = ""
    queries_used = 0

    def _do_search(query: str) -> tuple[bool, dict, bool]:
        """(success, fields_dict, stop_batch)"""
        nonlocal queries_used
        try:
            api_result = _sauron._api_post_search(query)
            queries_used += 1
            fields = _extract_person_fields(api_result)
            stop = False
            try:
                bal = float(api_result.get("balance", "999"))
                if bal < 1.0:
                    stop = True
            except Exception:
                pass
            found = fields["raw_count"] > 0
            return found, fields, stop
        except RuntimeError as e:
            msg = str(e)
            stop = any(kw in msg for kw in ("баланс", "Неверный", "API-ключ", "Secrets"))
            return False, {}, stop
        except Exception as e:
            return False, {}, False

    total = len(to_search)
    for i, record in enumerate(to_search, 1):
        fio = record['fio']

        # Прогресс
        try:
            pct = (i - 1) * 100 // total
            bar = '▓' * ((i - 1) * 10 // total) + '░' * (10 - (i - 1) * 10 // total)
            bot.edit_message_text(
                f"🔍 *Поиск по файлу…*\n\n"
                f"Обработано: *{i-1}* / {total}"
                + (f" _(пропущено по лимиту: {skipped})_" if skipped and i == 1 else "")
                + f"\n{bar} {pct}%\n\n"
                f"👤 Ищу: `{fio}`",
                chat_id, progress_msg_id, parse_mode="Markdown",
            )
        except Exception:
            pass

        result: dict = {
            "row_num":            record['row_num'],
            "source_fio":         fio,
            "found":              False,
            "found_fio":          "",
            "phones":             "",
            "emails":             "",
            "addresses":          "",
            "connections":        "",
            "related_contacts":   "",
            "related_addresses":  "",
            "related_fios_found": "",
            "sources":            "",
            "raw_count":          0,
            "confidence":         "",
            "error":              "",
        }

        # ── Основной поиск по ФИО ─────────────────────────────────────────
        found, fields, stop = _do_search(fio)

        if stop:
            result['error'] = "Остановлено — недостаточно баланса или ошибка API"
            results.append(result)
            stop_reason = result['error']
            break

        if found:
            result['found']       = True
            result['found_fio']   = fields['found_fio']
            result['phones']      = fields['phones']
            result['emails']      = fields['emails']
            result['addresses']   = fields['addresses']
            result['connections'] = fields['connections']
            result['sources']     = fields['sources']
            result['raw_count']   = fields['raw_count']

            # Уверенность: совпадение ФИО запроса и найденного
            if fields['found_fio']:
                q_words = set(fio.lower().split())
                f_words = set(fields['found_fio'].lower().split())
                overlap = len(q_words & f_words)
                if overlap == len(q_words):
                    result['confidence'] = "высокая"
                elif overlap >= 1:
                    result['confidence'] = "средняя"
                else:
                    result['confidence'] = "низкая"
            else:
                result['confidence'] = "нет ФИО в ответе"

            # ── Дополнительный поиск по связанным лицам ──────────────────
            related_fios = fields.get('related_fios', [])
            rel_contacts_list:  list[str] = []
            rel_addresses_list: list[str] = []
            rel_fios_found:     list[str] = []

            for rel_fio in related_fios[:DEFAULT_MAX_RELATED]:
                if delay_sec > 0:
                    time.sleep(delay_sec)

                rel_found, rel_fields, rel_stop = _do_search(rel_fio)

                if rel_stop:
                    stop_reason = "Остановлено при поиске связанных — баланс или ошибка API"
                    break

                if rel_found:
                    rel_fios_found.append(rel_fio)
                    if rel_fields['phones']:
                        rel_contacts_list.append(f"{rel_fio}: {rel_fields['phones']}")
                    if rel_fields['addresses']:
                        rel_addresses_list.append(f"{rel_fio}: {rel_fields['addresses']}")

                if delay_sec > 0 and rel_fio != related_fios[-1]:
                    time.sleep(delay_sec)

            result['related_contacts']   = '; '.join(rel_contacts_list)
            result['related_addresses']  = '; '.join(rel_addresses_list)
            result['related_fios_found'] = '; '.join(rel_fios_found)

        else:
            result['found']      = False
            result['confidence'] = "не найден"

        results.append(result)

        if stop_reason:
            break

        # Пауза между основными запросами
        if i < total:
            time.sleep(delay_sec)

    return results, stop_reason


# ═════════════════════════════════════════════════════════════════════════════
# Формирование отчётов
# ═════════════════════════════════════════════════════════════════════════════

def build_short_summary(results: list[dict], stop_reason: str = "") -> str:
    """Краткое итоговое сообщение для чата."""
    total  = len(results)
    found  = sum(1 for r in results if r.get('found'))
    errors = sum(1 for r in results if r.get('error') and not r.get('found'))
    not_found = total - found - errors

    lines = ["🔍 *Поиск по файлу завершён*\n"]
    lines.append(f"✅ Найдено: *{found}*")
    lines.append(f"❌ Не найдено: *{not_found}*")
    if errors:
        lines.append(f"⚠️ Ошибки: *{errors}*")
    lines.append(f"📊 Всего обработано: *{total}*")

    if stop_reason:
        lines.append(f"\n⛔ Остановлено: _{stop_reason}_")

    if found > 0:
        lines.append("\n*Найденные люди:*")
        count = 0
        for r in results:
            if not r.get('found'):
                continue
            count += 1
            if count > 10:
                lines.append(f"_…и ещё {found - 10}_")
                break
            phones_short = r.get('phones', '').split(';')[0].strip()
            addr_short   = r.get('addresses', '').split(';')[0].strip()[:40]
            lines.append(
                f"\n👤 `{r['source_fio']}`"
                + (f"\n  📱 {phones_short}" if phones_short else "")
                + (f"\n  🏠 {addr_short}" if addr_short else "")
            )

    return '\n'.join(lines)


def build_csv_report(results: list[dict]) -> bytes:
    """
    Генерирует CSV-отчёт со структурированными колонками.
    Кодировка utf-8-sig (с BOM) — корректно открывается в Excel.
    """
    columns = [
        "Исходное ФИО",
        "Строка №",
        "Найдено",
        "Найденное ФИО",
        "Телефоны",
        "Email",
        "Адреса",
        "Связанные лица",
        "Контакты связанных",
        "Адреса связанных",
        "Найденные связанные ФИО",
        "Источники",
        "Записей в Sauron",
        "Уверенность",
        "Ошибка",
    ]

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(columns)

    for r in results:
        writer.writerow([
            r.get('source_fio', ''),
            r.get('row_num', ''),
            "Да" if r.get('found') else "Нет",
            r.get('found_fio', ''),
            r.get('phones', ''),
            r.get('emails', ''),
            r.get('addresses', ''),
            r.get('connections', ''),
            r.get('related_contacts', ''),
            r.get('related_addresses', ''),
            r.get('related_fios_found', ''),
            r.get('sources', ''),
            r.get('raw_count', ''),
            r.get('confidence', ''),
            r.get('error', ''),
        ])

    return output.getvalue().encode('utf-8-sig')


def build_xlsx_report(results: list[dict]) -> bytes | None:
    """
    Генерирует XLSX-отчёт с форматированием.
    Возвращает None если openpyxl не установлен.
    """
    if not _HAS_OPENPYXL:
        return None

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sauron Report"

    HEADER_FILL  = PatternFill("solid", fgColor="1F4E79")
    FOUND_FILL   = PatternFill("solid", fgColor="E2EFDA")
    NOTFND_FILL  = PatternFill("solid", fgColor="FCE4D6")
    HEADER_FONT  = Font(bold=True, color="FFFFFF")
    WRAP         = Alignment(wrap_text=True, vertical="top")

    columns = [
        ("Исходное ФИО",      30),
        ("Строка №",           8),
        ("Найдено",            9),
        ("Найденное ФИО",     30),
        ("Телефоны",          25),
        ("Email",             20),
        ("Адреса",            40),
        ("Связанные лица",    35),
        ("Контакты связанных",35),
        ("Адреса связанных",  35),
        ("Найд. связанные ФИО",25),
        ("Источники",         30),
        ("Записей в Sauron",  12),
        ("Уверенность",       12),
        ("Ошибка",            30),
    ]

    # Заголовок
    for col_idx, (col_name, col_width) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font      = HEADER_FONT
        cell.fill      = HEADER_FILL
        cell.alignment = WRAP
        ws.column_dimensions[cell.column_letter].width = col_width

    ws.freeze_panes = "A2"

    # Данные
    for row_idx, r in enumerate(results, 2):
        row_fill = FOUND_FILL if r.get('found') else NOTFND_FILL
        values = [
            r.get('source_fio', ''),
            r.get('row_num', ''),
            "Да" if r.get('found') else "Нет",
            r.get('found_fio', ''),
            r.get('phones', ''),
            r.get('emails', ''),
            r.get('addresses', ''),
            r.get('connections', ''),
            r.get('related_contacts', ''),
            r.get('related_addresses', ''),
            r.get('related_fios_found', ''),
            r.get('sources', ''),
            r.get('raw_count', ''),
            r.get('confidence', ''),
            r.get('error', ''),
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.alignment = WRAP
            if col_idx in (1, 3, 14):  # выделяем ключевые колонки
                cell.fill = row_fill

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_summary(records: list[dict], filename: str, max_records: int = DEFAULT_MAX_FIO) -> str:
    """Preview для отображения в чате (используется в _run_file_sauron)."""
    total      = len(records)
    will_check = min(total, max_records)
    skipped    = total - will_check

    lines = [f"📄 *Файл:* `{filename}`", ""]
    lines.append(f"👤 *Найдено ФИО:* {total}")

    preview = records[:8]
    for r in preview:
        lines.append(f"  • {r['fio']}")
    if total > 8:
        lines.append(f"  _…ещё {total - 8}_")
    lines.append("")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔍 Будет проверено: *{will_check}* ФИО")
    if skipped:
        lines.append(f"⚠️ По лимиту ({max_records}) пропущено: *{skipped}*")

    return '\n'.join(lines)


def supported_formats() -> str:
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
