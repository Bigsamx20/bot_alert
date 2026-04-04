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

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

# ----------------- Load coins -----------------
coins = pd.read_csv("coins.csv")

# Defaults if missing
coins["bollinger_k"] = coins.get("bollinger_k", 2)
coins["band_expand"] = coins.get("band_expand", 50)
coins["band_shrink"] = coins.get("band_shrink", 10)

timeframes = ["1", "5", "15", "60"]

# ----------------- Track alerts (ANTI-SPAM) -----------------
last_alert = {
    coin: {
        tf: {"ema": None, "rsi": None, "bb": None}
        for tf in timeframes
    } for coin in coins["coin"]
}

# ----------------- Telegram -----------------
def send_alert(msg):
    try:
        requests.get(TELEGRAM_URL, params={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except:
        pass

# ----------------- Get Prices -----------------
def get_prices(symbol, interval):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": 200}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        closes = [float(i[4]) for i in data["result"]["list"]]
        closes.reverse()
        return closes
    except:
        return None

# ----------------- Manual Check -----------------
def check_bollinger_width(coin, tf):
    prices = get_prices(coin, tf)
    if not prices:
        print("No data")
        return

    df = pd.DataFrame(prices, columns=["close"])
    sma = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()

    upper = (sma + 2 * std).iloc[-1]
    lower = (sma - 2 * std).iloc[-1]
    width = upper - lower

    print(f"\n📊 {coin} | {tf}m")
    print(f"Price: {df['close'].iloc[-1]:.2f}")
    print(f"Width: {width:.2f}")
    print(f"Upper: {upper:.2f}")
    print(f"Lower: {lower:.2f}\n")

# ----------------- Summary -----------------
def show_summary(tf):
    print(f"\n--- SUMMARY {tf}m ---")
    for _, row in coins.iterrows():
        coin = row["coin"]
        prices = get_prices(coin, tf)
        if not prices:
            print(f"{coin}: no data")
            continue

        df = pd.DataFrame(prices, columns=["close"])
        sma = df["close"].rolling(20).mean()
        std = df["close"].rolling(20).std()

        upper = (sma + 2 * std).iloc[-1]
        lower = (sma - 2 * std).iloc[-1]
        width = upper - lower

        print(f"{coin} | {df['close'].iloc[-1]:.2f} | width: {width:.2f}")
    print("-------------------\n")

# ----------------- Manual Input Thread -----------------
def manual_input():
    while True:
        cmd = input("Command (check / summary / exit): ").lower()

        if cmd == "exit":
            break

        elif cmd == "check":
            coin = input("Coin: ").upper()
            tf = input("Timeframe: ")
            check_bollinger_width(coin, tf)

        elif cmd == "summary":
            tf = input("Timeframe: ")
            show_summary(tf)

        else:
            print("Invalid command")

threading.Thread(target=manual_input, daemon=True).start()

# ----------------- Start -----------------
send_alert("🚨 BOT STARTED: EMA + RSI + BB 🚨")

# ----------------- MAIN LOOP -----------------
while True:
    try:
        for _, row in coins.iterrows():
            coin = row["coin"]
            percent = row["percent"]
            direction = row["direction"]
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

                # -------- EMA --------
                df["EMA"] = df["close"].ewm(span=200).mean()
                ema = df["EMA"].iloc[-1]

                ema_signal = None
                if price > ema * (1 + percent / 100):
                    ema_signal = "above"
                elif price < ema * (1 - percent / 100):
                    ema_signal = "below"

                if ema_signal and last_alert[coin][tf]["ema"] != ema_signal:
                    send_alert(
                        f"📊 {coin} | {tf}m\n"
                        f"EMA {'BREAKOUT 🚀' if ema_signal=='above' else 'BREAKDOWN 🔻'}\n"
                        f"Price: {price:.2f}\nEMA: {ema:.2f}"
                    )
                    last_alert[coin][tf]["ema"] = ema_signal

                if ema_signal is None:
                    last_alert[coin][tf]["ema"] = None

                # -------- RSI --------
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = -delta.clip(upper=0).rolling(14).mean()
                rs = gain / loss.replace(0, 1e-10)
                rsi = 100 - (100 / (1 + rs)).iloc[-1]

                rsi_signal = None
                if abs(rsi - rsi_high) < 0.5:
                    rsi_signal = "overbought"
                elif abs(rsi - rsi_low) < 0.5:
                    rsi_signal = "oversold"

                if rsi_signal and last_alert[coin][tf]["rsi"] != rsi_signal:
                    send_alert(
                        f"📊 {coin} | {tf}m\n"
                        f"RSI HIT {'OVERBOUGHT 🔴' if rsi_signal=='overbought' else 'OVERSOLD 🟢'}\n"
                        f"RSI: {rsi:.2f}\nPrice: {price:.2f}"
                    )
                    last_alert[coin][tf]["rsi"] = rsi_signal

                if rsi_signal is None:
                    last_alert[coin][tf]["rsi"] = None

                # -------- BOLLINGER --------
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
                        f"📊 {coin} | {tf}m\n"
                        f"Bollinger {'EXPANSION 📈' if bb_signal=='expand' else 'SQUEEZE 🔥'}\n"
                        f"Width: {width:.2f}\n"
                        f"Price: {price:.2f}\n"
                        f"Upper: {upper:.2f}\n"
                        f"Lower: {lower:.2f}"
                    )
                    last_alert[coin][tf]["bb"] = bb_signal

                if bb_signal is None:
                    last_alert[coin][tf]["bb"] = None

        time.sleep(60)

    except Exception as e:
        print("Error:", e)
        time.sleep(30)
