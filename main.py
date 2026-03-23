import os
import telebot
import requests
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

CHAT_ID = None  # will store user chat id

BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"

# ===== START COMMAND =====
@bot.message_handler(commands=["start"])
def start(message):
    global CHAT_ID
    CHAT_ID = message.chat.id

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔥 Hot Solana Picks")

    bot.send_message(
        message.chat.id,
        "Bot activated 🚀\nYou'll now receive live Solana alerts.",
        reply_markup=markup,
    )


# ===== BUTTON (manual trigger) =====
@bot.message_handler(func=lambda message: message.text == "🔥 Hot Solana Picks")
def manual_fetch(message):
    send_picks(message.chat.id)


# ===== FETCH + FILTER FUNCTION =====
def send_picks(chat_id):
    try:
        r = requests.get(BOOSTS_URL, timeout=10)
        r.raise_for_status()
        data = r.json()

        solana_tokens = [x for x in data if x.get("chainId") == "solana"]

        filtered = []
        for t in solana_tokens:
            liquidity = float(t.get("liquidityUsd", 0) or 0)

            # 🔥 FILTER (important)
            if liquidity < 50000:
                continue

            filtered.append(t)

        top = filtered[:5]

        if not top:
            bot.send_message(chat_id, "No strong Solana picks right now.")
            return

        msg = "🔥 *Filtered Solana Picks:*\n\n"

        for t in top:
            address = t.get("tokenAddress", "N/A")
            amount = t.get("amount", "N/A")
            liquidity = t.get("liquidityUsd", "N/A")
            url = t.get("url", "")

            msg += (
                f"🪙 `{address[:6]}...`\n"
                f"💧 Liquidity: ${liquidity}\n"
                f"🚀 Boost: {amount}\n"
                f"{url}\n\n"
            )

        bot.send_message(chat_id, msg, parse_mode="Markdown")

    except Exception as e:
        bot.send_message(chat_id, f"Error: {str(e)}")


# ===== AUTO LOOP (THIS IS THE MONEY PART) =====
def auto_loop():
    global CHAT_ID

    while True:
        if CHAT_ID:
            try:
                send_picks(CHAT_ID)
            except:
                pass

        time.sleep(300)  # every 5 minutes


# ===== RUN BOTH THREADS =====
import threading

threading.Thread(target=auto_loop).start()

bot.infinity_polling()
