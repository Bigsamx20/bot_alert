import json
import time
import threading
import requests
import pandas as pd
from collections import defaultdict
import websocket

# =========================
# 🔴 YOUR DETAILS
# =========================
TOKEN = "PASTE_YOUR_TOKEN"
CHAT_ID = "PASTE_YOUR_CHAT_ID"

# =========================
# CONFIG
# =========================
WS_URL = "wss://stream.bybit.com/v5/public/linear"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","ADAUSDT",
    "DOGEUSDT","AVAXUSDT","LINKUSDT","MATICUSDT","LTCUSDT"
]

TIMEFRAMES = ["5", "60"]

ACCOUNT_SIZE = 100
LEVERAGE = 10

TP_PERCENT = 0.05
SL_PERCENT = 0.02

EMA_DISTANCE_THRESHOLD = 0.05  # 5%

COOLDOWN = 60  # seconds between trades per symbol

# =========================
# STATE
# =========================
market_data = defaultdict(lambda: defaultdict(pd.DataFrame))
open_trades = []
last_trade_time = defaultdict(int)

# =========================
# TELEGRAM
# =========================
def send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg}
        )
    except:
        pass

# =========================
# INDICATORS
# =========================
def ema(df, period=200):
    return df["close"].ewm(span=period).mean()

def rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# =========================
# STRATEGIES
# =========================
def giant_candle(df):
    if len(df) < 20:
        return None

    last = df.iloc[-1]

    avg_size = (df["high"] - df["low"]).rolling(20).mean().iloc[-1]
    candle_size = last["high"] - last["low"]

    if candle_size > 12 * avg_size:  # 🔥 12x (you can push to 15x)
        if last["close"] > last["open"]:
            return "BUY"
        else:
            return "SELL"

    return None

def ema_distance(df):
    if len(df) < 200:
        return None

    ema200 = ema(df).iloc[-1]
    price = df["close"].iloc[-1]

    distance = (price - ema200) / ema200

    if distance > EMA_DISTANCE_THRESHOLD:
        return "SELL"
    elif distance < -EMA_DISTANCE_THRESHOLD:
        return "BUY"

    return None

def rsi_signal(df):
    if len(df) < 14:
        return None

    val = rsi(df).iloc[-1]

    if val >= 95:
        return "SELL"
    elif val <= 5:
        return "BUY"

    return None

# =========================
# CONFLUENCE
# =========================
def confluence(signals):
    signals = [s for s in signals if s]

    if len(signals) < 2:
        return None

    if signals.count("BUY") >= 2:
        return "BUY"
    if signals.count("SELL") >= 2:
        return "SELL"

    return None

# =========================
# TRADING (PAPER)
# =========================
def position_size():
    return ACCOUNT_SIZE * LEVERAGE

def get_tp_sl(entry, side):
    if side == "BUY":
        return entry * (1 + TP_PERCENT), entry * (1 - SL_PERCENT)
    else:
        return entry * (1 - TP_PERCENT), entry * (1 + SL_PERCENT)

def open_trade(symbol, side, price):
    if time.time() - last_trade_time[symbol] < COOLDOWN:
        return

    # only 1 trade per symbol
    for t in open_trades:
        if t["symbol"] == symbol:
            return

    tp, sl = get_tp_sl(price, side)

    trade = {
        "symbol": symbol,
        "side": side,
        "entry": price,
        "tp": tp,
        "sl": sl
    }

    open_trades.append(trade)
    last_trade_time[symbol] = time.time()

    send(f"📝 OPEN {side} {symbol}\nEntry: {price:.2f}\nTP: {tp:.2f}\nSL: {sl:.2f}")

def close_trade(trade, price, reason):
    pnl = (price - trade["entry"]) / trade["entry"] * position_size()

    if trade["side"] == "SELL":
        pnl *= -1

    send(f"✅ CLOSED {trade['symbol']} {trade['side']}\nExit: {price:.2f}\nPnL: ${pnl:.2f}\n{reason}")

    open_trades.remove(trade)

def check_trades(symbol, price):
    for trade in open_trades[:]:
        if trade["symbol"] != symbol:
            continue

        if trade["side"] == "BUY":
            if price >= trade["tp"]:
                close_trade(trade, price, "TP")
            elif price <= trade["sl"]:
                close_trade(trade, price, "SL")
        else:
            if price <= trade["tp"]:
                close_trade(trade, price, "TP")
            elif price >= trade["sl"]:
                close_trade(trade, price, "SL")

# =========================
# WEBSOCKET
# =========================
def on_message(ws, message):
    msg = json.loads(message)
    topic = msg.get("topic")

    if not topic:
        return

    parts = topic.split(".")
    tf = parts[1]
    symbol = parts[2]

    candle = msg["data"][0]

    if not candle["confirm"]:
        return

    price = float(candle["close"])

    df = market_data[symbol][tf]

    new_row = {
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": price,
        "volume": float(candle["volume"])
    }

    df = pd.concat([df, pd.DataFrame([new_row])]).tail(300)
    market_data[symbol][tf] = df

    # ===== STRATEGIES =====
    sig1 = giant_candle(df)
    sig2 = ema_distance(df)
    sig3 = rsi_signal(df)

    if sig1:
        send(f"🔥 Giant Candle {symbol} {tf}: {sig1}")
    if sig2:
        send(f"📏 EMA Distance {symbol}: {sig2}")
    if sig3:
        send(f"📊 RSI {symbol}: {sig3}")

    final_signal = confluence([sig1, sig2, sig3])

    if final_signal:
        send(f"🚀 CONFLUENCE {symbol} {tf}: {final_signal}")
        open_trade(symbol, final_signal, price)

    check_trades(symbol, price)

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
# RUN
# =========================
print("BOT STARTING...")
send("🚀 BOT RUNNING (MULTI-STRATEGY)")

threading.Thread(target=start_ws).start()

while True:
    time.sleep(60)
