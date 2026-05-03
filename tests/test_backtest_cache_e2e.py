"""v6.9.0 e2e test: replay with cache cold + warm produces identical summaries.

Rules: zero em-dashes (literal or escaped). Uses the minimal replay fixture.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

# Set env vars before any trade_genius import; matches the pattern in
# existing test files (test_v6_7_0_system_test.py, conftest.py).
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "0:backtest_dummy_token")
os.environ.setdefault("CHAT_ID", "0")
os.environ.setdefault("DASHBOARD_PASSWORD", "")
os.environ.setdefault("FMP_API_KEY", "test-key")
os.environ.setdefault("TG_BACKTEST_MODE", "1")

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay_v511_minimal"
FIXTURE_DATE = "2026-04-28"
FIXTURE_TICKERS = ["AAPL", "QQQ"]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run_replay_on_fixture(bars_dir: Path) -> dict:
    """Run a single-day replay over bars_dir; return the summary dict."""
    from backtest.replay_v511_full import build_json_report, run_replay

    result = run_replay(
        date_str=FIXTURE_DATE,
        tickers=FIXTURE_TICKERS,
        bars_dir=bars_dir,
    )
    report = build_json_report(result)
    return report["summary"]


# ---------------------------------------------------------------------------
# Test: cache cold + warm summaries are identical
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_cold_and_warm_summaries_identical(tmp_path: Path) -> None:
    """Run replay twice (cold then warm); assert summary fields are bit-exact."""
    # Copy fixture to tmp so the cache does not bleed between test runs
    bars_dir = tmp_path / "bars"
    shutil.copytree(str(FIXTURE_DIR), str(bars_dir))

    # --- Cold run (no Parquet cache yet) ---
    cache_dir = bars_dir / ".cache_v2"
    assert not cache_dir.exists(), "Cache must not exist before cold run"

    t_cold_start = time.perf_counter()
    summary_cold = _run_replay_on_fixture(bars_dir)
    t_cold = time.perf_counter() - t_cold_start

    # Parquet should now exist for at least one ticker
    assert cache_dir.exists(), "Cache dir must be created after cold run"
    parquets = list(cache_dir.glob("**/*.parquet"))
    assert parquets, "At least one Parquet file must exist after cold run"

    # --- Warm run (Parquet cache present) ---
    t_warm_start = time.perf_counter()
    summary_warm = _run_replay_on_fixture(bars_dir)
    t_warm = time.perf_counter() - t_warm_start

    # Bit-exact comparison of replay summary
    assert summary_cold["entries"] == summary_warm["entries"], (
        f"entries mismatch: cold={summary_cold['entries']} warm={summary_warm['entries']}"
    )
    assert summary_cold["exits"] == summary_warm["exits"], (
        f"exits mismatch: cold={summary_cold['exits']} warm={summary_warm['exits']}"
    )
    # total_pnl must match exactly (same float)
    cold_pnl = summary_cold.get("total_pnl", 0.0) or 0.0
    warm_pnl = summary_warm.get("total_pnl", 0.0) or 0.0
    assert abs(cold_pnl - warm_pnl) < 1e-9, (
        f"total_pnl mismatch: cold={cold_pnl} warm={warm_pnl}"
    )

    # Report timings (not an assertion; informational)
    print(f"\n[e2e] cold={t_cold:.3f}s  warm={t_warm:.3f}s")
    print(f"[e2e] summary: entries={summary_cold['entries']} exits={summary_cold['exits']} pnl={cold_pnl:.4f}")


# ---------------------------------------------------------------------------
# Test: get_bars returns correct count for fixture
# ---------------------------------------------------------------------------


def test_get_bars_fixture_count(tmp_path: Path) -> None:
    """get_bars returns same bar count as direct JSONL load for fixture."""
    from backtest.bar_cache import get_bars
    from backtest.replay_v511_full import load_day_bars

    bars_dir = tmp_path / "bars"
    shutil.copytree(str(FIXTURE_DIR), str(bars_dir))

    cached = get_bars(bars_dir, "AAPL", FIXTURE_DATE)
    direct = load_day_bars(bars_dir, FIXTURE_DATE, "AAPL")

    assert len(cached) == len(direct), (
        f"Bar count mismatch: cache={len(cached)} direct={len(direct)}"
    )


# ---------------------------------------------------------------------------
# Test: all fixture tickers have matching bar counts
# ---------------------------------------------------------------------------


def test_all_tickers_bar_count_matches(tmp_path: Path) -> None:
    from backtest.bar_cache import get_bars
    from backtest.replay_v511_full import load_day_bars

    bars_dir = tmp_path / "bars"
    shutil.copytree(str(FIXTURE_DIR), str(bars_dir))

    for ticker in FIXTURE_TICKERS:
        cached = get_bars(bars_dir, ticker, FIXTURE_DATE)
        direct = load_day_bars(bars_dir, FIXTURE_DATE, ticker)
        assert len(cached) == len(direct), (
            f"{ticker}: cache={len(cached)} direct={len(direct)}"
        )
        # Verify close values match
        for c, d in zip(cached, direct):
            c_close = c.get("close") or 0.0
            d_close = d.get("close") or 0.0
            assert abs(float(c_close) - float(d_close)) < 1e-9, (
                f"{ticker} ts={c.get('ts')}: cache close={c_close} direct={d_close}"
            )
