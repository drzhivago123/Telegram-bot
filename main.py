import os
import json
import time
import threading
from typing import Any, Dict, List, Optional, Set

import requests
import telebot

print("OPTIMIZATION BOT V3 STARTING")

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)

CHAIN_ID = "solana"

BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
BOOSTS_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chainId}/{tokenAddress}"

REQUEST_TIMEOUT = 15
SCAN_INTERVAL_SECONDS = 180
ALERT_COOLDOWN_SECONDS = 3 * 3600

# Discovery / validation filters
MIN_LIQUIDITY_USD = 12000
MAX_LIQUIDITY_USD = 350000
MIN_VOLUME_24H_USD = 25000
MIN_BUYS_5M = 4
MIN_BUY_PRESSURE_5M = 1.25
MIN_BUY_PRESSURE_1H = 1.05
MIN_PRICE_CHANGE_5M = 0.5
MAX_PRICE_CHANGE_1H = 120
MAX_PAIR_AGE_HOURS = 24
TOP_SCAN_LIMIT = 40

# Confirmation logic
MIN_CONFIRMATION_STREAK = 2
BUY_SCORE_THRESHOLD = 92

# Paper trade rules
PAPER_POSITION_SIZE_USD = 100
STOP_LOSS_PCT = -12.0
TAKE_PROFIT_1_PCT = 25.0
TAKE_PROFIT_2_PCT = 60.0
LIQUIDITY_DROP_EXIT_PCT = 25.0

# Files
SUBSCRIBERS_FILE = "subscribers.json"
POSITIONS_FILE = "paper_positions.json"
SIGNALS_FILE = "signal_log.json"
ALERTS_FILE = "last_alerts.json"
CONFIRMATIONS_FILE = "confirmations.json"
PERFORMANCE_FILE = "performance.json"

# In-memory state
subscribers: Set[int] = set()
paper_positions: Dict[str, Dict[str, Any]] = {}
signal_log: List[Dict[str, Any]] = []
last_alerted_at: Dict[str, float] = {}
confirmation_state: Dict[str, Dict[str, Any]] = {}
performance: Dict[str, Any] = {}
last_scan_summary: str = "No scan yet."

state_lock = threading.Lock()


# =========================
# HELPERS
# =========================

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


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def get_json(url: str) -> Any:
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_state() -> None:
    global subscribers, paper_positions, signal_log, last_alerted_at, confirmation_state, performance
    subscribers = set(load_json(SUBSCRIBERS_FILE, []))
    paper_positions = load_json(POSITIONS_FILE, {})
    signal_log = load_json(SIGNALS_FILE, [])
    last_alerted_at = load_json(ALERTS_FILE, {})
    confirmation_state = load_json(CONFIRMATIONS_FILE, {})
    performance = load_json(PERFORMANCE_FILE, {
        "buy_setups": 0,
        "watch_alerts": 0,
        "positions_opened": 0,
        "positions_closed": 0,
        "tp1_hits": 0,
        "tp2_hits": 0,
        "stop_losses": 0,
        "momentum_exits": 0,
        "liquidity_exits": 0,
        "wins": 0,
        "losses": 0,
        "total_closed_pnl_usd": 0.0,
        "closed_positions": []
    })


def persist_state() -> None:
    save_json(SUBSCRIBERS_FILE, list(subscribers))
    save_json(POSITIONS_FILE, paper_positions)
    save_json(SIGNALS_FILE, signal_log[-300:])
    save_json(ALERTS_FILE, last_alerted_at)
    save_json(CONFIRMATIONS_FILE, confirmation_state)
    save_json(PERFORMANCE_FILE, performance)


def shorten(text: str, n: int = 8) -> str:
    if not text:
        return "N/A"
    return text if len(text) <= n else f"{text[:n]}..."


def build_menu() -> telebot.types.ReplyKeyboardMarkup:
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🔥 Sniper Picks", "📊 Status")
    markup.row("📂 Paper Positions", "📈 Performance")
    markup.row("🧾 Recent Signals")
    return markup


def pair_age_hours(pair: Dict[str, Any]) -> float:
    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return 9999.0
    age_seconds = max((int(time.time() * 1000) - int(created_ms)) / 1000, 0)
    return age_seconds / 3600


# =========================
# LAYER 1: DISCOVERY
# =========================

def fetch_discovery_candidates() -> List[Dict[str, Any]]:
    candidates = []

    try:
        top = get_json(BOOSTS_TOP_URL)
        if isinstance(top, list):
            candidates.extend([x for x in top if x.get("chainId") == CHAIN_ID])
    except Exception:
        pass

    try:
        latest = get_json(BOOSTS_LATEST_URL)
        if isinstance(latest, list):
            candidates.extend([x for x in latest if x.get("chainId") == CHAIN_ID])
    except Exception:
        pass

    deduped = {}
    for c in candidates:
        token_address = c.get("tokenAddress")
        if token_address:
            prev = deduped.get(token_address)
            if not prev:
                deduped[token_address] = c
            else:
                prev["amount"] = max(safe_float(prev.get("amount")), safe_float(c.get("amount")))
                prev["totalAmount"] = max(safe_float(prev.get("totalAmount")), safe_float(c.get("totalAmount")))

    return list(deduped.values())[:TOP_SCAN_LIMIT]


# =========================
# LAYER 2: VALIDATION
# =========================

def score_pair_for_selection(pair: Dict[str, Any]) -> float:
    liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
    volume_24h = safe_float(pair.get("volume", {}).get("h24"))
    buys_5m = safe_int(pair.get("txns", {}).get("m5", {}).get("buys"))
    price_change_5m = safe_float(pair.get("priceChange", {}).get("m5"))
    age_h = pair_age_hours(pair)
    freshness_bonus = max(0, 24 - age_h) * 2

    return (
        liquidity * 0.00008
        + volume_24h * 0.00003
        + buys_5m * 6
        + price_change_5m * 3
        + freshness_bonus
    )


def select_best_pair(pairs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not pairs:
        return None
    return sorted(pairs, key=score_pair_for_selection, reverse=True)[0]


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
        volume_1h = safe_float(best_pair.get("volume", {}).get("h1"))
        buys_5m = safe_int(best_pair.get("txns", {}).get("m5", {}).get("buys"))
        sells_5m = safe_int(best_pair.get("txns", {}).get("m5", {}).get("sells"))
        buys_1h = safe_int(best_pair.get("txns", {}).get("h1", {}).get("buys"))
        sells_1h = safe_int(best_pair.get("txns", {}).get("h1", {}).get("sells"))
        price_change_5m = safe_float(best_pair.get("priceChange", {}).get("m5"))
        price_change_1h = safe_float(best_pair.get("priceChange", {}).get("h1"))
        price_change_6h = safe_float(best_pair.get("priceChange", {}).get("h6"))
        price_usd = safe_float(best_pair.get("priceUsd"))
        pair_url = best_pair.get("url", "")
        dex_id = best_pair.get("dexId", "N/A")
        pair_address = best_pair.get("pairAddress", token_address)
        fdv = safe_float(best_pair.get("fdv"))
        market_cap = safe_float(best_pair.get("marketCap"))
        age_h = pair_age_hours(best_pair)

        base = best_pair.get("baseToken", {}) or {}
        token_name = base.get("name") or token_address
        token_symbol = base.get("symbol") or ""

        boost_amount = safe_float(token.get("amount"))
        total_boost = safe_float(token.get("totalAmount"))

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
        if price_usd <= 0:
            return None

        buy_pressure_5m = buys_5m / max(sells_5m, 1)
        buy_pressure_1h = buys_1h / max(sells_1h, 1)

        if buy_pressure_5m < MIN_BUY_PRESSURE_5M:
            return None
        if buy_pressure_1h < MIN_BUY_PRESSURE_1H:
            return None

        mcap_liq_ratio = market_cap / max(liquidity_usd, 1) if market_cap > 0 else 0

        overextension_penalty = 0
        if price_change_1h > 60:
            overextension_penalty += (price_change_1h - 60) * 1.2
        if price_change_5m > 25:
            overextension_penalty += (price_change_5m - 25) * 2.0

        validation_score = (
            min(liquidity_usd / 1000, 300) * 0.20
            + min(volume_24h / 1000, 500) * 0.22
            + min(volume_1h / 1000, 150) * 0.18
            + buys_5m * 5.0
            + buy_pressure_5m * 10.0
            + buy_pressure_1h * 5.0
            + price_change_5m * 3.5
            + max(min(price_change_1h, 40), 0) * 1.2
            + max(0, 24 - age_h) * 3.0
            + min(boost_amount, 200) * 0.5
            + min(total_boost, 500) * 0.15
            - min(max(mcap_liq_ratio - 10, 0), 50) * 1.5
            - overextension_penalty
        )

        return {
            "tokenAddress": token_address,
            "pairAddress": pair_address,
            "name": token_name,
            "symbol": token_symbol,
            "priceUsd": round(price_usd, 12),
            "liquidityUsd": round(liquidity_usd, 2),
            "volume24h": round(volume_24h, 2),
            "volume1h": round(volume_1h, 2),
            "buys5m": buys_5m,
            "sells5m": sells_5m,
            "buys1h": buys_1h,
            "sells1h": sells_1h,
            "buyPressure5m": round(buy_pressure_5m, 2),
            "buyPressure1h": round(buy_pressure_1h, 2),
            "priceChange5m": round(price_change_5m, 2),
            "priceChange1h": round(price_change_1h, 2),
            "priceChange6h": round(price_change_6h, 2),
            "boostAmount": boost_amount,
            "totalBoost": total_boost,
            "dexId": dex_id,
            "url": pair_url,
            "fdv": round(fdv, 2),
            "marketCap": round(market_cap, 2),
            "ageHours": round(age_h, 2),
            "mcapLiqRatio": round(mcap_liq_ratio, 2),
            "validationScore": round(validation_score, 2),
        }

    except Exception:
        return None


def fetch_ranked_candidates() -> List[Dict[str, Any]]:
    discovery = fetch_discovery_candidates()
    ranked: List[Dict[str, Any]] = []

    for token in discovery:
        item = enrich_token(token)
        if item:
            ranked.append(item)

    ranked.sort(key=lambda x: x["validationScore"], reverse=True)
    return ranked


# =========================
# LAYER 3: EXECUTION GUIDANCE
# =========================

def grade_pick(item: Dict[str, Any]) -> str:
    score = item["validationScore"]
    if score >= 120:
        return "A+"
    if score >= 95:
        return "A"
    if score >= 75:
        return "B"
    return "C"


def update_confirmation(item: Dict[str, Any]) -> int:
    key = item["pairAddress"]
    state = confirmation_state.get(key, {"streak": 0, "lastSeen": 0, "lastScore": 0})
    state["streak"] = state.get("streak", 0) + 1
    state["lastSeen"] = now_ts()
    state["lastScore"] = item["validationScore"]
    confirmation_state[key] = state
    return state["streak"]


def decay_confirmations(current_pairs: Set[str]) -> None:
    stale = []
    for key, state in confirmation_state.items():
        if key not in current_pairs:
            if now_ts() - safe_float(state.get("lastSeen", 0)) > SCAN_INTERVAL_SECONDS * 2:
                stale.append(key)
    for key in stale:
        confirmation_state.pop(key, None)


def suggest_action(item: Dict[str, Any], streak: int) -> str:
    grade = grade_pick(item)
    age_h = item["ageHours"]
    bp5 = item["buyPressure5m"]
    bp1 = item["buyPressure1h"]
    chg5 = item["priceChange5m"]
    chg1 = item["priceChange1h"]
    liq = item["liquidityUsd"]
    ratio = item["mcapLiqRatio"]
    score = item["validationScore"]

    if (
        grade in {"A+", "A"}
        and streak >= MIN_CONFIRMATION_STREAK
        and age_h <= 12
        and bp5 >= 1.8
        and bp1 >= 1.2
        and 1 <= chg5 <= 18
        and chg1 <= 55
        and liq >= 20000
        and ratio <= 18
        and score >= BUY_SCORE_THRESHOLD
    ):
        return "Buy Setup"

    if grade in {"A+", "A", "B"} and age_h <= 24 and bp5 >= 1.2:
        return "Watch"

    return "Avoid"


def format_pick(item: Dict[str, Any], action: str, streak: int, index: Optional[int] = None) -> str:
    prefix = f"{index}. " if index is not None else ""
    grade = grade_pick(item)

    return (
        f"{prefix}{item['name']} ({item['symbol']}) [{grade}]\n"
        f"Action: {action}\n"
        f"Confirmation: {streak} scan(s)\n"
        f"Price: ${item['priceUsd']}\n"
        f"Liquidity: ${item['liquidityUsd']:,.0f}\n"
        f"24h / 1h Vol: ${item['volume24h']:,.0f} / ${item['volume1h']:,.0f}\n"
        f"5m Buys/Sells: {item['buys5m']}/{item['sells5m']}\n"
        f"5m / 1h Buy Pressure: {item['buyPressure5m']}x / {item['buyPressure1h']}x\n"
        f"5m / 1h / 6h Change: {item['priceChange5m']}% / {item['priceChange1h']}% / {item['priceChange6h']}%\n"
        f"Age: {item['ageHours']}h\n"
        f"Boost / Total: {item['boostAmount']} / {item['totalBoost']}\n"
        f"MC/Liq Ratio: {item['mcapLiqRatio']}\n"
        f"Score: {item['validationScore']}\n"
        f"Token: {shorten(item['tokenAddress'])}\n"
        f"Chart: {item['url']}\n"
    )


# =========================
# PAPER TRADING ENGINE
# =========================

def log_signal(item: Dict[str, Any], action: str, streak: int) -> None:
    global signal_log
    signal_log.append({
        "time": now_str(),
        "tokenAddress": item["tokenAddress"],
        "pairAddress": item["pairAddress"],
        "name": item["name"],
        "symbol": item["symbol"],
        "priceUsd": item["priceUsd"],
        "liquidityUsd": item["liquidityUsd"],
        "volume24h": item["volume24h"],
        "buyPressure5m": item["buyPressure5m"],
        "priceChange5m": item["priceChange5m"],
        "score": item["validationScore"],
        "grade": grade_pick(item),
        "action": action,
        "confirmationStreak": streak,
        "url": item["url"],
    })
    signal_log = signal_log[-300:]


def open_paper_position(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    key = item["pairAddress"]
    if key in paper_positions and paper_positions[key].get("status") == "OPEN":
        return None

    units = PAPER_POSITION_SIZE_USD / max(item["priceUsd"], 1e-12)

    position = {
        "tokenAddress": item["tokenAddress"],
        "pairAddress": item["pairAddress"],
        "name": item["name"],
        "symbol": item["symbol"],
        "entryTime": now_str(),
        "entryTimestamp": now_ts(),
        "entryPrice": item["priceUsd"],
        "entryLiquidityUsd": item["liquidityUsd"],
        "currentPrice": item["priceUsd"],
        "currentLiquidityUsd": item["liquidityUsd"],
        "sizeUsd": PAPER_POSITION_SIZE_USD,
        "units": units,
        "status": "OPEN",
        "tp1Hit": False,
        "tp2Hit": False,
        "maxPrice": item["priceUsd"],
        "notes": [],
        "url": item["url"],
    }

    paper_positions[key] = position
    performance["positions_opened"] += 1
    return position


def update_position_metrics(position: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    current_price = item["priceUsd"]
    entry_price = position["entryPrice"]

    pnl_pct = ((current_price - entry_price) / entry_price) * 100
    pnl_usd = position["sizeUsd"] * (pnl_pct / 100)

    position["currentPrice"] = current_price
    position["currentLiquidityUsd"] = item["liquidityUsd"]
    position["maxPrice"] = max(position.get("maxPrice", current_price), current_price)
    position["pnlPct"] = round(pnl_pct, 2)
    position["pnlUsd"] = round(pnl_usd, 2)

    return position


def evaluate_sell_signal(position: Dict[str, Any], item: Dict[str, Any]) -> Optional[str]:
    position = update_position_metrics(position, item)

    pnl_pct = position["pnlPct"]
    current_liq = item["liquidityUsd"]
    entry_liq = position["entryLiquidityUsd"]
    liq_drop_pct = ((entry_liq - current_liq) / max(entry_liq, 1)) * 100

    if pnl_pct <= STOP_LOSS_PCT:
        return "Sell / Exit (Stop Loss)"

    if not position["tp1Hit"] and pnl_pct >= TAKE_PROFIT_1_PCT:
        position["tp1Hit"] = True
        performance["tp1_hits"] += 1
        position["notes"].append(f"{now_str()} TP1 triggered")
        return "Sell Partial (TP1)"

    if not position["tp2Hit"] and pnl_pct >= TAKE_PROFIT_2_PCT:
        position["tp2Hit"] = True
        performance["tp2_hits"] += 1
        position["notes"].append(f"{now_str()} TP2 triggered")
        return "Sell More (TP2)"

    if liq_drop_pct >= LIQUIDITY_DROP_EXIT_PCT:
        performance["liquidity_exits"] += 1
        return "Sell / Exit (Liquidity Drop)"

    if item["buys5m"] < item["sells5m"] and item["priceChange5m"] < 0:
        performance["momentum_exits"] += 1
        return "Sell / Exit (Momentum Weakening)"

    return None


def close_position(pair_address: str, reason: str) -> None:
    if pair_address not in paper_positions:
        return

    pos = paper_positions[pair_address]
    if pos.get("status") != "OPEN":
        return

    pos["status"] = "CLOSED"
    pos["closeReason"] = reason
    pos["closeTime"] = now_str()

    performance["positions_closed"] += 1
    pnl_usd = safe_float(pos.get("pnlUsd", 0))
    performance["total_closed_pnl_usd"] = round(
        safe_float(performance.get("total_closed_pnl_usd", 0)) + pnl_usd, 2
    )

    if pnl_usd >= 0:
        performance["wins"] += 1
    else:
        performance["losses"] += 1

    if "Stop Loss" in reason:
        performance["stop_losses"] += 1

    performance["closed_positions"].append({
        "name": pos["name"],
        "symbol": pos["symbol"],
        "entryTime": pos["entryTime"],
        "closeTime": pos["closeTime"],
        "entryPrice": pos["entryPrice"],
        "exitPrice": pos.get("currentPrice", pos["entryPrice"]),
        "pnlPct": pos.get("pnlPct", 0),
        "pnlUsd": pos.get("pnlUsd", 0),
        "reason": reason,
    })
    performance["closed_positions"] = performance["closed_positions"][-200:]


# =========================
# ALERT CONTROL
# =========================

def should_alert(item: Dict[str, Any]) -> bool:
    last = safe_float(last_alerted_at.get(item["pairAddress"], 0))
    return now_ts() - last >= ALERT_COOLDOWN_SECONDS


def mark_alerted(item: Dict[str, Any]) -> None:
    last_alerted_at[item["pairAddress"]] = now_ts()


# =========================
# BOT VIEWS
# =========================

def send_sniper_picks(chat_id: int) -> None:
    try:
        ranked = fetch_ranked_candidates()[:5]
        if not ranked:
            bot.send_message(chat_id, "No sniper-grade Solana setups right now.")
            return

        current_pairs = {x["pairAddress"] for x in ranked}
        decay_confirmations(current_pairs)

        lines = ["🎯 Optimized Sniper Picks\n"]
        for i, item in enumerate(ranked, start=1):
            streak = update_confirmation(item)
            action = suggest_action(item, streak)
            lines.append(format_pick(item, action, streak, i))

        bot.send_message(chat_id, "\n".join(lines))
        with state_lock:
            persist_state()

    except Exception as e:
        bot.send_message(chat_id, f"Error fetching picks: {e}")


def send_recent_signals(chat_id: int) -> None:
    if not signal_log:
        bot.send_message(chat_id, "No signals logged yet.")
        return

    recent = signal_log[-10:]
    lines = ["🧾 Recent Signals\n"]
    for s in reversed(recent):
        lines.append(
            f"{s['time']} | {s['name']} ({s['symbol']}) | "
            f"{s['action']} | Score {s['score']} | Confirm {s['confirmationStreak']}"
        )

    bot.send_message(chat_id, "\n".join(lines))


def send_positions(chat_id: int) -> None:
    open_positions = [p for p in paper_positions.values() if p.get("status") == "OPEN"]

    if not open_positions:
        bot.send_message(chat_id, "No open paper positions.")
        return

    lines = ["📂 Open Paper Positions\n"]
    for p in open_positions:
        lines.append(
            f"{p['name']} ({p['symbol']})\n"
            f"Entry: ${p['entryPrice']}\n"
            f"Current: ${p.get('currentPrice', p['entryPrice'])}\n"
            f"PnL: {p.get('pnlPct', 0)}% | ${p.get('pnlUsd', 0)}\n"
            f"TP1: {p['tp1Hit']} | TP2: {p['tp2Hit']}\n"
            f"{p['url']}\n"
        )

    bot.send_message(chat_id, "\n".join(lines))


def send_performance(chat_id: int) -> None:
    closed = safe_int(performance.get("positions_closed", 0))
    wins = safe_int(performance.get("wins", 0))
    losses = safe_int(performance.get("losses", 0))
    total_pnl = safe_float(performance.get("total_closed_pnl_usd", 0))
    win_rate = round((wins / closed) * 100, 2) if closed > 0 else 0.0
    avg_pnl = round(total_pnl / closed, 2) if closed > 0 else 0.0

    msg = (
        "📈 Performance Summary\n\n"
        f"Buy Setups: {performance.get('buy_setups', 0)}\n"
        f"Watch Alerts: {performance.get('watch_alerts', 0)}\n"
        f"Positions Opened: {performance.get('positions_opened', 0)}\n"
        f"Positions Closed: {closed}\n"
        f"TP1 Hits: {performance.get('tp1_hits', 0)}\n"
        f"TP2 Hits: {performance.get('tp2_hits', 0)}\n"
        f"Stop Losses: {performance.get('stop_losses', 0)}\n"
        f"Momentum Exits: {performance.get('momentum_exits', 0)}\n"
        f"Liquidity Exits: {performance.get('liquidity_exits', 0)}\n"
        f"Wins / Losses: {wins} / {losses}\n"
        f"Win Rate: {win_rate}%\n"
        f"Total Closed PnL: ${total_pnl}\n"
        f"Average Closed PnL: ${avg_pnl}\n"
    )
    bot.send_message(chat_id, msg)


# =========================
# SCAN LOOP
# =========================

def process_scan() -> None:
    global last_scan_summary

    try:
        ranked = fetch_ranked_candidates()
        if not ranked:
            last_scan_summary = "No valid candidates on latest scan."
            return

        current_pairs = {x["pairAddress"] for x in ranked}
        decay_confirmations(current_pairs)

        sent_count = 0
        paper_updates = 0

        ranked_by_pair = {x["pairAddress"]: x for x in ranked}

        # Update open positions first
        for pair_address, position in list(paper_positions.items()):
            if position.get("status") != "OPEN":
                continue

            item = ranked_by_pair.get(pair_address)
            if not item:
                continue

            sell_signal = evaluate_sell_signal(position, item)

            if sell_signal:
                msg = (
                    f"📉 Paper Position Update\n\n"
                    f"{item['name']} ({item['symbol']})\n"
                    f"Signal: {sell_signal}\n"
                    f"Entry: ${position['entryPrice']}\n"
                    f"Current: ${position['currentPrice']}\n"
                    f"PnL: {position['pnlPct']}% | ${position['pnlUsd']}\n"
                    f"{item['url']}"
                )

                for chat_id in list(subscribers):
                    try:
                        bot.send_message(chat_id, msg)
                    except Exception:
                        pass

                paper_updates += 1

                if sell_signal.startswith("Sell / Exit"):
                    close_position(pair_address, sell_signal)

        # New signals
        for item in ranked[:10]:
            streak = update_confirmation(item)
            action = suggest_action(item, streak)
            log_signal(item, action, streak)

            if action == "Buy Setup" and should_alert(item):
                performance["buy_setups"] += 1
                pos = open_paper_position(item)

                msg = (
                    f"🚨 Buy Setup\n\n"
                    f"{format_pick(item, action, streak)}"
                    f"Paper Entry: ${PAPER_POSITION_SIZE_USD}\n"
                )

                for chat_id in list(subscribers):
                    try:
                        bot.send_message(chat_id, msg)
                    except Exception:
                        pass

                if pos:
                    sent_count += 1
                mark_alerted(item)

            elif action == "Watch" and grade_pick(item) in {"A+", "A"} and should_alert(item):
                performance["watch_alerts"] += 1
                msg = f"👀 Watchlist Alert\n\n{format_pick(item, action, streak)}"
                for chat_id in list(subscribers):
                    try:
                        bot.send_message(chat_id, msg)
                    except Exception:
                        pass
                sent_count += 1
                mark_alerted(item)

        last_scan_summary = (
            f"Scan ok. {len(ranked)} ranked | {sent_count} new alerts | "
            f"{paper_updates} paper updates."
        )

    except Exception as e:
        last_scan_summary = f"Scan error: {e}"

    with state_lock:
        persist_state()


def background_loop() -> None:
    while True:
        if subscribers:
            process_scan()
        time.sleep(SCAN_INTERVAL_SECONDS)


# =========================
# TELEGRAM HANDLERS
# =========================

@bot.message_handler(commands=["start"])
def start_cmd(message):
    subscribers.add(message.chat.id)
    with state_lock:
        persist_state()

    bot.send_message(
        message.chat.id,
        "Optimization bot activated 🎯\nYou will receive Watch / Buy Setup / Sell updates.\nPaper trades use $100 virtual size by default.",
        reply_markup=build_menu(),
    )


@bot.message_handler(commands=["now"])
def now_cmd(message):
    send_sniper_picks(message.chat.id)


@bot.message_handler(commands=["status"])
def status_cmd(message):
    bot.send_message(message.chat.id, f"Status:\n{last_scan_summary}")


@bot.message_handler(commands=["positions"])
def positions_cmd(message):
    send_positions(message.chat.id)


@bot.message_handler(commands=["signals"])
def signals_cmd(message):
    send_recent_signals(message.chat.id)


@bot.message_handler(commands=["performance"])
def performance_cmd(message):
    send_performance(message.chat.id)


@bot.message_handler(commands=["stop"])
def stop_cmd(message):
    subscribers.discard(message.chat.id)
    with state_lock:
        persist_state()
    bot.send_message(message.chat.id, "Alerts stopped for this chat.")


@bot.message_handler(func=lambda m: m.text == "🔥 Sniper Picks")
def picks_button(message):
    send_sniper_picks(message.chat.id)


@bot.message_handler(func=lambda m: m.text == "📊 Status")
def status_button(message):
    bot.send_message(message.chat.id, f"Status:\n{last_scan_summary}")


@bot.message_handler(func=lambda m: m.text == "📂 Paper Positions")
def positions_button(message):
    send_positions(message.chat.id)


@bot.message_handler(func=lambda m: m.text == "📈 Performance")
def performance_button(message):
    send_performance(message.chat.id)


@bot.message_handler(func=lambda m: m.text == "🧾 Recent Signals")
def signals_button(message):
    send_recent_signals(message.chat.id)


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(
        message.chat.id,
        "Use /start, /now, /status, /positions, /signals, /performance, /stop or tap a button.",
        reply_markup=build_menu(),
    )


# =========================
# RUN
# =========================

load_state()
threading.Thread(target=background_loop, daemon=True).start()
bot.infinity_polling(skip_pending=True)
