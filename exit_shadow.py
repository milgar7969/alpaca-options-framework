"""
Exit-parameter shadow harness (observe-only).

Spawns virtual copies ("twins") of every real position under alternative exit
parameters and runs them against the same live quote/bar stream. Twins never
place orders and never touch the live position — they exist only to answer
"what would a different exit rule have produced?" with real market data.

Why a harness and not a log-and-compare:
  - A TIGHTER trail fires EARLIER than the live trail, so it is observable
    while we still hold. Exact, no modelling.
  - A WIDER stop fires LATER — the live stop closes the real position first
    and reality goes dark. The twin must therefore outlive the real exit and
    keep consuming quotes until its own rule triggers.

Variants (see VARIANTS below):
  baseline    — mirrors live config. Sanity check: if this does not reproduce
                the real exits closely, the harness is wrong and the other
                variants cannot be trusted.
  tight_trail — PEAK_TRAIL_PCT 0.92 (live 0.88): give back 8% of peak, not 12%.
  wide_stop   — SPY_STOP_FLOOR 0.25 / ATR_MULT 1.00 (live 0.10 / 0.75).
  both        — tight_trail + wide_stop combined.

Each twin exit emits one line, parsed by kpi_dashboard.py:
  EXIT_SHADOW | variant=<name> symbol=<occ> entry=<f> exit=<f> qty=<n>
              reason=<tp|stop|spy_stop|peak_trail|time_stop|no_quote> pnl=<f>

Safety: every public method is wrapped by the caller in try/except and this
module performs no I/O beyond logging. A bug in here must never be able to
affect a live order.
"""

import datetime
import logging

import config

logger = logging.getLogger(__name__)

# variant name -> parameter overrides (anything omitted falls back to live config)
VARIANTS = {
    "baseline":    {},
    "tight_trail": {"trail_pct": 0.92},
    "wide_stop":   {"stop_floor": 0.25, "stop_mult": 1.00},
    "both":        {"trail_pct": 0.92, "stop_floor": 0.25, "stop_mult": 1.00},
    # no_spy_stop: disable the underlying-based SPY stop entirely. The only
    # protections left are TP (1.50x), the peak trail (which arms only after a
    # 1.20x run-up, so it does nothing for a trade red from entry), and the hard
    # option stop at 0.50x. Tests whether recovered false-stops outweigh the
    # deeper losses on genuine adverse moves that now ride down to -50%.
    "no_spy_stop": {"disable_spy_stop": True},
}

# Drop a twin that has heard nothing for this long (symbol fell out of the
# subscription window) rather than settling it at a stale price.
_QUOTE_STALE_SECS = 900


class _Twin:
    """One virtual position under one variant's parameters."""

    __slots__ = ("variant", "symbol", "side", "strike", "qty", "entry_price",
                 "entry_spy", "entry_atr5", "peak_mid", "last_mid", "last_bid",
                 "last_quote_t", "closed", "exit_price", "exit_reason")

    def __init__(self, variant, pos):
        self.variant      = variant
        self.symbol       = pos.symbol
        self.side         = pos.side
        self.strike       = pos.strike
        self.qty          = pos.qty
        self.entry_price  = pos.entry_price
        self.entry_spy    = pos.entry_spy_price
        self.entry_atr5   = pos.entry_atr5
        self.peak_mid     = pos.entry_price
        self.last_mid     = pos.entry_price
        self.last_bid     = pos.entry_price   # settlement price; updated per quote
        self.last_quote_t = datetime.datetime.now(tz=config.ET)
        self.closed       = False
        self.exit_price   = 0.0
        self.exit_reason  = ""

    # ── parameter resolution ────────────────────────────────────────────────
    def p(self, key, default):
        return VARIANTS[self.variant].get(key, default)

    @property
    def trail_pct(self):   return self.p("trail_pct",  config.PEAK_TRAIL_PCT)
    @property
    def stop_floor(self):  return self.p("stop_floor", config.SPY_STOP_FLOOR)
    @property
    def stop_mult(self):   return self.p("stop_mult",  config.SPY_STOP_ATR_MULT)

    # ── evaluation ──────────────────────────────────────────────────────────
    # Triggers evaluate on the MID (mirrors main._evaluate_exit, which decides
    # on quote.mid). Settlement uses the BID, because every live exit routes
    # through close_position() — a market sell that fills at the bid. Modelling
    # both keeps the baseline twin honest against the real booked fills.
    def on_quote(self, mid: float, bid: float, now: datetime.datetime):
        """Mirror main._evaluate_exit ordering: TP, hard stop, peak trail."""
        if self.closed or mid <= 0 or bid <= 0:
            return
        self.last_mid     = mid
        self.last_bid     = bid
        self.last_quote_t = now
        if mid > self.peak_mid:
            self.peak_mid = mid

        if mid >= self.entry_price * config.TP_MULT:
            self._close("tp")
        elif mid <= self.entry_price * config.STOP_MULT:
            self._close("stop")
        elif self.peak_mid >= self.entry_price * config.PEAK_TRAIL_ACTIVATE:
            if mid <= self.peak_mid * self.trail_pct:
                self._close("peak_trail")

    def on_spy_bar(self, spy_close: float):
        """Mirror main._evaluate_spy_stop."""
        if self.closed or self.entry_spy <= 0:
            return
        if self.p("disable_spy_stop", False):
            return   # no_spy_stop variant — underlying stop removed entirely
        buf = max(self.stop_floor, self.stop_mult * self.entry_atr5)
        if self.side == "call" and spy_close < self.entry_spy - buf:
            self._close("spy_stop")
        elif self.side == "put" and spy_close > self.entry_spy + buf:
            self._close("spy_stop")

    def _close(self, reason: str):
        """Settle at the last known bid — a market close fills at the bid."""
        self.closed      = True
        self.exit_price  = self.last_bid
        self.exit_reason = reason
        pnl = (self.last_bid - self.entry_price) * self.qty * 100
        logger.info(
            "EXIT_SHADOW | variant=%s symbol=%s entry=%.4f exit=%.4f qty=%d "
            "reason=%s pnl=%.2f",
            self.variant, self.symbol, self.entry_price, self.last_bid, self.qty,
            reason, pnl,
        )


class ExitShadow:
    """Owns all live twins. All methods are no-throw by contract."""

    def __init__(self):
        self._twins: list[_Twin] = []

    def on_entry(self, pos):
        """Spawn one twin per variant for a newly opened real position."""
        for name in VARIANTS:
            self._twins.append(_Twin(name, pos))
        logger.info(
            "EXIT_SHADOW | spawned %d twins for %s entry=%.4f qty=%d",
            len(VARIANTS), pos.symbol, pos.entry_price, pos.qty,
        )

    def on_quote(self, symbol: str, mid: float, bid: float):
        if not self._twins:
            return
        now = datetime.datetime.now(tz=config.ET)
        for t in self._twins:
            if t.symbol == symbol:
                t.on_quote(mid, bid, now)
        self._reap(now)

    def on_spy_bar(self, spy_close: float):
        if not self._twins:
            return
        for t in self._twins:
            t.on_spy_bar(spy_close)
        self._reap(datetime.datetime.now(tz=config.ET))

    def close_all(self, reason: str = "time_stop"):
        """Force-settle every open twin (called at the 15:25 time stop)."""
        for t in self._twins:
            if not t.closed:
                t._close(reason)
        self._twins.clear()

    def _reap(self, now: datetime.datetime):
        """Drop closed twins; discard twins whose quotes have gone stale."""
        alive = []
        for t in self._twins:
            if t.closed:
                continue
            if (now - t.last_quote_t).total_seconds() > _QUOTE_STALE_SECS:
                t._close("no_quote")   # flagged; excluded from stats
                continue
            alive.append(t)
        self._twins = alive

    @property
    def open_count(self) -> int:
        return sum(1 for t in self._twins if not t.closed)
