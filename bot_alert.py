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

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"

# ----------------- Load coins -----------------
coins = pd.read_csv("coins.csv")

# Defaults
if "bollinger_k" not in coins.columns:
    coins["bollinger_k"] = 2
if "band_expand" not in coins.columns:
    coins["band_expand"] = 50
if "band_shrink" not in coins.columns:
    coins["band_shrink"] = 10

timeframes = ["1", "5", "15", "60"]

# ----------------- Track alerts (ANTI-SPAM) -----------------
last_alert = {
    coin: {tf: {"ema": None, "rsi": None, "bb": None} for tf in timeframes}
    for coin in coins["coin"]
}

# ----------------- Send Telegram -----------------
def send_alert(message):
    try:
        requests.get(f"{TELEGRAM_URL}/sendMessage", params={
            "chat_id": CHAT_ID,
            "text": message
        }, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# ----------------- FIXED Get Prices -----------------
def get_prices(symbol, interval):
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": 200
    }

    try:
        response = requests.get(url, params=params, timeout=10)

        if response.status_code != 200:
            print("HTTP error:", response.text)
            return None

        data = response.json()

        if "result" not in data or "list" not in data["result"]:
            print("Bad API response:", data)
            return None

        closes = [float(i[4]) for i in data["result"]["list"]]
        closes.reverse()
        return closes

    except Exception as e:
        print("Price fetch error:", e)
        return None

# ----------------- Manual Check (TELEGRAM) -----------------
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

# ----------------- Summary (TELEGRAM) -----------------
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

        message += f"{coin} | {price:.2f} | W:{width:.2f}\n"

    send_alert(message)

# ----------------- TELEGRAM COMMAND LISTENER -----------------
def telegram_listener():
    last_update_id = None

    while True:
        try:
            params = {"timeout": 10}
            if last_update_id:
                params["offset"] = last_update_id + 1

            response = requests.get(
                f"{TELEGRAM_URL}/getUpdates",
                params=params,
                timeout=15
            ).json()

            for update in response.get("result", []):
                last_update_id = update["update_id"]

                if "message" not in update:
                    continue

                chat_id = update["message"]["chat"]["id"]
                text = update["message"].get("text", "")

                if str(chat_id) != str(CHAT_ID):
                    continue

                parts = text.strip().split()

                if len(parts) == 0:
                    continue

                # -------- /check BTCUSDT 5 --------
                if parts[0].lower() == "/check" and len(parts) == 3:
                    coin = parts[1].upper()
                    tf = parts[2]
                    check_bollinger_width(coin, tf)

                # -------- /summary 5 --------
                elif parts[0].lower() == "/summary" and len(parts) == 2:
                    tf = parts[1]
                    show_summary(tf)

                else:
                    send_alert("❌ Invalid command\nUse:\n/check BTCUSDT 5\n/summary 5")

        except Exception as e:
            print("Telegram error:", e)

        time.sleep(2)

# Run Telegram listener
threading.Thread(target=telegram_listener, daemon=True).start()

# ----------------- START -----------------
send_alert("🚨 BOT FULLY FIXED & RUNNING 🚨")

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

                ema_signal = None
                if price > ema * (1 + percent / 100):
                    ema_signal = "above"
                elif price < ema * (1 - percent / 100):
                    ema_signal = "below"

                if ema_signal and last_alert[coin][tf]["ema"] != ema_signal:
                    send_alert(f"{coin} {tf}m EMA {'🚀' if ema_signal=='above' else '🔻'}")
                    last_alert[coin][tf]["ema"] = ema_signal

                if ema_signal is None:
                    last_alert[coin][tf]["ema"] = None

                # RSI
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = -delta.clip(upper=0).rolling(14).mean()
                rs = gain / loss.replace(0, 1e-10)
                rsi = 100 - (100/(1+rs)).iloc[-1]

                rsi_signal = None
                if abs(rsi - rsi_high) < 0.5:
                    rsi_signal = "high"
                elif abs(rsi - rsi_low) < 0.5:
                    rsi_signal = "low"

                if rsi_signal and last_alert[coin][tf]["rsi"] != rsi_signal:
                    send_alert(f"{coin} {tf}m RSI {'🔴' if rsi_signal=='high' else '🟢'} ({rsi:.2f})")
                    last_alert[coin][tf]["rsi"] = rsi_signal

                if rsi_signal is None:
                    last_alert[coin][tf]["rsi"] = None

                # Bollinger
                sma = df["close"].rolling(20).mean()
                std = df["close"].rolling(20).std()

                upper = (sma + 2 * std).iloc[-1]
                lower = (sma - 2 * std).iloc[-1]
                width = upper - lower

                bb_signal = None
                if width > expand:
                    bb_signal = "expand"
                elif width < shrink:
                    bb_signal = "shrink"

                if bb_signal and last_alert[coin][tf]["bb"] != bb_signal:
                    send_alert(
                        f"{coin} {tf}m BB {'📈' if bb_signal=='expand' else '🔥'}\n"
                        f"W:{width:.2f} P:{price:.2f}"
                    )
                    last_alert[coin][tf]["bb"] = bb_signal

                if bb_signal is None:
                    last_alert[coin][tf]["bb"] = None

        time.sleep(60)

    except Exception as e:
        print("Main loop error:", e)
        time.sleep(30)
