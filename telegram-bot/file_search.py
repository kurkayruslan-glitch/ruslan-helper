"""Поиск ФИО из файлов через Sauron.

Сценарий: пользователь присылает файл со списком ФИО → бот извлекает ФИО,
для каждого ищет в Sauron, собирает адреса/телефоны/связи, по связанным
людям делает один дополнительный запрос (глубина 1, лимит 3 чел./запрос).

После поиска для связанных лиц:
  • фильтрует телефоны на ликвидность (нормализация + антимаски)
  • детектирует присутствие в Максе / taxsee по источникам Sauron

Форматы файлов: txt, csv, xlsx, xls, docx, pdf.
Для csv/xlsx — приоритет именованным колонкам (ФИО/Фамилия/Имя/…).
Regex — запасной вариант для неструктурированных файлов.

Env (переопределяют умолчания):
  SAURON_FILE_MAX_FIO         — макс. ФИО за файл (умолч. 30)
  SAURON_FILE_DELAY_SEC       — пауза между запросами, сек (умолч. 2)
  SAURON_FILE_MAX_RELATED     — макс. связанных лиц на одно ФИО (умолч. 3)
  SAURON_FILE_MAX_REL_PHONES  — макс. ликвидных тел. связанных на ФИО (умолч. 10)
"""
import os
import re
import io
import csv
import time
import logging

logger = logging.getLogger(__name__)

# ── Лимиты ───────────────────────────────────────────────────────────────────
DEFAULT_MAX_FIO        = int(os.environ.get("SAURON_FILE_MAX_FIO",        "30"))
DEFAULT_DELAY_SEC      = float(os.environ.get("SAURON_FILE_DELAY_SEC",    "2"))
DEFAULT_MAX_RELATED    = int(os.environ.get("SAURON_FILE_MAX_RELATED",    "3"))
DEFAULT_MAX_REL_PHONES = int(os.environ.get("SAURON_FILE_MAX_REL_PHONES", "10"))

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
    if len(word) < 2 or len(word) > 30:
        return False
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Извлечение ФИО
# ═════════════════════════════════════════════════════════════════════════════

_FIO3_RE = re.compile(
    r'\b([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,25})\s+'
    r'([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,20})\s+'
    r'([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,20})\b'
)

_FIO2_RE = re.compile(
    r'\b([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,25})\s+'
    r'([А-ЯЁІЇЄ][а-яёіїє\'\-]{1,20})\b'
)


def extract_names(text: str) -> list[str]:
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
        if name not in seen and not any(name in ex for ex in seen):
            seen.add(name)
            found.append(name)

    return found


# ═════════════════════════════════════════════════════════════════════════════
# Детектор колонок для структурированных файлов
# ═════════════════════════════════════════════════════════════════════════════

_FIO_COLS    = frozenset({'фио', 'fullname', 'full_name', 'ф.и.о', 'ф.и.о.', 'полное имя', 'наименование', 'person', 'человек'})
_LAST_COLS   = frozenset({'фамилия', 'lastname', 'last_name', 'surname', 'фам', 'last'})
_FIRST_COLS  = frozenset({'имя', 'firstname', 'first_name', 'name', 'first'})
_PATRON_COLS = frozenset({'отчество', 'patronymic', 'patronym', 'middlename', 'middle_name', 'отч', 'middle'})
_PERSON_COLS = frozenset({'клиент', 'client', 'абонент', 'subscriber', 'пассажир', 'passenger',
                           'сотрудник', 'employee', 'владелец', 'owner', 'заказчик'})


def _norm_header(s: str) -> str:
    return re.sub(r'[\s_\-\.]+', '', str(s).lower().strip())


def _detect_fio_cols(headers: list[str]) -> dict[str, int]:
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
    return ' '.join(p for p in parts if p)


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
    text = _decode(data)
    for sep in (',', ';', '\t', '|'):
        try:
            reader = csv.reader(io.StringIO(text), delimiter=sep)
            all_rows = [r for r in reader if any(c.strip() for c in r)]
            if len(all_rows) >= 2 and len(all_rows[0]) >= 2:
                return all_rows[0], all_rows[1:]
        except Exception:
            continue
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
        return (rows[0], rows[1:]) if len(rows) >= 2 else ([], rows)
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
        return (rows[0], rows[1:]) if len(rows) >= 2 else ([], rows)
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
        'row_num'     : int
        'source_line' : str
        'fio'         : str   — основной ключ поиска
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

    def _from_structured(headers: list[str], rows: list[list[str]]):
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

    if fname.endswith('.csv'):
        headers, rows = _parse_csv_structured(data)
        _from_structured(headers, rows)

    elif fname.endswith('.xlsx'):
        if not _HAS_OPENPYXL:
            return [], "xlsx не поддерживается — опенпиксл не установлен."
        headers, rows = _parse_xlsx_structured(data)
        _from_structured(headers, rows)

    elif fname.endswith('.xls'):
        if not _HAS_XLRD:
            return [], "xls не поддерживается — xlrd не установлен."
        headers, rows = _parse_xls_structured(data)
        _from_structured(headers, rows)

    elif fname.endswith('.txt'):
        text = _decode(data)
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            words = stripped.split()
            if 2 <= len(words) <= 4:
                names = extract_names(stripped)
                if names:
                    for name in names:
                        _add(i, stripped, name)
                    continue
            for name in extract_names(stripped):
                _add(i, stripped, name)

    elif fname.endswith('.docx'):
        if not _HAS_DOCX:
            return [], "docx не поддерживается — python-docx не установлен."
        text = _parse_docx_text(data)
        for i, line in enumerate(text.splitlines(), 1):
            for name in extract_names(line.strip()):
                _add(i, line.strip(), name)

    elif fname.endswith('.pdf'):
        if not _HAS_PDF:
            return [], "pdf не поддерживается — pdfplumber не установлен."
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
# Проверка ликвидности телефонов
# ═════════════════════════════════════════════════════════════════════════════

# Префиксы валидных мобильных операторов RU/UA/KZ
_MOBILE_PREFIXES_RU = frozenset({
    '9', # 9xx — основной диапазон
})

# Маски: одинаковые цифры
_ALL_SAME_RE  = re.compile(r'^(\d)\1{9,}$')
# Последовательность: 1234567890 / 0987654321
_SEQUENTIAL = {'1234567890', '0987654321', '9876543210', '0123456789'}
# Явно технические/тестовые номера
_TECH_PREFIXES = ('70000', '79000', '700000', '710000', '71234', '70123')


def normalize_phone(raw: str) -> str | None:
    """
    Нормализует телефон в E.164-подобный формат (без +).
    Возвращает нормализованный номер или None если невалидный/маска.

    Поддерживает:
      RU:  7xxxxxxxxxx  / 8xxxxxxxxxx (10 цифр после 7/8)
      UA:  380xxxxxxxxx / 0xxxxxxxxx  (9 цифр после 0)
      KZ:  7xxxxxxxxxx  (10 цифр после 7)
    """
    if not raw:
        return None

    # Оставляем только цифры
    digits = re.sub(r'\D', '', raw)
    if not digits:
        return None

    # Длина: минимум 7, максимум 15 (E.164)
    if not (7 <= len(digits) <= 15):
        return None

    # Нормализация российских/казахских (7xxxxxxxxxx / 8xxxxxxxxxx)
    if len(digits) == 11 and digits[0] in ('7', '8'):
        normalized = '7' + digits[1:]
    # Украинских с 0 впереди (0xxxxxxxxx → 380xxxxxxxxx)
    elif len(digits) == 10 and digits[0] == '0':
        normalized = '380' + digits[1:]
    # Украинских уже с кодом 380
    elif len(digits) == 12 and digits.startswith('380'):
        normalized = digits
    # Прочие 10-значные
    elif len(digits) == 10:
        normalized = digits
    else:
        normalized = digits

    # ── Проверка на маски и мусор ─────────────────────────────────────────

    # Все одинаковые цифры
    if _ALL_SAME_RE.match(normalized):
        return None

    # Последовательные
    tail10 = normalized[-10:]
    if tail10 in _SEQUENTIAL:
        return None

    # Технические префиксы
    if any(normalized.startswith(p) for p in _TECH_PREFIXES):
        return None

    # Явно невалидные: 0000x, все нули
    if normalized.replace('0', '') == '':
        return None

    # Российский: цифра после 7 должна быть 9 (мобильный) или 4/8 (городской)
    if len(normalized) == 11 and normalized.startswith('7'):
        second = normalized[1]
        if second not in ('9', '4', '8', '3', '2'):
            return None

    return normalized


def check_phone_liquidity(raw: str, seen_normalized: set[str]) -> tuple[str | None, str]:
    """
    Проверяет ликвидность телефона.

    Возвращает (normalized_phone | None, reason_if_rejected).
    При валидном номере добавляет в seen_normalized (dedup).
    """
    norm = normalize_phone(raw)
    if norm is None:
        return None, f"невалидный формат: «{raw[:20]}»"
    if norm in seen_normalized:
        return None, f"дубль: {norm}"
    seen_normalized.add(norm)
    return norm, ""


def filter_liquid_phones(
    raw_phones_str: str,
    seen_normalized: set[str],
    max_phones: int = 20,
) -> tuple[list[str], int, list[str]]:
    """
    Из строки с телефонами (разделитель ';') возвращает:
      (liquid_list, discarded_count, reject_reasons)

    seen_normalized — общий глобальный set для дедупликации между записями.
    """
    liquid: list[str]  = []
    discarded = 0
    reasons: list[str] = []

    raw_list = [p.strip() for p in re.split(r'[;,\s]+', raw_phones_str) if p.strip()]
    for raw in raw_list[:max_phones]:
        norm, reason = check_phone_liquidity(raw, seen_normalized)
        if norm:
            liquid.append(norm)
        else:
            discarded += 1
            if reason:
                reasons.append(reason)

    return liquid, discarded, reasons


# ═════════════════════════════════════════════════════════════════════════════
# Детектор Макса / Максима / taxsee
# ═════════════════════════════════════════════════════════════════════════════

# Все известные маркеры присутствия в агрегаторе Maxim/taxsee.
# Проверяем поле Источник, поэтому контекст — название базы, не имя человека.
_MAXIM_RE = re.compile(
    r'taxsee|'
    r'\bmaxim(um)?\b|'                          # "Maxim", "Maximum" (латиница)
    r'максим(а|у|ом|е|овск)?\b|'               # "Максим", "Максимовск…" (кириллица)
    r'mac[s]?tax|мак[сш]такс|maxtax|'
    r'клиент[ыа]?\s*(maxim|макс|такси)|'
    r'база\s*(maxim|макс)|'
    r'driver\s*base\s*(maxim|макс)?',
    re.IGNORECASE,
)

# Более широкий паттерн по слову «Макс» отдельно — только в контексте «такси»
_MAXIM_BROAD_RE = re.compile(
    r'\bмакс\b.{0,20}(такси|taxi|водит|шофер|driver)',
    re.IGNORECASE,
)


def check_maxim(sources: str, raw_records: list[dict] | None = None) -> tuple[bool, str]:
    """
    Проверяет, есть ли признаки Maksa/taxsee в строке источников или raw Sauron-записях.

    Возвращает (in_maxim: bool, matched_source: str).
    """
    combined = sources or ''
    if raw_records:
        for rec in raw_records:
            src = str(rec.get('Источник', '') or rec.get('База', '') or '')
            combined += '\n' + src

    m = _MAXIM_RE.search(combined)
    if m:
        # Возвращаем фрагмент контекста
        start = max(0, m.start() - 15)
        end   = min(len(combined), m.end() + 15)
        snippet = combined[start:end].replace('\n', ' ').strip()
        return True, snippet

    m2 = _MAXIM_BROAD_RE.search(combined)
    if m2:
        start = max(0, m2.start() - 10)
        end   = min(len(combined), m2.end() + 10)
        snippet = combined[start:end].replace('\n', ' ').strip()
        return True, snippet

    return False, ''


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
    Извлекает структурированные поля из ответа Sauron.

    Возвращает dict:
        found_fio, phones_raw, emails, addresses, connections, sources,
        raw_count, related_fios, raw_records
    """
    records = api_result.get("response", [])
    if not records:
        return {
            "found_fio": "", "phones_raw": [], "emails": "",
            "addresses": "", "connections": "", "sources": "",
            "raw_count": 0, "related_fios": [], "raw_records": [],
        }

    all_fios:    list[str] = []
    all_phones:  list[str] = []
    all_emails:  list[str] = []
    all_addrs:   list[str] = []
    all_conns:   list[str] = []
    all_sources: list[str] = []
    related_fios: list[str] = []

    for rec in records:
        fio = _val(rec, 'ФИО', 'Фамилия', 'Имя')
        if fio and fio not in all_fios:
            all_fios.append(fio)

        for pf in ('Телефон', 'Телефон2', 'Телефон3', 'Phone', 'Моб', 'Mob'):
            ph = _val(rec, pf)
            if ph and ph not in all_phones:
                all_phones.append(ph)

        for ef in ('Email', 'E-mail', 'Эл. почта', 'Почта'):
            em = _val(rec, ef)
            if em and em not in all_emails:
                all_emails.append(em)

        addr_parts = []
        for af in ('Страна', 'Регион', 'Город', 'Населенный пункт', 'Адрес', 'Улица', 'Дом', 'Квартира'):
            v = _val(rec, af)
            if v:
                addr_parts.append(v)
        if addr_parts:
            addr = ', '.join(addr_parts)
            if addr not in all_addrs:
                all_addrs.append(addr)

        conn = _val(rec, 'Связь с лицом', 'Связанные лица', 'Связи', 'Родственники')
        if conn and conn not in all_conns:
            all_conns.append(conn)
            fio_part = re.split(r'[\d(]', conn)[0].strip()
            if fio_part and len(fio_part.split()) >= 2 and fio_part not in related_fios:
                related_fios.append(fio_part)

        src = _val(rec, 'Источник', 'База', 'Источники')
        if src and src not in all_sources:
            all_sources.append(src)

    def join(lst: list[str], n: int = 10, sep: str = '; ') -> str:
        return sep.join(lst[:n])

    return {
        "found_fio":   join(all_fios, 5),
        "phones_raw":  all_phones,                 # сырые телефоны (список)
        "emails":      join(all_emails, 5),
        "addresses":   join(all_addrs, 5),
        "connections": join(all_conns, 10),
        "sources":     join(all_sources, 10),
        "raw_count":   len(records),
        "related_fios": related_fios[:DEFAULT_MAX_RELATED],
        "raw_records": records,
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
      2. Фильтрует телефоны основного человека на ликвидность
      3. Проверяет наличие в Максе/taxsee по источникам
      4. Для связанных лиц (глубина 1, лимит MAX_RELATED):
         — ищет в Sauron
         — фильтрует их телефоны на ликвидность (дедупликация глобальная)
         — проверяет Макса для связанных

    Результат содержит новые поля:
      liquid_phones       — ликвидные тел. основного чел.
      discarded_phones    — кол-во отброшенных тел. основного чел.
      in_maxim            — найден в Максе (True/False)
      maxim_source        — контекст источника Макса
      liquid_rel_phones   — ликвидные тел. связанных (фио: тел; ...)
      rel_discarded       — кол-во отброшенных тел. связанных
      rel_in_maxim        — связанный найден в Максе
      phone_check_note    — краткий комментарий по фильтрации
    """
    import sauron as _sauron

    to_search = records[:max_records]
    skipped   = len(records) - len(to_search)
    results: list[dict] = []
    stop_reason = ""

    # Глобальный set нормализованных телефонов (дедуп по всему файлу)
    global_seen_phones: set[str] = set()

    def _do_search(query: str) -> tuple[bool, dict, bool]:
        try:
            api_result = _sauron._api_post_search(query)
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
        except Exception:
            return False, {}, False

    total = len(to_search)
    for i, record in enumerate(to_search, 1):
        fio = record['fio']

        # ── Прогресс ────────────────────────────────────────────────────────
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
            "row_num":          record['row_num'],
            "source_fio":       fio,
            "found":            False,
            "found_fio":        "",
            "phones_raw":       "",
            "liquid_phones":    "",
            "discarded_phones": 0,
            "emails":           "",
            "addresses":        "",
            "connections":      "",
            "in_maxim":         False,
            "maxim_source":     "",
            # Связанные лица
            "related_fios_found":  "",
            "liquid_rel_phones":   "",
            "rel_discarded":       0,
            "rel_addresses":       "",
            "rel_in_maxim":        False,
            "rel_maxim_source":    "",
            # Мета
            "sources":          "",
            "raw_count":        0,
            "confidence":       "",
            "phone_check_note": "",
            "error":            "",
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
            result['emails']      = fields['emails']
            result['addresses']   = fields['addresses']
            result['connections'] = fields['connections']
            result['sources']     = fields['sources']
            result['raw_count']   = fields['raw_count']

            # Ликвидность основных телефонов
            raw_phones_str = '; '.join(fields['phones_raw'])
            result['phones_raw'] = raw_phones_str

            liquid, disc, reasons = filter_liquid_phones(
                raw_phones_str, global_seen_phones
            )
            result['liquid_phones']    = '; '.join(liquid)
            result['discarded_phones'] = disc
            if reasons or disc:
                note_parts = []
                if disc:
                    note_parts.append(f"отброшено {disc}")
                if reasons:
                    note_parts.append('; '.join(reasons[:3]))
                result['phone_check_note'] = ', '.join(note_parts)

            # Проверка Макса для основного человека
            in_maxim, maxim_src = check_maxim(
                fields['sources'], fields.get('raw_records')
            )
            result['in_maxim']    = in_maxim
            result['maxim_source'] = maxim_src

            # Уверенность совпадения
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

            # ── Поиск связанных лиц ───────────────────────────────────────
            related_fios_list = fields.get('related_fios', [])
            rel_fios_found:    list[str] = []
            rel_liquid_all:    list[str] = []
            rel_addrs_list:    list[str] = []
            rel_disc_total = 0
            rel_in_maxim_found = False
            rel_maxim_sources: list[str] = []

            for rel_fio in related_fios_list[:DEFAULT_MAX_RELATED]:
                if delay_sec > 0:
                    time.sleep(delay_sec)

                rel_found, rel_fields, rel_stop = _do_search(rel_fio)

                if rel_stop:
                    stop_reason = "Остановлено при поиске связанных — баланс или ошибка API"
                    break

                if rel_found:
                    rel_fios_found.append(rel_fio)

                    # Ликвидные телефоны связанного
                    rel_raw_str = '; '.join(rel_fields.get('phones_raw', []))
                    rel_liquid, rel_disc, _ = filter_liquid_phones(
                        rel_raw_str, global_seen_phones,
                        max_phones=DEFAULT_MAX_REL_PHONES,
                    )
                    rel_disc_total += rel_disc

                    if rel_liquid:
                        # Формат: "Иванов Иван: 79012345678; 79012345679"
                        short_name = rel_fio.split()[0] if rel_fio.split() else rel_fio
                        rel_liquid_all.append(
                            f"{short_name}: " + '; '.join(rel_liquid)
                        )

                    # Адреса связанного
                    if rel_fields.get('addresses'):
                        short_name = rel_fio.split()[0] if rel_fio.split() else rel_fio
                        rel_addrs_list.append(
                            f"{short_name}: {rel_fields['addresses'][:100]}"
                        )

                    # Макс у связанного
                    rel_mx, rel_mx_src = check_maxim(
                        rel_fields.get('sources', ''),
                        rel_fields.get('raw_records'),
                    )
                    if rel_mx:
                        rel_in_maxim_found = True
                        rel_maxim_sources.append(f"{rel_fio}: {rel_mx_src}")

            result['related_fios_found'] = '; '.join(rel_fios_found)
            result['liquid_rel_phones']  = '\n'.join(rel_liquid_all)
            result['rel_discarded']      = rel_disc_total
            result['rel_addresses']      = '; '.join(rel_addrs_list)
            result['rel_in_maxim']       = rel_in_maxim_found
            result['rel_maxim_source']   = '; '.join(rel_maxim_sources)

            if stop_reason:
                results.append(result)
                break

        else:
            result['found']      = False
            result['confidence'] = "не найден"

        results.append(result)

        if stop_reason:
            break

        if i < total:
            time.sleep(delay_sec)

    return results, stop_reason


# ═════════════════════════════════════════════════════════════════════════════
# Формирование отчётов
# ═════════════════════════════════════════════════════════════════════════════

def build_short_summary(results: list[dict], stop_reason: str = "") -> str:
    """Краткая сводка в чат."""
    total     = len(results)
    found     = sum(1 for r in results if r.get('found'))
    errors    = sum(1 for r in results if r.get('error') and not r.get('found'))
    not_found = total - found - errors

    # Статистика по новым полям
    rel_found_total = sum(
        len([x for x in r.get('related_fios_found', '').split(';') if x.strip()])
        for r in results if r.get('found')
    )
    liquid_phones_total = sum(
        len([x for x in r.get('liquid_phones', '').split(';') if x.strip()])
        + len([x for x in r.get('liquid_rel_phones', '').replace('\n', ';').split(';') if x.strip()])
        for r in results if r.get('found')
    )
    maxim_total = sum(
        1 for r in results
        if r.get('in_maxim') or r.get('rel_in_maxim')
    )

    lines = ["🔍 *Поиск по файлу завершён*\n"]
    lines.append(f"👤 Найдено ФИО: *{found}*")
    lines.append(f"❌ Не найдено: *{not_found}*")
    if errors:
        lines.append(f"⚠️ Ошибки: *{errors}*")
    lines.append(f"📊 Обработано: *{total}*")
    lines.append("")
    lines.append(f"🔗 Связанных лиц найдено: *{rel_found_total}*")
    lines.append(f"📱 Ликвидных номеров: *{liquid_phones_total}*")
    lines.append(f"🚕 Совпадений в Максе/taxsee: *{maxim_total}*")

    if stop_reason:
        lines.append(f"\n⛔ Остановлено: _{stop_reason}_")

    # Топ найденных с ликвидными номерами
    has_liquid = [r for r in results if r.get('found') and r.get('liquid_phones')]
    if has_liquid:
        lines.append("\n*Топ найденных (с номерами):*")
        for r in has_liquid[:8]:
            ph = r['liquid_phones'].split(';')[0].strip()
            maxim_flag = " 🚕" if r.get('in_maxim') or r.get('rel_in_maxim') else ""
            lines.append(f"\n👤 `{r['source_fio']}`{maxim_flag}\n  📱 {ph}")
        if len(has_liquid) > 8:
            lines.append(f"_…и ещё {len(has_liquid) - 8}_")

    return '\n'.join(lines)


def build_csv_report(results: list[dict]) -> bytes:
    """CSV со всеми колонками включая ликвидность и Макс."""
    columns = [
        "Исходное ФИО",
        "Строка №",
        "Найдено",
        "Найденное ФИО",
        "Ликвидные тел. (осн.)",
        "Все тел. (сырые)",
        "Отброшено тел.",
        "Email",
        "Адреса",
        "В Максе",
        "Источник Макса",
        "Связанные лица",
        "Ликвидные тел. связанных",
        "Адреса связанных",
        "Связ. в Максе",
        "Источник Макса (связ.)",
        "Найд. связанные ФИО",
        "Источники",
        "Записей в Sauron",
        "Уверенность",
        "Комментарий по номерам",
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
            r.get('liquid_phones', ''),
            r.get('phones_raw', ''),
            r.get('discarded_phones', ''),
            r.get('emails', ''),
            r.get('addresses', ''),
            "Да" if r.get('in_maxim') else "Нет",
            r.get('maxim_source', ''),
            r.get('connections', ''),
            r.get('liquid_rel_phones', '').replace('\n', '; '),
            r.get('rel_addresses', ''),
            "Да" if r.get('rel_in_maxim') else "Нет",
            r.get('rel_maxim_source', ''),
            r.get('related_fios_found', ''),
            r.get('sources', ''),
            r.get('raw_count', ''),
            r.get('confidence', ''),
            r.get('phone_check_note', ''),
            r.get('error', ''),
        ])

    return output.getvalue().encode('utf-8-sig')


def build_xlsx_report(results: list[dict]) -> bytes | None:
    """XLSX с форматированием: ликвидность, Макс, связанные."""
    if not _HAS_OPENPYXL:
        return None

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sauron Report"

    HEADER_FILL  = PatternFill("solid", fgColor="1F4E79")
    FOUND_FILL   = PatternFill("solid", fgColor="E2EFDA")
    NOTFND_FILL  = PatternFill("solid", fgColor="FCE4D6")
    MAXIM_FILL   = PatternFill("solid", fgColor="FFF2CC")   # жёлтый — есть в Максе
    HEADER_FONT  = Font(bold=True, color="FFFFFF")
    MAXIM_FONT   = Font(bold=True, color="7F6000")
    WRAP         = Alignment(wrap_text=True, vertical="top")

    columns = [
        ("Исходное ФИО",           28),
        ("Строка №",                7),
        ("Найдено",                 8),
        ("Найденное ФИО",          28),
        ("Ликвидные тел. (осн.)",  26),
        ("Все тел. (сырые)",       26),
        ("Отброшено тел.",         10),
        ("Email",                  20),
        ("Адреса",                 38),
        ("В Максе",                 9),
        ("Источник Макса",         28),
        ("Связанные лица",         30),
        ("Ликвид. тел. связ.",     30),
        ("Адреса связанных",       35),
        ("Связ. в Максе",           9),
        ("Ист. Макса (связ.)",     28),
        ("Найд. связ. ФИО",        24),
        ("Источники",              28),
        ("Записей в Sauron",       10),
        ("Уверенность",            12),
        ("Коммент. по номерам",    25),
        ("Ошибка",                 28),
    ]

    for col_idx, (col_name, col_width) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font      = HEADER_FONT
        cell.fill      = HEADER_FILL
        cell.alignment = WRAP
        ws.column_dimensions[cell.column_letter].width = col_width

    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 36

    for row_idx, r in enumerate(results, 2):
        in_maxim_any = r.get('in_maxim') or r.get('rel_in_maxim')
        row_fill = (
            MAXIM_FILL  if r.get('found') and in_maxim_any else
            FOUND_FILL  if r.get('found') else
            NOTFND_FILL
        )

        values = [
            r.get('source_fio', ''),
            r.get('row_num', ''),
            "Да" if r.get('found') else "Нет",
            r.get('found_fio', ''),
            r.get('liquid_phones', ''),
            r.get('phones_raw', ''),
            r.get('discarded_phones', '') or '',
            r.get('emails', ''),
            r.get('addresses', ''),
            "✅ Да" if r.get('in_maxim') else "Нет",
            r.get('maxim_source', ''),
            r.get('connections', ''),
            r.get('liquid_rel_phones', '').replace('\n', '\n'),   # сохраняем переносы строк
            r.get('rel_addresses', ''),
            "✅ Да" if r.get('rel_in_maxim') else "Нет",
            r.get('rel_maxim_source', ''),
            r.get('related_fios_found', ''),
            r.get('sources', ''),
            r.get('raw_count', ''),
            r.get('confidence', ''),
            r.get('phone_check_note', ''),
            r.get('error', ''),
        ]

        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.alignment = WRAP
            # Красим только первые 3 колонки (ключевые идентификаторы)
            if col_idx in (1, 3):
                cell.fill = row_fill
            # Колонки «В Максе» — жёлтый если да
            if col_idx in (10, 15) and val and val.startswith("✅"):
                cell.fill = MAXIM_FILL
                cell.font = MAXIM_FONT

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_summary(records: list[dict], filename: str, max_records: int = DEFAULT_MAX_FIO) -> str:
    """Preview до начала поиска."""
    total      = len(records)
    will_check = min(total, max_records)
    skipped    = total - will_check

    lines = [f"📄 *Файл:* `{filename}`", ""]
    lines.append(f"👤 *Найдено ФИО:* {total}")

    for r in records[:8]:
        lines.append(f"  • {r['fio']}")
    if total > 8:
        lines.append(f"  _…ещё {total - 8}_")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
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
