import os
import telebot
from telebot import types
from keep_alive import keep_alive

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

bot = telebot.TeleBot(TOKEN)

MAIN_MENU_BUTTONS = [
    "📊 Статистика Я Тигр",
    "🛣️ Маршрут",
    "📋 ФОП Отчёт",
    "📍 Геопозиция",
]


def main_menu_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [types.KeyboardButton(text) for text in MAIN_MENU_BUTTONS]
    markup.add(*buttons)
    return markup


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.send_message(
        message.chat.id,
        "👋 Привет, Руслан! Я твой персональный помощник.\n\nВыбери раздел:",
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "📊 Статистика Я Тигр")
def handle_statistics(message):
    bot.send_message(
        message.chat.id,
        "📊 *Статистика Я Тигр*\n\nЭтот раздел будет подключён к Google Sheets.\n\nСкоро здесь появятся данные.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "🛣️ Маршрут")
def handle_route(message):
    bot.send_message(
        message.chat.id,
        "🛣️ *Маршрут*\n\nЭтот раздел будет подключён к Google Sheets.\n\nСкоро здесь появятся данные о маршруте.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "📋 ФОП Отчёт")
def handle_fop(message):
    bot.send_message(
        message.chat.id,
        "📋 *ФОП Отчёт*\n\nЭтот раздел будет подключён к Google Sheets.\n\nСкоро здесь появятся данные по ФОП.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "📍 Геопозиция")
def handle_geo(message):
    bot.send_message(
        message.chat.id,
        "📍 *Геопозиция*\n\nЭтот раздел будет подключён позже.\n\nОтправь мне свою геопозицию, и я её сохраню.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(content_types=["location"])
def handle_location(message):
    lat = message.location.latitude
    lon = message.location.longitude
    bot.send_message(
        message.chat.id,
        f"📍 Получена геопозиция:\nШирота: `{lat}`\nДолгота: `{lon}`",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(func=lambda m: True)
def handle_other(message):
    bot.send_message(
        message.chat.id,
        "Используй кнопки меню 👇",
        reply_markup=main_menu_keyboard(),
    )


if __name__ == "__main__":
    keep_alive()
    print("Bot is starting...")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Polling error: {e}. Restarting in 5 seconds...")
            import time
            time.sleep(5)
