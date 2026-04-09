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
import threading

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
# STEP 3B — TELEGRAM COMMAND LISTENER (/test)
# ============================================================

def telegram_command_listener():
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

                # --- TEST COMMAND ---
                if text.strip() == "/test":
                    send_telegram("🧪 TEST COMMAND RECEIVED — Bot is working!")

        except Exception as e:
            print("TELEGRAM LISTENER ERROR:", e)
            time.sleep(2)

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
    ema_s
