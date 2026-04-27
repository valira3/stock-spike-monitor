"""CSV ledger writer for the v5.4.0 backtest replay.

Output format (mirrors trade_genius's paper-side bookkeeping):

    # summary: trades=N wins=W losses=L total_pnl=$X win_rate=Y%
    ticker,side,entry_ts,entry_price,exit_ts,exit_price,qty,pnl_dollars,pnl_pct,exit_reason
    AAPL,BUY,2026-04-20T13:31:00Z,180.10,2026-04-20T15:02:00Z,182.50,100,240.00,1.33,trail_stop
    ...

Columns are the same regardless of long/short: pnl_dollars is
already direction-aware (computed by the replay engine), and side
records BUY (long) or SHORT (short).
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable

LEDGER_COLUMNS = (
    "ticker", "side",
    "entry_ts", "entry_price",
    "exit_ts", "exit_price",
    "qty", "pnl_dollars", "pnl_pct", "exit_reason",
)


def summarize(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    n = len(rows)
    wins = sum(1 for r in rows if (r.get("pnl_dollars") or 0) > 0)
    losses = sum(1 for r in rows if (r.get("pnl_dollars") or 0) < 0)
    total = round(sum((r.get("pnl_dollars") or 0.0) for r in rows), 2)
    wr = round(100.0 * wins / n, 2) if n else 0.0
    return {
        "trades": n, "wins": wins, "losses": losses,
        "total_pnl": total, "win_rate_pct": wr,
    }


def write_ledger(out_path: str | os.PathLike, rows: list[dict]) -> str:
    """Write the ledger CSV with a leading summary comment line.
    Returns the absolute path written.
    """
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    s = summarize(rows)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            f"# summary: trades={s['trades']} wins={s['wins']} "
            f"losses={s['losses']} total_pnl=${s['total_pnl']:+.2f} "
            f"win_rate={s['win_rate_pct']:.2f}%\n"
        )
        w = csv.DictWriter(fh, fieldnames=list(LEDGER_COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in LEDGER_COLUMNS})
    return str(p.resolve())
