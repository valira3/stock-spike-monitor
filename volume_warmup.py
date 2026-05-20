"""v7.2.1 \u2014 Backfill the bar archive with 55 trading days of 1-minute bars
for a newly-added (or RTH-promoted) ticker so the volume_bucket gate can
evaluate it on the very next scan instead of falling through cold-start
passthrough.

Public API:
    warmup_ticker(ticker, *, lookback_days=55, base_dir=None) -> dict

Returns a dict {ticker, days_seeded, bars_written, errors[], elapsed_s}.

Side effects:
  1. Pulls Alpaca SIP 1-minute bars for the lookback window.
  2. Writes them via bar_archive.write_bar into the per-day jsonl files.
  3. Triggers a volume baseline refresh so the new history is picked up
     by VolumeBucketBaseline immediately.

This module never raises \u2014 fetch / write / refresh failures are logged
and accumulated in errors[] so callers can decide whether to surface
them. It is safe to call from add_ticker, RTH boot, or a one-off Telegram
command.

Requires VAL_ALPACA_PAPER_KEY / VAL_ALPACA_PAPER_SECRET in env. Reuses
earnings_watcher.data_sources.fetch_minute_bars to avoid duplicating the
SIP/IEX fallback logic.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Mirror volume_bucket's lookback default. Matched at call time so callers
# do not have to import volume_bucket.
_DEFAULT_LOOKBACK = 55

# RTH window in UTC. Pre/post-market bars are written too \u2014 the volume
# baseline only reads RTH minutes (et_bucket field), but writing the full
# day means the same archive serves EW lookups without a second fetch.
# Alpaca returns whatever bars trade in the requested window, so we ask
# for 04:00 ET \u2192 20:00 ET = 08:00 UTC \u2192 00:00 UTC (next day) under EDT,
# 09:00 UTC \u2192 01:00 UTC under EST. We just send the broad UTC window
# and let Alpaca filter.
_DAY_START_UTC_HR = 8   # 04:00 ET in EDT (close enough; EST is 09:00 UTC)
_DAY_END_UTC_HR = 24    # 20:00 ET in EDT


# ---------------------------------------------------------------------------
# Date helpers (local copies of volume_bucket logic to keep this module
# import-light \u2014 calling volume_bucket._trading_days_back works too but
# couples us to a private function).
# ---------------------------------------------------------------------------

def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


# Cached holiday list \u2014 imported lazily from volume_bucket if available
# so the two stay in sync; fall back to a tiny stub set for safety.
def _is_market_holiday(d: date) -> bool:
    try:
        import volume_bucket as _vb  # type: ignore
        return _vb._is_us_market_holiday(d)
    except Exception:
        # Minimal stub: no holidays known. Worst case we fetch a no-trade day,
        # write zero bars for it, no harm.
        return False


def _trading_days_back(end: date, n: int) -> List[date]:
    out: List[date] = []
    d = end - timedelta(days=1)
    while len(out) < n:
        if not _is_weekend(d) and not _is_market_holiday(d):
            out.append(d)
        d -= timedelta(days=1)
        # Safety brake \u2014 should never need >2x calendar days for n trading days.
        if (end - d).days > n * 2 + 14:
            break
    out.sort()
    return out


# ---------------------------------------------------------------------------
# Bar normalisation \u2014 EW returns {timestamp, open, high, low, close, volume}.
# bar_archive expects {ts, open, high, low, close, total_volume, et_bucket, ...}.
# ---------------------------------------------------------------------------

def _et_bucket_for_ts(ts_iso: str) -> Optional[str]:
    """Convert a UTC ISO timestamp string to an HHMM ET bucket string.

    Uses zoneinfo for DST correctness. Returns None if parsing fails.
    """
    try:
        # Python 3.9+; trade_genius runs on 3.11.
        from zoneinfo import ZoneInfo  # type: ignore
        et = ZoneInfo("America/New_York")
    except Exception:
        return None
    try:
        # Accept "...+00:00" or "...Z" forms.
        s = ts_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_et = dt.astimezone(et)
        return f"{dt_et.hour:02d}{dt_et.minute:02d}"
    except Exception:
        return None


def _ew_bar_to_archive(bar: Dict[str, Any]) -> Dict[str, Any]:
    ts = bar.get("timestamp", "")
    return {
        "ts": ts,
        "et_bucket": _et_bucket_for_ts(ts),
        "open": bar.get("open"),
        "high": bar.get("high"),
        "low": bar.get("low"),
        "close": bar.get("close"),
        "total_volume": bar.get("volume"),
        "iex_volume": None,
        "iex_sip_ratio_used": None,
        "bid": None,
        "ask": None,
        "last_trade_price": None,
        "trade_count": None,
        "bar_vwap": None,
        "feed_source": "sip",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def warmup_ticker(
    ticker: str,
    *,
    lookback_days: int = _DEFAULT_LOOKBACK,
    base_dir: Optional[str] = None,
    refresh_baseline: bool = True,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Backfill `lookback_days` trading days of 1m bars for `ticker`.

    Idempotent at the directory level: if a per-day file already exists for
    this ticker we skip that day (we never duplicate bars). The volume
    baseline is then refreshed once at the end so the next gate check
    sees the new history.
    """
    t0 = time.time()
    sym = (ticker or "").strip().upper()
    out: Dict[str, Any] = {
        "ticker": sym,
        "days_seeded": 0,
        "days_skipped_existing": 0,
        "bars_written": 0,
        "errors": [],
        "elapsed_s": 0.0,
    }
    if not sym:
        out["errors"].append("empty_ticker")
        out["elapsed_s"] = round(time.time() - t0, 2)
        return out

    # Lazy imports so module load is cheap if warmup is never invoked.
    try:
        import bar_archive  # type: ignore
    except Exception as exc:
        out["errors"].append(f"bar_archive_import:{exc}")
        out["elapsed_s"] = round(time.time() - t0, 2)
        return out

    try:
        from earnings_watcher.data_sources import fetch_minute_bars  # type: ignore
    except Exception as exc:
        out["errors"].append(f"data_sources_import:{exc}")
        out["elapsed_s"] = round(time.time() - t0, 2)
        return out

    archive_base = base_dir or bar_archive.DEFAULT_BASE_DIR
    end_date = today or datetime.now(timezone.utc).date()
    days = _trading_days_back(end_date, lookback_days)

    logger.info(
        "[VOL-WARMUP] start ticker=%s lookback=%d days_resolved=%d archive=%s",
        sym, lookback_days, len(days), archive_base,
    )

    for d in days:
        day_str = d.strftime("%Y-%m-%d")
        day_file = Path(archive_base) / day_str / f"{sym}.jsonl"
        if day_file.exists() and day_file.stat().st_size > 0:
            out["days_skipped_existing"] += 1
            continue

        start_utc = datetime(d.year, d.month, d.day, _DAY_START_UTC_HR, 0,
                             tzinfo=timezone.utc)
        end_utc = start_utc + timedelta(hours=_DAY_END_UTC_HR - _DAY_START_UTC_HR)
        try:
            bars = fetch_minute_bars(sym, start_utc, end_utc)
        except Exception as exc:
            out["errors"].append(f"fetch:{day_str}:{exc}")
            continue

        if not bars:
            # No bars for that day (illiquid / pre-IPO / holiday) \u2014 not an error.
            continue

        wrote = 0
        for b in bars:
            archive_bar = _ew_bar_to_archive(b)
            try:
                bar_archive.write_bar(sym, archive_bar, base_dir=archive_base, today=d)
                wrote += 1
            except Exception as exc:
                out["errors"].append(f"write:{day_str}:{exc}")
                break
        if wrote > 0:
            out["days_seeded"] += 1
            out["bars_written"] += wrote

    # v10.0.1 -- volume baseline refresh retired along with the rest of
    # the v5_10_1_integration surface; refresh_baseline arg ignored.
    if refresh_baseline:
        out["baseline_days_available"] = None

    out["elapsed_s"] = round(time.time() - t0, 2)
    logger.info(
        "[VOL-WARMUP] done ticker=%s seeded=%d skipped_existing=%d bars=%d errors=%d elapsed=%.2fs",
        sym, out["days_seeded"], out["days_skipped_existing"],
        out["bars_written"], len(out["errors"]), out["elapsed_s"],
    )
    return out
