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

if not TOKEN or not CHAT_ID:
    print("Missing TOKEN or CHAT_ID")
    exit()

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"

# ---------------- LOAD COINS ----------------
FILE = "coins.csv"

try:
    coins = pd.read_csv(FILE)
except Exception:
    coins = pd.DataFrame(columns=[
        "coin", "percent", "direction",
        "rsi_overbought", "rsi_oversold",
        "band_expand", "band_shrink"
    ])

timeframes = ["1", "5", "15", "60"]

# ---------------- SAVE ----------------
def save_coins():
    coins.to_csv(FILE, index=False)

# ---------------- TRACKING ----------------
last_alert = {}

def init_coin_tracking(coin):
    last_alert[coin] = {
        tf: {
            "ema": None,
            "rsi": None,
            "bb": None,
            "candle": None
        }
        for tf in timeframes
    }

for c in coins["coin"]:
    init_coin_tracking(c)

# ---------------- TELEGRAM SEND ----------------
def send_alert(msg):
    try:
        requests.get(
            f"{TELEGRAM_URL}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        print("send_alert error:", e)

def send_image(path, caption):
    try:
        with open(path, "rb") as img:
            requests.post(
                f"{TELEGRAM_URL}/sendPhoto",
                params={"chat_id": CHAT_ID, "caption": caption},
                files={"photo": img},
                timeout=20
            )
    except Exception as e:
        print("send_image error:", e)

# ---------------- GET DATA ----------------
def get_ohlc(symbol, interval):
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

        if "result" not in data or "list" not in data["result"]:
            return None

        rows = data["result"]["list"]
        rows.reverse()  # oldest -> newest

        df = pd.DataFrame(rows, columns=[
            "start_time", "open", "high", "low", "close",
            "volume", "turnover"
        ])

        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df
    except Exception as e:
        print("get_ohlc error:", e)
        return None

# ---------------- INDICATORS ----------------
def add_indicators(df):
    df = df.copy()

    # EMA
    df["EMA"] = df["close"].ewm(span=200, adjust=False).mean()

    # Bollinger
    df["SMA20"] = df["close"].rolling(20).mean()
    df["STD20"] = df["close"].rolling(20).std()
    df["Upper"] = df["SMA20"] + 2 * df["STD20"]
    df["Lower"] = df["SMA20"] - 2 * df["STD20"]

    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-10)
    df["RSI"] = 100 - (100 / (1 + rs))

    # Candle body size
    df["body_size"] = (df["close"] - df["open"]).abs()
    df["avg_body_size"] = df["body_size"].rolling(20).mean()

    return df

# ---------------- CHARTS ----------------
def plot_standard_chart(df, coin, tf):
    df = add_indicators(df)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]}
    )

    ax1.plot(df.index, df["close"], label="Price")
    ax1.plot(df.index, df["EMA"], label="EMA200")
    ax1.plot(df.index, df["Upper"], label="Upper BB")
    ax1.plot(df.index, df["Lower"], label="Lower BB")
    ax1.plot(df.index, df["SMA20"], label="SMA20")
    ax1.set_title(f"{coin} | {tf}m")
    ax1.legend()

    ax2.plot(df.index, df["RSI"], label="RSI")
    ax2.axhline(90, linestyle="--")
    ax2.axhline(10, linestyle="--")
    ax2.set_ylim(0, 100)
    ax2.legend()

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.tight_layout()
    plt.savefig(tmp.name)
    plt.close()
    return tmp.name

def plot_giant_candle_chart(df, coin, tf):
    df = add_indicators(df)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]}
    )

    ax1.plot(df.index, df["close"], label="Price")
    ax1.plot(df.index, df["EMA"], label="EMA200")
    ax1.plot(df.index, df["Upper"], label="Upper BB")
    ax1.plot(df.index, df["Lower"], label="Lower BB")

    last_idx = df.index[-1]
    ax1.axvline(last_idx, linewidth=2)
    ax1.scatter([last_idx], [df["close"].iloc[-1]], s=80, label="Giant Candle")

    ax1.set_title(f"{coin} | {tf}m | Giant Candle")
    ax1.legend()

    ax2.plot(df.index, df["RSI"], label="RSI")
    ax2.axhline(90, linestyle="--")
    ax2.axhline(10, linestyle="--")
    ax2.set_ylim(0, 100)
    ax2.legend()

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.tight_layout()
    plt.savefig(tmp.name)
    plt.close()
    return tmp.name

# ---------------- COMMANDS ----------------
def check_bollinger_width(coin, tf):
    df = get_ohlc(coin, tf)
    if df is None or len(df) < 20:
        send_alert(f"{coin} {tf}m ❌ No data")
        return

    df = add_indicators(df)
    chart = plot_standard_chart(df, coin, tf)

    upper = df["Upper"].iloc[-1]
    lower = df["Lower"].iloc[-1]
    width = upper - lower
    price = df["close"].iloc[-1]

    send_image(
        chart,
        f"📊 {coin} | {tf}m\n"
        f"Manual Check\n"
        f"Price: {price:.2f}\n"
        f"Width: {width:.2f}\n"
        f"Upper: {upper:.2f}\n"
        f"Lower: {lower:.2f}"
    )

    os.unlink(chart)

def show_summary(tf):
    msg = f"📊 Summary {tf}m\n"
    for _, r in coins.iterrows():
        coin = r["coin"]
        df = get_ohlc(coin, tf)
        if df is None or len(df) < 20:
            msg += f"{coin}: no data\n"
            continue

        df = add_indicators(df)
        width = df["Upper"].iloc[-1] - df["Lower"].iloc[-1]
        price = df["close"].iloc[-1]

        msg += f"{coin} | P:{price:.2f} | W:{width:.2f}\n"

    send_alert(msg)

# ---------------- TELEGRAM LISTENER ----------------
def telegram_listener():
    global coins
    last_update_id = None

    while True:
        try:
            params = {"timeout": 10}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1

            res = requests.get(f"{TELEGRAM_URL}/getUpdates", params=params, timeout=15).json()

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

                # ADD
                if parts[0] == "/add" and len(parts) == 2:
                    data = parts[1].split(",")

                    if len(data) != 7:
                        send_alert("❌ Format:\n/add BTCUSDT,2,both,90,10,50,10")
                        continue

                    coin, p, d, rh, rl, be, bs = data
                    coin = coin.upper()

                    if coin in coins["coin"].values:
                        send_alert(f"❌ {coin} exists")
                        continue

                    coins.loc[len(coins)] = [
                        coin, float(p), d, float(rh), float(rl), float(be), float(bs)
                    ]

                    save_coins()
                    init_coin_tracking(coin)
                    send_alert(f"✅ {coin} added")

                # REMOVE
                elif parts[0] == "/remove" and len(parts) == 2:
                    coin = parts[1].upper()
                    coins = coins[coins["coin"] != coin]
                    save_coins()

                    if coin in last_alert:
                        del last_alert[coin]

                    send_alert(f"❌ {coin} removed")

                # LIST
                elif parts[0] == "/list":
                    if coins.empty:
                        send_alert("⚠️ No coins in list")
                    else:
                        send_alert("\n".join(coins["coin"]))

                # CHECK
                elif parts[0] == "/check" and len(parts) == 3:
                    check_bollinger_width(parts[1].upper(), parts[2])

                # SUMMARY
                elif parts[0] == "/summary" and len(parts) == 2:
                    show_summary(parts[1])

                else:
                    send_alert(
                        "Commands:\n"
                        "/add BTCUSDT,2,both,90,10,50,10\n"
                        "/remove BTCUSDT\n"
                        "/list\n"
                        "/check BTCUSDT 5\n"
                        "/summary 5"
                    )

        except Exception as e:
            print("TG error:", e)

        time.sleep(2)

threading.Thread(target=telegram_listener, daemon=True).start()

# ---------------- START ----------------
send_alert("🚨 FULL BOT RUNNING WITH GIANT CANDLE ALERTS 🚨")

# ---------------- MAIN LOOP ----------------
while True:
    try:
        for _, row in coins.iterrows():
            coin = row["coin"]
            percent = float(row["percent"])
            direction = str(row["direction"]).lower()
            rsi_high = float(row["rsi_overbought"])
            rsi_low = float(row["rsi_oversold"])
            expand = float(row["band_expand"])
            shrink = float(row["band_shrink"])

            for tf in timeframes:
                df = get_ohlc(coin, tf)
                if df is None or len(df) < 200:
                    continue

                df = add_indicators(df)

                price = df["close"].iloc[-1]
                ema = df["EMA"].iloc[-1]

                # ---------------- EMA ----------------
                ema_signal = None

                threshold_above = ema * (1 + percent / 100)
                threshold_below = ema * (1 - percent / 100)

                if direction == "above":
                    if price > threshold_above:
                        ema_signal = "above"
                elif direction == "below":
                    if price < threshold_below:
                        ema_signal = "below"
                elif direction == "both":
                    if price > threshold_above:
                        ema_signal = "above"
                    elif price < threshold_below:
                        ema_signal = "below"

                if ema_signal and last_alert[coin][tf]["ema"] != ema_signal:
                    if ema_signal == "above":
                        send_alert(
                            f"📊 {coin} | {tf}m\n"
                            f"EMA ABOVE 🚀\n"
                            f"Price: {price:.2f}\n"
                            f"EMA: {ema:.2f}\n"
                            f"Threshold: {threshold_above:.2f}"
                        )
                    else:
                        send_alert(
                            f"📊 {coin} | {tf}m\n"
                            f"EMA BELOW 🔻\n"
                            f"Price: {price:.2f}\n"
                            f"EMA: {ema:.2f}\n"
                            f"Threshold: {threshold_below:.2f}"
                        )
                    last_alert[coin][tf]["ema"] = ema_signal

                if threshold_below <= price <= threshold_above:
                    last_alert[coin][tf]["ema"] = None

                # ---------------- RSI ----------------
                rsi = df["RSI"].iloc[-1]
                rsi_signal = None

                if abs(rsi - rsi_high) < 0.3:
                    rsi_signal = "high"
                elif abs(rsi - rsi_low) < 0.3:
                    rsi_signal = "low"

                if rsi_signal and last_alert[coin][tf]["rsi"] != rsi_signal:
                    if rsi_signal == "high":
                        send_alert(
                            f"📊 {coin} | {tf}m\n"
                            f"RSI OVERBOUGHT 🔴\n"
                            f"RSI: {rsi:.2f}\n"
                            f"Price: {price:.2f}"
                        )
                    else:
                        send_alert(
                            f"📊 {coin} | {tf}m\n"
                            f"RSI OVERSOLD 🟢\n"
                            f"RSI: {rsi:.2f}\n"
                            f"Price: {price:.2f}"
                        )
                    last_alert[coin][tf]["rsi"] = rsi_signal

                if rsi_signal is None:
                    last_alert[coin][tf]["rsi"] = None

                # ---------------- BOLLINGER ----------------
                upper = df["Upper"].iloc[-1]
                lower = df["Lower"].iloc[-1]
                width = upper - lower

                bb_signal = None
                if width > expand:
                    bb_signal = "expand"
                elif width < shrink:
                    bb_signal = "shrink"

                if bb_signal and last_alert[coin][tf]["bb"] != bb_signal:
                    chart = plot_standard_chart(df, coin, tf)

                    send_image(
                        chart,
                        f"📊 {coin} | {tf}m\n"
                        f"Bollinger {'EXPANSION 📈' if bb_signal == 'expand' else 'SQUEEZE 🔥'}\n"
                        f"Width: {width:.2f}\n"
                        f"Price: {price:.2f}\n"
                        f"Upper: {upper:.2f}\n"
                        f"Lower: {lower:.2f}"
                    )

                    os.unlink(chart)
                    last_alert[coin][tf]["bb"] = bb_signal

                if bb_signal is None:
                    last_alert[coin][tf]["bb"] = None

                # ---------------- GIANT CANDLE ----------------
                current_body = df["body_size"].iloc[-1]
                avg_body = df["avg_body_size"].iloc[-2] if len(df) > 21 else None

                candle_signal = None
                multiplier = None

                if avg_body and avg_body > 0:
                    ratio = current_body / avg_body
                    ratio_int = int(round(ratio))

                    if 10 <= ratio_int <= 15:
                        candle_signal = f"{ratio_int}x"
                        multiplier = ratio_int

                if candle_signal and last_alert[coin][tf]["candle"] != candle_signal:
                    chart = plot_giant_candle_chart(df, coin, tf)

                    direction_text = "BULLISH 🟢" if df["close"].iloc[-1] >= df["open"].iloc[-1] else "BEARISH 🔴"

                    send_image(
                        chart,
                        f"📊 {coin} | {tf}m\n"
                        f"GIANT CANDLE ALERT 🔥\n"
                        f"Size: {multiplier}x candle\n"
                        f"Type: {direction_text}\n"
                        f"Body Size: {current_body:.4f}\n"
                        f"Average Body: {avg_body:.4f}\n"
                        f"Price: {price:.2f}"
                    )

                    os.unlink(chart)
                    last_alert[coin][tf]["candle"] = candle_signal

                if candle_signal is None:
                    last_alert[coin][tf]["candle"] = None

        time.sleep(60)

    except Exception as e:
        print("Main error:", e)
        time.sleep(30)
