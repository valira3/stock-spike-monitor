"""v5.13.2 \u2014 dashboard /api/state contract test for Track C.

Asserts the rewritten dashboard surface:

  - The standalone helper `v5_13_2_snapshot.build_tiger_sovereign_snapshot`
    returns a Phase 1\u20134 dict with the documented keys / value types,
    even when the trade_genius module has not warmed up.
  - The full `dashboard_server.snapshot()` carries `feature_flags`,
    `tiger_sovereign`, AND `shadow_data_status` (which the cron at
    `/home/user/workspace/cron_tracking/58c883b0/` depends on).

Boots the bot in SSM_SMOKE_TEST mode (no Telegram, no Alpaca, no
Polygon) so the import path is exercised end-to-end.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Standalone helper test \u2014 stub `m` module, no smoke import needed
# ---------------------------------------------------------------------------


class _StubM:
    """Minimal trade_genius surface required by build_tiger_sovereign_snapshot.

    The helper reads `_now_et`, `tiger_di`, `_QQQ_REGIME`, `fetch_1min_bars`,
    `_opening_avwap`, `or_high`, `or_low` \u2014 most via v5_10_6 helpers.
    Returning None / empty dicts everywhere exercises the defensive
    fallback paths.
    """

    BOT_VERSION = "5.13.1"
    TRADE_TICKERS: list[str] = ["AAPL", "MSFT"]
    positions: dict = {}
    short_positions: dict = {}
    or_high: dict = {}
    or_low: dict = {}

    def _now_et(self):
        from datetime import datetime, timezone

        return datetime(2026, 4, 28, 13, 30, tzinfo=timezone.utc)

    def fetch_1min_bars(self, ticker: str):
        return None

    def _opening_avwap(self, ticker: str):
        return None

    def tiger_di(self, ticker: str):
        return (None, None)


def test_build_tiger_sovereign_snapshot_shape_minimal():
    """Helper must return the documented top-level keys even with a
    cold module (no positions, no warmups). All defensive paths exercised.
    """
    sys.path.insert(0, str(REPO_ROOT))
    if "v5_13_2_snapshot" in sys.modules:
        del sys.modules["v5_13_2_snapshot"]
    import v5_13_2_snapshot as ts

    m = _StubM()
    snap = ts.build_tiger_sovereign_snapshot(
        m,
        list(m.TRADE_TICKERS),
        dict(m.positions),
        dict(m.short_positions),
        {},
    )
    assert isinstance(snap, dict)
    for k in ("phase1", "phase2", "phase3", "phase4"):
        assert k in snap, f"tiger_sovereign missing top-level key {k!r}"

    p1 = snap["phase1"]
    assert isinstance(p1, dict)
    # phase1 sub-blocks
    assert "long" in p1 and "short" in p1
    long_p = p1["long"]
    short_p = p1["short"]
    for side_blk in (long_p, short_p):
        assert isinstance(side_blk, dict)
        for k in ("qqq_5m_close", "qqq_5m_ema9", "qqq_avwap_0930", "qqq_last", "permit"):
            assert k in side_blk, f"phase1 side missing {k!r}"
        assert isinstance(side_blk["permit"], bool)

    assert isinstance(snap["phase2"], list)
    # phase2 list with two tickers, each row has the documented keys
    assert len(snap["phase2"]) == 2
    for row in snap["phase2"]:
        assert "ticker" in row
        assert row["vol_gate_status"] in {"PASS", "FAIL", "COLD", "OFF"}
        assert isinstance(row["two_consec_above"], bool)
        assert isinstance(row["two_consec_below"], bool)

    # phase3 / phase4 are empty when there are no positions
    assert snap["phase3"] == []
    assert snap["phase4"] == []


def test_build_tiger_sovereign_snapshot_with_positions():
    """When a LONG and SHORT are open, phase3 + phase4 carry the
    documented per-position rows.
    """
    sys.path.insert(0, str(REPO_ROOT))
    if "v5_13_2_snapshot" in sys.modules:
        del sys.modules["v5_13_2_snapshot"]
    import v5_13_2_snapshot as ts

    m = _StubM()
    longs = {
        "AAPL": {
            "ticker": "AAPL",
            "entry_price": 150.0,
            "shares": 10,
            "v5104_entry2_fired": True,
            "phase": "B",
            "titan_grip_state": None,
        }
    }
    shorts = {
        "NVDA": {
            "ticker": "NVDA",
            "entry_price": 500.0,
            "shares": 5,
            "v5104_entry2_fired": False,
            "phase": "C",
            "titan_grip_state": None,
        }
    }
    prices = {"AAPL": 152.0, "NVDA": 495.0}

    snap = ts.build_tiger_sovereign_snapshot(
        m,
        list(m.TRADE_TICKERS),
        longs,
        shorts,
        prices,
    )
    assert len(snap["phase3"]) == 2
    aapl = next((r for r in snap["phase3"] if r["ticker"] == "AAPL"), None)
    assert aapl is not None and aapl["side"] == "LONG"
    assert aapl["entry1_fired"] is True
    assert aapl["entry2_fired"] is True
    assert aapl["entry2_cross_pending"] is False

    nvda = next((r for r in snap["phase3"] if r["ticker"] == "NVDA"), None)
    assert nvda is not None and nvda["side"] == "SHORT"
    assert nvda["entry2_fired"] is False
    assert nvda["entry2_cross_pending"] is True

    assert len(snap["phase4"]) == 2
    aapl4 = next((r for r in snap["phase4"] if r["ticker"] == "AAPL"), None)
    assert aapl4 is not None
    assert "sentinel" in aapl4 and "titan_grip" in aapl4
    sen = aapl4["sentinel"]
    for k in (
        "a1_pnl",
        "a1_threshold",
        "a2_velocity",
        "a2_threshold",
        "b_close",
        "b_ema9",
        "b_delta",
    ):
        assert k in sen
    # A1 PnL = (152 - 150) * 10 = +20
    assert sen["a1_pnl"] == pytest.approx(20.0)
    assert sen["a1_threshold"] == pytest.approx(-500.0)
    tg = aapl4["titan_grip"]
    for k in ("stage", "anchor", "next_target", "ratchet_steps"):
        assert k in tg


def test_build_tiger_sovereign_snapshot_volume_gate_off_flag(monkeypatch):
    """When VOLUME_GATE_ENABLED is False, phase2 vol_gate_status must
    be \"OFF\" (operator-override visibility).
    """
    sys.path.insert(0, str(REPO_ROOT))
    monkeypatch.delenv("VOLUME_GATE_ENABLED", raising=False)
    # Reload feature_flags so the module-level constant picks up the
    # absence of the env var.
    if "engine.feature_flags" in sys.modules:
        del sys.modules["engine.feature_flags"]
    if "v5_13_2_snapshot" in sys.modules:
        del sys.modules["v5_13_2_snapshot"]
    import v5_13_2_snapshot as ts

    m = _StubM()
    snap = ts.build_tiger_sovereign_snapshot(
        m,
        list(m.TRADE_TICKERS),
        {},
        {},
        {},
    )
    assert all(r["vol_gate_status"] == "OFF" for r in snap["phase2"])


# ---------------------------------------------------------------------------
# Full dashboard_server.snapshot() integration test
# ---------------------------------------------------------------------------


@pytest.fixture
def smoke_module(monkeypatch):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    sys.path.insert(0, str(REPO_ROOT))
    for mod in ("trade_genius", "dashboard_server", "v5_13_2_snapshot", "engine.feature_flags"):
        if mod in sys.modules:
            del sys.modules[mod]
    import trade_genius
    import dashboard_server

    yield trade_genius, dashboard_server


def test_api_state_carries_v5_13_2_blocks(smoke_module):
    """dashboard_server.snapshot() must emit `feature_flags`,
    `tiger_sovereign`, AND `shadow_data_status`. The shadow_data_status
    field is load-bearing for the cron at
    /home/user/workspace/cron_tracking/58c883b0/ and MUST stay.
    """
    tg, ds = smoke_module
    snap = ds.snapshot()
    assert snap.get("ok") is True, f"snapshot failed: {snap}"

    # New v5.13.2 blocks
    assert "feature_flags" in snap
    ff = snap["feature_flags"]
    assert isinstance(ff, dict)
    assert "volume_gate_enabled" in ff
    assert "legacy_exits_enabled" in ff
    assert isinstance(ff["volume_gate_enabled"], bool)
    assert isinstance(ff["legacy_exits_enabled"], bool)

    assert "tiger_sovereign" in snap
    ts_blk = snap["tiger_sovereign"]
    assert isinstance(ts_blk, dict)
    for k in ("phase1", "phase2", "phase3", "phase4"):
        assert k in ts_blk

    p1 = ts_blk["phase1"]
    assert "long" in p1 and "short" in p1
    assert isinstance(p1["long"].get("permit"), bool)
    assert isinstance(p1["short"].get("permit"), bool)

    # Preserved field \u2014 cron dependency
    assert "shadow_data_status" in snap
    assert snap["shadow_data_status"] in ("live", "disabled_no_creds")

    # Backward-compat fields kept
    for k in ("section_i_permit", "per_ticker_v510", "per_position_v510"):
        assert k in snap
