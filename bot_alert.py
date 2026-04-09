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

BYBIT_REST = "https://api.bybit.com"
WS_URL = "wss://stream.bybit.com/v5/public/linear"

# Bybit intervals -> labels
TIMEFRAMES = {
    "1": "1m",
    "5": "5m",
    "60": "1h",
}

EMA_PERIOD = 200
RSI_PERIOD = 14
HISTORY_LIMIT = 250

TOP_VOLUME_COUNT = 40
TOP_GAINERS_COUNT = 10
TOTAL_SYMBOLS = 50

# ============================================================
# MODES & THRESHOLDS
# ============================================================

TESTING_MODE = False

# Live thresholds
REAL_EMA_DEVIATION = 0.65      # 65%
REAL_RSI_OVERBOUGHT = 95.0
REAL_RSI_OVERSOLD = 5.0
REAL_LARGE_CANDLE = 12.0
REAL_STRONG_CANDLE = 15.0

# Test thresholds (much more sensitive)
TEST_EMA_DEVIATION = 0.01      # 1%
TEST_RSI_OVERBOUGHT = 51.0
TEST_RSI_OVERSOLD = 49.0
TEST_LARGE_CANDLE = 1.2
TEST_STRONG_CANDLE = 1.5

# Active thresholds (will be set by apply_mode)
EMA_DEVIATION_THRESHOLD = REAL_EMA_DEVIATION
RSI_OVERBOUGHT = REAL_RSI_OVERBOUGHT
RSI_OVERSOLD = REAL_RSI_OVERSOLD
LARGE_CANDLE_RATIO = REAL_LARGE_CANDLE
STRONG_CANDLE_RATIO = REAL_STRONG_CANDLE

def apply_mode():
    global EMA_DEVIATION_THRESHOLD, RSI_OVERBOUGHT, RSI_OVERSOLD
    global LARGE_CANDLE_RATIO, STRONG_CANDLE_RATIO

    if TESTING_MODE:
        EMA_DEVIATION_THRESHOLD = TEST_EMA_DEVIATION
        RSI_OVERBOUGHT = TEST_RSI_OVERBOUGHT
        RSI_OVERSOLD = TEST_RSI_OVERSOLD
        LARGE_CANDLE_RATIO = TEST_LARGE_CANDLE
        STRONG_CANDLE_RATIO = TEST_STRONG_CANDLE
    else:
        EMA_DEVIATION_THRESHOLD = REAL_EMA_DEVIATION
        RSI_OVERBOUGHT = REAL_RSI_OVERBOUGHT
        RSI_OVERSOLD = REAL_RSI_OVERSOLD
        LARGE_CANDLE_RATIO = REAL_LARGE_CANDLE
        STRONG_CANDLE_RATIO = REAL_STRONG_CANDLE

# ============================================================
# GLOBALS
# ============================================================

SYMBOLS = []
candles = {}  # (symbol, tf) -> DataFrame

# indicator_state[(symbol, tf)] = {
#   "ema_active": bool,
#   "rsi_active": bool,
#   "candle_active": bool,
#   "last_confluence_level": int (0, 2, 3)
# }
indicator_state = {}

data_lock = threading.Lock()

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

                elif text == "/help":
                    send_telegram(
                        "📖 Commands:\n"
                        "/test - Check if bot is alive\n"
                        "/mode - Show current mode & thresholds\n"
                        "/symbols - Show monitored symbols\n"
                        "/status - Show basic status\n"
                        "/testmode_on - Enable Testing Mode\n"
                        "/testmode_off - Disable Testing Mode",
                        chat_id,
                    )

                elif text == "/mode":
                    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"
                    send_telegram(
                        f"📌 Current mode: {mode}\n"
                        f"EMA200 deviation: {EMA_DEVIATION_THRESHOLD * 100:.2f}%\n"
                        f"RSI extremes: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
                        f"Large candle: ≥{LARGE_CANDLE_RATIO}x (strong ≥{STRONG_CANDLE_RATIO}x)",
                        chat_id,
                    )

                elif text == "/symbols":
                    if not SYMBOLS:
                        send_telegram("No symbols loaded yet.", chat_id)
                    else:
                        send_telegram(
                            "📊 Monitored symbols (" + str(len(SYMBOLS)) + "):\n" +
                            ", ".join(SYMBOLS),
                            chat_id,
                        )

                elif text == "/status":
                    mode = "TESTING MODE" if TESTING_MODE else "LIVE MODE"
                    send_telegram(
                        f"✅ Bot running.\n"
                        f"Mode: {mode}\n"
                        f"Symbols: {len(SYMBOLS)}\n"
                        f"Timeframes: {', '.join(TIMEFRAMES.values())}\n"
                        f"EMA200 deviation: {EMA_DEVIATION_THRESHOLD * 100:.2f}%\n"
                        f"RSI extremes: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
                        f"Large candle: ≥{LARGE_CANDLE_RATIO}x (strong ≥{STRONG_CANDLE_RATIO}x)",
                        chat_id,
                    )

                elif text == "/testmode_on":
                    TESTING_MODE = True
                    apply_mode()
                    send_telegram(
                        "🧪 TESTING MODE ENABLED\n"
                        f"EMA200 deviation: {EMA_DEVIATION_THRESHOLD * 100:.2f}%\n"
                        f"RSI extremes: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
                        f"Large candle: ≥{LARGE_CANDLE_RATIO}x (strong ≥{STRONG_CANDLE_RATIO}x)",
                        chat_id,
                    )

                elif text == "/testmode_off":
                    TESTING_MODE = False
                    apply_mode()
                    send_telegram(
                        "✅ LIVE MODE ENABLED\n"
                        f"EMA200 deviation: {EMA_DEVIATION_THRESHOLD * 100:.2f}%\n"
                        f"RSI extremes: {RSI_OVERSOLD}/{RSI_OVERBOUGHT}\n"
                        f"Large candle: ≥{LARGE_CANDLE_RATIO}x (strong ≥{STRONG_CANDLE_RATIO}x)",
                        chat_id,
                    )

        except Exception as e:
            print("TELEGRAM LISTENER ERROR:", e)
            time.sleep(3)

# ============================================================
# BYBIT HELPERS
# ============================================================

def fetch_all_linear_tickers():
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
        return rows
    except Exception as e:
        print("FETCH TICKERS ERROR:", e)
        return []


def fetch_top_symbols():
    """
    Total = 50 coins
    - 40 top by volume
    - 10 top gainers
    """
    rows = fetch_all_linear_tickers()
    if not rows:
        return []

    # Top 40 by volume (turnover24h)
    def turnover_value(row):
        try:
            return float(row.get("turnover24h", 0))
        except Exception:
            return 0.0

    sorted_by_turnover = sorted(rows, key=turnover_value, reverse=True)
    top_volume = [row["symbol"] for row in sorted_by_turnover[:TOP_VOLUME_COUNT]]

    # Top 10 gainers by price24hPcnt
    def pct_change(row):
        try:
            return float(row.get("price24hPcnt", 0))
        except Exception:
            return 0.0

    sorted_by_gainers = sorted(rows, key=pct_change, reverse=True)
    top_gainers = [row["symbol"] for row in sorted_by_gainers[:TOP_GAINERS_COUNT]]

    combined = []
    seen = set()
    for sym in top_volume + top_gainers:
        if sym not in seen:
            seen.add(sym)
            combined.append(sym)

    combined = combined[:TOTAL_SYMBOLS]
    print("TOP SYMBOLS (VOLUME + GAINERS):", combined)
    return combined


def fetch_historical_klines(symbol: str, interval: str, limit: int = HISTORY_LIMIT):
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
                    indicator_state[(symbol, tf)] = {
                        "ema_active": False,
                        "rsi_active": False,
                        "candle_active": False,
                        "last_confluence_level": 0,
                    }
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


def calc_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    avg_gain = avg_gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = avg_loss.ewm(alpha=1/period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ============================================================
# SIGNAL LOGIC
# ============================================================

def evaluate_indicators(symbol: str, tf: str, tf_label: str, df: pd.DataFrame):
    """
    For the latest closed candle:
    - EMA200 deviation
    - RSI(14)
    - Large candle ratio
    Then:
    - Send single alerts (on activation only)
    - Send confluence alerts (2/3, 3/3) on activation only
    """
    if len(df) < max(EMA_PERIOD, RSI_PERIOD + 1, 2):
        return

    key = (symbol, tf)
    state = indicator_state.setdefault(key, {
        "ema_active": False,
        "rsi_active": False
