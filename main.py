import os
import time
import threading
from typing import Any, Dict, List, Optional, Set

import requests
import telebot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)

BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chainId}/{tokenAddress}"

CHAIN_ID = "solana"
SCAN_INTERVAL_SECONDS = 180
ALERT_COOLDOWN_SECONDS = 3 * 3600
REQUEST_TIMEOUT = 15

# Sniper filters
MIN_LIQUIDITY_USD = 12000
MAX_LIQUIDITY_USD = 250000
MIN_VOLUME_24H_USD = 30000
MIN_BUYS_5M = 4
MIN_PRICE_CHANGE_5M = 2
MAX_PRICE_CHANGE_1H = 120
MAX_PAIR_AGE_HOURS = 24
TOP_SCAN_LIMIT = 30

subscribers: Set[int] = set()
last_alerted_at: Dict[str, float] = {}
last_scan_summary: str = "No scan yet."


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def get_json(url: str) -> Any:
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def build_menu() -> telebot.types.ReplyKeyboardMarkup:
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🔥 Sniper Picks")
    markup.row("📊 Status")
    return markup


def shorten(text: str, n: int = 8) -> str:
    if not text:
        return "N/A"
    return text if len(text) <= n else f"{text[:n]}..."


def pair_age_hours(pair: Dict[str, Any]) -> float:
    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return 9999.0
    now_ms = int(time.time() * 1000)
    return max((now_ms - int(created_ms)) / 1000 / 3600, 0.0)


def select_best_pair(pairs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not pairs:
        return None

    def score_pair(p: Dict[str, Any]) -> float:
        liquidity = safe_float(p.get("liquidity", {}).get("usd"))
        volume_24h = safe_float(p.get("volume", {}).get("h24"))
        buys_5m = safe_int(p.get("txns", {}).get("m5", {}).get("buys"))
        price_change_5m = safe_float(p.get("priceChange", {}).get("m5"))
        age_h = pair_age_hours(p)

        freshness_bonus = max(0, 24 - age_h) * 2
        return (
            liquidity * 0.00008
            + volume_24h * 0.00003
            + buys_5m * 6
            + price_change_5m * 3
            + freshness_bonus
        )

    return sorted(pairs, key=score_pair, reverse=True)[0]


def enrich_token(token: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    token_address = token.get("tokenAddress")
    if not token_address:
        return None

    try:
        pairs = get_json(TOKEN_PAIRS_URL.format(chainId=CHAIN_ID, tokenAddress=token_address))
        if not isinstance(pairs, list) or not pairs:
            return None

        best_pair = select_best_pair(pairs)
        if not best_pair:
            return None

        liquidity_usd = safe_float(best_pair.get("liquidity", {}).get("usd"))
        volume_24h = safe_float(best_pair.get("volume", {}).get("h24"))
        buys_5m = safe_int(best_pair.get("txns", {}).get("m5", {}).get("buys"))
        sells_5m = safe_int(best_pair.get("txns", {}).get("m5", {}).get("sells"))
        buys_1h = safe_int(best_pair.get("txns", {}).get("h1", {}).get("buys"))
        sells_1h = safe_int(best_pair.get("txns", {}).get("h1", {}).get("sells"))
        price_change_5m = safe_float(best_pair.get("priceChange", {}).get("m5"))
        price_change_1h = safe_float(best_pair.get("priceChange", {}).get("h1"))
        price_usd = best_pair.get("priceUsd", "N/A")
        pair_url = best_pair.get("url", "")
        dex_id = best_pair.get("dexId", "N/A")
        pair_address = best_pair.get("pairAddress", token_address)
        fdv = safe_float(best_pair.get("fdv"))
        mcap = safe_float(best_pair.get("marketCap"))
        age_h = pair_age_hours(best_pair)

        base = best_pair.get("baseToken", {}) or {}
        token_name = base.get("name") or token_address
        token_symbol = base.get("symbol") or ""

        boost_amount = safe_float(token.get("amount"))
        total_boost = safe_float(token.get("totalAmount"))

        # Sniper filters
        if liquidity_usd < MIN_LIQUIDITY_USD or liquidity_usd > MAX_LIQUIDITY_USD:
            return None
        if volume_24h < MIN_VOLUME_24H_USD:
            return None
        if buys_5m < MIN_BUYS_5M:
            return None
        if price_change_5m < MIN_PRICE_CHANGE_5M:
            return None
        if price_change_1h > MAX_PRICE_CHANGE_1H:
            return None
        if age_h > MAX_PAIR_AGE_HOURS:
            return None

        buy_pressure = buys_5m / max(sells_5m, 1)
        h1_buy_pressure = buys_1h / max(sells_1h, 1)

        alpha_score = (
            min(liquidity_usd / 1000, 250) * 0.20
            + min(volume_24h / 1000, 500) * 0.25
            + buys_5m * 5.0
            + buy_pressure * 10.0
            + h1_buy_pressure * 5.0
            + price_change_5m * 4.0
            + max(0, 24 - age_h) * 3.0
            + min(boost_amount, 200) * 0.6
            + min(total_boost, 500) * 0.2
        )

        return {
            "tokenAddress": token_address,
            "pairAddress": pair_address,
            "name": token_name,
            "symbol": token_symbol,
            "priceUsd": price_usd,
            "liquidityUsd": round(liquidity_usd, 2),
            "volume24h": round(volume_24h, 2),
            "buys5m": buys_5m,
            "sells5m": sells_5m,
            "buys1h": buys_1h,
            "sells1h": sells_1h,
            "priceChange5m": round(price_change_5m, 2),
            "priceChange1h": round(price_change_1h, 2),
            "boostAmount": boost_amount,
            "totalBoost": total_boost,
            "dexId": dex_id,
            "url": pair_url,
            "fdv": round(fdv, 2),
            "marketCap": round(mcap, 2),
            "ageHours": round(age_h, 2),
            "buyPressure": round(buy_pressure, 2),
            "alphaScore": round(alpha_score, 2),
        }
    except Exception:
        return None


def fetch_ranked_candidates() -> List[Dict[str, Any]]:
    raw = get_json(BOOSTS_TOP_URL)
    if not isinstance(raw, list):
        return []

    solana_boosts = [x for x in raw if x.get("chainId") == CHAIN_ID][:TOP_SCAN_LIMIT]

    enriched: List[Dict[str, Any]] = []
    for token in solana_boosts:
        item = enrich_token(token)
        if item:
            enriched.append(item)

    enriched.sort(key=lambda x: x["alphaScore"], reverse=True)
    return enriched


def grade_pick(item: Dict[str, Any]) -> str:
    score = item["alphaScore"]
    if score >= 120:
        return "A+"
    if score >= 95:
        return "A"
    if score >= 75:
        return "B"
    return "C"


def format_pick(item: Dict[str, Any], index: Optional[int] = None) -> str:
    prefix = f"{index}. " if index is not None else ""
    grade = grade_pick(item)
    return (
        f"{prefix}{item['name']} ({item['symbol']}) [{grade}]\n"
        f"Price: ${item['priceUsd']}\n"
        f"Liquidity: ${item['liquidityUsd']:,.0f}\n"
        f"24h Vol: ${item['volume24h']:,.0f}\n"
        f"5m Buys/Sells: {item['buys5m']}/{item['sells5m']}\n"
        f"Buy Pressure: {item['buyPressure']}x\n"
        f"5m / 1h Change: {item['priceChange5m']}% / {item['priceChange1h']}%\n"
        f"Age: {item['ageHours']}h\n"
        f"Boost: {item['boostAmount']} | Total: {item['totalBoost']}\n"
        f"FDV / MC: ${item['fdv']:,.0f} / ${item['marketCap']:,.0f}\n"
        f"Alpha Score: {item['alphaScore']}\n"
        f"Token: {shorten(item['tokenAddress'])}\n"
        f"Chart: {item['url']}\n"
    )


def should_alert(item: Dict[str, Any]) -> bool:
    now = time.time()
    last = last_alerted_at.get(item["pairAddress"], 0)
    return now - last >= ALERT_COOLDOWN_SECONDS


def mark_alerted(item: Dict[str, Any]) -> None:
    last_alerted_at[item["pairAddress"]] = time.time()


def send_sniper_picks(chat_id: int) -> None:
    try:
        picks = fetch_ranked_candidates()[:5]
        if not picks:
            bot.send_message(chat_id, "No sniper-grade Solana setups right now.")
            return

        parts = ["🎯 Alpha Detection v2 — Sniper Picks\n"]
        for i, item in enumerate(picks, start=1):
            parts.append(format_pick(item, i))

        bot.send_message(chat_id, "\n".join(parts))
    except Exception as e:
        bot.send_message(chat_id, f"Error fetching picks: {e}")


def scan_and_alert() -> None:
    global last_scan_summary
    try:
        picks = fetch_ranked_candidates()
        if not picks:
            last_scan_summary = "No sniper-grade candidates on latest scan."
            return

        fresh = [p for p in picks[:10] if should_alert(p) and grade_pick(p) in {"A+", "A"}]
        if not fresh:
            last_scan_summary = f"Scan ok. {len(picks)} candidates ranked, no fresh A/A+ alerts."
            return

        text = ["🚨 Alpha Detection v2 Alert\n"]
        for item in fresh[:3]:
            text.append(format_pick(item))
            mark_alerted(item)

        payload = "\n".join(text)
        for chat_id in list(subscribers):
            try:
                bot.send_message(chat_id, payload)
            except Exception:
                pass

        last_scan_summary = f"Scan ok. {len(picks)} ranked, {len(fresh[:3])} alert(s) sent."
    except Exception as e:
        last_scan_summary = f"Scan error: {e}"


def background_loop() -> None:
    while True:
        if subscribers:
            scan_and_alert()
        time.sleep(SCAN_INTERVAL_SECONDS)


@bot.message_handler(commands=["start"])
def start(message):
    subscribers.add(message.chat.id)
    bot.send_message(
        message.chat.id,
        "Alpha Detection v2 activated 🎯\nYou'll now receive sniper-grade Solana alerts.",
        reply_markup=build_menu(),
    )


@bot.message_handler(commands=["now"])
def now_cmd(message):
    send_sniper_picks(message.chat.id)


@bot.message_handler(commands=["status"])
def status_cmd(message):
    bot.send_message(message.chat.id, f"Status:\n{last_scan_summary}")


@bot.message_handler(commands=["stop"])
def stop_cmd(message):
    subscribers.discard(message.chat.id)
    bot.send_message(message.chat.id, "Alerts stopped for this chat.")


@bot.message_handler(func=lambda m: m.text == "🔥 Sniper Picks")
def picks_button(message):
    send_sniper_picks(message.chat.id)


@bot.message_handler(func=lambda m: m.text == "📊 Status")
def status_button(message):
    bot.send_message(message.chat.id, f"Status:\n{last_scan_summary}")


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(
        message.chat.id,
        "Use /start, /now, /status, /stop or tap a button.",
        reply_markup=build_menu(),
    )


threading.Thread(target=background_loop, daemon=True).start()
bot.infinity_polling(skip_pending=True)
