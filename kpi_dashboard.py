#!/usr/bin/env python3
"""
SPY 0DTE bot — KPI dashboard generator.

Reads logs/trades_*.csv (closed trades) and logs/bot_*.log (ghost closes,
ORB shadow filter decisions) and emits a self-contained dark-theme HTML
report to gh_dashboard/index.html, mirroring the Strat v60 dashboard.

Usage:
    python3 kpi_dashboard.py [--days 30] [--out gh_dashboard/index.html]

Only generated HTML ever goes into gh_dashboard/ — never config or keys.
"""

import argparse
import csv
import datetime
import glob
import html
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR  = os.path.join(BASE_DIR, "logs")

# Selectable look-back windows (calendar days). Each is rendered to its own
# self-contained HTML page; a nav selector at the top links between them, so
# switching windows is just page navigation on the static GitHub Pages site.
# 30 is the canonical index.html (keeps existing links/bookmarks working).
WINDOWS = [30, 60, 90, 180, 360]


def _winfile(days: int) -> str:
    """Filename for a window: 30 → index.html, others → index-<days>.html."""
    return "index.html" if days == 30 else f"index-{days}.html"

GHOST_RE  = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) [\d:,]+ \[WARNING\] main: GHOST CLOSED: (\S+) "
    r"fill=([\d.]+) pnl=\$(-?[\d.]+)"
)
SHADOW_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) ([\d:]{8})[\d,]* \[INFO\] orb_filter: "
    r"ORB_SHADOW \| (BLOCK|ALLOW): (\S+) side=(\w+) bias=(\S+)"
)
EXITSH_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) [\d:,]+ \[INFO\] exit_shadow: EXIT_SHADOW \| "
    r"variant=(\w+) symbol=(\S+) entry=([\d.]+) exit=([\d.]+) qty=(\d+) "
    r"reason=(\w+) pnl=(-?[\d.]+)"
)
FIX2_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) ([\d:]{8})[\d,]* \[WARNING\] main: "
    r"FIX2_SHADOW \| would_latch symbol=(\S+) qty=(\d+) limit=([\d.]+)"
)


def is_artifact(r) -> bool:
    """
    Bug-artifact rows: 'time_stop' exits held under 60 seconds are force-closes
    of positions recovered after a restart (synthetic entry time, Alpaca
    day-average cost basis) — e.g. the Jun 3/4 watchdog-cascade rows and the
    May 27 recovered 12-lot. A genuine time-stop close holds for minutes.
    """
    hold = (r["exit_dt"] - r["entry_dt"]).total_seconds()
    return r["reason"] == "time_stop" and hold < 60


def load_trades(days: int):
    """Load closed trades from trades_*.csv, newest `days` trading days.
    Returns (clean_trades, artifact_trades)."""
    rows = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "trades_*.csv"))):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if not r.get("realized_pnl"):
                    continue
                try:
                    r["realized_pnl"] = float(r["realized_pnl"])
                    r["entry_dt"] = datetime.datetime.fromisoformat(r["entry_time"])
                    r["exit_dt"]  = datetime.datetime.fromisoformat(r["exit_time"])
                except (ValueError, KeyError):
                    continue
                rows.append(r)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    rows   = [r for r in rows if r["date"] >= cutoff]
    return ([r for r in rows if not is_artifact(r)],
            [r for r in rows if is_artifact(r)])


def load_ghosts(dates: set):
    """Parse GHOST CLOSED events from bot logs → [{date, symbol, pnl}]."""
    ghosts = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "bot_*.log"))):
        day = os.path.basename(path)[4:14]
        if day not in dates:
            continue
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    m = GHOST_RE.match(line)
                    if m:
                        ghosts.append({"date": m.group(1), "symbol": m.group(2),
                                       "pnl": float(m.group(4))})
        except OSError:
            continue
    return ghosts


def load_shadow(dates: set):
    """Parse ORB_SHADOW decisions → [{date, time, decision, symbol, side, bias}]."""
    out = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "bot_*.log"))):
        day = os.path.basename(path)[4:14]
        if day not in dates:
            continue
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    m = SHADOW_RE.match(line)
                    if m:
                        out.append({
                            "date": m.group(1), "time": m.group(2),
                            "decision": m.group(3), "symbol": m.group(4),
                            "side": m.group(5), "bias": m.group(6),
                        })
        except OSError:
            continue
    return out


def load_exit_shadow(dates: set):
    """Parse EXIT_SHADOW twin exits → [{date, variant, reason, pnl}].
    'no_quote' twins are unresolved (symbol left the subscription window)
    and are dropped rather than counted at a stale price."""
    out = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "bot_*.log"))):
        day = os.path.basename(path)[4:14]
        if day not in dates:
            continue
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    m = EXITSH_RE.match(line)
                    if m and m.group(7) != "no_quote":
                        out.append({
                            "date": m.group(1), "variant": m.group(2),
                            "symbol": m.group(3), "reason": m.group(7),
                            "pnl": float(m.group(8)),
                        })
        except OSError:
            continue
    return out


def load_fix2(dates: set):
    """Parse FIX2_SHADOW would-latch events → [{date, time, symbol}].
    Each is a point where the duplicate-order race fired an ambiguous
    CancelledError and Fix #2 (once live) would have latched entry_pending to
    block the duplicate. Shadow mode only logs — it does not yet block."""
    out = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "bot_*.log"))):
        day = os.path.basename(path)[4:14]
        if day not in dates:
            continue
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    m = FIX2_RE.match(line)
                    if m:
                        out.append({"date": m.group(1), "time": m.group(2),
                                    "symbol": m.group(3)})
        except OSError:
            continue
    return out


def match_shadow(trade, shadow):
    """Latest shadow decision for this trade's symbol within 120s before entry."""
    entry = trade["entry_dt"].replace(tzinfo=None)
    best = None
    for s in shadow:
        if s["date"] != trade["date"] or s["symbol"] != trade["symbol"]:
            continue
        st = datetime.datetime.strptime(f"{s['date']} {s['time']}", "%Y-%m-%d %H:%M:%S")
        delta = (entry - st).total_seconds()
        if 0 <= delta <= 120 and (best is None or st > best[0]):
            best = (st, s)
    return best[1] if best else None


def fmt_usd(v, sign=True):
    s = f"{abs(v):,.2f}"
    if sign:
        return f"+${s}" if v >= 0 else f"-${s}"
    return f"${s}"


def build(days: int, out_path: str):
    trades, artifacts = load_trades(days)
    if not trades:
        raise SystemExit("No trades found.")
    dates  = sorted({r["date"] for r in trades})
    ghosts = load_ghosts(set(dates))
    shadow = load_shadow(set(dates))
    xshad  = load_exit_shadow(set(dates))
    fix2   = load_fix2(set(dates))

    # ── Fix #2 shadow: would the latch have caught the ghosts? ────────────────
    # A ghost is a duplicate fill of an entry symbol; a FIX2_SHADOW event is the
    # ambiguous cancelled-buy on that same symbol/day that Fix #2 would latch.
    # If every ghost has a same-day, same-symbol shadow event, Fix #2 catches
    # the whole class. (Same symbol can repeat in a day, so this is a
    # correlation signal, not a per-event proof.)
    fix2_keys     = {(f["date"], f["symbol"]) for f in fix2}
    ghosts_caught = [g for g in ghosts if (g["date"], g["symbol"]) in fix2_keys]

    # ── Core KPIs ────────────────────────────────────────────────────────────
    pnl_total   = sum(r["realized_pnl"] for r in trades)
    wins        = [r for r in trades if r["realized_pnl"] >= 0]
    losses      = [r for r in trades if r["realized_pnl"] < 0]
    gross_win   = sum(r["realized_pnl"] for r in wins)
    gross_loss  = -sum(r["realized_pnl"] for r in losses)
    win_rate    = len(wins) / len(trades) * 100
    pf          = gross_win / gross_loss if gross_loss else float("inf")
    ev          = pnl_total / len(trades)
    avg_win     = gross_win / len(wins) if wins else 0
    avg_loss    = -gross_loss / len(losses) if losses else 0
    ghost_pnl   = sum(g["pnl"] for g in ghosts)
    actual_pnl  = pnl_total + ghost_pnl
    holds       = [(r["exit_dt"] - r["entry_dt"]).total_seconds() for r in trades]
    avg_hold    = sum(holds) / len(holds) / 60

    # ── Daily aggregates ─────────────────────────────────────────────────────
    daily = {d: {"pnl": 0.0, "n": 0, "wins": 0, "ghost": 0.0} for d in dates}
    for r in trades:
        d = daily[r["date"]]
        d["pnl"] += r["realized_pnl"]
        d["n"]   += 1
        d["wins"] += r["realized_pnl"] >= 0
    for g in ghosts:
        if g["date"] in daily:
            daily[g["date"]]["ghost"] += g["pnl"]

    cum, cum_curve = 0.0, []
    for d in dates:
        cum += daily[d]["pnl"] + daily[d]["ghost"]
        cum_curve.append(round(cum, 2))

    green_days = sum(1 for d in dates if daily[d]["pnl"] + daily[d]["ghost"] >= 0)

    # ── Exit reason / side breakdowns ────────────────────────────────────────
    def group(key_fn):
        g = {}
        for r in trades:
            k = key_fn(r)
            e = g.setdefault(k, {"n": 0, "pnl": 0.0, "wins": 0})
            e["n"] += 1
            e["pnl"] += r["realized_pnl"]
            e["wins"] += r["realized_pnl"] >= 0
        return g

    by_reason = group(lambda r: r["reason"])
    by_side   = group(lambda r: r["side"])
    by_hour   = group(lambda r: r["entry_dt"].astimezone().hour)

    # ── Afternoon-cutoff what-if (entries at/after 12:00 ET) ──────────────────
    # Derived from logs only — no bot change. Shows what stopping new entries
    # after noon would have done, since the edge concentrates in the morning.
    CUTOFF_HOUR = 12
    pm_trades   = [r for r in trades if r["entry_dt"].astimezone().hour >= CUTOFF_HOUR]
    am_trades   = [r for r in trades if r["entry_dt"].astimezone().hour <  CUTOFF_HOUR]
    pm_pnl      = sum(r["realized_pnl"] for r in pm_trades)
    am_pnl      = sum(r["realized_pnl"] for r in am_trades)
    pm_wins     = sum(1 for r in pm_trades if r["realized_pnl"] >= 0)
    am_wins     = sum(1 for r in am_trades if r["realized_pnl"] >= 0)

    # ── ORB shadow filter panel ──────────────────────────────────────────────
    shadow_days   = sorted({s["date"] for s in shadow})
    blocked       = []
    shadow_trades = [t for t in trades if t["date"] in shadow_days]
    for t in shadow_trades:
        m = match_shadow(t, shadow)
        if m and m["decision"] == "BLOCK":
            blocked.append({**t, "bias": m["bias"]})
    blocked_pnl   = sum(b["realized_pnl"] for b in blocked)
    shadow_actual = sum(t["realized_pnl"] for t in shadow_trades)
    shadow_filtered = shadow_actual - blocked_pnl
    blocked_wins  = [b for b in blocked if b["realized_pnl"] >= 0]
    blocked_loss  = [b for b in blocked if b["realized_pnl"] < 0]

    # ── Chart data ───────────────────────────────────────────────────────────
    chart = {
        "dates":     dates,
        "daily_pnl": [round(daily[d]["pnl"] + daily[d]["ghost"], 2) for d in dates],
        "cum":       cum_curve,
        "reasons":   {k: round(v["pnl"], 2) for k, v in sorted(by_reason.items())},
        "hours":     {f"{k:02d}:00": round(v["pnl"], 2) for k, v in sorted(by_hour.items())},
    }

    # ── HTML ─────────────────────────────────────────────────────────────────
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    if artifacts:
        art_pnl = sum(a["realized_pnl"] for a in artifacts)
        art_list = ", ".join(
            f"{a['date']} {fmt_usd(a['realized_pnl'])}" for a in artifacts
        )
        artifact_note = (
            f"Excluded {len(artifacts)} bug-artifact rows (restart-recovery "
            f"force-closes, hold &lt;60s), net {fmt_usd(art_pnl)}: {art_list}."
        )
    else:
        artifact_note = "No bug-artifact rows in window."

    def card(title, val, sub, cls=""):
        return f"""<div class="col"><div class="card px-2 py-2 text-center">
<div class="card-title mb-0">{title}</div>
<div class="stat-val fs-5 fw-bold {cls}">{val}</div>
<div class="stat-sub">{sub}</div></div></div>"""

    pnl_cls = "text-success" if actual_pnl >= 0 else "text-danger"
    cards = "".join([
        card("Net P&L (actual)", fmt_usd(actual_pnl), f"{fmt_usd(pnl_total)} booked, {fmt_usd(ghost_pnl)} ghosts", pnl_cls),
        card("Trades", str(len(trades)), f"{len(wins)} W / {len(losses)} L"),
        card("Win Rate", f"{win_rate:.1f}%", f"EV {fmt_usd(ev)}/trade",
             "text-success" if win_rate >= 50 else "text-warning"),
        card("Profit Factor", f"{pf:.2f}", f"avg W {fmt_usd(avg_win)} / L {fmt_usd(avg_loss)}",
             "text-success" if pf >= 1.5 else "text-warning"),
        card("Green Days", f"{green_days}/{len(dates)}", f"avg hold {avg_hold:.1f} min"),
        card("Ghost Events", str(len(ghosts)), f"net {fmt_usd(ghost_pnl)}",
             "text-warning" if ghosts else ""),
    ])

    reason_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v['n']}</td>"
        f"<td>{v['wins']/v['n']*100:.0f}%</td>"
        f"<td class=\"{'text-success' if v['pnl']>=0 else 'text-danger'}\">{fmt_usd(v['pnl'])}</td></tr>"
        for k, v in sorted(by_reason.items(), key=lambda x: -x[1]["pnl"])
    )
    side_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v['n']}</td>"
        f"<td>{v['wins']/v['n']*100:.0f}%</td>"
        f"<td class=\"{'text-success' if v['pnl']>=0 else 'text-danger'}\">{fmt_usd(v['pnl'])}</td></tr>"
        for k, v in sorted(by_side.items())
    )
    day_rows = "".join(
        f"<tr><td>{d}</td><td>{daily[d]['n']}</td>"
        f"<td>{daily[d]['wins']}/{daily[d]['n']}</td>"
        f"<td class=\"{'text-success' if daily[d]['pnl']>=0 else 'text-danger'}\">{fmt_usd(daily[d]['pnl'])}</td>"
        f"<td>{fmt_usd(daily[d]['ghost']) if daily[d]['ghost'] else '—'}</td>"
        f"<td class=\"{'text-success' if daily[d]['pnl']+daily[d]['ghost']>=0 else 'text-danger'}\">"
        f"{fmt_usd(daily[d]['pnl'] + daily[d]['ghost'])}</td></tr>"
        for d in reversed(dates)
    )
    block_rows = "".join(
        f"<tr><td>{b['date']} {b['entry_dt'].strftime('%H:%M')}</td>"
        f"<td>{html.escape(b['side'])} {float(b['strike']):.0f}</td>"
        f"<td>{html.escape(b['bias'])}</td><td>{html.escape(b['reason'])}</td>"
        f"<td class=\"{'text-success' if b['realized_pnl']>=0 else 'text-danger'}\">{fmt_usd(b['realized_pnl'])}</td></tr>"
        for b in blocked
    ) or "<tr><td colspan=5 class='text-muted'>No blocked trades yet</td></tr>"

    orb_improve = shadow_filtered - shadow_actual
    orb_cls     = "text-success" if orb_improve >= 0 else "text-danger"

    # ── Exit-shadow harness panel ────────────────────────────────────────────
    # Twins run alternative exit params on live quotes. 'baseline' mirrors the
    # live config and is the harness's own sanity check: if it does not track
    # the real booked P&L, the other variants are not trustworthy.
    VARIANT_LABEL = {
        "baseline":    "baseline (mirrors live)",
        "tight_trail": "tight_trail (0.92)",
        "wide_stop":   "wide_stop (0.25 / 1.0)",
        "both":        "both",
        "no_spy_stop": "no_spy_stop (underlying stop OFF)",
    }
    xs_days = sorted({x["date"] for x in xshad})
    xs_agg  = {}
    for x in xshad:
        e = xs_agg.setdefault(x["variant"], {"n": 0, "pnl": 0.0})
        e["n"]   += 1
        e["pnl"] += x["pnl"]
    # Actual booked P&L over the same days the harness was running
    xs_actual = sum(r["realized_pnl"] for r in trades if r["date"] in set(xs_days))
    xs_base   = xs_agg.get("baseline", {}).get("pnl")

    # ── tight_trail vs baseline running tally ────────────────────────────────
    # Apples-to-apples: baseline and tight_trail are twins spawned from the same
    # real entries and fed the identical live quote stream; the ONLY difference
    # is the peak-trail giveback (0.92 vs live 0.88). Their day-by-day delta is
    # therefore pure signal — immune to the baseline-vs-booked drift that comes
    # from tick-sampling. This is the number to watch for the trail decision.
    tt_daily = {}
    for x in xshad:
        if x["variant"] in ("baseline", "tight_trail"):
            e = tt_daily.setdefault(x["date"], {"baseline": 0.0, "tight_trail": 0.0})
            e[x["variant"]] += x["pnl"]
    tt_cum, tt_rows_data = 0.0, []
    for d in sorted(tt_daily):
        base = tt_daily[d]["baseline"]
        tt   = tt_daily[d]["tight_trail"]
        edge = tt - base
        tt_cum += edge
        tt_rows_data.append((d, base, tt, edge, tt_cum))
    tt_edge_total = tt_cum
    tt_win_days   = sum(1 for r in tt_rows_data if r[3] > 0)
    tt_lose_days  = sum(1 for r in tt_rows_data if r[3] < 0)

    if xs_agg:
        rows = []
        for v in ["baseline", "tight_trail", "wide_stop", "both", "no_spy_stop"]:
            if v not in xs_agg:
                continue
            a   = xs_agg[v]
            dv  = a["pnl"] - xs_actual
            cls = "text-success" if a["pnl"] >= 0 else "text-danger"
            dcl = "text-success" if dv >= 0 else "text-danger"
            note = " (sanity check)" if v == "baseline" else ""
            rows.append(
                f"<tr><td>{html.escape(VARIANT_LABEL.get(v, v))}{note}</td>"
                f"<td>{a['n']}</td>"
                f"<td class=\"{cls}\">{fmt_usd(a['pnl'])}</td>"
                f"<td class=\"{dcl}\">{fmt_usd(dv)}</td></tr>"
            )
        xs_rows = "".join(rows)
        drift   = abs(xs_base - xs_actual) if xs_base is not None else None
        xs_note = (
            f"Twins run alternative exit rules on live quotes; they place no orders. "
            f"Actual booked over the same {len(xs_days)} day(s): {fmt_usd(xs_actual)}. "
            + (f"Baseline drift vs actual: {fmt_usd(xs_base - xs_actual)} — "
               f"{'harness tracking reality well' if drift is not None and drift < 50 else 'investigate before trusting variants'}."
               if xs_base is not None else "")
        )
    else:
        xs_rows = ("<tr><td colspan=4 class='text-muted'>No twin exits recorded yet — "
                   "harness starts collecting on the next session.</td></tr>")
        xs_note = ("Spawns virtual positions per real entry under alternative exit "
                   "parameters (tighter trail / wider SPY stop) and settles them on live "
                   "quotes. Observe-only — no orders, no effect on live trading.")

    # ── tight_trail vs baseline tally rows ───────────────────────────────────
    if tt_rows_data:
        tt_rows = "".join(
            f"<tr><td>{d}</td>"
            f"<td class=\"{'text-success' if base>=0 else 'text-danger'}\">{fmt_usd(base)}</td>"
            f"<td class=\"{'text-success' if tt>=0 else 'text-danger'}\">{fmt_usd(tt)}</td>"
            f"<td class=\"{'text-success' if edge>=0 else 'text-danger'}\">{fmt_usd(edge)}</td>"
            f"<td class=\"{'text-success' if cum>=0 else 'text-danger'}\">{fmt_usd(cum)}</td></tr>"
            for (d, base, tt, edge, cum) in reversed(tt_rows_data)
        )
        tt_edge_cls = "text-success" if tt_edge_total >= 0 else "text-danger"
        tt_note = (
            f"Both series are twins fed the identical live quote stream — the only "
            f"difference is the peak-trail giveback (0.92 vs live 0.88), so their delta "
            f"is pure signal (immune to the baseline-vs-booked drift above). "
            f"Tight_trail ahead on {tt_win_days} day(s), behind on {tt_lose_days}. "
            f"Positive cumulative edge = the tighter trail is capturing more of the peak."
        )
    else:
        tt_rows = ("<tr><td colspan=5 class='text-muted'>No twin exits yet — "
                   "tally starts on the next session.</td></tr>")
        tt_edge_cls = ""
        tt_note = ("Running day-by-day tally of tight_trail (0.92) minus baseline (0.88), "
                   "both measured on identical live twins fed the same quotes.")

    # ── Fix #2 shadow panel rows ─────────────────────────────────────────────
    n_ghosts = len(ghosts)
    n_caught = len(ghosts_caught)
    if fix2:
        catch_txt = (f"{n_caught}/{n_ghosts}" if n_ghosts else f"{n_caught}/0")
        catch_cls = ("text-success" if n_ghosts and n_caught == n_ghosts
                     else "text-warning" if n_caught else "text-danger")
        fix2_rows = "".join(
            f"<tr><td>{f['date']} {f['time']}</td><td>{html.escape(f['symbol'])}</td>"
            f"<td class=\"{'text-success' if (f['date'], f['symbol']) in {(g['date'], g['symbol']) for g in ghosts} else 'text-muted'}\">"
            f"{'ghost followed' if (f['date'], f['symbol']) in {(g['date'], g['symbol']) for g in ghosts} else 'no ghost'}</td></tr>"
            for f in reversed(fix2)
        )
        fix2_note = (
            f"Each row is an ambiguous cancelled-buy where Fix #2 (once live) would "
            f"latch entry_pending and block the duplicate. Shadow mode logs only — "
            f"live order flow is unchanged. Of {n_ghosts} ghost event(s) in-window, "
            f"{n_caught} had a matching same-day shadow event → Fix #2 would have "
            f"caught {'all of them' if n_ghosts and n_caught == n_ghosts else 'that subset'}."
        )
    else:
        catch_txt = f"0/{n_ghosts}"
        catch_cls = "text-muted"
        fix2_rows = ("<tr><td colspan=3 class='text-muted'>No FIX2_SHADOW events yet — "
                     "fires on the next ambiguous cancelled-buy race. Collecting.</td></tr>")
        fix2_note = ("Shadow-log for the duplicate-order fix: logs where entry_pending "
                     "would be latched on an ambiguous cancelled buy, without changing "
                     "live order handling. Validates the fix before it ships.")

    # Afternoon-cutoff panel classes + takeaway
    am_cls     = "text-success" if am_pnl >= 0 else "text-danger"
    pm_cls     = "text-success" if pm_pnl >= 0 else "text-danger"
    cutoff_cls = "text-success" if -pm_pnl >= 0 else "text-danger"
    if pm_pnl < 0:
        cutoff_note = (f"Afternoon has been a net drag of {fmt_usd(pm_pnl)}; stopping new "
                       f"entries at noon would have improved booked P&L by {fmt_usd(-pm_pnl)}. "
                       f"Observation only — not enforced by the bot.")
    else:
        cutoff_note = (f"Afternoon is net positive ({fmt_usd(pm_pnl)}); a noon cutoff would "
                       f"have cost {fmt_usd(pm_pnl)}. No action warranted. Observation only.")

    # Window-selector nav — one link per window; the current one is highlighted.
    # "(all)" marks a window that reaches past the FULL dataset (not just this
    # page's span) — i.e. there is no more history for it to show.
    _all_csv = sorted(glob.glob(os.path.join(LOG_DIR, "trades_*.csv")))
    _earliest = os.path.basename(_all_csv[0])[7:17] if _all_csv else dates[0]
    total_span_days = (datetime.date.today() - datetime.date.fromisoformat(_earliest)).days + 1
    def _wlabel(d):
        return f"{d}d (all)" if d >= total_span_days else f"{d}d"
    nav_links = "".join(
        f'<a href="{_winfile(d)}" class="btn {"btn-info text-dark fw-bold" if d == days else "btn-outline-info"}">{_wlabel(d)}</a>'
        for d in WINDOWS
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SPY 0DTE — KPI Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{ --bs-body-bg:#0f172a; --bs-body-color:#e2e8f0; --card-bg:#1e293b;
           --border-color:#334155; --accent:#22d3ee; }}
  body {{ background:var(--bs-body-bg); color:var(--bs-body-color); font-family:'Segoe UI',sans-serif; }}
  .card {{ background:var(--card-bg); border:1px solid var(--border-color); border-radius:12px; }}
  .card-title {{ color:var(--accent); font-size:0.75rem; text-transform:uppercase;
                 letter-spacing:1px; font-weight:600; }}
  .stat-val {{ font-size:1.4rem; font-weight:700; }}
  .stat-sub {{ font-size:0.75rem; color:#94a3b8; }}
  .section-title {{ color:var(--accent); font-size:0.8rem; text-transform:uppercase;
                    letter-spacing:2px; font-weight:600; border-bottom:1px solid var(--border-color);
                    padding-bottom:8px; margin-bottom:16px; }}
  .table {{ color:var(--bs-body-color); font-size:0.8rem; }}
  .table th {{ color:var(--accent); font-weight:600; font-size:0.72rem; text-transform:uppercase;
               letter-spacing:0.5px; border-color:var(--border-color); background:#0f172a; }}
  .table td {{ border-color:var(--border-color); vertical-align:middle; }}
  .table tbody tr:hover {{ background:rgba(34,211,238,0.05); }}
  /* Brighter secondary/muted text for readability on the dark background */
  .text-muted, small.text-muted {{ color:#cbd5e1 !important; }}
  .stat-sub {{ color:#cbd5e1; }}
  /* Sortable table headers */
  table.sortable th {{ cursor:pointer; user-select:none; white-space:nowrap; }}
  table.sortable th:hover {{ color:#e2e8f0; }}
  table.sortable th::after {{ content:"\\2195"; opacity:0.35; font-size:0.7em; margin-left:4px; }}
  table.sortable th.asc::after  {{ content:"\\2191"; opacity:0.9; }}
  table.sortable th.desc::after {{ content:"\\2193"; opacity:0.9; }}
  ::-webkit-scrollbar {{ width:6px; height:6px; }}
  ::-webkit-scrollbar-track {{ background:#1e293b; }}
  ::-webkit-scrollbar-thumb {{ background:#334155; border-radius:3px; }}
</style>
</head>
<body>
<div class="container-fluid px-4 pt-4 pb-2">
  <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
    <div>
      <h4 class="mb-0 fw-bold" style="color:#22d3ee;">⚡ SPY 0DTE — KPI Dashboard
        <span style="color:#94a3b8;font-size:0.85rem;font-weight:400;">(paper, gamma-explosion strategy)</span></h4>
      <small class="text-muted">{dates[0]} → {dates[-1]} &nbsp;|&nbsp; {len(dates)} trading days in last {days}d
        &nbsp;|&nbsp; Generated {now}</small>
    </div>
    <div class="btn-group btn-group-sm" role="group" aria-label="look-back window">{nav_links}</div>
  </div>

  <div class="row g-2 mb-4 flex-nowrap">{cards}</div>

  <div class="row g-3 mb-4">
    <div class="col-md-8">
      <div class="card p-3">
        <div class="section-title">Equity Curve (cumulative, incl. ghosts)</div>
        <canvas id="eq" height="90"></canvas>
      </div>
    </div>
    <div class="col-md-4">
      <div class="card p-3">
        <div class="section-title">Daily P&L</div>
        <canvas id="daily" height="185"></canvas>
      </div>
    </div>
  </div>

  <div class="row g-3 mb-4">
    <div class="col-md-4">
      <div class="card p-3">
        <div class="section-title">P&L by Exit Reason</div>
        <table class="table table-sm mb-0 sortable"><thead>
          <tr><th>Reason</th><th>N</th><th>Win%</th><th>P&L</th></tr></thead>
          <tbody>{reason_rows}</tbody></table>
      </div>
    </div>
    <div class="col-md-4">
      <div class="card p-3">
        <div class="section-title">P&L by Side</div>
        <table class="table table-sm mb-0 sortable"><thead>
          <tr><th>Side</th><th>N</th><th>Win%</th><th>P&L</th></tr></thead>
          <tbody>{side_rows}</tbody></table>
      </div>
    </div>
    <div class="col-md-4">
      <div class="card p-3">
        <div class="section-title">P&L by Entry Hour</div>
        <canvas id="hours" height="150"></canvas>
      </div>
    </div>
  </div>

  <div class="row g-3 mb-4">
    <div class="col-md-5">
      <div class="card p-3">
        <div class="section-title">ORB Shadow Filter (observe-only, live since 2026-06-30)</div>
        <div class="d-flex gap-4 mb-2">
          <div><div class="card-title">Actual</div><div class="stat-val fs-6">{fmt_usd(shadow_actual)}</div></div>
          <div><div class="card-title">If Filtered</div><div class="stat-val fs-6">{fmt_usd(shadow_filtered)}</div></div>
          <div><div class="card-title">Filter Effect</div><div class="stat-val fs-6 {orb_cls}">{fmt_usd(orb_improve)}</div></div>
        </div>
        <div class="stat-sub mb-2">Blocked: {len(blocked_loss)} losses ({fmt_usd(sum(b['realized_pnl'] for b in blocked_loss))}),
          {len(blocked_wins)} wins ({fmt_usd(sum(b['realized_pnl'] for b in blocked_wins))})
          over {len(shadow_days)} shadow days</div>
        <table class="table table-sm mb-0 sortable"><thead>
          <tr><th>Entry</th><th>Trade</th><th>Bias</th><th>Exit</th><th>P&L</th></tr></thead>
          <tbody>{block_rows}</tbody></table>
      </div>
    </div>
    <div class="col-md-7">
      <div class="card p-3">
        <div class="section-title">Daily Detail</div>
        <div style="max-height:320px;overflow-y:auto;">
        <table class="table table-sm mb-0 sortable"><thead>
          <tr><th>Date</th><th>Trades</th><th>W/N</th><th>Booked</th><th>Ghosts</th><th>Actual</th></tr></thead>
          <tbody>{day_rows}</tbody></table>
        </div>
      </div>
    </div>
  </div>

  <div class="row g-3 mb-4">
    <div class="col-md-12">
      <div class="card p-3">
        <div class="section-title">Exit-Parameter Shadow Harness (observe-only — virtual twins, no orders)</div>
        <table class="table table-sm mb-2 sortable"><thead>
          <tr><th>Variant</th><th>Twin exits</th><th>Shadow P&L</th><th>vs Actual</th></tr></thead>
          <tbody>{xs_rows}</tbody></table>
        <div class="stat-sub">{xs_note}</div>
      </div>
    </div>
  </div>

  <div class="row g-3 mb-4">
    <div class="col-md-12">
      <div class="card p-3">
        <div class="section-title">Tight-Trail vs Baseline — Running Tally (apples-to-apples twins)</div>
        <div class="d-flex gap-4 mb-2 align-items-center flex-wrap">
          <div><div class="card-title">Cumulative Edge</div>
               <div class="stat-val fs-5 {tt_edge_cls}">{fmt_usd(tt_edge_total)}</div>
               <div class="stat-sub">tight_trail − baseline, {len(tt_rows_data)} day(s)</div></div>
          <div class="stat-sub" style="max-width:560px;">{tt_note}</div>
        </div>
        <div style="max-height:280px;overflow-y:auto;">
        <table class="table table-sm mb-0 sortable"><thead>
          <tr><th>Date</th><th>Baseline</th><th>Tight_trail</th><th>Edge</th><th>Cum. Edge</th></tr></thead>
          <tbody>{tt_rows}</tbody></table>
        </div>
      </div>
    </div>
  </div>

  <div class="row g-3 mb-4">
    <div class="col-md-12">
      <div class="card p-3">
        <div class="section-title">Fix #2 Shadow — Duplicate-Order Latch (observe-only, no order-flow change)</div>
        <div class="d-flex gap-4 mb-2 align-items-center flex-wrap">
          <div><div class="card-title">Shadow Events</div>
               <div class="stat-val fs-5">{len(fix2)}</div>
               <div class="stat-sub">would-latch fires</div></div>
          <div><div class="card-title">Ghosts Caught</div>
               <div class="stat-val fs-5 {catch_cls}">{catch_txt}</div>
               <div class="stat-sub">same-day/symbol match</div></div>
          <div class="stat-sub" style="max-width:560px;">{fix2_note}</div>
        </div>
        <div style="max-height:240px;overflow-y:auto;">
        <table class="table table-sm mb-0 sortable"><thead>
          <tr><th>When</th><th>Symbol</th><th>Outcome</th></tr></thead>
          <tbody>{fix2_rows}</tbody></table>
        </div>
      </div>
    </div>
  </div>

  <div class="row g-3 mb-4">
    <div class="col-md-12">
      <div class="card p-3">
        <div class="section-title">Morning vs Afternoon — Cutoff What-If (entries before / at-or-after 12:00 ET)</div>
        <div class="d-flex flex-wrap gap-4 align-items-center">
          <div><div class="card-title">Morning (&lt;12:00)</div>
               <div class="stat-val fs-6 {am_cls}">{fmt_usd(am_pnl)}</div>
               <div class="stat-sub">{len(am_trades)} trades · {am_wins} W</div></div>
          <div><div class="card-title">Afternoon (12:00+)</div>
               <div class="stat-val fs-6 {pm_cls}">{fmt_usd(pm_pnl)}</div>
               <div class="stat-sub">{len(pm_trades)} trades · {pm_wins} W</div></div>
          <div><div class="card-title">If stopped at noon</div>
               <div class="stat-val fs-6 {cutoff_cls}">{fmt_usd(-pm_pnl)}</div>
               <div class="stat-sub">effect vs booked {fmt_usd(pnl_total)}</div></div>
          <div class="stat-sub" style="max-width:340px;">{cutoff_note}</div>
        </div>
      </div>
    </div>
  </div>

  <div class="text-muted pb-4" style="font-size:0.7rem;">Paper trading on Alpaca.
    Ghost events = duplicate fills recovered by the sweeper; included in actual P&L.</div>
</div>

<script>
const D = {json.dumps(chart)};
const gridCfg = {{ color:'#334155' }};
const tickCfg = {{ color:'#94a3b8', font:{{size:10}} }};
new Chart(document.getElementById('eq'), {{
  type:'line',
  data:{{ labels:D.dates, datasets:[{{ data:D.cum, borderColor:'#22d3ee',
    backgroundColor:'rgba(34,211,238,0.08)', fill:true, tension:0.25, pointRadius:3 }}] }},
  options:{{ plugins:{{legend:{{display:false}}}},
    scales:{{ x:{{grid:gridCfg,ticks:tickCfg}}, y:{{grid:gridCfg,ticks:tickCfg}} }} }}
}});
new Chart(document.getElementById('daily'), {{
  type:'bar',
  data:{{ labels:D.dates, datasets:[{{ data:D.daily_pnl,
    backgroundColor:D.daily_pnl.map(v=>v>=0?'rgba(34,197,94,0.7)':'rgba(239,68,68,0.7)') }}] }},
  options:{{ plugins:{{legend:{{display:false}}}},
    scales:{{ x:{{grid:gridCfg,ticks:tickCfg}}, y:{{grid:gridCfg,ticks:tickCfg}} }} }}
}});
new Chart(document.getElementById('hours'), {{
  type:'bar',
  data:{{ labels:Object.keys(D.hours), datasets:[{{ data:Object.values(D.hours),
    backgroundColor:Object.values(D.hours).map(v=>v>=0?'rgba(34,197,94,0.7)':'rgba(239,68,68,0.7)') }}] }},
  options:{{ plugins:{{legend:{{display:false}}}},
    scales:{{ x:{{grid:gridCfg,ticks:tickCfg}}, y:{{grid:gridCfg,ticks:tickCfg}} }} }}
}});

// ── Sortable tables ─────────────────────────────────────────────────────────
// Click a header to sort by that column; click again to reverse. Numbers
// (P&L, N, %, "W/N") sort numerically, text sorts alphabetically.
function cellNum(s) {{
  // Pull the first number out of e.g. "+$1,234.00", "90%", "12/16", "put 746"
  const m = String(s).replace(/,/g, '').match(/-?\\d+(\\.\\d+)?/);
  return m ? parseFloat(m[0]) : null;
}}
document.querySelectorAll('table.sortable').forEach(function(table) {{
  const heads = table.tHead.rows[0].cells;
  Array.from(heads).forEach(function(th, col) {{
    th.addEventListener('click', function() {{
      const body = table.tBodies[0];
      const rows = Array.from(body.rows).filter(r => r.cells.length === heads.length);
      const asc  = !(th.classList.contains('asc'));
      Array.from(heads).forEach(h => h.classList.remove('asc', 'desc'));
      th.classList.add(asc ? 'asc' : 'desc');
      rows.sort(function(a, b) {{
        const x = a.cells[col].textContent.trim();
        const y = b.cells[col].textContent.trim();
        const nx = cellNum(x), ny = cellNum(y);
        let c;
        if (nx !== null && ny !== null) c = nx - ny;
        else c = x.localeCompare(y);
        return asc ? c : -c;
      }});
      rows.forEach(r => body.appendChild(r));
    }});
  }});
}});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html_doc)
    print(f"Dashboard written: {out_path}  [window: last {days}d]")
    print(f"  {len(dates)} days, {len(trades)} trades, booked {fmt_usd(pnl_total)}, "
          f"ghosts {fmt_usd(ghost_pnl)}, actual {fmt_usd(actual_pnl)}")
    print(f"  ORB shadow: {len(blocked)} blocks over {len(shadow_days)} days, "
          f"effect {fmt_usd(orb_improve)}")
    print(f"  Fix#2 shadow: {len(fix2)} would-latch events, "
          f"ghosts caught {len(ghosts_caught)}/{len(ghosts)}")
    if xs_agg:
        parts = ", ".join(f"{v}={fmt_usd(a['pnl'])}" for v, a in sorted(xs_agg.items()))
        print(f"  Exit shadow ({len(xs_days)} days, actual {fmt_usd(xs_actual)}): {parts}")
        print(f"  Tight_trail vs baseline: cum edge {fmt_usd(tt_edge_total)} "
              f"({tt_win_days}W/{tt_lose_days}L days)")
    else:
        print("  Exit shadow: no twin exits yet")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="build a single window; omit to build all WINDOWS")
    ap.add_argument("--out", default=None, help="output path for a single --days build")
    ap.add_argument("--out-dir", default=os.path.join(BASE_DIR, "gh_dashboard"),
                    help="directory for the multi-window build")
    args = ap.parse_args()

    if args.days is not None:
        out = args.out or os.path.join(args.out_dir, _winfile(args.days))
        build(args.days, out)
    else:
        # Build every window → its own self-contained page (30d is index.html).
        for d in WINDOWS:
            try:
                build(d, os.path.join(args.out_dir, _winfile(d)))
            except SystemExit as e:
                print(f"  [skip {d}d: {e}]")
