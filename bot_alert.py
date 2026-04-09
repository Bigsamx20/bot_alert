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

# Indicators
EMA_PERIOD = 200
EMA_DEVIATION = 0.70  # 70% above/below EMA200

RSI_PERIOD = 14
RSI_OVERBOUGHT = 95
RSI_OVERSOLD = 5

LARGE_CANDLE_MIN_RATIO = 12.0
LARGE_CANDLE_STRONG_RATIO = 15.0

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
# TELEGRAM COMMAND LISTENER (/test)
# ============================================================

def telegram_command_listener():
    if not TOKEN:
        print("TELEGRAM LISTENER DISABLED: No TOKEN")
        return

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

                if text.strip() == "/test":
                    send_telegram("🧪 TEST COMMAND RECEIVED — Bot is working!", chat_id)

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

        # Sort by 24h turnover (or volume)
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
        print("FETCH TOP 50 ERROR
