"""
MACD histogram flip bot - no filters, no confirmation delay.

Checks one or more symbols (e.g. BTCUSDT, ETHUSDT) on the 4h timeframe.

LONG signal:  the MACD histogram (MACD line minus signal line) flips from
              zero-or-negative to positive on this bar - i.e. the first
              green histogram bar. Fires immediately on that bar, no
              waiting for a price candle to confirm.
SHORT signal: the histogram flips from zero-or-positive to negative on
              this bar - i.e. the first red histogram bar. Also fires
              immediately.

No trend filter, no candle-color confirmation, no "does MACD hold"
requirement - this is the rawest, fastest, and noisiest version of the
strategy. Expect more signals and more false ones than the other bot.

Candle data comes from Kraken's public API - no API key needed, and no
account-level call quota to run into. Kraken is a US-licensed exchange
so it doesn't block requests from GitHub Actions' US-based runners the
way Binance does. Tradeoff: Kraken lists fewer tokens than Binance, so a
very new or small-cap symbol may not be found there - if that happens
the bot reports it clearly per-symbol instead of failing the whole run.

Uses its own state file (state_histogram.json) so it doesn't collide
with the other bot's state.json if both run in the same repo.
"""

import os
import json
import time
import requests

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
STATE_FILE = "state_histogram.json"

# Kraken uses its own asset codes for a few legacy coins.
KRAKEN_BASE_ALIASES = {"BTC": "XBT"}


def kraken_pair_candidates(symbol):
    """Given e.g. 'BTCUSDT', yield pair strings to try on Kraken, in order:
    the symbol as-is, then with BTC->XBT applied, then falling back to a
    USD quote instead of USDT (Kraken doesn't list USDT pairs for everything)."""
    quotes = ["USDT", "USDC", "USD", "EUR", "GBP", "BTC", "ETH"]
    base, quote = symbol, "USDT"
    for q in quotes:
        if symbol.endswith(q) and len(symbol) > len(q):
            base, quote = symbol[: -len(q)], q
            break

    seen = set()
    for b in (base, KRAKEN_BASE_ALIASES.get(base, base)):
        for q in ([quote, "USD"] if quote != "USD" else [quote]):
            pair = f"{b}{q}"
            if pair not in seen:
                seen.add(pair)
                yield pair


TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "BTCUSDT").split(",") if s.strip()]


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
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def detect_histogram_flips(candles, hist):
    """Returns two lists: long_signals, short_signals - each a list of
    {index, time, price} for every bar where the histogram flipped color."""
    long_signals, short_signals = [], []
    for i in range(1, len(candles)):
        prev_h, h = hist[i - 1], hist[i]
        if prev_h <= 0 and h > 0:
            long_signals.append({"index": i, "time": candles[i]["time"], "price": candles[i]["close"]})
        elif prev_h >= 0 and h < 0:
            short_signals.append({"index": i, "time": candles[i]["time"], "price": candles[i]["close"]})
    return long_signals, short_signals


def fetch_candles(symbol):
    last_error = None
    for pair in kraken_pair_candidates(symbol):
        resp = requests.get(KRAKEN_URL, params={"pair": pair, "interval": 240}, timeout=15)
        try:
            payload = resp.json()
        except ValueError:
            last_error = f"HTTP {resp.status_code}, non-JSON response for pair {pair}"
            continue

        if payload.get("error"):
            last_error = f"pair {pair}: {payload['error']}"
            continue

        result = payload.get("result", {})
        rows = None
        for key, val in result.items():
            if key != "last":
                rows = val
                break
        if not rows:
            last_error = f"pair {pair}: no data returned"
            continue

        now_s = time.time()
        bar_seconds = 4 * 3600
        candles = [
            {"time": row[0] * 1000, "open": float(row[1]), "high": float(row[2]),
             "low": float(row[3]), "close": float(row[4])}
            for row in rows
            if row[0] + bar_seconds <= now_s
        ]
        return candles

    raise RuntimeError(f"no Kraken pair worked for {symbol} - {last_error}")


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
            time.sleep(1)  # small pacing gap so rapid-fire requests don't trip the free-tier rate limit
            candles = fetch_candles(symbol)
            if len(candles) < 40:
                print(f"{symbol}: not enough history, skipping")
                continue

            closes = [c["close"] for c in candles]
            _, _, hist = compute_macd(closes)
            long_signals, short_signals = detect_histogram_flips(candles, hist)

            if long_signals:
                last = long_signals[-1]
                key = f"{symbol}:long_hist:last_time"
                if state.get(key) != last["time"]:
                    send_telegram(
                        f"🟢 *LONG SIGNAL* — {symbol} (4H)\n"
                        f"MACD histogram flipped positive (1st green bar). No filters.\n"
                        f"Price: {fmt_price(last['price'])}"
                    )
                    state[key] = last["time"]
                    changed = True

            if short_signals:
                last = short_signals[-1]
                key = f"{symbol}:short_hist:last_time"
                if state.get(key) != last["time"]:
                    send_telegram(
                        f"🔴 *SHORT SIGNAL* — {symbol} (4H)\n"
                        f"MACD histogram flipped negative (1st red bar). No filters.\n"
                        f"Price: {fmt_price(last['price'])}"
                    )
                    state[key] = last["time"]
                    changed = True

            print(f"{symbol}: checked, {len(long_signals)} long / {len(short_signals)} short historical flips")
        except Exception as e:
            print(f"{symbol}: error — {e}")

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()

