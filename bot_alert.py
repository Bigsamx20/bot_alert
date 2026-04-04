import requests
import pandas as pd
import time
import os
import threading

# ----------------- TELEGRAM SETTINGS -----------------
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    print("Missing TOKEN or CHAT_ID")
    exit()

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"

# ----------------- LOAD COINS -----------------
FILE = "coins.csv"

try:
    coins = pd.read_csv(FILE)
except:
    coins = pd.DataFrame(columns=[
        "coin","percent","direction",
        "rsi_overbought","rsi_oversold",
        "band_expand","band_shrink"
    ])

timeframes = ["1","5","15","60"]

# ----------------- SAVE -----------------
def save_coins():
    coins.to_csv(FILE, index=False)

# ----------------- ALERT TRACKING -----------------
last_alert = {}

def init_coin_tracking(coin):
    last_alert[coin] = {
        tf: {"ema": None, "rsi": None, "bb": None}
        for tf in timeframes
    }

# Initialize existing coins
for c in coins["coin"]:
    init_coin_tracking(c)

# ----------------- TELEGRAM -----------------
def send_alert(msg):
    try:
        requests.get(
            f"{TELEGRAM_URL}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass

# ----------------- GET PRICES -----------------
def get_prices(symbol, interval):
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": 200
            },
            timeout=10
        )
        data = r.json()
        closes = [float(i[4]) for i in data["result"]["list"]]
        closes.reverse()
        return closes
    except:
        return None

# ----------------- TELEGRAM LISTENER -----------------
def telegram_listener():
    global coins
    last_update_id = None

    while True:
        try:
            params = {"timeout": 10}
            if last_update_id:
                params["offset"] = last_update_id + 1

            res = requests.get(f"{TELEGRAM_URL}/getUpdates", params=params).json()

            for upd in res.get("result", []):
                last_update_id = upd["update_id"]

                if "message" not in upd:
                    continue

                chat_id = upd["message"]["chat"]["id"]
                text = upd["message"].get("text", "")

                if str(chat_id) != str(CHAT_ID):
                    continue

                parts = text.strip().split()

                if len(parts) == 0:
                    continue

                # ----------------- ADD COIN (FIXED) -----------------
                if parts[0].lower() == "/add" and len(parts) == 2:
                    try:
                        data = parts[1].split(",")

                        if len(data) != 7:
                            send_alert("❌ Format:\n/add BTCUSDT,2,both,90,10,50,10")
                            continue

                        coin, p, d, rh, rl, be, bs = data
                        coin = coin.upper()

                        # Prevent duplicate
                        if coin in coins["coin"].values:
                            send_alert(f"❌ {coin} already exists")
                            continue

                        # Add properly
                        new_row = {
                            "coin": coin,
                            "percent": float(p),
                            "direction": d.lower(),
                            "rsi_overbought": float(rh),
                            "rsi_oversold": float(rl),
                            "band_expand": float(be),
                            "band_shrink": float(bs)
                        }

                        coins.loc[len(coins)] = new_row

                        # 🔥 CRITICAL FIXES
                        save_coins()
                        init_coin_tracking(coin)

                        send_alert(f"✅ {coin} added and tracking started")

                    except Exception as e:
                        send_alert(f"❌ Add error:\n{e}")

                # ----------------- REMOVE -----------------
                elif parts[0].lower() == "/remove" and len(parts) == 2:
                    coin = parts[1].upper()

                    if coin in coins["coin"].values:
                        coins = coins[coins["coin"] != coin]

                        if coin in last_alert:
                            del last_alert[coin]

                        save_coins()
                        send_alert(f"❌ {coin} removed")
                    else:
                        send_alert(f"❌ {coin} not found")

                # ----------------- LIST -----------------
                elif parts[0].lower() == "/list":
                    if coins.empty:
                        send_alert("⚠️ No coins yet")
                    else:
                        msg = "📊 Coins:\n" + "\n".join(coins["coin"])
                        send_alert(msg)

                else:
                    send_alert(
                        "Commands:\n"
                        "/add BTCUSDT,2,both,90,10,50,10\n"
                        "/remove BTCUSDT\n"
                        "/list"
                    )

        except Exception as e:
            print("Telegram error:", e)

        time.sleep(2)

# Start Telegram thread
threading.Thread(target=telegram_listener, daemon=True).start()

# ----------------- START -----------------
send_alert("🚨 BOT RUNNING (ADD FIXED) 🚨")

# ----------------- MAIN LOOP -----------------
while True:
    try:
        for _, row in coins.iterrows():
            coin = row["coin"]
            percent = row["percent"]

            for tf in timeframes:
                prices = get_prices(coin, tf)
                if prices is None or len(prices) < 200:
                    continue

                df = pd.DataFrame(prices, columns=["close"])

                df["EMA"] = df["close"].ewm(span=200).mean()

                price = df["close"].iloc[-1]
                ema = df["EMA"].iloc[-1]

                if price > ema * (1 + percent/100):
                    if last_alert[coin][tf]["ema"] != "above":
                        send_alert(f"{coin} {tf}m ABOVE EMA")
                        last_alert[coin][tf]["ema"] = "above"

                elif price < ema * (1 - percent/100):
                    if last_alert[coin][tf]["ema"] != "below":
                        send_alert(f"{coin} {tf}m BELOW EMA")
                        last_alert[coin][tf]["ema"] = "below"

                else:
                    last_alert[coin][tf]["ema"] = None

        time.sleep(60)

    except Exception as e:
        print("Main error:", e)
        time.sleep(30)
