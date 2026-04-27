"""v5.4.0 offline replay engine.

Iterates over bars in /data/bars/ for the requested date range,
applies one of the SHADOW_CONFIGS gate rules per minute, opens a
synthetic position when the gate passes on a price-spike candidate,
then closes the position via a simple trail-stop / hard-eject / EOD
exit policy. The pairing math matches `replay_gene_configs.pnl_per_pair`.

This is the v5.4.0 MVP \u2014 the goal is a deterministic offline
replayer whose outputs can be diffed against real prod records via
--validate. The replay does NOT attempt to reproduce every nuance of
trade_genius's live decision tree. It is a minimum, faithful gate +
P&L pairer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from . import loader, ledger

# Lazy import shim \u2014 volume_profile drags in heavy deps.
def _load_shadow_configs() -> list[dict]:
    try:
        import volume_profile as _vp
        return list(_vp.SHADOW_CONFIGS)
    except Exception:
        # Fallback for environments where volume_profile cannot import.
        return [
            {"name": "TICKER+QQQ", "ticker_enabled": True, "index_enabled": True,
             "ticker_pct": 70, "index_pct": 100},
            {"name": "TICKER_ONLY", "ticker_enabled": True, "index_enabled": False,
             "ticker_pct": 70, "index_pct": 100},
            {"name": "QQQ_ONLY", "ticker_enabled": False, "index_enabled": True,
             "ticker_pct": 70, "index_pct": 100},
            {"name": "GEMINI_A", "ticker_enabled": True, "index_enabled": True,
             "ticker_pct": 110, "index_pct": 85},
            {"name": "BUCKET_FILL_100", "ticker_enabled": True, "index_enabled": True,
             "ticker_pct": 100, "index_pct": 100},
        ]


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------

def _et_bucket(ts_iso: str) -> str:
    """Convert UTC ISO ts to ET HHMM bucket key."""
    if not ts_iso:
        return ""
    s = ts_iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(ZoneInfo("America/New_York"))
    return f"{et.hour:02d}{et.minute:02d}"


def _gate_pass(cfg: dict, ticker_pct: Optional[float],
               index_pct: Optional[float]) -> bool:
    """Apply the shadow-config thresholds to the live percentages.

    Mirrors the lambdas in /home/user/workspace/backtest_v510/replay_gene_configs.py.
    """
    if cfg.get("ticker_enabled"):
        if ticker_pct is None:
            return False
        if ticker_pct < cfg["ticker_pct"]:
            return False
    if cfg.get("index_enabled"):
        if index_pct is None:
            return False
        if index_pct < cfg["index_pct"]:
            return False
    return True


# ---------------------------------------------------------------------------
# Replay engine
# ---------------------------------------------------------------------------

# Defaults chosen to match the v5.x paper executor: $1000/position,
# 1.5% trail, 3% hard-eject. These are intentionally simple \u2014 the
# engine is a backtester, not a trading bot.
DEFAULT_QTY_DOLLARS = 1000.0
DEFAULT_TRAIL_PCT = 0.015
DEFAULT_HARD_EJECT_PCT = 0.03

# Minimum spike to consider an entry candidate, vs the prior bar close.
ENTRY_TRIGGER_PCT = 0.005


def _ts_of(bar: dict) -> str:
    return bar.get("ts") or ""


def _close(bar: dict) -> Optional[float]:
    v = bar.get("close")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _vol(bar: dict) -> int:
    v = bar.get("iex_volume")
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _ticker_pct(cur_v: int, baseline: Optional[int]) -> Optional[float]:
    if not baseline:
        return None
    return round((cur_v / baseline) * 100.0, 2)


def replay_one_day(
    bars_dir: str,
    day: str,
    config: dict,
    *,
    index_symbol: str = "QQQ",
    qty_dollars: float = DEFAULT_QTY_DOLLARS,
    trail_pct: float = DEFAULT_TRAIL_PCT,
    hard_eject_pct: float = DEFAULT_HARD_EJECT_PCT,
    profile_baseline: Optional[dict] = None,
) -> list[dict]:
    """Replay one trading day for one config.

    Args:
        profile_baseline: optional dict {ticker: median_volume_per_minute}.
            If absent, an in-day rolling median is used as a self-baseline so
            the replayer can run on a stub archive without /data/volume_profiles/.
    """
    tickers = loader.list_tickers_for_day(bars_dir, day)
    # Index bars are loaded once for the whole day.
    idx_bars = loader.load_bars(bars_dir, day, index_symbol)
    idx_by_ts = {_ts_of(b): b for b in idx_bars}
    idx_self_baseline = _self_baseline([_vol(b) for b in idx_bars])

    rows: list[dict] = []
    for tk in tickers:
        if tk == index_symbol:
            continue
        bars = loader.load_bars(bars_dir, day, tk)
        if not bars:
            continue
        rows.extend(_replay_ticker(
            tk, bars, idx_by_ts, idx_self_baseline,
            config, qty_dollars, trail_pct, hard_eject_pct,
            profile_baseline,
        ))
    return rows


def _self_baseline(volumes: list[int]) -> Optional[int]:
    """Median of nonzero volumes for self-baselining when no profile is loaded."""
    nz = sorted(v for v in volumes if v > 0)
    if not nz:
        return None
    n = len(nz)
    if n % 2 == 1:
        return int(nz[n // 2])
    return int((nz[n // 2 - 1] + nz[n // 2]) / 2)


def _replay_ticker(
    ticker: str,
    bars: list[dict],
    idx_by_ts: dict[str, dict],
    idx_self_baseline: Optional[int],
    config: dict,
    qty_dollars: float,
    trail_pct: float,
    hard_eject_pct: float,
    profile_baseline: Optional[dict],
) -> list[dict]:
    self_baseline = _self_baseline([_vol(b) for b in bars])
    if profile_baseline:
        ticker_baseline = profile_baseline.get(ticker) or self_baseline
        index_baseline = profile_baseline.get("__index__") or idx_self_baseline
    else:
        ticker_baseline = self_baseline
        index_baseline = idx_self_baseline

    out: list[dict] = []
    open_pos: Optional[dict] = None  # {entry_ts, entry_price, qty, peak}

    prev_close: Optional[float] = None
    for i, bar in enumerate(bars):
        c = _close(bar)
        ts = _ts_of(bar)
        if c is None or not ts:
            continue

        # If position open, evaluate exits first (intra-bar logic
        # collapses to bar-close evaluation \u2014 matches trade_genius's
        # close-of-minute decision cadence).
        if open_pos is not None:
            open_pos["peak"] = max(open_pos["peak"], c)
            entry_p = open_pos["entry_price"]
            peak = open_pos["peak"]
            exit_reason: Optional[str] = None
            # Hard eject: drawdown from entry.
            if c <= entry_p * (1.0 - hard_eject_pct):
                exit_reason = "hard_eject"
            # Trail stop: drawdown from peak.
            elif c <= peak * (1.0 - trail_pct) and peak > entry_p:
                exit_reason = "trail_stop"
            # EOD close: last bar of the day.
            elif i == len(bars) - 1:
                exit_reason = "eod"
            if exit_reason is not None:
                qty = open_pos["qty"]
                pnl_dollars = round((c - entry_p) * qty, 2)
                pnl_pct = round((c - entry_p) / entry_p * 100.0, 4) if entry_p else 0.0
                out.append({
                    "ticker": ticker,
                    "side": "BUY",
                    "entry_ts": open_pos["entry_ts"],
                    "entry_price": round(entry_p, 4),
                    "exit_ts": ts,
                    "exit_price": round(c, 4),
                    "qty": qty,
                    "pnl_dollars": pnl_dollars,
                    "pnl_pct": pnl_pct,
                    "exit_reason": exit_reason,
                })
                open_pos = None
                prev_close = c
                continue
            prev_close = c
            continue

        # No open position \u2014 look for entry candidates.
        if prev_close is not None and prev_close > 0:
            move = (c - prev_close) / prev_close
            if move >= ENTRY_TRIGGER_PCT:
                # Volume gate.
                cur_v = _vol(bar)
                idx_bar = idx_by_ts.get(ts) or {}
                idx_v = _vol(idx_bar)
                t_pct = _ticker_pct(cur_v, ticker_baseline)
                q_pct = _ticker_pct(idx_v, index_baseline)
                if _gate_pass(config, t_pct, q_pct):
                    qty = max(1, int(qty_dollars / c)) if c > 0 else 1
                    open_pos = {
                        "entry_ts": ts,
                        "entry_price": c,
                        "qty": qty,
                        "peak": c,
                    }
        prev_close = c

    return out


# ---------------------------------------------------------------------------
# Validation: replay-vs-prod
# ---------------------------------------------------------------------------

WINDOW_SECONDS = 60


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except ValueError:
        return None


def validate(
    replay_rows: list[dict],
    prod_entries: list[dict],
    window_seconds: int = WINDOW_SECONDS,
) -> dict:
    """Pair predicted replay entries with prod shadow_positions entries.

    A match requires same ticker + same side + entry_ts within `window_seconds`.
    Each prod row is matched at most once (greedy on closest-ts).

    Returns:
        {
            "match_rate": float in [0, 1],
            "matches": [ {replay, prod, entry_drift_sec, entry_price_diff,
                          exit_price_diff} ],
            "replay_only": [ replay_row ],
            "prod_only": [ prod_row ],
        }
    """
    matches: list[dict] = []
    used_prod: set[int] = set()
    replay_only: list[dict] = []

    prod_parsed = []
    for i, p in enumerate(prod_entries):
        prod_parsed.append({
            "idx": i,
            "row": p,
            "ts": _parse_ts(p.get("entry_ts_utc") or ""),
            "ticker": (p.get("ticker") or "").upper(),
            "side": (p.get("side") or "").upper(),
        })

    for r in replay_rows:
        rts = _parse_ts(r.get("entry_ts") or "")
        rtk = (r.get("ticker") or "").upper()
        rside = (r.get("side") or "").upper()
        if rts is None or not rtk:
            replay_only.append(r)
            continue
        best_idx: Optional[int] = None
        best_drift: float = float("inf")
        for cand in prod_parsed:
            if cand["idx"] in used_prod:
                continue
            if cand["ticker"] != rtk or cand["side"] != rside:
                continue
            if cand["ts"] is None:
                continue
            drift = abs((rts - cand["ts"]).total_seconds())
            if drift <= window_seconds and drift < best_drift:
                best_drift = drift
                best_idx = cand["idx"]
        if best_idx is None:
            replay_only.append(r)
        else:
            used_prod.add(best_idx)
            prod_row = prod_parsed[best_idx]["row"]
            entry_diff = None
            try:
                if prod_row.get("entry_price") is not None:
                    entry_diff = round(
                        float(r.get("entry_price") or 0.0)
                        - float(prod_row["entry_price"]),
                        4,
                    )
            except (TypeError, ValueError):
                entry_diff = None
            exit_diff = None
            try:
                if (prod_row.get("exit_price") is not None
                        and r.get("exit_price") is not None):
                    exit_diff = round(
                        float(r["exit_price"]) - float(prod_row["exit_price"]),
                        4,
                    )
            except (TypeError, ValueError):
                exit_diff = None
            matches.append({
                "replay": r,
                "prod": prod_row,
                "entry_drift_sec": round(best_drift, 2),
                "entry_price_diff": entry_diff,
                "exit_price_diff": exit_diff,
            })

    prod_only = [p["row"] for p in prod_parsed if p["idx"] not in used_prod]
    n_prod = len(prod_entries)
    match_rate = (len(matches) / n_prod) if n_prod else 1.0
    return {
        "match_rate": round(match_rate, 4),
        "matches": matches,
        "replay_only": replay_only,
        "prod_only": prod_only,
    }


def write_validation_report(out_path: str, config_name: str,
                            start: str, end: str, vr: dict) -> str:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    matches = vr["matches"]
    n_match = len(matches)
    drift_entry = [m["entry_price_diff"] for m in matches
                   if m.get("entry_price_diff") is not None]
    drift_exit = [m["exit_price_diff"] for m in matches
                  if m.get("exit_price_diff") is not None]

    def _avg(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    lines: list[str] = []
    lines.append(f"# Replay-vs-Prod Validation \u2014 {config_name} {start} -> {end}\n")
    lines.append(f"- Match rate: **{vr['match_rate']:.2%}**")
    lines.append(f"- Matches: {n_match}")
    lines.append(f"- REPLAY_ONLY (replay said enter, prod did not): "
                 f"{len(vr['replay_only'])}")
    lines.append(f"- PROD_ONLY  (prod entered, replay did not): "
                 f"{len(vr['prod_only'])}\n")

    lines.append("## Drift summary (matched pairs)\n")
    lines.append(f"- Avg entry_price diff (replay - prod): {_avg(drift_entry)}")
    lines.append(f"- Avg exit_price diff  (replay - prod): {_avg(drift_exit)}\n")

    if vr["replay_only"]:
        lines.append("## REPLAY_ONLY entries\n")
        lines.append("| ticker | side | entry_ts | entry_price |")
        lines.append("|---|---|---|---|")
        for r in vr["replay_only"]:
            lines.append(
                f"| {r.get('ticker')} | {r.get('side')} | "
                f"{r.get('entry_ts')} | {r.get('entry_price')} |"
            )
        lines.append("")

    if vr["prod_only"]:
        lines.append("## PROD_ONLY entries\n")
        lines.append("| ticker | side | entry_ts_utc | entry_price |")
        lines.append("|---|---|---|---|")
        for r in vr["prod_only"]:
            lines.append(
                f"| {r.get('ticker')} | {r.get('side')} | "
                f"{r.get('entry_ts_utc')} | {r.get('entry_price')} |"
            )
        lines.append("")

    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p.resolve())


# ---------------------------------------------------------------------------
# v5.5.0 \u2014 dashboard-shape equity export
# ---------------------------------------------------------------------------

def equity_charts_payload(rows_by_cfg: dict[str, list[dict]]) -> dict:
    """Build a payload matching /api/shadow_charts from replay rows.

    Args:
        rows_by_cfg: mapping of config name -> list of replay rows
            (closed trades, ordered by entry_ts).

    Returns:
        { "configs": { <name>: { equity_curve, daily_pnl,
                                 win_rate_rolling } }, "as_of": <utc iso> }
    """
    out: dict[str, dict] = {}
    for name, rows in rows_by_cfg.items():
        cfg_rows = sorted(rows, key=lambda r: (r.get("exit_ts") or ""))
        cum = 0.0
        equity_curve: list[dict] = []
        daily: dict[str, dict] = {}
        wr_rolling: list[dict] = []
        wins_window: list[int] = []
        for idx, r in enumerate(cfg_rows, start=1):
            pnl = float(r.get("pnl_dollars") or 0.0)
            cum += pnl
            ts = r.get("exit_ts") or ""
            equity_curve.append({"ts": ts, "cum_pnl": round(cum, 2)})
            day = ts[:10] if len(ts) >= 10 else ts
            d = daily.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0})
            d["pnl"] = round(d["pnl"] + pnl, 2)
            d["trades"] += 1
            wins_window.append(1 if pnl > 0 else 0)
            if len(wins_window) > 20:
                wins_window.pop(0)
            if idx >= 20:
                wr = sum(wins_window) / 20.0
                wr_rolling.append({
                    "trade_idx": idx,
                    "win_rate": round(wr, 4),
                })
        out[name] = {
            "equity_curve": equity_curve,
            "daily_pnl": sorted(daily.values(), key=lambda d: d["date"]),
            "win_rate_rolling": wr_rolling,
        }
    return {
        "configs": out,
        "as_of": datetime.now(timezone.utc)
                       .isoformat().replace("+00:00", "Z"),
    }


def write_equity_export(path: str | os.PathLike,
                        rows_by_cfg: dict[str, list[dict]]) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = equity_charts_payload(rows_by_cfg)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(p.resolve())


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------

def _select_configs(name: str) -> list[dict]:
    all_cfgs = _load_shadow_configs()
    if name == "ALL":
        return all_cfgs
    for c in all_cfgs:
        if c["name"] == name:
            return [c]
    raise SystemExit(
        f"unknown --config {name!r}. Available: "
        f"{', '.join(c['name'] for c in all_cfgs)}, ALL"
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backtest.replay",
        description=(
            "v5.4.0 offline backtest replay. Replays SHADOW_CONFIGS "
            "over /data/bars/ JSONL archives and optionally validates "
            "predicted entries against prod state.db."
        ),
    )
    p.add_argument("--start", required=True, help="YYYY-MM-DD inclusive start")
    p.add_argument("--end", required=True, help="YYYY-MM-DD inclusive end")
    p.add_argument("--config", required=True,
                   help="SHADOW_CONFIGS name, or 'ALL' for every config")
    p.add_argument("--validate", action="store_true",
                   help="Compare replay vs prod shadow_positions in state.db")
    p.add_argument("--out", default="./backtest_out/",
                   help="Output directory (default: ./backtest_out/)")
    p.add_argument("--bars-dir", default="/data/bars/",
                   help="Bar archive directory (default: /data/bars/)")
    p.add_argument("--state-db", default="/data/state.db",
                   help="state.db path (default: /data/state.db)")
    # v5.5.0 \u2014 chart-shape JSON export, same format as /api/shadow_charts.
    p.add_argument("--export-equity", default=None, metavar="PATH",
                   help=(
                       "Optional path. When set, write a JSON file with the "
                       "same shape as /api/shadow_charts (configs: equity_curve, "
                       "daily_pnl, win_rate_rolling) so dashboard charts can be "
                       "rendered offline from a backtest run."
                   ))
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    configs = _select_configs(args.config)
    days = loader.daterange(args.start, args.end)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    overall_exit = 0
    rows_by_cfg: dict[str, list[dict]] = {}
    for cfg in configs:
        all_rows: list[dict] = []
        for day in days:
            all_rows.extend(replay_one_day(args.bars_dir, day, cfg))
        rows_by_cfg[cfg["name"]] = all_rows
        ledger_path = out_dir / f"{cfg['name']}_{args.start}_{args.end}.csv"
        ledger.write_ledger(str(ledger_path), all_rows)
        s = ledger.summarize(all_rows)
        print(
            f"[{cfg['name']}] trades={s['trades']} wins={s['wins']} "
            f"losses={s['losses']} pnl=${s['total_pnl']:+.2f} "
            f"win_rate={s['win_rate_pct']:.2f}% -> {ledger_path}"
        )

        if args.validate:
            prod = loader.load_prod_entries(
                args.state_db, cfg["name"], args.start, args.end
            )
            vr = validate(all_rows, prod)
            rep_path = out_dir / (
                f"{cfg['name']}_{args.start}_{args.end}_validation.md"
            )
            write_validation_report(str(rep_path), cfg["name"],
                                    args.start, args.end, vr)
            print(
                f"[{cfg['name']}] validate: match_rate={vr['match_rate']:.2%} "
                f"matches={len(vr['matches'])} replay_only={len(vr['replay_only'])} "
                f"prod_only={len(vr['prod_only'])} -> {rep_path}"
            )
            if vr["match_rate"] < 0.95:
                overall_exit = 1

    # v5.5.0 \u2014 dashboard-shape JSON export. Emit even when no configs
    # have trades so downstream consumers can detect "ran but empty".
    if args.export_equity:
        ep = write_equity_export(args.export_equity, rows_by_cfg)
        print(f"[export-equity] -> {ep}")
    return overall_exit


if __name__ == "__main__":
    sys.exit(main())
