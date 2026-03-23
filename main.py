import os
import telebot
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"


@bot.message_handler(commands=["start"])
def start(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔥 Hot Solana Picks")
    bot.send_message(message.chat.id, "Choose an option:", reply_markup=markup)


@bot.message_handler(func=lambda message: message.text == "🔥 Hot Solana Picks")
def hot_picks(message):
    try:
        response = requests.get(
            SEARCH_URL,
            params={"q": "SOL/USDC"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        pairs = data.get("pairs", [])
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"][:5]

        if not solana_pairs:
            bot.send_message(message.chat.id, "No Solana pairs found right now.")
            return

        reply = "🔥 Top Solana Picks:\n\n"
        for p in solana_pairs:
            name = p.get("baseToken", {}).get("name", "Unknown")
            symbol = p.get("baseToken", {}).get("symbol", "")
            price = p.get("priceUsd", "N/A")
            reply += f"{name} ({symbol}) — ${price}\n"

        bot.send_message(message.chat.id, reply)

    except requests.RequestException as e:
        bot.send_message(message.chat.id, f"HTTP error: {e}")
    except Exception as e:
        bot.send_message(message.chat.id, f"Bot error: {e}")


bot.infinity_polling()
