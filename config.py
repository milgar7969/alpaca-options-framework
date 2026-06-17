import os
from zoneinfo import ZoneInfo

# ── API credentials ────────────────────────────────────────────────────────────
ALPACA_API_KEY    = "YOUR_ALPACA_API_KEY_HERE"
ALPACA_API_SECRET = "YOUR_ALPACA_API_SECRET_HERE"
PAPER             = True   # set False only after paper validation passes

# ── Universe ───────────────────────────────────────────────────────────────────
UNDERLYING        = "SPY"

# ── Strike selection ───────────────────────────────────────────────────────────
# Target strike = round(SPY_price + min(ATR_MULT * ATR_5day, MAX_STRIKE_OFFSET)) to nearest STRIKE_STEP
ATR_MULT          = 0.60   # strike offset as multiple of 5-day daily ATR
MAX_STRIKE_OFFSET = 4.50   # hard cap — prevents elevated ATR from placing strikes
                            # so far OTM that SPY never reaches the approach zone
STRIKE_STEP       = 0.50   # SPY strikes are in $0.50 increments
STRIKE_ALTS       = 10     # extra strikes above/below — wide window, never resubscribe

# ── Entry filters ──────────────────────────────────────────────────────────────
ENTRY_START       = "09:45"  # ET — ignore signals before this
ENTRY_END         = "14:30"  # ET — no new entries after this
TIME_STOP         = "15:25"  # ET — force-close all positions

OPTION_MIN_PRICE  = 0.20   # min price — filters deeply OTM lottery tickets
OPTION_MAX_PRICE  = 10.00  # max price

# ── Momentum thresholds ────────────────────────────────────────────────────────
MIN_CONSEC_BARS   = 3      # consecutive green (or red) 1-min bars required
ROC_THRESHOLD     = 0.0003 # 5-bar rate-of-change minimum
ATR5_MIN_ENTRY    = 0.20   # min 5-bar intrabar ATR at entry — blocks low-vel theta-bleed setups

# ── Strike proximity zones ─────────────────────────────────────────────────────
ACTIVATION_PCT    = 0.003  # within 0.3% of strike → "activation" zone
APPROACH_PCT      = 0.007  # within 0.7% → "approach" zone (outer band)

# ── Proxy delta (disabled) ─────────────────────────────────────────────────────
# Delta only updates on 1-min bar ticks so stays 0 between bars and blocks
# all entries if used as a filter. Momentum + zone filters are sufficient.
PROXY_DELTA_MIN      = 0.0
REQUIRE_DELTA_RISING = False

# ── Risk & sizing ──────────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE  = 150.00  # dollars at risk per trade (stop loss basis)
MAX_DAILY_LOSS      = 300.00  # hard daily loss limit — bot stops new entries
MAX_TRADES_PER_DAY  = 999     # effectively unlimited during paper/data collection
TRADE_COOLDOWN_BARS = 3       # 1-min bars to wait after a close before re-entering

# ── Exit levels ────────────────────────────────────────────────────────────────
STOP_MULT          = 0.50   # hard stop: exit if price falls to 50% of entry
TP_MULT            = 1.50   # take profit: exit at 50% gain (1.5× entry)

# ── Peak trailing stop ─────────────────────────────────────────────────────────
# Arms once the option gains PEAK_TRAIL_ACTIVATE above entry.
# From that point, trails at PEAK_TRAIL_PCT × the highest mid seen.
# Activation threshold must clear bid/ask spread noise on cheap options —
# 1.20× requires a genuine $0.05+ move on a $0.25 option; spread noise can't reach it.
PEAK_TRAIL_ACTIVATE = 1.20  # trail arms: option must reach 20% gain first
PEAK_TRAIL_PCT      = 0.88  # trail stop: exit if mid falls to 88% of peak

# ── SPY-level stop ─────────────────────────────────────────────────────────────
# Fires on bar close if SPY closes more than buf dollars against the position.
# Buffer scales with intrabar volatility at entry — tighter when calm, wider
# when choppy — reducing the whipsaw false-stops a fixed dollar amount causes.
SPY_STOP_ATR_MULT  = 0.75  # buffer = 0.75 × atr5 at entry time
SPY_STOP_FLOOR     = 0.10  # minimum buffer regardless of atr5

# ── Polling ────────────────────────────────────────────────────────────────────
SNAPSHOT_POLL_SEC  = 30    # how often to poll REST snapshot for proxy-delta calc

# ── Misc ───────────────────────────────────────────────────────────────────────
ET      = ZoneInfo("America/New_York")
LOG_DIR = "logs"
