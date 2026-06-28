FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости отдельным слоем — кэшируется при rebuild
COPY telegram-bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Копируем только код бота
COPY telegram-bot/ .

# Health-сервер. PORT задаётся хостингом (Railway/Render) автоматически.
# Если хостинг не задаёт — используется 8000 (см. keep_alive.py).
EXPOSE 8000

# ВАЖНО: запускай ТОЛЬКО ОДИН контейнер / одну реплику.
# Telegram разрешает только один активный polling на токен.
# Два контейнера = 409 Conflict = оба падают.
CMD ["python3", "-u", "bot.py"]
