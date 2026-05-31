"""
SPY momentum engine — consumes 1-min bars from the stock stream.

Tracks:
  - Rolling 5-bar and 20-bar EMA
  - VWAP (session-reset each day)
  - 5-bar rate-of-change
  - Consecutive green / red bar count

Exposes:
  MomentumState.direction  →  "bull" | "bear" | "neutral"
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional
import datetime
import logging

import config

logger = logging.getLogger(__name__)


@dataclass
class Bar:
    t:     datetime.datetime
    open:  float
    high:  float
    low:   float
    close: float
    volume: float


@dataclass
class MomentumState:
    direction:      str   = "neutral"   # "bull" | "bear" | "neutral"
    ema5:           float = 0.0
    ema20:          float = 0.0
    vwap:           float = 0.0
    roc5:           float = 0.0
    consec_green:   int   = 0
    consec_red:     int   = 0
    last_close:     float = 0.0
    atr5:           float = 0.0         # rolling 5-bar ATR from live bars


class MomentumEngine:
    def __init__(self):
        self._bars:      Deque[Bar]  = deque(maxlen=50)
        self._ema5:      Optional[float] = None
        self._ema20:     Optional[float] = None

        # VWAP accumulators (reset each session)
        self._cum_tp_vol: float = 0.0
        self._cum_vol:    float = 0.0
        self._session_date: Optional[datetime.date] = None

        self.state = MomentumState()

    def _reset_session(self, date: datetime.date):
        # VWAP is session-specific — always reset
        self._cum_tp_vol   = 0.0
        self._cum_vol      = 0.0
        self._session_date = date
        # Consecutive bar counts are session-specific — reset
        # EMAs are intentionally NOT reset — they are continuous across sessions
        # and must carry forward so they're warm at market open
        self._bars.clear()
        self.state = MomentumState(
            ema5  = self.state.ema5,
            ema20 = self.state.ema20,
        )

    def _ema(self, prev: Optional[float], price: float, period: int) -> float:
        k = 2.0 / (period + 1)
        if prev is None:
            return price
        return price * k + prev * (1 - k)

    def preseed(self, bars: list) -> None:
        """
        Feed historical bars through the engine to warm up EMAs before market open.
        Call once at startup with the last 30 1-min bars from yesterday's session.
        Direction signal is ignored during preseed — only EMA state matters.
        """
        logger.info("Pre-seeding momentum engine with %d historical bars...", len(bars))
        for bar in bars:
            self.on_bar(bar)
        logger.info(
            "Pre-seed complete: EMA5=%.2f EMA20=%.2f",
            self.state.ema5, self.state.ema20,
        )

    def on_bar(self, bar: Bar) -> MomentumState:
        bar_date = bar.t.astimezone(config.ET).date()
        if bar_date != self._session_date:
            self._reset_session(bar_date)

        # VWAP
        tp = (bar.high + bar.low + bar.close) / 3.0
        self._cum_tp_vol += tp * bar.volume
        self._cum_vol    += bar.volume
        vwap = self._cum_tp_vol / self._cum_vol if self._cum_vol > 0 else bar.close

        # EMAs
        self._ema5  = self._ema(self._ema5,  bar.close, 5)
        self._ema20 = self._ema(self._ema20, bar.close, 20)

        self._bars.append(bar)

        # 5-bar ROC
        roc5 = 0.0
        if len(self._bars) >= 6:
            prev5 = self._bars[-6].close
            roc5  = (bar.close - prev5) / prev5 if prev5 != 0 else 0.0

        # Consecutive bars
        if bar.close > bar.open:
            consec_green = self.state.consec_green + 1
            consec_red   = 0
        elif bar.close < bar.open:
            consec_green = 0
            consec_red   = self.state.consec_red + 1
        else:
            consec_green = self.state.consec_green
            consec_red   = self.state.consec_red

        # Direction
        bull = (
            bar.close > vwap
            and self._ema5 > self._ema20
            and roc5 >= config.ROC_THRESHOLD
            and consec_green >= config.MIN_CONSEC_BARS
        )
        bear = (
            bar.close < vwap
            and self._ema5 < self._ema20
            and roc5 <= -config.ROC_THRESHOLD
            and consec_red >= config.MIN_CONSEC_BARS
        )

        direction = "bull" if bull else ("bear" if bear else "neutral")

        # Rolling 5-bar ATR using last 5 true ranges
        true_range = bar.high - bar.low  # simplified TR for 1-min bars (no overnight gap)
        recent_bars = list(self._bars)[-5:]
        atr5 = sum(b.high - b.low for b in recent_bars) / len(recent_bars) if recent_bars else true_range

        self.state = MomentumState(
            direction    = direction,
            ema5         = self._ema5,
            ema20        = self._ema20,
            vwap         = vwap,
            roc5         = roc5,
            consec_green = consec_green,
            consec_red   = consec_red,
            last_close   = bar.close,
            atr5         = atr5,
        )
        return self.state
