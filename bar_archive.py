"""v5.1.2 \u2014 1m bar JSONL persistence to /data/bars/YYYY-MM-DD/{TICKER}.jsonl.

Append-only, atomic-per-line on Linux ext4 (the kernel guarantees writes
< PIPE_BUF on a file opened in `a` mode are atomic). Files are created
lazily on first write.

Public API:
    write_bar(ticker, bar_dict, base_dir="/data/bars", today=None)
    cleanup_old_dirs(base_dir="/data/bars", retain_days=90, today=None)

The `today` parameter exists so smoke tests can stub the date without
freezing system time.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("trade_genius.bar_archive")

DEFAULT_BASE_DIR = "/data/bars"
DEFAULT_RETAIN_DAYS = 90

# Schema fields a bar SHOULD carry. Missing fields are written as null.
BAR_SCHEMA_FIELDS = (
    "ts", "et_bucket",
    "open", "high", "low", "close",
    "iex_volume", "iex_sip_ratio_used",
    "bid", "ask", "last_trade_price",
)


def _today_str(today: date | None = None) -> str:
    if today is None:
        today = datetime.utcnow().date()
    return today.strftime("%Y-%m-%d")


def _normalise_bar(bar: dict) -> dict:
    """Project the bar onto the canonical schema. Unknown keys are
    dropped; missing keys are filled with None."""
    out: dict = {}
    for k in BAR_SCHEMA_FIELDS:
        out[k] = bar.get(k) if bar is not None else None
    return out


def write_bar(
    ticker: str,
    bar: dict,
    *,
    base_dir: str | os.PathLike = DEFAULT_BASE_DIR,
    today: date | None = None,
) -> str | None:
    """Append a single JSONL line for `ticker` under
    `{base_dir}/{YYYY-MM-DD}/{TICKER}.jsonl`.

    Returns the absolute file path on success, or None on failure
    (failure is logged at warning level, never raised \u2014 archival
    must never break the trading loop).
    """
    if not ticker:
        return None
    try:
        sym = str(ticker).strip().upper()
        if not sym:
            return None
        day = _today_str(today)
        dir_path = Path(base_dir) / day
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{sym}.jsonl"
        line = json.dumps(_normalise_bar(bar), separators=(",", ":")) + "\n"
        # `a` mode: each write() of < PIPE_BUF (4096) is atomic on Linux.
        # Our lines are ~150 bytes; well under the limit.
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return str(file_path)
    except Exception as e:
        logger.warning("[BAR-ARCHIVE] write_bar %s failed: %s", ticker, e)
        return None


def cleanup_old_dirs(
    *,
    base_dir: str | os.PathLike = DEFAULT_BASE_DIR,
    retain_days: int = DEFAULT_RETAIN_DAYS,
    today: date | None = None,
) -> list[str]:
    """Delete dated directories older than `retain_days`. Returns list
    of deleted directory paths. Failure-tolerant per directory.
    """
    deleted: list[str] = []
    try:
        root = Path(base_dir)
        if not root.exists():
            return deleted
        cutoff = (today or datetime.utcnow().date()) - timedelta(days=retain_days)
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            try:
                d = datetime.strptime(child.name, "%Y-%m-%d").date()
            except ValueError:
                continue  # not a dated dir \u2014 skip
            if d < cutoff:
                try:
                    for f in child.iterdir():
                        try:
                            f.unlink()
                        except OSError:
                            pass
                    child.rmdir()
                    deleted.append(str(child))
                except OSError as e:
                    logger.warning("[BAR-ARCHIVE] cleanup %s failed: %s",
                                   child, e)
    except Exception as e:
        logger.warning("[BAR-ARCHIVE] cleanup_old_dirs failed: %s", e)
    return deleted
