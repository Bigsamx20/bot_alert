import requests
import pandas as pd
import time
import os
import threading
import matplotlib.pyplot as plt
import tempfile

# ---------------- TELEGRAM ----------------
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"

# ---------------- LOAD COINS ----------------
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

# ---------------- SAVE ----------------
def save_coins():
    coins.to_csv(FILE, index=False)

# ---------------- TRACKING ----------------
last_alert = {}

def init_coin_tracking(coin):
    last_alert[coin] = {
        tf: {"ema": None, "rsi": None, "bb": None}
        for tf in timeframes
    }

for c in coins["coin"]:
    init_coin_tracking(c)

# ---------------- TELEGRAM SEND ----------------
def send_alert(msg):
    try:
        requests.get(f"{TELEGRAM_URL}/sendMessage",
                     params={"chat_id": CHAT_ID, "text": msg}, timeout=10)
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
    except:
        pass

# ---------------- GET DATA ----------------
def get_prices(symbol, interval):
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category":"linear","symbol":symbol,"interval":interval,"limit":200},
            timeout=10
        )
        data = r.json()
        closes = [float(i[4]) for i in data["result"]["list"]]
        closes.reverse()
        return pd.DataFrame({"close": closes})
    except:
        return None

# ---------------- CHART ----------------
def plot_chart(df, coin, tf):
    df["EMA"] = df["close"].ewm(span=200).mean()
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

    ax1.plot(df["close"], label="Price")
    ax1.plot(df["EMA"], label="EMA200")
    ax1.plot(df["Upper"], label="Upper BB")
    ax1.plot(df["Lower"], label="Lower BB")
    ax1.legend()

    ax2.plot(df["RSI"], label="RSI")
    ax2.axhline(90)
    ax2.axhline(10)
    ax2.legend()

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.savefig(tmp.name)
    plt.close()
    return tmp.name

# ---------------- COMMANDS ----------------
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
        f"Width: {width:.2f}"
    )

    os.unlink(chart)

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
        width = (sma+2*std).iloc[-1]-(sma-2*std).iloc[-1]

        msg += f"{coin} | W:{width:.2f}\n"

    send_alert(msg)

# ---------------- TELEGRAM LISTENER ----------------
def telegram_listener():
    global coins
    last_update_id = None

    while True:
        try:
            res = requests.get(f"{TELEGRAM_URL}/getUpdates").json()

            for upd in res.get("result", []):
                last_update_id = upd["update_id"]

                if "message" not in upd:
                    continue

                chat_id = upd["message"]["chat"]["id"]
                text = upd["message"].get("text","")

                if str(chat_id) != str(CHAT_ID):
                    continue

                parts = text.split()

                # ADD
                if parts[0] == "/add" and len(parts) == 2:
                    data = parts[1].split(",")

                    if len(data) != 7:
                        send_alert("❌ Format:\n/add BTCUSDT,2,both,90,10,50,10")
                        continue

                    coin,p,d,rh,rl,be,bs = data
                    coin = coin.upper()

                    if coin in coins["coin"].values:
                        send_alert(f"❌ {coin} exists")
                        continue

                    coins.loc[len(coins)] = [
                        coin,float(p),d,float(rh),float(rl),float(be),float(bs)
                    ]

                    save_coins()
                    init_coin_tracking(coin)

                    send_alert(f"✅ {coin} added")

                # REMOVE
                elif parts[0] == "/remove":
                    coin = parts[1].upper()
                    coins = coins[coins["coin"]!=coin]
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
            print("TG error:", e)

        time.sleep(2)

threading.Thread(target=telegram_listener, daemon=True).start()

# ---------------- START ----------------
send_alert("🚨 FULL BOT RUNNING 🚨")

# ---------------- MAIN LOOP ----------------
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

                if price > ema*(1+percent/100):
                    if last_alert[coin][tf]["ema"]!="above":
                        send_alert(f"{coin} {tf}m ABOVE EMA 🚀")
                        last_alert[coin][tf]["ema"]="above"

                elif price < ema*(1-percent/100):
                    if last_alert[coin][tf]["ema"]!="below":
                        send_alert(f"{coin} {tf}m BELOW EMA 🔻")
                        last_alert[coin][tf]["ema"]="below"
                else:
                    last_alert[coin][tf]["ema"]=None

                # RSI (STRICT)
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = -delta.clip(upper=0).rolling(14).mean()
                rs = gain / loss.replace(0,1e-10)
                rsi = 100 - (100/(1+rs)).iloc[-1]

                if abs(rsi-rsi_high)<0.3:
                    if last_alert[coin][tf]["rsi"]!="high":
                        send_alert(f"{coin} {tf}m RSI OVERBOUGHT 🔴 ({rsi:.2f})")
                        last_alert[coin][tf]["rsi"]="high"

                elif abs(rsi-rsi_low)<0.3:
                    if last_alert[coin][tf]["rsi"]!="low":
                        send_alert(f"{coin} {tf}m RSI OVERSOLD 🟢 ({rsi:.2f})")
                        last_alert[coin][tf]["rsi"]="low"
                else:
                    last_alert[coin][tf]["rsi"]=None

                # BB
                sma = df["close"].rolling(20).mean()
                std = df["close"].rolling(20).std()
                upper = (sma+2*std).iloc[-1]
                lower = (sma-2*std).iloc[-1]
                width = upper-lower

                bb_signal=None
                if width>expand:
                    bb_signal="expand"
                elif width<shrink:
                    bb_signal="shrink"

                if bb_signal and last_alert[coin][tf]["bb"]!=bb_signal:
                    chart = plot_chart(df, coin, tf)

                    send_image(chart,
                        f"📊 {coin} | {tf}m\n"
                        f"Bollinger {'EXPANSION 📈' if bb_signal=='expand' else 'SQUEEZE 🔥'}\n"
                        f"Width: {width:.2f}\n"
                        f"Price: {price:.2f}"
                    )

                    os.unlink(chart)
                    last_alert[coin][tf]["bb"]=bb_signal

                if bb_signal is None:
                    last_alert[coin][tf]["bb"]=None

        time.sleep(60)

    except Exception as e:
        print("Main error:", e)
        time.sleep(30)
