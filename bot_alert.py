print("🔥 BOT STARTED")

import os
import json
import time
import threading
from datetime import datetime, timezone

import requests
import websocket
import pandas as pd

# ============================================================
# ENVIRONMENT
# ============================================================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

print("TOKEN LOADED:", TOKEN is not None)
print("CHAT_ID LOADED:", CHAT_ID is not None)

# ============================================================
# CONFIG
# ============================================================

WS_URL = "wss://stream.bybit.com/v5/public/linear"
BYBIT_REST = "https://api.bybit.com"

TIMEFRAMES = {
    "1": "1m",
    "5": "5m",
    "15": "15m",
    "60": "1h",
}

EMA_PERIOD = 200
HISTORY_LIMIT = 250
TOP_SYMBOLS_COUNT = 50

TESTING_MODE = False

# ============================================================
# SETTINGS
# ============================================================

# LIVE MODE: 65% deviation from EMA200
REAL_EMA_DEVIATION = 0.65

# TEST MODE: easier to trigger
TEST_EMA_DEVIATION = 0.01  # 1%

EMA_DEVIATION = REAL_EMA_DEVIATION

# ============================================================
# GLOBALS
# ============================================================

candles = {}       # (symbol, tf) -> DataFrame
region_state = {}  # (symbol, tf) -> "ABOVE" / "BELOW" / "NONE"
SYMBOLS = []
data_lock = threading.Lock()

# ============================================================
# MODE CONTROL
# ============================================================

def apply_mode():
    global EMA_DEVIATION
    EMA_DEVIATION = TEST_EMA_DEVIATION if TESTING_MODE else REAL_EMA_DEVIATION

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(msg: str, chat_id=None):
    target = chat_id if chat_id is not None else CHAT_ID

    if not TOKEN or not target:
        print("TELEGRAM SKIPPED: Missing TOKEN or CHAT_ID")
        return

    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": str(target),
            "text": msg,
        }
        r = requests.post(url, json=payload, timeout=10)
        print("TELEGRAM RESPONSE:", r.status_code, r.text)
    except Exception as e:
        print("TELEGRAM ERROR:", e)

def log_and_alert(msg: str, chat_id=None):
    print(msg)
    send_telegram(msg, chat_id)

# ============================================================
# TELEGRAM COMMAND LISTENER
# ============================================================

def telegram_command_listener():
    if not TOKEN:
        print("TELEGRAM LISTENER DISABLED: No TOKEN")
        return

    global TESTING_MODE
    last_update_id = None
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

    print("TELEGRAM COMMAND LISTENER STARTED")

    while True:
        try:
            params = {"timeout": 20}
            if last_update_id is not None:
                params["offset"] = last_update_id

            r = requests.get(url, params=params, timeout=30)
            data = r.json()

            if "result" not in data:
                print("TELEGRAM GETUPDATES BAD RESPONSE:", data)
                time.sleep(2)
                continue

            for update in data["result"]:
                last_update_id = update["update_id"] + 1

                if "message" not in update:
                    continue

                msg = update["message"]
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")

                print("TELEGRAM UPDATE:", text, "FROM CHAT:", chat_id)

                if text == "/test":
                    send_telegram("🧪 TEST COMMAND RECEIVED — Bot is working!", chat_id)

                elif text == "/testmode_on":
                    TESTING_MODE = True
                    apply_mode()
                    send_telegram(
                        "🧪 TESTING MODE ENABLED\n"
                        f"EMA200 deviation threshold: {EMA_DEVIATION * 100:.2f}%\n"
                        f"Alert logic: trigger once when entering region",
                        chat_id,
                    )

                elif text == "/testmode_off":
                    TESTING_MODE = False
                    apply_mode()
                    send_telegram(
                        "✅ LIVE MODE ENABLED\n"
                        f"EMA200 deviation threshold: {EMA_DEVIATION * 100:.2f}%\n"
                        f"Alert logic: trigger once when entering region",
                        chat_id,
                    )

                elif text == "/mode":
                    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"
                    send_telegram(
                        f"📌 Current mode: {mode}\n"
                        f"EMA200 deviation threshold: {EMA_DEVIATION * 100:.2f}%\n"
                        f"Alert logic: trigger once when entering region",
                        chat_id,
                    )

        except Exception as e:
            print("TELEGRAM LISTENER ERROR:", e)
            time.sleep(3)

# ============================================================
# BYBIT HELPERS
# ============================================================

def fetch_top_linear_usdt(count=50):
    url = f"{BYBIT_REST}/v5/market/tickers"
    params = {"category": "linear"}

    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()

        if data.get("retCode") != 0:
            print("BYBIT TICKERS ERROR:", data)
            return []

        rows = data.get("result", {}).get("list", [])
        rows = [row for row in rows if row.get("symbol", "").endswith("USDT")]

        def turnover_value(row):
            try:
                return float(row.get("turnover24h", 0))
            except Exception:
                return 0.0

        rows.sort(key=turnover_value, reverse=True)
        symbols = [row["symbol"] for row in rows[:count]]

        print(f"TOP {count} LINEAR USDT SYMBOLS:", symbols)
        return symbols

    except Exception as e:
        print("FETCH TOP SYMBOLS ERROR:", e)
        return []

def fetch_historical_klines(symbol: str, interval: str, limit: int = 250):
    url = f"{BYBIT_REST}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()

        if data.get("retCode") != 0:
            print(f"HISTORY ERROR {symbol} {interval}:", data)
            return None

        rows = data.get("result", {}).get("list", [])
        if not rows:
            print(f"NO HISTORY: {symbol} {interval}")
            return None

        rows.reverse()

        parsed = []
        for row in rows:
            parsed.append({
                "time": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })

        df = pd.DataFrame(parsed)
        df = df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
        return df

    except Exception as e:
        print(f"FETCH HISTORY ERROR {symbol} {interval}:", e)
        return None

def bootstrap_history():
    print("BOOTSTRAPPING HISTORICAL CANDLES...")

    total_loaded = 0

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES.keys():
            df = fetch_historical_klines(symbol, tf, HISTORY_LIMIT)
            if df is not None and not df.empty:
                with data_lock:
                    candles[(symbol, tf)] = df
                    region_state[(symbol, tf)] = "NONE"
                total_loaded += 1
                print(f"BOOTSTRAP OK: {symbol} {tf} -> {len(df)} candles")
            else:
                print(f"BOOTSTRAP FAILED: {symbol} {tf}")
            time.sleep(0.03)

    print("BOOTSTRAP COMPLETE. STREAMS LOADED:", total_loaded)

# ============================================================
# INDICATORS
# ============================================================

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

# ============================================================
# SIGNAL LOGIC
# ============================================================

def get_price_region(df: pd.DataFrame):
    if len(df) < EMA_PERIOD:
        return None

    work = df.copy()
    work["ema200"] = calc_ema(work["close"], EMA_PERIOD)

    last = work.iloc[-1]
    price = float(last["close"])
    ema200 = float(last["ema200"])

    if ema200 <= 0:
        return None

    deviation = (price - ema200) / ema200

    if deviation >= EMA_DEVIATION:
        return {
            "region": "ABOVE",
            "price": price,
            "ema200": ema200,
            "deviation_pct": deviation * 100.0,
        }

    if deviation <= -EMA_DEVIATION:
        return {
            "region": "BELOW",
            "price": price,
            "ema200": ema200,
            "deviation_pct": deviation * 100.0,
        }

    return {
        "region": "NONE",
        "price": price,
        "ema200": ema200,
        "deviation_pct": deviation * 100.0,
    }

def build_alert_message(symbol: str, tf_label: str, info: dict):
    region = info["region"]
    price = info["price"]
    ema200 = info["ema200"]
    deviation_pct = info["deviation_pct"]

    if region == "ABOVE":
        return (
            f"🚨 EMA200 REGION ENTERED | {symbol} {tf_label}\n"
            f"Direction: ABOVE\n"
            f"Price: {price:.6f}\n"
            f"EMA200: {ema200:.6f}\n"
            f"Deviation: +{abs(deviation_pct):.2f}%\n"
            f"Threshold: {EMA_DEVIATION * 100:.2f}%"
        )

    if region == "BELOW":
        return (
            f"🚨 EMA200 REGION ENTERED | {symbol} {tf_label}\n"
            f"Direction: BELOW\n"
            f"Price: {price:.6f}\n"
            f"EMA200: {ema200:.6f}\n"
            f"Deviation: -{abs(deviation_pct):.2f}%\n"
            f"Threshold: {EMA_DEVIATION * 100:.2f}%"
        )

    return None

def process_region_transition(symbol: str, tf: str, tf_label: str, df: pd.DataFrame):
    info = get_price_region(df)
    if info is None:
        return

    new_region = info["region"]
    key = (symbol, tf)
    old_region = region_state.get(key, "NONE")

    # No repeat alert while staying in same region
    if new_region == old_region:
        return

    # Update state
    region_state[key] = new_region

    # Alert only when entering ABOVE or BELOW region
    if new_region in ("ABOVE", "BELOW"):
        msg = build_alert_message(symbol, tf_label, info)
        if msg:
            log_and_alert(msg)

# ============================================================
# CANDLE STORAGE
# ============================================================

def update_candles(symbol: str, tf: str, kline: dict):
    key = (symbol, tf)

    ts = datetime.fromtimestamp(int(kline["start"]) / 1000, tz=timezone.utc)

    row = {
        "time": ts,
        "open": float(kline["open"]),
        "high": float(kline["high"]),
        "low": float(kline["low"]),
        "close": float(kline["close"]),
        "volume": float(kline["volume"]),
    }

    with data_lock:
        if key not in candles or candles[key].empty:
            candles[key] = pd.DataFrame([row])
            return candles[key]

        df = candles[key]

        if df.iloc[-1]["time"] == ts:
            df.iloc[-1] = [
                row["time"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            ]
        else:
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            if len(df) > HISTORY_LIMIT + 50:
                df = df.iloc[-(HISTORY_LIMIT + 20):].reset_index(drop=True)
            candles[key] = df

        return candles[key]

# ============================================================
# WEBSOCKET
# ============================================================

def on_message(ws, message):
    try:
        data = json.loads(message)
    except Exception as e:
        print("WS PARSE ERROR:", e)
        return

    if "topic" not in data or "data" not in data:
        return

    parts = data["topic"].split(".")
    if len(parts) != 3:
        return

    _, tf, symbol = parts
    tf_label = TIMEFRAMES.get(tf, tf)

    klines = data["data"]
    if isinstance(klines, dict):
        klines = [klines]

    for kline in klines:
        try:
            if not kline.get("confirm", False):
                continue  # closed candles only

            df = update_candles(symbol, tf, kline)
            process_region_transition(symbol, tf, tf_label, df)

        except Exception as e:
            print(f"ON_MESSAGE PROCESS ERROR {symbol} {tf_label}:", e)

def on_error(ws, error):
    print("WS ERROR:", error)

def on_close(ws, code, msg):
    print("WS CLOSED:", code, msg)

def on_open(ws):
    print("WS CONNECTED")

    args = []
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES.keys():
            args.append(f"kline.{tf}.{symbol}")

    try:
        ws.send(json.dumps({"op": "subscribe", "args": args}))
        print("SUBSCRIBED TO", len(args), "STREAMS")
    except Exception as e:
        print("SUBSCRIBE ERROR:", e)

def ws_forever():
    while True:
        try:
            print("STARTING WEBSOCKET CONNECTION...")
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print("WEBSOCKET LOOP ERROR:", e)

        print("RECONNECTING WEBSOCKET IN 5 SECONDS...")
        time.sleep(5)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("BOT STARTING...")

    apply_mode()

    if not TOKEN:
        print("WARNING: TOKEN is missing")
    if not CHAT_ID:
        print("WARNING: CHAT_ID is missing")

    SYMBOLS = fetch_top_linear_usdt(TOP_SYMBOLS_COUNT)
    if not SYMBOLS:
        print("NO SYMBOLS FETCHED — EXITING")
        raise SystemExit(1)

    bootstrap_history()

    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"

    send_telegram(
        "🚀 EMA200 REGION BOT RUNNING\n"
        f"Mode: {mode}\n"
        f"Top {TOP_SYMBOLS_COUNT} Bybit USDT-Perp by volume\n"
        f"Timeframes: {', '.join(TIMEFRAMES.values())}\n"
        f"EMA200 deviation threshold: {EMA_DEVIATION * 100:.2f}%\n"
        f"Alert logic: trigger ONCE when entering region"
    )

    threading.Thread(target=telegram_command_listener, daemon=True).start()
    ws_forever()
