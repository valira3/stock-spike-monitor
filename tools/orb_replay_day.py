"""tools.orb_replay_day -- replay archived 1m bars through the live runtime.

Reads /data/bars/YYYY-MM-DD/<TICKER>.jsonl files (the format produced by
bar_archive.py) and drives the LIVE orb.live_runtime end-to-end. Emits a
deterministic ledger of admissions + exits.

Use cases:
  1. Regression: snapshot today's ledger; after a v10 code change,
     replay yesterday and diff the ledger. Any change in admissions /
     exits that wasn't intentional surfaces as a diff.
  2. Probing: replay a specific archived day to study an edge case.
  3. Smoke: post-deploy run on the previous day to confirm the live
     code path still produces the same ledger.

Output (JSONL, one event per line):
  {"kind":"session_start", "date":"2026-05-09", "block_day":false}
  {"kind":"or_lock", "ticker":"AAPL", "or_high":..., "or_low":...}
  {"kind":"admit", "ticker":"AAPL", "side":"long", "shares":..., ...}
  {"kind":"exit",  "ticker":"AAPL", "reason":"target", "price":...}
  {"kind":"summary", "admits":N, "exits":N, "rejects":N}

Usage:
  python -m tools.orb_replay_day --date 2026-05-09 --tickers AAPL,MSFT
  python -m tools.orb_replay_day --date 2026-05-09 \\
      --base-dir /data/bars --out /tmp/ledger.jsonl

Constraints (per rule #7b -- no look-ahead):
  - Bars are sorted by `ts` ascending. Each bar is fed to the runtime
    before the runtime is asked any decisions about that bar.
  - 5-min closes are derived from the 1m bar stream: every 5th 1m
    bar's close acts as the 5m close + the next 1m bar's open is the
    fill. This matches engine/scan.py's wiring exactly.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from orb import live_runtime

logger = logging.getLogger(__name__)


OR_END_BUCKET_DEFAULT = 9 * 60 + 60
EOD_BUCKET = 15 * 60 + 55


@dataclass
class ReplayConfig:
    date_iso: str
    tickers: list[str]
    base_dir: str = "/data/bars"
    out_path: Optional[str] = None
    portfolio_id: str = "main"
    equity: float = 100_000.0
    vix_close_d1: Optional[float] = None
    or_minutes: int = 30


@dataclass
class _Event:
    kind: str
    payload: dict = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps({"kind": self.kind, **self.payload},
                          separators=(",", ":"))


# ----- Bar loader --------------------------------------------------


def _load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _et_bucket_from_bar(bar: dict) -> Optional[int]:
    """Extract the ET minute-of-day bucket from a bar.

    Prefers an explicit `et_bucket` field. If absent, parses `ts`
    (assumed to be a Unix epoch second or ISO string in UTC) into ET
    and computes the minute-of-day. Returns None if neither is usable.
    """
    et = bar.get("et_bucket")
    if isinstance(et, int):
        return et
    if isinstance(et, str) and ":" in et:
        # "HH:MM" or "HH:MM:SS"
        parts = et.split(":")
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            pass
    ts = bar.get("ts")
    if ts is None:
        return None
    try:
        from engine.timing import minutes_since_et_midnight
        if isinstance(ts, (int, float)):
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            return minutes_since_et_midnight(dt)
        if isinstance(ts, str):
            from datetime import datetime
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return minutes_since_et_midnight(dt)
    except Exception:
        return None
    return None


def _load_day_bars(cfg: ReplayConfig) -> dict[str, list[dict]]:
    """Returns {ticker: [bar, bar, ...]} sorted by `ts` ascending."""
    day_dir = Path(cfg.base_dir) / cfg.date_iso
    out: dict[str, list[dict]] = {}
    for tk in cfg.tickers:
        sym = tk.upper().strip()
        if not sym:
            continue
        path = day_dir / f"{sym}.jsonl"
        if not path.exists():
            logger.info("no archive for %s/%s", cfg.date_iso, sym)
            continue
        bars = _load_jsonl(path)
        bars.sort(key=lambda b: b.get("ts") or 0)
        out[sym] = bars
    return out


# ----- Replay core -------------------------------------------------


def _build_session_inputs(cfg: ReplayConfig,
                          per_ticker_bars: dict[str, list[dict]],
                          ) -> tuple[dict, dict]:
    """Pull (open_today, prev_close) from the bar stream itself when
    not supplied externally. open_today := first bar's open;
    prev_close := first bar's open as a degenerate fallback (we don't
    have prior session data here).
    """
    opens: dict[str, float] = {}
    pdc: dict[str, float] = {}
    for tk, bars in per_ticker_bars.items():
        if not bars:
            continue
        first = bars[0]
        opens[tk] = float(first.get("open") or first.get("close") or 0.0)
        pdc[tk] = opens[tk]  # degenerate
    return opens, pdc


def replay(cfg: ReplayConfig) -> list[_Event]:
    events: list[_Event] = []
    live_runtime._reset_for_testing()
    live_runtime.bootstrap()

    per_ticker = _load_day_bars(cfg)
    if not per_ticker:
        events.append(_Event("error",
                             {"reason": "no archives found",
                              "date": cfg.date_iso}))
        return events

    opens, pdc = _build_session_inputs(cfg, per_ticker)
    started = live_runtime.ensure_session_started(
        date_iso=cfg.date_iso,
        tickers=list(per_ticker.keys()),
        vix_close_d1=cfg.vix_close_d1,
        ticker_open_today=opens,
        ticker_prev_close=pdc,
        equity_per_portfolio={cfg.portfolio_id: cfg.equity},
    )
    snap = live_runtime.snapshot()
    events.append(_Event("session_start", {
        "date": cfg.date_iso,
        "started": bool(started),
        "block_day": snap.get("day_status", {}).get("block_day", False),
        "block_reason": snap.get("day_status", {}).get("block_reason", ""),
    }))

    # Merge all tickers' bars into one chronological stream
    merged: list[tuple[str, dict]] = []
    for tk, bars in per_ticker.items():
        for b in bars:
            merged.append((tk, b))
    merged.sort(key=lambda x: (x[1].get("ts") or 0, x[0]))

    # Track 5m close+next-open per ticker. Bucket math: bucket_min %
    # 5 == 4 means the bar at bucket_min is the LAST 1m of a 5m
    # window; the next bar's open is the fill price.
    pending_signals: dict[str, dict] = {}   # ticker -> last 5m close info
    open_tickets: dict[str, str] = {}       # ticker -> ticket_id

    admit_count = 0
    exit_count = 0
    reject_count = 0

    for tk, bar in merged:
        bucket = _et_bucket_from_bar(bar)
        if bucket is None:
            continue
        high = float(bar.get("high") or 0.0)
        low = float(bar.get("low") or 0.0)
        bopen = float(bar.get("open") or 0.0)
        close = float(bar.get("close") or 0.0)
        vol = float(bar.get("total_volume")
                    or bar.get("iex_volume") or 0.0)

        # 1. Always feed the bar (OrWindow only takes OR-window bars)
        live_runtime.feed_bar(
            ticker=tk,
            bar_high=high, bar_low=low, bar_open=bopen,
            bar_close=close, bar_volume=vol,
            bar_bucket_min=bucket,
        )

        # 2. If this completes a 5m window past the OR, attempt entry
        #    on the NEXT bar (next bar's open is the fill).
        or_end = 9 * 60 + 30 + cfg.or_minutes
        if (
            bucket >= or_end
            and bucket % 5 == 4
            and tk not in open_tickets
        ):
            pending_signals[tk] = {
                "five_min_close": close,
                "five_min_close_iso": str(bar.get("ts", "")),
                "bucket": bucket,
            }
        elif tk in pending_signals and tk not in open_tickets:
            sig = pending_signals.pop(tk)
            # Use this bar's open as the fill price
            for side in ("long", "short"):
                ent = live_runtime.check_entry(
                    portfolio_id=cfg.portfolio_id,
                    ticker=tk, side=side,
                    five_min_close=sig["five_min_close"],
                    next_open=bopen,
                    equity=cfg.equity,
                    signal_iso=sig["five_min_close_iso"],
                )
                if ent.ok:
                    admit_count += 1
                    open_tickets[tk] = ent.ticket_id
                    events.append(_Event("admit", {
                        "ticker": tk, "side": side, "shares": ent.shares,
                        "price": ent.price, "stop": ent.stop,
                        "target": ent.target,
                        "ticket_id": ent.ticket_id,
                        "bucket": bucket,
                    }))
                    break
                if ent.reason_no and ent.reason_no != "no_signal":
                    reject_count += 1
                    events.append(_Event("reject", {
                        "ticker": tk, "side": side,
                        "reason": ent.reason_no, "bucket": bucket,
                    }))

        # 3. Evaluate exit on any open position for this ticker
        if tk in open_tickets:
            ex = live_runtime.check_exit(
                portfolio_id=cfg.portfolio_id,
                ticker=tk, ticket_id=open_tickets[tk],
                bar_high=high, bar_low=low, bar_close=close,
                bar_bucket_min=bucket,
            )
            if ex.exit:
                exit_count += 1
                events.append(_Event("exit", {
                    "ticker": tk, "reason": ex.reason, "price": ex.price,
                    "ticket_id": open_tickets[tk], "bucket": bucket,
                }))
                open_tickets.pop(tk, None)

    events.append(_Event("summary", {
        "admits": admit_count, "exits": exit_count, "rejects": reject_count,
    }))
    return events


def write_ledger(events: list[_Event], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(ev.to_jsonl() + "\n")


# ----- CLI ---------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orb_replay_day",
        description="Replay an archived bar day through the live "
                    "v10 ORB runtime.",
    )
    parser.add_argument("--date", required=True,
                        help="Session date in YYYY-MM-DD.")
    parser.add_argument("--tickers", required=True,
                        help="Comma-separated tickers, e.g. AAPL,MSFT.")
    parser.add_argument("--base-dir", default="/data/bars",
                        help="Bar archive base dir.")
    parser.add_argument("--out", default=None,
                        help="Path to write ledger JSONL. If omitted, "
                             "ledger is printed to stdout.")
    parser.add_argument("--vix-d1", type=float, default=None,
                        help="Prior-day VIX close. If omitted, VIX gate "
                             "fail-closed default applies.")
    parser.add_argument("--equity", type=float, default=100_000.0,
                        help="Starting equity for the portfolio.")
    parser.add_argument("--portfolio", default="main",
                        help="Portfolio id (default: main).")
    args = parser.parse_args(argv)

    cfg = ReplayConfig(
        date_iso=args.date,
        tickers=[t.strip().upper() for t in args.tickers.split(",")
                 if t.strip()],
        base_dir=args.base_dir,
        out_path=args.out,
        portfolio_id=args.portfolio,
        equity=args.equity,
        vix_close_d1=args.vix_d1,
    )
    events = replay(cfg)
    if args.out:
        write_ledger(events, args.out)
        print(f"wrote {len(events)} events to {args.out}")
    else:
        for ev in events:
            print(ev.to_jsonl())
    return 0


if __name__ == "__main__":
    sys.exit(main())
