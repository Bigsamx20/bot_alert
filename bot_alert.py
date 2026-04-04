import requests
import pandas as pd
import time
import os
import threading

# ----------------- Telegram Settings -----------------
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    print("Error: TOKEN or CHAT_ID is missing")
    exit()

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# ----------------- Load coins -----------------
coins = pd.read_csv("coins.csv")

if "bollinger_k" not in coins.columns:
    coins["bollinger_k"] = 2
if "band_expand" not in coins.columns:
    coins["band_expand"] = 50
if "band_shrink" not in coins.columns:
    coins["band_shrink"] = 10

timeframes = ["1", "5", "15", "60"]

# ----------------- Track alerts -----------------
last_alert = {
    coin: {tf: {"ema": None, "rsi": None, "bb": None} for tf in timeframes}
    for coin in coins["coin"]
}

# ----------------- Telegram -----------------
def send_alert(msg):
    try:
        requests.get(f"{BASE_URL}/sendMessage", params={
            "chat_id": CHAT_ID,
            "text": msg
        }, timeout=10)
    except:
        pass

# ----------------- Get Prices -----------------
def get_prices(symbol, interval):
    try:
        r = requests.get(f"{BASE_URL.replace('/bot'+TOKEN,'')}/v5/market/kline",
                         params={"category": "linear", "symbol": symbol, "interval": interval, "limit": 200})
        data = r.json()
        closes = [float(i[4]) for i in data["result"]["list"]]
        closes.reverse()
        return closes
    except:
        return None

# ----------------- Bollinger Check -----------------
def check_bollinger_width(coin, tf):
    prices = get_prices(coin, tf)
    if not prices:
        send_alert(f"{coin} {tf}m ❌ No data")
        return

    df = pd.DataFrame(prices, columns=["close"])
    sma = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()

    upper = (sma + 2 * std).iloc[-1]
    lower = (sma - 2 * std).iloc[-1]
    width = upper - lower
    price = df["close"].iloc[-1]

    send_alert(
        f"📊 {coin} | {tf}m\n"
        f"Manual Check\n"
        f"Price: {price:.2f}\n"
        f"Width: {width:.2f}\n"
        f"Upper: {upper:.2f}\n"
        f"Lower: {lower:.2f}"
    )

# ----------------- Summary -----------------
def show_summary(tf):
    message = f"📊 Summary | {tf}m\n"
    for _, row in coins.iterrows():
        coin = row["coin"]
        prices = get_prices(coin, tf)

        if not prices:
            message += f"{coin}: no data\n"
            continue

        df = pd.DataFrame(prices, columns=["close"])
        sma = df["close"].rolling(20).mean()
        std = df["close"].rolling(20).std()

        width = (sma + 2*std).iloc[-1] - (sma - 2*std).iloc[-1]
        price = df["close"].iloc[-1]

        message += f"{coin} | {price:.2f} | W: {width:.2f}\n"

    send_alert(message)

# ----------------- TELEGRAM COMMAND LISTENER -----------------
def telegram_listener():
    last_update_id = None

    while True:
        try:
            url = f"{BASE_URL}/getUpdates"
            params = {"timeout": 10}

            if last_update_id:
                params["offset"] = last_update_id + 1

            response = requests.get(url, params=params, timeout=15).json()

            for update in response.get("result", []):
                last_update_id = update["update_id"]

                if "message" not in update:
                    continue

                chat_id = update["message"]["chat"]["id"]
                text = update["message"].get("text", "")

                if str(chat_id) != str(CHAT_ID):
                    continue  # ignore others

                parts = text.strip().split()

                # -------- COMMAND: /check BTCUSDT 5 --------
                if parts[0].lower() == "/check" and len(parts) == 3:
                    coin = parts[1].upper()
                    tf = parts[2]
                    check_bollinger_width(coin, tf)

                # -------- COMMAND: /summary 5 --------
                elif parts[0].lower() == "/summary" and len(parts) == 2:
                    tf = parts[1]
                    show_summary(tf)

                else:
                    send_alert("❌ Invalid command.\nUse:\n/check BTCUSDT 5\n/summary 5")

        except Exception as e:
            print("Telegram listener error:", e)

        time.sleep(2)

# Run listener in background
threading.Thread(target=telegram_listener, daemon=True).start()

# ----------------- START -----------------
send_alert("🚨 BOT STARTED WITH TELEGRAM COMMANDS 🚨")

# ----------------- MAIN LOOP -----------------
while True:
    try:
        for _, row in coins.iterrows():
            coin = row["coin"]
            percent = row["percent"]
            rsi_high = row["rsi_overbought"]
            rsi_low = row["rsi_oversold"]
            expand = row["band_expand"]
            shrink = row["band_shrink"]

            for tf in timeframes:
                prices = get_prices(coin, tf)
                if not prices:
                    continue

                df = pd.DataFrame(prices, columns=["close"])
                price = df["close"].iloc[-1]

                # EMA
                df["EMA"] = df["close"].ewm(span=200).mean()
                ema = df["EMA"].iloc[-1]

                if price > ema * (1 + percent / 100):
                    send_alert(f"{coin} {tf}m EMA BREAKOUT 🚀")

                elif price < ema * (1 - percent / 100):
                    send_alert(f"{coin} {tf}m EMA BREAKDOWN 🔻")

                # RSI
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = -delta.clip(upper=0).rolling(14).mean()
                rs = gain / loss.replace(0, 1e-10)
                rsi = 100 - (100/(1+rs)).iloc[-1]

                if abs(rsi - rsi_high) < 0.5:
                    send_alert(f"{coin} {tf}m RSI ~{rsi_high}")

                elif abs(rsi - rsi_low) < 0.5:
                    send_alert(f"{coin} {tf}m RSI ~{rsi_low}")

                # Bollinger
                sma = df["close"].rolling(20).mean()
                std = df["close"].rolling(20).std()

                upper = (sma + 2*std).iloc[-1]
                lower = (sma - 2*std).iloc[-1]
                width = upper - lower

                if width > expand:
                    send_alert(f"{coin} {tf}m BB EXPANSION 📈 | W:{width:.2f}")

                elif width < shrink:
                    send_alert(f"{coin} {tf}m BB SQUEEZE 🔥 | W:{width:.2f}")

        time.sleep(60)

    except Exception as e:
        print("Error:", e)
        time.sleep(30)
