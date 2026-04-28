"""v5.10.6 \u2014 /api/state v5.10 payload contract test.

Asserts that dashboard_server.snapshot() carries every v5.10 field the
dashboard now depends on:

  - Top-level: section_i_permit, per_ticker_v510, per_position_v510
  - Per-position rows: phase, sovereign_brake_distance_dollars,
    entry_2_fired

Boots the bot in SSM_SMOKE_TEST mode (no Telegram, no Alpaca, no Polygon)
so the import path is exercised end-to-end. Then patches a synthetic
LONG and SHORT position into the module's positions/short_positions
dicts and confirms snapshot() returns the v5.10 fields populated.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def smoke_module(monkeypatch):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    sys.path.insert(0, str(REPO_ROOT))
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    if "dashboard_server" in sys.modules:
        del sys.modules["dashboard_server"]
    import trade_genius
    import dashboard_server

    yield trade_genius, dashboard_server


def test_api_state_carries_v510_top_level_fields(smoke_module):
    tg, ds = smoke_module
    snap = ds.snapshot()
    assert snap.get("ok") is True, f"snapshot failed: {snap}"

    assert "section_i_permit" in snap, "missing section_i_permit"
    assert "per_ticker_v510" in snap, "missing per_ticker_v510"
    assert "per_position_v510" in snap, "missing per_position_v510"

    sip = snap["section_i_permit"]
    assert isinstance(sip, dict)
    for key in (
        "long_open",
        "short_open",
        "qqq_5m_close",
        "qqq_5m_ema9",
        "qqq_avwap_0930",
        "sovereign_anchor_open",
    ):
        assert key in sip, f"section_i_permit missing {key}"
    assert isinstance(sip["long_open"], bool)
    assert isinstance(sip["short_open"], bool)
    assert isinstance(sip["sovereign_anchor_open"], bool)

    assert isinstance(snap["per_ticker_v510"], dict)
    assert isinstance(snap["per_position_v510"], dict)


def test_api_state_per_ticker_v510_shape(smoke_module):
    tg, ds = smoke_module
    snap = ds.snapshot()
    per_t = snap.get("per_ticker_v510") or {}

    if not per_t:
        return

    sample = next(iter(per_t.values()))
    assert "vol_bucket" in sample
    vb = sample["vol_bucket"]
    assert vb["state"] in ("PASS", "FAIL", "COLDSTART")
    assert isinstance(vb["current_1m_vol"], int)

    assert "boundary_hold" in sample
    bh = sample["boundary_hold"]
    assert bh["state"] in ("ARMED", "SATISFIED", "BROKEN")
    assert isinstance(bh["last_two_closes"], list)


def test_api_state_position_rows_carry_v510_fields(smoke_module):
    tg, ds = smoke_module

    tg.positions["AAPL"] = {
        "ticker": "AAPL",
        "entry_price": 150.0,
        "shares": 10,
        "stop": 148.0,
        "phase": "B",
        "v5104_entry2_fired": True,
        "entry_time": "10:35 CDT",
        "entry_count": 2,
    }
    tg.short_positions["NVDA"] = {
        "ticker": "NVDA",
        "entry_price": 500.0,
        "shares": 5,
        "stop": 510.0,
        "phase": "C",
        "v5104_entry2_fired": False,
        "entry_time": "11:05 CDT",
        "date": "2026-04-28",
    }

    try:
        snap = ds.snapshot()
        rows = snap.get("positions") or []
        by_key = {(r["ticker"], r["side"]): r for r in rows}

        long_row = by_key.get(("AAPL", "LONG"))
        assert long_row is not None
        assert long_row["phase"] == "B"
        assert long_row["entry_2_fired"] is True
        assert "sovereign_brake_distance_dollars" in long_row
        assert isinstance(long_row["sovereign_brake_distance_dollars"], (int, float))

        short_row = by_key.get(("NVDA", "SHORT"))
        assert short_row is not None
        assert short_row["phase"] == "C"
        assert short_row["entry_2_fired"] is False
        assert "sovereign_brake_distance_dollars" in short_row

        per_pos = snap.get("per_position_v510") or {}
        assert "AAPL:LONG" in per_pos
        assert per_pos["AAPL:LONG"]["phase"] == "B"
        assert per_pos["AAPL:LONG"]["entry_2_fired"] is True
        assert "NVDA:SHORT" in per_pos
        assert per_pos["NVDA:SHORT"]["phase"] == "C"
    finally:
        tg.positions.pop("AAPL", None)
        tg.short_positions.pop("NVDA", None)
