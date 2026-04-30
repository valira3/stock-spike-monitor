"""v5.21.0 -- sentinel block schema tests for Track C.

Tests for the extended _sentinel_block output introduced in v5.21.0:
  - Legacy key backwards-compatibility (7 keys unchanged).
  - 6 new vAA-1 alarm sub-dicts present.
  - Correct sub-field names in each new sub-dict.
  - Alarm A_LOSS trigger logic at the -500 threshold.
  - Alarm C monotone-decreasing ADX trigger logic.
  - Alarm D HVP Lock ratio calculation and trigger.
  - Alarm E divergence trap LONG trigger.
  - EXPECTED_KEYS contract updated with 6 new keys.

No em-dashes in this file. No engine exit logic called -- snapshot is
read-only state surface only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_m():
    """Minimal trade_genius surface for _sentinel_block (only needs
    _qqq_regime_state indirectly via v5_10_6_snapshot helpers).
    """

    class _StubM:
        BOT_VERSION = "5.21.0"
        TRADE_TICKERS: list = []
        positions: dict = {}
        short_positions: dict = {}
        or_high: dict = {}
        or_low: dict = {}

        def _now_et(self):
            from datetime import datetime, timezone

            return datetime(2026, 4, 29, 13, 30, tzinfo=timezone.utc)

        def fetch_1min_bars(self, ticker: str):
            return None

        def _opening_avwap(self, ticker: str):
            return None

        def tiger_di(self, ticker: str):
            return (None, None)

    return _StubM()


def _load_snapshot():
    """Import (or reload) the snapshot module so each test gets a
    consistent state.
    """
    if "v5_13_2_snapshot" in sys.modules:
        del sys.modules["v5_13_2_snapshot"]
    import v5_13_2_snapshot as ts

    return ts


def _call_sentinel(pos: dict, side: str = "LONG", prices: dict | None = None):
    """Call _sentinel_block with a minimal position dict."""
    ts = _load_snapshot()
    m = _make_stub_m()
    return ts._sentinel_block(m, "AAPL", pos, side, prices or {"AAPL": 100.0})


# ---------------------------------------------------------------------------
# 1. Legacy keys still present
# ---------------------------------------------------------------------------


def test_legacy_keys_still_present():
    """All 7 legacy sentinel keys must remain in the output regardless
    of what new keys are added.
    """
    pos = {"entry_price": 100.0, "shares": 10}
    out = _call_sentinel(pos, "LONG", {"AAPL": 102.0})
    for key in (
        "a1_pnl",
        "a1_threshold",
        "a2_velocity",
        "a2_threshold",
        "b_close",
        "b_ema9",
        "b_delta",
    ):
        assert key in out, f"Legacy key {key!r} missing from sentinel output"


# ---------------------------------------------------------------------------
# 2. New keys present
# ---------------------------------------------------------------------------


def test_new_keys_present():
    """All 6 new vAA-1 alarm sub-dict keys must be in the output."""
    pos = {"entry_price": 100.0, "shares": 10}
    out = _call_sentinel(pos, "LONG", {"AAPL": 102.0})
    for key in (
        "a_loss",
        "a_flash",
        "b_trend_death",
        "c_velocity_ratchet",
        "d_hvp_lock",
        "e_divergence_trap",
    ):
        assert key in out, f"New key {key!r} missing from sentinel output"


# ---------------------------------------------------------------------------
# 3. New key sub-structure
# ---------------------------------------------------------------------------


def test_new_key_substructure():
    """Each new alarm sub-dict must contain all documented sub-fields."""
    pos = {"entry_price": 100.0, "shares": 10}
    out = _call_sentinel(pos, "LONG", {"AAPL": 102.0})

    # a_loss
    a_loss = out["a_loss"]
    for f in ("pnl", "threshold", "armed", "triggered"):
        assert f in a_loss, f"a_loss missing field {f!r}"

    # a_flash
    a_flash = out["a_flash"]
    for f in ("velocity_pct", "threshold_pct", "window_sec", "armed", "triggered"):
        assert f in a_flash, f"a_flash missing field {f!r}"
    assert a_flash["window_sec"] == 60

    # b_trend_death
    b_trend = out["b_trend_death"]
    for f in ("close", "ema9", "delta", "armed", "triggered", "side_aware_note"):
        assert f in b_trend, f"b_trend_death missing field {f!r}"

    # c_velocity_ratchet
    c_vel = out["c_velocity_ratchet"]
    for f in ("adx_window", "monotone_decreasing", "stop_price", "armed", "triggered"):
        assert f in c_vel, f"c_velocity_ratchet missing field {f!r}"
    assert isinstance(c_vel["adx_window"], list) and len(c_vel["adx_window"]) == 3

    # d_hvp_lock
    d_hvp = out["d_hvp_lock"]
    for f in ("trade_hvp", "current_5m_adx", "ratio", "threshold_ratio", "armed", "triggered"):
        assert f in d_hvp, f"d_hvp_lock missing field {f!r}"
    assert d_hvp["threshold_ratio"] == pytest.approx(0.75)

    # e_divergence_trap
    e_div = out["e_divergence_trap"]
    for f in (
        "stored_peak_price",
        "stored_peak_rsi",
        "current_price",
        "current_rsi_15",
        "is_extreme",
        "rsi_diverging",
        "pre_blocked_for_strike",
        "post_ratchet_stop",
        "armed",
        "triggered",
    ):
        assert f in e_div, f"e_divergence_trap missing field {f!r}"


# ---------------------------------------------------------------------------
# 4. A_LOSS trigger at threshold
# ---------------------------------------------------------------------------


def test_a_loss_triggered_at_threshold():
    """a_loss.triggered must be True when pnl <= -500 and False otherwise."""
    # pnl = (mark - entry) * shares = (90 - 155.1) * 10 = -651 (approx)
    # Use direct entry+mark arithmetic to control pnl precisely.

    # Trigger: pnl = -501 -> (mark - entry) * shares = -501
    # shares=10, entry=200, mark = 200 - 50.1 = 149.9
    pos_trigger = {"entry_price": 200.0, "shares": 10}
    out_t = _call_sentinel(pos_trigger, "LONG", {"AAPL": 149.9})
    # pnl = (149.9 - 200) * 10 = -501
    assert out_t["a_loss"]["triggered"] is True, (
        f"Expected triggered=True for pnl=-501, got {out_t['a_loss']}"
    )

    # No trigger: pnl = -499 -> mark = 200 - 49.9 = 150.1
    pos_no = {"entry_price": 200.0, "shares": 10}
    out_n = _call_sentinel(pos_no, "LONG", {"AAPL": 150.1})
    # pnl = (150.1 - 200) * 10 = -499
    assert out_n["a_loss"]["triggered"] is False, (
        f"Expected triggered=False for pnl=-499, got {out_n['a_loss']}"
    )


# ---------------------------------------------------------------------------
# 5. C monotone-decreasing
# ---------------------------------------------------------------------------


def test_c_monotone_decreasing():
    """c_velocity_ratchet.triggered must be True for strictly decreasing
    ADX window and False when equal or non-decreasing.
    """
    # Strictly decreasing: 20 > 18 > 15 -> triggered=True
    pos_dec = {
        "entry_price": 100.0,
        "shares": 10,
        "adx_1m_history": [20.0, 18.0, 15.0],
    }
    out_dec = _call_sentinel(pos_dec, "LONG", {"AAPL": 100.0})
    assert out_dec["c_velocity_ratchet"]["triggered"] is True, (
        f"Expected triggered=True for [20,18,15], got {out_dec['c_velocity_ratchet']}"
    )
    assert out_dec["c_velocity_ratchet"]["monotone_decreasing"] is True

    # Not strictly decreasing (equal last two values): [20,18,18] -> False
    pos_flat = {
        "entry_price": 100.0,
        "shares": 10,
        "adx_1m_history": [20.0, 18.0, 18.0],
    }
    out_flat = _call_sentinel(pos_flat, "LONG", {"AAPL": 100.0})
    assert out_flat["c_velocity_ratchet"]["triggered"] is False, (
        f"Expected triggered=False for [20,18,18], got {out_flat['c_velocity_ratchet']}"
    )


# ---------------------------------------------------------------------------
# 6. D HVP Lock ratio
# ---------------------------------------------------------------------------


def test_d_hvp_lock_ratio():
    """d_hvp_lock ratio = current / trade_hvp and triggered < 0.75."""
    # trade_hvp=40, current_5m_adx=29 -> ratio=29/40=0.725 -> triggered=True
    pos_t = {
        "entry_price": 100.0,
        "shares": 10,
        "trade_hvp": 40.0,
        "adx_5m_current": 29.0,
    }
    out_t = _call_sentinel(pos_t, "LONG", {"AAPL": 100.0})
    d_t = out_t["d_hvp_lock"]
    assert d_t["ratio"] == pytest.approx(0.725), f"Expected ratio=0.725, got {d_t['ratio']}"
    assert d_t["triggered"] is True, f"Expected triggered=True for ratio=0.725, got {d_t}"

    # trade_hvp=40, current_5m_adx=31 -> ratio=31/40=0.775 -> triggered=False
    pos_f = {
        "entry_price": 100.0,
        "shares": 10,
        "trade_hvp": 40.0,
        "adx_5m_current": 31.0,
    }
    out_f = _call_sentinel(pos_f, "LONG", {"AAPL": 100.0})
    d_f = out_f["d_hvp_lock"]
    assert d_f["triggered"] is False, f"Expected triggered=False for ratio=0.775, got {d_f}"


# ---------------------------------------------------------------------------
# 7. E divergence trap LONG
# ---------------------------------------------------------------------------


def test_e_divergence_long():
    """For LONG side: triggered=True when current_price > stored_peak_price
    AND current_rsi < stored_peak_rsi (classic bearish divergence).
    """
    # Setup: stored peak at price=100, rsi=70.
    # Current: price=105 (new high) AND rsi=60 (lower) -> triggered=True.
    pos_t = {
        "entry_price": 98.0,
        "shares": 10,
        "stored_peak_price": 100.0,
        "stored_peak_rsi": 70.0,
        "current_rsi_15": 60.0,
    }
    out_t = _call_sentinel(pos_t, "LONG", {"AAPL": 105.0})
    e_t = out_t["e_divergence_trap"]
    assert e_t["is_extreme"] is True, "Expected is_extreme=True"
    assert e_t["rsi_diverging"] is True, "Expected rsi_diverging=True"
    assert e_t["triggered"] is True, f"Expected triggered=True for LONG divergence, got {e_t}"

    # No trigger: rsi also above stored -> no divergence.
    pos_f = {
        "entry_price": 98.0,
        "shares": 10,
        "stored_peak_price": 100.0,
        "stored_peak_rsi": 70.0,
        "current_rsi_15": 75.0,  # higher RSI -- no divergence
    }
    out_f = _call_sentinel(pos_f, "LONG", {"AAPL": 105.0})
    e_f = out_f["e_divergence_trap"]
    assert e_f["triggered"] is False, f"Expected triggered=False when RSI not diverging, got {e_f}"


# ---------------------------------------------------------------------------
# 8. EXPECTED_KEYS contract updated
# ---------------------------------------------------------------------------


def test_expected_keys_contract_updated():
    """EXPECTED_KEYS['sentinel'] must contain all 6 new vAA-1 alarm keys."""
    if "v5_13_2_snapshot" in sys.modules:
        del sys.modules["v5_13_2_snapshot"]
    import v5_13_2_snapshot as ts

    assert hasattr(ts, "EXPECTED_KEYS"), "EXPECTED_KEYS not found in v5_13_2_snapshot"
    sentinel_keys = ts.EXPECTED_KEYS["sentinel"]
    for key in (
        "a_loss",
        "a_flash",
        "b_trend_death",
        "c_velocity_ratchet",
        "d_hvp_lock",
        "e_divergence_trap",
    ):
        assert key in sentinel_keys, f"EXPECTED_KEYS['sentinel'] missing new key {key!r}"
    # Legacy keys must still be present too.
    for key in (
        "a1_pnl",
        "a1_threshold",
        "a2_velocity",
        "a2_threshold",
        "b_close",
        "b_ema9",
        "b_delta",
    ):
        assert key in sentinel_keys, f"EXPECTED_KEYS['sentinel'] missing legacy key {key!r}"
