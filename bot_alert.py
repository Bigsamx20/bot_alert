# =========================
# 🚀 FINAL FULL BOT (WEBSOCKET + 2/3 CONFLUENCE)
# =========================

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
import requests
import websocket

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise SystemExit("Missing TOKEN or CHAT_ID environment variables.")

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
BYBIT_WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"

# =========================
# SETTINGS
# =========================
TIMEFRAMES = ["5", "60"]
TOP_N_COINS = 50

RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
RSI_PERIOD = 14

PAPER_TRADES_FILE = "paper_trades.json"

# =========================
# STATE
# =========================
session = requests.Session()
data_lock = threading.Lock()

coins = []
market_data = defaultdict(lambda: defaultdict(dict))
paper_trades = {"open": [], "closed": []}

# =========================
# UTILS
# =========================
def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def send(msg):
    try:
        session.get(
            f"{TELEGRAM_URL}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except:
        pass

# =========================
# DATA FETCH
# =========================
def get_ohlc(symbol, tf):
    try:
        r = session.get(BYBIT_KLINE_URL, params={
            "category": "linear",
            "symbol": symbol,
            "interval": tf,
            "limit": 200
        }, timeout=10)

        rows = r.json()["result"]["list"]
        rows.reverse()

        df = pd.DataFrame(rows, columns=["time","open","high","low","close","volume","turnover"])

        for c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        return df.dropna()
    except:
        return None

# =========================
# INDICATORS
# =========================
def rsi(series):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/RSI_PERIOD).mean()
    avg_loss = loss.ewm(alpha=1/RSI_PERIOD).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(df):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(14).mean()

def add_indicators(df):
    df["EMA200"] = df["close"].ewm(span=200).mean()
    df["RSI"] = rsi(df["close"])
    df["body"] = (df["close"] - df["open"]).abs()
    df["avg_body"] = df["body"].rolling(20).mean()
    return df

# =========================
# SIGNAL (2/3 CONFLUENCE)
# =========================
def get_signal(symbol):
    df = market_data[symbol]["5"]

    if df is None or len(df) < 50:
        return None

    df = add_indicators(df.copy())

    signals = []

    price = df["close"].iloc[-1]
    ema = df["EMA200"].iloc[-1]

    # EMA
    signals.append("BUY" if price > ema else "SELL")

    # RSI
    r = df["RSI"].iloc[-2]
    if r < RSI_OVERSOLD:
        signals.append("BUY")
    elif r > RSI_OVERBOUGHT:
        signals.append("SELL")

    # Candle
    body = df["body"].iloc[-1]
    avg = df["avg_body"].iloc[-2]

    if avg > 0 and body > avg:
        if df["close"].iloc[-1] > df["open"].iloc[-1]:
            signals.append("BUY")
        else:
            signals.append("SELL")

    if signals.count("BUY") >= 2:
        return "BUY"
    if signals.count("SELL") >= 2:
        return "SELL"

    return None

# =========================
# PAPER TRADING
# =========================
def open_trade(symbol, side, price):
    df = add_indicators(market_data[symbol]["5"].copy())
    a = atr(df).iloc[-1]

    sl = price - a*0.5 if side == "BUY" else price + a*0.5
    tp = price + a*1.0 if side == "BUY" else price - a*1.0

    trade = {
        "symbol": symbol,
        "side": side,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "time": now()
    }

    paper_trades["open"].append(trade)

    send(f"📝 {side} {symbol}\nEntry: {price:.4f}\nSL: {sl:.4f}\nTP: {tp:.4f}")

def update_trades(symbol, price):
    for t in paper_trades["open"][:]:
        if t["symbol"] != symbol:
            continue

        if t["side"] == "BUY":
            if price <= t["sl"] or price >= t["tp"]:
                paper_trades["open"].remove(t)
                send(f"✅ CLOSED {symbol}")
        else:
            if price >= t["sl"] or price <= t["tp"]:
                paper_trades["open"].remove(t)
                send(f"✅ CLOSED {symbol}")

# =========================
# UNIVERSE
# =========================
def get_top_coins():
    r = session.get(BYBIT_TICKERS_URL, params={"category": "linear"})
    data = r.json()["result"]["list"]

    ranked = sorted(data, key=lambda x: float(x["turnover24h"]), reverse=True)
    return [c["symbol"] for c in ranked[:TOP_N_COINS]]

# =========================
# WEBSOCKET
# =========================
def on_message(ws, message):
    msg = json.loads(message)
    topic = msg.get("topic")

    if not topic or "kline" not in topic:
        return

    parts = topic.split(".")
    tf = parts[1]
    symbol = parts[2]

    candle = msg["data"][0]

    row = {
        "time": int(candle["start"]),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": float(candle["volume"]),
        "turnover": float(candle["turnover"]),
    }

    with data_lock:
        df = market_data[symbol][tf]

        if df is None or len(df) == 0:
            return

        if candle["confirm"]:
            df = pd.concat([df, pd.DataFrame([row])]).tail(200)
            market_data[symbol][tf] = df

            # === ALERTS (independent) ===
            df_ind = add_indicators(df.copy())

            price = df_ind["close"].iloc[-1]
            ema = df_ind["EMA200"].iloc[-1]
            rsi_val = df_ind["RSI"].iloc[-2]

            # EMA alert
            if abs((price - ema)/ema*100) > 2:
                send(f"EMA ALERT {symbol} {tf}")

            # RSI alert
            if rsi_val < RSI_OVERSOLD:
                send(f"RSI OVERSOLD {symbol} {tf}")
            elif rsi_val > RSI_OVERBOUGHT:
                send(f"RSI OVERBOUGHT {symbol} {tf}")

            # Candle alert
            body = df_ind["body"].iloc[-1]
            avg = df_ind["avg_body"].iloc[-2]
            if avg > 0 and body > avg * 2:
                send(f"GIANT CANDLE {symbol} {tf}")

            # === TRADING (ONLY 5m) ===
            if tf == "5":
                signal = get_signal(symbol)

                if signal:
                    open_trade(symbol, signal, price)

                update_trades(symbol, price)

def start_ws():
    ws = websocket.WebSocketApp(
        BYBIT_WS_LINEAR,
        on_message=on_message
    )

    def on_open(ws):
        topics = []
        for c in coins:
            for tf in TIMEFRAMES:
                topics.append(f"kline.{tf}.{c}")

        ws.send(json.dumps({"op": "subscribe", "args": topics}))

    ws.on_open = on_open
    ws.run_forever()

# =========================
# START
# =========================
coins = get_top_coins()

# preload data
for c in coins:
    for tf in TIMEFRAMES:
        market_data[c][tf] = get_ohlc(c, tf)

send(f"🚀 FULL BOT RUNNING\nCoins: {len(coins)}\nMode: 2/3 Confluence")

threading.Thread(target=start_ws).start()

while True:
    time.sleep(60)
