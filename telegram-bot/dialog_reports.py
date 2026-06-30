import html
import os
import re
from datetime import datetime


DIALOG_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".aac", ".flac", ".webm"}


def dialog_progress_text(filename: str, percent: int, stage: str, detail: str = "") -> str:
    percent = max(0, min(100, int(percent)))
    filled = round(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)
    clean_name = (filename or "audio").replace("\n", " ").strip()
    if len(clean_name) > 70:
        clean_name = clean_name[:67] + "..."
    lines = [
        f"⏳ Обработка аудио: {clean_name}",
        f"[{bar}] {percent}%",
        stage,
    ]
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def dialog_report_filename(original_filename: str, now: datetime | None = None) -> str:
    base = os.path.splitext(os.path.basename(original_filename or "audio"))[0]
    base = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", base).strip("_")
    if len(base) > 40:
        base = base[:40].strip("_")
    if not base:
        base = "audio"
    ts = (now or datetime.now()).strftime("%Y%m%d_%H%M")
    return f"dialog_analysis_{base}_{ts}.html"


def clean_dialog_analysis_markdown(analysis: str) -> str:
    lines = (analysis or "").replace("\r\n", "\n").split("\n")
    cleaned = []
    at_top = True
    for line in lines:
        stripped = line.strip()
        if at_top:
            if not stripped:
                continue
            if re.match(r"^#\s*Анализ аудиозаписи разговора\s*$", stripped, re.IGNORECASE):
                continue
            if re.match(r"^(Файл|Длительность по распознаванию)\s*:", stripped, re.IGNORECASE):
                continue
            at_top = False
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _inline_markdown_to_html(text: str) -> str:
    out = html.escape(text or "")
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", out)
    return out


def _is_table_separator(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 2:
        return False
    return all(re.match(r"^:?-{3,}:?$", c or "") for c in cells)


def _split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _markdown_table_to_html(table_lines: list[str]) -> str:
    rows = [_split_table_row(line) for line in table_lines if not _is_table_separator(line)]
    if not rows:
        return ""
    headers = rows[0]
    body_rows = rows[1:]
    parts = ["<div class=\"table-wrap\"><table><thead><tr>"]
    for cell in headers:
        parts.append(f"<th>{_inline_markdown_to_html(cell)}</th>")
    parts.append("</tr></thead><tbody>")
    for row in body_rows:
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        parts.append("<tr>")
        for cell in row[:len(headers)]:
            parts.append(f"<td>{_inline_markdown_to_html(cell)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def markdown_to_report_html(markdown_text: str) -> str:
    lines = (markdown_text or "").replace("\r\n", "\n").split("\n")
    out = []
    paragraph = []
    in_ul = False
    in_ol = False
    in_code = False
    code_lines = []

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            text = " ".join(p.strip() for p in paragraph if p.strip())
            out.append(f"<p>{_inline_markdown_to_html(text)}</p>")
            paragraph = []

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def flush_code():
        nonlocal code_lines
        if code_lines:
            code_text = html.escape("\n".join(code_lines))
            out.append(f"<pre><code>{code_text}</code></pre>")
            code_lines = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if in_code:
            if stripped.startswith("```"):
                in_code = False
                flush_code()
            else:
                code_lines.append(line)
            i += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            close_lists()
            in_code = True
            i += 1
            continue

        if not stripped:
            flush_paragraph()
            close_lists()
            i += 1
            continue

        if "|" in stripped and i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
            flush_paragraph()
            close_lists()
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i].strip():
                table_lines.append(lines[i])
                i += 1
            out.append(_markdown_table_to_html(table_lines))
            continue

        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            close_lists()
            level = 2 if len(heading.group(1)) <= 2 else 3
            out.append(f"<h{level}>{_inline_markdown_to_html(heading.group(2).strip())}</h{level}>")
            i += 1
            continue

        bullet = re.match(r"^[-•]\s+(.+)$", stripped)
        if bullet:
            flush_paragraph()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline_markdown_to_html(bullet.group(1))}</li>")
            i += 1
            continue

        numbered = re.match(r"^\d+[\.\)]\s+(.+)$", stripped)
        if numbered:
            flush_paragraph()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline_markdown_to_html(numbered.group(1))}</li>")
            i += 1
            continue

        close_lists()
        paragraph.append(line)
        i += 1

    flush_paragraph()
    close_lists()
    flush_code()
    return "\n".join(part for part in out if part)


def dialog_analysis_is_meaningful(analysis: str) -> bool:
    clean = clean_dialog_analysis_markdown(analysis)
    if len(clean.strip()) < 6000:
        return False
    lowered = clean.lower()
    markers = (
        "короткий вывод",
        "диагноз менеджера",
        "главные оценки",
        "оценки",
        "что найдено",
        "общая оценка",
        "оценка менеджера",
        "оценка переговорщика",
        "подробный разбор",
        "разбор по репликам",
        "таблица ошибок",
        "топ-10",
        "тренерские заметки",
        "контроль разговора",
        "доверие",
        "рекоменда",
        "ошибк",
    )
    return sum(1 for marker in markers if marker in lowered) >= 8


def is_llm_error(text: str) -> bool:
    value = str(text or "").lstrip()
    return value.startswith(("❌", "⏳"))


def compact_dialog_transcript(text: str, limit: int = 12000) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    head_len = max(1000, int(limit * 0.55))
    tail_len = max(1000, limit - head_len)
    return (
        text[:head_len].rstrip()
        + "\n\n[СЕРЕДИНА ТРАНСКРИПТА СОКРАЩЕНА: Kryven не принял полный объём за один запрос]\n\n"
        + text[-tail_len:].lstrip()
    )


def split_dialog_transcript(text: str, max_chars: int = 8500, max_chunks: int = 8) -> list[str]:
    lines = str(text or "").splitlines()
    if not lines:
        return []

    chunks = []
    current = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current).strip())
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append("\n".join(current).strip())

    chunks = [chunk for chunk in chunks if chunk]
    if len(chunks) <= max_chunks:
        return chunks

    keep_start = max_chunks // 2
    keep_end = max_chunks - keep_start
    middle_note = (
        "[Часть средних фрагментов не отправлена отдельным запросом, потому что запись слишком длинная. "
        "В финальном отчёте учитывай, что выводы ограничены доступным объёмом.]"
    )
    return chunks[:keep_start] + [middle_note] + chunks[-keep_end:]


def build_local_dialog_fallback_report(
    filename: str,
    duration_text: str,
    transcript_text: str,
    reason: str = "",
) -> str:
    excerpt = compact_dialog_transcript(transcript_text, 30000)
    note = reason.strip() or "Kryven не вернул содержательный аналитический текст."
    return (
        "# Отчет CallInsight\n\n"
        f"Файл: `{filename or 'audio'}`\n"
        "Модель: `Whisper + Kryven`\n"
        "Язык: `ru`\n"
        f"Длительность по распознаванию: {duration_text or 'не определена'}\n"
        "Фокус: `Разбор диалога`\n"
        "Цель звонящего: `Понять суть диалога, ошибки, риски и итог разговора`\n"
        "Контекст: это скрипт Днепровского офиса по РФ.\n"
        f"Качество аналитики: автоматический резервный отчёт. Причина: {note}\n\n"
        "## Короткий вывод\n\n"
        "Kryven не вернул достаточно большой аналитический отчёт. Ниже сохранён расширенный резервный файл: "
        "шаблон оценки, причина сбоя и расшифровка, чтобы материал не потерялся.\n\n"
        "## Общая оценка\n\n"
        "| Пункт | Вывод |\n"
        "|---|---|\n"
        "| Цель разговора | Нужно повторить LLM-анализ по сохранённой расшифровке |\n"
        "| Достигнута ли цель | Полная аналитика не построена автоматически |\n"
        "| Кто контролировал диалог | Нужен повторный разбор |\n"
        "| Кто выглядел увереннее | Нужен повторный разбор |\n"
        "| Кто выглядел слабее | Нужен повторный разбор |\n"
        "| Эмоциональный фон | Нужен повторный разбор |\n"
        "| Степень уверенности вывода | низкая: это резервный файл без полноценного LLM-разбора |\n\n"
        "## Диагноз менеджера\n\n"
        "**Уровень:** не определён автоматически.  \n"
        "**Вердикт:** повтори обработку, чтобы получить полноценный CallInsight-отчёт.\n\n"
        "## Оценки\n\n"
        "| Критерий | Балл 1-10 | Что видно в диалоге | Почему такой балл | Как улучшить |\n"
        "|---|---:|---|---|---|\n"
        "| Контроль разговора | н/д | Нужен содержательный LLM-разбор | Kryven не вернул полный отчёт | Повторить обработку записи |\n"
        "| Доверие | н/д | Нужен содержательный LLM-разбор | Kryven не вернул полный отчёт | Повторить обработку записи |\n"
        "| Ясность инструкции | н/д | Нужен содержательный LLM-разбор | Kryven не вернул полный отчёт | Повторить обработку записи |\n"
        "| Движение к легитимной цели | н/д | Нужен содержательный LLM-разбор | Kryven не вернул полный отчёт | Повторить обработку записи |\n"
        "| Проверяемость и корректность | н/д | Нужен содержательный LLM-разбор | Kryven не вернул полный отчёт | Повторить обработку записи |\n"
        "| Потенциал конверсии | н/д | Нужен содержательный LLM-разбор | Kryven не вернул полный отчёт | Повторить обработку записи |\n\n"
        "## Что улучшить\n\n"
        "- Повторить обработку этой записи.\n"
        "- Если запись длинная, отправить её частями.\n"
        "- Проверить, что Kryven отвечает большим текстом, а не короткой шапкой.\n\n"
        "## Расшифровка\n\n"
        "```text\n"
        f"{excerpt}\n"
        "```\n\n"
        "## Важные моменты\n\n"
        "Файл не пустой: расшифровка сохранена. Для полноценной оценки менеджера, контроля, доверия, ясности инструкции "
        "и соответствия скрипту Днепровского офиса по РФ отправь запись повторно или раздели её на более короткие части."
    )


def _extract_outline(markdown_text: str, limit: int = 9) -> list[str]:
    items = []
    for line in (markdown_text or "").splitlines():
        match = re.match(r"^##\s+(.+)$", line.strip())
        if match:
            title = match.group(1).strip()
            if title and title not in items:
                items.append(title)
        if len(items) >= limit:
            break
    return items


def _outline_html(items: list[str]) -> str:
    if not items:
        return ""
    links = "".join(f"<span>{html.escape(item)}</span>" for item in items)
    return f"<div class=\"outline\">{links}</div>"


def build_dialog_report_html(
    filename: str,
    duration_text: str,
    analysis: str,
    *,
    generated_at: datetime | None = None,
) -> str:
    clean_analysis = clean_dialog_analysis_markdown(analysis)
    body_html = markdown_to_report_html(clean_analysis)
    if not body_html.strip():
        body_html = (
            "<div class=\"notice\"><strong>Отчёт не содержит аналитического текста.</strong><br>"
            "Модель вернула пустую шапку без разбора. Повтори обработку записи.</div>"
        )
    safe_filename = html.escape(filename or "audio")
    safe_duration = html.escape(duration_text or "не определена")
    generated_value = (generated_at or datetime.now()).strftime("%d.%m.%Y %H:%M")
    safe_generated_at = html.escape(generated_value)
    outline = _outline_html(_extract_outline(clean_analysis))
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Анализ диалога</title>
  <style>
    :root {{
      --bg: #eef2f5;
      --paper: #ffffff;
      --ink: #17212b;
      --muted: #637083;
      --line: #d8e0e8;
      --accent: #176b87;
      --accent-2: #102a43;
      --mint: #dff3ed;
      --soft: #eef7fa;
      --gold: #f7c873;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      line-height: 1.58;
    }}
    .page {{
      max-width: 1040px;
      margin: 0 auto;
      padding: 24px;
    }}
    .cover {{
      position: relative;
      overflow: hidden;
      background:
        linear-gradient(135deg, rgba(16,42,67,.98), rgba(23,107,135,.96)),
        radial-gradient(circle at 85% 20%, rgba(247,200,115,.35), transparent 36%);
      color: #fff;
      border-radius: 18px;
      padding: 32px;
      box-shadow: 0 18px 44px rgba(16, 42, 67, .18);
    }}
    .eyebrow {{
      margin: 0 0 10px;
      color: rgba(255,255,255,.78);
      font-size: 13px;
      letter-spacing: .08em;
      text-transform: uppercase;
      font-weight: 800;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(31px, 7vw, 52px);
      line-height: 1.03;
      letter-spacing: 0;
    }}
    .subtitle {{
      margin: 14px 0 0;
      max-width: 760px;
      color: rgba(255,255,255,.86);
      font-size: 16px;
    }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-top: 24px;
    }}
    .meta-card {{
      background: rgba(255,255,255,.12);
      border: 1px solid rgba(255,255,255,.2);
      border-radius: 12px;
      padding: 13px;
      min-width: 0;
    }}
    .meta-label {{
      display: block;
      color: rgba(255,255,255,.68);
      font-size: 12px;
      margin-bottom: 5px;
    }}
    .meta-value {{
      display: block;
      overflow-wrap: anywhere;
      font-weight: 800;
    }}
    .outline {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 18px 0 0;
    }}
    .outline span {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 5px 10px;
      border-radius: 999px;
      color: #d9f4ff;
      background: rgba(255,255,255,.12);
      border: 1px solid rgba(255,255,255,.18);
      font-size: 12px;
      font-weight: 700;
    }}
    .content {{
      margin-top: 18px;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 30px;
      box-shadow: 0 10px 28px rgba(16, 42, 67, .08);
    }}
    .notice {{
      border: 1px solid #f0c36d;
      background: #fff8e6;
      color: #5f4200;
      border-radius: 12px;
      padding: 14px 16px;
      margin: 0 0 18px;
    }}
    h2 {{
      margin: 34px 0 14px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
      color: var(--accent-2);
      font-size: 24px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    h2:first-child {{ margin-top: 0; padding-top: 0; border-top: 0; }}
    h3 {{
      margin: 24px 0 10px;
      color: var(--accent);
      font-size: 18px;
      line-height: 1.3;
      letter-spacing: 0;
    }}
    p {{ margin: 10px 0; }}
    ul, ol {{ padding-left: 22px; }}
    li {{ margin: 6px 0; }}
    strong {{ color: #111827; }}
    pre {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      margin: 14px 0 22px;
    }}
    code {{
      background: var(--soft);
      border: 1px solid #d3e9ef;
      border-radius: 6px;
      padding: 1px 5px;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: .95em;
    }}
    pre code {{
      background: transparent;
      border: 0;
      padding: 0;
      font-size: .9em;
    }}
    .table-wrap {{
      width: 100%;
      overflow-x: auto;
      margin: 14px 0 22px;
      border: 1px solid var(--line);
      border-radius: 12px;
    }}
    table {{
      width: 100%;
      min-width: 720px;
      border-collapse: collapse;
      background: #fff;
      font-size: 14px;
    }}
    th {{
      background: linear-gradient(180deg, var(--soft), #e8f1f5);
      color: var(--accent-2);
      font-weight: 800;
      text-align: left;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
    }}
    td {{
      vertical-align: top;
      padding: 10px 12px;
      border-bottom: 1px solid #edf1f5;
    }}
    tr:nth-child(even) td {{ background: #fbfdfe; }}
    tr:last-child td {{ border-bottom: 0; }}
    .footer {{
      margin: 18px 4px 0;
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }}
    @media (max-width: 720px) {{
      .page {{ padding: 10px; }}
      .cover {{ border-radius: 14px; padding: 20px; }}
      .meta {{ grid-template-columns: 1fr; }}
      .content {{ border-radius: 14px; padding: 18px; }}
      h2 {{ font-size: 21px; }}
      table {{ min-width: 640px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="cover">
      <p class="eyebrow">Ruslan Helper · CallInsight</p>
      <h1>Анализ диалога</h1>
      <p class="subtitle">Профессиональный разбор аудиозаписи: роли участников, важные реплики, ошибки, психология разговора и рекомендации.</p>
      <div class="meta">
        <div class="meta-card"><span class="meta-label">Файл</span><span class="meta-value">{safe_filename}</span></div>
        <div class="meta-card"><span class="meta-label">Длительность</span><span class="meta-value">{safe_duration}</span></div>
        <div class="meta-card"><span class="meta-label">Создано</span><span class="meta-value">{safe_generated_at}</span></div>
      </div>
      {outline}
    </section>
    <section class="content">
      {body_html}
    </section>
    <p class="footer">Отчет подготовлен автоматически. Сомнительные места проверяй по исходной аудиозаписи.</p>
  </main>
</body>
</html>"""


def is_audio_doc(filename: str, mime_type) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in DIALOG_AUDIO_EXTS:
        return True
    if mime_type and str(mime_type).startswith("audio/"):
        return True
    return False


def fmt_audio_time(seconds) -> str:
    try:
        total = int(float(seconds))
    except Exception:
        return "??:??"
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _obj_get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def format_whisper_transcript(transcript_obj) -> tuple[str, str, str]:
    if isinstance(transcript_obj, str):
        text = transcript_obj.strip()
        return text, text, ""

    text = (_obj_get(transcript_obj, "text", "") or "").strip()
    duration = _obj_get(transcript_obj, "duration", "")
    segments = _obj_get(transcript_obj, "segments", None) or []

    timed_lines = []
    for seg in segments:
        seg_text = (_obj_get(seg, "text", "") or "").strip()
        if not seg_text:
            continue
        start = fmt_audio_time(_obj_get(seg, "start", ""))
        timed_lines.append(f"[{start}] {seg_text}")

    timed_text = "\n".join(timed_lines).strip() or text
    duration_text = fmt_audio_time(duration) if duration not in ("", None) else ""
    return text, timed_text, duration_text
