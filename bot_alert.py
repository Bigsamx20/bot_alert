import requests
import pandas as pd
import time
import os

# ----------------- Telegram Settings -----------------
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    print("Error: TOKEN or CHAT_ID is missing")
    exit()

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

# ----------------- Load coin list -----------------
try:
    coins = pd.read_csv("coins.csv")
except FileNotFoundError:
    print("Error: coins.csv not found")
    exit()
except Exception as e:
    print(f"Error reading coins.csv: {e}")
    exit()

required_columns = ["coin", "percent", "direction"]
for col in required_columns:
    if col not in coins.columns:
        print(f"Error: Missing required column '{col}' in coins.csv")
        exit()

# ----------------- Settings -----------------
timeframes = ["1", "5", "15", "60"]

RSI_OVERBOUGHT = 90
RSI_OVERSOLD = 10
TOLERANCE = 0.5  # how close RSI must be to 90 or 10

# ----------------- Track last alerts -----------------
last_alert = {
    coin: {
        tf: {
            "ema": None,
            "rsi": None
        } for tf in timeframes
    } for coin in coins["coin"]
}

# ----------------- Send Telegram alert -----------------
def send_alert(message):
    try:
        params = {"chat_id": CHAT_ID, "text": message}
        response = requests.get(TELEGRAM_URL, params=params, timeout=15)

        if response.status_code != 200:
            print("Telegram error:", response.text)
    except Exception as e:
        print("Telegram exception:", e)

# ----------------- Get prices from Bybit -----------------
def get_prices(symbol, interval):
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": 200
    }

    try:
        response = requests.get(url, params=params, timeout=15)
    except Exception as e:
        print(f"Network error fetching {symbol} ({interval}m): {e}")
        return None

    if response.status_code != 200:
        print(f"Error fetching {symbol} ({interval}m): {response.status_code}")
        print(response.text)
        return None

    try:
        data = response.json()
    except Exception:
        print(f"JSON error for {symbol} ({interval}m): {response.text}")
        return None

    if "result" not in data or "list" not in data["result"]:
        print(f"Unexpected response for {symbol} ({interval}m): {data}")
        return None

    try:
        closes = [float(item[4]) for item in data["result"]["list"]]
        closes.reverse()
        return closes
    except Exception as e:
        print(f"Parsing error for {symbol} ({interval}m): {e}")
        return None

# ----------------- Start message -----------------
send_alert("🚨 BOT STARTED: EMA + RSI (PRECISION 90/10) 🚨")

# ----------------- Main loop -----------------
while True:
    try:
        for _, row in coins.iterrows():
            coin = str(row["coin"]).strip().upper()
            percent = float(row["percent"])
            direction = str(row["direction"]).strip().lower()

            if direction not in ["above", "below", "both"]:
                print(f"Skipping {coin}: invalid direction '{direction}'")
                continue

            for tf in timeframes:
                prices = get_prices(coin, tf)
                if prices is None or len(prices) < 200:
                    print(f"Skipping {coin} ({tf}m): not enough data")
                    continue

                df = pd.DataFrame(prices, columns=["close"])

                # ----------------- EMA -----------------
                df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()

                # ----------------- RSI -----------------
                delta = df["close"].diff()
                gain = delta.clip(lower=0)
                loss = -delta.clip(upper=0)

                avg_gain = gain.rolling(window=14, min_periods=14).mean()
                avg_loss = loss.rolling(window=14, min_periods=14).mean()

                rs = avg_gain / avg_loss.replace(0, 1e-10)
                df["RSI"] = 100 - (100 / (1 + rs))

                # ----------------- Current values -----------------
                current_price = df["close"].iloc[-1]
                ema200 = df["EMA200"].iloc[-1]
                rsi = df["RSI"].iloc[-1]

                threshold_above = ema200 * (1 + percent / 100)
                threshold_below = ema200 * (1 - percent / 100)

                # ==================================================
                # 🔥 EMA SIGNAL
                # ==================================================
                ema_signal = None

                if direction == "above" and current_price > threshold_above:
                    ema_signal = "above"
                elif direction == "below" and current_price < threshold_below:
                    ema_signal = "below"
                elif direction == "both":
                    if current_price > threshold_above:
                        ema_signal = "above"
                    elif current_price < threshold_below:
                        ema_signal = "below"

                if ema_signal and last_alert[coin][tf]["ema"] != ema_signal:
                    if ema_signal == "above":
                        message = (
                            f"📊 {coin} | {tf}m\n"
                            f"Type: EMA Breakout 🚀\n"
                            f"Price: {current_price:.2f}\n"
                            f"EMA200: {ema200:.2f}\n"
                            f"RSI: {rsi:.2f}\n"
                            f"Above +{percent}%"
                        )
                    else:
                        message = (
                            f"📊 {coin} | {tf}m\n"
                            f"Type: EMA Breakdown 🔻\n"
                            f"Price: {current_price:.2f}\n"
                            f"EMA200: {ema200:.2f}\n"
                            f"RSI: {rsi:.2f}\n"
                            f"Below -{percent}%"
                        )

                    send_alert(message)
                    last_alert[coin][tf]["ema"] = ema_signal

                if threshold_below <= current_price <= threshold_above:
                    last_alert[coin][tf]["ema"] = None

                # ==================================================
                # 🎯 RSI SIGNAL (PRECISION 90 / 10)
                # ==================================================
                rsi_signal = None

                if abs(rsi - RSI_OVERBOUGHT) <= TOLERANCE:
                    rsi_signal = "overbought"
                elif abs(rsi - RSI_OVERSOLD) <= TOLERANCE:
                    rsi_signal = "oversold"

                if rsi_signal and last_alert[coin][tf]["rsi"] != rsi_signal:
                    if rsi_signal == "overbought":
                        message = (
                            f"📊 {coin} | {tf}m\n"
                            f"Type: RSI 🎯 HIT 90\n"
                            f"RSI: {rsi:.2f}\n"
                            f"Price: {current_price:.2f}"
                        )
                    else:
                        message = (
                            f"📊 {coin} | {tf}m\n"
                            f"Type: RSI 🎯 HIT 10\n"
                            f"RSI: {rsi:.2f}\n"
                            f"Price: {current_price:.2f}"
                        )

                    send_alert(message)
                    last_alert[coin][tf]["rsi"] = rsi_signal

                # Reset RSI
                if not (
                    abs(rsi - RSI_OVERBOUGHT) <= TOLERANCE or
                    abs(rsi - RSI_OVERSOLD) <= TOLERANCE
                ):
                    last_alert[coin][tf]["rsi"] = None

        print("Checked all coins... waiting 60 seconds\n")
        time.sleep(60)

    except Exception as e:
        print("Unexpected error:", e)
        print("Waiting 30 seconds before retrying...")
        time.sleep(30)
