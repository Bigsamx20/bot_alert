import requests
import pandas as pd
import time
import os
import threading

# ----------------- Telegram Settings -----------------
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    print("Missing TOKEN or CHAT_ID")
    exit()

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"

# ----------------- Load coins -----------------
FILE = "coins.csv"

try:
    coins = pd.read_csv(FILE)
except:
    coins = pd.DataFrame(columns=[
        "coin","percent","direction",
        "rsi_overbought","rsi_oversold",
        "band_expand","band_shrink"
    ])

timeframes = ["1", "5", "15", "60"]

# ----------------- Save coins -----------------
def save_coins():
    coins.to_csv(FILE, index=False)

# ----------------- Alert tracker -----------------
last_alert = {}

def init_coin_tracking(coin):
    last_alert[coin] = {
        tf: {"ema": None, "rsi": None, "bb": None}
        for tf in timeframes
    }

for c in coins["coin"]:
    init_coin_tracking(c)

# ----------------- Telegram -----------------
def send_alert(msg):
    try:
        requests.get(f"{TELEGRAM_URL}/sendMessage",
                     params={"chat_id": CHAT_ID, "text": msg},
                     timeout=10)
    except:
        pass

# ----------------- Get Prices -----------------
def get_prices(symbol, interval):
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={
                "category":"linear",
                "symbol":symbol,
                "interval":interval,
                "limit":200
            }, timeout=10
        )
        data = r.json()
        closes = [float(i[4]) for i in data["result"]["list"]]
        closes.reverse()
        return closes
    except:
        return None

# ----------------- Manual Check -----------------
def check_bollinger_width(coin, tf):
    prices = get_prices(coin, tf)
    if not prices:
        send_alert(f"{coin} {tf}m ❌ No data")
        return

    df = pd.DataFrame(prices, columns=["close"])
    sma = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()

    upper = (sma + 2*std).iloc[-1]
    lower = (sma - 2*std).iloc[-1]
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
    msg = f"📊 Summary | {tf}m\n"
    for _, row in coins.iterrows():
        coin = row["coin"]
        prices = get_prices(coin, tf)
        if not prices:
            msg += f"{coin}: no data\n"
            continue

        df = pd.DataFrame(prices, columns=["close"])
        sma = df["close"].rolling(20).mean()
        std = df["close"].rolling(20).std()

        width = (sma + 2*std).iloc[-1] - (sma - 2*std).iloc[-1]
        price = df["close"].iloc[-1]

        msg += f"{coin} | {price:.2f} | W:{width:.2f}\n"

    send_alert(msg)

# ----------------- TELEGRAM COMMANDS -----------------
def telegram_listener():
    global coins

    last_update_id = None

    while True:
        try:
            params = {"timeout":10}
            if last_update_id:
                params["offset"] = last_update_id + 1

            res = requests.get(f"{TELEGRAM_URL}/getUpdates", params=params).json()

            for upd in res.get("result", []):
                last_update_id = upd["update_id"]

                if "message" not in upd:
                    continue

                chat_id = upd["message"]["chat"]["id"]
                text = upd["message"].get("text","")

                if str(chat_id) != str(CHAT_ID):
                    continue

                parts = text.split()

                # -------- ADD --------
                if parts[0] == "/add" and len(parts) == 8:
                    coin = parts[1].upper()

                    new_row = {
                        "coin": coin,
                        "percent": float(parts[2]),
                        "direction": parts[3],
                        "rsi_overbought": float(parts[4]),
                        "rsi_oversold": float(parts[5]),
                        "band_expand": float(parts[6]),
                        "band_shrink": float(parts[7])
                    }

                    coins = coins[coins["coin"] != coin]
                    coins = pd.concat([coins, pd.DataFrame([new_row])], ignore_index=True)

                    init_coin_tracking(coin)
                    save_coins()

                    send_alert(f"✅ Added {coin}")

                # -------- REMOVE --------
                elif parts[0] == "/remove" and len(parts) == 2:
                    coin = parts[1].upper()

                    coins = coins[coins["coin"] != coin]
                    if coin in last_alert:
                        del last_alert[coin]

                    save_coins()
                    send_alert(f"❌ Removed {coin}")

                # -------- LIST --------
                elif parts[0] == "/list":
                    msg = "📊 Coins:\n"
                    for _, r in coins.iterrows():
                        msg += f"{r['coin']} | EMA:{r['percent']}%\n"
                    send_alert(msg)

                # -------- CHECK --------
                elif parts[0] == "/check" and len(parts) == 3:
                    check_bollinger_width(parts[1].upper(), parts[2])

                # -------- SUMMARY --------
                elif parts[0] == "/summary" and len(parts) == 2:
                    show_summary(parts[1])

                else:
                    send_alert("❌ Invalid command")

        except Exception as e:
            print("Telegram error:", e)

        time.sleep(2)

threading.Thread(target=telegram_listener, daemon=True).start()

# ----------------- START -----------------
send_alert("🚨 BOT WITH FULL CONTROL RUNNING 🚨")

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

                if price > ema*(1+percent/100):
                    send_alert(f"{coin} {tf}m EMA 🚀")

                elif price < ema*(1-percent/100):
                    send_alert(f"{coin} {tf}m EMA 🔻")

                # RSI
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = -delta.clip(upper=0).rolling(14).mean()
                rs = gain / loss.replace(0,1e-10)
                rsi = 100 - (100/(1+rs)).iloc[-1]

                if abs(rsi-rsi_high)<0.5:
                    send_alert(f"{coin} {tf}m RSI 🔴 {rsi:.2f}")

                elif abs(rsi-rsi_low)<0.5:
                    send_alert(f"{coin} {tf}m RSI 🟢 {rsi:.2f}")

                # BB FULL FORMAT
                sma = df["close"].rolling(20).mean()
                std = df["close"].rolling(20).std()

                upper = (sma+2*std).iloc[-1]
                lower = (sma-2*std).iloc[-1]
                width = upper-lower

                if width > expand:
                    send_alert(
                        f"📊 {coin} | {tf}m\n"
                        f"Bollinger EXPANSION 📈\n"
                        f"Width: {width:.2f}\n"
                        f"Price: {price:.2f}\n"
                        f"Upper: {upper:.2f}\n"
                        f"Lower: {lower:.2f}"
                    )

                elif width < shrink:
                    send_alert(
                        f"📊 {coin} | {tf}m\n"
                        f"Bollinger SQUEEZE 🔥\n"
                        f"Width: {width:.2f}\n"
                        f"Price: {price:.2f}\n"
                        f"Upper: {upper:.2f}\n"
                        f"Lower: {lower:.2f}"
                    )

        time.sleep(60)

    except Exception as e:
        print("Main loop error:", e)
        time.sleep(30)
