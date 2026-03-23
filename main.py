import os
import telebot
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/solana"

@bot.message_handler(commands=['start'])
def start(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔥 Hot Solana Picks")
    bot.send_message(message.chat.id, "Choose an option:", reply_markup=markup)


@bot.message_handler(func=lambda message: message.text == "🔥 Hot Solana Picks")
def hot_picks(message):
    try:
        response = requests.get(DEX_URL)
        data = response.json()

        pairs = data.get("pairs", [])[:5]

        reply = "🔥 *Top Solana Picks:*\n\n"
        for p in pairs:
            name = p.get("baseToken", {}).get("name", "Unknown")
            price = p.get("priceUsd", "N/A")
            reply += f"{name} — ${price}\n"

        bot.send_message(message.chat.id, reply, parse_mode="Markdown")

    except Exception as e:
        bot.send_message(message.chat.id, "Error fetching data 😅")


bot.polling()
