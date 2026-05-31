"""
Global mutable state for the bot session.

Keeps track of:
  - Current open position (at most one at a time in Phase 0)
  - Latest SPY price (updated from stock stream)
  - Latest option quotes (updated from option stream, keyed by symbol)
  - Proxy delta trackers per symbol
  - Append-only CSV trade log
"""

import csv
import datetime
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

import config
from signals import ProxyDeltaTracker, Quote

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol:        str
    side:          str            # "call" or "put"
    strike:        float
    qty:           int
    entry_price:   float          # option mid at fill
    entry_time:    datetime.datetime
    order_id:      str
    target1_hit:   bool  = False
    qty_remaining: int   = 0
    peak_mid:      float = 0.0   # highest option mid seen since entry — for peak trailing stop

    def __post_init__(self):
        if self.qty_remaining == 0:
            self.qty_remaining = self.qty
        if self.peak_mid == 0.0:
            self.peak_mid = self.entry_price


class BotState:
    def __init__(self):
        self.spy_price:   float = 0.0
        self.position:    Optional[Position] = None
        self.exit_pending: bool = False   # True while a close_position order is in-flight

        # Latest quotes per option symbol  {symbol: Quote}
        self.option_quotes: Dict[str, Quote] = {}

        # Proxy delta tracker per symbol
        self.delta_trackers: Dict[str, ProxyDeltaTracker] = {}

        today = datetime.date.today().strftime("%Y-%m-%d")
        self._log_path = os.path.join(config.LOG_DIR, f"trades_{today}.csv")
        self._ensure_log()

    # ── Quote helpers ─────────────────────────────────────────────────────────

    def update_option_quote(self, symbol: str, bid: float, ask: float, ts: datetime.datetime):
        self.option_quotes[symbol] = Quote(symbol=symbol, bid=bid, ask=ask, timestamp=ts)

        if self.spy_price > 0:
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (ask or bid)
            tracker = self.delta_trackers.setdefault(symbol, ProxyDeltaTracker())
            tracker.update(mid, self.spy_price, ts)

    def get_quote(self, symbol: str) -> Optional[Quote]:
        return self.option_quotes.get(symbol)

    def get_tracker(self, symbol: str) -> ProxyDeltaTracker:
        return self.delta_trackers.setdefault(symbol, ProxyDeltaTracker())

    # ── Position helpers ──────────────────────────────────────────────────────

    def open_position(self, pos: Position):
        if self.position is not None:
            logger.error("Attempted to open a second position while one is already open.")
            return
        self.position = pos
        logger.info("Position opened: %s", pos)

    def close_position(self, exit_price: float, reason: str) -> float:
        if self.position is None:
            return 0.0
        pos = self.position
        realized_pnl = (exit_price - pos.entry_price) * pos.qty_remaining * 100
        self._log_trade(pos, exit_price, reason, realized_pnl)
        self.position     = None
        self.exit_pending = False
        logger.info(
            "Position closed: symbol=%s reason=%s exit=%.2f pnl=$%.2f",
            pos.symbol, reason, exit_price, realized_pnl,
        )
        return realized_pnl

    # ── Logging ───────────────────────────────────────────────────────────────

    def _ensure_log(self):
        os.makedirs(config.LOG_DIR, exist_ok=True)
        if not os.path.exists(self._log_path):
            with open(self._log_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "date", "symbol", "side", "strike",
                    "entry_price", "exit_price", "qty", "reason",
                    "realized_pnl", "entry_time", "exit_time",
                ])

    def _log_trade(
        self,
        pos:          Position,
        exit_price:   float,
        reason:       str,
        realized_pnl: float,
        partial_qty:  Optional[int] = None,
    ):
        qty = partial_qty if partial_qty is not None else pos.qty_remaining
        with open(self._log_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.date.today().isoformat(),
                pos.symbol,
                pos.side,
                pos.strike,
                pos.entry_price,
                exit_price,
                qty,
                reason,
                f"{realized_pnl:.2f}",
                pos.entry_time.isoformat(),
                datetime.datetime.now(tz=config.ET).isoformat(),
            ])
