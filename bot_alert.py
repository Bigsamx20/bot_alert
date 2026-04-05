import os
import time
import threading
import requests
import pandas as pd

# =========================
# TELEGRAM / GENERAL CONFIG
# =========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise SystemExit("Missing TOKEN or CHAT_ID environment variables.")

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"

# Track only these timeframes
TIMEFRAMES = ["5", "15", "60"]

# Top N auto-tracked coins
TOP_N_COINS = 100

# Universal extreme EMA distance threshold
EXTREME_EMA_DISTANCE_PERCENT = 15.0

# Faster scan cycle
SCAN_INTERVAL_SECONDS = 15

# Optional local exclusions file
REMOVED_COINS_FILE = "removed_coins.txt"

# Reuse HTTP connections
session = requests.Session()

# Shared data
data_lock = threading.Lock()
coins = pd.DataFrame(columns=["coin"])
last_alert = {}

# =========================
# FILE HELPERS
# =========================
def load_removed_symbols() -> set[str]:
    if not os.path.exists(REMOVED_COINS_FILE):
        return set()
    try:
        with open(REMOVED_COINS_FILE, "r", encoding="utf-8") as f:
            return {line.strip().upper() for line in f if line.strip()}
    except Exception:
        return set()

def save_removed_symbols(symbols: set[str]) -> None:
    try:
        with open(REMOVED_COINS_FILE, "w", encoding="utf-8") as f:
            for sym in sorted(symbols):
                f.write(sym + "\n")
    except Exception as e:
        print("save_removed_symbols error:", e)

removed_symbols = load_removed_symbols()

# =========================
# ALERT TRACKING
# =========================
def init_coin_tracking(coin: str) -> None:
    last_alert[coin] = {tf: None for tf in TIMEFRAMES}

def sync_tracking() -> None:
    current = set(coins["coin"].dropna().astype(str).str.upper())
    existing = set(last_alert.keys())

    for coin in current - existing:
        init_coin_tracking(coin)

    for coin in existing - current:
        del last_alert[coin]

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
def fetch_all_trading_linear_symbols() -> list[str]:
    symbols = []
    cursor = None

    while True:
        params = {
            "category": "linear",
            "status": "Trading",
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            r = session.get(BYBIT_INSTRUMENTS_URL, params=params, timeout=20)
            data = r.json()
            result = data.get("result", {})
            items = result.get("list", [])

            if not items:
                break

            for item in items:
                symbol = str(item.get("symbol", "")).upper()
                status = item.get("status")
                if symbol and status == "Trading":
                    symbols.append(symbol)

            cursor = result.get("nextPageCursor")
            if not cursor:
                break

        except Exception as e:
            print("fetch_all_trading_linear_symbols error:", e)
            break

    return sorted(set(symbols))

def fetch_linear_tickers() -> dict[str, float]:
    turnover_map = {}
    try:
        r = session.get(
            BYBIT_TICKERS_URL,
            params={"category": "linear"},
            timeout=20,
        )
        data = r.json()
        items = data.get("result", {}).get("list", [])

        for item in items:
            symbol = str(item.get("symbol", "")).upper()
            turnover = item.get("turnover24h", "0")
            try:
                turnover_map[symbol] = float(turnover)
            except Exception:
                turnover_map[symbol] = 0.0
    except Exception as e:
        print("fetch_linear_tickers error:", e)

    return turnover_map

def rebuild_coin_universe() -> None:
    global coins

    symbols = fetch_all_trading_linear_symbols()
    turnover_map = fetch_linear_tickers()

    ranked = []
    for sym in symbols:
        if sym in removed_symbols:
            continue
        ranked.append((sym, turnover_map.get(sym, 0.0)))

    ranked.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [sym for sym, _ in ranked[:TOP_N_COINS]]

    coins = pd.DataFrame({"coin": top_symbols})
    sync_tracking()

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
            columns=["start_time", "open", "high", "low", "close", "volume", "turnover"],
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
        f"Tracked coins: {len(coin_list)}\n"
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
    global coins, removed_symbols
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

                if cmd == "/list":
                    with data_lock:
                        coin_list = coins["coin"].tolist()

                    if not coin_list:
                        send_alert("⚠️ No coins in list")
                    else:
                        msg = (
                            f"📋 Top {len(coin_list)} Bybit Coins\n"
                            f"Timeframes: 5m / 15m / 60m\n"
                            f"Extreme EMA Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}%\n"
                        )
                        msg += "\n".join(coin_list[:100])
                        send_alert(msg)

                elif cmd == "/check" and len(parts) == 3:
                    check_coin(parts[1].upper(), parts[2])

                elif cmd == "/summary" and len(parts) == 2:
                    show_summary(parts[1])

                elif cmd == "/refresh":
                    with data_lock:
                        rebuild_coin_universe()
                    send_alert(f"🔄 Refreshed top {TOP_N_COINS} Bybit coins\nTracked coins: {len(coins)}")

                elif cmd == "/remove" and len(parts) == 2:
                    coin = parts[1].upper().strip()
                    removed_symbols.add(coin)
                    save_removed_symbols(removed_symbols)
                    with data_lock:
                        rebuild_coin_universe()
                    send_alert(f"❌ {coin} removed from tracking")

                else:
                    send_alert(
                        "Commands:\n"
                        "/list\n"
                        "/check BTCUSDT 5\n"
                        "/summary 5\n"
                        "/refresh\n"
                        "/remove BTCUSDT"
                    )

        except Exception as e:
            print("telegram_listener error:", e)

        time.sleep(2)

# =========================
# AUTO REFRESH
# =========================
def auto_refresh_universe():
    while True:
        try:
            with data_lock:
                rebuild_coin_universe()
        except Exception as e:
            print("auto_refresh_universe error:", e)
        time.sleep(3600)

# =========================
# STARTUP
# =========================
with data_lock:
    rebuild_coin_universe()

threading.Thread(target=telegram_listener, daemon=True).start()
threading.Thread(target=auto_refresh_universe, daemon=True).start()

send_alert(
    f"🚨 EMA EXTREME BOT RUNNING 🚨\n"
    f"Tracked coins: {len(coins)}\n"
    f"Mode: Top {TOP_N_COINS} Bybit linear coins by 24h turnover\n"
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
