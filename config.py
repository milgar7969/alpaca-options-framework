import os
from zoneinfo import ZoneInfo

# ── API credentials ────────────────────────────────────────────────────────────
ALPACA_API_KEY    = "YOUR_ALPACA_API_KEY_HERE"
ALPACA_API_SECRET = "YOUR_ALPACA_API_SECRET_HERE"
PAPER             = True   # set False only after paper validation passes

# ── Universe ───────────────────────────────────────────────────────────────────
UNDERLYING        = "SPY"

# ── Strike selection (pre-market) ──────────────────────────────────────────────
# Target strike = round(SPY_price + ATR_MULT * ATR_5day) to nearest STRIKE_STEP
ATR_MULT          = 0.60   # how far OTM to target (calibrate after backtesting)
STRIKE_STEP       = 0.50   # SPY strikes are in $0.50 increments
STRIKE_ALTS       = 10     # extra strikes above/below — wide window, never resubscribe

# ── Entry filters ──────────────────────────────────────────────────────────────
ENTRY_START       = "09:45"  # ET — ignore signals before this
ENTRY_END         = "14:30"  # ET — no new entries after this
TIME_STOP         = "15:25"  # ET — force-close all positions

OPTION_MIN_PRICE  = 0.20   # min price — filters deeply OTM lottery tickets
OPTION_MAX_PRICE  = 10.00  # max price

# Momentum thresholds
MIN_CONSEC_BARS   = 3      # consecutive green (or red) 1-min bars required
ROC_THRESHOLD     = 0.0003 # 5-bar rate-of-change minimum
ATR5_MIN_ENTRY    = 0.20   # min 5-bar ATR at entry — blocks low-vol theta-bleed setups

# Strike proximity zones
ACTIVATION_PCT    = 0.003  # within 0.3% of strike → "activation" zone (~$2.20 at SPY $740)
APPROACH_PCT      = 0.007  # within 0.7% → "approach" zone (~$5.20 at SPY $740)

# Proxy delta — disabled: delta only updates on 1-min bar ticks so stays 0 between
# bars and blocks all entries. Momentum + zone filters are sufficient gatekeepers.
PROXY_DELTA_MIN   = 0.0    # 0.0 = disabled (was 0.03)
REQUIRE_DELTA_RISING = False

# ── Risk & sizing ──────────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE = 150.00   # dollars at risk per trade (stop loss basis)
MAX_DAILY_LOSS     = 300.00   # hard daily loss limit — bot shuts down entries
MAX_TRADES_PER_DAY = 999      # effectively unlimited during paper/data collection
TRADE_COOLDOWN_BARS = 3       # 1-min bars to wait after a trade closes before re-entering

# ── Bracket order exit levels ──────────────────────────────────────────────────
STOP_MULT          = 0.50   # hard stop:          exit if price falls to 50% of entry
TP_MULT            = 1.50   # take-profit:        exit at 50% gain (1.5× entry)

# Peak trailing stop — locks in recoveries
# Activates once the option has gained PEAK_TRAIL_ACTIVATE above entry.
# From that point, trails at PEAK_TRAIL_PCT × the highest mid seen.
# Example: entry=$0.96, peak=$1.15 → trail fires at $1.012 (near BE)
PEAK_TRAIL_ACTIVATE = 1.20  # trail arm: option must reach 20% gain before trail is active
PEAK_TRAIL_PCT      = 0.88  # trail stop: exit if mid falls to 88% of peak

# ── Polling ────────────────────────────────────────────────────────────────────
SNAPSHOT_POLL_SEC  = 30    # how often to poll REST snapshot for proxy-delta calc

# ── Misc ───────────────────────────────────────────────────────────────────────
ET                 = ZoneInfo("America/New_York")
LOG_DIR            = "logs"
