print("🔥 BOT STARTED")

import os
import json
import time
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

TIMEFRAMES = {
    "1": "1m",
    "5": "5m",
    "15": "15m",
    "60": "1h",
}

EMA_PERIOD = 200
RSI_PERIOD = 14
HISTORY_LIMIT = 250
TOP_SYMBOLS_COUNT = 50

TESTING_MODE = False

# ============================================================
# LIVE SETTINGS (UPDATED HERE)
# ============================================================

REAL_EMA_DEVIATION = 0.10   # 🔥 10%
REAL_RSI_OVERBOUGHT = 95
REAL_RSI_OVERSOLD = 5
REAL_LARGE_CANDLE_MIN = 12.0
REAL_LARGE_CANDLE_STRONG = 15.0

# ============================================================
# TEST SETTINGS
# ============================================================

TEST_EMA_DEVIATION = 0.01
TEST_RSI_OVERBOUGHT = 51
TEST_RSI_OVERSOLD = 49
TEST_LARGE_CANDLE_MIN = 1.2
TEST_LARGE_CANDLE_STRONG = 1.5

EMA_DEVIATION = REAL_EMA_DEVIATION
RSI_OVERBOUGHT = REAL_RSI_OVERBOUGHT
RSI_OVERSOLD = REAL_RSI_OVERSOLD
LARGE_CANDLE_MIN_RATIO = REAL_LARGE_CANDLE_MIN
LARGE_CANDLE_STRONG_RATIO = REAL_LARGE_CANDLE_STRONG

# ============================================================
# GLOBALS
# ============================================================

candles = {}
last_alerted_candle = {}
SYMBOLS = []
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
# TELEGRAM
# ============================================================

def send_telegram(msg: str, chat_id=None):
    target = chat_id if chat_id is not None else CHAT_ID

    if not TOKEN or not target:
        print("TELEGRAM SKIPPED")
        return

    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": str(target), "text": msg}, timeout=10)
    except Exception as e:
        print("TELEGRAM ERROR:", e)

def log_and_alert(msg: str):
    print(msg)
    send_telegram(msg)

# ============================================================
# TELEGRAM LISTENER
# ============================================================

def telegram_command_listener():
    if not TOKEN:
        return

    global TESTING_MODE
    last_update_id = None

    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            params = {"timeout": 20, "offset": last_update_id}
            data = requests.get(url, params=params, timeout=30).json()

            for update in data.get("result", []):
                last_update_id = update["update_id"] + 1

                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")

                if text == "/test":
                    send_telegram("🧪 Bot working!", chat_id)

                elif text == "/testmode_on":
                    TESTING_MODE = True
                    apply_mode()
                    send_telegram("🧪 TEST MODE ON", chat_id)

                elif text == "/testmode_off":
                    TESTING_MODE = False
                    apply_mode()
                    send_telegram("✅ LIVE MODE", chat_id)

        except:
            time.sleep(2)

# ============================================================
# BYBIT DATA
# ============================================================

def fetch_top_symbols():
    r = requests.get(f"{BYBIT_REST}/v5/market/tickers", params={"category": "linear"})
    data = r.json()["result"]["list"]

    data = [d for d in data if d["symbol"].endswith("USDT")]
    data.sort(key=lambda x: float(x["turnover24h"]), reverse=True)

    return [d["symbol"] for d in data[:TOP_SYMBOLS_COUNT]]

def fetch_history(symbol, tf):
    r = requests.get(f"{BYBIT_REST}/v5/market/kline", params={
        "category": "linear",
        "symbol": symbol,
        "interval": tf,
        "limit": HISTORY_LIMIT
    })
    rows = r.json()["result"]["list"]
    rows.reverse()

    df = pd.DataFrame([{
        "time": datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc),
        "open": float(r[1]),
        "high": float(r[2]),
        "low": float(r[3]),
        "close": float(r[4]),
        "volume": float(r[5]),
    } for r in rows])

    return df

def bootstrap():
    for s in SYMBOLS:
        for tf in TIMEFRAMES:
            candles[(s, tf)] = fetch_history(s, tf)

# ============================================================
# INDICATORS
# ============================================================

def ema(series, period):
    return series.ewm(span=period).mean()

def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    rs = gain.rolling(period).mean() / (loss.rolling(period).mean() + 1e-9)
    return 100 - (100 / (1 + rs))

# ============================================================
# SIGNALS
# ============================================================

def analyze(symbol, tf_label, df):
    if len(df) < EMA_PERIOD:
        return None

    df["ema"] = ema(df["close"], EMA_PERIOD)
    df["rsi"] = rsi(df["close"], RSI_PERIOD)

    last = df.iloc[-1]

    price = last["close"]
    ema200 = last["ema"]

    deviation = (price - ema200) / ema200

    if deviation >= EMA_DEVIATION:
        return f"📌 {symbol} {tf_label}\nABOVE EMA200 {deviation*100:.2f}%"

    if deviation <= -EMA_DEVIATION:
        return f"📌 {symbol} {tf_label}\nBELOW EMA200 {deviation*100:.2f}%"

    return None

# ============================================================
# WS
# ============================================================

def on_message(ws, msg):
    data = json.loads(msg)

    if "topic" not in data:
        return

    _, tf, symbol = data["topic"].split(".")

    for k in data["data"]:
        if not k["confirm"]:
            continue

        df = candles[(symbol, tf)]
        new = {
            "time": datetime.fromtimestamp(int(k["start"]) / 1000, tz=timezone.utc),
            "open": float(k["open"]),
            "high": float(k["high"]),
            "low": float(k["low"]),
            "close": float(k["close"]),
            "volume": float(k["volume"]),
        }

        df = pd.concat([df, pd.DataFrame([new])]).tail(HISTORY_LIMIT)
        candles[(symbol, tf)] = df

        msg = analyze(symbol, TIMEFRAMES[tf], df)
        if msg:
            log_and_alert(msg)

def ws_loop():
    while True:
        ws = websocket.WebSocketApp(
            WS_URL,
            on_message=on_message
        )

        def on_open(ws):
            args = [f"kline.{tf}.{s}" for s in SYMBOLS for tf in TIMEFRAMES]
            ws.send(json.dumps({"op": "subscribe", "args": args}))

        ws.on_open = on_open
        ws.run_forever()
        time.sleep(5)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    apply_mode()

    SYMBOLS = fetch_top_symbols()
    bootstrap()

    send_telegram("🚀 BOT RUNNING (EMA200 10%)")

    threading.Thread(target=telegram_command_listener, daemon=True).start()
    ws_loop()
