"""v6.14.7 -- regression tests for phase2 vol_gate_status reflecting
the real per-ticker volume bucket state (PASS/FAIL/COLDSTART) rather
than being pinned to COLD by a silent TypeError.

Background: `_phase2_block` in v5_13_2_snapshot.py was calling
`_v510._vol_bucket_per_ticker(m, tickers, minute_hhmm)` with 3
positional args, but v5.20.5 added a required 4th arg
(`prev_minute_hhmm`). The TypeError was caught by a bare-except that
reduced `vol` to `{}`, so every ticker's `vol_gate_status` defaulted
to "COLD" -- even after v6.14.4-6 fixed every other volume surface.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import bot_version
import v5_13_2_snapshot as snap


def test_bot_version_is_6_14_7_or_newer():
    parts = [int(p) for p in bot_version.BOT_VERSION.split(".")]
    assert parts >= [6, 14, 7]


class _FakeNowET:
    """Fake `m._now_et()` returning a fixed RTH timestamp."""

    def __init__(self, dt):
        self._dt = dt

    def __call__(self):
        return self._dt


def _fake_m(now_et_dt):
    m = SimpleNamespace()
    m._now_et = _FakeNowET(now_et_dt)
    m.or_high = {}
    m.or_low = {}
    m._ws_consumer = None
    return m


def test_phase2_passes_prev_minute_hhmm_argument(monkeypatch):
    """Direct contract test: `_phase2_block` must call
    `_v510._vol_bucket_per_ticker` with FOUR positional args (the v5.20.5
    signature) so the call does not TypeError into the bare-except path.
    """
    captured = {}

    def fake_vbpt(m, tickers, minute_hhmm, prev_minute_hhmm):
        captured["minute_hhmm"] = minute_hhmm
        captured["prev_minute_hhmm"] = prev_minute_hhmm
        return {t: {"state": "PASS"} for t in tickers}

    def fake_bnd(m, tickers):
        return {t: {"state": "ARMED", "side": None} for t in tickers}

    monkeypatch.setattr(snap._v510, "_vol_bucket_per_ticker", fake_vbpt)
    monkeypatch.setattr(snap._v510, "_boundary_hold_per_ticker", fake_bnd)
    # Force VOLUME_GATE_ENABLED to True so the OFF override doesn't
    # mask the real status.
    import engine.feature_flags as ff
    monkeypatch.setattr(ff, "VOLUME_GATE_ENABLED", True, raising=False)

    now_et = datetime(2026, 5, 4, 14, 35, 0, tzinfo=timezone.utc)
    m = _fake_m(now_et)
    rows = snap._phase2_block(m, ["AAPL", "MSFT"])

    assert "minute_hhmm" in captured, (
        "_vol_bucket_per_ticker was not called -- TypeError likely caught "
        "by bare-except"
    )
    assert captured["minute_hhmm"] == "1435"
    # prev_minute_hhmm may legitimately be None (volume_profile import
    # failure, etc.) but the KEY must be present, proving the 4-arg
    # signature was used.
    assert "prev_minute_hhmm" in captured
    assert len(rows) == 2
    for row in rows:
        assert row["vol_gate_status"] == "PASS", (
            f"expected PASS, got {row['vol_gate_status']!r} for "
            f"ticker {row['ticker']}"
        )


def test_phase2_maps_fail_state_to_fail_status(monkeypatch):
    """When bb.check returns FAIL (warm baseline + low current vol),
    phase2.vol_gate_status must be FAIL, not COLD."""

    def fake_vbpt(m, tickers, minute_hhmm, prev_minute_hhmm):
        return {t: {"state": "FAIL"} for t in tickers}

    def fake_bnd(m, tickers):
        return {t: {"state": "ARMED", "side": None} for t in tickers}

    monkeypatch.setattr(snap._v510, "_vol_bucket_per_ticker", fake_vbpt)
    monkeypatch.setattr(snap._v510, "_boundary_hold_per_ticker", fake_bnd)
    import engine.feature_flags as ff
    monkeypatch.setattr(ff, "VOLUME_GATE_ENABLED", True, raising=False)

    now_et = datetime(2026, 5, 4, 14, 35, 0, tzinfo=timezone.utc)
    m = _fake_m(now_et)
    rows = snap._phase2_block(m, ["AAPL"])
    assert rows[0]["vol_gate_status"] == "FAIL"


def test_phase2_maps_pass_state_to_pass_status(monkeypatch):
    def fake_vbpt(m, tickers, minute_hhmm, prev_minute_hhmm):
        return {t: {"state": "PASS"} for t in tickers}

    def fake_bnd(m, tickers):
        return {t: {"state": "ARMED", "side": None} for t in tickers}

    monkeypatch.setattr(snap._v510, "_vol_bucket_per_ticker", fake_vbpt)
    monkeypatch.setattr(snap._v510, "_boundary_hold_per_ticker", fake_bnd)
    import engine.feature_flags as ff
    monkeypatch.setattr(ff, "VOLUME_GATE_ENABLED", True, raising=False)

    now_et = datetime(2026, 5, 4, 14, 35, 0, tzinfo=timezone.utc)
    m = _fake_m(now_et)
    rows = snap._phase2_block(m, ["NVDA"])
    assert rows[0]["vol_gate_status"] == "PASS"


def test_phase2_maps_coldstart_state_to_cold_status(monkeypatch):
    def fake_vbpt(m, tickers, minute_hhmm, prev_minute_hhmm):
        return {t: {"state": "COLDSTART"} for t in tickers}

    def fake_bnd(m, tickers):
        return {t: {"state": "ARMED", "side": None} for t in tickers}

    monkeypatch.setattr(snap._v510, "_vol_bucket_per_ticker", fake_vbpt)
    monkeypatch.setattr(snap._v510, "_boundary_hold_per_ticker", fake_bnd)
    import engine.feature_flags as ff
    monkeypatch.setattr(ff, "VOLUME_GATE_ENABLED", True, raising=False)

    now_et = datetime(2026, 5, 4, 14, 35, 0, tzinfo=timezone.utc)
    m = _fake_m(now_et)
    rows = snap._phase2_block(m, ["AAPL"])
    assert rows[0]["vol_gate_status"] == "COLD"


def test_phase2_off_override_when_gate_disabled(monkeypatch):
    """When VOLUME_GATE_ENABLED=False, status is OFF regardless of
    underlying vb.state -- this preserves the v5.20.6 behaviour."""

    def fake_vbpt(m, tickers, minute_hhmm, prev_minute_hhmm):
        return {t: {"state": "PASS"} for t in tickers}

    def fake_bnd(m, tickers):
        return {t: {"state": "ARMED", "side": None} for t in tickers}

    monkeypatch.setattr(snap._v510, "_vol_bucket_per_ticker", fake_vbpt)
    monkeypatch.setattr(snap._v510, "_boundary_hold_per_ticker", fake_bnd)
    import engine.feature_flags as ff
    monkeypatch.setattr(ff, "VOLUME_GATE_ENABLED", False, raising=False)

    now_et = datetime(2026, 5, 4, 14, 35, 0, tzinfo=timezone.utc)
    m = _fake_m(now_et)
    rows = snap._phase2_block(m, ["AAPL"])
    assert rows[0]["vol_gate_status"] == "OFF"
