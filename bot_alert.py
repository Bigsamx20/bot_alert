import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
import requests
import websocket

# =========================
# TELEGRAM / BYBIT CONFIG
# =========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise SystemExit("Missing TOKEN or CHAT_ID environment variables.")

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
BYBIT_WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"

# =========================
# BOT SETTINGS
# =========================
TIMEFRAMES = ["5", "15", "60"]
TOP_N_COINS = 100
UNIVERSE_REFRESH_SECONDS = 3600

# EMA alert settings (kept for alerts only)
EXTREME_EMA_DISTANCE_PERCENT = 65.0

# RSI PAPER TEST SETTINGS
# Easier thresholds for faster testing
RSI_OVERBOUGHT = 80.0
RSI_OVERSOLD = 20.0
RSI_TOLERANCE = 0.5
RSI_PERIOD = 14

# Giant candle alert settings
GIANT_CANDLE_MIN = 10
GIANT_CANDLE_MAX = 15

# Paper trade settings
PAPER_TRADES_FILE = "paper_trades.json"
PAPER_SL_PERCENT = 5.0
PAPER_TP_PERCENT = 10.0

REMOVED_COINS_FILE = "removed_coins.txt"

# =========================
# GLOBAL STATE
# =========================
session = requests.Session()
data_lock = threading.Lock()

coins = []
market_data = defaultdict(lambda: defaultdict(dict))
last_alert = {}

ws_app = None
ws_thread = None
subscribed_topics = set()
should_run_ws = True

paper_trades = {"open": [], "closed": []}

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

def load_paper_trades() -> dict:
    if not os.path.exists(PAPER_TRADES_FILE):
        return {"open": [], "closed": []}
    try:
        with open(PAPER_TRADES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "open" not in data:
                data["open"] = []
            if "closed" not in data:
                data["closed"] = []
            return data
    except Exception:
        return {"open": [], "closed": []}

def save_paper_trades() -> None:
    try:
        with open(PAPER_TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(paper_trades, f, indent=2)
    except Exception as e:
        print("save_paper_trades error:", e)

removed_symbols = load_removed_symbols()
paper_trades = load_paper_trades()

# =========================
# TIME HELPERS
# =========================
def now_utc_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def format_candle_close_time(start_time_ms: int, tf: str) -> str:
    minutes = int(tf)
    close_time_ms = int(start_time_ms) + minutes * 60 * 1000
    dt = datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

# =========================
# TELEGRAM
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
# ALERT TRACKING
# =========================
def init_coin_tracking(symbol: str) -> None:
    last_alert[symbol] = {
        tf: {
            "ema": None,
            "rsi": None,
            "candle": None,
        }
        for tf in TIMEFRAMES
    }

def sync_tracking() -> None:
    current = set(coins)
    existing = set(last_alert.keys())

    for symbol in current - existing:
        init_coin_tracking(symbol)

    for symbol in existing - current:
        del last_alert[symbol]

# =========================
# PAPER TRADING HELPERS
# =========================
def has_open_trade(symbol: str, tf: str) -> bool:
    for trade in paper_trades["open"]:
        if trade["symbol"] == symbol and trade["timeframe"] == tf and trade["status"] == "OPEN":
            return True
    return False

def open_paper_trade(symbol: str, tf: str, side: str, price: float, reason: str) -> None:
    if has_open_trade(symbol, tf):
        return

    if side == "BUY":
        sl = price * (1 - PAPER_SL_PERCENT / 100)
        tp = price * (1 + PAPER_TP_PERCENT / 100)
    else:
        sl = price * (1 + PAPER_SL_PERCENT / 100)
        tp = price * (1 - PAPER_TP_PERCENT / 100)

    trade = {
        "symbol": symbol,
        "timeframe": tf,
        "side": side,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "entry_time": now_utc_text(),
        "status": "OPEN",
        "reason": reason,
    }

    paper_trades["open"].append(trade)
    save_paper_trades()

    send_alert(
        f"📝 PAPER {side} OPENED\n"
        f"{symbol} | {tf}m\n"
        f"Entry: {price:.6f}\n"
        f"SL: {sl:.6f}\n"
        f"TP: {tp:.6f}\n"
        f"Reason: {reason}\n"
        f"Time: {trade['entry_time']}"
    )

def close_paper_trade(trade: dict, exit_price: float, result: str) -> None:
    if trade["side"] == "BUY":
        pnl_pct = ((exit_price - trade["entry"]) / trade["entry"]) * 100
    else:
        pnl_pct = ((trade["entry"] - exit_price) / trade["entry"]) * 100

    trade["exit"] = exit_price
    trade["exit_time"] = now_utc_text()
    trade["result"] = result
    trade["pnl_pct"] = pnl_pct
    trade["status"] = "CLOSED"

    paper_trades["open"] = [
        t for t in paper_trades["open"]
        if not (
            t["symbol"] == trade["symbol"]
            and t["timeframe"] == trade["timeframe"]
            and t["entry_time"] == trade["entry_time"]
        )
    ]

    paper_trades["closed"].append(trade)
    save_paper_trades()

    emoji = "✅" if pnl_pct >= 0 else "❌"

    send_alert(
        f"{emoji} PAPER {trade['side']} CLOSED\n"
        f"{trade['symbol']} | {trade['timeframe']}m\n"
        f"Entry: {trade['entry']:.6f}\n"
        f"Exit: {exit_price:.6f}\n"
        f"Result: {result}\n"
        f"PnL: {pnl_pct:.2f}%\n"
        f"Time: {trade['exit_time']}"
    )

def update_paper_trades(symbol: str, tf: str, current_price: float) -> None:
    for trade in paper_trades["open"][:]:
        if trade["symbol"] != symbol or trade["timeframe"] != tf or trade["status"] != "OPEN":
            continue

        if trade["side"] == "BUY":
            if current_price <= trade["sl"]:
                close_paper_trade(trade, current_price, "STOP LOSS")
            elif current_price >= trade["tp"]:
                close_paper_trade(trade, current_price, "TAKE PROFIT")

        elif trade["side"] == "SELL":
            if current_price >= trade["sl"]:
                close_paper_trade(trade, current_price, "STOP LOSS")
            elif current_price <= trade["tp"]:
                close_paper_trade(trade, current_price, "TAKE PROFIT")

# =========================
# BYBIT REST HELPERS
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

def fetch_linear_tickers_turnover() -> dict[str, float]:
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
        print("fetch_linear_tickers_turnover error:", e)

    return turnover_map

def rebuild_coin_universe() -> list[str]:
    symbols = fetch_all_trading_linear_symbols()
    turnover_map = fetch_linear_tickers_turnover()

    ranked = []
    for sym in symbols:
        if sym in removed_symbols:
            continue
        ranked.append((sym, turnover_map.get(sym, 0.0)))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in ranked[:TOP_N_COINS]]

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
            timeout=20,
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

        for col in ["start_time", "open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna().reset_index(drop=True)
        if len(df) < 200:
            return None

        return df
    except Exception as e:
        print(f"get_ohlc error for {symbol} {interval}: {e}")
        return None

# =========================
# INDICATOR HELPERS
# =========================
def build_working_df(symbol: str, tf: str) -> pd.DataFrame | None:
    state = market_data[symbol][tf]
    final_df = state.get("final")

    if final_df is None or final_df.empty:
        return None

    working = final_df.copy()

    current = state.get("current")
    if current:
        if len(working) > 0 and int(working.iloc[-1]["start_time"]) == int(current["start_time"]):
            working.iloc[-1] = current
        else:
            current_df = pd.DataFrame([current])
            working = pd.concat([working, current_df], ignore_index=True)

    working = working.tail(220).reset_index(drop=True)
    return working

def calculate_rsi_wilder(close_series: pd.Series, period: int = 14) -> pd.Series:
    delta = close_series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["RSI"] = calculate_rsi_wilder(df["close"], RSI_PERIOD)

    df["body_size"] = (df["close"] - df["open"]).abs()
    df["avg_body_size"] = df["body_size"].rolling(20).mean()

    return df

# =========================
# STRATEGY HELPERS
# =========================
def ema_distance_percent(price: float, ema: float) -> float:
    if ema == 0:
        return 0.0
    return ((price - ema) / ema) * 100

def classify_ema_extreme(distance_pct: float) -> str | None:
    if distance_pct >= EXTREME_EMA_DISTANCE_PERCENT:
        return "above"
    if distance_pct <= -EXTREME_EMA_DISTANCE_PERCENT:
        return "below"
    return None

def classify_rsi_signal(rsi: float) -> str | None:
    if abs(rsi - RSI_OVERBOUGHT) < RSI_TOLERANCE:
        return "high"
    if abs(rsi - RSI_OVERSOLD) < RSI_TOLERANCE:
        return "low"
    return None

def classify_giant_candle(ratio_int: int) -> str | None:
    if GIANT_CANDLE_MIN <= ratio_int <= GIANT_CANDLE_MAX:
        return f"{ratio_int}x"
    return None

# =========================
# SIGNAL EVALUATION
# =========================
def evaluate_symbol_tf(symbol: str, tf: str) -> None:
    df = build_working_df(symbol, tf)
    if df is None or len(df) < 200:
        return

    df = add_indicators(df)

    # EMA alert uses live candle
    live_price = float(df["close"].iloc[-1])
    live_ema = float(df["EMA200"].iloc[-1])
    distance_pct = ema_distance_percent(live_price, live_ema)
    ema_signal = classify_ema_extreme(distance_pct)

    if ema_signal and last_alert[symbol][tf]["ema"] != ema_signal:
        if ema_signal == "above":
            send_alert(
                f"📊 {symbol} | {tf}m\n"
                f"EXTREMELY FAR ABOVE EMA 🚀\n"
                f"Distance: {distance_pct:.2f}%\n"
                f"Price: {live_price:.6f}\n"
                f"EMA200: {live_ema:.6f}\n"
                f"Required Distance: ±{EXTREME_EMA_DISTANCE_PERCENT:.2f}%"
            )
        else:
            send_alert(
                f"📊 {symbol} | {tf}m\n"
                f"EXTREMELY FAR BELOW EMA 🔻\n"
                f"Distance: {distance_pct:.2f}%\n"
                f"Price: {live_price:.6f}\n"
                f"EMA200: {live_ema:.6f}\n"
                f"Required Distance: ±{EXTREME_EMA_DISTANCE_PERCENT:.2f}%"
            )
        last_alert[symbol][tf]["ema"] = ema_signal

    if ema_signal is None:
        last_alert[symbol][tf]["ema"] = None

    # RSI alert uses last closed candle
    if len(df) < 2:
        return

    closed_price = float(df["close"].iloc[-2])
    closed_rsi = float(df["RSI"].iloc[-2])
    closed_start_time = int(df["start_time"].iloc[-2])
    closed_time_text = format_candle_close_time(closed_start_time, tf)

    rsi_signal = classify_rsi_signal(closed_rsi)

    if rsi_signal and last_alert[symbol][tf]["rsi"] != rsi_signal:
        if rsi_signal == "high":
            send_alert(
                f"📊 {symbol} | {tf}m\n"
                f"RSI OVERBOUGHT 80 🔴\n"
                f"RSI: {closed_rsi:.2f}\n"
                f"Zone: {RSI_OVERBOUGHT} ± {RSI_TOLERANCE}\n"
                f"Price: {closed_price:.6f}\n"
                f"Last Candle Close: {closed_time_text}"
            )
        else:
            send_alert(
                f"📊 {symbol} | {tf}m\n"
                f"RSI OVERSOLD 20 🟢\n"
                f"RSI: {closed_rsi:.2f}\n"
                f"Zone: {RSI_OVERSOLD} ± {RSI_TOLERANCE}\n"
                f"Price: {closed_price:.6f}\n"
                f"Last Candle Close: {closed_time_text}"
            )
        last_alert[symbol][tf]["rsi"] = rsi_signal

    if rsi_signal is None:
        last_alert[symbol][tf]["rsi"] = None

    # Giant candle alert uses live candle
    current_body = df["body_size"].iloc[-1]
    avg_body = df["avg_body_size"].iloc[-2] if len(df) > 21 else None

    candle_signal = None
    multiplier = None

    if avg_body is not None and pd.notna(avg_body) and avg_body > 0:
        ratio = current_body / avg_body
        ratio_int = int(round(ratio))
        candle_signal = classify_giant_candle(ratio_int)
        multiplier = ratio_int if candle_signal else None

    if candle_signal and last_alert[symbol][tf]["candle"] != candle_signal:
        direction_text = "BULLISH 🟢" if float(df["close"].iloc[-1]) >= float(df["open"].iloc[-1]) else "BEARISH 🔴"
        send_alert(
            f"📊 {symbol} | {tf}m\n"
            f"GIANT CANDLE ALERT 🔥\n"
            f"Size: {multiplier}x candle\n"
            f"Type: {direction_text}\n"
            f"Body Size: {float(current_body):.6f}\n"
            f"Average Body: {float(avg_body):.6f}\n"
            f"Price: {live_price:.6f}"
        )
        last_alert[symbol][tf]["candle"] = candle_signal

    if candle_signal is None:
        last_alert[symbol][tf]["candle"] = None

    # PAPER TRADE ENTRY - RSI ONLY TEST
    buy_signal = rsi_signal == "low"
    sell_signal = rsi_signal == "high"

    if buy_signal:
        open_paper_trade(symbol, tf, "BUY", live_price, "RSI oversold test")

    if sell_signal:
        open_paper_trade(symbol, tf, "SELL", live_price, "RSI overbought test")

    # PAPER TRADE EXIT
    update_paper_trades(symbol, tf, live_price)

# =========================
# STARTUP HISTORY LOAD
# =========================
def load_initial_history(symbols: list[str]) -> None:
    for symbol in symbols:
        for tf in TIMEFRAMES:
            df = get_ohlc(symbol, tf)
            if df is None:
                continue

            market_data[symbol][tf]["final"] = df.copy().tail(220).reset_index(drop=True)
            market_data[symbol][tf]["current"] = None

            if len(df) > 0:
                market_data[symbol][tf]["last_start"] = int(df.iloc[-1]["start_time"])

# =========================
# TELEGRAM COMMANDS
# =========================
def check_coin(symbol: str, tf: str) -> None:
    if tf not in TIMEFRAMES:
        send_alert("❌ Timeframe must be 5, 15, or 60")
        return

    with data_lock:
        if symbol not in market_data or tf not in market_data[symbol]:
            df = get_ohlc(symbol, tf)
            if df is None:
                send_alert(f"{symbol} {tf}m ❌ No data")
                return
            market_data[symbol][tf]["final"] = df.copy().tail(220).reset_index(drop=True)
            market_data[symbol][tf]["current"] = None

    df = build_working_df(symbol, tf)
    if df is None or len(df) < 2:
        send_alert(f"{symbol} {tf}m ❌ No data")
        return

    df = add_indicators(df)

    live_price = float(df["close"].iloc[-1])
    live_ema = float(df["EMA200"].iloc[-1])
    distance = ema_distance_percent(live_price, live_ema)
    ema_status = classify_ema_extreme(distance)

    closed_rsi = float(df["RSI"].iloc[-2])
    closed_start_time = int(df["start_time"].iloc[-2])
    closed_time_text = format_candle_close_time(closed_start_time, tf)

    rsi_status = classify_rsi_signal(closed_rsi)

    msg = f"📊 {symbol} | {tf}m\n"

    if ema_status == "above":
        msg += "EMA Status: EXTREMELY FAR ABOVE 🚀\n"
    elif ema_status == "below":
        msg += "EMA Status: EXTREMELY FAR BELOW 🔻\n"
    else:
        msg += "EMA Status: NOT FAR ENOUGH\n"

    if rsi_status == "high":
        msg += "RSI Status: OVERBOUGHT 80 🔴\n"
    elif rsi_status == "low":
        msg += "RSI Status: OVERSOLD 20 🟢\n"
    else:
        msg += "RSI Status: NEUTRAL\n"

    msg += (
        f"Distance: {distance:.2f}%\n"
        f"RSI (closed candle): {closed_rsi:.2f}\n"
        f"Last Candle Close: {closed_time_text}\n"
        f"Live Price: {live_price:.6f}\n"
        f"EMA200: {live_ema:.6f}"
    )

    send_alert(msg)

def show_summary(tf: str) -> None:
    if tf not in TIMEFRAMES:
        send_alert("❌ Timeframe must be 5, 15, or 60")
        return

    with data_lock:
        symbol_list = list(coins)

    msg = (
        f"📊 Summary {tf}m\n"
        f"Tracked coins: {len(symbol_list)}\n"
        f"RSI Test Levels: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
    )

    found = 0
    for symbol in symbol_list:
        df = build_working_df(symbol, tf)
        if df is None or len(df) < 2:
            continue

        df = add_indicators(df)

        live_price = float(df["close"].iloc[-1])
        live_ema = float(df["EMA200"].iloc[-1])
        distance = ema_distance_percent(live_price, live_ema)
        ema_status = classify_ema_extreme(distance)

        closed_rsi = float(df["RSI"].iloc[-2])
        rsi_status = classify_rsi_signal(closed_rsi)

        if ema_status is None and rsi_status is None:
            continue

        parts = [symbol]

        if ema_status == "above":
            parts.append(f"EMA ABOVE {distance:.2f}%")
        elif ema_status == "below":
            parts.append(f"EMA BELOW {distance:.2f}%")

        if rsi_status == "high":
            parts.append(f"RSI80 {closed_rsi:.2f} OB")
        elif rsi_status == "low":
            parts.append(f"RSI20 {closed_rsi:.2f} OS")

        msg += " | ".join(parts) + "\n"
        found += 1

        if found >= 50:
            msg += "... more coins omitted"
            break

    if found == 0:
        msg += "No current signals found."

    send_alert(msg)

def show_paper_summary() -> None:
    open_count = len(paper_trades["open"])
    closed_count = len(paper_trades["closed"])

    total_pnl = 0.0
    wins = 0
    losses = 0

    for trade in paper_trades["closed"]:
        pnl = float(trade.get("pnl_pct", 0))
        total_pnl += pnl
        if pnl >= 0:
            wins += 1
        else:
            losses += 1

    msg = (
        f"📝 PAPER TRADE SUMMARY\n"
        f"Open Trades: {open_count}\n"
        f"Closed Trades: {closed_count}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Total PnL: {total_pnl:.2f}%"
    )

    if open_count > 0:
        msg += "\n\nOpen Positions:\n"
        for t in paper_trades["open"][:10]:
            msg += (
                f"{t['symbol']} | {t['timeframe']}m | {t['side']} | "
                f"Entry: {float(t['entry']):.6f}\n"
            )

    send_alert(msg)

def telegram_listener() -> None:
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
                        symbol_list = list(coins)

                    if not symbol_list:
                        send_alert("⚠️ No coins in list")
                    else:
                        msg = (
                            f"📋 Top {len(symbol_list)} Bybit Coins\n"
                            f"Timeframes: 5m / 15m / 60m\n"
                            f"RSI Test Levels: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
                            f"Paper Trading: RSI ONLY TEST\n"
                        )
                        msg += "\n".join(symbol_list[:100])
                        send_alert(msg)

                elif cmd == "/check" and len(parts) == 3:
                    check_coin(parts[1].upper(), parts[2])

                elif cmd == "/summary" and len(parts) == 2:
                    show_summary(parts[1])

                elif cmd == "/papersummary":
                    show_paper_summary()

                elif cmd == "/refresh":
                    refresh_universe_and_data()
                    send_alert(f"🔄 Refreshed top {TOP_N_COINS} Bybit coins\nTracked coins: {len(coins)}")

                elif cmd == "/remove" and len(parts) == 2:
                    symbol = parts[1].upper().strip()
                    removed_symbols.add(symbol)
                    save_removed_symbols(removed_symbols)
                    refresh_universe_and_data()
                    send_alert(f"❌ {symbol} removed from tracking")

                else:
                    send_alert(
                        "Commands:\n"
                        "/list\n"
                        "/check BTCUSDT 5\n"
                        "/summary 5\n"
                        "/papersummary\n"
                        "/refresh\n"
                        "/remove BTCUSDT"
                    )

        except Exception as e:
            print("telegram_listener error:", e)

        time.sleep(2)

# =========================
# WEBSOCKET
# =========================
def build_topics(symbols: list[str]) -> list[str]:
    topics = []
    for symbol in symbols:
        for tf in TIMEFRAMES:
            topics.append(f"kline.{tf}.{symbol}")
    return topics

def subscribe_topics() -> None:
    global ws_app, subscribed_topics
    if ws_app is None:
        return

    topics = build_topics(list(coins))
    new_topics = [t for t in topics if t not in subscribed_topics]

    chunk_size = 10
    for i in range(0, len(new_topics), chunk_size):
        chunk = new_topics[i:i + chunk_size]
        if not chunk:
            continue

        payload = {"op": "subscribe", "args": chunk}
        try:
            ws_app.send(json.dumps(payload))
            for topic in chunk:
                subscribed_topics.add(topic)
        except Exception as e:
            print("subscribe_topics error:", e)

def on_open(ws):
    print("WebSocket opened")
    subscribe_topics()

def on_message(ws, message):
    try:
        msg = json.loads(message)

        topic = msg.get("topic")
        if not topic or not topic.startswith("kline."):
            return

        data_arr = msg.get("data", [])
        if not data_arr:
            return

        candle = data_arr[0]
        parts = topic.split(".")
        if len(parts) != 3:
            return

        tf = parts[1]
        symbol = parts[2].upper()

        row = {
            "start_time": int(candle["start"]),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle["volume"]),
            "turnover": float(candle["turnover"]),
        }

        confirm = bool(candle.get("confirm", False))

        with data_lock:
            if symbol not in last_alert:
                init_coin_tracking(symbol)

            state = market_data[symbol][tf]
            final_df = state.get("final")

            if final_df is None:
                return

            state["current"] = row
            state["last_start"] = row["start_time"]

            if confirm:
                if len(final_df) > 0 and int(final_df.iloc[-1]["start_time"]) == row["start_time"]:
                    final_df.iloc[-1] = row
                else:
                    final_df = pd.concat([final_df, pd.DataFrame([row])], ignore_index=True)
                    final_df = final_df.tail(220).reset_index(drop=True)

                state["final"] = final_df
                state["current"] = None

            evaluate_symbol_tf(symbol, tf)

    except Exception as e:
        print("on_message error:", e)

def on_error(ws, error):
    print("WebSocket error:", error)

def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed:", close_status_code, close_msg)

def websocket_loop():
    global ws_app, subscribed_topics

    while should_run_ws:
        try:
            subscribed_topics = set()
            ws_app = websocket.WebSocketApp(
                BYBIT_WS_LINEAR,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws_app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print("websocket_loop error:", e)

        time.sleep(5)

# =========================
# UNIVERSE REFRESH
# =========================
def refresh_universe_and_data() -> None:
    global coins

    with data_lock:
        new_coins = rebuild_coin_universe()
        coins = new_coins
        sync_tracking()

        for symbol in list(market_data.keys()):
            if symbol not in coins:
                del market_data[symbol]

    load_initial_history(list(coins))
    subscribe_topics()

def universe_refresh_loop():
    while True:
        try:
            refresh_universe_and_data()
        except Exception as e:
            print("universe_refresh_loop error:", e)
        time.sleep(UNIVERSE_REFRESH_SECONDS)

# =========================
# STARTUP
# =========================
coins = rebuild_coin_universe()
sync_tracking()
load_initial_history(list(coins))
save_paper_trades()

threading.Thread(target=telegram_listener, daemon=True).start()
threading.Thread(target=universe_refresh_loop, daemon=True).start()

ws_thread = threading.Thread(target=websocket_loop, daemon=True)
ws_thread.start()

send_alert(
    f"🚨 HYBRID RSI PAPER TEST BOT RUNNING 🚨\n"
    f"Tracked coins: {len(coins)}\n"
    f"Mode: Top {TOP_N_COINS} Bybit linear coins by 24h turnover\n"
    f"Timeframes: 5m / 15m / 60m\n"
    f"RSI Test Levels: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
    f"Paper Trading: RSI ONLY TEST\n"
    f"Data source: Bybit REST + Bybit WebSocket"
)

while True:
    time.sleep(60)
