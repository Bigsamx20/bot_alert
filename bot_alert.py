# ============================================================
# STEP 1 — IMPORTS & ENVIRONMENT VARIABLES
# ============================================================

import os
import json
import time
import requests
import websocket
import pandas as pd

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

print("TOKEN LOADED:", TOKEN is not None)
print("CHAT_ID LOADED:", CHAT_ID is not None)

# ============================================================
# STEP 2 — TELEGRAM SEND FUNCTION
# ============================================================

def send(msg):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg}
        r = requests.post(url, json=payload)
        print("TELEGRAM RESPONSE:", r.text)
    except Exception as e:
        print("TELEGRAM ERROR:", e)

send("🚀 BOT RUNNING (MULTI-STRATEGY)")  # Startup message

# ============================================================
# STEP 3 — WEBSOCKET CALLBACKS
# ============================================================

def on_message(ws, message):
    print("WS MESSAGE RECEIVED")
    try:
        data = json.loads(message)
        # You can add your strategy logic here
    except Exception as e:
        print("MESSAGE ERROR:", e)

def on_error(ws, error):
    print("WS ERROR:", error)

def on_close(ws, code, msg):
    print("WS CLOSED:", code, msg)

def on_open(ws):
    print("WS CONNECTED")
    args = ["kline.1m.BTCUSDT", "kline.5m.BTCUSDT"]
    ws.send(json.dumps({"op": "subscribe", "args": args}))
    print("SUBSCRIBED TO:", args)

# ============================================================
# STEP 4 — START WEBSOCKET
# ============================================================

def start_ws():
    print("STARTING WEBSOCKET...")
    ws = websocket.WebSocketApp(
        "wss://stream.bybit.com/v5/public/spot",
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.on_open = on_open
    ws.run_forever()

# ============================================================
# STEP 5 — MAIN LOOP
# ============================================================

if __name__ == "__main__":
    print("BOT STARTING MAIN LOOP...")
    while True:
        try:
            start_ws()
        except Exception as e:
            print("MAIN LOOP ERROR:", e)
            time.sleep(5)
