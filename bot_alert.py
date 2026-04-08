# ============================================================
# STEP 1 — IMPORTS & ENVIRONMENT VARIABLES
# ============================================================

import os
import json
import time
import requests
import websocket
import pandas as pd
import numpy as np
from datetime import datetime

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

print("TOKEN LOADED:", TOKEN is not None)
print("CHAT_ID LOADED:", CHAT_ID is not None)

# ============================================================
# STEP 2 — CONFIGURATION
# ============================================================

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
TIMEFRAMES = ["1m", "5m", "15m"]

PRIMARY_TF = "1m"
CONFIRM_TF = "5m"

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

PAPER_BALANCE = 10_000.0
RISK_PER_TRADE = 0.01

WS_URL = "wss://stream.bybit.com/v5/public/spot"

candles = {}
positions = {}
paper_balance = PAPER_BALANCE
last_combo_signals = {}

# ============================================================
# STEP 3 — TELEGRAM UTILITIES
# ============================================================

def send_telegram(msg: str):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM SKIPPED: Missing TOKEN or CHAT_ID")
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg}
        r = requests.post(url, json=payload, timeout=10)
        print("TELEGRAM RESPONSE:", r.status_code, r.text)
    except Exception as e:
        print("TELEGRAM ERROR:", e)


def log_and_alert(msg: str):
    print(msg)
    send_telegram(msg)

# ============================================================
# STEP 4 — INDICATOR CALCULATIONS
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


def calc_macd(series: pd.Series, fast: int, slow: int, signal: int):
    ema_fast = calc_ema(series, fast)
    ema_slow = calc_ema(series, slow)
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - signal_line
    return macd, signal_line, hist

# ============================================================
# STEP 5 — SIGNAL GENERATION
# ============================================================

def generate_signals(df: pd.DataFrame):
    min_len = max(EMA_SLOW, RSI_PERIOD, MACD_SLOW + MACD_SIGNAL)
    if len(df) < min_len:
        return {"ema": None, "rsi": None, "macd": None, "combo": None}

    close = df["close"]

    df["ema_fast"] = calc_ema(close, EMA_FAST)
    df["ema_slow"] = calc_ema(close, EMA_SLOW)
    df["rsi"] = calc_rsi(close, RSI_PERIOD)
    df["macd"], df["macd_signal"], df["macd_hist"] = calc_macd(
        close, MACD_FAST, MACD_SLOW, MACD_SIGNAL
    )

    last = df.iloc[-1]

    ema_fast = last["ema_fast"]
    ema_slow = last["ema_slow"]
    rsi = last["rsi"]
    macd_val = last["macd"]
    macd_signal = last["macd_signal"]

    if ema_fast > ema_slow:
        ema_sig = "BUY"
    elif ema_fast < ema_slow:
        ema_sig = "SELL"
    else:
        ema_sig = None

    if rsi > 55:
        rsi_sig = "BUY"
    elif rsi < 45:
        rsi_sig = "SELL"
    else:
        rsi_sig = None

    if macd_val > macd_signal:
        macd_sig = "BUY"
    elif macd_val < macd_signal:
        macd_sig = "SELL"
    else:
        macd_sig = None

    if ema_sig == rsi_sig == macd_sig and ema_sig is not None:
        combo_sig = ema_sig
    else:
        combo_sig = None

    return {
        "ema": ema_sig,
        "rsi": rsi_sig,
        "macd": macd_sig,
        "combo": combo_sig,
    }

# ============================================================
# STEP 6 — LARGE CANDLE DETECTION
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

    if ratio >= 12:
        return ratio
    return None

# ============================================================
# STEP 7 — PAPER TRADING ENGINE
# ============================================================

def get_position(symbol: str):
    return positions.get(symbol)


def open_position(symbol: str, side: str, price: float):
    global paper_balance

    risk_amount = paper_balance * RISK_PER_TRADE
    if price <= 0:
        return

    size = risk_amount / price
    positions[symbol] = {"side": side, "entry": price, "size": size}
    log_and_alert(
        f"📈 PAPER {side} OPENED {symbol} | Entry: {price:.4f} | Size: {size:.6f} | Balance: {paper_balance:.2f}"
    )


def close_position(symbol: str, price: float):
    global paper_balance

    pos = positions.get(symbol)
    if not pos:
        return

    side = pos["side"]
    entry = pos["entry"]
    size = pos["size"]

    if side == "LONG":
        pnl = (price - entry) * size
    else:
        pnl = (entry - price) * size

    paper_balance += pnl
    log_and_alert(
        f"📉 PAPER {side} CLOSED {symbol} | Exit: {price:.4f} | PnL: {pnl:.2f} | New Balance: {paper_balance:.2f}"
    )

    del positions[symbol]


def execute_combo_trade(symbol: str, combo_signal: str, price: float):
    pos = get_position(symbol)

    if combo_signal == "BUY":
        if pos is None:
            open_position(symbol, "LONG", price)
        elif pos["side"] == "LONG":
            pass
        else:
            close_position(symbol, price)
            open_position(symbol, "LONG", price)

    elif combo_signal == "SELL":
        if pos is not None and pos["side"] == "LONG":
            close_position(symbol, price)

# ============================================================
# STEP 8 — MULTI-TIMEFRAME CONFLUENCE
# ============================================================

def set_last_combo(symbol: str, tf: str, combo: str | None):
    last_combo_signals[(symbol, tf)] = combo


def get_last_combo(symbol: str, tf: str):
    return last_combo_signals.get((symbol, tf))


def process_confluence(symbol: str, tf: str, combo_signal: str, price: float):
    log_and_alert(f"🔔 COMBO {combo_signal} | {symbol} {tf} | Price: {price:.4f}")

    if tf != PRIMARY_TF:
        return

    confirm_combo = get_last_combo(symbol, CONFIRM_TF)

    if confirm_combo == combo_signal:
        log_and_alert(
            f"✅ MULTI-TF CONFLUENCE | {symbol} {PRIMARY_TF}+{CONFIRM_TF} | {combo_signal} | Price: {price:.4f}"
        )
        execute_combo_trade(symbol, combo_signal, price)
    else:
        print(
            f"NO MULTI-TF CONFLUENCE | {symbol} {PRIMARY_TF}={combo_signal}, {CONFIRM_TF}={confirm_combo}"
        )

# ============================================================
# STEP 9 — CANDLE MANAGEMENT
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
# STEP 10 — WEBSOCKET CALLBACKS
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

    for kline in data["data"]:
        if kline.get("confirm") is not True:
            continue

        df = update_candles(symbol, tf, kline)

        ratio = detect_large_candle(df)
        if ratio:
            log_and_alert(
                f"🔥 LARGE CANDLE ({ratio:.1f}x) | {symbol} {tf} | "
                f"Range expanded massively vs previous candle."
            )

        signals = generate_signals(df)
        price = float(kline["close"])

        if signals["ema"]:
            log_and_alert(
                f"📊 EMA {signals['ema']} | {symbol} {tf} | Price: {price:.4f}"
            )
        if signals["rsi"]:
            log_and_alert(
                f"📊 RSI {signals['rsi']} | {symbol} {tf} | Price: {price:.4f}"
            )
        if signals["macd"]:
            log_and_alert(
                f"📊 MACD {signals['macd']} | {symbol} {tf} | Price: {price:.4f}"
            )

        combo = signals["combo"]
        set_last_combo(symbol, tf, combo)

        if combo:
            process_confluence(symbol, tf, combo, price)


def on_error(ws, error):
    print("WS ERROR:", error)


def on_close(ws, code, msg):
    print("WS CLOSED:", code, msg)


def on_open(ws):
    print("WS CONNECTED")
    args = []
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            args.append(f"kline.{tf}.{symbol}")
    sub_msg = {"op": "subscribe", "args": args}
    ws.send(json.dumps(sub_msg))
    print("SUBSCRIBED TO:", args)

# ============================================================
# STEP 11 — WEBSOCKET RUNNER (FINAL FIXED VERSION)
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

    import threading

    # Start WebSocket in background thread
    wst = threading.Thread(
        target=ws.run_forever,
        kwargs={"ping_interval": 20, "ping_timeout": 10}
    )
    wst.daemon = True
    wst.start()

    print("WEBSOCKET THREAD STARTED")

    # Keep main thread alive forever
    while True:
        time.sleep(1)

# ============================================================
# STEP 12 — MAIN LOOP
# ============================================================

if __name__ == "__main__":
    print("BOT STARTING MAIN LOOP...")
    send_telegram("🚀 BOT RUNNING (EMA + RSI + MACD + CONFLUENCE + LARGE CANDLE)")
    while True:
        try:
            start_ws()
        except Exception as e:
            print("MAIN LOOP ERROR:", e)
            time.sleep(5)
