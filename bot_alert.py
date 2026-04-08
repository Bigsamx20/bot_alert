# =========================
# 🚀 REAL-TIME TEST BOT (NO DELAY)
# =========================

import json
import time
import threading
import requests
import pandas as pd
from collections import defaultdict
import websocket
import random

# =========================
# 🔴 PUT YOUR DETAILS HERE
# =========================
TOKEN = "PASTE_YOUR_TOKEN"
CHAT_ID = "PASTE_YOUR_CHAT_ID"

# =========================
# CONFIG
# =========================
WS_URL = "wss://stream.bybit.com/v5/public/linear"

SYMBOLS = ["BTCUSDT"]
TIMEFRAMES = ["1"]  # FAST TEST

ACCOUNT_SIZE = 100
LEVERAGE = 10

TP_PERCENT = 0.05
SL_PERCENT = 0.02

# =========================
# STATE
# =========================
market_data = defaultdict(lambda: defaultdict(pd.DataFrame))
open_trades = []

# =========================
# TELEGRAM
# =========================
def send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        print("Telegram error:", e)

# =========================
# POSITION
# =========================
def position_size():
    return ACCOUNT_SIZE * LEVERAGE

def get_tp_sl(entry, side):
    if side == "BUY":
        return entry * 1.05, entry * 0.98
    else:
        return entry * 0.95, entry * 1.02

# =========================
# TRADES
# =========================
def open_trade(symbol, side, price):
    tp, sl = get_tp_sl(price, side)

    trade = {
        "symbol": symbol,
        "side": side,
        "entry": price,
        "tp": tp,
        "sl": sl,
        "status": "OPEN"
    }

    open_trades.append(trade)

    send(f"📝 OPEN {side} {symbol}\nEntry: {price:.2f}\nTP: {tp:.2f}\nSL: {sl:.2f}")

def check_trades(price):
    for trade in open_trades[:]:
        side = trade["side"]

        if side == "BUY":
            if price >= trade["tp"]:
                pnl = TP_PERCENT * position_size()
                close_trade(trade, price, pnl, "TP")
            elif price <= trade["sl"]:
                pnl = -SL_PERCENT * position_size()
                close_trade(trade, price, pnl, "SL")

        else:
            if price <= trade["tp"]:
                pnl = TP_PERCENT * position_size()
                close_trade(trade, price, pnl, "TP")
            elif price >= trade["sl"]:
                pnl = -SL_PERCENT * position_size()
                close_trade(trade, price, pnl, "SL")

def close_trade(trade, price, pnl, reason):
    send(f"✅ CLOSED {trade['symbol']} {trade['side']}\nExit: {price:.2f}\nPnL: ${pnl:.2f}\n{reason}")
    open_trades.remove(trade)

# =========================
# WEBSOCKET
# =========================
def on_message(ws, message):
    print("DATA RECEIVED")

    msg = json.loads(message)
    topic = msg.get("topic")

    if not topic:
        return

    parts = topic.split(".")
    tf = parts[1]
    symbol = parts[2]

    candle = msg["data"][0]

    price = float(candle["close"])

    # 🚀 LIVE UPDATE MESSAGE
    send(f"📡 {symbol} {tf} PRICE: {price:.2f}")

    # 🚀 FORCE TRADE EVERY MESSAGE
    signal = random.choice(["BUY", "SELL"])
    open_trade(symbol, signal, price)

    check_trades(price)

# =========================
# START WS
# =========================
def start_ws():
    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message
    )

    def on_open(ws):
        args = []
        for s in SYMBOLS:
            for tf in TIMEFRAMES:
                args.append(f"kline.{tf}.{s}")

        ws.send(json.dumps({"op": "subscribe", "args": args}))
        print("WS CONNECTED")

    ws.on_open = on_open
    ws.run_forever()

# =========================
# START BOT
# =========================
print("BOT STARTING...")
send("🚀 TEST BOT RUNNING (FAST MODE)")

threading.Thread(target=start_ws).start()

while True:
    time.sleep(60)
