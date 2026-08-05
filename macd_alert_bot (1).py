"""
MACD bullish-cross + first-green-close signal bot.

Checks one or more Binance symbols on the 4h timeframe. Fires a Telegram
alert the first time a MACD bullish crossover is confirmed by the first
green candle close afterward (same logic as the scanner artifact).

State is stored in state.json so the same signal never alerts twice, even
if this script runs every 15 minutes.
"""

import os
import json
import time
import requests

BINANCE_URL = "https://api.binance.com/api/v3/klines"
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
ALERT_ON_CROSS = os.environ.get("ALERT_ON_CROSS", "false").lower() == "true"


def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    prev = values[0]
    for v in values[1:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def compute_macd(closes, fast=12, slow=26, signal_p=9):
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal_p)
    return macd_line, signal_line


def detect_signals(candles, macd_line, signal_line):
    """Returns list of (index, time, price) for confirmed signals, and the
    index of a currently-armed (crossed, not yet confirmed) setup, if any."""
    signals = []
    pending = None
    for i in range(1, len(candles)):
        was_below = macd_line[i - 1] <= signal_line[i - 1]
        now_above = macd_line[i] > signal_line[i]
        if was_below and now_above:
            pending = i
        if pending is not None:
            if i > pending and macd_line[i] < signal_line[i]:
                pending = None
                continue
            c = candles[i]
            if c["close"] > c["open"]:
                signals.append({"index": i, "time": c["time"], "price": c["close"], "cross_index": pending})
                pending = None
    return signals, pending


def fetch_candles(symbol):
    resp = requests.get(BINANCE_URL, params={"symbol": symbol, "interval": "4h", "limit": 300}, timeout=15)
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(data.get("msg", "Unknown Binance error"))
    now_ms = int(time.time() * 1000)
    candles = [
        {"time": row[0], "open": float(row[1]), "high": float(row[2]),
         "low": float(row[3]), "close": float(row[4])}
        for row in data if row[6] <= now_ms
    ]
    return candles


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=15)
    if not resp.ok:
        print("Telegram send failed:", resp.text)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fmt_price(p):
    return f"{p:,.2f}" if p >= 1000 else f"{p:.4f}" if p >= 1 else f"{p:.6f}"


def main():
    state = load_state()
    changed = False

    for symbol in SYMBOLS:
        try:
            candles = fetch_candles(symbol)
            if len(candles) < 40:
                print(f"{symbol}: not enough history, skipping")
                continue
            closes = [c["close"] for c in candles]
            macd_line, signal_line = compute_macd(closes)
            signals, armed_idx = detect_signals(candles, macd_line, signal_line)

            key_signal = f"{symbol}:last_signal_time"
            key_armed = f"{symbol}:last_armed_time"

            if signals:
                last = signals[-1]
                if state.get(key_signal) != last["time"]:
                    send_telegram(
                        f"🟢 *SIGNAL* — {symbol} (4H)\n"
                        f"MACD bullish cross confirmed by first green close.\n"
                        f"Price: {fmt_price(last['price'])}"
                    )
                    state[key_signal] = last["time"]
                    changed = True

            if ALERT_ON_CROSS and armed_idx is not None:
                armed_time = candles[armed_idx]["time"]
                if state.get(key_armed) != armed_time:
                    send_telegram(
                        f"🟡 *ARMED* — {symbol} (4H)\n"
                        f"MACD crossed bullish, waiting on first green close."
                    )
                    state[key_armed] = armed_time
                    changed = True

            print(f"{symbol}: checked, {len(signals)} historical signals")
        except Exception as e:
            print(f"{symbol}: error — {e}")

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
