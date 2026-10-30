# Alpaca Options Bot Framework

A production-ready Python framework for building **live options trading bots on Alpaca Markets**.

This repo solves the hard infrastructure problems — real-time streaming, position management, order execution, and all the Alpaca API quirks — so you can focus on your strategy.

> **Note:** This repo includes the full working 0DTE SPY gamma explosion strategy — all parameters, the live entry/exit logic in `signals.py`, and the complete 22-page strategy manual ([`SPY_0DTE_Strategy.pdf`](./SPY_0DTE_Strategy.pdf)) covering the options physics, every design decision, live session post-mortems (including the losing days), and the bug write-ups. Live results: [milgar7969.github.io/spy-0dte-dashboard](https://milgar7969.github.io/spy-0dte-dashboard/)

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

**8. A cancelled buy can still fill**
If an `asyncio.CancelledError` tears through your buy task mid-fill-wait (WebSocket teardown, task cancellation), cancelling the order on Alpaca is not guaranteed to win the race — the order can fill anyway, leaving a position your bot doesn't know it owns. The framework defends in depth: `buy_limit()` re-checks the order status after a cancelled wait and returns the fill if it landed (so the position is tracked normally), and a bar-driven **ghost sweeper** polls REST every minute and force-closes any option position that isn't tracked locally — worst-case exposure is ~60 seconds.

**9. Feed teardown can hang the event loop at end-of-day**
Stopping the WebSocket streams (`feed.stop()`) can block the event loop for 20+ seconds. If you run a watchdog that restarts on a silent loop, this creates a restart cascade: each fresh boot is already past the time stop, immediately re-fires it, hangs again, restarts again. The framework sidesteps the teardown entirely — after the time stop closes all positions, it **verifies the account is flat via REST (with retries), then hard-exits with `os._exit(0)`**. Observed live: flat-check plus exit completes in under 100ms.

**10. The interrupt that hits your buy isn't always a `CancelledError`**
Quirk #8's recovery path never fired live: when a buy gets interrupted mid-fill-wait, the exception that actually arrives is often `AttributeError: 'CANCELLED'` from inside alpaca-py, not `asyncio.CancelledError`. Meanwhile the next quote tick sees no position and fires a *second* buy, which leaves you with a duplicate fill. The framework catches `(asyncio.CancelledError, Exception)` around the buy. On any ambiguous outcome it keeps `entry_pending` **latched** so no duplicate can fire, cancels open option orders, and auto-releases the latch after 20 seconds so entries can't wedge. A late fill that slips through is still caught by the ghost sweeper.

---

## Architecture

```
asyncio.gather(
    feed.start(),               # StockDataStream + OptionDataStream + TradingStream
    _option_subscriber(),       # waits for 9:30 ET bar → subscribes strikes
    _resubscribe_watcher(),     # re-subscribes if SPY moves ±$3
    _exit_monitor(),            # 30-second safety net (WebSocket reconnect gaps)
    _time_stop_watcher(),       # after noon once flat, or 3:25 PM ET: close all → verify flat → clean exit
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
├── risk.py         Position sizing, daily loss / profit-target / win-streak gates,
│                     cooldown, P&L restore
├── strikes.py      ATR calculation, dynamic strike selection, OCC symbol builder
├── momentum.py     EMA5/EMA20, VWAP, ROC5, atr5, consecutive bar engine
│
├── orb_filter.py   Clock-hour ORB directional regime filter (shadow mode)
├── exit_shadow.py  Exit-parameter shadow: virtual twins under alternative exit rules
├── gate_shadow.py  Gate-continuation shadow: what if we hadn't stopped for the day?
├── kpi_dashboard.py  Self-contained HTML KPI report generator (see below)
│
└── signals.py      Entry and exit signal logic — the LIVE strategy, as traded
                      daily on paper: momentum match, ATR velocity gate, strike
                      proximity zones, ITM guard, proxy delta.
```

**The full strategy ships in `signals.py`** — this is the exact entry logic the public dashboard results come from, not a demo. Run it as-is, or swap in your own logic behind the same interface.

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

### 3. (Optional) Adjust the strategy in `signals.py`

The live entry logic ships ready to run — no implementation required. `check_entry()` receives:
- `side` — "call" or "put"
- `strike` — the option's strike price
- `option_quote` — current bid/ask/mid
- `momentum` — live momentum state (EMA5, EMA20, VWAP, ROC5, atr5, direction)
- `spy_price` — latest SPY price
- `atr5` — 5-bar rolling ATR (intrabar velocity)

Return `True` to trigger a limit buy. The framework handles sizing, order submission, fill confirmation, and position tracking automatically — so replacing the shipped strategy with your own is a single-function change.

### 4. Run

```bash
python main.py
```

The bot waits for 9:30 ET, subscribes options at the actual open price, and takes new entries from 9:45 ET until noon (`ENTRY_END = "12:00"`; afternoons bled on paper, so the window was cut from 14:30). Open positions keep managing their exits after noon. Once the bot is past the cutoff **and** fully done (no position, no order in flight, no open shadow twin), it runs the end-of-day routine and exits early, because nothing after that point can trade. Otherwise it force-closes all positions at 3:25 PM ET.

**Terminal controls:**
- `q + Enter` — quit cleanly (closes all positions)
- `r + Enter` — restart (leaves positions open, recovers on next start)
- `t + Enter` — print today's trade table
- `o + Enter` — print the watched option window: every entry-eligible symbol with live bid/ask/mid and quote age. A row stuck on "no quote yet" after the open usually means the contract doesn't exist; check that `STRIKE_STEP` matches your underlying's real strike spacing.

---

## How Exits Work

The exit infrastructure is independent of the entry strategy. Once a position is open, exits are **quote-driven** — `_evaluate_exit()` fires on every option quote tick for the held symbol via `asyncio.create_task()`. Sub-50ms from quote arrival to order submission.

```
On every option quote tick for the held symbol:
  1. Update peak_mid (highest option mid seen since entry)
  2. IF mid >= entry × TP_MULT      → close_position() → "tp"
  3. IF mid <= entry × STOP_MULT    → close_position() → "stop"
  4. IF peak_mid >= entry × PEAK_TRAIL_ACTIVATE:
       trail_stop = peak_mid × PEAK_TRAIL_PCT
       IF mid <= trail_stop         → close_position() → "peak_trail"

On every SPY 1-min bar close:
  5. buf = max(SPY_STOP_FLOOR, SPY_STOP_ATR_MULT × atr5_at_entry)
     IF SPY closed more than buf past the entry SPY price, against the position
                                    → close_position() → "spy_stop"

Every 30 seconds (safety net — covers WebSocket reconnect gaps):
  → re-evaluates the same conditions using the cached quote

At 3:25 PM ET (time stop watcher):
  → close_position() → "time_stop"
```

In practice the **SPY-level stop is the real loss exit**: it cuts a trade when the *underlying* moves against you, long before a 0DTE option decays to the 50% hard stop. The buffer scales with intrabar volatility at entry, so it's tighter in calm tape and wider in chop.

**Session gates (new entries only):** besides the daily loss limit, `PROFIT_TARGET` (default $125 realized) and `CONSEC_WIN_STOP` (default 3 wins in a row) lock new entries for the rest of the day. An open position still exits normally. Both reset each morning. Across a mid-day restart the P&L-based locks are restored, but the win-streak counter resets.

**Race-condition safety:** `exit_pending` is set to `True` before the first `await` in `_evaluate_exit()`. Because asyncio is cooperative (no preemption between synchronous lines), no duplicate exit orders can be submitted even when multiple quote ticks arrive simultaneously.

All exits are logged to `logs/trades_YYYY-MM-DD.csv` with entry price, exit price, quantity, reason, P&L, and timestamps.

---

## Shadow Filters — Validate Before You Gate

`orb_filter.py` demonstrates a pattern worth stealing even if you don't use the filter itself: **run a new filter in observe-only mode before letting it block live trades.**

The filter tracks each clock hour's opening-range high/low (first 1-min bar of the hour) and compares it to the prior hour's: both higher → call-only bias, both lower → put-only bias, mixed → no restriction. Wired into the entry path, `check_shadow()` logs what it *would* have done on every real entry signal:

```
ORB_SHADOW | BLOCK: SPY260702C00753000 side=call bias=put — trade allowed (shadow mode)
ORB_SHADOW | ALLOW: SPY260702P00746000 side=put bias=put
```

It always returns `True` — zero effect on trading. After enough sessions you can score every BLOCK against the trade's actual P&L and decide with data, not backtest hope, whether to flip it live (a two-line change). Backtests lie in subtle ways — pre-market data availability, restart behavior, bar timing. Shadow mode tests the filter in the exact code path that would run it.

The same idea, extended to exits and session gates:

- **`exit_shadow.py`**: on every real entry it spawns virtual "twins" under alternative exit parameters (a tighter trail, a wider SPY-stop, no SPY-stop at all) plus a `baseline` twin that mirrors the live config. All twins consume the same live quote stream and settle at the bid. A wider stop fires *later* than the live one, so twins outlive the real exit and keep tracking quotes until their own rule triggers. Compare each variant against `baseline` rather than against booked P&L: they share the same fill model, so the comparison is apples-to-apples.
- **`gate_shadow.py`**: once a profit-target or win-streak gate locks the bot, it keeps taking the entries the bot *would* have taken as virtual positions, with the same cooldown, noon cutoff and sizer. At the time stop it logs `actual vs would-have`, so you can tell whether stopping saved money or cost it.

Twins place no orders, and every call into them is wrapped in try/except, so a bug there can't touch a live order. One lesson from running them: a variant that looks great for a few weeks can flip sign when the market regime changes. Give a shadow months, not days, before you ship it.

---

## KPI Dashboard

`kpi_dashboard.py` generates a self-contained dark-theme HTML report (Chart.js via CDN, no build step) from the framework's own logs:

- Equity curve and daily P&L over a rolling calendar window (`--days 30`)
- Win rate, profit factor, EV/trade, breakdowns by exit reason / side / entry hour
- **True account P&L**: parses ghost-sweeper closes out of the bot logs and adds them to the booked CSV totals — duplicate fills don't silently vanish from your stats
- Shadow scoreboards: ORB filter (actual vs. would-have-been-filtered, every block listed) and exit-variant running tallies vs. baseline

```bash
python kpi_dashboard.py                      # builds every window: index.html (30d) + index-60/90/180/360.html
python kpi_dashboard.py --days 90 --out dashboard/index.html   # one window only
```

Each page carries a 30 / 60 / 90 / 180 / 360-day selector bar linking the others.

Push the output to any static host (GitHub Pages works fine) for a public, auto-updating track record.

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

- **Educational purposes only.** This is a personal project. Nothing here is financial or investment advice, and the author is not a licensed financial advisor.
- **Paper results are not live results.** All published results (README, strategy manual, dashboard) come from Alpaca **paper trading**, with simulated fills and no real money. Live trading can differ materially: slippage, partial fills, liquidity, and order rejections.
- **Past performance does not guarantee future results.** Paper or live, a profitable stretch says nothing certain about the next one.
- **0DTE options are high risk.** They can lose 100% of the premium within minutes.
- **No warranty, no liability.** The software is provided "as is" (see [LICENSE](./LICENSE)). The author accepts no liability for any losses or damages from using it. You are solely responsible for any orders it places on your account.
- **Test on paper first.** Run it on a paper account, and read the code, before you ever point it at real money.

---

*Built with Python 3.11 · alpaca-py · Paper Trading Only*
