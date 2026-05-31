"""
Strike selection and OCC symbol construction.

Two modes:
  - startup_atr()            : fetches 5-day daily ATR once at startup (fallback baseline)
  - compute_dynamic_strikes(): called on every 1-min bar with live SPY price + live ATR
                               returns call/put OCC symbols without hitting the chain API
"""

import datetime
import math
from typing import Optional

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import OptionChainRequest
from alpaca.data.enums import DataFeed

import config


def _atr_5day(stock_client: StockHistoricalDataClient) -> float:
    end   = datetime.datetime.now(tz=config.ET)
    start = end - datetime.timedelta(days=10)  # fetch 10 days, keep 5 complete
    req   = StockBarsRequest(
        symbol_or_symbols=config.UNDERLYING,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    bars = stock_client.get_stock_bars(req)[config.UNDERLYING]
    bars = [b for b in bars if b.close is not None][-6:-1]  # last 5 complete days
    if not bars:
        return 3.0  # fallback ATR if data unavailable
    ranges = [b.high - b.low for b in bars]
    return sum(ranges) / len(ranges)


def _current_spy_price(stock_client: StockHistoricalDataClient) -> float:
    req    = StockLatestQuoteRequest(symbol_or_symbols=config.UNDERLYING, feed=DataFeed.IEX)
    quotes = stock_client.get_stock_latest_quote(req)
    q      = quotes[config.UNDERLYING]
    mid    = (q.ask_price + q.bid_price) / 2.0
    return mid if mid > 0 else q.ask_price


def _round_to_step(price: float, step: float) -> float:
    return round(round(price / step) * step, 2)


def build_occ_symbol(underlying: str, expiry: datetime.date, side: str, strike: float) -> str:
    """
    Alpaca OCC format (no space padding — WebSocket rejects spaces):
      {root}{YYMMDD}{C|P}{strike * 1000:08d}
    Example: SPY260513C00560000
    """
    yymmdd     = expiry.strftime("%y%m%d")
    cp         = "C" if side.upper() == "CALL" else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying}{yymmdd}{cp}{strike_int:08d}"


def select_strikes(
    stock_client: StockHistoricalDataClient,
    option_client: OptionHistoricalDataClient,
    expiry: Optional[datetime.date] = None,
) -> dict:
    """
    Returns a dict with selected strikes and OCC symbols for today.

    {
      "spy_price":   560.10,
      "atr":         4.32,
      "expiry":      date(2026, 5, 13),
      "call_strike": 563.50,
      "put_strike":  556.50,
      "call_symbols": ["SPY   260513C00563500", ...],  # target + alts
      "put_symbols":  ["SPY   260513P00556500", ...],
    }
    """
    if expiry is None:
        expiry = datetime.date.today()

    spy_price = _current_spy_price(stock_client)
    atr       = _atr_5day(stock_client)

    offset = config.ATR_MULT * atr

    call_target = _round_to_step(spy_price + offset, config.STRIKE_STEP)
    put_target  = _round_to_step(spy_price - offset, config.STRIKE_STEP)

    # Build call alternates: target and N strikes above/below
    call_strikes = [
        call_target + i * config.STRIKE_STEP
        for i in range(-config.STRIKE_ALTS, config.STRIKE_ALTS + 1)
    ]
    put_strikes = [
        put_target + i * config.STRIKE_STEP
        for i in range(-config.STRIKE_ALTS, config.STRIKE_ALTS + 1)
    ]

    # Validate strikes exist in the chain (Alpaca won't stream a non-existent contract)
    valid_call_symbols = _validate_strikes(option_client, expiry, "CALL", call_strikes)
    valid_put_symbols  = _validate_strikes(option_client, expiry, "PUT",  put_strikes)

    return {
        "spy_price":    spy_price,
        "atr":          atr,
        "expiry":       expiry,
        "call_strike":  call_target,
        "put_strike":   put_target,
        "call_symbols": valid_call_symbols,
        "put_symbols":  valid_put_symbols,
    }


def _validate_strikes(
    option_client: OptionHistoricalDataClient,
    expiry: datetime.date,
    side: str,
    strikes: list[float],
) -> list[str]:
    """
    Cross-check strikes against the live option chain.
    Returns OCC symbols that actually exist.
    """
    req = OptionChainRequest(
        underlying_symbol=config.UNDERLYING,
        expiration_date=expiry,
        type=side.lower(),
        strike_price_gte=min(strikes) - 1,
        strike_price_lte=max(strikes) + 1,
    )
    try:
        chain = option_client.get_option_chain(req)
        existing = set(chain.keys())
    except Exception:
        existing = set()

    symbols = []
    for strike in strikes:
        sym = build_occ_symbol(config.UNDERLYING, expiry, side, strike)
        if not existing or sym in existing:
            symbols.append(sym)

    return symbols


def primary_call_symbol(selection: dict) -> str:
    """Return the primary call OCC symbol (closest to target strike)."""
    syms = selection["call_symbols"]
    mid = len(syms) // 2
    return syms[mid] if syms else ""


def primary_put_symbol(selection: dict) -> str:
    """Return the primary put OCC symbol (closest to target strike)."""
    syms = selection["put_symbols"]
    mid = len(syms) // 2
    return syms[mid] if syms else ""


def startup_atr(stock_client: StockHistoricalDataClient) -> float:
    """
    Fetch 5-day daily ATR once at startup as a baseline.
    Used to seed the dynamic strike logic before enough 1-min bars accumulate.
    """
    return _atr_5day(stock_client)


def compute_dynamic_strikes(
    spy_price: float,
    atr:       float,
    expiry:    Optional[datetime.date] = None,
) -> dict:
    """
    Called on every 1-min bar. Computes the current ideal call/put strikes
    from live SPY price and live ATR. Builds OCC symbols directly — no chain
    API call needed since SPY strikes are always in $0.50 increments.

    Returns:
      {
        "call_strike":  563.50,
        "put_strike":   556.50,
        "call_symbols": ["SPY   260513C00563500", ...],   # target + 1 alt each side
        "put_symbols":  ["SPY   260513P00556500", ...],
      }
    """
    if expiry is None:
        expiry = datetime.date.today()

    # atr is always the daily ATR baseline passed from main.py — no scaling needed.
    # The 1-min live ATR from the momentum engine is intentionally NOT used here
    # because premarket and early-session 1-min ranges are too small to be meaningful
    # for daily strike offset calculation.
    call_target = _round_to_step(spy_price + config.ATR_MULT * atr, config.STRIKE_STEP)
    put_target  = _round_to_step(spy_price - config.ATR_MULT * atr, config.STRIKE_STEP)

    # Build target ± STRIKE_ALTS strikes — wide enough to survive SPY moving several
    # dollars intraday without needing to re-subscribe mid-session.
    call_strikes = [
        call_target + i * config.STRIKE_STEP
        for i in range(-config.STRIKE_ALTS, config.STRIKE_ALTS + 1)
    ]
    put_strikes = [
        put_target + i * config.STRIKE_STEP
        for i in range(-config.STRIKE_ALTS, config.STRIKE_ALTS + 1)
    ]

    call_symbols = [build_occ_symbol(config.UNDERLYING, expiry, "CALL", s) for s in call_strikes]
    put_symbols  = [build_occ_symbol(config.UNDERLYING, expiry, "PUT",  s) for s in put_strikes]

    return {
        "call_strike":  call_target,
        "put_strike":   put_target,
        "call_symbols": call_symbols,
        "put_symbols":  put_symbols,
    }
