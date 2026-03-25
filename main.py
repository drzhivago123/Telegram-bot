import os
import time
import threading
from typing import Any, Dict, List, Optional, Set

import requests
import telebot

print("TRADING BOT V5 STARTING")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")

CHAIN_ID = "solana"
BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
BOOSTS_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chainId}/{tokenAddress}"

REQUEST_TIMEOUT = 15
SCAN_INTERVAL_SECONDS = 180
ALERT_COOLDOWN_SECONDS = 3 * 3600

# More selective filters
MIN_LIQUIDITY_USD = 25000
MAX_LIQUIDITY_USD = 300000
MIN_VOLUME_24H_USD = 75000
MIN_VOLUME_1H_USD = 12000
MIN_BUYS_5M = 6
MIN_BUY_PRESSURE_5M = 1.8
MIN_BUY_PRESSURE_1H = 1.15
MIN_PRICE_CHANGE_5M = 1.0
MAX_PRICE_CHANGE_5M = 18.0
MAX_PRICE_CHANGE_1H = 60.0
MAX_PAIR_AGE_HOURS = 18.0
MAX_MCAP_LIQ_RATIO = 18.0

# Trade plan
DEFAULT_POSITION_SIZE_USD = 100
STOP_LOSS_PCT = -10.0
TP1_PCT = 20.0
TP2_PCT = 45.0
TRAILING_WARNING_PCT = 12.0

subscribers: Set[int] = set()
positions: Dict[str, Dict[str, Any]] = {}
last_alert_time: Dict[str, float] = {}


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


def now_ts() -> float:
    return time.time()


def get_json(url: str) -> Any:
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def pair_age_hours(pair: Dict[str, Any]) -> float:
    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return 9999.0
    age_seconds = max((int(time.time() * 1000) - int(created_ms)) / 1000, 0)
    return age_seconds / 3600


def fetch_discovery_tokens() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    try:
        top = get_json(BOOSTS_TOP_URL)
        if isinstance(top, list):
            out.extend([x for x in top if x.get("chainId") == CHAIN_ID])
    except Exception:
        pass

    try:
        latest = get_json(BOOSTS_LATEST_URL)
        if isinstance(latest, list):
            out.extend([x for x in latest if x.get("chainId") == CHAIN_ID])
    except Exception:
        pass

    deduped = {}
    for item in out:
        addr = item.get("tokenAddress")
        if not addr:
            continue
        if addr not in deduped:
            deduped[addr] = item
        else:
            deduped[addr]["amount"] = max(
                safe_float(deduped[addr].get("amount")),
                safe_float(item.get("amount")),
            )
            deduped[addr]["totalAmount"] = max(
                safe_float(deduped[addr].get("totalAmount")),
                safe_float(item.get("totalAmount")),
            )

    return list(deduped.values())[:40]


def choose_best_pair(pairs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not pairs:
        return None

    def score(pair: Dict[str, Any]) -> float:
        liq = safe_float(pair.get("liquidity", {}).get("usd"))
        vol24 = safe_float(pair.get("volume", {}).get("h24"))
        buys5 = safe_int(pair.get("txns", {}).get("m5", {}).get("buys"))
        change5 = safe_float(pair.get("priceChange", {}).get("m5"))
        age = pair_age_hours(pair)
        freshness = max(0, 18 - age)
        return liq * 0.00008 + vol24 * 0.00002 + buys5 * 6 + change5 * 3 + freshness * 3

    return sorted(pairs, key=score, reverse=True)[0]


def enrich_token(token: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    token_address = token.get("tokenAddress")
    if not token_address:
        return None

    try:
        pairs = get_json(TOKEN_PAIRS_URL.format(chainId=CHAIN_ID, tokenAddress=token_address))
        if not isinstance(pairs, list) or not pairs:
            return None

        pair = choose_best_pair(pairs)
        if not pair:
            return None

        liq = safe_float(pair.get("liquidity", {}).get("usd"))
        vol24 = safe_float(pair.get("volume", {}).get("h24"))
        vol1 = safe_float(pair.get("volume", {}).get("h1"))
        buys5 = safe_int(pair.get("txns", {}).get("m5", {}).get("buys"))
        sells5 = safe_int(pair.get("txns", {}).get("m5", {}).get("sells"))
        buys1 = safe_int(pair.get("txns", {}).get("h1", {}).get("buys"))
        sells1 = safe_int(pair.get("txns", {}).get("h1", {}).get("sells"))
        change5 = safe_float(pair.get("priceChange", {}).get("m5"))
        change1 = safe_float(pair.get("priceChange", {}).get("h1"))
        change6 = safe_float(pair.get("priceChange", {}).get("h6"))
        price = safe_float(pair.get("priceUsd"))
        age = pair_age_hours(pair)
        fdv = safe_float(pair.get("fdv"))
        mcap = safe_float(pair.get("marketCap"))
        pair_address = pair.get("pairAddress", token_address)
        pair_url = pair.get("url", "")
        dex_id = pair.get("dexId", "N/A")

        base = pair.get("baseToken", {}) or {}
        name = base.get("name", token_address)
        symbol = base.get("symbol", "")

        bp5 = buys5 / max(sells5, 1)
        bp1 = buys1 / max(sells1, 1)
        mcap_liq_ratio = (mcap / liq) if mcap > 0 and liq > 0 else 0
        boost_amount = safe_float(token.get("amount"))
        total_boost = safe_float(token.get("totalAmount"))

        # Strict filters
        if price <= 0:
            return None
        if liq < MIN_LIQUIDITY_USD or liq > MAX_LIQUIDITY_USD:
            return None
        if vol24 < MIN_VOLUME_24H_USD:
            return None
        if vol1 < MIN_VOLUME_1H_USD:
            return None
        if buys5 < MIN_BUYS_5M:
            return None
        if bp5 < MIN_BUY_PRESSURE_5M:
            return None
        if bp1 < MIN_BUY_PRESSURE_1H:
            return None
        if change5 < MIN_PRICE_CHANGE_5M or change5 > MAX_PRICE_CHANGE_5M:
            return None
        if change1 > MAX_PRICE_CHANGE_1H:
            return None
        if age > MAX_PAIR_AGE_HOURS:
            return None
        if mcap_liq_ratio > MAX_MCAP_LIQ_RATIO:
            return None

        score = (
            min(liq / 1000, 300) * 0.24
            + min(vol24 / 1000, 600) * 0.18
            + min(vol1 / 1000, 120) * 0.20
            + buys5 * 4.5
            + bp5 * 12
            + bp1 * 6
            + change5 * 3.5
            + max(0, 18 - age) * 3
            + min(boost_amount, 200) * 0.30
            + min(total_boost, 500) * 0.10
        )

        return {
            "tokenAddress": token_address,
            "pairAddress": pair_address,
            "name": name,
            "symbol": symbol,
            "price": price,
            "liq": round(liq, 2),
            "vol24": round(vol24, 2),
            "vol1": round(vol1, 2),
            "buys5": buys5,
            "sells5": sells5,
            "buys1": buys1,
            "sells1": sells1,
            "bp5": round(bp5, 2),
            "bp1": round(bp1, 2),
            "chg5": round(change5, 2),
            "chg1": round(change1, 2),
            "chg6": round(change6, 2),
            "age": round(age, 2),
            "fdv": round(fdv, 2),
            "mcap": round(mcap, 2),
            "mcapLiqRatio": round(mcap_liq_ratio, 2),
            "boost": boost_amount,
            "totalBoost": total_boost,
            "score": round(score, 2),
            "url": pair_url,
            "dex": dex_id,
        }
    except Exception:
        return None


def fetch_ranked_tokens() -> List[Dict[str, Any]]:
    ranked = []
    for token in fetch_discovery_tokens():
        item = enrich_token(token)
        if item:
            ranked.append(item)
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def alert_allowed(pair_address: str) -> bool:
    last = last_alert_time.get(pair_address, 0)
    return now_ts() - last >= ALERT_COOLDOWN_SECONDS


def mark_alert(pair_address: str) -> None:
    last_alert_time[pair_address] = now_ts()


def classify_risk(token: Dict[str, Any]) -> str:
    if token["bp5"] >= 2.5 and token["liq"] >= 50000 and token["age"] <= 8:
        return "LOWER"
    if token["bp5"] >= 1.8 and token["liq"] >= 25000:
        return "MEDIUM"
    return "HIGH"


def trade_plan(token: Dict[str, Any]) -> Dict[str, Any]:
    price = token["price"]

    entry_now = price
    entry_pullback = price * 0.97
    stop_loss = price * (1 + STOP_LOSS_PCT / 100)
    tp1 = price * (1 + TP1_PCT / 100)
    tp2 = price * (1 + TP2_PCT / 100)

    action = "WATCH"
    if token["score"] >= 95 and token["bp5"] >= 1.8 and token["bp1"] >= 1.15:
        action = "BUY NOW"
    elif token["score"] >= 82:
        action = "WAIT FOR PULLBACK"

    return {
        "action": action,
        "entry_now": entry_now,
        "entry_pullback": entry_pullback,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "risk": classify_risk(token),
    }


def format_buy_message(token: Dict[str, Any]) -> str:
    plan = trade_plan(token)

    return (
        f"🚨 *TRADE SETUP*\n\n"
        f"*{token['name']} ({token['symbol']})*\n"
        f"*Score:* {token['score']}\n"
        f"*Risk:* {plan['risk']}\n\n"
        f"*BUY SIGNAL:* *{plan['action']}*\n\n"
        f"*BUY ZONE*\n"
        f"• *Buy now:* `${token['price']}`\n"
        f"• *Better entry:* `${round(plan['entry_pullback'], 12)}` to `${round(plan['entry_now'], 12)}`\n\n"
        f"*SELL ZONES*\n"
        f"• *TP1:* `${round(plan['tp1'], 12)}` `(+{TP1_PCT}%)`\n"
        f"• *TP2:* `${round(plan['tp2'], 12)}` `(+{TP2_PCT}%)`\n"
        f"• *Stop loss:* `${round(plan['stop_loss'], 12)}` `({STOP_LOSS_PCT}%)`\n\n"
        f"*WHY IT QUALIFIED*\n"
        f"• Liquidity: `${token['liq']:,.0f}`\n"
        f"• 24h / 1h Volume: `${token['vol24']:,.0f}` / `${token['vol1']:,.0f}`\n"
        f"• 5m Buy Pressure: `x{token['bp5']}`\n"
        f"• 1h Buy Pressure: `x{token['bp1']}`\n"
        f"• 5m / 1h Change: `{token['chg5']}% / {token['chg1']}%`\n"
        f"• Age: `{token['age']}h`\n"
        f"• MC/Liq Ratio: `{token['mcapLiqRatio']}`\n\n"
        f"*Chart:* {token['url']}"
    )


def open_position(token: Dict[str, Any]) -> None:
    if token["pairAddress"] in positions:
        return

    plan = trade_plan(token)
    positions[token["pairAddress"]] = {
        "name": token["name"],
        "symbol": token["symbol"],
        "entry": token["price"],
        "tp1_price": plan["tp1"],
        "tp2_price": plan["tp2"],
        "stop_price": plan["stop_loss"],
        "tp1_hit": False,
        "tp2_hit": False,
        "opened_at": now_ts(),
        "url": token["url"],
    }


def format_exit_message(token: Dict[str, Any], label: str, pnl_pct: float) -> str:
    return (
        f"📉 *EXIT UPDATE*\n\n"
        f"*{token['name']} ({token['symbol']})*\n"
        f"*Signal:* *{label}*\n"
        f"*Current Price:* `${token['price']}`\n"
        f"*PnL:* `{round(pnl_pct, 2)}%`\n"
        f"*Chart:* {token['url']}"
    )


def check_positions(tokens: List[Dict[str, Any]]) -> None:
    by_pair = {t["pairAddress"]: t for t in tokens}

    for pair, pos in list(positions.items()):
        token = by_pair.get(pair)
        if not token:
            continue

        entry = pos["entry"]
        current = token["price"]
        pnl_pct = ((current - entry) / entry) * 100

        message = None
        close_position = False

        if current <= pos["stop_price"]:
            message = format_exit_message(token, "SELL / EXIT (STOP LOSS)", pnl_pct)
            close_position = True

        elif not pos["tp1_hit"] and current >= pos["tp1_price"]:
            pos["tp1_hit"] = True
            message = format_exit_message(token, "SELL PARTIAL (TP1)", pnl_pct)

        elif not pos["tp2_hit"] and current >= pos["tp2_price"]:
            pos["tp2_hit"] = True
            message = format_exit_message(token, "SELL MORE (TP2)", pnl_pct)

        elif pos["tp1_hit"] and pnl_pct < TRAILING_WARNING_PCT:
            message = format_exit_message(token, "PROTECT PROFITS / CONSIDER EXIT", pnl_pct)

        elif token["buys5"] < token["sells5"] and token["chg5"] < 0:
            message = format_exit_message(token, "SELL / EXIT (MOMENTUM WEAKENING)", pnl_pct)
            close_position = True

        if message:
            for chat_id in list(subscribers):
                try:
                    bot.send_message(chat_id, message)
                except Exception:
                    pass

        if close_position:
            positions.pop(pair, None)


def scan_and_alert() -> None:
    tokens = fetch_ranked_tokens()
    if not tokens:
        return

    check_positions(tokens)

    # only send top 3 strongest fresh alerts
    sent = 0
    for token in tokens[:8]:
        plan = trade_plan(token)

        if plan["action"] == "WATCH":
            continue
        if not alert_allowed(token["pairAddress"]):
            continue

        message = format_buy_message(token)
        for chat_id in list(subscribers):
            try:
                bot.send_message(chat_id, message)
            except Exception:
                pass

        open_position(token)
        mark_alert(token["pairAddress"])
        sent += 1

        if sent >= 3:
            break


def show_now(chat_id: int) -> None:
    tokens = fetch_ranked_tokens()[:5]
    if not tokens:
        bot.send_message(chat_id, "No strong setups right now.")
        return

    for token in tokens:
        bot.send_message(chat_id, format_buy_message(token))


def show_status(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        (
            f"*BOT STATUS*\n\n"
            f"• Subscribers: `{len(subscribers)}`\n"
            f"• Open positions: `{len(positions)}`\n"
            f"• Scan interval: `{SCAN_INTERVAL_SECONDS}s`\n"
            f"• Alert cooldown: `{ALERT_COOLDOWN_SECONDS // 3600}h`\n"
        ),
    )


def show_positions(chat_id: int) -> None:
    if not positions:
        bot.send_message(chat_id, "No open paper positions.")
        return

    lines = ["📂 *OPEN POSITIONS*\n"]
    for _, pos in positions.items():
        lines.append(
            f"*{pos['name']} ({pos['symbol']})*\n"
            f"• Entry: `${pos['entry']}`\n"
            f"• TP1 hit: `{pos['tp1_hit']}`\n"
            f"• TP2 hit: `{pos['tp2_hit']}`\n"
            f"• Chart: {pos['url']}\n"
        )
    bot.send_message(chat_id, "\n".join(lines))


def loop() -> None:
    while True:
        if subscribers:
            try:
                scan_and_alert()
            except Exception as e:
                print("SCAN ERROR:", e)
        time.sleep(SCAN_INTERVAL_SECONDS)


@bot.message_handler(commands=["start"])
def start(message):
    subscribers.add(message.chat.id)
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🔥 Sniper Picks", "📊 Status")
    markup.row("📂 Paper Positions", "🛑 Stop Alerts")
    bot.send_message(
        message.chat.id,
        "*Selective trading bot active* 🚀\n\nYou will now get clearer buy and sell zones.",
        reply_markup=markup,
    )


@bot.message_handler(commands=["stop"])
def stop(message):
    subscribers.discard(message.chat.id)
    bot.send_message(message.chat.id, "Alerts stopped.")


@bot.message_handler(commands=["now"])
def now_cmd(message):
    show_now(message.chat.id)


@bot.message_handler(commands=["status"])
def status_cmd(message):
    show_status(message.chat.id)


@bot.message_handler(func=lambda m: m.text == "🔥 Sniper Picks")
def sniper_picks(message):
    show_now(message.chat.id)


@bot.message_handler(func=lambda m: m.text == "📊 Status")
def status_btn(message):
    show_status(message.chat.id)


@bot.message_handler(func=lambda m: m.text == "📂 Paper Positions")
def positions_btn(message):
    show_positions(message.chat.id)


@bot.message_handler(func=lambda m: m.text == "🛑 Stop Alerts")
def stop_btn(message):
    subscribers.discard(message.chat.id)
    bot.send_message(message.chat.id, "Alerts stopped.")


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(
        message.chat.id,
        "Use /start, /now, /status, /stop or the buttons.",
    )


threading.Thread(target=loop, daemon=True).start()
bot.infinity_polling(skip_pending=True)
