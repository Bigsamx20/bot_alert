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
    raise SystemExit

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"

# ---------------- FIXED RSI SETTINGS ----------------
RSI_OVERBOUGHT = 90
RSI_OVERSOLD = 10
RSI_TOLERANCE = 0.3

# ---------------- EMA DISTANCE CLASSIFICATION ----------------
EMA_CLASSIFICATION_TIMEFRAMES = {"5", "15", "60"}

EMA_STRONG_MIN = 4.0
EMA_VERY_STRONG_MIN = 6.0
EMA_OVERSTRETCHED_MIN = 10.0

# ---------------- FILES ----------------
MANUAL_FILE = "coins.csv"

# manual coins file keeps only your custom overrides / added coins
try:
    manual_coins = pd.read_csv(MANUAL_FILE)
except Exception:
    manual_coins = pd.DataFrame(columns=[
        "coin", "percent", "direction", "band_expand", "band_shrink"
    ])

required_cols = ["coin", "percent", "direction", "band_expand", "band_shrink"]
for col in required_cols:
    if col not in manual_coins.columns:
        manual_coins[col] = None

manual_coins = manual_coins[required_cols]

timeframes = ["1", "5", "15", "60"]

# default settings for auto-fetched coins
DEFAULT_PERCENT = 2.0
DEFAULT_DIRECTION = "both"
DEFAULT_BAND_EXPAND = 50.0
DEFAULT_BAND_SHRINK = 10.0

# this will hold the merged universe used by the bot
coins = pd.DataFrame(columns=required_cols)

# coins removed manually should stay removed even if Bybit still lists them
removed_symbols = set()

# ---------------- SAVE / LOAD ----------------
def save_manual_coins():
    manual_coins.to_csv(MANUAL_FILE, index=False)

# ---------------- TELEGRAM SEND ----------------
def send_alert(msg: str):
    try:
        requests.get(
            f"{TELEGRAM_URL}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        print("send_alert error:", e)

def send_image(path: str, caption: str):
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

# ---------------- BYBIT AUTO-FETCH ----------------
def fetch_all_bybit_linear_symbols():
    """
    Fetch all Trading linear instruments from Bybit using pagination.
    Official docs: /v5/market/instruments-info with category=linear,
    limit up to 1000 and cursor pagination. status can filter trading pairs.
    """
    symbols = []
    cursor = None

    while True:
        params = {
            "category": "linear",
            "limit": 1000,
            "status": "Trading",
        }
        if cursor:
            params["cursor"] = cursor

        try:
            r = requests.get(
                "https://api.bybit.com/v5/market/instruments-info",
                params=params,
                timeout=15
            )
            data = r.json()

            if "result" not in data or "list" not in data["result"]:
                break

            items = data["result"]["list"]
            for item in items:
                symbol = item.get("symbol")
                status = item.get("status")
                if symbol and status == "Trading":
                    symbols.append(symbol.upper())

            cursor = data["result"].get("nextPageCursor")
            if not cursor:
                break

        except Exception as e:
            print("fetch_all_bybit_linear_symbols error:", e)
            break

    return sorted(set(symbols))

def rebuild_coin_universe():
    """
    Merge auto-fetched Bybit symbols with manual overrides.
    Manual rows override defaults for matching symbols.
    """
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

    # normalize manual coins
    if not manual_coins.empty:
        temp_manual = manual_coins.copy()
        temp_manual["coin"] = temp_manual["coin"].str.upper()
    else:
        temp_manual = manual_coins.copy()

    # remove any manually removed symbols
    if not temp_manual.empty:
        temp_manual = temp_manual[~temp_manual["coin"].isin(removed_symbols)]

    # merge: manual overrides auto
    if auto_df.empty and temp_manual.empty:
        coins = pd.DataFrame(columns=required_cols)
        return

    merged = auto_df.copy()

    if not temp_manual.empty:
        merged = merged[~merged["coin"].isin(temp_manual["coin"])]
        merged = pd.concat([merged, temp_manual], ignore_index=True)

    merged = merged.drop_duplicates(subset=["coin"], keep="last")
    merged = merged.sort_values("coin").reset_index(drop=True)

    coins = merged[required_cols]

# ---------------- TRACKING ----------------
last_alert = {}

def init_coin_tracking(coin: str):
    last_alert[coin] = {
        tf: {
            "ema": None,
            "ema_strength": None,
            "rsi": None,
            "bb": None,
            "candle": None
        }
        for tf in timeframes
    }

def sync_tracking_with_coin_universe():
    existing = set(last_alert.keys())
    current = set(coins["coin"].dropna().str.upper())

    for coin in current - existing:
        init_coin_tracking(coin)

    for coin in existing - current:
        del last_alert[coin]

# build initial universe
rebuild_coin_universe()
sync_tracking_with_coin_universe()

# ---------------- GET DATA ----------------
def get_ohlc(symbol: str, interval: str):
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={
                "category": "linear",
                "symbol": symbol.upper(),
                "interval": interval,
                "limit": 200
            },
            timeout=10
        )
        data = r.json()

        if "result" not in data or "list" not in data["result"]:
            return None

        rows = data["result"]["list"]
        rows.reverse()

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
def add_indicators(df: pd.DataFrame):
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

# ---------------- HELPERS ----------------
def ema_distance_percent(price: float, ema: float) -> float:
    if ema == 0:
        return 0.0
    return ((price - ema) / ema) * 100

def classify_ema_distance(distance_pct: float):
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
    if level == "strong":
        return "STRONG"
    if level == "very_strong":
        return "VERY STRONG"
    if level == "overstretched":
        return "OVERSTRETCHED"
    return "NORMAL"

def get_strength_details(distance_pct: float, tf: str):
    if tf not in EMA_CLASSIFICATION_TIMEFRAMES:
        return None, None, None

    level = classify_ema_distance(distance_pct)
    if not level:
        return None, None, None

    direction = classify_direction(distance_pct)
    label = format_strength_label(level)
    return level, direction, label

# ---------------- CHARTS ----------------
def plot_standard_chart(df: pd.DataFrame, coin: str, tf: str):
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

def plot_giant_candle_chart(df: pd.DataFrame, coin: str, tf: str):
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

# ---------------- COMMANDS ----------------
def check_bollinger_width(coin: str, tf: str):
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
        f"Width: {width:.2f}\n"
        f"Upper: {upper:.2f}\n"
        f"Lower: {lower:.2f}"
    )

    os.unlink(chart)

def show_summary(tf: str):
    msg = f"📊 Summary {tf}m\nRSI Zones: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
    for _, r in coins.iterrows():
        coin = r["coin"]
        df = get_ohlc(coin, tf)
        if df is None or len(df) < 20:
            msg += f"{coin}: no data\n"
            continue

        df = add_indicators(df)
        width = df["Upper"].iloc[-1] - df["Lower"].iloc[-1]
        price = df["close"].iloc[-1]
        ema = df["EMA"].iloc[-1]
        distance = ema_distance_percent(price, ema)

        _level, _direction, strength_label = get_strength_details(distance, tf)
        strength_part = f" | S:{strength_label}" if strength_label else ""

        msg += f"{coin} | P:{price:.2f} | D:{distance:.2f}%{strength_part} | W:{width:.2f}\n"

    send_alert(msg)

# ---------------- TELEGRAM LISTENER ----------------
def telegram_listener():
    global manual_coins, coins
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

                if parts[0] == "/add" and len(parts) == 2:
                    data = parts[1].split(",")

                    if len(data) != 5:
                        send_alert("❌ Format:\n/add BTCUSDT,2,both,50,10")
                        continue

                    coin, p, d, be, bs = data
                    coin = coin.upper()

                    if coin in removed_symbols:
                        removed_symbols.remove(coin)

                    manual_coins = manual_coins[manual_coins["coin"].str.upper() != coin]
                    manual_coins.loc[len(manual_coins)] = [
                        coin, float(p), d.lower(), float(be), float(bs)
                    ]

                    save_manual_coins()
                    rebuild_coin_universe()
                    sync_tracking_with_coin_universe()

                    send_alert(f"✅ {coin} added / updated\nRSI fixed at {RSI_OVERBOUGHT}/{RSI_OVERSOLD}")

                elif parts[0] == "/remove" and len(parts) == 2:
                    coin = parts[1].upper()

                    removed_symbols.add(coin)
                    manual_coins = manual_coins[manual_coins["coin"].str.upper() != coin]
                    save_manual_coins()

                    rebuild_coin_universe()
                    sync_tracking_with_coin_universe()

                    send_alert(f"❌ {coin} removed")

                elif parts[0] == "/list":
                    if coins.empty:
                        send_alert("⚠️ No coins in list")
                    else:
                        msg = (
                            f"📋 Coins\n"
                            f"Universe: {len(coins)} symbols\n"
                            f"RSI fixed: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
                        )
                        # keep list manageable in Telegram
                        preview = coins["coin"].tolist()[:100]
                        msg += "\n".join(preview)
                        if len(coins) > 100:
                            msg += f"\n... and {len(coins) - 100} more"
                        send_alert(msg)

                elif parts[0] == "/check" and len(parts) == 3:
                    check_bollinger_width(parts[1].upper(), parts[2])

                elif parts[0] == "/summary" and len(parts) == 2:
                    show_summary(parts[1])

                elif parts[0] == "/refresh":
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

threading.Thread(target=telegram_listener, daemon=True).start()

# ---------------- AUTO REFRESH THREAD ----------------
def auto_refresh_universe():
    while True:
        try:
            rebuild_coin_universe()
            sync_tracking_with_coin_universe()
        except Exception as e:
            print("auto_refresh_universe error:", e)
        time.sleep(3600)  # refresh every hour

threading.Thread(target=auto_refresh_universe, daemon=True).start()

# ---------------- START ----------------
send_alert(
    f"🚨 FULL BOT RUNNING 🚨\n"
    f"Universe: {len(coins)} Bybit linear symbols\n"
    f"RSI fixed at {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
    f"EMA strength alerts active on 5m / 15m / 60m"
)

# ---------------- MAIN LOOP ----------------
while True:
    try:
        for _, row in coins.iterrows():
            coin = row["coin"]
            percent = float(row["percent"])
            direction = str(row["direction"]).lower()
            expand = float(row["band_expand"])
            shrink = float(row["band_shrink"])

            for tf in timeframes:
                df = get_ohlc(coin, tf)
                if df is None or len(df) < 200:
                    continue

                df = add_indicators(df)

                price = df["close"].iloc[-1]
                ema = df["EMA"].iloc[-1]
                distance_pct = ema_distance_percent(price, ema)

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

                # ---------------- EMA DISTANCE STRENGTH ----------------
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

                if tf in EMA_CLASSIFICATION_TIMEFRAMES:
                    if classify_ema_distance(distance_pct) is None:
                        last_alert[coin][tf]["ema_strength"] = None

                # ---------------- RSI ----------------
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

                # ---------------- BOLLINGER ----------------
                upper = df["Upper"].iloc[-1]
                lower = df["Lower"].iloc[-1]
                width = upper - lower

                bb_signal = None
                if width > expand:
                    bb_signal = "wide"
                elif width < shrink:
                    bb_signal = "tight"

                if bb_signal and last_alert[coin][tf]["bb"] != bb_signal:
                    chart = plot_standard_chart(df, coin, tf)

                    _level, bb_strength_direction, bb_strength_label = get_strength_details(distance_pct, tf)
                    strength_line = ""
                    if bb_strength_label:
                        direction_text = "ABOVE EMA 🚀" if bb_strength_direction == "above" else "BELOW EMA 🔻"
                        strength_line = (
                            f"Strength: {bb_strength_label}\n"
                            f"EMA Direction: {direction_text}\n"
                        )

                    send_image(
                        chart,
                        f"📊 {coin} | {tf}m\n"
                        f"Bollinger Alert\n"
                        f"{strength_line}"
                        f"Width: {width:.2f}\n"
                        f"Price: {price:.2f}\n"
                        f"EMA Distance: {distance_pct:.2f}%\n"
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
