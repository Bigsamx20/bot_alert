print("🔥 BOT STARTED — BEFORE IMPORTS")

import os
import json
import time
import requests
import websocket
import pandas as pd
import numpy as np
from datetime import datetime
import threading

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

# Bybit linear (USDT perpetual) public WS
WS_URL = "wss://stream.bybit.com/v5/public/linear"

# Timeframes (Bybit intervals) and labels
TIMEFRAMES = {
    "1": "1m",
    "5": "5m",
    "15": "15m",
    "60": "1h",
}

# Indicator base periods
EMA_PERIOD = 200
RSI_PERIOD = 14

# -------- Testing Mode System --------
TESTING_MODE = False

# Real settings
REAL_EMA_DEVIATION = 0.70
REAL_RSI_OVERBOUGHT = 95
REAL_RSI_OVERSOLD = 5
REAL_LARGE_CANDLE_MIN = 12.0
REAL_LARGE_CANDLE_STRONG = 15.0

# Testing settings (very sensitive)
TEST_EMA_DEVIATION = 0.01
TEST_RSI_OVERBOUGHT = 51
TEST_RSI_OVERSOLD = 49
TEST_LARGE_CANDLE_MIN = 1.2
TEST_LARGE_CANDLE_STRONG = 1.5

# Working settings (will be set by apply_mode)
EMA_DEVIATION = REAL_EMA_DEVIATION
RSI_OVERBOUGHT = REAL_RSI_OVERBOUGHT
RSI_OVERSOLD = REAL_RSI_OVERSOLD
LARGE_CANDLE_MIN_RATIO = REAL_LARGE_CANDLE_MIN
LARGE_CANDLE_STRONG_RATIO = REAL_LARGE_CANDLE_STRONG


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


# Data storage
candles = {}  # (symbol, tf) -> DataFrame

# ============================================================
# TELEGRAM HELPERS
# ============================================================

def send_telegram(msg: str, chat_id: str | int | None = None):
    target = chat_id if chat_id is not None else CHAT_ID
    if not TOKEN or not target:
        print("TELEGRAM SKIPPED: Missing TOKEN or CHAT_ID")
        return

    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": target, "text": msg}
        r = requests.post(url, json=payload, timeout=10)
        print("TELEGRAM RESPONSE:", r.status_code, r.text)
    except Exception as e:
        print("TELEGRAM ERROR:", e)


def log_and_alert(msg: str):
    print(msg)
    send_telegram(msg)

# ============================================================
# TELEGRAM COMMAND LISTENER (/test + testing mode)
# ============================================================

def telegram_command_listener():
    if not TOKEN:
        print("TELEGRAM LISTENER DISABLED: No TOKEN")
        return

    global TESTING_MODE

    last_update_id = None
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

    while True:
        try:
            params = {"timeout": 10, "offset": last_update_id}
            r = requests.get(url, params=params, timeout=15)
            data = r.json()

            if "result" not in data:
                time.sleep(1)
                continue

            for update in data["result"]:
                last_update_id = update["update_id"] + 1

                if "message" not in update:
                    continue

                msg = update["message"]
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")

                print("TELEGRAM UPDATE:", text, "FROM CHAT:", chat_id)

                t = text.strip()

                # /test
                if t == "/test":
                    send_telegram("🧪 TEST COMMAND RECEIVED — Bot is working!", chat_id)
                    continue

                # /testmode_on
                if t == "/testmode_on":
                    TESTING_MODE = True
                    apply_mode()
                    send_telegram(
                        "🧪 TESTING MODE ENABLED\n"
                        "EMA deviation: 1%\n"
                        "RSI: 49/51\n"
                        "Large candle: ≥1.2x (strong ≥1.5x)",
                        chat_id,
                    )
                    continue

                # /testmode_off
                if t == "/testmode_off":
                    TESTING_MODE = False
                    apply_mode()
                    send_telegram(
                        "✅ TESTING MODE DISABLED\n"
                        f"EMA deviation: {REAL_EMA_DEVIATION*100:.0f}%\n"
                        f"RSI: {REAL_RSI_OVERSOLD}/{REAL_RSI_OVERBOUGHT}\n"
                        f"Large candle: ≥{REAL_LARGE_CANDLE_MIN}x (strong ≥{REAL_LARGE_CANDLE_STRONG}x)",
                        chat_id,
                    )
                    continue

                # /mode
                if t == "/mode":
                    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"
                    send_telegram(
                        f"📌 Current mode: {mode}\n"
                        f"EMA deviation: {EMA_DEVIATION*100:.2f}%\n"
                        f"RSI: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
                        f"Large candle: ≥{LARGE_CANDLE_MIN_RATIO}x (strong ≥{LARGE_CANDLE_STRONG_RATIO}x)",
                        chat_id,
                    )
                    continue

        except Exception as e:
            print("TELEGRAM LISTENER ERROR:", e)
            time.sleep(2)

# ============================================================
# BYBIT SYMBOL FETCHER (TOP 50 LINEAR USDT PERPS BY VOLUME)
# ============================================================

def fetch_top_50_linear_usdt():
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear"}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            print("BYBIT TICKERS ERROR:", data)
            return []

        rows = data.get("result", {}).get("list", [])
        # Filter USDT pairs only
        rows = [row for row in rows if row.get("symbol", "").endswith("USDT")]

        # Sort by 24h turnover
        def _vol(row):
            try:
                return float(row.get("turnover24h", "0"))
            except Exception:
                return 0.0

        rows.sort(key=_vol, reverse=True)
        top = rows[:50]
        symbols = [row["symbol"] for row in top]
        print("TOP 50 LINEAR USDT SYMBOLS:", symbols)
        return symbols

    except Exception as e:
        print("FETCH TOP 50 ERROR:", e)
        return []

# ============================================================
# INDICATORS
# ============================================================

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain = pd.Series(gain).rolling(period).mean()
    loss = pd.Series(loss).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ============================================================
# LARGE CANDLE DETECTION
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

# ============================================================
# CANDLE MANAGEMENT
# ============================================================

def update_candles(symbol: str, tf: str, kline: dict):
    key = (symbol, tf)

    start_time = int(kline["start"]) / 1000
    open_price = float(kline["open"])
    high_price = float(kline["high"])
    low_price = float(kline["low"])
    close_price = float(kline["close"])
    volume = float(kline["volume"])

    ts = datetime.utcfromtimestamp(start_time)

    if key not in candles:
        candles[key] = pd.DataFrame(
            columns=["time", "open", "high", "low", "close", "volume"]
        )

    df = candles[key]

    if len(df) > 0 and df.iloc[-1]["time"] == ts:
        df.iloc[-1] = [ts, open_price, high_price, low_price, close_price, volume]
    else:
        new_row = {
            "time": ts,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }
        candles[key] = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

    return candles[key]

# ============================================================
# SIGNAL GENERATION (EMA200, RSI, LARGE CANDLE, CONFLUENCE)
# ============================================================

def analyze_signals(symbol: str, tf: str, df: pd.DataFrame):
    if len(df) < max(EMA_PERIOD, RSI_PERIOD + 1):
        return None

    close = df["close"]
    ema_series = calc_ema(close, EMA_PERIOD)
    rsi_series = calc_rsi(close, RSI_PERIOD)

    df["ema200"] = ema_series
    df["rsi"] = rsi_series

    last = df.iloc[-1]
    price = last["close"]
    ema200 = last["ema200"]
    rsi = last["rsi"]

    ema_signal = None
    ema_info = None
    if ema200 > 0:
        deviation = (price - ema200) / ema200  # e.g. 0.70 = +70%
        if deviation >= EMA_DEVIATION:
            ema_signal = "ABOVE"
            ema_info = (price, ema200, deviation * 100.0)
        elif deviation <= -EMA_DEVIATION:
            ema_signal = "BELOW"
            ema_info = (price, ema200, deviation * 100.0)

    rsi_signal = None
    if rsi >= RSI_OVERBOUGHT:
        rsi_signal = "OVERBOUGHT"
    elif rsi <= RSI_OVERSOLD:
        rsi_signal = "OVERSOLD"

    lc_ratio = detect_large_candle(df)
    lc_signal = None
    if lc_ratio is not None:
        if lc_ratio >= LARGE_CANDLE_STRONG_RATIO:
            lc_signal = "LARGE_STRONG"
        else:
            lc_signal = "LARGE"

    return {
        "ema_signal": ema_signal,
        "ema_info": ema_info,
        "rsi_signal": rsi_signal,
        "rsi_value": rsi,
        "lc_signal": lc_signal,
        "lc_ratio": lc_ratio,
    }


def build_alert_message(symbol: str, tf: str, sig: dict):
    conditions = []

    # EMA
    if sig["ema_signal"] is not None:
        price, ema200, dev_pct = sig["ema_info"]
        direction = "ABOVE" if sig["ema_signal"] == "ABOVE" else "BELOW"
        conditions.append(
            f"EMA200 Deviation {direction} | Price: {price:.4f} | EMA200: {ema200:.4f} | Deviation: {dev_pct:+.1f}%"
        )

    # RSI
    if sig["rsi_signal"] is not None:
        if sig["rsi_signal"] == "OVERBOUGHT":
            conditions.append(f"RSI Extreme OVERBOUGHT | RSI: {sig['rsi_value']:.1f}")
        else:
            conditions.append(f"RSI Extreme OVERSOLD | RSI: {sig['rsi_value']:.1f}")

    # Large candle
    if sig["lc_signal"] is not None:
        if sig["lc_signal"] == "LARGE_STRONG":
            conditions.append(
                f"Large Candle STRONG | Ratio: {sig['lc_ratio']:.1f}x (>= {LARGE_CANDLE_STRONG_RATIO}x)"
            )
        else:
            conditions.append(
                f"Large Candle | Ratio: {sig['lc_ratio']:.1f}x (>= {LARGE_CANDLE_MIN_RATIO}x)"
            )

    active_count = len(conditions)
    if active_count == 0:
        return None

    if active_count == 1:
        header = f"📌 SINGLE CONDITION | {symbol} {tf}"
    elif active_count == 2:
        header = f"⚡ CONFLUENCE (2/3) | {symbol} {tf}"
    else:
        header = f"🔥 STRONG CONFLUENCE (3/3) | {symbol} {tf}"

    body = "\n".join(conditions)
    return f"{header}\n{body}"

# ============================================================
# WEBSOCKET CALLBACKS
# ============================================================

SYMBOLS = []  # will be filled at runtime

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
    # topic format: kline.<interval>.<symbol>
    if len(parts) != 3:
        return

    _, tf, symbol = parts
    tf_label = TIMEFRAMES.get(tf, tf)

    for kline in data["data"]:
        if not kline.get("confirm", False):
            continue  # only closed candles

        df = update_candles(symbol, tf, kline)
        sig = analyze_signals(symbol, tf_label, df)
        if sig is None:
            continue

        msg = build_alert_message(symbol, tf_label, sig)
        if msg:
            log_and_alert(msg)


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
    sub_msg = {"op": "subscribe", "args": args}
    ws.send(json.dumps(sub_msg))
    print("SUBSCRIBED TO:", args)

# ============================================================
# WEBSOCKET RUNNER
# ============================================================

def start_ws():
    print("STARTING WEBSOCKET...")

    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.on_open = on_open

    wst = threading.Thread(
        target=ws.run_forever,
        kwargs={"ping_interval": 20, "ping_timeout": 10}
    )
    wst.daemon = True
    wst.start()

    print("WEBSOCKET THREAD STARTED")

    while True:
        time.sleep(1)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("BOT STARTING...")

    # Apply initial mode (LIVE by default)
    apply_mode()

    # Fetch top 50 linear USDT symbols
    SYMBOLS = fetch_top_50_linear_usdt()
    if not SYMBOLS:
        print("NO SYMBOLS FETCHED — EXITING")
        exit(1)

    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"

    send_telegram(
        "🚀 DERIVATIVES SCANNER RUNNING\n"
        f"Mode: {mode}\n"
        f"Top 50 Bybit USDT-Perp by volume\n"
        f"Timeframes: {', '.join(TIMEFRAMES.values())}\n"
        f"EMA200 deviation: {EMA_DEVIATION*100:.2f}%\n"
        f"RSI extremes: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
        f"Large candle: ≥{LARGE_CANDLE_MIN_RATIO}x (strong ≥{LARGE_CANDLE_STRONG_RATIO}x)"
    )

    # Start Telegram command listener
    threading.Thread(target=telegram_command_listener, daemon=True).start()

    while True:
        try:
            start_ws()
        except Exception as e:
            print("MAIN LOOP ERROR:", e)
            time.sleep(5)
