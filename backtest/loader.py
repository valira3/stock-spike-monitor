"""Bar + trade-log loaders for the v5.14.0 backtest data layer.

Bars are persisted by `bar_archive.write_bar` as one JSONL file per
ticker per UTC date under `<base_dir>/<YYYY-MM-DD>/<TICKER>.jsonl`.
Each line carries the canonical schema from `bar_archive.BAR_SCHEMA_FIELDS`.

Closed-trade records live in the live trade log at `trade_log.jsonl`
(written by `trade_genius.trade_log_append` whenever a real position
closes). v5.14.0 retired the `shadow_positions` table; the prod source
of truth for "what entered / what exited" is now the trade log itself.

Open positions persisted across restarts live in the
`executor_positions` table in state.db (managed by `persistence.py`),
which we expose for any backtest harness that needs to know what the
live engine considers "currently held".
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


def daterange(start: str, end: str) -> list[str]:
    """Inclusive list of YYYY-MM-DD strings between start and end."""
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    if e < s:
        return []
    out: list[str] = []
    cur = s
    while cur <= e:
        out.append(cur.strftime("%Y-%m-%d"))
        cur = cur + timedelta(days=1)
    return out


def list_tickers_for_day(bars_dir: str | os.PathLike, day: str) -> list[str]:
    """Return uppercase ticker symbols that have a bars file on `day`."""
    p = Path(bars_dir) / day
    if not p.is_dir():
        return []
    out: list[str] = []
    for f in sorted(p.iterdir()):
        if f.is_file() and f.suffix == ".jsonl":
            out.append(f.stem.upper())
    return out


def load_bars(bars_dir: str | os.PathLike, day: str, ticker: str) -> list[dict]:
    """Load all 1m bars for `ticker` on `day`, sorted by timestamp.

    Missing files return []. Malformed lines are skipped (not raised) so
    a partially-corrupt archive doesn't kill the whole replay.
    """
    p = Path(bars_dir) / day / f"{ticker.upper()}.jsonl"
    if not p.is_file():
        return []
    bars: list[dict] = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                bars.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    bars.sort(key=lambda b: b.get("ts") or "")
    return bars


def load_prod_trades_from_log(
    trade_log_path: str | os.PathLike,
    start: str,
    end: str,
    portfolio: str | None = None,
) -> list[dict]:
    """Return prod trade-log rows whose `date` falls in [start, end].

    Each row carries the schema written by `trade_genius.trade_log_append`,
    which includes (at minimum): date, ticker, side, qty, entry_price,
    exit_price, entry_ts, exit_ts, realized_pnl, exit_reason, portfolio,
    entry_id. v5.14.0 replaced shadow_positions as the prod entry source.

    Args:
      trade_log_path: path to trade_log.jsonl
      start, end:     inclusive YYYY-MM-DD bounds
      portfolio:      optional "paper" or "tp" filter
    """
    p = Path(trade_log_path)
    if not p.is_file():
        return []
    rows: list[dict] = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = row.get("date", "")
            if d < start or d > end:
                continue
            if portfolio and row.get("portfolio") != portfolio:
                continue
            rows.append(row)
    rows.sort(key=lambda r: r.get("entry_ts") or r.get("date") or "")
    return rows


def load_open_executor_positions(
    state_db: str | os.PathLike,
    executor_name: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    """Return rows from executor_positions, the persisted open-position
    mirror managed by persistence.save_executor_position.

    Optional filters narrow by executor_name (e.g. "Val") and/or mode
    (e.g. "paper"). Returns [] if the database file is missing.
    """
    p = Path(state_db)
    if not p.is_file():
        return []
    where_parts: list[str] = []
    params: list[object] = []
    if executor_name is not None:
        where_parts.append("executor_name = ?")
        params.append(executor_name)
    if mode is not None:
        where_parts.append("mode = ?")
        params.append(mode)
    where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    conn = sqlite3.connect(str(p))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT executor_name, mode, ticker, side, qty, entry_price, "
            "entry_ts_utc, source, stop, trail, last_updated_utc "
            "FROM executor_positions" + where + " ORDER BY entry_ts_utc ASC",
            tuple(params),
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
