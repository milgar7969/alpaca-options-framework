# Alpaca Options Bot Framework

A production-ready Python framework for building **live options trading bots on Alpaca Markets**.

This repo solves the hard infrastructure problems — real-time streaming, position management, order execution, and all the Alpaca API quirks — so you can focus on your strategy.

> **Note:** This repo includes a full working 0DTE SPY gamma explosion strategy — all parameters, entry/exit logic, and live session write-ups. See the r/alpacamarkets post series for the full breakdown of what works, what breaks, and why.

---

## What the Documentation Doesn't Tell You

If you've tried to build an options bot on Alpaca, you've probably hit these:

**1. Bracket orders are rejected**
```
error 42210000: complex orders not supported
```
Bracket legs, sell limits — all rejected for options on Alpaca paper trading. The only working exit is `close_position()`. This framework builds its entire exit system around that constraint.

**2. Sell limit orders are rejected**
```
error 40310000: cannot submit sell order — no existing position
```
Alpaca treats a sell limit on an option as an attempt to open a short position. Even when you own the position, it's unreliable. Use `close_position()`.

**3. Greeks return null for 0DTE contracts**
Alpaca computes Greeks via Black-Scholes. At T=0 (expiry day) the model is undefined. Every 0DTE contract returns null for delta, gamma, theta, vega. This framework includes a proxy delta implementation as a workaround.

**4. SPY price updates only once per 1-minute bar**
`StockDataStream` delivers bars at the close of each minute. Option quotes arrive many times per second. Between bar closes the underlying price is stale — proxy delta stays at zero. The framework handles this gracefully with a disable flag.

**5. Option subscriptions go stale intraday**
If you subscribe at market open and SPY moves $8 by noon, your subscribed strikes are irrelevant. The framework includes a background re-subscription watcher triggered by price movement (default: ±$3).

**6. Premarket price ≠ open price**
Subscribing options before 9:30 ET uses the premarket SPY price. SPY can gap significantly at open. The framework defers all option subscriptions until the first 9:30 ET RTH bar.

**7. Restart abandons open positions**
If your process crashes with an open position, that position still exists on Alpaca. On startup the framework polls REST, finds any existing option positions, and reconstructs local state automatically.

---

## Architecture

```
asyncio.gather(
    feed.start(),               # StockDataStream + OptionDataStream + TradingStream
    _option_subscriber(),       # waits for 9:30 ET bar → subscribes strikes
    _resubscribe_watcher(),     # re-subscribes if SPY moves ±$3
    _exit_monitor(),            # 30-second safety net (WebSocket reconnect gaps)
    _time_stop_watcher(),       # force-close everything at 3:25 PM ET
    _status_loop(),             # live terminal display
)

# Quote-driven exits (fires per quote tick, not a gathered task):
# on_option_quote() → asyncio.create_task(_evaluate_exit(quote))
# Sub-50ms from quote arrival to order submission.
```

Three concurrent WebSocket streams. Entry logic runs in the quote handler. Exit logic is **quote-driven** — `_evaluate_exit()` fires on every tick for the held symbol via `asyncio.create_task()`, so `close_position()` is never called from inside a stream callback.

---

## File Structure

```
alpaca-options-framework/
│
├── config.py       All parameters in one place — API keys, thresholds, timing
├── main.py         asyncio orchestrator, exit monitor, terminal UI, keyboard cmds
│
├── feeds.py        WebSocket stream manager (subscribe / add_option_symbols)
├── orders.py       Alpaca TradingClient wrapper (buy_limit / close_position)
├── state.py        Session state: position, quote cache, peak_mid tracking, CSV log
├── risk.py         Position sizing, daily loss gate, cooldown, P&L restore
├── strikes.py      ATR calculation, dynamic strike selection, OCC symbol builder
├── momentum.py     EMA5/EMA20, VWAP, ROC5, atr5, consecutive bar engine
│
└── signals.py      ← YOUR STRATEGY GOES HERE
                      Entry and exit signal logic — placeholder implementation
                      with full documentation of the interface.
```

**`signals.py` is the only file you need to implement.** Everything else is working infrastructure.

---

## Quick Start

### 1. Install dependencies

```bash
pip install "alpaca-py>=0.43.0"
```

Requires Python 3.9+. All other dependencies are stdlib.

### 2. Add your Alpaca credentials

Open `config.py` and replace the placeholders:

```python
ALPACA_API_KEY    = "YOUR_ALPACA_API_KEY_HERE"
ALPACA_API_SECRET = "YOUR_ALPACA_API_SECRET_HERE"
PAPER             = True   # always start on paper trading
```

Get your keys: [alpaca.markets](https://alpaca.markets) → Paper Trading → API Keys → Generate

### 3. Implement your strategy in `signals.py`

Open `signals.py` and implement `check_entry()`. The function receives:
- `side` — "call" or "put"
- `strike` — the option's strike price
- `option_quote` — current bid/ask/mid
- `momentum` — live momentum state (EMA5, EMA20, VWAP, ROC5, atr5, direction)
- `spy_price` — latest SPY price
- `atr5` — 5-bar rolling ATR (intrabar velocity)

Return `True` to trigger a limit buy. The framework handles sizing, order submission, fill confirmation, and position tracking automatically.

### 4. Run

```bash
python main.py
```

The bot waits for 9:30 ET, subscribes options at the actual open price, and becomes active at 9:45 ET. Force-closes all positions at 3:25 PM ET.

**Terminal controls:**
- `q + Enter` — quit cleanly (closes all positions)
- `r + Enter` — restart (leaves positions open, recovers on next start)
- `t + Enter` — print today's trade table

---

## How Exits Work (No Strategy Required)

Even with a placeholder `check_entry()`, the exit infrastructure is fully functional. Once a position is open, exits are **quote-driven** — `_evaluate_exit()` fires on every option quote tick for the held symbol via `asyncio.create_task()`. Sub-50ms from quote arrival to order submission.

```
On every option quote tick for the held symbol:
  1. Update peak_mid (highest option mid seen since entry)
  2. IF mid >= entry × TP_MULT      → close_position() → "tp"
  3. IF mid <= entry × STOP_MULT    → close_position() → "stop"
  4. IF peak_mid >= entry × PEAK_TRAIL_ACTIVATE:
       trail_stop = peak_mid × PEAK_TRAIL_PCT
       IF mid <= trail_stop         → close_position() → "peak_trail"

Every 30 seconds (safety net — covers WebSocket reconnect gaps):
  → re-evaluates the same conditions using the cached quote

At 3:25 PM ET (time stop watcher):
  → close_position() → "time_stop"
```

**Race-condition safety:** `exit_pending` is set to `True` before the first `await` in `_evaluate_exit()`. Because asyncio is cooperative (no preemption between synchronous lines), no duplicate exit orders can be submitted even when multiple quote ticks arrive simultaneously.

All exits are logged to `logs/trades_YYYY-MM-DD.csv` with entry price, exit price, quantity, reason, P&L, and timestamps.

---

## Momentum Engine

`momentum.py` computes five indicators per 1-minute bar:

| Indicator | Formula | Notes |
|---|---|---|
| EMA5 | Exponential MA, period=5 | Fast trend |
| EMA20 | Exponential MA, period=20 | Slow trend |
| VWAP | Sum(typical_price × vol) / Sum(vol) | Resets at 9:30 ET |
| ROC5 | (close - close[5]) / close[5] | 5-bar rate of change |
| atr5 | Avg(high - low) over last 5 bars | Intrabar velocity |

Pre-seeded with the last 30 historical RTH 1-min bars at startup so all indicators are meaningful from the first live bar.

BULL / BEAR / NEUTRAL direction is derived by combining all five — see `momentum.py` for the exact conditions.

---

## WebSocket vs REST — Why Both

A common question: why use WebSocket streaming for quotes instead of REST polling?

**The bot uses both — for different purposes:**

| Operation | Method | Reason |
|---|---|---|
| Real-time SPY 1-min bars | WebSocket (StockDataStream) | Pushed at bar close, zero polling overhead |
| Real-time option quotes | WebSocket (OptionDataStream) | Every quote tick, sub-10ms delivery |
| Order fill events | WebSocket (TradingStream) | Instant fill confirmation |
| Historical bars at startup | REST | One-shot fetch, no need for streaming |
| 5-day ATR baseline | REST | One-shot fetch at startup |
| Submit entry order | REST | `submit_order()` — synchronous, want confirmation |
| Close position | REST | `close_position()` — only working exit method |
| Recover open positions on restart | REST | `get_all_positions()` — definitive server state |

**Why not REST polling for option quotes?**

The bot subscribes to 42 option symbols simultaneously (21 calls + 21 puts). Polling all 42 every second = 42 REST requests/second. Alpaca's free tier allows ~200 requests/minute — you'd hit the rate limit in under 5 seconds.

Even if rate limits weren't an issue, REST polling introduces latency on every request (50–200ms per HTTP round trip). For 0DTE entries where you're trying to catch a move in progress, a WebSocket that pushes quotes as they happen is significantly more responsive.

There's also a data completeness issue: if an option spikes and reverses within a 1-second polling window, REST never sees the peak. WebSocket delivers every tick — the spike would update `peak_mid` and potentially arm the trailing stop. With REST polling, that recovery is invisible.

**Rule of thumb:** use WebSocket for anything that changes multiple times per second. Use REST for one-shot queries and order submission.

---

## Requirements

- Python 3.9+
- `alpaca-py >= 0.43.0`
- Alpaca account (free) with options trading enabled on paper
- macOS or Linux (Windows works but terminal display may vary)

---

## Contributing

Issues and PRs welcome. If you've found additional Alpaca API quirks not documented here, please open an issue — building a comprehensive list of limitations helps everyone in this space.

---

## Disclaimer

This software is for educational purposes only. It does not constitute financial advice. Paper trading performance does not guarantee live trading results. Use at your own risk.

---

*Built with Python 3.11 · alpaca-py · Paper Trading Only*
