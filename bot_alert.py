import requests
import pandas as pd
import time
import os

# ----------------- Telegram Settings -----------------
TOKEN = os.getenv("TOKEN")  # From Railway variable
CHAT_ID = os.getenv("CHAT_ID")  # From Railway variable

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

# ----------------- Track last alerts -----------------
timeframes = ["1", "5", "15", "60"]  # 1m, 5m, 15m, 1h
last_alert = {coin: {tf: None for tf in timeframes} for coin in coins["coin"]}

# ----------------- Send Telegram alert -----------------
def send_alert(message):
    try:
        params = {"chat_id": CHAT_ID, "text": message}
        response = requests.get(TELEGRAM_URL, params=params, timeout=15)

        if response.status_code != 200:
            print("Telegram error:", response.text)
    except Exception as e:
        print("Telegram exception:", e)

# ----------------- Get prices from Bybit (v5 API) -----------------
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
        closes.reverse()  # oldest -> newest
        return closes
    except Exception as e:
        print(f"Parsing error for {symbol} ({interval}m): {e}")
        return None

# ----------------- Start message -----------------
send_alert("✅ Bot started successfully (EMA200 alert bot active)")

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
                    print(f"Skipping {coin} ({tf}m): not enough price data")
                    continue

                df = pd.DataFrame(prices, columns=["close"])
                df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()

                current_price = df["close"].iloc[-1]
                ema200 = df["EMA200"].iloc[-1]

                threshold_above = ema200 * (1 + percent / 100)
                threshold_below = ema200 * (1 - percent / 100)

                new_alert = None

                if direction == "above" and current_price > threshold_above:
                    new_alert = "above"
                elif direction == "below" and current_price < threshold_below:
                    new_alert = "below"
                elif direction == "both":
                    if current_price > threshold_above:
                        new_alert = "above"
                    elif current_price < threshold_below:
                        new_alert = "below"

                # ----------------- Send alert if condition is newly met -----------------
                if new_alert and last_alert[coin][tf] != new_alert:
                    if new_alert == "above":
                        message = (
                            f"📊 {coin} | {tf}m\n"
                            f"Price: {current_price:.2f}\n"
                            f"EMA200: {ema200:.2f}\n"
                            f"Signal: 🚀 ABOVE +{percent}%\n"
                            f"Threshold: {threshold_above:.2f}"
                        )
                    else:
                        message = (
                            f"📊 {coin} | {tf}m\n"
                            f"Price: {current_price:.2f}\n"
                            f"EMA200: {ema200:.2f}\n"
                            f"Signal: 🔻 BELOW -{percent}%\n"
                            f"Threshold: {threshold_below:.2f}"
                        )

                    print("Sending alert:")
                    print(message)
                    send_alert(message)
                    last_alert[coin][tf] = new_alert

                # ----------------- Reset alert when price returns inside range -----------------
                if threshold_below <= current_price <= threshold_above:
                    last_alert[coin][tf] = None

        print("Checked all coins for all timeframes... waiting 60 seconds\n")
        time.sleep(60)

    except Exception as e:
        print("Unexpected error:", e)
        print("Waiting 30 seconds before retrying...")
        time.sleep(30)
