import os
import json
import time
import threading
from typing import Any, Dict, List, Optional, Set

import requests
import telebot

print("TRADING BOT V4 STARTING")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)

CHAIN_ID = "solana"

BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chainId}/{tokenAddress}"

REQUEST_TIMEOUT = 15
SCAN_INTERVAL_SECONDS = 180

# Filters
MIN_LIQUIDITY_USD = 15000
MIN_VOLUME_24H_USD = 30000
MIN_BUYS_5M = 4

# Trade rules
STOP_LOSS = -12
TP1 = 25
TP2 = 60

PAPER_SIZE = 100

subscribers: Set[int] = set()
positions = {}

# =========================
# HELPERS
# =========================

def get_json(url):
    return requests.get(url, timeout=REQUEST_TIMEOUT).json()

def safe(x, d=0):
    try:
        return float(x)
    except:
        return d

# =========================
# CORE LOGIC
# =========================

def fetch_tokens():
    raw = get_json(BOOSTS_TOP_URL)
    return [x for x in raw if x.get("chainId") == CHAIN_ID][:20]

def enrich(token):
    try:
        addr = token["tokenAddress"]
        pairs = get_json(TOKEN_PAIRS_URL.format(chainId=CHAIN_ID, tokenAddress=addr))
        if not pairs:
            return None

        p = pairs[0]

        liq = safe(p["liquidity"]["usd"])
        vol = safe(p["volume"]["h24"])
        buys = p["txns"]["m5"]["buys"]
        sells = p["txns"]["m5"]["sells"]
        price = safe(p["priceUsd"])

        if liq < MIN_LIQUIDITY_USD or vol < MIN_VOLUME_24H_USD or buys < MIN_BUYS_5M:
            return None

        bp = buys / max(sells, 1)

        return {
            "name": p["baseToken"]["name"],
            "symbol": p["baseToken"]["symbol"],
            "price": price,
            "liq": liq,
            "vol": vol,
            "buys": buys,
            "sells": sells,
            "bp": round(bp, 2),
            "url": p["url"],
            "pair": p["pairAddress"]
        }
    except:
        return None

# =========================
# TRADE PLAN
# =========================

def trade_plan(t):
    price = t["price"]

    entry_low = price * 0.97
    entry_high = price

    tp1 = price * (1 + TP1 / 100)
    tp2 = price * (1 + TP2 / 100)
    sl = price * (1 + STOP_LOSS / 100)

    risk = "LOW"
    if t["bp"] < 1.5:
        risk = "MEDIUM"
    if t["bp"] < 1.2:
        risk = "HIGH"

    return entry_low, entry_high, tp1, tp2, sl, risk

# =========================
# PAPER TRADING
# =========================

def open_trade(t):
    if t["pair"] in positions:
        return

    positions[t["pair"]] = {
        "entry": t["price"],
        "tp1": False,
        "tp2": False,
        "name": t["name"],
        "symbol": t["symbol"]
    }

def check_positions(tokens):
    for t in tokens:
        if t["pair"] not in positions:
            continue

        pos = positions[t["pair"]]
        price = t["price"]

        pnl = (price - pos["entry"]) / pos["entry"] * 100

        msg = None

        if pnl <= STOP_LOSS:
            msg = f"❌ STOP LOSS {t['symbol']} {round(pnl,2)}%"
            del positions[t["pair"]]

        elif not pos["tp1"] and pnl >= TP1:
            pos["tp1"] = True
            msg = f"💰 TP1 HIT {t['symbol']} +{round(pnl,2)}%"

        elif not pos["tp2"] and pnl >= TP2:
            pos["tp2"] = True
            msg = f"🚀 TP2 HIT {t['symbol']} +{round(pnl,2)}%"

        if msg:
            for s in subscribers:
                bot.send_message(s, msg)

# =========================
# MAIN SCAN
# =========================

def scan():
    tokens = []
    for x in fetch_tokens():
        t = enrich(x)
        if t:
            tokens.append(t)

    check_positions(tokens)

    for t in tokens[:5]:
        entry_low, entry_high, tp1, tp2, sl, risk = trade_plan(t)

        msg = f"""
🚨 BUY SETUP

{t['name']} ({t['symbol']})

Price: ${t['price']}

ENTRY:
{round(entry_low,8)} - {round(entry_high,8)}

POSITION:
${PAPER_SIZE}

TAKE PROFIT:
TP1: {round(tp1,8)}
TP2: {round(tp2,8)}

STOP LOSS:
{round(sl,8)}

BUY PRESSURE: {t['bp']}x
RISK: {risk}

Chart:
{t['url']}
"""

        open_trade(t)

        for s in subscribers:
            bot.send_message(s, msg)

# =========================
# LOOP
# =========================

def loop():
    while True:
        if subscribers:
            try:
                scan()
            except Exception as e:
                print("ERROR:", e)
        time.sleep(SCAN_INTERVAL_SECONDS)

# =========================
# TELEGRAM
# =========================

@bot.message_handler(commands=["start"])
def start(m):
    subscribers.add(m.chat.id)
    bot.send_message(m.chat.id, "Trading bot active 🚀")

@bot.message_handler(commands=["stop"])
def stop(m):
    subscribers.discard(m.chat.id)
    bot.send_message(m.chat.id, "Stopped")

@bot.message_handler(commands=["now"])
def now(m):
    scan()

# =========================
# RUN
# =========================

threading.Thread(target=loop, daemon=True).start()
bot.infinity_polling(skip_pending=True)
