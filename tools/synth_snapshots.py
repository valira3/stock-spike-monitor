#!/usr/bin/env python3
"""synth_snapshots.py -- synthesize /api/state snapshots from a backtest day.

Replaces unreliable / missing snapshots-live captures with deterministic
synthetic snapshots derived from:
  - results/week_replay_v2/{portfolio}_{DATE}.jsonl  (orb_replay_day output)
  - results/week_replay/{portfolio}_eod/per_day/{DATE}.json (afternoon_backtest)
  - data/{DATE}/{TICKER}.jsonl  (1-min RTH bars for mark/unrealized math)

For each (date), emits one snapshot per 5-min bucket (78 buckets/day,
09:30 -> 16:00 ET) shaped as the snapshots-live schema:

    {"ts_et": "2026-05-15T10:15:00 ET",
     "captured_at_utc": "2026-05-15T14:15:00Z",
     "state": { /api/state at that minute, synthesized }}

The "state" object is built from a base template (one real captured
snapshot at 09:30 ET on 2026-05-15) with time-varying fields updated
per-bucket:
  - server_time / server_time_label
  - trades_today (cumulative BUY/SELL events fired by this bucket)
  - positions (open positions at this bucket, with current mark/unrealized)
  - portfolios.{main,val}.equity / day_pnl / positions / trades_today

Usage:
    python tools/synth_snapshots.py --dates 2026-05-11,...,2026-05-15
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import date as _date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

TICKERS = ("AAPL", "AMZN", "AVGO", "GOOG", "META", "MSFT",
           "NFLX", "NVDA", "ORCL", "QQQ", "SPY", "TSLA")

START_MIN = 570  # 9:30 ET
END_MIN = 960    # 16:00 ET
STEP_MIN = 5     # one snapshot per 5-min bucket

PORTFOLIOS_EQUITY = {"main": 100_000.0, "val": 30_185.0}


def _load_base_state() -> dict:
    """Grab one real snapshot from snapshots-live as the template."""
    snap_file = REPO / "data" / "snapshots" / "2026-05-15.jsonl"
    if not snap_file.exists():
        raise FileNotFoundError(
            f"Need a real snapshot template at {snap_file}. "
            "Pull from origin/snapshots-live first."
        )
    for line in snap_file.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("state"):
            return d["state"]
    raise RuntimeError("No state-bearing line in template file")


def _load_morning_events(date_str: str, portfolio: str) -> tuple[list, list, dict]:
    """Read orb_replay_day output -> (admits, exits, or_locks_by_ticker)."""
    f = REPO / "results" / "week_replay_v2" / f"{portfolio}_{date_str}.jsonl"
    admits, exits = [], []
    or_locks: dict = {}
    if not f.exists():
        return admits, exits, or_locks
    for line in f.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        k = e.get("kind")
        if k == "admit":
            admits.append(e)
        elif k == "exit":
            exits.append(e)
        elif k == "or_lock":
            or_locks[e["ticker"]] = e
    return admits, exits, or_locks


def _load_eod_pairs(date_str: str, portfolio: str) -> list[dict]:
    f = REPO / "results" / "week_replay" / f"{portfolio}_eod" / "per_day" / f"{date_str}.json"
    if not f.exists():
        return []
    try:
        d = json.loads(f.read_text())
    except json.JSONDecodeError:
        return []
    return d.get("pnl_pairs") or []


def _load_bars(date_str: str) -> dict[str, dict[int, dict]]:
    """Return {ticker: {minute: bar}} for RTH 1-min bars."""
    out: dict[str, dict[int, dict]] = {}
    for tk in TICKERS:
        f = REPO / "data" / date_str / f"{tk}.jsonl"
        if not f.exists():
            continue
        bars: dict[int, dict] = {}
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            b = json.loads(line)
            dt = datetime.fromisoformat(b["ts"]).astimezone(ET)
            m = dt.hour * 60 + dt.minute
            bars[m] = b
        out[tk] = bars
    return out


def _mark_for(bars: dict[str, dict[int, dict]], ticker: str, minute: int) -> float | None:
    """Last close at or before `minute` for `ticker`."""
    tk_bars = bars.get(ticker, {})
    for m in range(minute, START_MIN - 1, -1):
        if m in tk_bars:
            return float(tk_bars[m].get("close") or 0)
    return None


def _pair_admits_exits(admits: list[dict], exits: list[dict]) -> list[dict]:
    """Pair admit -> exit by ticket_id, compute pnl per trade."""
    by_id = {a["ticket_id"]: a for a in admits if a.get("ticket_id")}
    pairs = []
    for e in exits:
        a = by_id.get(e.get("ticket_id"))
        if not a:
            continue
        side = a["side"]
        entry = float(a["price"])
        exitp = float(e.get("exit_price") or e.get("price") or 0)
        shares = int(a["shares"])
        pnl = (exitp - entry) * shares if side == "long" else (entry - exitp) * shares
        pairs.append({
            "ticker": a["ticker"],
            "side": side,
            "entry_min": int(a["bucket"]),
            "exit_min": int(e["bucket"]),
            "entry_price": entry,
            "exit_price": exitp,
            "shares": shares,
            "stop_price": float(a.get("stop") or 0),
            "pnl": pnl,
            "exit_reason": e.get("reason", "?"),
            "leg": "morning",
        })
    return pairs


def _trade_row(p: dict, action: str, price: float, minute: int, date_str: str,
               portfolio: str) -> dict:
    """Render a trades_today row for either entry or exit action."""
    h, m = minute // 60, minute % 60
    return {
        "action": action,
        "ticker": p["ticker"],
        "time": f"{h:02d}:{m:02d} ET",
        "price": round(price, 4),
        "shares": p["shares"],
        "stop": round(p.get("stop_price") or 0, 4),
        "entry_price": round(p["entry_price"], 4),
        "side": p["side"].upper(),
        "portfolio": portfolio,
        "date": date_str,
        "leg": p.get("leg", "morning"),
        "reason": p.get("exit_reason") if action in ("SELL", "COVER") else "",
        "pnl": round(p["pnl"], 2) if action in ("SELL", "COVER") else None,
    }


def _position_row(p: dict, mark: float, minute: int, date_str: str) -> dict:
    """Render an open-position row at the given minute."""
    side_sign = 1 if p["side"] == "long" else -1
    unrealized = (mark - p["entry_price"]) * side_sign * p["shares"]
    held_seconds = (minute - p["entry_min"]) * 60
    # Build UTC entry ts (entry_min is ET; UTC = ET + 4 in May = 9:30 ET -> 13:30 UTC)
    eh, em = p["entry_min"] // 60, p["entry_min"] % 60
    et_dt = datetime.combine(_date.fromisoformat(date_str), time(eh, em, tzinfo=ET))
    return {
        "ticker": p["ticker"],
        "side": p["side"].upper(),
        "shares": p["shares"],
        "entry": round(p["entry_price"], 4),
        "mark": round(mark, 4),
        "cost": round(p["entry_price"] * p["shares"], 2),
        "entry_stop": round(p.get("stop_price") or 0, 4),
        "stop": round(p.get("stop_price") or 0, 4),
        "effective_stop": round(p.get("stop_price") or 0, 4),
        "unrealized": round(unrealized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "risk_dollars": round(abs(p["entry_price"] - (p.get("stop_price") or p["entry_price"])) * p["shares"], 2),
        "held_seconds": held_seconds,
        "entry_ts_utc": et_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "phase": "A",
        "leg": p.get("leg", "morning"),
        "portfolio": p.get("portfolio", "main"),
    }


def _build_snapshot(base: dict, date_str: str, minute: int,
                    pairs_main: list[dict], pairs_val: list[dict],
                    bars: dict[str, dict[int, dict]]) -> dict:
    """Build one /api/state snapshot at the given minute."""
    state = copy.deepcopy(base)
    h, m = minute // 60, minute % 60
    et_dt = datetime.combine(_date.fromisoformat(date_str), time(h, m, tzinfo=ET))
    state["server_time"] = et_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["server_time_label"] = (
        et_dt.strftime("%a %b %-d") + f" | {h:02d}:{m:02d}:00 ET"
    )
    state["version"] = "9.1.130-replay"

    # Per-portfolio buckets.
    portfolios_out = {}
    for portfolio, pairs in (("main", pairs_main), ("val", pairs_val)):
        starting_equity = PORTFOLIOS_EQUITY[portfolio]
        trades_today_p, positions_p = [], []
        realized = 0.0
        for p in pairs:
            ent = p["entry_min"]
            ext = p["exit_min"]
            if minute < ent:
                continue
            # Entry event (BUY for long, SHORT for short)
            entry_action = "BUY" if p["side"] == "long" else "SHORT"
            trades_today_p.append(_trade_row(p, entry_action, p["entry_price"], ent, date_str, portfolio))
            if minute >= ext:
                # Exit event already happened
                exit_action = "SELL" if p["side"] == "long" else "COVER"
                trades_today_p.append(_trade_row(p, exit_action, p["exit_price"], ext, date_str, portfolio))
                realized += p["pnl"]
            else:
                # Still open at `minute` -> render as a position
                mark = _mark_for(bars, p["ticker"], minute) or p["entry_price"]
                positions_p.append(_position_row({**p, "portfolio": portfolio}, mark, minute, date_str))

        portfolios_out[portfolio] = {
            "portfolio_id": portfolio,
            "equity": round(starting_equity + realized, 2),
            "day_pnl": round(realized, 2),
            "positions": positions_p,
            "trades_today": trades_today_p,
            "strip": {
                "cooldowns": {"long": 0, "short": 0, "total": 0},
                "errors": {"count": 0, "last": None},
                "positions": len(positions_p),
                "day_pnl": round(realized, 2),
                "state": "active" if positions_p or trades_today_p else ("paused" if minute < START_MIN else "active"),
            },
            "subscribed": True,
        }
    # Preserve gene from base (disabled) but reset its activity
    if "gene" in state.get("portfolios", {}):
        portfolios_out["gene"] = {**state["portfolios"]["gene"], "trades_today": [], "positions": [], "day_pnl": 0.0}
    state["portfolios"] = portfolios_out

    # Top-level "main"-shaped fields (legacy clients read these).
    state["trades_today"] = portfolios_out["main"]["trades_today"]
    state["positions"] = portfolios_out["main"]["positions"]

    return state


def synth_day(date_str: str, base: dict) -> list[dict]:
    """Synthesize all 5-min snapshots for one trading day."""
    # Morning admits/exits per portfolio
    adm_m, ex_m, _ = _load_morning_events(date_str, "main")
    adm_v, ex_v, _ = _load_morning_events(date_str, "val")
    pairs_main = _pair_admits_exits(adm_m, ex_m)
    pairs_val = _pair_admits_exits(adm_v, ex_v)

    # EOD pairs per portfolio
    for p in _load_eod_pairs(date_str, "main"):
        pairs_main.append({
            "ticker": p["ticker"], "side": p["side"],
            "entry_min": int(p["entry_bucket"]),
            "exit_min": int(p["exit_bucket"]),
            "entry_price": float(p["entry_price"]),
            "exit_price": float(p["exit_price"]),
            "shares": int(p["shares"]),
            "stop_price": float(p.get("stop_price") or 0),
            "pnl": float(p["pnl_dollars"]),
            "exit_reason": p.get("exit_reason", "eod"),
            "leg": "eod",
        })
    for p in _load_eod_pairs(date_str, "val"):
        pairs_val.append({
            "ticker": p["ticker"], "side": p["side"],
            "entry_min": int(p["entry_bucket"]),
            "exit_min": int(p["exit_bucket"]),
            "entry_price": float(p["entry_price"]),
            "exit_price": float(p["exit_price"]),
            "shares": int(p["shares"]),
            "stop_price": float(p.get("stop_price") or 0),
            "pnl": float(p["pnl_dollars"]),
            "exit_reason": p.get("exit_reason", "eod"),
            "leg": "eod",
        })

    bars = _load_bars(date_str)

    # Emit one snapshot every 5 min from 9:30 to 16:05 ET.
    out = []
    for minute in range(START_MIN, END_MIN + 5 + 1, STEP_MIN):
        state = _build_snapshot(base, date_str, minute, pairs_main, pairs_val, bars)
        et_label = state["server_time_label"]
        # ts_et formatted as the snapshots-live schema expects.
        et_iso = (
            datetime.combine(_date.fromisoformat(date_str),
                             time(minute // 60, minute % 60, tzinfo=ET))
            .isoformat()
        )
        # Strip the tz suffix and append " ET" to match snapshots-live convention.
        et_iso_clean = et_iso.rsplit("-", 1)[0]  # drop "-04:00"
        out.append({
            "ts_et": et_iso_clean + " ET",
            "captured_at_utc": (
                datetime.combine(_date.fromisoformat(date_str),
                                 time(minute // 60, minute % 60, tzinfo=ET))
                .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            ),
            "state": state,
        })
    return out


def summarize(date_str: str) -> dict:
    """Compact per-day summary for the trading-simulation report."""
    adm_m, ex_m, _ = _load_morning_events(date_str, "main")
    adm_v, ex_v, _ = _load_morning_events(date_str, "val")
    pairs_main_morn = _pair_admits_exits(adm_m, ex_m)
    pairs_val_morn = _pair_admits_exits(adm_v, ex_v)
    eod_main = _load_eod_pairs(date_str, "main")
    eod_val = _load_eod_pairs(date_str, "val")

    def pl(pairs):
        return sum(float(p["pnl"] if "pnl" in p else p.get("pnl_dollars", 0)) for p in pairs)

    return {
        "date": date_str,
        "main_morning_pnl": round(pl(pairs_main_morn), 2),
        "main_morning_n": len(pairs_main_morn),
        "main_eod_pnl": round(pl(eod_main), 2),
        "main_eod_n": len(eod_main),
        "main_total": round(pl(pairs_main_morn) + pl(eod_main), 2),
        "val_morning_pnl": round(pl(pairs_val_morn), 2),
        "val_morning_n": len(pairs_val_morn),
        "val_eod_pnl": round(pl(eod_val), 2),
        "val_eod_n": len(eod_val),
        "val_total": round(pl(pairs_val_morn) + pl(eod_val), 2),
        "trades": [
            {**p, "portfolio": "main", "leg": p.get("leg", "morning")} for p in pairs_main_morn
        ] + [
            {"ticker": p["ticker"], "side": p["side"], "entry_min": int(p["entry_bucket"]),
             "exit_min": int(p["exit_bucket"]), "entry_price": float(p["entry_price"]),
             "exit_price": float(p["exit_price"]), "shares": int(p["shares"]),
             "pnl": float(p["pnl_dollars"]), "exit_reason": p.get("exit_reason", "eod"),
             "leg": "eod", "portfolio": "main"} for p in eod_main
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default="2026-05-11,2026-05-12,2026-05-13,2026-05-14,2026-05-15")
    ap.add_argument("--out-dir", default="data/snapshots")
    args = ap.parse_args()

    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    base = _load_base_state()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== synth_snapshots: {len(dates)} dates ===\n")
    for d in dates:
        snaps = synth_day(d, base)
        out_path = out_dir / f"{d}.jsonl"
        with open(out_path, "w", encoding="utf-8") as fh:
            for s in snaps:
                fh.write(json.dumps(s, separators=(",", ":")) + "\n")
        s = summarize(d)
        print(f"{d}: {len(snaps)} snapshots written")
        print(f"  Main: {s['main_morning_n']} morning + {s['main_eod_n']} EOD = ${s['main_total']:>+8,.0f}")
        print(f"  Val : {s['val_morning_n']} morning + {s['val_eod_n']} EOD = ${s['val_total']:>+8,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
