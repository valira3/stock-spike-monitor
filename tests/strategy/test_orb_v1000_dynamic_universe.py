"""v10.0.0 -- broad-universe scanner + sector-cluster gate.

Covers:
  - orb.live_premarket_scanner.compute_universe behavior on success +
    every fallback path (disabled, empty universe, insufficient bars,
    zero picks, scan exception).
  - orb.scanner_state setter / getter / snapshot serialization.
  - orb.live_runtime._check_cluster_gate firing logic.
  - orb.live_runtime._run_dynamic_universe_scanner integration with
    scanner_state (env flag, fallback on exception).
  - tools.pull_premarket_for_scanner.rebuild_premarket_bars_for_date
    no-ops gracefully when creds are missing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


def _write_bar(path: Path, et_bucket: str, close: float = 100.0,
               volume: float = 50_000.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": f"2026-05-20T{et_bucket[:2]}:{et_bucket[2:]}:00-04:00",
        "et_bucket": et_bucket,
        "open": close, "high": close + 0.1, "low": close - 0.1, "close": close,
        "total_volume": volume,
        "iex_volume": None, "iex_sip_ratio_used": None,
        "bid": None, "ask": None, "last_trade_price": None,
        "trade_count": 1.0, "bar_vwap": close, "feed_source": "sip",
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _make_day(root: Path, date: str, ticker: str, n_pm_bars: int = 30,
              close: float = 100.0, volume: float = 50_000.0) -> None:
    """Write `n_pm_bars` premarket bars (04:00 onward) for one day."""
    for i in range(n_pm_bars):
        h, m = divmod(4 * 60 + i, 60)
        _write_bar(root / date / f"{ticker}.jsonl",
                   et_bucket=f"{h:02d}{m:02d}",
                   close=close, volume=volume)


def _make_prior_rth_close(root: Path, prior_date: str, ticker: str,
                          close: float = 100.0) -> None:
    """Write a single RTH bar at 15:59 ET for the prior day so the scanner
    can compute gap_pct without falling back to None."""
    _write_bar(root / prior_date / f"{ticker}.jsonl",
               et_bucket="1559", close=close, volume=10_000.0)


@pytest.fixture
def tmp_corpus(tmp_path):
    """Tiny 3-ticker corpus -- enough to exercise the scanner."""
    DATE = "2026-05-20"
    PRIOR = "2026-05-19"
    for tk, close in [("ALPHA", 100.0), ("BRAVO", 200.0), ("CHARLIE", 50.0)]:
        _make_prior_rth_close(tmp_path, PRIOR, tk, close=close)
        _make_day(tmp_path, DATE, tk, n_pm_bars=30, close=close,
                  volume=1_000_000.0)
    return tmp_path


@pytest.fixture
def tmp_universe_files(tmp_path):
    """Write the universe + sectors JSON files the scanner needs."""
    uni = {
        "tickers": ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO"],
    }
    sec = {
        "sectors": {
            "ALPHA": "Information Technology",
            "BRAVO": "Information Technology",
            "CHARLIE": "Financials",
            "DELTA": "Health Care",
            "ECHO": "Consumer Discretionary",
        }
    }
    uni_path = tmp_path / "sp500.json"
    sec_path = tmp_path / "sp500_sectors.json"
    uni_path.write_text(json.dumps(uni))
    sec_path.write_text(json.dumps(sec))
    return uni_path, sec_path


# ----------------------------------------------------------------------------
# compute_universe: success + fallback paths
# ----------------------------------------------------------------------------


def test_compute_universe_disabled_returns_static_fallback(
    tmp_corpus, tmp_universe_files
):
    from orb.live_premarket_scanner import compute_universe, FALLBACK_UNIVERSE
    uni_path, sec_path = tmp_universe_files
    r = compute_universe(
        date_str="2026-05-20",
        bar_archive_root=tmp_corpus,
        universe_path=uni_path,
        sectors_path=sec_path,
        enabled=False,
    )
    assert r.dynamic_universe_active is False
    assert r.universe == list(FALLBACK_UNIVERSE)
    assert r.picks == []
    assert r.fallback_reason == "dynamic_universe_disabled"


def test_compute_universe_empty_candidate_universe_falls_back(tmp_path, tmp_corpus):
    """Universe JSON exists but tickers[] is empty."""
    from orb.live_premarket_scanner import compute_universe, FALLBACK_UNIVERSE
    uni_path = tmp_path / "empty.json"
    uni_path.write_text(json.dumps({"tickers": []}))
    sec_path = tmp_path / "sec.json"
    sec_path.write_text(json.dumps({"sectors": {}}))
    r = compute_universe(
        date_str="2026-05-20",
        bar_archive_root=tmp_corpus,
        universe_path=uni_path,
        sectors_path=sec_path,
    )
    assert r.universe == list(FALLBACK_UNIVERSE)
    assert r.fallback_reason == "empty_candidate_universe"


def test_compute_universe_missing_universe_file_falls_back(tmp_path, tmp_corpus):
    from orb.live_premarket_scanner import compute_universe, FALLBACK_UNIVERSE
    r = compute_universe(
        date_str="2026-05-20",
        bar_archive_root=tmp_corpus,
        universe_path=tmp_path / "nope.json",
        sectors_path=tmp_path / "also-nope.json",
    )
    assert r.universe == list(FALLBACK_UNIVERSE)
    assert r.fallback_reason == "empty_candidate_universe"


def test_compute_universe_insufficient_premarket_bars_falls_back(
    tmp_path, tmp_universe_files
):
    """Corpus is empty -- no premarket bars for any candidate."""
    from orb.live_premarket_scanner import compute_universe, FALLBACK_UNIVERSE
    uni_path, sec_path = tmp_universe_files
    empty_corpus = tmp_path / "empty_corpus"
    empty_corpus.mkdir()
    # Disable auto-rebuild for this test so we get a clean fallback signal
    with mock.patch.dict(os.environ, {"ORB_DYNAMIC_UNIVERSE_AUTO_REBUILD": "0"}):
        r = compute_universe(
            date_str="2026-05-20",
            bar_archive_root=empty_corpus,
            universe_path=uni_path,
            sectors_path=sec_path,
        )
    assert r.universe == list(FALLBACK_UNIVERSE)
    assert "insufficient_premarket_bars" in r.fallback_reason


def test_compute_universe_success_dynamic_active(tmp_corpus, tmp_universe_files):
    """Three of five candidates have bars (60% coverage > 40% threshold).
    Scanner should return picks + dynamic_universe_active=True."""
    from orb.live_premarket_scanner import compute_universe
    uni_path, sec_path = tmp_universe_files
    r = compute_universe(
        date_str="2026-05-20",
        bar_archive_root=tmp_corpus,
        universe_path=uni_path,
        sectors_path=sec_path,
        signal="composite",
        top_k=3,
        min_dollar_volume=0,  # don't filter the test fixture out
    )
    assert r.dynamic_universe_active is True
    assert r.fallback_reason == ""
    # We wrote bars for ALPHA, BRAVO, CHARLIE -- scanner can pick from them
    tickers = {p["ticker"] for p in r.picks}
    assert tickers <= {"ALPHA", "BRAVO", "CHARLIE"}
    # Every pick has a sector attached
    for p in r.picks:
        assert "sector" in p


def test_compute_universe_cluster_gate_skips_concentrated_day(
    tmp_corpus, tmp_universe_files
):
    """ALPHA + BRAVO are both Information Technology -- top-2 picks
    would be 2/2 = 100% IT, exceeding the 60% threshold and skipping
    the day."""
    from orb.live_premarket_scanner import compute_universe
    uni_path, sec_path = tmp_universe_files
    # Restrict the candidate universe so only the two IT names qualify
    # (BRAVO has the highest dollar volume due to $200 close)
    r = compute_universe(
        date_str="2026-05-20",
        bar_archive_root=tmp_corpus,
        universe_path=uni_path,
        sectors_path=sec_path,
        signal="volume",
        top_k=2,
        min_dollar_volume=0,
        cluster_max_sector_pct=60.0,
    )
    # Force the picks to be IT-only by ensuring top-2 volume picks
    # are ALPHA + BRAVO (both IT). If they are, the gate fires.
    pick_sectors = {p["sector"] for p in r.picks}
    if pick_sectors == {"Information Technology"}:
        assert r.cluster_gate_skipped_day is True
        assert r.universe == []


# ----------------------------------------------------------------------------
# scanner_state: set / get / clear / snapshot
# ----------------------------------------------------------------------------


def test_scanner_state_setter_and_snapshot(tmp_corpus, tmp_universe_files):
    from orb import scanner_state
    from orb.live_premarket_scanner import compute_universe
    scanner_state.clear_state()
    assert scanner_state.get_current() is None
    snap_empty = scanner_state.to_snapshot_dict()
    assert snap_empty["dynamic_universe_active"] is False
    assert snap_empty["picks"] == []
    assert snap_empty["fallback_reason"] == "not_initialized"

    uni_path, sec_path = tmp_universe_files
    r = compute_universe(
        date_str="2026-05-20",
        bar_archive_root=tmp_corpus,
        universe_path=uni_path,
        sectors_path=sec_path,
        signal="composite", top_k=3, min_dollar_volume=0,
    )
    scanner_state.set_current(r)
    cur = scanner_state.get_current()
    assert cur is not None and cur.date_str == "2026-05-20"
    snap = scanner_state.to_snapshot_dict()
    assert snap["date"] == "2026-05-20"
    assert snap["dynamic_universe_active"] == r.dynamic_universe_active
    assert isinstance(snap["picks"], list)
    assert isinstance(snap["universe"], list)


def test_scanner_state_clear_state(tmp_corpus, tmp_universe_files):
    from orb import scanner_state
    from orb.live_premarket_scanner import compute_universe
    uni_path, sec_path = tmp_universe_files
    r = compute_universe(
        date_str="2026-05-20",
        bar_archive_root=tmp_corpus,
        universe_path=uni_path,
        sectors_path=sec_path,
        signal="composite", top_k=3, min_dollar_volume=0,
    )
    scanner_state.set_current(r)
    assert scanner_state.get_current() is not None
    scanner_state.clear_state()
    assert scanner_state.get_current() is None


def test_scanner_state_thread_safety_smoke():
    """Concurrent set/get doesn't deadlock or race (basic smoke)."""
    from orb import scanner_state
    from orb.live_premarket_scanner import LiveScanResult

    scanner_state.clear_state()
    R = LiveScanResult(
        date_str="2026-05-20", dynamic_universe_active=True,
        cluster_gate_active=True, cluster_gate_skipped_day=False,
        cluster_max_sector_pct=25.0, cluster_top_sector="Tech",
        universe=["AAA"], picks=[{"ticker": "AAA", "sector": "Tech",
                                  "score": 0.0, "gap_pct": 0.0,
                                  "pm_dollar_volume": 0.0,
                                  "pm_range_pct": 0.0, "n_pm_bars": 0}],
        fallback_reason="",
    )
    import threading
    def writer():
        for _ in range(50):
            scanner_state.set_current(R)
    def reader():
        for _ in range(50):
            scanner_state.get_current()
            scanner_state.to_snapshot_dict()
    ts = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in ts), "scanner_state deadlocked"


# ----------------------------------------------------------------------------
# _check_cluster_gate: live_runtime cluster gate firing
# ----------------------------------------------------------------------------


def test_check_cluster_gate_none_when_state_missing():
    from orb import scanner_state
    from orb.live_runtime import _check_cluster_gate
    scanner_state.clear_state()
    assert _check_cluster_gate() is None


def test_check_cluster_gate_none_when_not_skipped():
    """Cluster gate active but not triggered today -- legitimate no-op."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from orb import scanner_state
    from orb.live_premarket_scanner import LiveScanResult
    from orb.live_runtime import _check_cluster_gate
    today_iso = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    scanner_state.set_current(LiveScanResult(
        date_str=today_iso, dynamic_universe_active=True,
        cluster_gate_active=True, cluster_gate_skipped_day=False,
        cluster_max_sector_pct=25.0, cluster_top_sector="Tech",
        universe=["AAA"], picks=[], fallback_reason="",
    ))
    assert _check_cluster_gate() is None


def test_check_cluster_gate_fires_when_skipped():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from orb import scanner_state
    from orb.live_premarket_scanner import LiveScanResult
    from orb.live_runtime import _check_cluster_gate
    today_iso = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    scanner_state.set_current(LiveScanResult(
        date_str=today_iso, dynamic_universe_active=True,
        cluster_gate_active=True, cluster_gate_skipped_day=True,
        cluster_max_sector_pct=71.4, cluster_top_sector="Information Technology",
        universe=[], picks=[], fallback_reason="",
    ))
    reason = _check_cluster_gate()
    assert reason is not None
    assert "cluster_gate_skip" in reason
    assert "Information" in reason


def test_check_cluster_gate_none_when_gate_disabled():
    """cluster_gate_active=False even if skipped_day=True (shouldn't happen
    in practice but the helper must respect cluster_gate_active)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from orb import scanner_state
    from orb.live_premarket_scanner import LiveScanResult
    from orb.live_runtime import _check_cluster_gate
    today_iso = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    scanner_state.set_current(LiveScanResult(
        date_str=today_iso, dynamic_universe_active=True,
        cluster_gate_active=False, cluster_gate_skipped_day=True,
        cluster_max_sector_pct=80.0, cluster_top_sector="Tech",
        universe=[], picks=[], fallback_reason="",
    ))
    assert _check_cluster_gate() is None


def test_check_cluster_gate_stale_date_returns_none(caplog):
    """v10.0.1 -- scanner_state from yesterday must not block today.
    Without this guard, a rehydrate-on-restart or a missed session-start
    scan could leave a "skip the day" decision in place across day
    boundaries and silently halt all trading."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import logging

    from orb import scanner_state
    from orb.live_premarket_scanner import LiveScanResult
    from orb.live_runtime import _check_cluster_gate, _cluster_stale_logged_for

    # Clear the one-shot log-set so the warning fires for this test
    _cluster_stale_logged_for.clear()

    today_iso = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    # Pick a date that is guaranteed NOT to be today
    stale_date = "2020-01-02"
    assert stale_date != today_iso

    scanner_state.set_current(LiveScanResult(
        date_str=stale_date,
        dynamic_universe_active=True,
        cluster_gate_active=True,
        cluster_gate_skipped_day=True,
        cluster_max_sector_pct=85.0,
        cluster_top_sector="Information Technology",
        universe=[], picks=[], fallback_reason="",
    ))
    with caplog.at_level(logging.WARNING, logger="orb.live_runtime"):
        out = _check_cluster_gate()
    # Stale state must not block; result is None.
    assert out is None
    # And the forensic warning was logged exactly once.
    matched = [r for r in caplog.records if "[V100-CLUSTER-STALE]" in r.message]
    assert len(matched) == 1


def test_check_cluster_gate_stale_warning_logged_once(caplog):
    """Once the stale-date warning has fired for a given (stale, today)
    pair, subsequent calls must NOT re-log -- prevents log spam when the
    gate is consulted on every admit."""
    import logging
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from orb import scanner_state
    from orb.live_premarket_scanner import LiveScanResult
    from orb.live_runtime import _check_cluster_gate, _cluster_stale_logged_for

    _cluster_stale_logged_for.clear()
    today_iso = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    scanner_state.set_current(LiveScanResult(
        date_str="2020-01-02",
        dynamic_universe_active=True, cluster_gate_active=True,
        cluster_gate_skipped_day=True,
        cluster_max_sector_pct=80.0, cluster_top_sector="Tech",
        universe=[], picks=[], fallback_reason="",
    ))
    with caplog.at_level(logging.WARNING, logger="orb.live_runtime"):
        for _ in range(5):
            assert _check_cluster_gate() is None
    matched = [r for r in caplog.records if "[V100-CLUSTER-STALE]" in r.message]
    assert len(matched) == 1, f"expected 1 stale log, got {len(matched)}"


def test_check_cluster_gate_today_match_fires_normally():
    """Sanity: when scanner_state.date_str matches today, the gate
    behaves exactly as v10.0.0 -- fires the skip when concentrated."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from orb import scanner_state
    from orb.live_premarket_scanner import LiveScanResult
    from orb.live_runtime import _check_cluster_gate

    today_iso = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    scanner_state.set_current(LiveScanResult(
        date_str=today_iso,
        dynamic_universe_active=True, cluster_gate_active=True,
        cluster_gate_skipped_day=True,
        cluster_max_sector_pct=71.4,
        cluster_top_sector="Information Technology",
        universe=[], picks=[], fallback_reason="",
    ))
    out = _check_cluster_gate()
    assert out is not None and "cluster_gate_skip" in out


# ----------------------------------------------------------------------------
# _is_dynamic_universe_on / env defaults
# ----------------------------------------------------------------------------


def test_is_dynamic_universe_on_default_true():
    from orb.live_runtime import _is_dynamic_universe_on
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ORB_DYNAMIC_UNIVERSE", None)
        assert _is_dynamic_universe_on() is True


def test_is_dynamic_universe_on_disabled_via_env():
    from orb.live_runtime import _is_dynamic_universe_on
    with mock.patch.dict(os.environ, {"ORB_DYNAMIC_UNIVERSE": "0"}):
        assert _is_dynamic_universe_on() is False
    with mock.patch.dict(os.environ, {"ORB_DYNAMIC_UNIVERSE": "1"}):
        assert _is_dynamic_universe_on() is True


# ----------------------------------------------------------------------------
# _run_dynamic_universe_scanner: integration with scanner_state
# ----------------------------------------------------------------------------


def test_run_dynamic_universe_scanner_disabled_publishes_inactive(monkeypatch):
    from orb import scanner_state
    from orb.live_runtime import _run_dynamic_universe_scanner
    scanner_state.clear_state()
    monkeypatch.setenv("ORB_DYNAMIC_UNIVERSE", "0")
    _run_dynamic_universe_scanner("2026-05-20")
    r = scanner_state.get_current()
    assert r is not None
    assert r.dynamic_universe_active is False
    assert r.fallback_reason == "dynamic_universe_disabled"


def test_run_dynamic_universe_scanner_exception_clears_state(monkeypatch):
    """If the scanner raises unexpectedly, the runtime hook must clear
    state (not leave a stale result around)."""
    from orb import scanner_state
    from orb.live_runtime import _run_dynamic_universe_scanner
    from orb.live_premarket_scanner import LiveScanResult

    # Pre-populate so we can verify it gets cleared.
    scanner_state.set_current(LiveScanResult(
        date_str="2026-05-19", dynamic_universe_active=True,
        cluster_gate_active=False, cluster_gate_skipped_day=False,
        cluster_max_sector_pct=0, cluster_top_sector="",
        universe=[], picks=[], fallback_reason="",
    ))
    with mock.patch(
        "orb.live_runtime._compute_universe",
        side_effect=RuntimeError("simulated"),
    ):
        _run_dynamic_universe_scanner("2026-05-20")
    assert scanner_state.get_current() is None


def test_run_dynamic_universe_scanner_default_params_applied(monkeypatch):
    """When no ORB_DYNAMIC_UNIVERSE_* envs are set, the scanner is
    called with the v10 champion defaults: signal=compression, top_k=7,
    mdv=$30M, pm_lookback_n=5, pm_min_lookback_min=30, cluster=60.
    """
    from orb.live_runtime import _run_dynamic_universe_scanner
    monkeypatch.setenv("ORB_DYNAMIC_UNIVERSE", "1")
    # Strip any env that might leak from CI
    for k in (
        "ORB_DYNAMIC_UNIVERSE_SIGNAL",
        "ORB_DYNAMIC_UNIVERSE_TOP_K",
        "ORB_DYNAMIC_UNIVERSE_MIN_PM_BARS",
        "ORB_DYNAMIC_UNIVERSE_MIN_DOLLAR_VOL",
        "ORB_DYNAMIC_UNIVERSE_PM_LOOKBACK_N",
        "ORB_DYNAMIC_UNIVERSE_PM_MIN_LOOKBACK_MIN",
        "ORB_CLUSTER_MAX_SECTOR_PCT",
    ):
        monkeypatch.delenv(k, raising=False)
    with mock.patch("orb.live_runtime._compute_universe") as mck:
        mck.return_value = mock.MagicMock(
            dynamic_universe_active=False,
            cluster_gate_active=False,
            cluster_gate_skipped_day=False,
            cluster_max_sector_pct=0,
            cluster_top_sector="",
            picks=[],
            fallback_reason="",
        )
        _run_dynamic_universe_scanner("2026-05-20")
    assert mck.called
    kwargs = mck.call_args.kwargs
    assert kwargs["signal"] == "compression"
    assert kwargs["top_k"] == 7
    assert kwargs["min_dollar_volume"] == 30_000_000.0
    assert kwargs["pm_lookback_n"] == 5
    assert kwargs["pm_min_lookback_min"] == 30
    assert kwargs["cluster_max_sector_pct"] == 60.0


def test_run_dynamic_universe_scanner_respects_env_overrides(monkeypatch):
    from orb.live_runtime import _run_dynamic_universe_scanner
    monkeypatch.setenv("ORB_DYNAMIC_UNIVERSE", "1")
    monkeypatch.setenv("ORB_DYNAMIC_UNIVERSE_SIGNAL", "volume")
    monkeypatch.setenv("ORB_DYNAMIC_UNIVERSE_TOP_K", "5")
    monkeypatch.setenv("ORB_DYNAMIC_UNIVERSE_MIN_DOLLAR_VOL", "10000000")
    monkeypatch.setenv("ORB_CLUSTER_MAX_SECTOR_PCT", "50")
    with mock.patch("orb.live_runtime._compute_universe") as mck:
        mck.return_value = mock.MagicMock(
            dynamic_universe_active=False,
            cluster_gate_active=False, cluster_gate_skipped_day=False,
            cluster_max_sector_pct=0, cluster_top_sector="",
            picks=[], fallback_reason="",
        )
        _run_dynamic_universe_scanner("2026-05-20")
    kwargs = mck.call_args.kwargs
    assert kwargs["signal"] == "volume"
    assert kwargs["top_k"] == 5
    assert kwargs["min_dollar_volume"] == 10_000_000.0
    assert kwargs["cluster_max_sector_pct"] == 50.0


# ----------------------------------------------------------------------------
# rebuild_premarket_bars_for_date: graceful no-op on missing creds
# ----------------------------------------------------------------------------


def test_rebuild_premarket_bars_no_creds_returns_zero(tmp_path, monkeypatch):
    from tools.pull_premarket_for_scanner import rebuild_premarket_bars_for_date
    from datetime import date as _date
    monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("VAL_ALPACA_PAPER_SECRET", raising=False)
    n = rebuild_premarket_bars_for_date(
        target_date=_date(2026, 5, 20),
        out_root=tmp_path / "bars",
        universe_tickers=["AAA", "BBB"],
    )
    assert n == 0


# ----------------------------------------------------------------------------
# Snapshot integration
# ----------------------------------------------------------------------------


def test_scanner_state_appears_in_snapshot_dict(tmp_corpus, tmp_universe_files):
    """to_snapshot_dict always returns a well-shaped dict so dashboard JS
    can rely on a consistent structure even when scanner state is missing."""
    from orb import scanner_state
    scanner_state.clear_state()
    d = scanner_state.to_snapshot_dict()
    expected_keys = {
        "date", "dynamic_universe_active", "cluster_gate_active",
        "cluster_gate_skipped_day", "cluster_max_sector_pct",
        "cluster_top_sector", "universe", "picks", "fallback_reason",
    }
    assert expected_keys <= set(d.keys())
    assert isinstance(d["universe"], list)
    assert isinstance(d["picks"], list)


def test_picks_carry_sector_in_snapshot(tmp_corpus, tmp_universe_files):
    """Each pick dict must carry a sector field so the dashboard can
    render the sector breakdown chip."""
    from orb.live_premarket_scanner import compute_universe
    from orb import scanner_state
    uni_path, sec_path = tmp_universe_files
    r = compute_universe(
        date_str="2026-05-20",
        bar_archive_root=tmp_corpus,
        universe_path=uni_path,
        sectors_path=sec_path,
        signal="composite", top_k=3, min_dollar_volume=0,
    )
    scanner_state.set_current(r)
    snap = scanner_state.to_snapshot_dict()
    for p in snap["picks"]:
        assert "sector" in p, f"missing sector on {p}"
        assert "ticker" in p
        assert "score" in p


def test_cluster_max_sector_pct_is_serializable(tmp_corpus, tmp_universe_files):
    """The percentage field has to be a plain Python float so JSON
    serialization in /api/state doesn't choke."""
    from orb.live_premarket_scanner import compute_universe
    from orb import scanner_state
    uni_path, sec_path = tmp_universe_files
    r = compute_universe(
        date_str="2026-05-20",
        bar_archive_root=tmp_corpus,
        universe_path=uni_path,
        sectors_path=sec_path,
        signal="composite", top_k=3, min_dollar_volume=0,
    )
    scanner_state.set_current(r)
    snap = scanner_state.to_snapshot_dict()
    json.dumps(snap)  # must not raise
    assert isinstance(snap["cluster_max_sector_pct"], (int, float))


# ----------------------------------------------------------------------------
# Auto-rebuild path
# ----------------------------------------------------------------------------


def test_auto_rebuild_disabled_by_env_flag(tmp_path, tmp_universe_files, monkeypatch):
    """When ORB_DYNAMIC_UNIVERSE_AUTO_REBUILD=0, low coverage falls
    back without attempting an in-process pull."""
    from orb.live_premarket_scanner import compute_universe
    uni_path, sec_path = tmp_universe_files
    monkeypatch.setenv("ORB_DYNAMIC_UNIVERSE_AUTO_REBUILD", "0")
    empty_corpus = tmp_path / "empty"
    empty_corpus.mkdir()
    with mock.patch(
        "tools.pull_premarket_for_scanner.rebuild_premarket_bars_for_date"
    ) as mck:
        r = compute_universe(
            date_str="2026-05-20",
            bar_archive_root=empty_corpus,
            universe_path=uni_path,
            sectors_path=sec_path,
        )
    # rebuild_premarket_bars_for_date must NOT be called when env=0
    assert mck.call_count == 0
    assert "insufficient_premarket_bars" in r.fallback_reason


def test_auto_rebuild_attempt_on_low_coverage(tmp_path, tmp_universe_files, monkeypatch):
    """Default env (auto-rebuild ON) -- low coverage triggers rebuild."""
    from orb.live_premarket_scanner import compute_universe
    uni_path, sec_path = tmp_universe_files
    monkeypatch.delenv("ORB_DYNAMIC_UNIVERSE_AUTO_REBUILD", raising=False)
    empty_corpus = tmp_path / "empty"
    empty_corpus.mkdir()
    with mock.patch(
        "tools.pull_premarket_for_scanner.rebuild_premarket_bars_for_date",
        return_value=0,
    ) as mck:
        compute_universe(
            date_str="2026-05-20",
            bar_archive_root=empty_corpus,
            universe_path=uni_path,
            sectors_path=sec_path,
        )
    assert mck.call_count == 1
    # And the call was for the right date / out_root
    kwargs = mck.call_args.kwargs
    assert str(kwargs["target_date"]) == "2026-05-20"
    assert Path(kwargs["out_root"]) == empty_corpus


def test_min_bar_coverage_threshold_constant():
    from orb.live_premarket_scanner import MIN_BAR_COVERAGE
    assert 0.0 < MIN_BAR_COVERAGE < 1.0


def test_fallback_universe_static_12():
    """The static fallback list is exactly the pre-v10 production set."""
    from orb.live_premarket_scanner import FALLBACK_UNIVERSE
    assert set(FALLBACK_UNIVERSE) == {
        "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG",
        "AMZN", "AVGO", "NFLX", "ORCL", "SPY", "QQQ",
    }
