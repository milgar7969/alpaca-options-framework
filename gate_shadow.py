"""
Gate-continuation shadow (observe-only).

Once a session-ending gate locks new entries — the $125 PROFIT_TARGET or the
CONSEC_WIN_STOP win streak — the real bot stops trading and "keeps the money."
This module answers the natural question: *what would the day have ended at if
we had NOT stopped?*

It does that the same honest way as exit_shadow: when a gate is locked and the
real bot would otherwise have taken an entry (check_entry passes, respecting the
noon cutoff and a mirror of the trade cooldown), we spawn a VIRTUAL position and
run it to a virtual exit on the live quote/bar stream, settling at the bid
exactly like close_position() does. No orders, no effect on the live bot.

At EOD we log the comparison:
  actual (stopped at the gate)  vs  counterfactual (gate P&L + continuation)

Each virtual trade also appends a row to logs/gate_shadow_YYYY-MM-DD.csv.

Safety: every public method is wrapped by the caller in try/except and this
module performs no I/O beyond logging + a CSV append. A bug here must never be
able to touch a live order.
"""

import csv
import datetime
import logging
import os

import config
from exit_shadow import _Twin   # reuse the validated baseline exit model (bid settlement)

logger = logging.getLogger(__name__)


class _ShadowPos:
    """Minimal Position-shaped object a _Twin can consume."""
    __slots__ = ("symbol", "side", "strike", "qty", "entry_price",
                 "entry_spy_price", "entry_atr5")

    def __init__(self, symbol, side, strike, qty, entry_price, entry_spy, entry_atr5):
        self.symbol = symbol; self.side = side; self.strike = strike; self.qty = qty
        self.entry_price = entry_price
        self.entry_spy_price = entry_spy; self.entry_atr5 = entry_atr5


class GateShadow:
    """Virtual continuation book that runs only after a gate locks the real bot."""

    def __init__(self):
        self._active        = False
        self._reason        = ""        # which gate locked ("profit target" / "win streak")
        self._pnl_at_lock   = 0.0       # real realized day P&L at the moment of lock
        self._cont_pnl      = 0.0       # summed P&L of virtual continuation trades
        self._n_cont        = 0
        self._open: _Twin   = None      # at most one virtual position at a time
        self._cooldown_bars = 0         # mirrors TRADE_COOLDOWN_BARS between virtual trades

    @property
    def active(self) -> bool:
        return self._active

    @property
    def has_open(self) -> bool:
        return self._open is not None and not self._open.closed

    def activate(self, reason: str, pnl_at_lock: float):
        """Called once, when a gate first locks the real bot for the day."""
        if self._active:
            return
        self._active      = True
        self._reason      = reason
        self._pnl_at_lock = pnl_at_lock
        logger.info("GATE_SHADOW | activated reason=%s day_pnl_at_lock=$%.2f — "
                    "logging would-have trades for the rest of the session",
                    reason, pnl_at_lock)

    # ── cadence ───────────────────────────────────────────────────────────────
    def tick_bar(self):
        if self._cooldown_bars > 0:
            self._cooldown_bars -= 1

    def can_enter(self) -> bool:
        """Mirror the real entry cadence: one virtual position at a time, cooldown between."""
        return self._active and not self.has_open and self._cooldown_bars <= 0

    def enter(self, symbol, side, strike, qty, entry_mid, spy_price, atr5):
        """Spawn a virtual baseline position for a would-have entry."""
        if not self.can_enter() or qty <= 0 or entry_mid <= 0:
            return
        pos = _ShadowPos(symbol, side, strike, qty, entry_mid, spy_price, atr5)
        self._open = _Twin("baseline", pos)
        logger.info("GATE_SHADOW | would-enter %s %s×%d @%.2f (reason=%s)",
                    side, symbol, qty, entry_mid, self._reason)

    # ── live quote / bar feed (same stream exit_shadow consumes) ────────────────
    def on_quote(self, symbol: str, mid: float, bid: float):
        if not self.has_open or self._open.symbol != symbol:
            return
        now = datetime.datetime.now(tz=config.ET)
        self._open.on_quote(mid, bid, now)
        self._settle_if_closed()

    def on_spy_bar(self, spy_close: float):
        if not self.has_open:
            return
        self._open.on_spy_bar(spy_close)
        self._settle_if_closed()

    def _settle_if_closed(self):
        t = self._open
        if t is None or not t.closed:
            return
        pnl = (t.exit_price - t.entry_price) * t.qty * 100
        self._cont_pnl += pnl
        self._n_cont   += 1
        self._cooldown_bars = config.TRADE_COOLDOWN_BARS
        logger.info("GATE_SHADOW | would-exit %s reason=%s pnl=$%.2f | continuation=$%.2f",
                    t.symbol, t.exit_reason, pnl, self._cont_pnl)
        self._append_csv(t, pnl)
        self._open = None

    def _append_csv(self, t, pnl):
        try:
            day = datetime.datetime.now(tz=config.ET).strftime("%Y-%m-%d")
            path = os.path.join(config.LOG_DIR, f"gate_shadow_{day}.csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["date", "lock_reason", "symbol", "side", "qty",
                                "entry", "exit", "reason", "pnl", "cont_total"])
                w.writerow([day, self._reason, t.symbol, t.side, t.qty,
                            f"{t.entry_price:.2f}", f"{t.exit_price:.2f}",
                            t.exit_reason, f"{pnl:.2f}", f"{self._cont_pnl:.2f}"])
        except OSError as e:
            logger.error("GATE_SHADOW csv append failed (ignored): %s", e)

    # ── EOD ─────────────────────────────────────────────────────────────────
    def eod_summary(self, actual_day_pnl: float):
        """Called at the 15:25 time stop. Settles any open twin, logs the comparison."""
        if not self._active:
            return
        if self.has_open:
            self._open._close("time_stop")
            self._settle_if_closed()
        counterfactual = self._pnl_at_lock + self._cont_pnl
        delta = counterfactual - actual_day_pnl
        verdict = ("gate SAVED money" if delta < 0 else
                   "gate COST money" if delta > 0 else "gate neutral")
        logger.warning(
            "GATE_SHADOW | EOD reason=%s | actual(stopped)=$%.2f | "
            "would-have=$%.2f (lock $%.2f + %d cont trades $%.2f) | delta=$%+.2f → %s",
            self._reason, actual_day_pnl, counterfactual, self._pnl_at_lock,
            self._n_cont, self._cont_pnl, delta, verdict,
        )

    def reset_day(self):
        self.__init__()
