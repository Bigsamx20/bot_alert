import requests
import pandas as pd
import time

# ----------------- Telegram Settings -----------------
TOKEN = "8276758800:AAEPjxoMAn_uXEkMyAqzQCrLmzl2pfE4Lf8"
CHAT_ID = "6903033357"
TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

# ----------------- Load coin list -----------------
try:
    coins = pd.read_csv("coins.csv")
except FileNotFoundError:
    print("Error: coins.csv not found")
    exit()

# ----------------- Track last alerts -----------------
# We now track per coin per timeframe
timeframes = ["1", "5", "15", "60"]  # 1m, 5m, 15m, 1h
last_alert = {coin: {tf: None for tf in timeframes} for coin in coins['coin']}

# ----------------- Send Telegram alert -----------------
def send_alert(message):
    try:
        params = {"chat_id": CHAT_ID, "text": message}
        r = requests.get(TELEGRAM_URL, params=params)
        if r.status_code != 200:
            print("Telegram error:", r.text)
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
        r = requests.get(url, params=params)
    except Exception as e:
        print(f"Network error fetching {symbol} ({interval}m): {e}")
        return None

    if r.status_code != 200:
        print(f"Error fetching {symbol} ({interval}m): {r.status_code}")
        print(r.text)
        return None

    try:
        data = r.json()
    except:
        print(f"JSON error for {symbol} ({interval}m): {r.text}")
        return None

    if "result" not in data or "list" not in data["result"]:
        print(f"Unexpected response for {symbol} ({interval}m): {data}")
        return None

    try:
        closes = [float(item[4]) for item in data["result"]["list"]]
        closes.reverse()  # oldest → newest
        return closes
    except Exception as e:
        print(f"Parsing error for {symbol} ({interval}m): {e}")
        return None

# ----------------- Main loop -----------------
# Send test alert once when bot starts
send_alert("✅ Bot started successfully (EMA + RSI active)")

while True:
    try:
        for index, row in coins.iterrows():
            coin = row['coin']
            percent = float(row['percent'])
            direction = row['direction'].lower()

            for tf in timeframes:
                prices = get_prices(coin, tf)
                if prices is None:
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

                # Send alert if new for this timeframe
                if new_alert and last_alert[coin][tf] != new_alert:
                    if new_alert == "above":
                        message = f"{coin} 🚀 {tf}m ALERT: Price {current_price:.2f} ABOVE EMA200 + {percent}% ({threshold_above:.2f})"
                    else:
                        message = f"{coin} 🔻 {tf}m ALERT: Price {current_price:.2f} BELOW EMA200 - {percent}% ({threshold_below:.2f})"

                    print("Sending alert:", message)
                    send_alert(message)
                    last_alert[coin][tf] = new_alert

                # Reset alert if price is back inside threshold
                if threshold_below <= current_price <= threshold_above:
                    last_alert[coin][tf] = None

        print("Checked all coins for all timeframes... waiting 60 seconds\n")
        time.sleep(60)

    except Exception as e:
        print("Unexpected error:", e)
        print("Waiting 30 seconds before retrying...")
        time.sleep(30)