import os
import telebot
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

DEX_URL = "https://api.dexscreener.com/latest/dex/pairs/solana"

@bot.message_handler(commands=['start'])
def start(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔥 Hot Solana Picks")
    bot.send_message(message.chat.id, "Choose an option:", reply_markup=markup)

@bot.message_handler(func=lambda message: True)
def handle(message):
    if message.text == "🔥 Hot Solana Picks":
        data = requests.get(DEX_URL).json()
        pairs = data.get("pairs", [])[:5]

        response = "🔥 Top Solana Pairs:\n\n"
        for p in pairs:
            name = p["baseToken"]["name"]
            price = p["priceUsd"]
            volume = p["volume"]["h24"]

            response += f"{name}\n💰 ${price}\n📊 Vol: {volume}\n\n"

        bot.send_message(message.chat.id, response)

bot.polling()
