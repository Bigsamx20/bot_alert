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

required_columns = ["coin", "percent", "direction", "rsi_overbought", "rsi_oversold"]
for col in required_columns:
    if col not in coins.columns:
        print(f"Error: Missing required column '{col}' in coins.csv")
        exit()

# Optional Bollinger columns for per-coin customization
if "bollinger_k" not in coins.columns:
    coins["bollinger_k"] = 2
if "band_expand" not in coins.columns:
    coins["band_expand"] = 50
if "band_shrink" not in coins.columns:
    coins["band_shrink"] = 10

# ----------------- Settings -----------------
timeframes = ["1", "5", "15", "60"]

# ----------------- Track last alerts -----------------
last_alert = {
    coin: {
        tf: {
            "ema": None,
            "rsi_zone": None,
            "bollinger": None
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
    params = {"category": "linear", "symbol": symbol.upper(), "interval": interval, "limit": 200}
    try:
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        closes = [float(item[4]) for item in data["result"]["list"]]
        closes.reverse()
        return closes
    except Exception as e:
        print(f"Error fetching prices for {symbol} ({interval}m): {e}")
        return None

# ----------------- Manual Bollinger width check -----------------
def check_bollinger_width(coin, tf):
    row = coins.loc[coins["coin"].str.upper() == coin.upper()]
    if row.empty:
        print(f"Coin {coin} not found in coins.csv")
        return
    row = row.iloc[0]
    bollinger_k = float(row.get("bollinger_k", 2))
    window = 20
    prices = get_prices(coin, tf)
    if not prices:
        print(f"No price data for {coin} {tf}m")
        return
    df = pd.DataFrame(prices, columns=["close"])
    sma = df["close"].rolling(window=window).mean()
    std = df["close"].rolling(window=window).std()
    upper_band = sma + bollinger_k * std
    lower_band = sma - bollinger_k * std
    width = upper_band.iloc[-1] - lower_band.iloc[-1]
    print(f"\nCoin: {coin.upper()} | Interval: {tf}m")
    print(f"Current Price: {df['close'].iloc[-1]:.2f}")
    print(f"Bollinger Band Width: {width:.2f}")
    print(f"Upper Band: {upper_band.iloc[-1]:.2f}")
    print(f"Lower Band: {lower_band.iloc[-1]:.2f}")

# ----------------- Start message -----------------
send_alert("🚨 BOT STARTED: EMA + RSI + Bollinger Width + Manual Check 🚨")

# ----------------- Main loop -----------------
while True:
    try:
        for _, row in coins.iterrows():
            coin = str(row["coin"]).strip().upper()
            percent = float(row["percent"])
            direction = str(row["direction"]).strip().lower()
            rsi_overbought = float(row["rsi_overbought"])
            rsi_oversold = float(row["rsi_oversold"])
            bollinger_k = float(row.get("bollinger_k", 2))
            band_expand = float(row.get("band_expand", 50))
            band_shrink = float(row.get("band_shrink", 10))

            if direction not in ["above", "below", "both"]:
                print(f"Skipping {coin}: invalid direction '{direction}'")
                continue

            for tf in timeframes:
                prices = get_prices(coin, tf)
                if prices is None or len(prices) < 200:
                    print(f"Skipping {coin} ({tf}m): not enough data")
                    continue

                df = pd.DataFrame(prices, columns=["close"])

                # EMA
                df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()

                # RSI
                delta = df["close"].diff()
                gain = delta.clip(lower=0)
                loss = -delta.clip(upper=0)
                avg_gain = gain.rolling(window=14, min_periods=14).mean()
                avg_loss = loss.rolling(window=14, min_periods=14).mean()
                rs = avg_gain / avg_loss.replace(0, 1e-10)
                df["RSI"] = 100 - (100 / (1 + rs))

                current_price = df["close"].iloc[-1]
                ema200 = df["EMA200"].iloc[-1]
                rsi = df["RSI"].iloc[-1]

                threshold_above = ema200 * (1 + percent / 100)
                threshold_below = ema200 * (1 - percent / 100)

                # -------- EMA Alert --------
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
                    message = f"📊 {coin} | {tf}m\nType: EMA {'Breakout 🚀' if ema_signal=='above' else 'Breakdown 🔻'}\nPrice: {current_price:.2f}\nEMA200: {ema200:.2f}\nRSI: {rsi:.2f}\n"
                    send_alert(message)
                    last_alert[coin][tf]["ema"] = ema_signal

                if threshold_below <= current_price <= threshold_above:
                    last_alert[coin][tf]["ema"] = None

                # -------- RSI Alert --------
                current_zone = None
                if rsi >= rsi_overbought - 0.5 and rsi <= rsi_overbought + 0.5:
                    current_zone = "overbought"
                elif rsi >= rsi_oversold - 0.5 and rsi <= rsi_oversold + 0.5:
                    current_zone = "oversold"

                rsi_signal = None
                if current_zone and current_zone != last_alert[coin][tf]["rsi_zone"]:
                    rsi_signal = current_zone
                    message = f"📊 {coin} | {tf}m\nType: RSI 🎯 ENTERED {current_zone.upper()} ZONE\nRSI: {rsi:.2f}\nPrice: {current_price:.2f}"
                    send_alert(message)
                    last_alert[coin][tf]["rsi_zone"] = rsi_signal

                if current_zone is None:
                    last_alert[coin][tf]["rsi_zone"] = None

                # -------- Bollinger Band Width Alert --------
                window = 20
                sma = df["close"].rolling(window=window).mean()
                std = df["close"].rolling(window=window).std()
                upper_band = sma + bollinger_k * std
                lower_band = sma - bollinger_k * std
                band_width = upper_band.iloc[-1] - lower_band.iloc[-1]

                bollinger_signal = None
                if band_width > band_expand:
                    bollinger_signal = "expanded"
                elif band_width < band_shrink:
                    bollinger_signal = "contracted"

                if bollinger_signal and last_alert[coin][tf]["bollinger"] != bollinger_signal:
                    message = f"📊 {coin} | {tf}m\nBollinger Band {bollinger_signal.upper()}\nWidth: {band_width:.2f}\nPrice: {current_price:.2f}"
                    send_alert(message)
                    last_alert[coin][tf]["bollinger"] = bollinger_signal

                if band_shrink <= band_width <= band_expand:
                    last_alert[coin][tf]["bollinger"] = None

        print("Checked all coins... waiting 60 seconds\n")
        time.sleep(60)

    except Exception as e:
        print("Unexpected error:", e)
        print("Waiting 30 seconds before retrying...")
        time.sleep(30)
