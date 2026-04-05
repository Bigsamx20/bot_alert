import os
import time
import tempfile
import threading
import requests
import pandas as pd
import matplotlib.pyplot as plt

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

# Giant candle only on this timeframe
GIANT_CANDLE_TIMEFRAME = "60"

# Fixed RSI for all coins
RSI_OVERBOUGHT = 90
RSI_OVERSOLD = 10
RSI_TOLERANCE = 0.3

# EMA distance classification
EMA_CLASSIFICATION_TIMEFRAMES = {"5", "15", "60"}
EMA_STRONG_MIN = 4.0
EMA_VERY_STRONG_MIN = 6.0
EMA_OVERSTRETCHED_MIN = 10.0

# Bollinger width percentage classification
BB_VERY_HIGH_MIN = 12.0
BB_EXTREME_MIN = 20.0

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

    for col in ["rsi_overbought", "rsi_oversold"]:
        if col in df.columns:
            df = df.drop(columns=[col])

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
last_alert: dict[str, dict[str, dict[str, str | None]]] = {}

def init_coin_tracking(coin: str) -> None:
    last_alert[coin] = {
        tf: {
            "ema": None,
            "ema_strength": None,
            "rsi": None,
            "bb": None,
            "candle": None,
        }
        for tf in TIMEFRAMES
    }

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

def send_image(path: str, caption: str) -> None:
    try:
        with open(path, "rb") as img:
            requests.post(
                f"{TELEGRAM_URL}/sendPhoto",
                params={"chat_id": CHAT_ID, "caption": caption},
                files={"photo": img},
                timeout=30,
            )
    except Exception as e:
        print("send_image error:", e)

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

    df["SMA20"] = df["close"].rolling(20).mean()
    df["STD20"] = df["close"].rolling(20).std()
    df["Upper"] = df["SMA20"] + 2 * df["STD20"]
    df["Lower"] = df["SMA20"] - 2 * df["STD20"]

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-10)
    df["RSI"] = 100 - (100 / (1 + rs))

    df["body_size"] = (df["close"] - df["open"]).abs()
    df["avg_body_size"] = df["body_size"].rolling(20).mean()

    return df

# =========================
# HELPERS
# =========================
def ema_distance_percent(price: float, ema: float) -> float:
    if ema == 0:
        return 0.0
    return ((price - ema) / ema) * 100

def classify_ema_distance(distance_pct: float) -> str | None:
    abs_dist = abs(distance_pct)
    if abs_dist >= EMA_OVERSTRETCHED_MIN:
        return "overstretched"
    if abs_dist >= EMA_VERY_STRONG_MIN:
        return "very_strong"
    if abs_dist >= EMA_STRONG_MIN:
        return "strong"
    return None

def classify_direction(distance_pct: float) -> str:
    return "above" if distance_pct > 0 else "below"

def format_strength_label(level: str) -> str:
    return {
        "strong": "STRONG",
        "very_strong": "VERY STRONG",
        "overstretched": "OVERSTRETCHED",
    }.get(level, "NORMAL")

def get_strength_details(distance_pct: float, tf: str):
    if tf not in EMA_CLASSIFICATION_TIMEFRAMES:
        return None, None, None

    level = classify_ema_distance(distance_pct)
    if not level:
        return None, None, None

    direction = classify_direction(distance_pct)
    label = format_strength_label(level)
    return level, direction, label

def classify_bb_width_percent(width_percent: float) -> str | None:
    if width_percent >= BB_EXTREME_MIN:
        return "EXTREME"
    if width_percent >= BB_VERY_HIGH_MIN:
        return "VERY HIGH"
    return None

# =========================
# CHARTS
# =========================
def plot_standard_chart(df: pd.DataFrame, coin: str, tf: str) -> str:
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
    ax2.axhline(RSI_OVERBOUGHT, linestyle="--")
    ax2.axhline(RSI_OVERSOLD, linestyle="--")
    ax2.set_ylim(0, 100)
    ax2.legend()

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.tight_layout()
    plt.savefig(tmp.name)
    plt.close()
    return tmp.name

def plot_giant_candle_chart(df: pd.DataFrame, coin: str, tf: str) -> str:
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
    ax2.axhline(RSI_OVERBOUGHT, linestyle="--")
    ax2.axhline(RSI_OVERSOLD, linestyle="--")
    ax2.set_ylim(0, 100)
    ax2.legend()

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.tight_layout()
    plt.savefig(tmp.name)
    plt.close()
    return tmp.name

# =========================
# COMMANDS
# =========================
def check_bollinger_width(coin: str, tf: str) -> None:
    df = get_ohlc(coin, tf)
    if df is None or len(df) < 20:
        send_alert(f"{coin} {tf}m ❌ No data")
        return

    df = add_indicators(df)
    chart = plot_standard_chart(df, coin, tf)

    upper = df["Upper"].iloc[-1]
    lower = df["Lower"].iloc[-1]
    middle = df["SMA20"].iloc[-1]
    width_percent = ((upper - lower) / middle) * 100 if pd.notna(middle) and middle != 0 else 0.0
    width_label = classify_bb_width_percent(width_percent) or "NORMAL"

    price = df["close"].iloc[-1]
    ema = df["EMA"].iloc[-1]
    distance = ema_distance_percent(price, ema)

    _level, direction, strength_label = get_strength_details(distance, tf)
    strength_line = ""
    if strength_label:
        direction_text = "ABOVE EMA 🚀" if direction == "above" else "BELOW EMA 🔻"
        strength_line = f"Strength: {strength_label} ({direction_text})\n"

    send_image(
        chart,
        f"📊 {coin} | {tf}m\n"
        f"Manual Check\n"
        f"Price: {price:.2f}\n"
        f"EMA Distance: {distance:.2f}%\n"
        f"{strength_line}"
        f"Bollinger Width: {width_percent:.2f}%\n"
        f"Width Status: {width_label}\n"
        f"Upper: {upper:.2f}\n"
        f"Lower: {lower:.2f}"
    )

    try:
        os.unlink(chart)
    except OSError:
        pass

def show_summary(tf: str) -> None:
    msg = f"📊 Summary {tf}m\nRSI Zones: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"

    for _, r in coins.iterrows():
        coin = r["coin"]
        df = get_ohlc(coin, tf)
        if df is None or len(df) < 20:
            msg += f"{coin}: no data\n"
            continue

        df = add_indicators(df)
        upper = df["Upper"].iloc[-1]
        lower = df["Lower"].iloc[-1]
        middle = df["SMA20"].iloc[-1]
        width_percent = ((upper - lower) / middle) * 100 if pd.notna(middle) and middle != 0 else 0.0

        price = df["close"].iloc[-1]
        ema = df["EMA"].iloc[-1]
        distance = ema_distance_percent(price, ema)

        _level, _direction, strength_label = get_strength_details(distance, tf)
        strength_part = f" | S:{strength_label}" if strength_label else ""

        width_label = classify_bb_width_percent(width_percent)
        width_part = f" | BW:{width_percent:.2f}%"
        if width_label:
            width_part += f" {width_label}"

        msg += f"{coin} | P:{price:.2f} | D:{distance:.2f}%{strength_part}{width_part}\n"

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

                    send_alert(f"✅ {coin} added / updated\nRSI fixed at {RSI_OVERBOUGHT}/{RSI_OVERSOLD}")

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
                            f"RSI fixed: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
                        )
                        preview = coins["coin"].tolist()[:100]
                        msg += "\n".join(preview)
                        if len(coins) > 100:
                            msg += f"\n... and {len(coins) - 100} more"
                        send_alert(msg)

                elif cmd == "/check" and len(parts) == 3:
                    check_bollinger_width(parts[1].upper(), parts[2])

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
                        "/refresh\n\n"
                        f"RSI is fixed at {RSI_OVERBOUGHT}/{RSI_OVERSOLD}"
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
    f"🚨 FULL BOT RUNNING 🚨\n"
    f"Universe: {len(coins)} Bybit linear symbols\n"
    f"RSI fixed at {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
    f"Timeframes: 5m / 15m / 60m\n"
    f"Giant candles: 1h only\n"
    f"EMA strength alerts active on 5m / 15m / 60m\n"
    f"Bollinger width alerts active at 12%+"
)

# =========================
# MAIN LOOP
# =========================
while True:
    try:
        for _, row in coins.iterrows():
            coin = row["coin"]
            percent = float(row["percent"])
            direction = str(row["direction"]).lower()

            for tf in TIMEFRAMES:
                df = get_ohlc(coin, tf)
                if df is None or len(df) < 200:
                    continue

                df = add_indicators(df)

                price = df["close"].iloc[-1]
                ema = df["EMA"].iloc[-1]
                distance_pct = ema_distance_percent(price, ema)

                # ---------- EMA ----------
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
                            f"Distance: {distance_pct:.2f}%\n"
                            f"Threshold: {threshold_above:.2f}"
                        )
                    else:
                        send_alert(
                            f"📊 {coin} | {tf}m\n"
                            f"EMA BELOW 🔻\n"
                            f"Price: {price:.2f}\n"
                            f"EMA: {ema:.2f}\n"
                            f"Distance: {distance_pct:.2f}%\n"
                            f"Threshold: {threshold_below:.2f}"
                        )
                    last_alert[coin][tf]["ema"] = ema_signal

                if threshold_below <= price <= threshold_above:
                    last_alert[coin][tf]["ema"] = None

                # ---------- EMA DISTANCE STRENGTH ----------
                strength_signal = None
                strength_direction = None

                if tf in EMA_CLASSIFICATION_TIMEFRAMES:
                    strength_level = classify_ema_distance(distance_pct)
                    if strength_level:
                        strength_direction = classify_direction(distance_pct)
                        strength_signal = f"{strength_direction}_{strength_level}"

                if strength_signal and last_alert[coin][tf]["ema_strength"] != strength_signal:
                    level = strength_signal.split("_", 1)[1]
                    direction_text = "ABOVE EMA 🚀" if strength_direction == "above" else "BELOW EMA 🔻"

                    send_alert(
                        f"📊 {coin} | {tf}m\n"
                        f"EMA DISTANCE ALERT\n"
                        f"Strength: {format_strength_label(level)}\n"
                        f"Direction: {direction_text}\n"
                        f"Distance: {distance_pct:.2f}%\n"
                        f"Price: {price:.2f}\n"
                        f"EMA: {ema:.2f}"
                    )
                    last_alert[coin][tf]["ema_strength"] = strength_signal

                if tf in EMA_CLASSIFICATION_TIMEFRAMES and classify_ema_distance(distance_pct) is None:
                    last_alert[coin][tf]["ema_strength"] = None

                # ---------- RSI ----------
                rsi = df["RSI"].iloc[-1]
                rsi_signal = None

                if abs(rsi - RSI_OVERBOUGHT) < RSI_TOLERANCE:
                    rsi_signal = "high"
                elif abs(rsi - RSI_OVERSOLD) < RSI_TOLERANCE:
                    rsi_signal = "low"

                if rsi_signal and last_alert[coin][tf]["rsi"] != rsi_signal:
                    if rsi_signal == "high":
                        send_alert(
                            f"📊 {coin} | {tf}m\n"
                            f"RSI OVERBOUGHT 🔴\n"
                            f"RSI: {rsi:.2f}\n"
                            f"Zone: {RSI_OVERBOUGHT} ± {RSI_TOLERANCE}\n"
                            f"Price: {price:.2f}"
                        )
                    else:
                        send_alert(
                            f"📊 {coin} | {tf}m\n"
                            f"RSI OVERSOLD 🟢\n"
                            f"RSI: {rsi:.2f}\n"
                            f"Zone: {RSI_OVERSOLD} ± {RSI_TOLERANCE}\n"
                            f"Price: {price:.2f}"
                        )
                    last_alert[coin][tf]["rsi"] = rsi_signal

                if rsi_signal is None:
                    last_alert[coin][tf]["rsi"] = None

                # ---------- BOLLINGER WIDTH PERCENT ----------
                upper = df["Upper"].iloc[-1]
                lower = df["Lower"].iloc[-1]
                middle = df["SMA20"].iloc[-1]

                bb_signal = None
                bb_strength = None
                width_percent = None

                if pd.notna(middle) and middle != 0:
                    width_percent = ((upper - lower) / middle) * 100

                    if width_percent >= BB_EXTREME_MIN:
                        bb_signal = "high_width"
                        bb_strength = "EXTREME"
                    elif width_percent >= BB_VERY_HIGH_MIN:
                        bb_signal = "high_width"
                        bb_strength = "VERY HIGH"

                if bb_signal and last_alert[coin][tf]["bb"] != bb_strength:
                    chart = plot_standard_chart(df, coin, tf)

                    _level, bb_strength_direction, ema_strength_label = get_strength_details(distance_pct, tf)
                    strength_line = ""
                    if ema_strength_label:
                        direction_text = "ABOVE EMA 🚀" if bb_strength_direction == "above" else "BELOW EMA 🔻"
                        strength_line = (
                            f"EMA Strength: {ema_strength_label}\n"
                            f"EMA Direction: {direction_text}\n"
                        )

                    send_image(
                        chart,
                        f"📊 {coin} | {tf}m\n"
                        f"Bollinger Alert\n"
                        f"Bollinger Width: {width_percent:.2f}%\n"
                        f"Width Status: {bb_strength}\n"
                        f"{strength_line}"
                        f"Price: {price:.2f}\n"
                        f"EMA Distance: {distance_pct:.2f}%\n"
                        f"Upper: {upper:.2f}\n"
                        f"Lower: {lower:.2f}"
                    )

                    try:
                        os.unlink(chart)
                    except OSError:
                        pass

                    last_alert[coin][tf]["bb"] = bb_strength

                if width_percent is None or width_percent < BB_VERY_HIGH_MIN:
                    last_alert[coin][tf]["bb"] = None

                # ---------- GIANT CANDLE (1H ONLY) ----------
                if tf == GIANT_CANDLE_TIMEFRAME:
                    current_body = df["body_size"].iloc[-1]
                    avg_body = df["avg_body_size"].iloc[-2] if len(df) > 21 else None

                    candle_signal = None
                    multiplier = None

                    if avg_body is not None and avg_body > 0:
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

                        try:
                            os.unlink(chart)
                        except OSError:
                            pass

                        last_alert[coin][tf]["candle"] = candle_signal

                    if candle_signal is None:
                        last_alert[coin][tf]["candle"] = None

        time.sleep(60)

    except Exception as e:
        print("Main error:", e)
        time.sleep(30)
