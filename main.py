"""
Entry point — orchestrates the full bot lifecycle.

Flow:
  1. Startup: fetch ATR baseline, pre-seed EMAs, recover open positions
  2. On each 1-min SPY bar:
       a. Update momentum engine (EMA5/20, VWAP, ROC5, atr5, direction)
       b. Tick risk cooldown
       c. Recompute dynamic strikes, update routing table
       d. Log bar summary
  3. On each option quote tick (quote-driven exits):
       - Update quote cache and proxy delta tracker
       - IF holding this symbol: asyncio.create_task(_evaluate_exit())
         → sub-50ms response, fires on every tick
       - ELSE: evaluate entry signal if no open position
  4. _exit_monitor: 30-second safety net in case quotes stop arriving
  5. _time_stop_watcher: force-close all positions at 15:25 ET

Exit priority (evaluated in _evaluate_exit on every quote):
  1. TP       — mid >= entry × TP_MULT (1.50×)
  2. Stop     — mid <= entry × STOP_MULT (0.50×)
  3. Trail    — peak_mid >= entry × 1.20 AND mid <= peak_mid × 0.88
  4. TimeStop — clock >= 15:25 ET (separate watcher)
"""

import asyncio
import csv
import datetime
import logging
import os
import re
import select
import signal
import sys
import threading

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from alpaca.trading.enums import AssetClass

import config
from feeds import FeedManager
from momentum import MomentumEngine, Bar
from orders import OrderManager
from risk import RiskManager
from signals import check_entry
from state import BotState, Position
from strikes import startup_atr, compute_dynamic_strikes

os.makedirs(config.LOG_DIR, exist_ok=True)
_today_str = datetime.date.today().strftime("%Y-%m-%d")
logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{config.LOG_DIR}/bot_{_today_str}.log"),
    ],
)
logger = logging.getLogger("main")


# ── Shared singletons ─────────────────────────────────────────────────────────

momentum_engine = MomentumEngine()
bot_state       = BotState()
risk_manager    = RiskManager()
order_manager   = OrderManager()

_entry_lock: asyncio.Lock = asyncio.Lock()

# Routing table: {occ_symbol: (side, strike)} — updated every bar
_current_subscriptions: dict[str, tuple] = {}
_feed:               FeedManager    = None   # set in main()
_baseline_atr:       float          = 3.0    # set in main()
_market_open_event:  asyncio.Event  = None   # set in main(); fired on first RTH bar
_open_bar_strikes:   dict           = {}     # strike data from the opening bar

# Re-subscription tracking
_RESUB_THRESHOLD     = 3.0   # SPY points of movement before re-subscribing option window
_last_sub_spy_price: float = 0.0


def _build_routing_table(strikes: dict) -> dict:
    """
    Build {occ_symbol: (side, actual_strike)} routing table.
    Each symbol gets its OWN parsed strike — NOT the shared target strike.
    This ensures zone and directionality checks in check_entry use the real
    strike of each symbol, not a shared target that may be several strikes away.
    """
    meta = {}
    for sym in strikes["call_symbols"]:
        _, actual_strike = _parse_occ_symbol(sym)
        if actual_strike is not None:
            meta[sym] = ("call", actual_strike)
    for sym in strikes["put_symbols"]:
        _, actual_strike = _parse_occ_symbol(sym)
        if actual_strike is not None:
            meta[sym] = ("put", actual_strike)
    return meta


# ── Bar handler ───────────────────────────────────────────────────────────────

async def on_spy_bar(bar):
    global _current_subscriptions, _open_bar_strikes

    b = Bar(
        t      = bar.timestamp,
        open   = float(bar.open),
        high   = float(bar.high),
        low    = float(bar.low),
        close  = float(bar.close),
        volume = float(bar.volume),
    )
    bot_state.spy_price = b.close
    m_state = momentum_engine.on_bar(b)

    # Tick cooldown counter
    risk_manager.tick_bar()

    # Fire market-open event on first RTH bar (≥ 09:30 ET)
    bar_time = b.t.astimezone(config.ET).time()
    if not _market_open_event.is_set() and bar_time >= datetime.time(9, 30):
        _open_bar_strikes = compute_dynamic_strikes(b.close, _baseline_atr)
        _market_open_event.set()
        logger.info(
            "Market open bar: SPY=%.2f — option subscription triggered",
            b.close,
        )

    # Update routing table every bar (no WebSocket changes — subscriptions fixed at open)
    # new_strikes initialised to zero so the BAR log below is always safe pre-market
    new_strikes = {"call_strike": 0.0, "put_strike": 0.0}
    if _market_open_event.is_set():
        new_strikes = compute_dynamic_strikes(b.close, _baseline_atr)
        new_meta    = _build_routing_table(new_strikes)

        if set(new_meta) != set(_current_subscriptions):
            logger.info(
                "Strikes updated: call=%.2f put=%.2f (SPY=%.2f ATR=%.3f)",
                new_strikes["call_strike"], new_strikes["put_strike"], b.close, _baseline_atr,
            )
            _current_subscriptions = new_meta

    # Per-bar console summary
    pos = bot_state.position
    logger.info(
        "BAR | SPY=%.2f | %s | EMA5=%.2f EMA20=%.2f | VWAP=%.2f | ROC=%.4f | "
        "consec=%s | atr5=%.3f | call=%.2f put=%.2f | pos=%s | pnl=$%.2f",
        b.close,
        m_state.direction.upper(),
        m_state.ema5, m_state.ema20,
        m_state.vwap,
        m_state.roc5,
        f"+{m_state.consec_green}g" if m_state.consec_green else f"-{m_state.consec_red}r",
        m_state.atr5,
        new_strikes["call_strike"],
        new_strikes["put_strike"],
        f"{pos.symbol} @{pos.entry_price:.2f}" if pos else "NONE",
        risk_manager.daily_pnl,
    )


# ── Trade update handler (order event stream) ─────────────────────────────────

async def on_trade_update(update):
    """Receives real-time order events — used for logging only for now."""
    try:
        logger.debug("Trade update: event=%s order=%s", update.event, update.order.id)
    except Exception:
        pass


# ── Option quote handler ───────────────────────────────────────────────────────

async def on_option_quote(quote):
    sym = quote.symbol
    bid = float(quote.bid_price or 0)
    ask = float(quote.ask_price or 0)
    ts  = quote.timestamp

    # Update proxy delta tracker
    bot_state.update_option_quote(sym, bid, ask, ts)

    # Quote-driven exit — fires on every tick for the held symbol.
    # asyncio.create_task() schedules the coroutine on the event loop and
    # returns immediately, so close_position() is never called from inside
    # the stream callback itself.
    pos = bot_state.position
    if pos and pos.symbol == sym and not bot_state.exit_pending:
        asyncio.create_task(_evaluate_exit(bot_state.get_quote(sym)))
        return

    # Entry evaluation
    if bot_state.position is not None:
        return
    if sym not in _current_subscriptions:
        return
    if not _entry_lock.locked():
        await _evaluate_entry(sym)


# ── Entry evaluation ──────────────────────────────────────────────────────────

async def _evaluate_entry(symbol: str):
    async with _entry_lock:
        if bot_state.position is not None:
            return
        if not risk_manager.can_trade():
            return

        side, strike = _current_subscriptions.get(symbol, (None, None))
        if side is None:
            return

        quote   = bot_state.get_quote(symbol)
        tracker = bot_state.get_tracker(symbol)
        if quote is None:
            return

        should_enter = check_entry(
            side          = side,
            strike        = strike,
            option_quote  = quote,
            momentum      = momentum_engine.state,
            proxy_tracker = tracker,
            spy_price     = bot_state.spy_price,
            trades_today  = risk_manager.trades_today,
            has_open_pos  = False,
            atr5          = momentum_engine.state.atr5,
        )
        if not should_enter:
            return

        entry_mid   = quote.mid
        qty         = risk_manager.size_trade(entry_mid)
        limit_price = round(entry_mid * 1.02, 2)

        logger.info("Placing entry: %s qty=%d limit=%.2f", symbol, qty, limit_price)
        order = await order_manager.buy_limit(symbol, qty, limit_price)

        if order is None:
            logger.warning("Entry failed/timed out for %s", symbol)
            return

        fill_price = order_manager.get_fill_price(order)
        if fill_price <= 0:
            logger.warning("Fill price unavailable for %s", symbol)
            return

        pos = Position(
            symbol      = symbol,
            side        = side,
            strike      = strike,
            qty         = qty,
            entry_price = fill_price,
            entry_time  = datetime.datetime.now(tz=config.ET),
            order_id    = str(order.id),
        )
        bot_state.open_position(pos)
        logger.info(
            "ENTERED: %s at %.2f × %d | TP=%.2f | Stop=%.2f",
            symbol, fill_price, qty,
            round(fill_price * config.TP_MULT,   2),
            round(fill_price * config.STOP_MULT, 2),
        )


# ── Trade list display ────────────────────────────────────────────────────────

def _print_trades():
    """Print today's closed trades from trades_YYYY-MM-DD.csv to the terminal."""
    today    = datetime.date.today()
    log_path = os.path.join(config.LOG_DIR, f"trades_{today.strftime('%Y-%m-%d')}.csv")
    today    = today.isoformat()

    if not os.path.exists(log_path):
        print("  No trades log found.")
        return

    rows = []
    try:
        with open(log_path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("date") == today:
                    rows.append(row)
    except Exception as e:
        print(f"  Error reading trades log: {e}")
        return

    def _fmt_time(iso: str) -> str:
        """ISO timestamp → HH:MM:SS ET."""
        try:
            dt = datetime.datetime.fromisoformat(iso).astimezone(config.ET)
            return dt.strftime("%H:%M:%S")
        except Exception:
            return "  --:--  "

    def _fmt_duration(entry_iso: str, exit_iso: str) -> str:
        """Return elapsed time as Xm Ys."""
        try:
            t0 = datetime.datetime.fromisoformat(entry_iso)
            t1 = datetime.datetime.fromisoformat(exit_iso)
            secs = int((t1 - t0).total_seconds())
            return f"{secs // 60}m {secs % 60:02d}s"
        except Exception:
            return "  --   "

    print("\n" + "─" * 86)
    print(f"  TODAY'S TRADES  ({today})  —  {len(rows)} closed")
    print("─" * 86)
    if not rows:
        print("  No closed trades yet today.")
    else:
        print(f"  {'#':<3}  {'Symbol':<22}  {'Side':<5}  "
              f"{'Entry $':>7}  {'Exit $':>6}  {'Qty':>3}  "
              f"{'In':>8}  {'Out':>8}  {'TiT':>7}  "
              f"{'Reason':<8}  {'P&L':>8}")
        print("  " + "-" * 82)
        total = 0.0
        for i, row in enumerate(rows, 1):
            pnl       = float(row.get("realized_pnl", 0))
            total    += pnl
            icon      = "✅" if pnl >= 0 else "❌"
            entry_t   = _fmt_time(row.get("entry_time", ""))
            exit_t    = _fmt_time(row.get("exit_time",  ""))
            duration  = _fmt_duration(row.get("entry_time", ""), row.get("exit_time", ""))
            print(
                f"  {i:<3}  {row.get('symbol',''):<22}  {row.get('side',''):<5}  "
                f"${float(row.get('entry_price', 0)):>6.2f}  "
                f"${float(row.get('exit_price',  0)):>5.2f}  "
                f"{int(float(row.get('qty', 0))):>3}  "
                f"{entry_t:>8}  {exit_t:>8}  {duration:>7}  "
                f"{row.get('reason',''):<8}  "
                f"{icon} ${pnl:>+7.2f}"
            )
        print("  " + "-" * 82)
        print(f"  {'TOTAL':>65}  ${total:>+7.2f}")
    print("─" * 86 + "\n")


# ── Daily P&L recovery from CSV ──────────────────────────────────────────────

def _restore_daily_pnl():
    """
    Read today's closed trades from trades_YYYY-MM-DD.csv and restore risk manager counters.
    Called after reset_day() so a restart doesn't wipe the session P&L.
    """
    today    = datetime.date.today()
    log_path = os.path.join(config.LOG_DIR, f"trades_{today.strftime('%Y-%m-%d')}.csv")
    today    = today.isoformat()
    if not os.path.exists(log_path):
        return

    daily_pnl    = 0.0
    trades_today = 0
    try:
        with open(log_path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("date") == today:
                    daily_pnl    += float(row.get("realized_pnl", 0))
                    trades_today += 1
    except Exception as e:
        logger.warning("Could not restore daily P&L from CSV: %s", e)
        return

    if trades_today > 0:
        risk_manager.restore_day(daily_pnl, trades_today)
    else:
        logger.info("No trades found in CSV for today — starting fresh.")


# ── OCC symbol parser ─────────────────────────────────────────────────────────

def _parse_occ_symbol(symbol: str):
    """
    Parse an OCC symbol like SPY260520C00740000.
    Returns (side, strike) or (None, None) on failure.
    """
    m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', symbol)
    if not m:
        return None, None
    side   = "call" if m.group(3) == "C" else "put"
    strike = int(m.group(4)) / 1000.0
    return side, strike


# ── Position recovery (called at startup after a restart) ─────────────────────

def _recover_open_position():
    """
    On startup, poll Alpaca REST for any existing option position.
    If found, reconstruct BotState so the exit monitor picks it up immediately.
    Called synchronously before the asyncio loop takes over.
    """
    try:
        positions = order_manager.get_open_positions()
        for p in positions:
            if p.asset_class != AssetClass.US_OPTION:
                continue
            symbol = p.symbol
            side, strike = _parse_occ_symbol(symbol)
            if side is None:
                logger.warning("Could not parse recovered position symbol: %s", symbol)
                continue
            qty          = int(float(p.qty))
            entry_price  = float(p.avg_entry_price)
            recovered    = Position(
                symbol      = symbol,
                side        = side,
                strike      = strike,
                qty         = qty,
                entry_price = entry_price,
                entry_time  = datetime.datetime.now(tz=config.ET),   # approx
                order_id    = "recovered",
            )
            bot_state.open_position(recovered)
            logger.info(
                "RECOVERED position from Alpaca: %s entry=%.2f qty=%d | "
                "TP=%.2f Stop=%.2f",
                symbol, entry_price, qty,
                round(entry_price * config.TP_MULT,   2),
                round(entry_price * config.STOP_MULT, 2),
            )
            return   # only one position at a time
    except Exception as e:
        logger.warning("Position recovery check failed: %s", e)


# ── Dynamic re-subscription watcher ──────────────────────────────────────────

async def _resubscribe_watcher():
    """
    After market open, watches SPY price and re-subscribes the option window
    whenever SPY moves ±_RESUB_THRESHOLD points from the last subscription price.

    Rules:
      - Never resubscribes from inside a stream callback (safe — standalone task).
      - Always keeps the currently held symbol subscribed, regardless of where
        SPY has moved, so the exit monitor's quote feed is never interrupted.
      - Alpaca deduplicates internally — passing already-subscribed symbols is harmless.
    """
    global _last_sub_spy_price, _current_subscriptions

    logger.info("Re-subscription watcher waiting for market open...")
    await _market_open_event.wait()
    _last_sub_spy_price = bot_state.spy_price
    logger.info("Re-subscription watcher active. Anchor SPY=%.2f threshold=±%.1f pts",
                _last_sub_spy_price, _RESUB_THRESHOLD)

    while True:
        await asyncio.sleep(10)

        spy = bot_state.spy_price
        if spy <= 0 or _last_sub_spy_price <= 0:
            continue

        move = abs(spy - _last_sub_spy_price)
        if move < _RESUB_THRESHOLD:
            continue

        logger.info(
            "Re-subscribe triggered: SPY moved %.2f pts (anchor=%.2f → now=%.2f)",
            move, _last_sub_spy_price, spy,
        )

        new_strikes = compute_dynamic_strikes(spy, _baseline_atr)
        new_symbols = new_strikes["call_symbols"] + new_strikes["put_symbols"]

        # Always keep the held symbol — exit monitor depends on its quotes
        pos      = bot_state.position
        held_sym = pos.symbol if pos else None
        if held_sym and held_sym not in new_symbols:
            new_symbols.append(held_sym)
            logger.info("Held symbol pinned in subscription: %s", held_sym)

        # Update routing table first (in-memory, always safe)
        _current_subscriptions = _build_routing_table(new_strikes)
        _last_sub_spy_price    = spy

        # Attempt WebSocket expansion — wrapped in try/except so a stream
        # hiccup never kills bar delivery. If this fails, routing table is
        # already updated so entry logic stays correct for the current window.
        try:
            _feed.add_option_symbols(new_symbols)
            logger.info(
                "Re-subscribed: call=%.2f put=%.2f | %d symbols total",
                new_strikes["call_strike"], new_strikes["put_strike"], len(new_symbols),
            )
        except Exception as e:
            logger.warning("Re-subscribe WebSocket call failed (routing table updated): %s", e)
        logger.info(
            "Re-subscribed: call=%.2f put=%.2f | %d symbols total",
            new_strikes["call_strike"], new_strikes["put_strike"], len(new_symbols),
        )


# ── Market-open option subscriber ────────────────────────────────────────────

async def _option_subscriber():
    """
    Waits for the first 9:30 ET bar, then subscribes to option quotes.
    Runs as a separate task — NOT inside the stream callback chain,
    so calling subscribe_options() here is safe (no deadlock).
    """
    logger.info("Option subscriber waiting for market open (09:30 ET)...")
    await _market_open_event.wait()

    strikes  = _open_bar_strikes
    all_syms = strikes["call_symbols"] + strikes["put_symbols"]

    # If we recovered a position on restart, pin its symbol even if outside window
    pos      = bot_state.position
    held_sym = pos.symbol if pos else None
    if held_sym and held_sym not in all_syms:
        all_syms.append(held_sym)
        logger.info("Recovered position symbol pinned at open subscription: %s", held_sym)

    # Populate routing table — each symbol keyed to its own actual strike
    _current_subscriptions.update(_build_routing_table(strikes))

    _feed.subscribe_options(all_syms)
    logger.info(
        "Subscribed at open: call=%.2f put=%.2f | %d symbols",
        strikes["call_strike"], strikes["put_strike"], len(all_syms),
    )
    # One-shot task — sleep until cancelled so _guarded doesn't restart it
    await asyncio.sleep(float("inf"))


# ── Exit evaluation (quote-driven) ────────────────────────────────────────────

async def _evaluate_exit(quote):
    """
    Evaluates TP / stop / peak trail on every option quote tick for the
    held symbol. Called via asyncio.create_task() from on_option_quote,
    so close_position() never executes inside the stream callback.

    Race-condition safety: exit_pending is set to True before the first
    await. Because asyncio is cooperative, no other task can run between
    the exit_pending check and the exit_pending = True assignment — there
    is no await between those two lines.
    """
    pos = bot_state.position
    if pos is None or bot_state.exit_pending:
        return
    if quote is None:
        return

    mid        = quote.mid
    tp_price   = pos.entry_price * config.TP_MULT
    stop_price = pos.entry_price * config.STOP_MULT

    # Update peak mid — monotonically increasing, safe under concurrency
    if mid > pos.peak_mid:
        pos.peak_mid = mid

    if mid >= tp_price:
        reason = "tp"
    elif mid <= stop_price:
        reason = "stop"
    elif pos.peak_mid >= pos.entry_price * config.PEAK_TRAIL_ACTIVATE:
        trail_stop = pos.peak_mid * config.PEAK_TRAIL_PCT
        if mid <= trail_stop:
            logger.info(
                "PEAK TRAIL: mid=%.2f peak=%.2f trail_stop=%.2f entry=%.2f",
                mid, pos.peak_mid, trail_stop, pos.entry_price,
            )
            reason = "peak_trail"
        else:
            return
    else:
        return

    # Set exit_pending BEFORE first await — prevents duplicate exit tasks
    bot_state.exit_pending = True
    logger.info("EXIT signal: reason=%s mid=%.2f tp=%.2f stop=%.2f",
                reason, mid, tp_price, stop_price)

    order = await order_manager.close_position(pos.symbol, pos.qty_remaining)
    fill  = order_manager.get_fill_price(order) if order else mid

    pnl = bot_state.close_position(fill, reason)
    bot_state.exit_pending = False
    risk_manager.record_trade(pnl)

    icon = "✅" if pnl >= 0 else "❌"
    logger.info("%s %s: %s fill=%.2f pnl=$%.2f | daily=$%.2f trades=%d",
                icon, reason.upper(), pos.symbol, fill,
                pnl, risk_manager.daily_pnl, risk_manager.trades_today)


# ── Exit monitor (30-second safety net) ───────────────────────────────────────

async def _exit_monitor():
    """
    Fallback safety net — fires every 30 seconds in case option quotes
    stop arriving (WebSocket hiccup, reconnect gap). Normal exits are
    handled quote-driven via _evaluate_exit() called from on_option_quote.
    """
    while True:
        await asyncio.sleep(30)

        pos = bot_state.position
        if pos is None or bot_state.exit_pending:
            continue

        quote = bot_state.get_quote(pos.symbol)
        if quote is None:
            continue

        asyncio.create_task(_evaluate_exit(quote))


# ── Task wrapper ───────────────────────────────────────────────────────────────

async def _guarded(coro_factory, name: str, restart_delay: float = 5.0):
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            logger.info("%s cancelled — stopping.", name)
            break
        except Exception as e:
            logger.error("%s crashed: %s — restarting in %.0fs", name, e, restart_delay)
            await asyncio.sleep(restart_delay)


# ── Periodic status display ───────────────────────────────────────────────────

async def _status_loop():
    """
    Print status block every 5 seconds when a position is open (you want
    to watch P&L tick), or every 60 seconds when flat (bar-level is enough).
    """
    while True:
        await asyncio.sleep(5)
        if bot_state.position is not None:
            _print_status()
        else:
            # Only print once per minute when no position
            now = datetime.datetime.now(tz=config.ET)
            if now.second < 5:   # fires in the first 5s of each minute
                _print_status()


def _print_status():
    now_et = datetime.datetime.now(tz=config.ET).strftime("%H:%M:%S")
    m      = momentum_engine.state
    pos    = bot_state.position
    spy    = bot_state.spy_price

    # Direction indicator
    dir_str = {"bull": "▲ BULL", "bear": "▼ BEAR", "neutral": "── NEUT"}.get(m.direction, m.direction)

    lines = [
        "─" * 60,
        f"  {now_et} ET  |  SPY ${spy:.2f}  |  {dir_str}  |  VWAP ${m.vwap:.2f}",
        f"  EMA5 ${m.ema5:.2f}  EMA20 ${m.ema20:.2f}  |  ROC {m.roc5:+.4f}  |  "
        f"consec {'+' if m.consec_green else '-'}{m.consec_green or m.consec_red}",
    ]

    if pos:
        quote       = bot_state.get_quote(pos.symbol)
        current_mid = quote.mid if quote else pos.entry_price
        unreal_pnl  = (current_mid - pos.entry_price) * pos.qty_remaining * 100
        pct_chg     = (current_mid / pos.entry_price - 1) * 100 if pos.entry_price else 0
        tp_price    = round(pos.entry_price * config.TP_MULT,   2)
        stop_price  = round(pos.entry_price * config.STOP_MULT, 2)
        pnl_sign    = "+" if unreal_pnl >= 0 else ""
        elapsed     = datetime.datetime.now(tz=config.ET) - pos.entry_time
        total_secs  = int(elapsed.total_seconds())
        time_in_trade = f"{total_secs // 60}m {total_secs % 60:02d}s"
        # Peak trailing stop display
        trail_armed = pos.peak_mid >= pos.entry_price * config.PEAK_TRAIL_ACTIVATE
        trail_stop  = round(pos.peak_mid * config.PEAK_TRAIL_PCT, 2) if trail_armed else None
        trail_str   = (f"  Trail ${trail_stop:.2f} (peak ${pos.peak_mid:.2f})" if trail_armed
                       else f"  Trail ARMED @ ${pos.entry_price * config.PEAK_TRAIL_ACTIVATE:.2f}")
        lines += [
            "  " + "·" * 56,
            f"  POSITION: {pos.symbol}  ({pos.side.upper()} ${pos.strike:.0f})  |  in trade {time_in_trade}",
            f"  Entry ${pos.entry_price:.2f}  ×  {pos.qty_remaining} contracts",
            f"  Mid   ${current_mid:.2f}  ({pct_chg:+.0f}%)  |  "
            f"TP ${tp_price:.2f}  Stop ${stop_price:.2f}",
            f"  Unrealised P&L: {pnl_sign}${unreal_pnl:.2f}  |{trail_str}",
        ]
    else:
        cooldown = getattr(risk_manager, "_cooldown_bars", 0)
        status   = f"cooldown {cooldown} bars" if cooldown else "ready to trade"
        lines.append(f"  NO POSITION  |  {status}")

    lines += [
        "  " + "·" * 56,
        f"  Daily P&L ${risk_manager.daily_pnl:+.2f}  |  "
        f"Trades {risk_manager.trades_today}  |  "
        f"Gate {'🔒 LOCKED' if risk_manager.locked else '🟢 open'}",
        "─" * 60,
    ]

    print("\n".join(lines), flush=True)


# ── Time stop ─────────────────────────────────────────────────────────────────

async def _time_stop_watcher(feed: FeedManager):
    while True:
        await asyncio.sleep(10)
        now_et = datetime.datetime.now(tz=config.ET).strftime("%H:%M")
        if now_et >= config.TIME_STOP:
            logger.info("TIME STOP reached (%s). Closing all positions.", config.TIME_STOP)
            await _force_close_all()
            await feed.stop()
            return


async def _force_close_all():
    pos = bot_state.position
    if pos is None:
        return
    order = await order_manager.close_position(pos.symbol, pos.qty_remaining)
    fill  = order_manager.get_fill_price(order) if order else pos.entry_price * 0.5
    pnl   = bot_state.close_position(fill, "time_stop")
    risk_manager.record_trade(pnl)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global _feed, _baseline_atr, _market_open_event

    _market_open_event = asyncio.Event()
    risk_manager.reset_day()
    _restore_daily_pnl()   # replay today's closed trades after a restart

    stock_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)

    # Daily ATR baseline
    logger.info("Fetching daily ATR baseline...")
    _baseline_atr = startup_atr(stock_client)
    logger.info("Baseline ATR: %.2f", _baseline_atr)

    # Pre-seed EMAs with last 30 RTH 1-min bars
    logger.info("Pre-seeding momentum engine...")
    try:
        seed_end   = datetime.datetime.now(tz=config.ET)
        seed_start = seed_end - datetime.timedelta(days=5)  # 5 days covers Mon→Fri lookback
        seed_req   = StockBarsRequest(
            symbol_or_symbols = config.UNDERLYING,
            timeframe         = TimeFrame.Minute,
            start             = seed_start,
            end               = seed_end,
            feed              = DataFeed.IEX,
        )
        raw = stock_client.get_stock_bars(seed_req)[config.UNDERLYING]
        rth_open  = datetime.time(9, 30)
        rth_close = datetime.time(16, 0)
        raw = [b for b in raw if rth_open <= b.timestamp.astimezone(config.ET).time() < rth_close]
        raw = raw[-30:]
        seed_bars = [
            Bar(t=b.timestamp, open=float(b.open), high=float(b.high),
                low=float(b.low), close=float(b.close), volume=float(b.volume))
            for b in raw
        ]
        momentum_engine.preseed(seed_bars)
        if seed_bars:
            bot_state.spy_price = seed_bars[-1].close
    except Exception as e:
        logger.warning("Pre-seed failed (%s) — EMAs will warm from live bars.", e)

    # Check for any position left open from a previous run (e.g. after 'r' restart)
    _recover_open_position()

    logger.info("Startup complete — waiting for 09:30 ET market open to subscribe options.")

    _feed = FeedManager(
        on_spy_bar      = on_spy_bar,
        on_option_quote = on_option_quote,
        on_trade_update = on_trade_update,
    )

    loop = asyncio.get_event_loop()
    _shutdown_event = asyncio.Event()

    async def _shutdown(reason: str = "signal"):
        """Full shutdown — cancels orders and closes positions before exit."""
        if _shutdown_event.is_set():
            return
        logger.info("Shutdown (%s). Cancelling open orders...", reason)
        order_manager.cancel_all_options()
        await _force_close_all()
        _shutdown_event.set()
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task():
                task.cancel()

    async def _soft_shutdown(reason: str = "restart"):
        """
        Soft shutdown for restart — cancels unfilled orders but leaves open
        positions on Alpaca. They will be recovered automatically on next startup.
        """
        if _shutdown_event.is_set():
            return
        logger.info("Soft shutdown (%s). Leaving positions open for recovery.", reason)
        order_manager.cancel_all_options()   # cancel any pending limit orders
        _shutdown_event.set()
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task():
                task.cancel()

    def _signal_shutdown():
        async def _do():
            await _shutdown("Ctrl+C / SIGTERM")
            os._exit(0)
        asyncio.create_task(_do())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_shutdown)

    def _keyboard_watcher():
        print("  >> Bot running.  q = quit  |  r = restart (keeps positions)  |  t = trades")
        while not _shutdown_event.is_set():
            try:
                # Poll stdin with 1s timeout — never blocks indefinitely
                ready, _, _ = select.select([sys.stdin], [], [], 1.0)
                if ready:
                    line = sys.stdin.readline().strip().lower()
                    if line == "q":
                        logger.info("Keyboard quit requested.")
                        future = asyncio.run_coroutine_threadsafe(_shutdown("keyboard"), loop)
                        future.result(timeout=8)
                        os._exit(0)
                    elif line == "r":
                        logger.info("Keyboard restart requested — positions left open for recovery.")
                        future = asyncio.run_coroutine_threadsafe(_soft_shutdown("restart"), loop)
                        future.result(timeout=8)
                        os.execl(sys.executable, sys.executable, *sys.argv)
                    elif line == "t":
                        _print_trades()
            except Exception:
                break

    threading.Thread(target=_keyboard_watcher, daemon=True).start()

    await asyncio.gather(
        _guarded(_feed.start,                          "feed"),
        _guarded(_option_subscriber,                   "option_subscriber"),
        _guarded(_resubscribe_watcher,                 "resubscribe_watcher"),
        _guarded(_exit_monitor,                        "exit_monitor"),
        _guarded(_status_loop,                         "status_loop"),
        _guarded(lambda: _time_stop_watcher(_feed),    "time_stop_watcher"),
        return_exceptions=True,
    )

    logger.info("=" * 60)
    logger.info("SESSION COMPLETE | Trades: %d | P&L: $%.2f",
                risk_manager.trades_today, risk_manager.daily_pnl)
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
