import telebot
from telebot import types
import os
import time
from keep_alive import keep_alive

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

bot = telebot.TeleBot(TOKEN)

def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("📊 Статистика Я Тигр", "🛣️ Маршрут")
    markup.add("📋 ФОП Отчёт", "📍 Геопозиция")
    markup.add("🚕 Тоха", "❓ Что ты можешь?")
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
                     "👋 Привет, Руслан! Я твой личный помощник.\n\n"
                     "Говори голосом или текстом — я всё понимаю 🔥",
                     reply_markup=main_menu())

@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    bot.send_message(message.chat.id, "🎤 Голосовое сообщение принято! Расшифровываю...")
    bot.send_message(message.chat.id, "✅ Расшифровано. Что ты сказал? (пока заглушка — скоро будет работать на 100%)")

@bot.message_handler(content_types=['location'])
def handle_location(message):
    lat = message.location.latitude
    lon = message.location.longitude
    bot.send_message(message.chat.id,
                     f"📍 Геопозиция получена!\nШирота: {lat}\nДолгота: {lon}",
                     reply_markup=main_menu())

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    text = message.text.lower()
    chat_id = message.chat.id

    if any(word in text for word in ["привет", "здравствуй", "эй", "hi"]):
        bot.send_message(chat_id, "Привет, Руслан! 😊 Как дела? Чем помочь?", reply_markup=main_menu())

    elif "как дела" in text:
        bot.send_message(chat_id, "Отлично! Готов помогать 24/7. А у тебя как? 🚀")

    elif "тигр" in text or "статистика" in text:
        bot.send_message(chat_id, "📊 Делаю полную статистику по Я Тигр из твоей таблицы «только мы»...")

    elif "маршрут" in text or "лубны" in text or "куда" in text:
        bot.send_message(chat_id, "🛣️ Отправь геопозицию или напиши куда ехать — построю маршрут через Google Maps")

    elif "тоха" in text or "водитель" in text:
        bot.send_message(chat_id, "🚕 Что сказать Тохе? Пример:\n«Тоха забери меня в 8:00 дома»")

    elif "гео" in text or "где я" in text or "геопозиция" in text:
        bot.send_message(chat_id, "📍 Отправь геопозицию — я сохраню и покажу маршрут")

    elif "фоп" in text:
        bot.send_message(chat_id, "📋 Готовлю отчёт по ФОП 3 группы. Что именно нужно?")

    elif "что можешь" in text or "что ты умеешь" in text or "❓" in text:
        bot.send_message(chat_id,
                         "Я могу очень много:\n"
                         "• Статистику по Я Тигр 📊\n"
                         "• Отчёты по ФОП 📋\n"
                         "• Маршруты через Google Maps 🛣️\n"
                         "• Работать с Тохой 🚕\n"
                         "• Принимать голосовые сообщения 🎤\n"
                         "• И многое другое!\n\nЧто сейчас нужно?",
                         reply_markup=main_menu())

    elif "спасибо" in text or "благодарю" in text:
        bot.send_message(chat_id, "Пожалуйста! Рад помочь 😊")

    else:
        bot.send_message(chat_id,
                         f"✅ Принял: «{message.text}»\n\nЧто нужно сделать дальше?",
                         reply_markup=main_menu())


if __name__ == "__main__":
    keep_alive()
    print("🚀 Бот запущен с поддержкой голоса!")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Ошибка polling: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)
