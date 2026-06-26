# Ruslan Helper

Персональный Telegram-бот (@Ruslan_pomohnik_bot) для Руслана — владельца такси-бизнеса «Я Тигр» (Украина).

---

## ⚡ КАК СДЕЛАТЬ БОТ 24/7 (обязательно прочитай)

### Проблема
Пока бот запущен только как **Workspace Workflow** — он работает ТОЛЬКО пока открыт Replit в браузере или Codex на телефоне. Как только закрываешь → бот умирает.

### Решение: Replit Deployment → Reserved VM

```
Шаг 1. Нажми кнопку Deploy (синяя, вверху справа в Replit)
Шаг 2. Выбери тип: Reserved VM  (НЕ Autoscale — он не подходит для Telegram polling)
Шаг 3. Run command: cd telegram-bot && DEPLOYMENT_MODE=production python3 -u bot.py
Шаг 4. Добавь все Secrets (см. список ниже) в настройках Deployment
Шаг 5. Нажми Publish
Шаг 6. ВАЖНО: после деплоя НЕ запускай бот в workspace workflow одновременно!
        Telegram разрешает ТОЛЬКО ОДИН polling-процесс на токен.
        Два одновременных → 409 Conflict → оба падают.
```

**Стоимость:** Reserved VM ≈ $7/мес. Бот работает 24/7, браузер можно закрыть.

### Проверка что бот живёт
После деплоя открой в браузере:
```
https://<твой-реплит-домен>/health   → JSON со статусом и аптаймом
https://<твой-реплит-домен>/status   → текст, режим, аптайм
```
Если видишь `"mode": "production"` — бот работает в постоянном режиме ✅

---

## Secrets (переменные окружения)

Обязательные:
```
TELEGRAM_BOT_TOKEN      — токен бота от @BotFather
LLM_BACKEND             — openai / grok / gemini / llama (по умолчанию openai)
OPENAI_API_KEY          — ключ OpenAI (для ChatGPT и TTS/STT)
```

Для звонков (хотя бы одно):
```
TWILIO_ACCOUNT_SID      — Twilio Account SID
TWILIO_AUTH_TOKEN       — Twilio Auth Token
TWILIO_FROM_NUMBER      — номер Twilio (+1...)
TELNYX_API_KEY          — Telnyx API key (альтернатива Twilio)
TELNYX_FROM_NUMBER      — номер Telnyx (+...)
```

Для SMS/гео Тохе:
```
TOHA_PHONE_NUMBER       — номер Тохи (+380...)
TOHA_CHAT_ID            — Telegram chat_id Тохи (опционально)
```

Прочее:
```
XAI_API_KEY             — если LLM_BACKEND=grok
GEMINI_API_KEY          — если LLM_BACKEND=gemini
MONTHLY_AI_BUDGET_USD   — мягкий лимит расходов ИИ в месяц (например: 20)
DAILY_AI_BUDGET_USD     — дневной лимит (например: 2)
DEPLOYMENT_MODE         — устанавливается автоматически через run command (production)
```

---

## Архитектура

```
telegram-bot/bot.py          — главный файл, всё в одном (2700+ строк)
telegram-bot/keep_alive.py   — Flask сервер (порт 8000):
                               /         — health ping
                               /health   — JSON статус + аптайм
                               /status   — текст статус
                               /api/twiml — TwiML для Twilio звонков
telegram-bot/grok.py         — xAI Grok бэкенд
telegram-bot/chatgpt.py      — OpenAI ChatGPT бэкенд
telegram-bot/gemini.py       — Google Gemini бэкенд
telegram-bot/llama.py        — Ollama локальный бэкенд
telegram-bot/memory.py       — постоянная память фактов (SQLite)
telegram-bot/reminders.py    — напоминания (SQLite)
telegram-bot/tax_calendar.py — ФОП дедлайны
telegram-bot/calls.py        — звонки (Twilio / Telnyx)
telegram-bot/sheets.py       — Google Sheets интеграция
telegram-bot/sms.py          — SMS Тохе
```

---

## Workspace workflow (для разработки)

```bash
# Запуск бота в dev-режиме (только пока открыт браузер):
cd telegram-bot && python3 -u bot.py

# Переменные: берутся из telegram-bot/.env или Replit Secrets
```

**Никогда не запускай одновременно:**
- workspace workflow "Telegram Bot" И
- Replit Deployment Reserved VM

→ Получишь 409 Conflict, оба процесса упадут.

---

## Режимы запуска

| Режим | Где | Как запустить | DEPLOYMENT_MODE | 24/7? |
|-------|-----|---------------|-----------------|-------|
| Development | Replit Workspace | Workflow "Telegram Bot" | development | ❌ |
| Production | Replit Reserved VM | Deploy → Reserved VM | production | ✅ |

В production-режиме при **персистентном 409 Conflict** (5+ подряд) бот завершает процесс —
Replit автоматически перезапустит его (в workspace уже никого нет).

---

## Функции бота

- ⚡ Jarvis Mode — командный центр (статус, бриф, что дальше, режим тишины)
- 🤖 ИИ чат — GPT/Grok/Gemini/Llama через свободный диалог
- 🧠 Память — запоминает факты между сессиями
- 🔔 Напоминания — с автоматической проверкой каждую минуту
- 📋 ФОП — налоговый календарь, дедлайны
- 📊 Google Sheets — читать, писать, анализировать
- 📞 Звонки — Twilio/Telnyx с голосовым сценарием
- 💬 SMS — отправка Тохе
- 🎤 Голос — отправляй голосовые, получай голосовые ответы (TTS)
- 💰 USDT TRC20 — аналитика кошелька
- 🚕 Тоха — геопозиция, SMS, координация
- ☀️ Утренний бриф — автоматически в 08:00 (погода, ФОП, напоминания)
