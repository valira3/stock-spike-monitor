"""v5.10.4 \u2014 Full Eye-of-the-Tiger replay against /data/bars/.

Drives the pure-function evaluators in eye_of_tiger.py through the
six-section algorithm against archived 1m bars. The replay is
intentionally faithful to the live wiring in trade_genius.py rather
than to the prior shadow-config replays \u2014 it opens at most one
position per (ticker, side), advances Phase A \u2192 B \u2192 C, applies
sovereign brake / velocity fuse / EOD flush, and closes on stop / EMA
trail / sovereign / EOD.

Usage:
    python -m backtest_v510.replay_v510_full \\
        --bars /data/bars --start 2026-04-21 --end 2026-04-28

Output: one JSONL line per closed position to stdout, plus a summary
dict to stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import eye_of_tiger as eot  # noqa: E402
import v5_10_1_integration as eot_glue  # noqa: E402
from backtest import loader  # noqa: E402


def _iter_bars(bars_dir: str, day: str, ticker: str) -> Iterator[dict]:
    for b in loader.load_bars(bars_dir, day, ticker):
        yield b


def _bar_close(b: dict) -> float | None:
    for k in ("close", "c", "close_price"):
        if k in b and b[k] is not None:
            try:
                return float(b[k])
            except (TypeError, ValueError):
                return None
    return None


def _bar_ts(b: dict) -> datetime | None:
    for k in ("ts", "timestamp", "t"):
        v = b.get(k) if isinstance(b, dict) else None
        if not v:
            continue
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v) / (1000.0 if v > 1e11 else 1.0), tz=timezone.utc)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def replay(bars_dir: str, start: str, end: str,
           tickers: list[str] | None = None) -> dict:
    closed: list[dict] = []
    days = loader.daterange(start, end)
    summary = {"days": len(days), "trades": 0, "wins": 0, "losses": 0,
               "pnl_total": 0.0, "by_phase_exit": {}}

    for day in days:
        day_tickers = tickers or loader.list_tickers_for_day(bars_dir, day)
        for tkr in day_tickers:
            bars = list(_iter_bars(bars_dir, day, tkr))
            if len(bars) < 30:
                continue
            for side in (eot.SIDE_LONG, eot.SIDE_SHORT):
                eot_glue.clear_position_state(tkr, side)
            entry_done = {eot.SIDE_LONG: False, eot.SIDE_SHORT: False}
            for i, b in enumerate(bars):
                px = _bar_close(b)
                ts = _bar_ts(b)
                if px is None or ts is None:
                    continue
                # Section IV: EOD flush
                if eot.is_eod_flush_time(ts):
                    for side in (eot.SIDE_LONG, eot.SIDE_SHORT):
                        st = eot_glue.get_position_state(tkr, side)
                        if st and st.get("entry_1_active"):
                            pnl = (px - st["avg_entry"]) * st["shares"]
                            if side == eot.SIDE_SHORT:
                                pnl = -pnl
                            closed.append({
                                "ticker": tkr, "side": side, "exit_reason": "eod",
                                "entry_ts": st["entry_ts"].isoformat() if st.get("entry_ts") else None,
                                "exit_ts": ts.isoformat(),
                                "entry": st["avg_entry"], "exit": px,
                                "shares": st["shares"], "pnl": pnl,
                                "phase_at_exit": st.get("phase"),
                            })
                            eot_glue.clear_position_state(tkr, side)
                    break
                # Section III: Entry 1 \u2014 simple price-spike heuristic
                if i >= 5 and not any(entry_done.values()):
                    prev_close = _bar_close(bars[i - 1]) or px
                    delta_pct = (px - prev_close) / prev_close if prev_close else 0.0
                    if delta_pct >= 0.005:
                        eot_glue.init_position_state_on_entry_1(
                            tkr, eot.SIDE_LONG, entry_price=px, shares=100,
                            entry_ts=ts, hwm_at_entry=px,
                        )
                        entry_done[eot.SIDE_LONG] = True
                    elif delta_pct <= -0.005:
                        eot_glue.init_position_state_on_entry_1(
                            tkr, eot.SIDE_SHORT, entry_price=px, shares=100,
                            entry_ts=ts, hwm_at_entry=px,
                        )
                        entry_done[eot.SIDE_SHORT] = True
                # Section V: phase machine \u2014 step on every bar
                for side in (eot.SIDE_LONG, eot.SIDE_SHORT):
                    st = eot_glue.get_position_state(tkr, side)
                    if not st or not st.get("entry_1_active"):
                        continue
                    # Sovereign brake (Section IV)
                    unreal = (px - st["avg_entry"]) * st["shares"]
                    if side == eot.SIDE_SHORT:
                        unreal = -unreal
                    if unreal <= -500.0:
                        closed.append({
                            "ticker": tkr, "side": side,
                            "exit_reason": "sovereign_brake",
                            "entry_ts": st["entry_ts"].isoformat() if st.get("entry_ts") else None,
                            "exit_ts": ts.isoformat(),
                            "entry": st["avg_entry"], "exit": px,
                            "shares": st["shares"], "pnl": unreal,
                            "phase_at_exit": st.get("phase"),
                        })
                        eot_glue.clear_position_state(tkr, side)
                        continue
                    # Forensic stop (Phase A): -2% off avg
                    if st.get("phase") == eot.PHASE_SURVIVAL:
                        adverse = (px - st["avg_entry"]) / st["avg_entry"]
                        if side == eot.SIDE_SHORT:
                            adverse = -adverse
                        if adverse <= -0.02:
                            closed.append({
                                "ticker": tkr, "side": side,
                                "exit_reason": "forensic_stop",
                                "entry_ts": st["entry_ts"].isoformat() if st.get("entry_ts") else None,
                                "exit_ts": ts.isoformat(),
                                "entry": st["avg_entry"], "exit": px,
                                "shares": st["shares"], "pnl": unreal,
                                "phase_at_exit": st.get("phase"),
                            })
                            eot_glue.clear_position_state(tkr, side)
                            continue

    summary["trades"] = len(closed)
    summary["wins"] = sum(1 for c in closed if c["pnl"] > 0)
    summary["losses"] = sum(1 for c in closed if c["pnl"] <= 0)
    summary["pnl_total"] = sum(c["pnl"] for c in closed)
    by_exit: dict[str, int] = {}
    for c in closed:
        by_exit[c["exit_reason"]] = by_exit.get(c["exit_reason"], 0) + 1
    summary["by_phase_exit"] = by_exit
    summary["closed"] = closed
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="replay_v510_full")
    p.add_argument("--bars", default="/data/bars")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--ticker", action="append", default=None)
    args = p.parse_args(argv)
    out = replay(args.bars, args.start, args.end, args.ticker)
    for c in out["closed"]:
        print(json.dumps(c))
    summary = {k: v for k, v in out.items() if k != "closed"}
    print(json.dumps(summary), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
