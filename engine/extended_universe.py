# -*- coding: utf-8 -*-
"""v7.1.0 \u2014 Dynamic extended-hours universe overlay.

During pre-market (03:00\u201308:30 CT) and post-market (15:00\u201319:00 CT) sessions,
the bot scans an expanded ticker universe consisting of:

    1. The always-on production core (TICKERS_DEFAULT, 12 megacaps).
    2. An earnings-driven overlay: today's BMO + AMC reporters from the FMP
       earnings calendar, filtered to market_cap >= $10B and avg_vol >= 100K
       (reusing earnings_watcher.data_sources.get_today_earnings_universe).

RTH behavior is unchanged \u2014 the scan loop continues to iterate
``trade_genius.TRADE_TICKERS`` exactly as before.

Design constraints honored:

  * MUST NOT mutate ``TICKERS`` / ``TRADE_TICKERS`` \u2014 those are persisted to
    /data/tickers.json by UNIVERSE_GUARD and would leak into RTH cycles,
    dashboard, telegram, and broker lifecycle.
  * MUST NOT import from trade_genius or eye_of_tiger at module load
    (avoid the circular-import landmine that stung v6.x). Lazy imports
    inside the helpers.
  * MUST be feature-flagged via ``EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED``
    (default false). When false, returns plain ``TRADE_TICKERS``.
  * MUST tolerate every external-call failure mode \u2014 missing FMP key,
    Alpaca timeout, malformed cache \u2014 by falling back to the prod core.

Author: val.  v7.1.0.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache (per-process, refreshed once per UTC day or on TTL miss)
# ---------------------------------------------------------------------------

# Cache TTL: 1 hour. The scan loop fires every SCAN_INTERVAL (~10s) so we
# do NOT want a fresh FMP call every cycle. The earnings calendar shifts
# at most twice per day (pre-market open, post-market open) so an hourly
# refresh is plenty.
_CACHE_TTL_SEC = 3600

_cache_lock = threading.Lock()
_cache_universe: Optional[List[str]] = None
_cache_built_at: float = 0.0
_cache_built_for_date: str = ""


def _flag_enabled() -> bool:
    """Honor the EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED env flag.

    Default off so this is a no-op deploy until Val flips the variable in
    Railway. Accepts ``1`` / ``true`` / ``yes`` (case-insensitive).
    """
    raw = os.getenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _max_overlay_size() -> int:
    """Cap how many earnings tickers we add on top of the prod core.

    Default 18 \u2014 keeps total universe at ~30 (12 core + up to 18 overlay),
    matching ``market_brief.MAX_PREMARKET_TICKERS = 30`` and well under the
    ``trade_genius.TICKERS_MAX = 40`` sanity cap.
    """
    try:
        return int(os.getenv("EXTENDED_HOURS_OVERLAY_MAX", "18"))
    except ValueError:
        return 18


def _today_iso() -> str:
    """UTC YYYY-MM-DD. Earnings calendar uses UTC dates."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _prod_core() -> List[str]:
    """Return the always-on prod core (TICKERS_DEFAULT). Lazy import."""
    try:
        import trade_genius as tg
        return list(getattr(tg, "TRADE_TICKERS", []) or [])
    except Exception as exc:
        logger.error("[V710-OVERLAY] prod core import failed: %s", exc)
        return []


def _fetch_earnings_overlay(date_iso: str) -> List[str]:
    """Pull today's BMO + AMC reporters from earnings_watcher.

    Returns a deduped list, prod-core tickers stripped (they're already
    in the universe), capped at ``_max_overlay_size()``.

    On any error returns []; caller falls back to prod core only.
    """
    try:
        from earnings_watcher.data_sources import get_today_earnings_universe
    except Exception as exc:
        logger.warning("[V710-OVERLAY] earnings_watcher import failed: %s", exc)
        return []

    try:
        bmo, amc = get_today_earnings_universe(date_iso)
    except Exception as exc:
        logger.warning("[V710-OVERLAY] get_today_earnings_universe failed: %s", exc)
        return []

    core = set(t.upper() for t in _prod_core())
    seen: set = set(core)
    overlay: List[str] = []
    cap = _max_overlay_size()

    # BMO first (more relevant for pre-market scans), then AMC fill-in.
    for ticker in list(bmo or []) + list(amc or []):
        sym = (ticker or "").strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        overlay.append(sym)
        if len(overlay) >= cap:
            break

    logger.info(
        "[V710-OVERLAY] date=%s bmo=%d amc=%d overlay=%d core=%d",
        date_iso, len(bmo or []), len(amc or []), len(overlay), len(core),
    )
    return overlay


def _build_universe() -> List[str]:
    """Compose prod_core + earnings_overlay. Always returns a non-empty
    list as long as prod core resolves; overlay is best-effort."""
    core = _prod_core()
    overlay = _fetch_earnings_overlay(_today_iso())
    # Order: core first (so cycle-budget-bounded scans never starve them)
    # then overlay tickers in BMO-then-AMC order.
    return list(core) + list(overlay)


def get_extended_hours_universe() -> List[str]:
    """Return the effective ticker universe for an extended-hours scan cycle.

    Cached for ``_CACHE_TTL_SEC`` (default 1h). Cache key includes the UTC
    date so a Wed\u2192Thu rollover at 23:00 ET correctly invalidates.

    Falls back to ``TRADE_TICKERS`` on any error \u2014 the bot keeps trading
    the prod core even if FMP / Alpaca are down.
    """
    global _cache_universe, _cache_built_at, _cache_built_for_date

    if not _flag_enabled():
        return _prod_core()

    today = _today_iso()
    now = time.time()

    with _cache_lock:
        cache_fresh = (
            _cache_universe is not None
            and _cache_built_for_date == today
            and (now - _cache_built_at) < _CACHE_TTL_SEC
        )
        if cache_fresh:
            return list(_cache_universe or [])

        try:
            universe = _build_universe()
            if not universe:
                # Defensive: never return empty.
                logger.warning("[V710-OVERLAY] _build_universe returned empty; falling back")
                universe = _prod_core()
            _cache_universe = list(universe)
            _cache_built_at = now
            _cache_built_for_date = today
        except Exception as exc:
            logger.error("[V710-OVERLAY] _build_universe crashed: %s", exc, exc_info=True)
            _cache_universe = None
            return _prod_core()

        return list(_cache_universe)


def effective_scan_tickers(session: str) -> List[str]:
    """Top-level helper called by engine.scan.

    Parameters
    ----------
    session : str
        One of 'rth' | 'extended' | 'off' as returned by
        ``trade_genius._market_session()``.

    Returns
    -------
    list of tickers to iterate this scan cycle.

    Contract
    --------
    * RTH session \u2014 always returns ``TRADE_TICKERS`` (no overlay; production
      RTH path is the proven hot path and we do not perturb it).
    * Extended session \u2014 returns ``TRADE_TICKERS`` plus today's earnings
      overlay (capped, deduped) IF the feature flag is on; else
      ``TRADE_TICKERS``.
    * Off session \u2014 returns ``TRADE_TICKERS`` (caller is expected to skip
      anyway; this is just defensive).
    """
    if session == "extended":
        return get_extended_hours_universe()
    return _prod_core()


def reset_cache_for_test() -> None:
    """Test hook \u2014 wipes module-level cache between unit tests."""
    global _cache_universe, _cache_built_at, _cache_built_for_date
    with _cache_lock:
        _cache_universe = None
        _cache_built_at = 0.0
        _cache_built_for_date = ""
