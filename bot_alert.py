import requests
import pandas as pd
import time
import os
import threading
import matplotlib.pyplot as plt
import tempfile

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

# ----------------- ALERT TRACKER -----------------
last_alert = {}

def init_coin_tracking(coin):
    last_alert[coin] = {
        tf: {"ema": None, "rsi": None, "bb": None}
        for tf in timeframes
    }

for c in coins["coin"]:
    init_coin_tracking(c)

# ----------------- TELEGRAM -----------------
def send_alert(msg):
    try:
        requests.get(f"{TELEGRAM_URL}/sendMessage",
                     params={"chat_id": CHAT_ID, "text": msg},
                     timeout=10)
    except:
        pass

def send_image(path, caption):
    try:
        with open(path, "rb") as img:
            requests.post(
                f"{TELEGRAM_URL}/sendPhoto",
                params={"chat_id": CHAT_ID, "caption": caption},
                files={"photo": img}
            )
    except Exception as e:
        print("Image error:", e)

# ----------------- GET PRICES -----------------
def get_prices(symbol, interval):
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={
                "category":"linear",
                "symbol":symbol,
                "interval":interval,
                "limit":200
            },
            timeout=10
        )
        data = r.json()
        closes = [float(i[4]) for i in data["result"]["list"]]
        closes.reverse()
        return pd.DataFrame({"close": closes})
    except:
        return None

# ----------------- CHART -----------------
def plot_chart(df, coin, tf):
    df["EMA200"] = df["close"].ewm(span=200).mean()
    df["SMA"] = df["close"].rolling(20).mean()
    df["STD"] = df["close"].rolling(20).std()
    df["Upper"] = df["SMA"] + 2*df["STD"]
    df["Lower"] = df["SMA"] - 2*df["STD"]

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0,1e-10)
    df["RSI"] = 100 - (100/(1+rs))

    fig, (ax1, ax2) = plt.subplots(2,1, figsize=(10,6), sharex=True)

    ax1.plot(df["close"])
    ax1.plot(df["EMA200"])
    ax1.plot(df["Upper"])
    ax1.plot(df["Lower"])
    ax1.set_title(f"{coin} {tf}m")

    ax2.plot(df["RSI"])
    ax2.axhline(70)
    ax2.axhline(30)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.savefig(tmp.name)
    plt.close()
    return tmp.name

# ----------------- MANUAL CHECK -----------------
def check_bollinger_width(coin, tf):
    df = get_prices(coin, tf)
    if df is None:
        send_alert(f"{coin} {tf}m ❌ No data")
        return

    chart = plot_chart(df, coin, tf)

    sma = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()

    upper = (sma + 2*std).iloc[-1]
    lower = (sma - 2*std).iloc[-1]
    width = upper - lower
    price = df["close"].iloc[-1]

    send_image(chart,
        f"📊 {coin} | {tf}m\n"
        f"Manual Check\n"
        f"Price: {price:.2f}\n"
        f"Width: {width:.2f}\n"
        f"Upper: {upper:.2f}\n"
        f"Lower: {lower:.2f}"
    )

    os.unlink(chart)

# ----------------- SUMMARY -----------------
def show_summary(tf):
    msg = f"📊 Summary {tf}m\n"
    for _, r in coins.iterrows():
        coin = r["coin"]
        df = get_prices(coin, tf)
        if df is None:
            msg += f"{coin}: no data\n"
            continue

        sma = df["close"].rolling(20).mean()
        std = df["close"].rolling(20).std()
        width = (sma + 2*std).iloc[-1] - (sma - 2*std).iloc[-1]

        msg += f"{coin} | W:{width:.2f}\n"

    send_alert(msg)

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

                # ADD
                if parts[0] == "/add" and len(parts) == 2:
                    data = parts[1].split(",")
                    if len(data) != 7:
                        send_alert("❌ Wrong format")
                        continue

                    coin, p, d, rh, rl, be, bs = data
                    coin = coin.upper()

                    coins.loc[len(coins)] = [
                        coin, float(p), d,
                        float(rh), float(rl),
                        float(be), float(bs)
                    ]

                    init_coin_tracking(coin)
                    save_coins()
                    send_alert(f"✅ {coin} added")

                # REMOVE
                elif parts[0] == "/remove":
                    coin = parts[1].upper()
                    coins = coins[coins["coin"] != coin]
                    save_coins()
                    send_alert(f"❌ {coin} removed")

                # LIST
                elif parts[0] == "/list":
                    send_alert("\n".join(coins["coin"]))

                # CHECK
                elif parts[0] == "/check":
                    check_bollinger_width(parts[1], parts[2])

                # SUMMARY
                elif parts[0] == "/summary":
                    show_summary(parts[1])

        except Exception as e:
            print("Telegram error:", e)

        time.sleep(2)

threading.Thread(target=telegram_listener, daemon=True).start()

# ----------------- START -----------------
send_alert("🚨 BOT FULLY RUNNING 🚨")

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
                df = get_prices(coin, tf)
                if df is None:
                    continue

                price = df["close"].iloc[-1]

                # EMA
                df["EMA"] = df["close"].ewm(span=200).mean()
                ema = df["EMA"].iloc[-1]

                ema_signal = None
                if price > ema*(1+percent/100):
                    ema_signal = "above"
                elif price < ema*(1-percent/100):
                    ema_signal = "below"

                if ema_signal and last_alert[coin][tf]["ema"] != ema_signal:
                    send_alert(f"{coin} {tf}m EMA {ema_signal}")
                    last_alert[coin][tf]["ema"] = ema_signal
                if ema_signal is None:
                    last_alert[coin][tf]["ema"] = None

                # RSI
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = -delta.clip(upper=0).rolling(14).mean()
                rs = gain / loss.replace(0,1e-10)
                rsi = 100 - (100/(1+rs)).iloc[-1]

                rsi_signal = None
                if abs(rsi - rsi_high) < 0.5:
                    rsi_signal = "high"
                elif abs(rsi - rsi_low) < 0.5:
                    rsi_signal = "low"

                if rsi_signal and last_alert[coin][tf]["rsi"] != rsi_signal:
                    send_alert(f"{coin} {tf}m RSI {rsi_signal} {rsi:.2f}")
                    last_alert[coin][tf]["rsi"] = rsi_signal
                if rsi_signal is None:
                    last_alert[coin][tf]["rsi"] = None

                # BB
                sma = df["close"].rolling(20).mean()
                std = df["close"].rolling(20).std()
                upper = (sma + 2*std).iloc[-1]
                lower = (sma - 2*std).iloc[-1]
                width = upper - lower

                bb_signal = None
                if width > expand:
                    bb_signal = "expand"
                elif width < shrink:
                    bb_signal = "shrink"

                if bb_signal and last_alert[coin][tf]["bb"] != bb_signal:
                    chart = plot_chart(df, coin, tf)

                    send_image(chart,
                        f"📊 {coin} | {tf}m\n"
                        f"Bollinger {'EXPANSION 📈' if bb_signal=='expand' else 'SQUEEZE 🔥'}\n"
                        f"Width: {width:.2f}\n"
                        f"Price: {price:.2f}\n"
                        f"Upper: {upper:.2f}\n"
                        f"Lower: {lower:.2f}"
                    )

                    os.unlink(chart)
                    last_alert[coin][tf]["bb"] = bb_signal

                if bb_signal is None:
                    last_alert[coin][tf]["bb"] = None

        time.sleep(60)

    except Exception as e:
        print("Main error:", e)
        time.sleep(30)
