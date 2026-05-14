"""
Фоновый сканер 200.zona.media
Ищет погибших за окт–дек 2025 (5–7 месяцев назад от 14.05.2026)
с контактами родственников (archive.ph / vk.com).
Отправляет каждую находку в Telegram и сохраняет в contacts_db.json.

Используется как daemon-поток внутри bot.py (команда /zona_scan).
Флаг остановки — threading.Event, передаётся снаружи.
"""
import os, requests, re, json, time, random, threading
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = 7959647798
TG_BASE  = f"https://api.telegram.org/bot{TOKEN}"

# Дата гибели: 5–7 месяцев от 14.05.2026 = 14.10.2025 – 14.12.2025
DATE_FROM = datetime(2025, 10, 14)
DATE_TO   = datetime(2025, 12, 14)
TARGET    = 20
DB_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contacts_db.json")
INDEX     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zona_index.txt")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Глобальный флаг для остановки (используется ботом)
STOP_EVENT = threading.Event()
SCAN_LOCK  = threading.Lock()
IS_RUNNING = threading.Event()


def tg_send(text: str):
    try:
        requests.post(f"{TG_BASE}/sendMessage", json={
            "chat_id": OWNER_ID, "text": text, "disable_web_page_preview": True
        }, timeout=10)
    except Exception:
        pass


def parse_date(s: str):
    m = re.search(r'\b(\d{2})\.(\d{2})\.(\d{4})\b', s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except Exception:
            pass
    return None


def fetch_and_check(url: str):
    """Загружает страницу zona.media и возвращает запись если: дата в диапазоне И есть контакты."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return None
        html = r.content.decode('utf-8', errors='ignore')
        if "Just a moment" in html:
            return None

        decoded  = unquote(url)
        parts    = decoded.replace("https://200.zona.media/", "").split("/")
        name     = parts[-1].replace(".html", "").replace("_", " ") if parts else ""
        region   = parts[0].replace("_", " ") if len(parts) > 0 else ""
        mil_type = parts[1].replace("_", " ") if len(parts) > 1 else ""

        # Дата гибели из "погиб DD.MM.YYYY"
        m_death = re.search(r'погиб(?:ла)?\s+(\d{2}\.\d{2}\.\d{4})', html, re.I)
        if not m_death:
            return None
        death_str = m_death.group(1)
        death_dt  = parse_date(death_str)
        if not death_dt:
            return None
        if not (DATE_FROM <= death_dt <= DATE_TO):
            return None

        # Контакты: archive.ph + vk.com (с фильтром системных доменов)
        skip = ["zona.media", "google", "gtm", "favicon", "airtable",
                "googletagmanager", "yandex.ru", "report"]
        all_links = re.findall(r'href="(https?://[^"]{10,})"', html)
        vk  = [l for l in all_links
               if ("vk.com" in l or "vkontakte" in l)
               and not any(s in l for s in skip)
               and len(l) > 20]
        arc = [l for l in all_links if "archive.ph" in l or "web.archive.org" in l]
        if not vk and not arc:
            return None

        # Доп поля: населённый пункт, звание
        text  = re.sub(r"<[^>]+>", "\n", html)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        location = rank = ""
        for i, line in enumerate(lines):
            if ("Населённый пункт" in line or "Населенный пункт" in line) and i + 1 < len(lines):
                location = lines[i + 1]
            if line == "Звание" and i + 1 < len(lines):
                rank = lines[i + 1]

        return {
            "name": name, "region": region, "type": mil_type,
            "location": location, "rank": rank,
            "death_date": death_str,
            "vk": vk[:5], "archive": arc[:5], "url": url,
            "source": "zona.media",
        }
    except Exception:
        return None


def grok_web_fallback(needed: int):
    """Если zona.media даёт мало — добираем через Grok web search."""
    try:
        from grok import ask_grok
    except Exception:
        return []
    prompt = (
        f"Найди во ВКонтакте посты о погибших российских военных в период "
        f"14 октября – 14 декабря 2025 года. Нужно {needed} записей. "
        f"Для каждого укажи: ФИО, регион, дату гибели (в формате DD.MM.YYYY), "
        f"и ссылки на странички ВКонтакте родственников (мать/отец/жена/брат/сестра) "
        f"или архивные ссылки (archive.ph). "
        f"Верни строго JSON-массив объектов вида: "
        f'[{{"name":"...","region":"...","death_date":"DD.MM.YYYY",'
        f'"vk":["url1","url2"],"archive":["url1"]}}]. '
        f"Только реальные проверяемые ссылки, без выдумок."
    )
    try:
        reply = ask_grok(OWNER_ID, prompt)
        # Ищем JSON-массив в ответе
        m = re.search(r'\[[\s\S]*\]', reply)
        if not m:
            return []
        data = json.loads(m.group(0))
        out = []
        for rec in data:
            if not isinstance(rec, dict):
                continue
            dt = parse_date(rec.get("death_date", ""))
            if not dt or not (DATE_FROM <= dt <= DATE_TO):
                continue
            if not rec.get("vk") and not rec.get("archive"):
                continue
            out.append({
                "name":       rec.get("name", ""),
                "region":     rec.get("region", ""),
                "type":       "",
                "location":   "",
                "rank":       "",
                "death_date": rec.get("death_date", ""),
                "vk":         list(rec.get("vk", []))[:5],
                "archive":    list(rec.get("archive", []))[:5],
                "url":        "",
                "source":     "grok_web",
            })
        return out
    except Exception as e:
        tg_send(f"⚠️ Grok fallback failed: {str(e)[:100]}")
        return []


def main(stop_event: threading.Event = None):
    """Основной цикл сканирования. stop_event можно передать снаружи для остановки."""
    if stop_event is None:
        stop_event = STOP_EVENT
    stop_event.clear()
    IS_RUNNING.set()

    try:
        # Загружаем уже найденное
        found = []
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, encoding='utf-8') as f:
                    found = json.load(f)
            except Exception:
                found = []

        # Фильтруем по новому диапазону дат — записи из старого диапазона не считаем
        valid = []
        for rec in found:
            dt = parse_date(rec.get("death_date", ""))
            if dt and DATE_FROM <= dt <= DATE_TO:
                valid.append(rec)
        found = valid

        # Индекс
        if not os.path.exists(INDEX):
            tg_send("❌ zona_index.txt не найден. Запусти /zona_build сначала.")
            return
        with open(INDEX, encoding='utf-8') as f:
            all_urls = [l.strip() for l in f if l.strip()]

        found_urls = {r.get('url', '') for r in found if r.get('url')}
        remaining  = [u for u in all_urls if u not in found_urls]
        random.shuffle(remaining)

        tg_send(
            f"🔍 Сканер запущен. Ищу погибших 14.10–14.12.2025 с контактами.\n"
            f"Уже в базе: {len(found)}/{TARGET}\n"
            f"К проверке: {len(remaining):,} URL\n"
            f"Отправь /zona_scan stop чтобы прервать."
        )
        print(f"Сканирую {len(remaining)} URL (перемешано)…")

        WORKERS = 8
        BATCH   = 16
        idx = 0
        checked = 0
        last_status_at = 0

        while len(found) < TARGET and idx < len(remaining):
            if stop_event.is_set():
                tg_send(f"⏹ Сканер остановлен. Найдено {len(found)}/{TARGET}, проверено {checked}.")
                return

            batch    = remaining[idx:idx + BATCH]
            idx     += BATCH
            checked += len(batch)

            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = {ex.submit(fetch_and_check, u): u for u in batch}
                for fut in as_completed(futs):
                    if stop_event.is_set():
                        break
                    res = fut.result()
                    if res and len(found) < TARGET:
                        found.append(res)
                        with SCAN_LOCK:
                            with open(DB_FILE, 'w', encoding='utf-8') as f:
                                json.dump(found, f, ensure_ascii=False, indent=2)

                        contacts_text = "\n".join(res['vk'][:3] + res['archive'][:3])
                        msg = (
                            f"✅ [{len(found)}/{TARGET}] {res['name']}\n"
                            f"📍 {res['region']}, {res['location']}\n"
                            f"⚔️ {res['type']}{' | ' + res['rank'] if res['rank'] else ''}\n"
                            f"💀 Дата гибели: {res['death_date']}\n"
                            f"🔗 Контакты родственников:\n{contacts_text}\n"
                            f"📄 {res['url']}"
                        )
                        tg_send(msg)
                        print(f"✅ [{len(found)}/{TARGET}] {res['name']} | {res['death_date']}")

            # Статус каждые 500 проверенных
            if checked - last_status_at >= 500:
                last_status_at = checked
                print(f"Проверено: {checked} | Найдено: {len(found)}")
                if checked % 2000 == 0:
                    tg_send(f"⏳ Сканер: проверено {checked:,} страниц, найдено {len(found)}/{TARGET}")

            # Fallback Grok после 50К проверок если найдено < 10
            if checked == 50000 and len(found) < 10:
                tg_send(f"🌐 Найдено только {len(found)}/{TARGET} в zona.media. Подключаю Grok web search…")
                extra = grok_web_fallback(TARGET - len(found))
                for rec in extra:
                    if len(found) >= TARGET:
                        break
                    found.append(rec)
                    with SCAN_LOCK:
                        with open(DB_FILE, 'w', encoding='utf-8') as f:
                            json.dump(found, f, ensure_ascii=False, indent=2)
                    contacts_text = "\n".join(rec['vk'][:3] + rec['archive'][:3])
                    tg_send(
                        f"✅ [{len(found)}/{TARGET}] (Grok) {rec['name']}\n"
                        f"📍 {rec['region']}\n"
                        f"💀 {rec['death_date']}\n"
                        f"🔗 {contacts_text}"
                    )

            time.sleep(0.15)

        # Финал
        if len(found) >= TARGET:
            tg_send(f"🎉 Готово! Найдено {len(found)} контактов за 14.10–14.12.2025.\nПосмотреть всё: /contacts")
        else:
            tg_send(
                f"⚠️ Сканирование завершено. Найдено {len(found)}/{TARGET}.\n"
                f"Проверено {checked:,} страниц из {len(remaining):,}.\n"
                f"Посмотреть: /contacts"
            )
        print(f"\nИтог: {len(found)}/{TARGET}, проверено {checked}")
    finally:
        IS_RUNNING.clear()


if __name__ == "__main__":
    main()
