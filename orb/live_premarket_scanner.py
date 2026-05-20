"""Live wrapper around orb.premarket_scanner for production.

Production reads premarket bars from `/data/bars/<DATE>/<TICKER>.jsonl`
(the bar archive that bar_archive.py writes). This module:

  1. Loads the S&P 500 universe + sector classification from
     `data/universe/sp500.json` and `data/universe/sp500_sectors.json`.
  2. Calls `orb.premarket_scanner.scan_day` against the live bar archive
     for the requested date.
  3. Computes the sector-concentration of the top-K picks; sets the
     cluster gate state.
  4. Fails open: if premarket bars are missing for too many tickers
     (e.g. the 09:25 ET batch pull didn't run), returns the static
     fallback universe and marks dynamic_universe_active=False so
     /api/state shows the degradation cleanly.

Used by orb.live_runtime.ensure_session_started during session boot.
Pure read; no broker side-effects.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from orb.premarket_scanner import scan_day, ScanResult


logger = logging.getLogger(__name__)


# Fallback universe used when dynamic-universe is disabled OR when the
# batch pull didn't deliver enough premarket bars. Mirrors the static
# 12-ticker production list pre-v10.
FALLBACK_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG",
    "AMZN", "AVGO", "NFLX", "ORCL", "SPY", "QQQ",
)

# If fewer than this fraction of the candidate universe has premarket
# bars in the live archive, we treat the day as "not enough data" and
# fall back to the static 12. Tuned to allow the pull to be partial
# (e.g. 480/504 succeeds) but reject a complete pull failure (~50/504).
MIN_BAR_COVERAGE = 0.40


@dataclass(frozen=True)
class LiveScanResult:
    """Aggregate result of the live scanner pass for one trading day."""
    date_str: str
    dynamic_universe_active: bool
    cluster_gate_active: bool
    cluster_gate_skipped_day: bool
    cluster_max_sector_pct: float
    cluster_top_sector: str
    universe: list[str]
    picks: list[dict]
    fallback_reason: str  # "" when dynamic mode succeeded


def _load_universe(universe_path: Path) -> list[str]:
    """Load the S&P 500 candidate universe (or any compatible JSON)."""
    try:
        doc = json.loads(universe_path.read_text())
        return list(doc.get("tickers") or [])
    except FileNotFoundError:
        logger.warning("[V10-SCANNER] universe file not found: %s", universe_path)
        return []
    except Exception as e:
        logger.warning("[V10-SCANNER] universe parse failed (%s): %s", universe_path, e)
        return []


def _load_sectors(sectors_path: Path) -> dict[str, str]:
    """Load the per-ticker GICS sector map."""
    try:
        doc = json.loads(sectors_path.read_text())
        return dict(doc.get("sectors") or {})
    except FileNotFoundError:
        logger.warning("[V10-SCANNER] sectors file not found: %s", sectors_path)
        return {}
    except Exception as e:
        logger.warning("[V10-SCANNER] sectors parse failed (%s): %s", sectors_path, e)
        return {}


def _has_premarket_bars(corpus_root: Path, date_str: str, ticker: str) -> bool:
    """Cheap existence + non-empty check on the day's JSONL file."""
    p = corpus_root / date_str / f"{ticker}.jsonl"
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def compute_universe(
    *,
    date_str: str,
    bar_archive_root: Path | str,
    universe_path: Path | str,
    sectors_path: Path | str,
    signal: str = "compression",
    top_k: int = 7,
    min_pm_bars: int = 10,
    min_dollar_volume: float = 30_000_000.0,
    pm_lookback_n: int = 5,
    pm_min_lookback_min: int = 30,
    cluster_max_sector_pct: float = 60.0,
    enabled: bool = True,
) -> LiveScanResult:
    """Compute today's universe for the morning ORB engine.

    Parameters mirror the backtest harness so backtest <-> live parity is
    a single config diff.
    """
    bar_archive_root = Path(bar_archive_root)
    universe_path = Path(universe_path)
    sectors_path = Path(sectors_path)

    if not enabled:
        return LiveScanResult(
            date_str=date_str,
            dynamic_universe_active=False,
            cluster_gate_active=False,
            cluster_gate_skipped_day=False,
            cluster_max_sector_pct=0.0,
            cluster_top_sector="",
            universe=list(FALLBACK_UNIVERSE),
            picks=[],
            fallback_reason="dynamic_universe_disabled",
        )

    candidates = _load_universe(universe_path)
    if not candidates:
        logger.warning("[V10-SCANNER] empty candidate universe; falling back to static 12")
        return LiveScanResult(
            date_str=date_str,
            dynamic_universe_active=False,
            cluster_gate_active=False,
            cluster_gate_skipped_day=False,
            cluster_max_sector_pct=0.0,
            cluster_top_sector="",
            universe=list(FALLBACK_UNIVERSE),
            picks=[],
            fallback_reason="empty_candidate_universe",
        )

    # Coverage check: how many candidates have premarket bars today?
    have_bars = sum(
        1 for tk in candidates
        if _has_premarket_bars(bar_archive_root, date_str, tk)
    )
    coverage = have_bars / max(len(candidates), 1)

    # Auto-rebuild path: if the 09:24 ET batch pull didn't deliver
    # (Railway sync lag, GHA workflow failure, etc.) and we're inside the
    # scanner-window (which the caller decides \u2014 here we just attempt
    # if coverage is below threshold AND the auto-rebuild env flag is on),
    # try a single in-process pull from Alpaca SIP before giving up. The
    # rebuild takes ~30s for S&P 500 / one date.
    auto_rebuild_on = os.environ.get(
        "ORB_DYNAMIC_UNIVERSE_AUTO_REBUILD", "1"
    ) == "1"
    if coverage < MIN_BAR_COVERAGE and auto_rebuild_on:
        logger.warning(
            "[V10-SCANNER-REBUILD] coverage %d/%d (%.1f%%) below %.0f%% "
            "-- attempting in-process Alpaca pull for %s",
            have_bars, len(candidates), coverage * 100,
            MIN_BAR_COVERAGE * 100, date_str,
        )
        try:
            from tools.pull_premarket_for_scanner import (
                rebuild_premarket_bars_for_date,
            )
            from datetime import date as _date

            n_pulled = rebuild_premarket_bars_for_date(
                target_date=_date.fromisoformat(date_str),
                out_root=bar_archive_root,
                universe_tickers=candidates,
            )
            logger.info(
                "[V10-SCANNER-REBUILD] pulled %d bars; re-checking coverage",
                n_pulled,
            )
        except Exception as e:
            logger.warning("[V10-SCANNER-REBUILD] pull failed: %s", e)
        # Recompute coverage after rebuild attempt
        have_bars = sum(
            1 for tk in candidates
            if _has_premarket_bars(bar_archive_root, date_str, tk)
        )
        coverage = have_bars / max(len(candidates), 1)

    if coverage < MIN_BAR_COVERAGE:
        logger.warning(
            "[V10-SCANNER] insufficient premarket bars after rebuild "
            "attempt: %d/%d candidates (%.1f%% < %.0f%% threshold) -- "
            "falling back to static 12",
            have_bars,
            len(candidates),
            coverage * 100,
            MIN_BAR_COVERAGE * 100,
        )
        return LiveScanResult(
            date_str=date_str,
            dynamic_universe_active=False,
            cluster_gate_active=False,
            cluster_gate_skipped_day=False,
            cluster_max_sector_pct=0.0,
            cluster_top_sector="",
            universe=list(FALLBACK_UNIVERSE),
            picks=[],
            fallback_reason=f"insufficient_premarket_bars_{have_bars}_of_{len(candidates)}",
        )

    # Real scan -- uses the JSONL bars in the live archive.
    try:
        results: list[ScanResult] = scan_day(
            bar_archive_root,
            date_str,
            candidates,
            signal=signal,
            top_k=top_k,
            min_pm_bars=min_pm_bars,
            min_dollar_volume=min_dollar_volume,
            pm_lookback_n=pm_lookback_n,
            pm_min_lookback_min=pm_min_lookback_min,
        )
    except Exception as e:
        logger.exception("[V10-SCANNER] scan_day raised: %s -- fallback", e)
        return LiveScanResult(
            date_str=date_str,
            dynamic_universe_active=False,
            cluster_gate_active=False,
            cluster_gate_skipped_day=False,
            cluster_max_sector_pct=0.0,
            cluster_top_sector="",
            universe=list(FALLBACK_UNIVERSE),
            picks=[],
            fallback_reason=f"scan_exception:{type(e).__name__}",
        )

    if not results:
        logger.warning("[V10-SCANNER] scanner produced 0 picks for %s -- fallback", date_str)
        return LiveScanResult(
            date_str=date_str,
            dynamic_universe_active=False,
            cluster_gate_active=False,
            cluster_gate_skipped_day=False,
            cluster_max_sector_pct=0.0,
            cluster_top_sector="",
            universe=list(FALLBACK_UNIVERSE),
            picks=[],
            fallback_reason="zero_picks",
        )

    # Sector-cluster gate
    sectors = _load_sectors(sectors_path)
    pick_sectors = [sectors.get(r.ticker, "Unknown") for r in results]
    sec_counts = Counter(pick_sectors)
    top_sector, top_n = sec_counts.most_common(1)[0] if sec_counts else ("", 0)
    top_pct = (100.0 * top_n / len(results)) if results else 0.0
    cluster_skipped = (
        cluster_max_sector_pct > 0
        and top_pct >= cluster_max_sector_pct
    )

    if cluster_skipped:
        logger.info(
            "[V10-SCANNER-CLUSTER-SKIP] %s -- %s concentration %.0f%% (%d/%d) "
            ">= threshold %.0f%%; skipping the day",
            date_str, top_sector, top_pct, top_n, len(results),
            cluster_max_sector_pct,
        )
        chosen_universe: list[str] = []  # day skipped
    else:
        chosen_universe = [r.ticker for r in results]
        logger.info(
            "[V10-SCANNER-PICKS] %s -- top-%d: %s; max_sector=%s (%.0f%%)",
            date_str, len(results),
            ",".join(chosen_universe), top_sector, top_pct,
        )

    pick_dicts = [
        {
            "ticker": r.ticker,
            "score": round(r.score, 6),
            "gap_pct": round(r.gap_pct * 100, 4),
            "pm_dollar_volume": round(r.pm_dollar_volume, 0),
            "pm_range_pct": round(r.pm_range_pct * 100, 4),
            "n_pm_bars": r.n_pm_bars,
            "sector": sectors.get(r.ticker, "Unknown"),
        }
        for r in results
    ]
    return LiveScanResult(
        date_str=date_str,
        dynamic_universe_active=True,
        cluster_gate_active=(cluster_max_sector_pct > 0),
        cluster_gate_skipped_day=cluster_skipped,
        cluster_max_sector_pct=top_pct,
        cluster_top_sector=top_sector,
        universe=chosen_universe,
        picks=pick_dicts,
        fallback_reason="",
    )


def default_bar_archive_root() -> Path:
    """Resolve the bar archive root, matching bar_archive.py's convention.

    Precedence:
      1. BARS_BASE_DIR (matches engine/scan.py and tools/orb_spy_loader.py).
         Used by the simulator to redirect the scanner at the corpus
         without symlinking TG_DATA_ROOT/bars (which was the pre-2026-05-20
         approach that caused the bar-archive-write-back contamination).
      2. ${TG_DATA_ROOT}/bars -- production default (TG_DATA_ROOT defaults
         to /data, so the resolved path is /data/bars).
    """
    bars_dir = os.environ.get("BARS_BASE_DIR")
    if bars_dir:
        return Path(bars_dir)
    root = os.environ.get("TG_DATA_ROOT", "/data")
    return Path(root) / "bars"


def default_universe_path() -> Path:
    """Resolve data/universe/sp500.json from the repo or TG_DATA_ROOT."""
    # In production this lives at /data/universe; in dev it's in the repo.
    repo_path = Path(__file__).resolve().parent.parent / "data" / "universe" / "sp500.json"
    if repo_path.is_file():
        return repo_path
    return Path(os.environ.get("TG_DATA_ROOT", "/data")) / "universe" / "sp500.json"


def default_sectors_path() -> Path:
    """Resolve data/universe/sp500_sectors.json."""
    repo_path = (
        Path(__file__).resolve().parent.parent
        / "data" / "universe" / "sp500_sectors.json"
    )
    if repo_path.is_file():
        return repo_path
    return (
        Path(os.environ.get("TG_DATA_ROOT", "/data"))
        / "universe" / "sp500_sectors.json"
    )
