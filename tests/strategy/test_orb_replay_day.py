"""Tests for tools.orb_replay_day -- replays archived bars through the
live runtime.

Builds a synthetic JSONL bar archive in a tmp dir, then replays it and
asserts the emitted ledger contains the expected admit + exit events.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orb import live_runtime
from tools.orb_replay_day import ReplayConfig, replay, write_ledger


@pytest.fixture(autouse=True)
def reset_runtime():
    live_runtime._reset_for_testing()
    yield
    live_runtime._reset_for_testing()


@pytest.fixture
def isolated_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ORB_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORB_PARTIAL_PROFIT_AT_1R", "0")  # v8.1.3 legacy default
    yield monkeypatch


def _write_bars(base: Path, date_iso: str, ticker: str,
                bars: list[dict]) -> None:
    d = base / date_iso
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ticker}.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        for b in bars:
            fh.write(json.dumps(b) + "\n")


def _build_or_then_breakout(or_low: float, or_high: float,
                            *, breakout_close: float,
                            bucket_offset: int = 0) -> list[dict]:
    """30 OR bars + 5 post-OR 1m bars culminating in a 5m breakout
    close. Each bar carries `et_bucket`, `open/high/low/close`, `total_volume`,
    and a synthetic monotone `ts`.
    """
    bars: list[dict] = []
    base_bucket = 9 * 60 + 30
    base_ts = 1_000_000  # arbitrary monotone seed
    mid = (or_high + or_low) / 2.0
    # OR window: 30 bars, first carries the high+low extremes
    for i in range(30):
        bars.append({
            "ts": base_ts + (base_bucket + i) * 60,
            "et_bucket": base_bucket + i,
            "open": mid - 0.02,
            "high": or_high if i == 0 else mid + 0.05,
            "low": or_low if i == 0 else mid - 0.05,
            "close": mid + 0.02,
            "total_volume": 10_000,
        })
    # Post-OR: bars 600..604 (a 5m window) with rising close culminating
    # at breakout_close on bar 604.
    or_end = base_bucket + 30  # 600
    for j in range(5):
        bucket = or_end + j
        last = (j == 4)
        c = breakout_close if last else (mid + 0.02 + (j * 0.1))
        bars.append({
            "ts": base_ts + bucket * 60,
            "et_bucket": bucket,
            "open": mid + 0.02 + (j * 0.05),
            "high": breakout_close + 0.1 if last else mid + 0.1 + (j * 0.1),
            "low": mid - 0.05,
            "close": c,
            "total_volume": 20_000,
        })
    # Bar 605 -- next-open used as fill. Close above target zone to
    # trigger the target exit on the same bar.
    target_hint = breakout_close + ((breakout_close - or_low) * 2.5)
    bars.append({
        "ts": base_ts + (or_end + 5) * 60,
        "et_bucket": or_end + 5,
        "open": breakout_close,
        "high": target_hint * 1.01,
        "low": breakout_close - 0.02,
        "close": target_hint,
        "total_volume": 25_000,
    })
    return bars


class TestReplay:

    def test_admit_then_exit_target(self, isolated_env, tmp_path):
        date_iso = "2026-05-09"
        bars = _build_or_then_breakout(or_low=99.5, or_high=100.5,
                                       breakout_close=101.0)
        _write_bars(tmp_path, date_iso, "AAPL", bars)
        cfg = ReplayConfig(
            date_iso=date_iso, tickers=["AAPL"],
            base_dir=str(tmp_path),
            vix_close_d1=18.0, equity=100_000.0,
        )
        events = replay(cfg)
        kinds = [e.kind for e in events]
        assert "session_start" in kinds
        assert "admit" in kinds, f"no admit in ledger; kinds={kinds}"
        admit_ev = next(e for e in events if e.kind == "admit")
        assert admit_ev.payload["ticker"] == "AAPL"
        assert admit_ev.payload["side"] == "long"
        assert admit_ev.payload["shares"] > 0
        # Target is breakout_close + 2.5R, which we close at on bar 605
        assert "exit" in kinds, f"no exit in ledger; kinds={kinds}"
        exit_ev = next(e for e in events if e.kind == "exit")
        assert exit_ev.payload["reason"] in ("target", "be_stop")

    def test_no_archive_emits_error(self, isolated_env, tmp_path):
        cfg = ReplayConfig(
            date_iso="2026-05-09", tickers=["NONE"],
            base_dir=str(tmp_path),
            vix_close_d1=18.0,
        )
        events = replay(cfg)
        assert events[0].kind == "error"

    def test_summary_present(self, isolated_env, tmp_path):
        date_iso = "2026-05-09"
        bars = _build_or_then_breakout(or_low=99.5, or_high=100.5,
                                       breakout_close=101.0)
        _write_bars(tmp_path, date_iso, "AAPL", bars)
        cfg = ReplayConfig(
            date_iso=date_iso, tickers=["AAPL"],
            base_dir=str(tmp_path), vix_close_d1=18.0,
        )
        events = replay(cfg)
        assert events[-1].kind == "summary"
        assert "admits" in events[-1].payload

    def test_write_ledger_round_trip(self, isolated_env, tmp_path):
        date_iso = "2026-05-09"
        bars = _build_or_then_breakout(or_low=99.5, or_high=100.5,
                                       breakout_close=101.0)
        _write_bars(tmp_path, date_iso, "AAPL", bars)
        cfg = ReplayConfig(
            date_iso=date_iso, tickers=["AAPL"],
            base_dir=str(tmp_path), vix_close_d1=18.0,
        )
        events = replay(cfg)
        out_path = tmp_path / "ledger.jsonl"
        write_ledger(events, str(out_path))
        # Round-trip parse
        with open(out_path, "r", encoding="utf-8") as fh:
            parsed = [json.loads(line) for line in fh if line.strip()]
        assert len(parsed) == len(events)
        assert parsed[0]["kind"] == "session_start"
