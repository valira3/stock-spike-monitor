"""v7.2.1 \u2014 Extended-hours \u2192 RTH ticker promotion.

When PMR or PMC fires a signal with conv >= EW_RTH_PROMOTION_MIN_CONV (default
0.30), promote that ticker into the next applicable RTH session's universe for
exactly one trading day:

    PMR fires (premarket cycle) \u2192 promote into TODAY's RTH session.
    PMC fires (afterhours cycle) \u2192 promote into NEXT trading day's RTH session.

Persistence: a single JSON sidecar at /data/earnings_watcher/rth_promotions.json
with shape:

    {
        "2026-05-07": {
            "tickers": ["DDOG", "COIN"],
            "sources": {"DDOG": "pmr", "COIN": "pmr"},
            "convs":   {"DDOG": 0.41,  "COIN": 0.55},
            "added_at_utc": ["2026-05-07T12:14:33+00:00", ...]
        },
        "2026-05-08": {...}
    }

trade_genius._init_tickers reads today's entry on RTH boot and merges it into
the universe. Old date entries are pruned to keep the file small.

This module is intentionally dependency-free and best-effort: any IO error is
logged and swallowed so EW signal flow is never blocked by promotion.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Conviction threshold to promote. Default 0.30 (loose) per Val 2026\u201105\u201107.
RTH_PROMOTION_MIN_CONV: float = _f("EW_RTH_PROMOTION_MIN_CONV", 0.30)

# Sidecar path. Override via EW_RTH_PROMOTION_PATH for tests.
DEFAULT_PROMOTION_PATH = Path(
    os.environ.get(
        "EW_RTH_PROMOTION_PATH",
        "/data/earnings_watcher/rth_promotions.json",
    )
)

# Retention: keep entries for this many days behind today. Anything older is
# pruned on every write so the file never grows unbounded.
RETENTION_DAYS = 7


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _today_iso(now_utc: Optional[datetime] = None) -> str:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    return now_utc.strftime("%Y-%m-%d")


def _next_trading_day_iso(now_utc: Optional[datetime] = None) -> str:
    """Next weekday (Mon\u2013Fri) after now_utc.

    For PMC fires we want the NEXT RTH session. PMC runs after-hours (16:00\u201320:00 ET);
    if now is Friday after-hours we promote into Monday. We do not consult a
    holiday calendar here \u2014 a holiday-day promotion just expires unused, which
    is the safer failure mode than skipping promotions.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    nxt = now_utc + timedelta(days=1)
    while nxt.weekday() >= 5:  # 5=Sat, 6=Sun
        nxt += timedelta(days=1)
    return nxt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _resolve_path(path: Optional[Path]) -> Path:
    return path if path is not None else DEFAULT_PROMOTION_PATH


def _safe_load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as exc:
        logger.warning(
            "[EW-RTH-PROMOTION] failed to load %s: %s \u2014 starting fresh",
            path, exc,
        )
        return {}


def _safe_save(path: Path, data: Dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.warning("[EW-RTH-PROMOTION] failed to save %s: %s", path, exc)
        return False


def _prune(data: Dict[str, Any], today_iso: str) -> Dict[str, Any]:
    """Drop entries older than RETENTION_DAYS days behind today_iso."""
    try:
        today = datetime.strptime(today_iso, "%Y-%m-%d").date()
    except ValueError:
        return data
    cutoff = today - timedelta(days=RETENTION_DAYS)
    return {
        d: v for d, v in data.items()
        if _date_or_none(d) is None or _date_or_none(d) >= cutoff
    }


def _date_or_none(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def maybe_promote(
    intent: Dict[str, Any],
    *,
    path: Optional[Path] = None,
    now_utc: Optional[datetime] = None,
    min_conv: Optional[float] = None,
) -> Optional[str]:
    """Inspect a fired EW intent and, if it qualifies, promote the ticker into
    a future RTH session.

    Returns the target date string ("YYYY-MM-DD") if promotion happened,
    otherwise None.

    PMR (strategy="pmr") promotes into TODAY's RTH.
    PMC (strategy="pmc") promotes into the next trading day's RTH.
    Other strategies (DMI, future) are ignored \u2014 promotion is opt-in by
    strategy attribute.
    """
    if not intent:
        return None
    strategy = (intent.get("strategy") or "").lower()
    if strategy not in ("pmr", "pmc"):
        return None

    threshold = RTH_PROMOTION_MIN_CONV if min_conv is None else float(min_conv)
    conv = float(intent.get("conv", 0.0) or 0.0)
    if conv < threshold:
        logger.debug(
            "[EW-RTH-PROMOTION] skip ticker=%s strategy=%s conv=%.4f < %.4f",
            intent.get("ticker"), strategy, conv, threshold,
        )
        return None

    ticker = (intent.get("ticker") or "").upper().strip()
    if not ticker:
        return None

    if strategy == "pmr":
        target = _today_iso(now_utc)
    else:
        target = _next_trading_day_iso(now_utc)

    p = _resolve_path(path)
    data = _safe_load(p)
    today_iso = _today_iso(now_utc)
    data = _prune(data, today_iso)

    entry = data.get(target)
    if entry is None or not isinstance(entry, dict):
        entry = {
            "tickers": [],
            "sources": {},
            "convs": {},
            "added_at_utc": [],
        }
    tickers: List[str] = list(entry.get("tickers") or [])
    sources: Dict[str, str] = dict(entry.get("sources") or {})
    convs: Dict[str, float] = dict(entry.get("convs") or {})
    added: List[str] = list(entry.get("added_at_utc") or [])

    if ticker not in tickers:
        tickers.append(ticker)
    # Always overwrite with the latest source + conv so a higher-conv re-fire
    # is reflected; promotion is "latest fire wins" for that ticker/day.
    sources[ticker] = strategy
    convs[ticker] = round(conv, 4)
    stamp = (now_utc or datetime.now(timezone.utc)).isoformat()
    added.append(f"{ticker}:{stamp}")

    data[target] = {
        "tickers": tickers,
        "sources": sources,
        "convs": convs,
        "added_at_utc": added,
    }
    _safe_save(p, data)
    logger.info(
        "[EW-RTH-PROMOTION] promote ticker=%s strategy=%s conv=%.4f target_date=%s total_for_day=%d",
        ticker, strategy, conv, target, len(tickers),
    )
    return target


def get_promotions_for(
    date_iso: str,
    *,
    path: Optional[Path] = None,
) -> List[str]:
    """Return list of tickers promoted into RTH for the given date. Empty if
    no entry. Order is insertion order (first promoted first)."""
    p = _resolve_path(path)
    data = _safe_load(p)
    entry = data.get(date_iso)
    if not isinstance(entry, dict):
        return []
    out = entry.get("tickers")
    if not isinstance(out, list):
        return []
    return [t for t in out if isinstance(t, str) and t]


def clear_for_date(
    date_iso: str,
    *,
    path: Optional[Path] = None,
) -> bool:
    """Drop the entry for a given date. Used by ops/tests."""
    p = _resolve_path(path)
    data = _safe_load(p)
    if date_iso in data:
        del data[date_iso]
        return _safe_save(p, data)
    return True
