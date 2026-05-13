import telebot
from telebot import types
import os
import time
import io
from openai import OpenAI
from keep_alive import keep_alive

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

OPENAI_BASE_URL = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
OPENAI_API_KEY = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")

openai_client = OpenAI(
    base_url=OPENAI_BASE_URL,
    api_key=OPENAI_API_KEY,
)

bot = telebot.TeleBot(TOKEN)


def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("📊 Статистика Я Тигр", "🛣️ Маршрут")
    markup.add("📋 ФОП Отчёт", "📍 Геопозиция")
    markup.add("🚕 Тоха", "❓ Что ты можешь?")
    return markup


def process_text(chat_id, text):
    t = text.lower()

    if any(word in t for word in ["привет", "здравствуй", "эй", "hi"]):
        bot.send_message(chat_id, "Привет, Руслан! 😊 Как дела? Чем помочь?", reply_markup=main_menu())

    elif "как дела" in t:
        bot.send_message(chat_id, "Отлично! Готов помогать 24/7. А у тебя как? 🚀")

    elif "тигр" in t or "статистика" in t:
        bot.send_message(chat_id, "📊 Делаю полную статистику по Я Тигр из твоей таблицы «только мы»...")

    elif "маршрут" in t or "лубны" in t or "куда" in t:
        bot.send_message(chat_id, "🛣️ Отправь геопозицию или напиши куда ехать — построю маршрут через Google Maps")

    elif "тоха" in t or "водитель" in t:
        bot.send_message(chat_id, "🚕 Что сказать Тохе? Пример:\n«Тоха забери меня в 8:00 дома»")

    elif "гео" in t or "где я" in t or "геопозиция" in t:
        bot.send_message(chat_id, "📍 Отправь геопозицию — я сохраню и покажу маршрут")

    elif "фоп" in t:
        bot.send_message(chat_id, "📋 Готовлю отчёт по ФОП 3 группы. Что именно нужно?")

    elif "что можешь" in t or "что ты умеешь" in t or "❓" in t:
        bot.send_message(chat_id,
                         "Я могу очень много:\n"
                         "• Статистику по Я Тигр 📊\n"
                         "• Отчёты по ФОП 📋\n"
                         "• Маршруты через Google Maps 🛣️\n"
                         "• Работать с Тохой 🚕\n"
                         "• Принимать и расшифровывать голосовые 🎤\n"
                         "• И многое другое!\n\nЧто сейчас нужно?",
                         reply_markup=main_menu())

    elif "спасибо" in t or "благодарю" in t:
        bot.send_message(chat_id, "Пожалуйста! Рад помочь 😊")

    else:
        bot.send_message(chat_id,
                         f"✅ Принял: «{text}»\n\nЧто нужно сделать дальше?",
                         reply_markup=main_menu())


@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
                     "👋 Привет, Руслан! Я твой личный помощник.\n\n"
                     "Говори голосом или текстом — я всё понимаю 🔥",
                     reply_markup=main_menu())


@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "🎤 Голосовое получено, расшифровываю...")

    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)

        audio_file = io.BytesIO(downloaded)
        audio_file.name = "voice.ogg"

        transcript = openai_client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file,
            response_format="json",
        )
        text = transcript.text
        bot.send_message(chat_id, f"🗣️ Ты сказал: «{text}»")
        process_text(chat_id, text)

    except Exception as e:
        print(f"Ошибка расшифровки: {e}")
        bot.send_message(chat_id, "⚠️ Не удалось расшифровать голосовое. Попробуй ещё раз.")


@bot.message_handler(content_types=['location'])
def handle_location(message):
    lat = message.location.latitude
    lon = message.location.longitude
    bot.send_message(message.chat.id,
                     f"📍 Геопозиция получена!\nШирота: {lat}\nДолгота: {lon}",
                     reply_markup=main_menu())


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    process_text(message.chat.id, message.text)


if __name__ == "__main__":
    keep_alive()
    print("🚀 Ruslan Personal Helper запущен с распознаванием голоса!")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Ошибка polling: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)
