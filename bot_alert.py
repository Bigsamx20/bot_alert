# =========================
# 🚀 PAPER TRADING BOT (FIXED + DEBUG)
# =========================

import json
import time
import threading
import requests
import pandas as pd
from collections import defaultdict
import websocket

# =========================
# 🔴 INSERT YOUR DETAILS HERE
# =========================
TOKEN = "8276758800:AAFGXPI4q4xsZgAbpDqq_PDEsCYu94jaVXs"
CHAT_ID = "6903033357"

# =========================
# CONFIG
# =========================
WS_URL = "wss://stream.bybit.com/v5/public/linear"

SYMBOLS = ["BTCUSDT"]
TIMEFRAMES = ["5", "60"]

ACCOUNT_SIZE = 100
LEVERAGE = 10

TP_PERCENT = 0.05
SL_PERCENT = 0.02

RSI_OB = 70
RSI_OS = 30

# =========================
# STATE
# =========================
market_data = defaultdict(lambda: defaultdict(pd.DataFrame))
open_trades = []

# =========================
# TELEGRAM (WITH DEBUG)
# =========================
def send(msg):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg}
        )
        print("Telegram:", r.text)
    except Exception as e:
        print("Telegram error:", e)

# =========================
# INDICATORS
# =========================
def rsi(series):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14).mean()
    avg_loss = loss.ewm(alpha=1/14).mean()
    rs = avg_gain / avg_loss
    return 100 - (100/(1+rs))

def add_indicators(df):
    df["EMA"] = df["close"].ewm(span=200).mean()
    df["RSI"] = rsi(df["close"])
    df["body"] = (df["close"] - df["open"]).abs()
    df["avg_body"] = df["body"].rolling(20).mean()
    df["vol_avg"] = df["volume"].rolling(20).mean()
    return df

# =========================
# POSITION LOGIC
# =========================
def calc_position_value():
    return ACCOUNT_SIZE * LEVERAGE

def get_tp_sl(entry, side):
    if side == "BUY":
        tp = entry * (1 + TP_PERCENT)
        sl = entry * (1 - SL_PERCENT)
    else:
        tp = entry * (1 - TP_PERCENT)
        sl = entry * (1 + SL_PERCENT)
    return tp, sl

# =========================
# SIGNAL (2/3)
# =========================
def get_signal(df):
    df = add_indicators(df.copy())

    signals = []

    price = df["close"].iloc[-1]
    ema = df["EMA"].iloc[-1]

    signals.append("BUY" if price > ema else "SELL")

    r = df["RSI"].iloc[-2]
    if r < RSI_OS:
        signals.append("BUY")
    elif r > RSI_OB:
        signals.append("SELL")

    body = df["body"].iloc[-1]
    avg = df["avg_body"].iloc[-2]

    if avg > 0 and body > avg:
        if df["close"].iloc[-1] > df["open"].iloc[-1]:
            signals.append("BUY")
        else:
            signals.append("SELL")

    vol = df["volume"].iloc[-1]
    vol_avg = df["vol_avg"].iloc[-1]

    if vol < vol_avg:
        return None

    if signals.count("BUY") >= 2:
        return "BUY"
    if signals.count("SELL") >= 2:
        return "SELL"

    return None

# =========================
# PAPER TRADING
# =========================
def open_trade(symbol, side, entry):
    position = calc_position_value()
    tp, sl = get_tp_sl(entry, side)

    trade = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "position": position,
        "status": "OPEN"
    }

    open_trades.append(trade)

    send(
        f"📝 OPEN\n{side} {symbol}\nEntry: {entry:.2f}\nTP: {tp:.2f}\nSL: {sl:.2f}"
    )

def check_trades(price):
    for trade in open_trades[:]:
        side = trade["side"]

        if side == "BUY":
            if price >= trade["tp"]:
                pnl = TP_PERCENT * trade["position"]
                close_trade(trade, price, pnl, "TP")
            elif price <= trade["sl"]:
                pnl = -(SL_PERCENT * trade["position"])
                close_trade(trade, price, pnl, "SL")
        else:
            if price <= trade["tp"]:
                pnl = TP_PERCENT * trade["position"]
                close_trade(trade, price, pnl, "TP")
            elif price >= trade["sl"]:
                pnl = -(SL_PERCENT * trade["position"])
                close_trade(trade, price, pnl, "SL")

def close_trade(trade, price, pnl, reason):
    send(
        f"✅ CLOSED {trade['symbol']}\n"
        f"{trade['side']}\nExit: {price:.2f}\nPnL: ${pnl:.2f}\nReason: {reason}"
    )
    open_trades.remove(trade)

# =========================
# WEBSOCKET
# =========================
def on_message(ws, message):
    print("WS MESSAGE RECEIVED")

    msg = json.loads(message)
    topic = msg.get("topic")

    if not topic:
        return

    parts = topic.split(".")
    tf = parts[1]
    symbol = parts[2]

    candle = msg["data"][0]

    row = {
        "open": float(candle["open"]),
        "close": float(candle["close"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "volume": float(candle["volume"]),
    }

    df = market_data[symbol][tf]

    if df is None or df.empty:
        market_data[symbol][tf] = pd.DataFrame([row])
        return

    if candle["confirm"]:
        df = pd.concat([df, pd.DataFrame([row])]).tail(200)
        market_data[symbol][tf] = df

        df_ind = add_indicators(df.copy())
        price = df_ind["close"].iloc[-1]

        r = df_ind["RSI"].iloc[-2]
        if r < RSI_OS:
            send(f"RSI OVERSOLD {symbol} {tf}")
        elif r > RSI_OB:
            send(f"RSI OVERBOUGHT {symbol} {tf}")

        if tf == "5":
            signal = get_signal(df)
            if signal:
                open_trade(symbol, signal, price)

        check_trades(price)

# =========================
# START
# =========================
def start_ws():
    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message
    )

    def on_open(ws):
        args = [f"kline.{tf}.{s}" for s in SYMBOLS for tf in TIMEFRAMES]
        ws.send(json.dumps({"op": "subscribe", "args": args}))

    ws.on_open = on_open
    ws.run_forever()

print("BOT STARTING...")
send("🚀 BOT STARTED")

threading.Thread(target=start_ws).start()

while True:
    time.sleep(60)
