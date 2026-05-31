# Alpaca Options Bot Framework

A production-ready Python framework for building **live options trading bots on Alpaca Markets**.

This repo solves the hard infrastructure problems — real-time streaming, position management, order execution, and all the Alpaca API quirks — so you can focus on your strategy.

> **Note:** This is the open-source infrastructure framework. A complete working strategy (0DTE SPY gamma explosion bot with live-tested entry/exit logic and full documentation) is available separately at [gumroad link — coming soon].

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
    _exit_monitor(),            # TP / stop / trail check every 5 seconds
    _time_stop_watcher(),       # force-close everything at 3:25 PM ET
    _status_loop(),             # live terminal display
)
```

Three concurrent WebSocket streams. Entry logic runs in the option quote handler. Exit logic is fully decoupled in its own task — `close_position()` cannot be called safely from inside a stream callback.

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

## What the Exit Monitor Does (No Strategy Required)

Even with a placeholder `check_entry()`, the exit infrastructure is fully functional. Once you open a position manually or via code, the framework manages it:

```
Every 5 seconds:
  1. Update peak_mid (highest option mid seen since entry)
  2. IF mid >= entry × TP_MULT      → close_position() → "tp"
  3. IF mid <= entry × STOP_MULT    → close_position() → "stop"
  4. IF peak_mid >= entry × 1.20:
       trail_stop = peak_mid × 0.88
       IF mid <= trail_stop         → close_position() → "peak_trail"

At 3:25 PM ET:
  → close_position() → "time_stop"
```

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
