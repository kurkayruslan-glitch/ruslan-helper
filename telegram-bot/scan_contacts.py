"""
Фоновый сканер 200.zona.media
Ищет погибших за сен–ноя 2025 с контактами родственников (archive.ph / vk.com)
Отправляет каждую находку в Telegram и сохраняет в contacts_db.json
"""
import os, sys, requests, re, json, time, random, signal
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = 7959647798
TG_BASE  = f"https://api.telegram.org/bot{TOKEN}"

# Дата гибели: 6–8 месяцев от 14.05.2026 = 14.09.2025 – 14.11.2025
DATE_FROM = datetime(2025, 9, 14)
DATE_TO   = datetime(2025, 11, 14)
TARGET    = 20
DB_FILE   = "/home/runner/workspace/telegram-bot/contacts_db.json"
INDEX     = "/home/runner/workspace/telegram-bot/zona_index.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

def tg_send(text):
    try:
        requests.post(f"{TG_BASE}/sendMessage", json={
            "chat_id": OWNER_ID, "text": text, "disable_web_page_preview": True
        }, timeout=10)
    except: pass

def parse_date(s):
    m = re.search(r'\b(\d{2})\.(\d{2})\.(\d{4})\b', s)
    if m:
        try: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except: pass
    return None

def fetch_and_check(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200: return None
        html = r.content.decode('utf-8', errors='ignore')
        if "Just a moment" in html or r.status_code == 403: return None

        decoded  = unquote(url)
        parts    = decoded.replace("https://200.zona.media/", "").split("/")
        name     = parts[-1].replace(".html","").replace("_"," ") if parts else ""
        region   = parts[0].replace("_"," ") if len(parts)>0 else ""
        mil_type = parts[1].replace("_"," ") if len(parts)>1 else ""

        # Дата гибели из "погиб DD.MM.YYYY"
        m_death = re.search(r'погиб(?:ла)?\s+(\d{2}\.\d{2}\.\d{4})', html, re.I)
        if not m_death: return None
        death_str = m_death.group(1)
        death_dt  = parse_date(death_str)
        if not death_dt: return None
        if not (DATE_FROM <= death_dt <= DATE_TO): return None

        # Контакты
        skip = ["zona.media","google","gtm","favicon","airtable","googletagmanager","yandex.ru","report"]
        all_links = re.findall(r'href="(https?://[^"]{10,})"', html)
        vk  = [l for l in all_links if ("vk.com" in l or "vkontakte" in l) and not any(s in l for s in skip)]
        arc = [l for l in all_links if "archive.ph" in l or "web.archive.org" in l]
        if not vk and not arc: return None

        # Доп поля
        text  = re.sub(r"<[^>]+>", "\n", html)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        location = rank = ""
        for i, line in enumerate(lines):
            if "Населённый пункт" in line or "Населенный пункт" in line:
                if i+1 < len(lines): location = lines[i+1]
            if line == "Звание" and i+1 < len(lines):
                rank = lines[i+1]

        return {"name":name,"region":region,"type":mil_type,"location":location,
                "rank":rank,"death_date":death_str,"vk":vk[:5],"archive":arc[:5],"url":url}
    except: return None

def main():
    # Загружаем уже найденное
    found = []
    if os.path.exists(DB_FILE):
        with open(DB_FILE, encoding='utf-8') as f:
            found = json.load(f)
        print(f"Продолжаем: уже найдено {len(found)}")

    # Индекс
    with open(INDEX, encoding='utf-8') as f:
        all_urls = [l.strip() for l in f if l.strip()]

    # Исключаем уже найденные
    found_urls = {r['url'] for r in found}
    remaining  = [u for u in all_urls if u not in found_urls]
    random.shuffle(remaining)

    tg_send(f"🔍 Сканер запущен. Ищу погибших 14.09–14.11.2025 с контактами.\nУже найдено: {len(found)}/{TARGET}")
    print(f"Сканирую {len(remaining)} URL (перемешано)…")

    WORKERS = 8
    BATCH   = 16
    idx = 0
    checked = 0

    while len(found) < TARGET and idx < len(remaining):
        batch    = remaining[idx:idx+BATCH]
        idx     += BATCH
        checked += len(batch)

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(fetch_and_check, u): u for u in batch}
            for fut in as_completed(futs):
                res = fut.result()
                if res and len(found) < TARGET:
                    found.append(res)
                    # Сохраняем прогресс
                    with open(DB_FILE, 'w', encoding='utf-8') as f:
                        json.dump(found, f, ensure_ascii=False, indent=2)
                    
                    # Отправляем в Telegram
                    contacts_text = "\n".join(res['vk'][:3] + res['archive'][:3])
                    msg = (f"✅ [{len(found)}/{TARGET}] {res['name']}\n"
                           f"📍 {res['region']}, {res['location']}\n"
                           f"⚔️ {res['type']}{' | '+res['rank'] if res['rank'] else ''}\n"
                           f"💀 Дата гибели: {res['death_date']}\n"
                           f"🔗 Контакты родственников:\n{contacts_text}\n"
                           f"📄 {res['url']}")
                    tg_send(msg)
                    print(f"✅ [{len(found)}/{TARGET}] {res['name']} | {res['death_date']}")

        if checked % 500 == 0:
            print(f"Проверено: {checked} | Найдено: {len(found)}")
            if checked % 2000 == 0:
                tg_send(f"⏳ Сканер: проверено {checked}, найдено {len(found)}/{TARGET}")

        time.sleep(0.15)

    # Финал
    if len(found) >= TARGET:
        tg_send(f"🎉 Готово! Найдено {len(found)} контактов за сен–ноя 2025.\nФайл сохранён: contacts_db.json")
    else:
        tg_send(f"⚠️ Сканирование завершено. Найдено {len(found)}/{TARGET}.\nПроверено {checked} страниц.")
    
    print(f"\nИтог: {len(found)}/{TARGET}, проверено {checked}")

if __name__ == "__main__":
    main()
