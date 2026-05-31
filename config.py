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

# ── Momentum thresholds — calibrate for your strategy ─────────────────────────
# These values drive the BULL/BEAR signal in momentum.py.
# Start conservative and loosen based on your live session data.
# Calibrated values are included in the full strategy package.
MIN_CONSEC_BARS   = None   # int: consecutive green/red bars required (e.g. 2–4)
ROC_THRESHOLD     = None   # float: 5-bar ROC minimum (e.g. 0.0002–0.0005)
ATR5_MIN_ENTRY    = None   # float: min intrabar velocity gate (e.g. 0.15–0.25)

# ── Strike proximity zones — calibrate for your strategy ──────────────────────
# How close to the strike SPY must be before an entry is considered.
# Tighter = fewer but higher-quality entries.
# Calibrated values are included in the full strategy package.
ACTIVATION_PCT    = None   # float: "activation" zone radius as % of SPY price
APPROACH_PCT      = None   # float: "approach" zone radius (wider outer band)

# ── Proxy delta (disabled by default) ─────────────────────────────────────────
# Delta only updates on 1-min bar ticks so stays 0 between bars.
# Set PROXY_DELTA_MIN = 0.0 to disable entirely.
PROXY_DELTA_MIN      = 0.0
REQUIRE_DELTA_RISING = False

# ── Risk & sizing ──────────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE  = 150.00  # dollars at risk per trade (stop loss basis)
MAX_DAILY_LOSS      = 300.00  # hard daily loss limit — bot stops new entries
MAX_TRADES_PER_DAY  = 999     # effectively unlimited; lower for live trading
TRADE_COOLDOWN_BARS = 3       # bars to wait after any close before re-entering

# ── Exit levels — calibrate for your strategy ─────────────────────────────────
# These multipliers apply to the entry fill price.
# Standard starting points: stop=0.50, tp=1.50 — adjust based on your W/L ratio.
# Calibrated values are included in the full strategy package.
STOP_MULT          = None   # float: hard stop (e.g. 0.40–0.60)
TP_MULT            = None   # float: take profit (e.g. 1.40–2.00)

# ── Peak trailing stop — calibrate for your strategy ──────────────────────────
# Arms once the option reaches PEAK_TRAIL_ACTIVATE × entry price.
# Then trails at PEAK_TRAIL_PCT × the highest mid seen.
# Threshold must clear normal bid/ask spread noise for cheap options.
# Calibrated values are included in the full strategy package.
PEAK_TRAIL_ACTIVATE = None  # float: min gain before trail arms (e.g. 1.10–1.30)
PEAK_TRAIL_PCT      = None  # float: trail as % of peak (e.g. 0.85–0.92)

# ── Polling ────────────────────────────────────────────────────────────────────
SNAPSHOT_POLL_SEC  = 30    # how often to poll REST snapshot for proxy-delta calc

# ── Polling ────────────────────────────────────────────────────────────────────
SNAPSHOT_POLL_SEC  = 30    # how often to poll REST snapshot for proxy-delta calc

# ── Misc ───────────────────────────────────────────────────────────────────────
ET      = ZoneInfo("America/New_York")
LOG_DIR = "logs"

# ── Startup validation ─────────────────────────────────────────────────────────
# Fails immediately if required strategy values are not set.
_REQUIRED = {
    "MIN_CONSEC_BARS":    MIN_CONSEC_BARS,
    "ROC_THRESHOLD":      ROC_THRESHOLD,
    "ATR5_MIN_ENTRY":     ATR5_MIN_ENTRY,
    "ACTIVATION_PCT":     ACTIVATION_PCT,
    "APPROACH_PCT":       APPROACH_PCT,
    "STOP_MULT":          STOP_MULT,
    "TP_MULT":            TP_MULT,
    "PEAK_TRAIL_ACTIVATE":PEAK_TRAIL_ACTIVATE,
    "PEAK_TRAIL_PCT":     PEAK_TRAIL_PCT,
}
_missing = [k for k, v in _REQUIRED.items() if v is None]
if _missing:
    raise ValueError(
        f"\n\nconfig.py: the following strategy parameters are not set:\n"
        + "\n".join(f"  {k} = None" for k in _missing)
        + "\n\nImplement your own values, or get the calibrated full strategy package at:\n"
        + "  https://gumroad.com/milgar7969  [coming soon]\n"
    )
