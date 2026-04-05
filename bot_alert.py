import os
import time
import threading
import requests
import pandas as pd

# =========================
# TELEGRAM / BOT SETTINGS
# =========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise SystemExit("Missing TOKEN or CHAT_ID environment variables.")

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"

# Only these timeframes
TIMEFRAMES = ["5", "15", "60"]

# Extreme EMA distance standard for all coins
EXTREME_EMA_DISTANCE_PERCENT = 15.0

# Faster scan cycle
SCAN_INTERVAL_SECONDS = 15

# File for your tracked coins
COINS_FILE = "coins.csv"

# Reuse HTTP connections
session = requests.Session()

# Thread lock for shared data
data_lock = threading.Lock()

# =========================
# LOAD / SAVE COINS
# =========================
def load_coins() -> pd.DataFrame:
    try:
        df = pd.read_csv(COINS_FILE)
    except Exception:
        df = pd.DataFrame(columns=["coin"])

    if "coin" not in df.columns:
        df["coin"] = None

    df["coin"] = df["coin"].astype(str).str.upper().str.strip()
    df = df[df["coin"].notna()]
    df = df[df["coin"] != ""]
    df = df.drop_duplicates(subset=["coin"]).reset_index(drop=True)
    return df[["coin"]]

def save_coins(df: pd.DataFrame) -> None:
    df.to_csv(COINS_FILE, index=False)

coins = load_coins()

# =========================
# ALERT TRACKING
# =========================
last_alert = {}

def init_coin_tracking(coin: str) -> None:
    last_alert[coin] = {tf: None for tf in TIMEFRAMES}

def sync_tracking() -> None:
    current = set(coins["coin"].tolist())
    existing = set(last_alert.keys())

    for coin in current - existing:
        init_coin_tracking(coin)

    for coin in existing - current:
        del last_alert[coin]

sync_tracking()

# =========================
# TELEGRAM HELPERS
# =========================
def send_alert(message: str) -> None:
    try:
        session.get(
            f"{TELEGRAM_URL}/sendMessage",
            params={"chat_id": CHAT_ID, "text": message},
            timeout=15,
        )
    except Exception as e:
        print("send_alert error:", e)

# =========================
# BYBIT HELPERS
# =========================
def symbol_exists_on_bybit(symbol: str) -> bool:
    try:
        r = session.get(
            BYBIT_INSTRUMENTS_URL,
            params={"category": "linear", "symbol": symbol.upper()},
            timeout=15,
        )
        data = r.json()
        items = data.get("result", {}).get("list", [])
        for item in items:
            if item.get("symbol", "").upper() == symbol.upper() and item.get("status") == "Trading":
                return True
        return False
    except Exception as e:
        print("symbol_exists_on_bybit error:", e)
        return False

def get_ohlc(symbol: str, interval: str) -> pd.DataFrame | None:
    try:
        r = session.get(
            BYBIT_KLINE_URL,
            params={
                "category": "linear",
                "symbol": symbol.upper(),
                "interval": interval,
                "limit": 200,
            },
            timeout=15,
        )
        data = r.json()
        rows = data.get("result", {}).get("list", [])
        if not rows:
            return None

        rows.reverse()

        df = pd.DataFrame(
            rows,
            columns=["start_time", "open", "high", "low", "close", "volume", "turnover"]
        )

        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna().reset_index(drop=True)
        if len(df) < 200:
            return None

        return df
    except Exception as e:
        print(f"get_ohlc error for {symbol} {interval}: {e}")
        return None

# =========================
# EMA HELPERS
# =========================
def add_ema(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
    return df

def ema_distance_percent(price: float, ema: float) -> float:
    if ema == 0:
        return 0.0
    return ((price - ema) / ema) * 100

def classify_extreme(distance_pct: float) -> str | None:
    if distance_pct >= EXTREME_EMA_DISTANCE_PERCENT:
        return "above"
    if distance_pct <= -EXTREME_EMA_DISTANCE_PERCENT:
        return "below"
    return None

# =========================
# COMMANDS
# =========================
def check_coin(coin: str, tf: str) -> None:
    if tf not in TIMEFRAMES:
        send_alert("❌ Timeframe must be 5, 15, or 60")
        return

    df = get_ohlc(coin, tf)
    if df is None:
        send_alert(f"{coin} {tf}m ❌ No data")
        return

    df = add_ema(df)
    price = df["close"].iloc[-1]
    ema = df["EMA200"].iloc[-1]
    distance = ema_distance_percent(price, ema)
    status = classify_extreme(distance)

    if status == "above":
        label = "EXTREMELY FAR ABOVE EMA 🚀"
    elif status == "below":
        label = "EXTREMELY FAR BELOW EMA 🔻"
    else:
        label = "NOT EXTREME"

    send_alert(
        f"📊 {coin} | {tf}m\n"
        f"EMA CHECK\n"
        f"Status: {label}\n"
        f"Distance: {distance:.2f}%\n"
        f"Price: {price:.6f}\n"
        f"EMA200: {ema:.6f}\n"
        f"Extreme Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}%"
    )

def show_summary(tf: str) -> None:
    if tf not in TIMEFRAMES:
        send_alert("❌ Timeframe must be 5, 15, or 60")
        return

    with data_lock:
        coin_list = coins["coin"].tolist()

    msg = (
        f"📊 Summary {tf}m\n"
        f"Extreme EMA Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}%\n"
    )

    found = 0
    for coin in coin_list:
        df = get_ohlc(coin, tf)
        if df is None:
            continue

        df = add_ema(df)
        price = df["close"].iloc[-1]
        ema = df["EMA200"].iloc[-1]
        distance = ema_distance_percent(price, ema)
        status = classify_extreme(distance)

        if status is None:
            continue

        direction = "ABOVE" if status == "above" else "BELOW"
        msg += f"{coin} | {direction} | D:{distance:.2f}%\n"
        found += 1

        if found >= 50:
            msg += "... more coins omitted"
            break

    if found == 0:
        msg += "No extreme EMA-distance coins found."

    send_alert(msg)

# =========================
# TELEGRAM LISTENER
# =========================
def telegram_listener():
    global coins
    last_update_id = None

    while True:
        try:
            params = {"timeout": 10}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1

            res = session.get(f"{TELEGRAM_URL}/getUpdates", params=params, timeout=20).json()

            for upd in res.get("result", []):
                last_update_id = upd["update_id"]

                if "message" not in upd:
                    continue

                message = upd["message"]
                chat_id = message["chat"]["id"]
                text = message.get("text", "")

                if str(chat_id) != str(CHAT_ID):
                    continue

                parts = text.strip().split()
                if not parts:
                    continue

                cmd = parts[0].lower()

                if cmd == "/add" and len(parts) == 2:
                    coin = parts[1].upper().strip()

                    if not coin.endswith("USDT"):
                        send_alert("❌ Use a valid Bybit linear symbol like BTCUSDT")
                        continue

                    if not symbol_exists_on_bybit(coin):
                        send_alert(f"❌ {coin} not found on Bybit linear derivatives")
                        continue

                    with data_lock:
                        if coin in coins["coin"].tolist():
                            send_alert(f"❌ {coin} already exists")
                            continue

                        coins.loc[len(coins)] = [coin]
                        coins = coins.drop_duplicates(subset=["coin"]).reset_index(drop=True)
                        save_coins(coins)
                        sync_tracking()

                    send_alert(f"✅ {coin} added")

                elif cmd == "/remove" and len(parts) == 2:
                    coin = parts[1].upper().strip()

                    with data_lock:
                        if coin not in coins["coin"].tolist():
                            send_alert(f"❌ {coin} not found")
                            continue

                        coins = coins[coins["coin"] != coin].reset_index(drop=True)
                        save_coins(coins)
                        sync_tracking()

                    send_alert(f"❌ {coin} removed")

                elif cmd == "/list":
                    with data_lock:
                        coin_list = coins["coin"].tolist()

                    if not coin_list:
                        send_alert("⚠️ No coins in list")
                    else:
                        msg = (
                            f"📋 Coins\n"
                            f"Tracked: {len(coin_list)}\n"
                            f"Timeframes: 5m / 15m / 60m\n"
                            f"Extreme EMA Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}%\n"
                        )
                        msg += "\n".join(coin_list[:100])
                        if len(coin_list) > 100:
                            msg += f"\n... and {len(coin_list) - 100} more"
                        send_alert(msg)

                elif cmd == "/check" and len(parts) == 3:
                    check_coin(parts[1].upper(), parts[2])

                elif cmd == "/summary" and len(parts) == 2:
                    show_summary(parts[1])

                elif cmd == "/refresh":
                    with data_lock:
                        coins = load_coins()
                        sync_tracking()
                    send_alert(f"🔄 Reloaded local coin list\nTracked coins: {len(coins)}")

                else:
                    send_alert(
                        "Commands:\n"
                        "/add BTCUSDT\n"
                        "/remove BTCUSDT\n"
                        "/list\n"
                        "/check BTCUSDT 5\n"
                        "/summary 5\n"
                        "/refresh"
                    )

        except Exception as e:
            print("telegram_listener error:", e)

        time.sleep(2)

# =========================
# STARTUP
# =========================
threading.Thread(target=telegram_listener, daemon=True).start()

send_alert(
    f"🚨 EMA EXTREME BOT RUNNING 🚨\n"
    f"Tracked coins: {len(coins)}\n"
    f"Timeframes: 5m / 15m / 60m\n"
    f"Alert Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}% away from EMA200\n"
    f"Scan interval: {SCAN_INTERVAL_SECONDS}s"
)

# =========================
# MAIN LOOP
# =========================
while True:
    try:
        with data_lock:
            coin_list = coins["coin"].tolist()

        for coin in coin_list:
            for tf in TIMEFRAMES:
                df = get_ohlc(coin, tf)
                if df is None:
                    continue

                df = add_ema(df)
                price = df["close"].iloc[-1]
                ema = df["EMA200"].iloc[-1]
                distance_pct = ema_distance_percent(price, ema)
                signal = classify_extreme(distance_pct)

                if signal and last_alert[coin][tf] != signal:
                    if signal == "above":
                        send_alert(
                            f"📊 {coin} | {tf}m\n"
                            f"EXTREMELY FAR ABOVE EMA 🚀\n"
                            f"Distance: {distance_pct:.2f}%\n"
                            f"Price: {price:.6f}\n"
                            f"EMA200: {ema:.6f}\n"
                            f"Extreme Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}%"
                        )
                    else:
                        send_alert(
                            f"📊 {coin} | {tf}m\n"
                            f"EXTREMELY FAR BELOW EMA 🔻\n"
                            f"Distance: {distance_pct:.2f}%\n"
                            f"Price: {price:.6f}\n"
                            f"EMA200: {ema:.6f}\n"
                            f"Extreme Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}%"
                        )
                    last_alert[coin][tf] = signal

                if signal is None:
                    last_alert[coin][tf] = None

        time.sleep(SCAN_INTERVAL_SECONDS)

    except Exception as e:
        print("Main loop error:", e)
        time.sleep(10)
