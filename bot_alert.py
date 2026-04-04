import requests
import pandas as pd
import time
import os
import threading
import matplotlib.pyplot as plt
import tempfile

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

timeframes = ["1","5","15","60"]

# ----------------- Save coins -----------------
def save_coins():
    coins.to_csv(FILE, index=False)

# ----------------- Alert tracker -----------------
last_alert = {}
def init_coin_tracking(c):
    last_alert[c] = {tf: {"ema":None,"rsi":None,"bb":None} for tf in timeframes}
for c in coins["coin"]:
    init_coin_tracking(c)

# ----------------- Send Telegram -----------------
def send_alert(msg):
    try:
        requests.get(f"{TELEGRAM_URL}/sendMessage", params={"chat_id":CHAT_ID,"text":msg}, timeout=10)
    except:
        pass

def send_image(image_path, caption):
    try:
        with open(image_path, "rb") as img:
            requests.post(
                f"{TELEGRAM_URL}/sendPhoto",
                params={"chat_id": CHAT_ID, "caption": caption},
                files={"photo": img}
            )
    except Exception as e:
        print("Image send error:", e)

# ----------------- Get Prices -----------------
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

# ----------------- Plot Line Chart (EMA+BB+RSI) -----------------
def plot_bollinger_rsi(df, coin, tf):
    df["EMA200"] = df["close"].ewm(span=200).mean()
    df["SMA"] = df["close"].rolling(20).mean()
    df["STD"] = df["close"].rolling(20).std()
    df["Upper"] = df["SMA"] + 2*df["STD"]
    df["Lower"] = df["SMA"] - 2*df["STD"]

    # RSI calculation
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0,1e-10)
    df["RSI"] = 100 - (100/(1+rs))

    fig, (ax1, ax2) = plt.subplots(2,1, figsize=(10,6), sharex=True, gridspec_kw={'height_ratios':[3,1]})
    
    # Price + EMA + Bollinger
    ax1.plot(df.index, df["close"], label="Close", color="blue")
    ax1.plot(df.index, df["EMA200"], label="EMA200", color="orange")
    ax1.plot(df.index, df["Upper"], label="Upper BB", color="green")
    ax1.plot(df.index, df["SMA"], label="SMA20", color="grey")
    ax1.plot(df.index, df["Lower"], label="Lower BB", color="red")
    ax1.set_title(f"{coin} ({tf}m) Price + EMA200 + Bollinger Bands")
    ax1.legend()
    
    # RSI
    ax2.plot(df.index, df["RSI"], label="RSI", color="purple")
    ax2.axhline(70, color="red", linestyle="--")
    ax2.axhline(30, color="green", linestyle="--")
    ax2.set_title("RSI")
    ax2.set_ylim(0,100)
    ax2.legend()
    
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.tight_layout()
    plt.savefig(tmp.name)
    plt.close()
    return tmp.name

# ----------------- Manual Check -----------------
def check_bollinger_width(coin, tf):
    df = get_prices(coin, tf)
    if df is None:
        send_alert(f"{coin} {tf}m ❌ No data")
        return
    chart = plot_bollinger_rsi(df, coin, tf)
    last = df["close"].iloc[-1]
    sma = df["SMA"].iloc[-1]
    upper = df["Upper"].iloc[-1]
    lower = df["Lower"].iloc[-1]
    width = upper - lower

    caption = (
        f"📊 {coin} | {tf}m\n"
        f"Manual Bollinger Check\n"
        f"Price: {last:.2f}\n"
        f"Width: {width:.2f}\n"
        f"Upper: {upper:.2f}\n"
        f"Lower: {lower:.2f}"
    )
    send_image(chart, caption)
    os.unlink(chart)

# ----------------- Telegram Listener -----------------
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
                if "message" not in upd: continue
                chat_id = upd["message"]["chat"]["id"]
                text = upd["message"].get("text","")
                if str(chat_id) != str(CHAT_ID): continue
                parts = text.strip().split()

                if parts[0]=="/check" and len(parts)==3:
                    check_bollinger_width(parts[1].upper(), parts[2])
                elif parts[0]=="/summary" and len(parts)==2:
                    show_summary(parts[1])
                else:
                    send_alert("❌ Invalid. Use:\n/check BTCUSDT 5\n/summary 5")

        except Exception as e:
            print("Telegram listener error:", e)
        time.sleep(2)

threading.Thread(target=telegram_listener, daemon=True).start()

# ----------------- Startup -----------------
send_alert("🚨 BOT WITH EMA+BB+RSI CHARTS RUNNING 🚨")

# ----------------- Main Loop -----------------
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
                if df is None: continue
                price = df["close"].iloc[-1]

                # ---------- EMA ----------
                df["EMA200"] = df["close"].ewm(span=200).mean()
                ema = df["EMA200"].iloc[-1]

                ema_signal = None
                if price > ema*(1+percent/100):
                    ema_signal = "above"
                elif price < ema*(1-percent/100):
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

                # ---------- RSI ----------
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = -delta.clip(upper=0).rolling(14).mean()
                rs = gain / loss.replace(0,1e-10)
                rsi = 100 - (100/(1+rs)).iloc[-1]

                rsi_signal = None
                if abs(rsi-rsi_high)<0.5:
                    rsi_signal="high"
                elif abs(rsi-rsi_low)<0.5:
                    rsi_signal="low"

                if rsi_signal and last_alert[coin][tf]["rsi"] != rsi_signal:
                    send_alert(
                        f"📊 {coin} | {tf}m\n"
                        f"RSI {'OVERBOUGHT 🔴' if rsi_signal=='high' else 'OVERSOLD 🟢'}\n"
                        f"RSI: {rsi:.2f}\nPrice: {price:.2f}"
                    )
                    last_alert[coin][tf]["rsi"]=rsi_signal
                if rsi_signal is None:
                    last_alert[coin][tf]["rsi"]=None

                # ---------- Bollinger ----------
                df["SMA"] = df["close"].rolling(20).mean()
                df["STD"] = df["close"].rolling(20).std()
                df["Upper"] = df["SMA"] + 2*df["STD"]
                df["Lower"] = df["SMA"] - 2*df["STD"]
                upper = df["Upper"].iloc[-1]
                lower = df["Lower"].iloc[-1]
                width = upper - lower

                bb_signal = None
                if width > expand:
                    bb_signal="expand"
                elif width < shrink:
                    bb_signal="shrink"

                if bb_signal and last_alert[coin][tf]["bb"] != bb_signal:
                    chart = plot_bollinger_rsi(df, coin, tf)
                    caption = (
                        f"📊 {coin} | {tf}m\n"
                        f"Bollinger {'EXPANSION 📈' if bb_signal=='expand' else 'SQUEEZE 🔥'}\n"
                        f"Width: {width:.2f}\n"
                        f"Price: {price:.2f}\n"
                        f"Upper: {upper:.2f}\n"
                        f"Lower: {lower:.2f}"
                    )
                    send_image(chart, caption)
                    os.unlink(chart)
                    last_alert[coin][tf]["bb"]=bb_signal
                if bb_signal is None:
                    last_alert[coin][tf]["bb"]=None

        time.sleep(60)

    except Exception as e:
        print("Main loop error:", e)
        time.sleep(30)
