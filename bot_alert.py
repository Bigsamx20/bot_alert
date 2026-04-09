print("🔥 BOT STARTED")

import os
import json
import time
import math
import threading
from datetime import datetime, timezone

import requests
import websocket
import pandas as pd

# ============================================================
# ENVIRONMENT
# ============================================================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

print("TOKEN LOADED:", TOKEN is not None)
print("CHAT_ID LOADED:", CHAT_ID is not None)

# ============================================================
# CONFIG
# ============================================================

WS_URL = "wss://stream.bybit.com/v5/public/linear"
BYBIT_REST = "https://api.bybit.com"

# Bybit intervals:
# 1 = 1m, 5 = 5m, 60 = 1h
TIMEFRAMES = {
    "1": "1m",
    "5": "5m",
    "60": "1h",
}

BYBIT_CATEGORY = "linear"  # only Bybit linear market
SYMBOL_SUFFIX = "USDT"

RSI_PERIOD = 14
EMA_PERIOD = 200
HISTORY_LIMIT = 260

TOP_SYMBOLS_COUNT = 50
TOP_GAINERS_COUNT = 10
UNIVERSE_REFRESH_SECONDS = 300  # refresh symbol list every 5 minutes

# ============================================================
# SETTINGS
# ============================================================

# IMPORTANT:
# Your spec said "65%" deviation from EMA200.
# In practice this is extremely large. Most traders actually mean 0.65%.
# This bot supports both:
# - LIVE default below is 0.65% = 0.0065
# - If you truly want 65%, change to 0.65
REAL_EMA_DEVIATION = 0.0065   # 0.65%
TEST_EMA_DEVIATION = 0.0010   # 0.10% for easier testing

RSI_OVERBOUGHT = 95
RSI_OVERSOLD = 5

LARGE_CANDLE_MIN_MULTIPLE = 12.0
LARGE_CANDLE_EXTREME_MULTIPLE = 15.0
EPSILON = 1e-12

TESTING_MODE = False
EMA_DEVIATION = REAL_EMA_DEVIATION

# ============================================================
# GLOBALS
# ============================================================

candles = {}                 # (symbol, tf) -> DataFrame
last_alert_keys = set()      # dedupe: (symbol, tf, alert_type, candle_time_iso)
SYMBOLS = []                 # current active universe
subscribed_topics = set()    # track live subscriptions
data_lock = threading.Lock()
ws_app = None
ws_lock = threading.Lock()

# ============================================================
# MODE CONTROL
# ============================================================

def apply_mode():
    global EMA_DEVIATION
    EMA_DEVIATION = TEST_EMA_DEVIATION if TESTING_MODE else REAL_EMA_DEVIATION

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(msg: str, chat_id=None):
    target = chat_id if chat_id is not None else CHAT_ID

    if not TOKEN or not target:
        print("TELEGRAM SKIPPED: Missing TOKEN or CHAT_ID")
        return

    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": str(target),
            "text": msg,
        }
        r = requests.post(url, json=payload, timeout=15)
        print("TELEGRAM RESPONSE:", r.status_code, r.text)
    except Exception as e:
        print("TELEGRAM ERROR:", e)

def log_and_alert(msg: str, chat_id=None):
    print(msg)
    send_telegram(msg, chat_id)

# ============================================================
# TELEGRAM COMMAND LISTENER
# ============================================================

def telegram_command_listener():
    if not TOKEN:
        print("TELEGRAM LISTENER DISABLED: No TOKEN")
        return

    global TESTING_MODE
    last_update_id = None
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

    print("TELEGRAM COMMAND LISTENER STARTED")

    while True:
        try:
            params = {"timeout": 20}
            if last_update_id is not None:
                params["offset"] = last_update_id

            r = requests.get(url, params=params, timeout=30)
            data = r.json()

            if "result" not in data:
                print("TELEGRAM GETUPDATES BAD RESPONSE:", data)
                time.sleep(2)
                continue

            for update in data["result"]:
                last_update_id = update["update_id"] + 1

                if "message" not in update:
                    continue

                msg = update["message"]
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")

                print("TELEGRAM UPDATE:", text, "FROM CHAT:", chat_id)

                if text == "/test":
                    send_telegram("🧪 TEST COMMAND RECEIVED — Bot is working!", chat_id)

                elif text == "/testmode_on":
                    TESTING_MODE = True
                    apply_mode()
                    send_telegram(
                        "🧪 TESTING MODE ENABLED\n"
                        f"EMA200 deviation threshold: {EMA_DEVIATION * 100:.4f}%\n"
                        "Signal logic active: RSI, EMA200, large candle, confluence\n"
                        "Dedupe: one alert per symbol/timeframe/type/candle",
                        chat_id,
                    )

                elif text == "/testmode_off":
                    TESTING_MODE = False
                    apply_mode()
                    send_telegram(
                        "✅ LIVE MODE ENABLED\n"
                        f"EMA200 deviation threshold: {EMA_DEVIATION * 100:.4f}%\n"
                        "Signal logic active: RSI, EMA200, large candle, confluence\n"
                        "Dedupe: one alert per symbol/timeframe/type/candle",
                        chat_id,
                    )

                elif text == "/mode":
                    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"
                    send_telegram(
                        f"📌 Current mode: {mode}\n"
                        f"EMA200 deviation threshold: {EMA_DEVIATION * 100:.4f}%\n"
                        f"RSI: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
                        f"Large candle: {LARGE_CANDLE_MIN_MULTIPLE}x to {LARGE_CANDLE_EXTREME_MULTIPLE}x+\n"
                        f"Universe size: {len(SYMBOLS)} symbols",
                        chat_id,
                    )

                elif text == "/symbols":
                    preview = ", ".join(SYMBOLS[:25])
                    send_telegram(
                        f"📊 Current universe ({len(SYMBOLS)} symbols)\n"
                        f"{preview}" + ("\n..." if len(SYMBOLS) > 25 else ""),
                        chat_id,
                    )

                elif text == "/help":
                    send_telegram(
                        "/test - bot check\n"
                        "/mode - current mode/settings\n"
                        "/symbols - current monitored symbols\n"
                        "/testmode_on - enable test mode\n"
                        "/testmode_off - enable live mode",
                        chat_id,
                    )

        except Exception as e:
            print("TELEGRAM LISTENER ERROR:", e)
            time.sleep(3)

# ============================================================
# BYBIT HELPERS
# ============================================================

def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def fetch_all_linear_tickers():
    url = f"{BYBIT_REST}/v5/market/tickers"
    params = {"category": BYBIT_CATEGORY}

    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()

        if data.get("retCode") != 0:
            print("BYBIT TICKERS ERROR:", data)
            return []

        rows = data.get("result", {}).get("list", [])
        rows = [row for row in rows if row.get("symbol", "").endswith(SYMBOL_SUFFIX)]
        return rows

    except Exception as e:
        print("FETCH ALL TICKERS ERROR:", e)
        return []

def build_symbol_universe():
    """
    Spec:
    1) Monitor top 50 coins
    2) Include 10 gaining coins on Bybit
    3) Only Bybit exchange
    """
    rows = fetch_all_linear_tickers()
    if not rows:
        return []

    # Top 50 by turnover24h
    rows_by_turnover = sorted(
        rows,
        key=lambda x: safe_float(x.get("turnover24h", 0)),
        reverse=True
    )
    top_50 = [row["symbol"] for row in rows_by_turnover[:TOP_SYMBOLS_COUNT]]

    # Top 10 gainers by price24hPcnt
    rows_by_gain = sorted(
        rows,
        key=lambda x: safe_float(x.get("price24hPcnt", 0)),
        reverse=True
    )
    top_10_gainers = [row["symbol"] for row in rows_by_gain[:TOP_GAINERS_COUNT]]

    # Union while preserving order
    final_symbols = []
    seen = set()

    for sym in top_50 + top_10_gainers:
        if sym not in seen:
            seen.add(sym)
            final_symbols.append(sym)

    print("TOP 50:", top_50)
    print("TOP 10 GAINERS:", top_10_gainers)
    print("FINAL UNIVERSE:", final_symbols)
    return final_symbols

def fetch_historical_klines(symbol: str, interval: str, limit: int = HISTORY_LIMIT):
    url = f"{BYBIT_REST}/v5/market/kline"
    params = {
        "category": BYBIT_CATEGORY,
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()

        if data.get("retCode") != 0:
            print(f"HISTORY ERROR {symbol} {interval}:", data)
            return None

        rows = data.get("result", {}).get("list", [])
        if not rows:
            print(f"NO HISTORY: {symbol} {interval}")
            return None

        rows.reverse()

        parsed = []
        for row in rows:
            parsed.append({
                "time": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
                "open": safe_float(row[1]),
                "high": safe_float(row[2]),
                "low": safe_float(row[3]),
                "close": safe_float(row[4]),
                "volume": safe_float(row[5]),
            })

        df = pd.DataFrame(parsed)
        df = df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
        return df

    except Exception as e:
        print(f"FETCH HISTORY ERROR {symbol} {interval}:", e)
        return None

def bootstrap_history_for_symbols(symbols):
    print("BOOTSTRAPPING HISTORICAL CANDLES...")
    total_loaded = 0

    for symbol in symbols:
        for tf in TIMEFRAMES.keys():
            df = fetch_historical_klines(symbol, tf, HISTORY_LIMIT)
            if df is not None and not df.empty:
                with data_lock:
                    candles[(symbol, tf)] = df
                total_loaded += 1
                print(f"BOOTSTRAP OK: {symbol} {tf} -> {len(df)} candles")
            else:
                print(f"BOOTSTRAP FAILED: {symbol} {tf}")
            time.sleep(0.03)

    print("BOOTSTRAP COMPLETE. STREAMS LOADED:", total_loaded)

# ============================================================
# INDICATORS
# ============================================================

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(100)
    return rsi

# ============================================================
# SIGNAL LOGIC
# ============================================================

def compute_signal_info(df: pd.DataFrame):
    """
    Returns:
    {
      "candle_time": datetime,
      "price": float,
      "ema200": float,
      "ema_distance_pct": float,
      "rsi": float,
      "rsi_hit": bool,
      "rsi_side": "OVERBOUGHT"/"OVERSOLD"/None,
      "ema_hit": bool,
      "ema_side": "ABOVE"/"BELOW"/None,
      "candle_hit": bool,
      "candle_severity": "LARGE"/"EXTREME"/None,
      "body_multiple": float,
      "range_multiple": float,
      "hit_count": int,
      "conditions": [...]
    }
    """
    if df is None or len(df) < max(EMA_PERIOD, RSI_PERIOD + 5, 2):
        return None

    work = df.copy()
    work["ema200"] = calc_ema(work["close"], EMA_PERIOD)
    work["rsi"] = calc_rsi(work["close"], RSI_PERIOD)

    last = work.iloc[-1]
    prev = work.iloc[-2]

    price = safe_float(last["close"])
    ema200 = safe_float(last["ema200"])
    rsi = safe_float(last["rsi"])
    candle_time = last["time"]

    if ema200 <= 0:
        return None

    ema_distance = (price - ema200) / ema200
    ema_distance_pct = ema_distance * 100.0

    # RSI
    rsi_hit = False
    rsi_side = None
    if rsi >= RSI_OVERBOUGHT:
        rsi_hit = True
        rsi_side = "OVERBOUGHT"
    elif rsi <= RSI_OVERSOLD:
        rsi_hit = True
        rsi_side = "OVERSOLD"

    # EMA distance
    ema_hit = False
    ema_side = None
    if ema_distance >= EMA_DEVIATION:
        ema_hit = True
        ema_side = "ABOVE"
    elif ema_distance <= -EMA_DEVIATION:
        ema_hit = True
        ema_side = "BELOW"

    # Large candle vs previous
    curr_body = abs(safe_float(last["close"]) - safe_float(last["open"]))
    prev_body = abs(safe_float(prev["close"]) - safe_float(prev["open"]))
    curr_range = safe_float(last["high"]) - safe_float(last["low"])
    prev_range = safe_float(prev["high"]) - safe_float(prev["low"])

    body_multiple = curr_body / max(prev_body, EPSILON)
    range_multiple = curr_range / max(prev_range, EPSILON)
    max_multiple = max(body_multiple, range_multiple)

    candle_hit = max_multiple >= LARGE_CANDLE_MIN_MULTIPLE
    candle_severity = None
    if candle_hit:
        candle_severity = "EXTREME" if max_multiple >= LARGE_CANDLE_EXTREME_MULTIPLE else "LARGE"

    conditions = []
    if rsi_hit:
        if rsi_side == "OVERBOUGHT":
            conditions.append(f"RSI >= {RSI_OVERBOUGHT} ({rsi:.2f})")
        else:
            conditions.append(f"RSI <= {RSI_OVERSOLD} ({rsi:.2f})")

    if ema_hit:
        if ema_side == "ABOVE":
            conditions.append(f"Price above EMA200 by {abs(ema_distance_pct):.2f}%")
        else:
            conditions.append(f"Price below EMA200 by {abs(ema_distance_pct):.2f}%")

    if candle_hit:
        conditions.append(
            f"Large candle {candle_severity} "
            f"(body {body_multiple:.2f}x, range {range_multiple:.2f}x)"
        )

    hit_count = int(rsi_hit) + int(ema_hit) + int(candle_hit)

    return {
        "candle_time": candle_time,
        "price": price,
        "ema200": ema200,
        "ema_distance_pct": ema_distance_pct,
        "rsi": rsi,
        "rsi_hit": rsi_hit,
        "rsi_side": rsi_side,
        "ema_hit": ema_hit,
        "ema_side": ema_side,
        "candle_hit": candle_hit,
        "candle_severity": candle_severity,
        "body_multiple": body_multiple,
        "range_multiple": range_multiple,
        "hit_count": hit_count,
        "conditions": conditions,
    }

def make_alert_key(symbol: str, tf: str, alert_type: str, candle_time: datetime):
    return (symbol, tf, alert_type, candle_time.isoformat())

def should_send_alert(symbol: str, tf: str, alert_type: str, candle_time: datetime):
    key = make_alert_key(symbol, tf, alert_type, candle_time)
    if key in last_alert_keys:
        return False
    last_alert_keys.add(key)
    return True

def build_single_alert_message(symbol: str, tf_label: str, signal: dict, alert_type: str):
    price = signal["price"]
    ema200 = signal["ema200"]
    ema_distance_pct = signal["ema_distance_pct"]
    rsi = signal["rsi"]
    body_multiple = signal["body_multiple"]
    range_multiple = signal["range_multiple"]
    candle_time = signal["candle_time"].strftime("%Y-%m-%d %H:%M:%S UTC")

    if alert_type == "RSI_EXTREME":
        direction = signal["rsi_side"]
        return (
            f"🚨 RSI EXTREME | {symbol} {tf_label}\n"
            f"Direction: {direction}\n"
            f"RSI({RSI_PERIOD}): {rsi:.2f}\n"
            f"Price: {price:.6f}\n"
            f"Candle: {candle_time}"
        )

    if alert_type == "EMA200_DISTANCE":
        direction = signal["ema_side"]
        sign = "+" if ema_distance_pct >= 0 else "-"
        return (
            f"🚨 EMA200 DISTANCE | {symbol} {tf_label}\n"
            f"Direction: {direction}\n"
            f"Price: {price:.6f}\n"
            f"EMA200: {ema200:.6f}\n"
            f"Deviation: {sign}{abs(ema_distance_pct):.2f}%\n"
            f"Threshold: {EMA_DEVIATION * 100:.4f}%\n"
            f"Candle: {candle_time}"
        )

    if alert_type == "LARGE_CANDLE":
        severity = signal["candle_severity"]
        return (
            f"🚨 LARGE CANDLE | {symbol} {tf_label}\n"
            f"Severity: {severity}\n"
            f"Body Multiple: {body_multiple:.2f}x\n"
            f"Range Multiple: {range_multiple:.2f}x\n"
            f"Price: {price:.6f}\n"
            f"Candle: {candle_time}"
        )

    return None

def build_confluence_alert_message(symbol: str, tf_label: str, signal: dict):
    price = signal["price"]
    ema200 = signal["ema200"]
    ema_distance_pct = signal["ema_distance_pct"]
    rsi = signal["rsi"]
    body_multiple = signal["body_multiple"]
    range_multiple = signal["range_multiple"]
    candle_time = signal["candle_time"].strftime("%Y-%m-%d %H:%M:%S UTC")

    cond_text = "\n".join([f"- {c}" for c in signal["conditions"]])

    return (
        f"🔥 CONFLUENCE 2/3 | {symbol} {tf_label}\n"
        f"Hits: {signal['hit_count']}/3\n"
        f"{cond_text}\n"
        f"Price: {price:.6f}\n"
        f"RSI({RSI_PERIOD}): {rsi:.2f}\n"
        f"EMA200: {ema200:.6f}\n"
        f"EMA Distance: {ema_distance_pct:.2f}%\n"
        f"Body Multiple: {body_multiple:.2f}x\n"
        f"Range Multiple: {range_multiple:.2f}x\n"
        f"Candle: {candle_time}"
    )

def process_closed_candle(symbol: str, tf: str, tf_label: str, df: pd.DataFrame):
    signal = compute_signal_info(df)
    if signal is None:
        return

    candle_time = signal["candle_time"]

    # "No interference on confluence or single alert"
    # Priority:
    # 1) Confluence (2 or 3 hits)
    # 2) Large candle
    # 3) RSI extreme
    # 4) EMA distance
    #
    # If confluence exists on same candle, we send ONLY confluence
    # and suppress same-candle single alerts.

    if signal["hit_count"] >= 2:
        alert_type = "CONFLUENCE_2_OF_3"
        if should_send_alert(symbol, tf, alert_type, candle_time):
            msg = build_confluence_alert_message(symbol, tf_label, signal)
            log_and_alert(msg)
        return

    if signal["candle_hit"]:
        alert_type = "LARGE_CANDLE"
        if should_send_alert(symbol, tf, alert_type, candle_time):
            msg = build_single_alert_message(symbol, tf_label, signal, alert_type)
            if msg:
                log_and_alert(msg)
        return

    if signal["rsi_hit"]:
        alert_type = "RSI_EXTREME"
        if should_send_alert(symbol, tf, alert_type, candle_time):
            msg = build_single_alert_message(symbol, tf_label, signal, alert_type)
            if msg:
                log_and_alert(msg)
        return

    if signal["ema_hit"]:
        alert_type = "EMA200_DISTANCE"
        if should_send_alert(symbol, tf, alert_type, candle_time):
            msg = build_single_alert_message(symbol, tf_label, signal, alert_type)
            if msg:
                log_and_alert(msg)
        return

# ============================================================
# CANDLE STORAGE
# ============================================================

def update_candles(symbol: str, tf: str, kline: dict):
    key = (symbol, tf)

    ts = datetime.fromtimestamp(int(kline["start"]) / 1000, tz=timezone.utc)

    row = {
        "time": ts,
        "open": safe_float(kline["open"]),
        "high": safe_float(kline["high"]),
        "low": safe_float(kline["low"]),
        "close": safe_float(kline["close"]),
        "volume": safe_float(kline["volume"]),
    }

    with data_lock:
        if key not in candles or candles[key].empty:
            candles[key] = pd.DataFrame([row])
            return candles[key]

        df = candles[key]

        if df.iloc[-1]["time"] == ts:
            df.iloc[-1] = [
                row["time"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            ]
        else:
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            if len(df) > HISTORY_LIMIT + 60:
                df = df.iloc[-(HISTORY_LIMIT + 20):].reset_index(drop=True)
            candles[key] = df

        return candles[key]

# ============================================================
# WEBSOCKET
# ============================================================

def topic_for(symbol: str, tf: str):
    return f"kline.{tf}.{symbol}"

def subscribe_topics(topics):
    global ws_app
    if not topics:
        return

    with ws_lock:
        ws = ws_app

    if ws is None:
        print("SUBSCRIBE SKIPPED: ws_app is None")
        return

    try:
        payload = {"op": "subscribe", "args": topics}
        ws.send(json.dumps(payload))
        print("SUBSCRIBED:", len(topics), "topics")
    except Exception as e:
        print("SUBSCRIBE ERROR:", e)

def on_message(ws, message):
    try:
        data = json.loads(message)
    except Exception as e:
        print("WS PARSE ERROR:", e)
        return

    if "topic" not in data or "data" not in data:
        return

    parts = data["topic"].split(".")
    if len(parts) != 3:
        return

    _, tf, symbol = parts
    tf_label = TIMEFRAMES.get(tf, tf)

    klines = data["data"]
    if isinstance(klines, dict):
        klines = [klines]

    for kline in klines:
        try:
            if not kline.get("confirm", False):
                continue  # closed candles only

            df = update_candles(symbol, tf, kline)
            process_closed_candle(symbol, tf, tf_label, df)

        except Exception as e:
            print(f"ON_MESSAGE PROCESS ERROR {symbol} {tf_label}:", e)

def on_error(ws, error):
    print("WS ERROR:", error)

def on_close(ws, code, msg):
    print("WS CLOSED:", code, msg)

def on_open(ws):
    global ws_app
    with ws_lock:
        ws_app = ws

    print("WS CONNECTED")

    topics = []
    with data_lock:
        for symbol in SYMBOLS:
            for tf in TIMEFRAMES.keys():
                t = topic_for(symbol, tf)
                topics.append(t)
                subscribed_topics.add(t)

    subscribe_topics(topics)

def ws_forever():
    global ws_app
    while True:
        try:
            print("STARTING WEBSOCKET CONNECTION...")
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            with ws_lock:
                ws_app = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print("WEBSOCKET LOOP ERROR:", e)

        print("RECONNECTING WEBSOCKET IN 5 SECONDS...")
        time.sleep(5)

# ============================================================
# UNIVERSE REFRESH
# ============================================================

def refresh_universe_loop():
    """
    Periodically refresh top 50 + top 10 gainers from Bybit.
    If new symbols appear, bootstrap them and subscribe them live.
    Old symbols remain in memory unless you choose to prune them.
    """
    global SYMBOLS

    while True:
        try:
            time.sleep(UNIVERSE_REFRESH_SECONDS)

            print("REFRESHING UNIVERSE...")
            new_symbols = build_symbol_universe()
            if not new_symbols:
                print("UNIVERSE REFRESH FAILED: no symbols returned")
                continue

            with data_lock:
                old_set = set(SYMBOLS)
                new_set = set(new_symbols)

            added = [s for s in new_symbols if s not in old_set]
            removed = [s for s in SYMBOLS if s not in new_set]

            if not added and not removed:
                print("UNIVERSE UNCHANGED")
                continue

            print("UNIVERSE CHANGED")
            print("ADDED:", added)
            print("REMOVED:", removed)

            # Bootstrap new symbols
            if added:
                bootstrap_history_for_symbols(added)

            # Update global symbol list
            with data_lock:
                SYMBOLS = new_symbols

            # Subscribe only newly added topics
            new_topics = []
            for symbol in added:
                for tf in TIMEFRAMES.keys():
                    t = topic_for(symbol, tf)
                    if t not in subscribed_topics:
                        subscribed_topics.add(t)
                        new_topics.append(t)

            if new_topics:
                subscribe_topics(new_topics)

            send_telegram(
                "🔄 Universe updated\n"
                f"Total symbols: {len(SYMBOLS)}\n"
                f"Added: {len(added)}\n"
                f"Removed: {len(removed)}"
            )

        except Exception as e:
            print("UNIVERSE REFRESH LOOP ERROR:", e)
            time.sleep(5)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("BOT STARTING...")

    apply_mode()

    if not TOKEN:
        print("WARNING: TOKEN is missing")
    if not CHAT_ID:
        print("WARNING: CHAT_ID is missing")

    SYMBOLS = build_symbol_universe()
    if not SYMBOLS:
        print("NO SYMBOLS FETCHED — EXITING")
        raise SystemExit(1)

    bootstrap_history_for_symbols(SYMBOLS)

    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"

    send_telegram(
        "🚀 BYBIT ALERT BOT RUNNING\n"
        f"Mode: {mode}\n"
        "Exchange: Bybit only\n"
        f"Market: {BYBIT_CATEGORY}\n"
        f"Universe: Top {TOP_SYMBOLS_COUNT} + Top {TOP_GAINERS_COUNT} gainers (deduped)\n"
        f"Symbols loaded: {len(SYMBOLS)}\n"
        f"Timeframes: {', '.join(TIMEFRAMES.values())}\n"
        f"RSI: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
        f"EMA200 deviation threshold: {EMA_DEVIATION * 100:.4f}%\n"
        f"Large candle: {LARGE_CANDLE_MIN_MULTIPLE}x to {LARGE_CANDLE_EXTREME_MULTIPLE}x+\n"
        "Confluence: alert when 2/3 indicators align\n"
        "Single alert priority: Large Candle > RSI > EMA\n"
        "No same-candle interference: confluence suppresses single alerts"
    )

    threading.Thread(target=telegram_command_listener, daemon=True).start()
    threading.Thread(target=refresh_universe_loop, daemon=True).start()
    ws_forever()
