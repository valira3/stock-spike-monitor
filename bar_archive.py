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

# v6.9.4 -- resolve default from BAR_ARCHIVE_BASE > TG_DATA_ROOT > /data.
# Callers that pass base_dir= explicitly are unaffected.
_TG_DATA_ROOT = os.environ.get("TG_DATA_ROOT", "/data")
DEFAULT_BASE_DIR = os.environ.get("BAR_ARCHIVE_BASE", _TG_DATA_ROOT + "/bars")
# v6.14.2 \u2014 bar archive retention is env-overridable. The 55-day
# volume_bucket lookback needs ~80 calendar days of safety margin, so
# the legacy 90-day default sometimes leaves us with only 53 trading
# days available when the window grazes a holiday cluster (e.g.
# Presidents Day + March break). Set BAR_ARCHIVE_RETAIN_DAYS to widen
# the window. Cleanup runs once per day at EOD via broker.lifecycle,
# so a higher value just costs slightly more disk \u2014 the seeded
# archive is roughly 1.3 MB / trading day across the 12 prod tickers.
try:
    DEFAULT_RETAIN_DAYS = int(os.environ.get("BAR_ARCHIVE_RETAIN_DAYS") or 90)
except ValueError:
    DEFAULT_RETAIN_DAYS = 90

# Schema fields a bar SHOULD carry. Missing fields are written as null.
# v5.31.0 \u2014 added trade_count + bar_vwap (Alpaca-only fields; Yahoo-
# sourced bars carry None for both, which the schema accepts).
BAR_SCHEMA_FIELDS = (
    "ts",
    "et_bucket",
    "open",
    "high",
    "low",
    "close",
    # v6.14.0 \u2014 SIP-aggregated total volume from the Alpaca bar feed.
    # Was missing from the schema in v6.5.0\u20136.13.x; ingest wrote
    # iex_volume only and that field carried 0 on the SIP path. The new
    # field is what volume_bucket.py reads first; iex_volume is retained
    # so that legacy bars on disk still parse cleanly. See issue #354.
    "total_volume",
    "iex_volume",
    "iex_sip_ratio_used",
    "bid",
    "ask",
    "last_trade_price",
    "trade_count",
    "bar_vwap",
    # v6.5.0 M-4 — feed provenance tag ("sip" or "iex").
    # Defaults to None for legacy bars written before the SIP migration.
    # The _normalise_bar helper already handles missing keys via .get().
    "feed_source",
)

DAILY_BAR_SCHEMA_FIELDS = (
    "date",
    "ticker",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "or_high",
    "or_low",
    "pdc",
    "sess_hod",
    "sess_lod",
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
    except PermissionError as e:
        # v6.9.4 -- suppress to DEBUG in smoke-test mode to avoid 54k warning flood.
        if os.environ.get("SSM_SMOKE_TEST") == "1":
            logger.debug("[BAR-ARCHIVE] write_bar %s failed: %s", ticker, e)
        else:
            logger.warning("[BAR-ARCHIVE] write_bar %s failed: %s", ticker, e)
        return None
    except Exception as e:
        logger.warning("[BAR-ARCHIVE] write_bar %s failed: %s", ticker, e)
        return None


DEFAULT_DAILY_BAR_DIR = os.environ.get("BAR_ARCHIVE_BASE", _TG_DATA_ROOT + "/bars") + "/daily"


def _normalise_daily_bar(bar: dict) -> dict:
    """v5.31.0 \u2014 project a daily bar onto DAILY_BAR_SCHEMA_FIELDS.

    Unknown keys are dropped; missing keys default to None.
    """
    out: dict = {}
    for k in DAILY_BAR_SCHEMA_FIELDS:
        out[k] = bar.get(k) if bar is not None else None
    return out


def write_daily_bar(
    ticker: str,
    bar: dict,
    *,
    base_dir: str | os.PathLike = DEFAULT_DAILY_BAR_DIR,
) -> str | None:
    """v5.31.0 \u2014 append a daily OHLC line to
    ``{base_dir}/{TICKER}.jsonl`` (no per-date subdir; cross-day flat
    archive). Returns the absolute path on success, or None on failure
    (failure-tolerant: never raises into the caller).

    Sister of :func:`write_bar`. Used by ``broker/lifecycle.py:eod_close``
    to capture one row per trade-ticker per session at end-of-day.
    """
    if not ticker:
        return None
    try:
        sym = str(ticker).strip().upper()
        if not sym:
            return None
        dir_path = Path(base_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{sym}.jsonl"
        line = json.dumps(_normalise_daily_bar(bar), separators=(",", ":")) + "\n"
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return str(file_path)
    except PermissionError as e:
        # v6.9.4 -- suppress to DEBUG in smoke-test mode.
        if os.environ.get("SSM_SMOKE_TEST") == "1":
            logger.debug("[BAR-ARCHIVE] write_daily_bar %s failed: %s", ticker, e)
        else:
            logger.warning("[BAR-ARCHIVE] write_daily_bar %s failed: %s", ticker, e)
        return None
    except Exception as e:
        logger.warning("[BAR-ARCHIVE] write_daily_bar %s failed: %s", ticker, e)
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
                    logger.warning("[BAR-ARCHIVE] cleanup %s failed: %s", child, e)
    except Exception as e:
        logger.warning("[BAR-ARCHIVE] cleanup_old_dirs failed: %s", e)
    return deleted
