"""
Signal engine — entry and exit logic.

This file is the strategy layer. The framework ships with placeholder
implementations so you can plug in your own logic.

A complete 0DTE momentum strategy implementation (entry filters, ATR gate,
zone checks, proxy delta, exit rules) is available as a paid add-on at:
https://gumroad.com/milgar7969  [coming soon]

Key concepts
------------
- check_entry()  : called on every option quote tick when no position is open.
                   Return True to trigger a limit buy order.
- check_exit()   : evaluated every 5 seconds by the exit monitor.
                   Returns an ExitSignal describing what action to take.
- ProxyDeltaTracker : estimates option delta from price-change ratio.
                      Disabled in Phase 0 (SPY price only updates 1x/min).
"""

import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

import config
from momentum import MomentumState

logger = logging.getLogger(__name__)


@dataclass
class Quote:
    symbol:    str
    bid:       float
    ask:       float
    timestamp: datetime.datetime

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ask or self.bid


@dataclass
class ProxyDeltaTracker:
    """
    Estimates delta = Δoption_price / ΔSPY_price.
    Updated on each option quote tick paired with the latest SPY price.

    Note: disabled in Phase 0 because SPY price only updates once per
    1-min bar. Between bars the denominator is 0 and delta stays flat.
    Re-enable when tick-level SPY price is available (e.g. from the
    underlying_price field in OptionDataStream snapshots).
    """
    _prev_option_mid: Optional[float] = field(default=None, repr=False)
    _prev_spy_price:  Optional[float] = field(default=None, repr=False)
    _prev_ts:         Optional[datetime.datetime] = field(default=None, repr=False)

    proxy_delta:  float = 0.0
    delta_rising: bool  = False

    def update(self, option_mid: float, spy_price: float, ts: datetime.datetime) -> float:
        if (
            self._prev_option_mid is not None
            and self._prev_spy_price is not None
            and spy_price != self._prev_spy_price
        ):
            d_opt = option_mid - self._prev_option_mid
            d_spy = spy_price  - self._prev_spy_price
            new_delta = d_opt / d_spy if d_spy != 0 else self.proxy_delta
            new_delta = max(-1.0, min(1.0, new_delta))
            self.delta_rising = new_delta > self.proxy_delta
            self.proxy_delta  = new_delta

        self._prev_option_mid = option_mid
        self._prev_spy_price  = spy_price
        self._prev_ts         = ts
        return self.proxy_delta


def _in_entry_window() -> bool:
    now_et = datetime.datetime.now(tz=config.ET).strftime("%H:%M")
    return config.ENTRY_START <= now_et <= config.ENTRY_END


def _past_time_stop() -> bool:
    now_et = datetime.datetime.now(tz=config.ET).strftime("%H:%M")
    return now_et >= config.TIME_STOP


def _zone(spy_price: float, strike: float) -> str:
    """Return proximity zone of SPY relative to the strike."""
    dist_pct = abs(strike - spy_price) / spy_price
    if dist_pct <= config.ACTIVATION_PCT:
        return "activation"
    if dist_pct <= config.APPROACH_PCT:
        return "approach"
    return "dead"


# ── Entry signal ──────────────────────────────────────────────────────────────

def check_entry(
    *,
    side:          str,               # "call" or "put"
    strike:        float,
    option_quote:  Quote,
    momentum:      MomentumState,
    proxy_tracker: ProxyDeltaTracker,
    spy_price:     float,
    trades_today:  int,
    has_open_pos:  bool,
    atr5:          float = 0.0,
) -> bool:
    """
    Return True when all entry conditions are satisfied.

    This is a placeholder implementation — it checks the structural
    guards (time window, position limit, daily trade cap) but does
    NOT implement any directional or signal logic.

    Replace this function with your own strategy. A complete
    implementation is available at: https://gumroad.com/milgar7969

    Suggested filters to implement:
        - Momentum direction match (BULL for calls, BEAR for puts)
        - ATR5 velocity gate (atr5 >= config.ATR5_MIN_ENTRY)
        - Strike proximity zone check (activation only)
        - Option price range (OPTION_MIN_PRICE / OPTION_MAX_PRICE)
        - ITM guard (call: spy < strike, put: spy > strike)
        - Proxy delta minimum (if tick-level SPY price available)
    """
    # ── Structural guards (always required) ───────────────────────────────────
    if has_open_pos:
        return False
    if trades_today >= config.MAX_TRADES_PER_DAY:
        return False
    if not _in_entry_window():
        return False

    # ── Add your signal logic below ───────────────────────────────────────────
    # Example structure:
    #
    # required_direction = "bull" if side == "call" else "bear"
    # if momentum.direction != required_direction:
    #     return False
    #
    # if atr5 < config.ATR5_MIN_ENTRY:
    #     return False
    #
    # price = option_quote.mid
    # if price < config.OPTION_MIN_PRICE or price > config.OPTION_MAX_PRICE:
    #     return False
    #
    # if side == "call" and spy_price >= strike:
    #     return False
    # if side == "put"  and spy_price <= strike:
    #     return False
    #
    # zone = _zone(spy_price, strike)
    # if zone != "activation":
    #     return False
    #
    # return True

    return False   # default: no entries until you implement your logic


# ── Exit signal ───────────────────────────────────────────────────────────────

@dataclass
class ExitSignal:
    should_exit:  bool = False
    reason:       str  = ""
    partial_exit: bool = False
    qty_to_close: int  = 0


def check_exit(
    *,
    entry_price:  float,
    current_mid:  float,
    qty_held:     int,
    target1_hit:  bool,
    momentum:     MomentumState,
    side:         str,
) -> ExitSignal:
    """
    Evaluate exit conditions and return an ExitSignal.

    Note: in this framework the primary exit logic runs inline in
    main._exit_monitor() (TP, stop, peak trail). This function is
    provided as an extension point for additional exit rules such as:
        - Momentum flip (BEAR regime while holding a call)
        - Time-based theta exit (held > N min and mid < entry × 0.90)
        - Partial profit taking at an intermediate target

    Return ExitSignal(False) to take no action.
    """
    if _past_time_stop():
        return ExitSignal(True, "time_stop", partial_exit=False, qty_to_close=qty_held)

    # Add your exit rules here
    return ExitSignal(False)
