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

# Only these timeframes
TIMEFRAMES = ["5", "15", "60"]

# Universal standard for all coins
EXTREME_EMA_DISTANCE_PERCENT = 10.0

# Default settings for auto-fetched coins
DEFAULT_PERCENT = 2.0
DEFAULT_DIRECTION = "both"
DEFAULT_BAND_EXPAND = 50.0
DEFAULT_BAND_SHRINK = 10.0

# Files
MANUAL_COINS_FILE = "coins.csv"
REMOVED_COINS_FILE = "removed_coins.txt"

# =========================
# FILE HELPERS
# =========================
def load_manual_coins() -> pd.DataFrame:
    try:
        df = pd.read_csv(MANUAL_COINS_FILE)
    except Exception:
        df = pd.DataFrame(columns=["coin", "percent", "direction", "band_expand", "band_shrink"])

    required_cols = ["coin", "percent", "direction", "band_expand", "band_shrink"]
    for col in required_cols:
        if col not in df.columns:
            df[col] = None

    return df[required_cols]

def save_manual_coins(df: pd.DataFrame) -> None:
    df.to_csv(MANUAL_COINS_FILE, index=False)

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

manual_coins = load_manual_coins()
removed_symbols = load_removed_symbols()

coins = pd.DataFrame(columns=["coin", "percent", "direction", "band_expand", "band_shrink"])

# =========================
# ALERT TRACKING
# =========================
last_alert: dict[str, dict[str, str | None]] = {}

def init_coin_tracking(coin: str) -> None:
    last_alert[coin] = {tf: None for tf in TIMEFRAMES}

def sync_tracking_with_coin_universe() -> None:
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
        requests.get(
            f"{TELEGRAM_URL}/sendMessage",
            params={"chat_id": CHAT_ID, "text": message},
            timeout=15,
        )
    except Exception as e:
        print("send_alert error:", e)

# =========================
# BYBIT DATA
# =========================
def fetch_all_bybit_linear_symbols() -> list[str]:
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
            r = requests.get(BYBIT_INSTRUMENTS_URL, params=params, timeout=20)
            data = r.json()

            result = data.get("result", {})
            items = result.get("list", [])

            if not items:
                break

            for item in items:
                symbol = item.get("symbol")
                status = item.get("status")
                if symbol and status == "Trading":
                    symbols.append(symbol.upper())

            cursor = result.get("nextPageCursor")
            if not cursor:
                break

        except Exception as e:
            print("fetch_all_bybit_linear_symbols error:", e)
            break

    return sorted(set(symbols))

def rebuild_coin_universe() -> None:
    global coins

    auto_symbols = fetch_all_bybit_linear_symbols()

    auto_rows = []
    for sym in auto_symbols:
        if sym in removed_symbols:
            continue
        auto_rows.append({
            "coin": sym,
            "percent": DEFAULT_PERCENT,
            "direction": DEFAULT_DIRECTION,
            "band_expand": DEFAULT_BAND_EXPAND,
            "band_shrink": DEFAULT_BAND_SHRINK,
        })

    auto_df = pd.DataFrame(auto_rows)

    manual_df = manual_coins.copy()
    if not manual_df.empty:
        manual_df["coin"] = manual_df["coin"].astype(str).str.upper()
        manual_df = manual_df[~manual_df["coin"].isin(removed_symbols)]

    if auto_df.empty and manual_df.empty:
        coins = pd.DataFrame(columns=["coin", "percent", "direction", "band_expand", "band_shrink"])
        return

    merged = auto_df.copy()

    if not manual_df.empty:
        merged = merged[~merged["coin"].isin(manual_df["coin"])]
        merged = pd.concat([merged, manual_df], ignore_index=True)

    merged = merged.drop_duplicates(subset=["coin"], keep="last")
    merged = merged.sort_values("coin").reset_index(drop=True)
    coins = merged[["coin", "percent", "direction", "band_expand", "band_shrink"]]

def get_ohlc(symbol: str, interval: str) -> pd.DataFrame | None:
    try:
        r = requests.get(
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
        result = data.get("result", {})
        rows = result.get("list", [])
        if not rows:
            return None

        rows.reverse()

        df = pd.DataFrame(
            rows,
            columns=["start_time", "open", "high", "low", "close", "volume", "turnover"],
        )

        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df.dropna().reset_index(drop=True)
    except Exception as e:
        print(f"get_ohlc error for {symbol} {interval}: {e}")
        return None

# =========================
# INDICATORS
# =========================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["EMA"] = df["close"].ewm(span=200, adjust=False).mean()
    return df

# =========================
# HELPERS
# =========================
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
    df = get_ohlc(coin, tf)
    if df is None or len(df) < 200:
        send_alert(f"{coin} {tf}m ❌ No data")
        return

    df = add_indicators(df)
    price = df["close"].iloc[-1]
    ema = df["EMA"].iloc[-1]
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
    msg = (
        f"📊 Summary {tf}m\n"
        f"Extreme EMA Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}%\n"
    )

    count = 0
    for _, r in coins.iterrows():
        coin = r["coin"]
        df = get_ohlc(coin, tf)
        if df is None or len(df) < 200:
            continue

        df = add_indicators(df)
        price = df["close"].iloc[-1]
        ema = df["EMA"].iloc[-1]
        distance = ema_distance_percent(price, ema)
        status = classify_extreme(distance)

        if status is None:
            continue

        direction = "ABOVE" if status == "above" else "BELOW"
        msg += f"{coin} | {direction} | D:{distance:.2f}%\n"
        count += 1

        if count >= 50:
            msg += "... more coins omitted"
            break

    if count == 0:
        msg += "No extreme EMA-distance coins found."

    send_alert(msg)

# =========================
# TELEGRAM LISTENER
# =========================
def telegram_listener():
    global manual_coins, coins, removed_symbols
    last_update_id = None

    while True:
        try:
            params = {"timeout": 10}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1

            res = requests.get(f"{TELEGRAM_URL}/getUpdates", params=params, timeout=20).json()

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
                    data = parts[1].split(",")
                    if len(data) != 5:
                        send_alert("❌ Format:\n/add BTCUSDT,2,both,50,10")
                        continue

                    coin, p, d, be, bs = data
                    coin = coin.upper()

                    if coin in removed_symbols:
                        removed_symbols.remove(coin)
                        save_removed_symbols(removed_symbols)

                    manual_coins = manual_coins[manual_coins["coin"].astype(str).str.upper() != coin]
                    manual_coins.loc[len(manual_coins)] = [
                        coin, float(p), d.lower(), float(be), float(bs)
                    ]

                    save_manual_coins(manual_coins)
                    rebuild_coin_universe()
                    sync_tracking_with_coin_universe()

                    send_alert(f"✅ {coin} added / updated")

                elif cmd == "/remove" and len(parts) == 2:
                    coin = parts[1].upper()

                    removed_symbols.add(coin)
                    save_removed_symbols(removed_symbols)

                    manual_coins = manual_coins[manual_coins["coin"].astype(str).str.upper() != coin]
                    save_manual_coins(manual_coins)

                    rebuild_coin_universe()
                    sync_tracking_with_coin_universe()

                    send_alert(f"❌ {coin} removed")

                elif cmd == "/list":
                    if coins.empty:
                        send_alert("⚠️ No coins in list")
                    else:
                        msg = (
                            f"📋 Coins\n"
                            f"Universe: {len(coins)} symbols\n"
                            f"Timeframes: 5m / 15m / 60m\n"
                            f"Extreme EMA Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}%\n"
                        )
                        preview = coins["coin"].tolist()[:100]
                        msg += "\n".join(preview)
                        if len(coins) > 100:
                            msg += f"\n... and {len(coins) - 100} more"
                        send_alert(msg)

                elif cmd == "/check" and len(parts) == 3:
                    check_coin(parts[1].upper(), parts[2])

                elif cmd == "/summary" and len(parts) == 2:
                    show_summary(parts[1])

                elif cmd == "/refresh":
                    rebuild_coin_universe()
                    sync_tracking_with_coin_universe()
                    send_alert(f"🔄 Refreshed Bybit universe\nTotal symbols: {len(coins)}")

                else:
                    send_alert(
                        "Commands:\n"
                        "/add BTCUSDT,2,both,50,10\n"
                        "/remove BTCUSDT\n"
                        "/list\n"
                        "/check BTCUSDT 5\n"
                        "/summary 5\n"
                        "/refresh"
                    )

        except Exception as e:
            print("TG error:", e)

        time.sleep(2)

# =========================
# AUTO REFRESH
# =========================
def auto_refresh_universe():
    while True:
        try:
            rebuild_coin_universe()
            sync_tracking_with_coin_universe()
        except Exception as e:
            print("auto_refresh_universe error:", e)
        time.sleep(3600)

# =========================
# STARTUP
# =========================
rebuild_coin_universe()
sync_tracking_with_coin_universe()

threading.Thread(target=telegram_listener, daemon=True).start()
threading.Thread(target=auto_refresh_universe, daemon=True).start()

send_alert(
    f"🚨 EMA EXTREME BOT RUNNING 🚨\n"
    f"Universe: {len(coins)} Bybit linear symbols\n"
    f"Timeframes: 5m / 15m / 60m\n"
    f"Alert Standard: {EXTREME_EMA_DISTANCE_PERCENT:.2f}% away from EMA200"
)

# =========================
# MAIN LOOP
# =========================
while True:
    try:
        for _, row in coins.iterrows():
            coin = row["coin"]

            for tf in TIMEFRAMES:
                df = get_ohlc(coin, tf)
                if df is None or len(df) < 200:
                    continue

                df = add_indicators(df)
                price = df["close"].iloc[-1]
                ema = df["EMA"].iloc[-1]
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

        time.sleep(60)

    except Exception as e:
        print("Main error:", e)
        time.sleep(30)
