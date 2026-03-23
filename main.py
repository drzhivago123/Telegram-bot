import os
import time
import threading
import requests
import telebot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # your personal chat id for auto alerts

bot = telebot.TeleBot(TOKEN)

BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
CHECK_INTERVAL_SECONDS = 120

# stores token addresses already alerted
seen_tokens = set()


def fetch_solana_boosts(limit=5):
    r = requests.get(BOOSTS_URL, timeout=15)
    r.raise_for_status()
    data = r.json()

    solana_tokens = [x for x in data if x.get("chainId") == "solana"]
    return solana_tokens[:limit]


def format_token_line(index, token):
    token_address = token.get("tokenAddress", "N/A")
    amount = token.get("amount", "N/A")
    total_amount = token.get("totalAmount", "N/A")
    url = token.get("url", "")

    return (
        f"{index}. {token_address}\n"
        f"Boost: {amount}\n"
        f"Total Boost: {total_amount}\n"
        f"{url}\n"
    )


@bot.message_handler(commands=["start"])
def start(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔥 Hot Solana Picks")
    markup.add("📡 Start Alerts", "🛑 Stop Alerts")
    bot.send_message(
        message.chat.id,
        "Hustlemilk is live.\nChoose an option:",
        reply_markup=markup,
    )


@bot.message_handler(func=lambda message: message.text == "🔥 Hot Solana Picks")
def hot_picks(message):
    try:
        tokens = fetch_solana_boosts(limit=5)

        if not tokens:
            bot.send_message(message.chat.id, "No Solana picks found right now.")
            return

        lines = ["🔥 Hot Solana Picks:\n"]
        for i, token in enumerate(tokens, start=1):
            lines.append(format_token_line(i, token))

        bot.send_message(message.chat.id, "\n".join(lines))

    except requests.RequestException as e:
        bot.send_message(message.chat.id, f"HTTP error: {e}")
    except Exception as e:
        bot.send_message(message.chat.id, f"Bot error: {e}")


alerts_enabled = False


@bot.message_handler(func=lambda message: message.text == "📡 Start Alerts")
def start_alerts(message):
    global alerts_enabled
    alerts_enabled = True
    bot.send_message(message.chat.id, "Auto alerts started.")


@bot.message_handler(func=lambda message: message.text == "🛑 Stop Alerts")
def stop_alerts(message):
    global alerts_enabled
    alerts_enabled = False
    bot.send_message(message.chat.id, "Auto alerts stopped.")


def alert_loop():
    global alerts_enabled

    while True:
        try:
            if alerts_enabled and CHAT_ID:
                tokens = fetch_solana_boosts(limit=10)

                for token in tokens:
                    token_address = token.get("tokenAddress")
                    if not token_address:
                        continue

                    if token_address not in seen_tokens:
                        seen_tokens.add(token_address)

                        msg = (
                            "🚨 New Solana Boost Alert\n\n"
                            + format_token_line(1, token)
                        )
                        bot.send_message(CHAT_ID, msg)

            time.sleep(CHECK_INTERVAL_SECONDS)

        except Exception as e:
            # keep loop alive even if one request fails
            print(f"Alert loop error: {e}")
            time.sleep(CHECK_INTERVAL_SECONDS)


def main():
    loop_thread = threading.Thread(target=alert_loop, daemon=True)
    loop_thread.start()

    print("Bot running...")
    bot.infinity_polling()


if __name__ == "__main__":
    main()
