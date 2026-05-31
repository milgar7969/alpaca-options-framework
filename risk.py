"""
Risk manager — position sizing, daily loss gate, and trade cooldown.

Rules:
  - Size each trade so that a full stop-loss hit = MAX_RISK_PER_TRADE
  - Hard-stop the bot for new entries if cumulative daily loss >= MAX_DAILY_LOSS
  - Enforce a cooldown of TRADE_COOLDOWN_BARS between trades (prevents chasing)
"""

import logging
import math

import config

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self._daily_pnl:     float = 0.0
        self._trades_today:  int   = 0
        self._locked:        bool  = False   # True = daily loss limit hit
        self._cooldown_bars: int   = 0       # bars remaining before next entry allowed

    # ── Queries ───────────────────────────────────────────────────────────────

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def locked(self) -> bool:
        return self._locked

    def can_trade(self) -> bool:
        if self._locked:
            logger.debug("Risk gate LOCKED — daily loss limit reached.")
            return False
        if self._cooldown_bars > 0:
            logger.debug("In cooldown — %d bars remaining.", self._cooldown_bars)
            return False
        if self._trades_today >= config.MAX_TRADES_PER_DAY:
            logger.debug("Max trades per day reached (%d).", self._cooldown_bars)
            return False
        return True

    # ── Cooldown ──────────────────────────────────────────────────────────────

    def tick_bar(self):
        """Call on every 1-min bar to decrement cooldown counter."""
        if self._cooldown_bars > 0:
            self._cooldown_bars -= 1

    def start_cooldown(self):
        self._cooldown_bars = config.TRADE_COOLDOWN_BARS
        logger.info("Cooldown started — %d bars before next entry.", config.TRADE_COOLDOWN_BARS)

    # ── Sizing ────────────────────────────────────────────────────────────────

    def size_trade(self, entry_price: float) -> int:
        """
        Return number of contracts so that a full stop-loss hit = MAX_RISK_PER_TRADE.

        risk_per_contract = entry_price * (1 - STOP_MULT) * 100
        """
        if entry_price <= 0:
            return 0
        risk_per_contract = entry_price * (1.0 - config.STOP_MULT) * 100
        contracts = math.floor(config.MAX_RISK_PER_TRADE / risk_per_contract)
        contracts = max(1, contracts)
        logger.info(
            "Sizing: entry=%.2f risk/contract=$%.2f → %d contract(s)",
            entry_price, risk_per_contract, contracts,
        )
        return contracts

    # ── P&L tracking ─────────────────────────────────────────────────────────

    def record_trade(self, realized_pnl: float):
        """Call after each trade closes with the net P&L (positive or negative)."""
        self._daily_pnl    += realized_pnl
        self._trades_today += 1
        self.start_cooldown()
        logger.info(
            "Trade recorded. P&L: $%.2f | Daily P&L: $%.2f | Trades today: %d",
            realized_pnl, self._daily_pnl, self._trades_today,
        )
        if self._daily_pnl <= -config.MAX_DAILY_LOSS:
            self._locked = True
            logger.warning(
                "DAILY LOSS LIMIT HIT ($%.2f). No new entries for rest of session.",
                self._daily_pnl,
            )

    def reset_day(self):
        """Call at the start of each new session."""
        self._daily_pnl     = 0.0
        self._trades_today  = 0
        self._locked        = False
        self._cooldown_bars = 0
        logger.info("Risk manager reset for new session.")

    def restore_day(self, daily_pnl: float, trades_today: int):
        """
        Restore daily counters from a previous run (e.g. after restart).
        Re-evaluates the daily loss lock so risk gates remain correct.
        """
        self._daily_pnl    = daily_pnl
        self._trades_today = trades_today
        if self._daily_pnl <= -config.MAX_DAILY_LOSS:
            self._locked = True
            logger.warning("Daily loss limit already hit ($%.2f) — gate LOCKED.", self._daily_pnl)
        logger.info(
            "Risk counters restored: daily_pnl=$%.2f trades=%d locked=%s",
            self._daily_pnl, self._trades_today, self._locked,
        )
