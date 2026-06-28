# Ruslan Helper — деплой 24/7 на внешний хостинг

Бот: @Ruslan_pomohnik_bot | Polling-режим (не webhook)

---

## Обязательные env-переменные (Secrets)

```
TELEGRAM_BOT_TOKEN      — токен от @BotFather (обязателен)
LLM_BACKEND             — openai / grok / gemini / llama  (по умолчанию: openai)
OPENAI_API_KEY          — ключ OpenAI (для ChatGPT + TTS/STT)
```

Опциональные (для звонков, SMS, Sheets):
```
TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
TELNYX_API_KEY / TELNYX_FROM_NUMBER
TOHA_PHONE_NUMBER
XAI_API_KEY             — если LLM_BACKEND=grok
GEMINI_API_KEY          — если LLM_BACKEND=gemini
MONTHLY_AI_BUDGET_USD   — мягкий лимит расходов ИИ в месяц (например: 20)
DAILY_AI_BUDGET_USD     — дневной лимит (например: 2)
```

Google Sheets (если нужна интеграция):
```
GOOGLE_SERVICE_ACCOUNT_JSON   — содержимое service-account.json одной строкой
```

---

## Railway

1. Создай новый проект: **New Project → Deploy from GitHub repo**
2. Выбери репозиторий
3. Railway найдёт `Dockerfile` автоматически
4. В Settings → Variables добавь все Secrets из списка выше
5. В Settings → **Replicas: установи 1** (критично — см. ниже)
6. Deploy

Start command (если Railway спрашивает вручную):
```
python3 -u bot.py
```
Working directory: `telegram-bot`

---

## Render

1. New → **Web Service** → Connect repo
2. Runtime: **Docker** (найдёт `Dockerfile` в корне)  
   — или Runtime: Python, Root Directory: `telegram-bot`, Start Command: `python3 -u bot.py`
3. Instance Type: **Starter** или выше (Free засыпает — бот умрёт)
4. Environment Variables: добавь Secrets из списка выше
5. Scaling → **Max instances: 1** (критично — см. ниже)
6. Deploy

---

## VPS (Ubuntu/Debian)

```bash
# 1. Клонируй репо и установи зависимости
git clone <repo-url> ruslan-helper
cd ruslan-helper
pip3 install -r telegram-bot/requirements.txt

# 2. Создай файл с переменными окружения
cp telegram-bot/.env.example telegram-bot/.env  # или создай вручную
# Заполни TELEGRAM_BOT_TOKEN, OPENAI_API_KEY и т.д.

# 3. Запусти через systemd (пример unit-файла):
# /etc/systemd/system/ruslan-helper.service
[Unit]
Description=Ruslan Helper Telegram Bot
After=network.target

[Service]
WorkingDirectory=/path/to/ruslan-helper/telegram-bot
ExecStart=/usr/bin/python3 -u bot.py
EnvironmentFile=/path/to/ruslan-helper/telegram-bot/.env
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

# Активируй:
sudo systemctl daemon-reload
sudo systemctl enable ruslan-helper
sudo systemctl start ruslan-helper
sudo systemctl status ruslan-helper
```

---

## Почему ОБЯЗАТЕЛЬНО один процесс / одна реплика

Telegram API разрешает **только один активный long-polling соединение на токен**.

Если запустить два экземпляра бота одновременно:
- Оба получат `409 Conflict` от Telegram
- Оба упадут
- Бот перестанет отвечать

**Правило:** scale=1, replicas=1, один worker. Никаких исключений для polling-бота.

Если нужен failover — используй Telegram Webhook вместо polling (другая архитектура, в текущем коде не реализована).

---

## Как проверить что бот живой

**1. Health endpoint** (если PORT открыт хостингом):
```
https://<твой-домен>/health
```
Должен вернуть JSON:
```json
{"status": "ok", "bot": "Ruslan Helper (@Ruslan_pomohnik_bot)", "uptime_seconds": 123, ...}
```

**2. Логи запуска** — ищи эти строки:
```
🔗 Webhook сброшен — polling готов.
🕐 Polling start | Режим: DEVELOPMENT 🟡 | 2026-... Kyiv
🚀 Ruslan Personal Helper с SMS для Тохи!
```

**3. Написать боту в Telegram** — если отвечает, всё работает.

**4. Проверить что нет 409:**
В логах НЕ должно быть:
```
⛔ 409 CONFLICT: бот уже запущен в другом месте!
```
Если видишь 409 — где-то запущен второй экземпляр, найди и останови его.

---

## Структура запуска

```
telegram-bot/bot.py          — точка входа, запускай именно его
telegram-bot/keep_alive.py   — Flask health-сервер (порт из $PORT или 8000)
                               слушает: /, /health, /status, /api/twiml
```

Health-сервер запускается в daemon-потоке — не блокирует и не конкурирует с polling.
