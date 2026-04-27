"""Bar + state.db loaders for the v5.4.0 backtest CLI.

Bars are persisted by `bar_archive.write_bar` as one JSONL file per
ticker per UTC date under `<base_dir>/<YYYY-MM-DD>/<TICKER>.jsonl`.
Each line carries the canonical schema from `bar_archive.BAR_SCHEMA_FIELDS`.

state.db is the SQLite store managed by `persistence.py`. The
production source of truth for entered/exited positions in the v5.2.0+
era is the `shadow_positions` table, which records (config_name,
ticker, side, qty, entry_ts_utc, entry_price, exit_ts_utc,
exit_price, realized_pnl). We treat those rows as the prod records to
compare replay against.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable


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
    bars.sort(key=lambda b: (b.get("ts") or ""))
    return bars


def load_prod_entries(
    state_db: str | os.PathLike,
    config_name: str,
    start: str,
    end: str,
) -> list[dict]:
    """Return prod shadow_positions rows for `config_name` whose
    entry_ts_utc falls in [start 00:00 UTC, end 23:59:59 UTC].

    Each returned dict has: ticker, side, qty, entry_ts_utc,
    entry_price, exit_ts_utc, exit_price, realized_pnl.
    """
    p = Path(state_db)
    if not p.is_file():
        return []
    s_iso = f"{start}T00:00:00Z"
    e_iso = f"{end}T23:59:59Z"
    conn = sqlite3.connect(str(p))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT config_name, ticker, side, qty, entry_ts_utc, "
            "entry_price, exit_ts_utc, exit_price, realized_pnl "
            "FROM shadow_positions "
            "WHERE config_name = ? "
            "AND entry_ts_utc >= ? AND entry_ts_utc <= ? "
            "ORDER BY entry_ts_utc ASC",
            (config_name, s_iso, e_iso),
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
