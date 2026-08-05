"""
MACD bullish-cross + first-green-close signal bot.

Checks one or more symbols (e.g. BTCUSDT, ETHUSDT) on the 4h timeframe.
Fires a Telegram alert the first time a MACD bullish crossover is
confirmed by the first green candle close afterward (same logic as the
scanner artifact).

Candle data comes from CryptoCompare's free public API rather than an
exchange directly, because exchanges like Binance block API requests
from GitHub Actions' US-based runners for regulatory reasons -
CryptoCompare aggregates market data and isn't a regulated exchange, so
it isn't geo-restricted the same way.

State is stored in state.json so the same signal never alerts twice, even
if this script runs every 15 minutes.
"""

import os
import json
import time
import requests

CRYPTOCOMPARE_URL = "https://min-api.cryptocompare.com/data/v2/histohour"
STATE_FILE = "state.json"

# Quote assets checked longest-first so e.g. "USDT" matches before "T" would.
KNOWN_QUOTES = ["USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "EUR", "GBP"]


def split_symbol(symbol):
    """'BTCUSDT' -> ('BTC', 'USDT'). Falls back to a USDT quote if no
    known suffix matches."""
    for q in KNOWN_QUOTES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    return symbol, "USDT"

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
    base, quote = split_symbol(symbol)
    resp = requests.get(
        CRYPTOCOMPARE_URL,
        params={"fsym": base, "tsym": quote, "aggregate": 4, "limit": 300},
        timeout=15,
    )
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"HTTP {resp.status_code}, non-JSON response: {resp.text[:200]}")

    if payload.get("Response") != "Success":
        # Surface whatever the API actually sent back so failures are diagnosable,
        # since CryptoCompare's error envelope has changed shape before.
        detail = payload.get("Message") or payload.get("Err") or payload
        raise RuntimeError(f"HTTP {resp.status_code}: {str(detail)[:200]}")

    rows = payload["Data"]["Data"]
    now_s = time.time()
    bar_seconds = 4 * 3600
    candles = [
        {"time": row["time"] * 1000, "open": float(row["open"]), "high": float(row["high"]),
         "low": float(row["low"]), "close": float(row["close"])}
        for row in rows
        if row["time"] + bar_seconds <= now_s and (row["open"] or row["close"])
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
