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
TOP_N_COINS = 100

RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
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
# HELPERS
# =========================
def now_utc_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def send_alert(msg):
    try:
        session.get(
            f"{TELEGRAM_URL}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except:
        pass

# =========================
# DATA
# =========================
def get_ohlc(symbol, interval):
    r = session.get(
        BYBIT_KLINE_URL,
        params={"category": "linear", "symbol": symbol, "interval": interval, "limit": 200},
    )
    rows = r.json()["result"]["list"]
    rows.reverse()

    df = pd.DataFrame(rows, columns=["time","open","high","low","close","volume","turnover"])
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    return df.dropna()

# =========================
# INDICATORS
# =========================
def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1/period).mean()
    avg_loss = loss.ewm(alpha=1/period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_atr(df, period=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def add_indicators(df):
    df["EMA200"] = df["close"].ewm(span=200).mean()
    df["RSI"] = calculate_rsi(df["close"])
    df["body"] = (df["close"] - df["open"]).abs()
    df["avg_body"] = df["body"].rolling(20).mean()
    return df

# =========================
# STRATEGY
# =========================
def generate_signal(symbol):
    df5 = market_data[symbol]["5"]
    df1h = market_data[symbol]["60"]

    if df5 is None or df1h is None:
        return None

    df5 = add_indicators(df5.copy())
    df1h = add_indicators(df1h.copy())

    if len(df5) < 3:
        return None

    rsi = df5["RSI"].iloc[-2]
    prev = df5["RSI"].iloc[-3]

    price_1h = df1h["close"].iloc[-1]
    ema_1h = df1h["EMA200"].iloc[-1]

    trend_up = price_1h > ema_1h
    trend_down = price_1h < ema_1h

    body = df5["body"].iloc[-1]
    avg_body = df5["avg_body"].iloc[-2]

    momentum = avg_body > 0 and body > avg_body

    if trend_up and prev < RSI_OVERSOLD and rsi > RSI_OVERSOLD and momentum:
        return "BUY"

    if trend_down and prev > RSI_OVERBOUGHT and rsi < RSI_OVERBOUGHT and momentum:
        return "SELL"

    return None

# =========================
# PAPER TRADING
# =========================
def open_trade(symbol, side, price):
    df = market_data[symbol]["5"]
    df = add_indicators(df.copy())

    atr = calculate_atr(df).iloc[-1]

    if side == "BUY":
        sl = price - atr * 0.5
        tp = price + atr * 1.0
    else:
        sl = price + atr * 0.5
        tp = price - atr * 1.0

    trade = {
        "symbol": symbol,
        "side": side,
        "entry": price,
        "sl": sl,
        "tp": tp,
    }

    paper_trades["open"].append(trade)

    send_alert(f"{side} {symbol} @ {price:.4f}")

def update_trades(symbol, price):
    for t in paper_trades["open"][:]:
        if t["symbol"] != symbol:
            continue

        if t["side"] == "BUY":
            if price <= t["sl"] or price >= t["tp"]:
                paper_trades["open"].remove(t)
        else:
            if price >= t["sl"] or price <= t["tp"]:
                paper_trades["open"].remove(t)

# =========================
# MAIN LOOP (SIMPLIFIED)
# =========================
def run():
    global coins

    coins = ["BTCUSDT", "ETHUSDT"]

    while True:
        for sym in coins:
            for tf in TIMEFRAMES:
                df = get_ohlc(sym, tf)
                market_data[sym][tf] = df

            signal = generate_signal(sym)
            price = market_data[sym]["5"]["close"].iloc[-1]

            if signal:
                open_trade(sym, signal, price)

            update_trades(sym, price)

        time.sleep(30)

# =========================
# START
# =========================
send_alert("🚀 Bot started (5m + 1h confluence)")
run()
