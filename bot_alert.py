print("🔥 BOT STARTED")

import os
import json
import time
import threading
from datetime import datetime, timezone

import requests
import websocket
import pandas as pd
import numpy as np

# ============================================================
# ENVIRONMENT
# ============================================================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # default alert chat

print("TOKEN LOADED:", TOKEN is not None)
print("CHAT_ID LOADED:", CHAT_ID is not None)

# ============================================================
# CONFIG
# ============================================================

WS_URL = "wss://stream.bybit.com/v5/public/linear"
BYBIT_REST = "https://api.bybit.com"

TIMEFRAMES = {
    "1": "1m",
    "5": "5m",
    "15": "15m",
    "60": "1h",
}

EMA_PERIOD = 200
RSI_PERIOD = 14
HISTORY_LIMIT = 250   # must be > EMA_PERIOD
TOP_SYMBOLS_COUNT = 50

# -------- Testing Mode --------
TESTING_MODE = False

# Real settings
# 0.007 = 0.70%
REAL_EMA_DEVIATION = 0.007
REAL_RSI_OVERBOUGHT = 95
REAL_RSI_OVERSOLD = 5
REAL_LARGE_CANDLE_MIN = 12.0
REAL_LARGE_CANDLE_STRONG = 15.0

# Testing settings (very sensitive)
TEST_EMA_DEVIATION = 0.0001   # 0.01%
TEST_RSI_OVERBOUGHT = 51
TEST_RSI_OVERSOLD = 49
TEST_LARGE_CANDLE_MIN = 1.2
TEST_LARGE_CANDLE_STRONG = 1.5

# Active settings
EMA_DEVIATION = REAL_EMA_DEVIATION
RSI_OVERBOUGHT = REAL_RSI_OVERBOUGHT
RSI_OVERSOLD = REAL_RSI_OVERSOLD
LARGE_CANDLE_MIN_RATIO = REAL_LARGE_CANDLE_MIN
LARGE_CANDLE_STRONG_RATIO = REAL_LARGE_CANDLE_STRONG

# ============================================================
# GLOBALS
# ============================================================

candles = {}             # (symbol, tf) -> DataFrame
last_alerted_candle = {} # (symbol, tf) -> last closed candle timestamp alerted
SYMBOLS = []

# lock for shared data
data_lock = threading.Lock()

# ============================================================
# MODE CONTROL
# ============================================================

def apply_mode():
    global EMA_DEVIATION, RSI_OVERBOUGHT, RSI_OVERSOLD
    global LARGE_CANDLE_MIN_RATIO, LARGE_CANDLE_STRONG_RATIO

    if TESTING_MODE:
        EMA_DEVIATION = TEST_EMA_DEVIATION
        RSI_OVERBOUGHT = TEST_RSI_OVERBOUGHT
        RSI_OVERSOLD = TEST_RSI_OVERSOLD
        LARGE_CANDLE_MIN_RATIO = TEST_LARGE_CANDLE_MIN
        LARGE_CANDLE_STRONG_RATIO = TEST_LARGE_CANDLE_STRONG
    else:
        EMA_DEVIATION = REAL_EMA_DEVIATION
        RSI_OVERBOUGHT = REAL_RSI_OVERBOUGHT
        RSI_OVERSOLD = REAL_RSI_OVERSOLD
        LARGE_CANDLE_MIN_RATIO = REAL_LARGE_CANDLE_MIN
        LARGE_CANDLE_STRONG_RATIO = REAL_LARGE_CANDLE_STRONG

# ============================================================
# TELEGRAM HELPERS
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
        r = requests.post(url, json=payload, timeout=10)
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
                        f"EMA deviation: {EMA_DEVIATION * 100:.4f}%\n"
                        f"RSI: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
                        f"Large candle: ≥{LARGE_CANDLE_MIN_RATIO}x (strong ≥{LARGE_CANDLE_STRONG_RATIO}x)",
                        chat_id,
                    )

                elif text == "/testmode_off":
                    TESTING_MODE = False
                    apply_mode()
                    send_telegram(
                        "✅ TESTING MODE DISABLED\n"
                        f"EMA deviation: {EMA_DEVIATION * 100:.2f}%\n"
                        f"RSI: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
                        f"Large candle: ≥{LARGE_CANDLE_MIN_RATIO}x (strong ≥{LARGE_CANDLE_STRONG_RATIO}x)",
                        chat_id,
                    )

                elif text == "/mode":
                    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"
                    send_telegram(
                        f"📌 Current mode: {mode}\n"
                        f"EMA deviation: {EMA_DEVIATION * 100:.4f}%\n"
                        f"RSI: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
                        f"Large candle: ≥{LARGE_CANDLE_MIN_RATIO}x (strong ≥{LARGE_CANDLE_STRONG_RATIO}x)",
                        chat_id,
                    )

        except Exception as e:
            print("TELEGRAM LISTENER ERROR:", e)
            time.sleep(3)

# ============================================================
# BYBIT HELPERS
# ============================================================

def fetch_top_linear_usdt(count=50):
    url = f"{BYBIT_REST}/v5/market/tickers"
    params = {"category": "linear"}

    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()

        if data.get("retCode") != 0:
            print("BYBIT TICKERS ERROR:", data)
            return []

        rows = data.get("result", {}).get("list", [])
        rows = [row for row in rows if row.get("symbol", "").endswith("USDT")]

        def turnover_value(row):
            try:
                return float(row.get("turnover24h", 0))
            except Exception:
                return 0.0

        rows.sort(key=turnover_value, reverse=True)
        symbols = [row["symbol"] for row in rows[:count]]

        print(f"TOP {count} LINEAR USDT SYMBOLS:", symbols)
        return symbols

    except Exception as e:
        print("FETCH TOP SYMBOLS ERROR:", e)
        return []

def fetch_historical_klines(symbol: str, interval: str, limit: int = 250):
    """
    Bybit v5 kline:
    result.list entries are usually strings:
    [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
    Returned newest first, so we reverse.
    """
    url = f"{BYBIT_REST}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    try:
        r = requests.get(url, params=params, timeout=15)
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
            try:
                parsed.append({
                    "time": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            except Exception as e:
                print(f"PARSE HISTORY ROW ERROR {symbol} {interval}:", e)

        if not parsed:
            return None

        df = pd.DataFrame(parsed)
        df = df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
        return df

    except Exception as e:
        print(f"FETCH HISTORY ERROR {symbol} {interval}:", e)
        return None

def bootstrap_history():
    print("BOOTSTRAPPING HISTORICAL CANDLES...")

    total_loaded = 0

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES.keys():
            try:
                df = fetch_historical_klines(symbol, tf, HISTORY_LIMIT)
                if df is not None and not df.empty:
                    with data_lock:
                        candles[(symbol, tf)] = df
                    total_loaded += 1
                    print(f"BOOTSTRAP OK: {symbol} {tf} -> {len(df)} candles")
                else:
                    print(f"BOOTSTRAP FAILED: {symbol} {tf}")
                time.sleep(0.03)
            except Exception as e:
                print(f"BOOTSTRAP ERROR {symbol} {tf}:", e)

    print("BOOTSTRAP COMPLETE. STREAMS LOADED:", total_loaded)

# ============================================================
# INDICATORS
# ============================================================

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def calc_rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ============================================================
# SIGNAL LOGIC
# ============================================================

def detect_large_candle(df: pd.DataFrame):
    if len(df) < 2:
        return None

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    prev_range = prev["high"] - prev["low"]
    curr_range = curr["high"] - curr["low"]

    if prev_range <= 0:
        return None

    ratio = curr_range / prev_range
    if ratio >= LARGE_CANDLE_MIN_RATIO:
        return ratio

    return None

def analyze_signals(symbol: str, tf_label: str, df: pd.DataFrame):
    result = {
        "ema_signal": None,
        "ema_info": None,
        "rsi_signal": None,
        "rsi_value": None,
        "lc_signal": None,
        "lc_ratio": None,
    }

    # Large candle can work with just 2 candles
    lc_ratio = detect_large_candle(df)
    if lc_ratio is not None:
        result["lc_ratio"] = lc_ratio
        result["lc_signal"] = "LARGE_STRONG" if lc_ratio >= LARGE_CANDLE_STRONG_RATIO else "LARGE"

    # EMA / RSI need enough history
    if len(df) >= max(EMA_PERIOD, RSI_PERIOD + 1):
        work = df.copy()

        work["ema200"] = calc_ema(work["close"], EMA_PERIOD)
        work["rsi"] = calc_rsi(work["close"], RSI_PERIOD)

        last = work.iloc[-1]
        price = float(last["close"])
        ema200 = float(last["ema200"])
        rsi = float(last["rsi"]) if not pd.isna(last["rsi"]) else None

        if ema200 > 0:
            deviation = (price - ema200) / ema200

            if deviation >= EMA_DEVIATION:
                result["ema_signal"] = "ABOVE"
                result["ema_info"] = (price, ema200, deviation * 100.0)
            elif deviation <= -EMA_DEVIATION:
                result["ema_signal"] = "BELOW"
                result["ema_info"] = (price, ema200, deviation * 100.0)

        if rsi is not None:
            result["rsi_value"] = rsi
            if rsi >= RSI_OVERBOUGHT:
                result["rsi_signal"] = "OVERBOUGHT"
            elif rsi <= RSI_OVERSOLD:
                result["rsi_signal"] = "OVERSOLD"

    return result

def build_alert_message(symbol: str, tf_label: str, sig: dict):
    conditions = []

    if sig["ema_signal"] is not None and sig["ema_info"] is not None:
        price, ema200, dev_pct = sig["ema_info"]
        conditions.append(
            f"EMA200 Deviation {sig['ema_signal']} | Price: {price:.6f} | EMA200: {ema200:.6f} | Deviation: {dev_pct:+.2f}%"
        )

    if sig["rsi_signal"] is not None and sig["rsi_value"] is not None:
        if sig["rsi_signal"] == "OVERBOUGHT":
            conditions.append(f"RSI Extreme OVERBOUGHT | RSI: {sig['rsi_value']:.2f}")
        else:
            conditions.append(f"RSI Extreme OVERSOLD | RSI: {sig['rsi_value']:.2f}")

    if sig["lc_signal"] is not None and sig["lc_ratio"] is not None:
        if sig["lc_signal"] == "LARGE_STRONG":
            conditions.append(
                f"Large Candle STRONG | Ratio: {sig['lc_ratio']:.2f}x (>= {LARGE_CANDLE_STRONG_RATIO}x)"
            )
        else:
            conditions.append(
                f"Large Candle | Ratio: {sig['lc_ratio']:.2f}x (>= {LARGE_CANDLE_MIN_RATIO}x)"
            )

    if not conditions:
        return None

    count = len(conditions)
    if count == 1:
        header = f"📌 SINGLE CONDITION | {symbol} {tf_label}"
    elif count == 2:
        header = f"⚡ CONFLUENCE (2/3) | {symbol} {tf_label}"
    else:
        header = f"🔥 STRONG CONFLUENCE (3/3) | {symbol} {tf_label}"

    return header + "\n" + "\n".join(conditions)

# ============================================================
# CANDLE STORAGE
# ============================================================

def update_candles(symbol: str, tf: str, kline: dict):
    key = (symbol, tf)

    start_time_ms = int(kline["start"])
    ts = datetime.fromtimestamp(start_time_ms / 1000, tz=timezone.utc)

    row = {
        "time": ts,
        "open": float(kline["open"]),
        "high": float(kline["high"]),
        "low": float(kline["low"]),
        "close": float(kline["close"]),
        "volume": float(kline["volume"]),
    }

    with data_lock:
        if key not in candles or candles[key].empty:
            candles[key] = pd.DataFrame([row])
            return candles[key]

        df = candles[key]

        if df.iloc[-1]["time"] == ts:
            df.iloc[-1] = [row["time"], row["open"], row["high"], row["low"], row["close"], row["volume"]]
        else:
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            if len(df) > HISTORY_LIMIT + 50:
                df = df.iloc[-(HISTORY_LIMIT + 20):].reset_index(drop=True)
            candles[key] = df

        return candles[key]

# ============================================================
# DUPLICATE ALERT CONTROL
# ============================================================

def should_alert(symbol: str, tf: str, closed_candle_time):
    key = (symbol, tf)
    prev = last_alerted_candle.get(key)

    if prev == closed_candle_time:
        return False

    last_alerted_candle[key] = closed_candle_time
    return True

# ============================================================
# WEBSOCKET CALLBACKS
# ============================================================

def on_message(ws, message):
    try:
        data = json.loads(message)
    except Exception as e:
        print("WS PARSE ERROR:", e)
        return

    if "topic" not in data or "data" not in data:
        return

    topic = data["topic"]
    parts = topic.split(".")
    if len(parts) != 3:
        return

    _, tf, symbol = parts
    tf_label = TIMEFRAMES.get(tf, tf)

    try:
        klines = data["data"]
        if isinstance(klines, dict):
            klines = [klines]
    except Exception:
        return

    for kline in klines:
        try:
            if not kline.get("confirm", False):
                continue  # only closed candles

            df = update_candles(symbol, tf, kline)
            sig = analyze_signals(symbol, tf_label, df)

            msg = build_alert_message(symbol, tf_label, sig)
            if not msg:
                continue

            closed_candle_time = df.iloc[-1]["time"]

            if should_alert(symbol, tf, closed_candle_time):
                print(f"ALERT READY: {symbol} {tf_label}")
                log_and_alert(msg)

        except Exception as e:
            print(f"ON_MESSAGE PROCESS ERROR {symbol} {tf_label}:", e)

def on_error(ws, error):
    print("WS ERROR:", error)

def on_close(ws, code, msg):
    print("WS CLOSED:", code, msg)

def on_open(ws):
    print("WS CONNECTED")

    args = []
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES.keys():
            args.append(f"kline.{tf}.{symbol}")

    try:
        sub_msg = {"op": "subscribe", "args": args}
        ws.send(json.dumps(sub_msg))
        print("SUBSCRIBED TO", len(args), "STREAMS")
    except Exception as e:
        print("SUBSCRIBE ERROR:", e)

# ============================================================
# WEBSOCKET RUNNER
# ============================================================

def ws_forever():
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
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print("WEBSOCKET LOOP ERROR:", e)

        print("RECONNECTING WEBSOCKET IN 5 SECONDS...")
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

    SYMBOLS = fetch_top_linear_usdt(TOP_SYMBOLS_COUNT)
    if not SYMBOLS:
        print("NO SYMBOLS FETCHED — EXITING")
        raise SystemExit(1)

    bootstrap_history()

    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"

    send_telegram(
        "🚀 DERIVATIVES SCANNER RUNNING\n"
        f"Mode: {mode}\n"
        f"Top {TOP_SYMBOLS_COUNT} Bybit USDT-Perp by volume\n"
        f"Timeframes: {', '.join(TIMEFRAMES.values())}\n"
        f"EMA200 deviation: {EMA_DEVIATION * 100:.4f}%\n"
        f"RSI extremes: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
        f"Large candle: ≥{LARGE_CANDLE_MIN_RATIO}x (strong ≥{LARGE_CANDLE_STRONG_RATIO}x)"
    )

    threading.Thread(target=telegram_command_listener, daemon=True).start()
    ws_forever()
