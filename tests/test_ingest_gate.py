"""tests/test_ingest_gate.py - Unit tests for engine/ingest_gate.py hysteresis + gate logic.

Covers two-state gate transitions, dry_run mode, hysteresis timing,
SSM_INGEST_GATE_DISABLED override, and fail-open behavior.

No em-dashes in this file per team rules.
"""
import os
import sys
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _reset_gate():
    """Reset engine.ingest_gate module-level state between tests."""
    import importlib
    import engine.ingest_gate as gate_mod
    importlib.reload(gate_mod)
    return gate_mod


def _seed_sla_color(ticker, color):
    """Seed the SLA module with a specific color for a ticker."""
    import importlib
    import ingest.sla as sla_mod
    importlib.reload(sla_mod)
    # Directly inject into _health_states
    from ingest.sla import IngestHealthState
    from collections import deque
    sla_mod._health_states[ticker] = IngestHealthState(
        ticker=ticker,
        color=color,
        metrics=[],
        entered_color_at=time.monotonic(),
        transition_history=deque(maxlen=20),
    )
    sla_mod._health_states[None] = IngestHealthState(
        ticker=None,
        color="green",
        metrics=[],
        entered_color_at=time.monotonic(),
        transition_history=deque(maxlen=20),
    )
    return sla_mod


class TestGateModeOff:
    def test_off_mode_always_allows(self):
        gate = _reset_gate()
        with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "off"}):
            decision = gate.evaluate_gate("AAPL")
        assert decision.decision == "allow"
        assert decision.overridden is True
        assert decision.gate_mode == "off"

    def test_disabled_env_var_equiv_to_off(self):
        gate = _reset_gate()
        with patch.dict(os.environ, {
            "SSM_INGEST_GATE_DISABLED": "1",
            "SSM_INGEST_GATE_MODE": "enforce",
        }):
            decision = gate.evaluate_gate("AAPL")
        assert decision.decision == "allow"
        assert decision.gate_mode == "off"


class TestDryRunMode:
    def test_dry_run_always_allows_even_when_red(self):
        gate = _reset_gate()
        _seed_sla_color("NVDA", "red")
        # Advance past the hysteresis threshold by mocking time
        import ingest_config
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 0.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "dry_run"}):
                decision = gate.evaluate_gate("NVDA")
        # dry_run: always allow even if blocked
        assert decision.decision == "allow"
        assert decision.gate_mode == "dry_run"
        assert decision.overridden is True

    def test_dry_run_green_returns_allow(self):
        gate = _reset_gate()
        _seed_sla_color("AAPL", "green")
        with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "dry_run"}):
            decision = gate.evaluate_gate("AAPL")
        assert decision.decision == "allow"


class TestEnforceMode:
    def test_enforce_green_returns_allow(self):
        gate = _reset_gate()
        _seed_sla_color("TSLA", "green")
        with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
            decision = gate.evaluate_gate("TSLA")
        assert decision.decision == "allow"
        assert decision.overridden is False

    def test_enforce_red_below_threshold_still_allows(self):
        """RED spike shorter than SLA_GATE_RED_MIN does not trigger BLOCK."""
        gate = _reset_gate()
        _seed_sla_color("TSLA", "red")
        import ingest_config
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 300.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                decision = gate.evaluate_gate("TSLA")
        # Red timer has not yet crossed 300 s
        assert decision.decision == "allow"

    def test_enforce_red_above_threshold_blocks(self):
        """After RED held for > SLA_GATE_RED_MIN, gate enters BLOCKED."""
        gate = _reset_gate()
        _seed_sla_color("TSLA", "red")
        import ingest_config
        # Set threshold to 0 so first call with red immediately blocks
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 0.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                with patch("ingest.sla._is_rth", return_value=True):
                    # First call seeds red_since; with 0s threshold, immediately blocked
                    decision = gate.evaluate_gate("TSLA")
        assert decision.decision == "block"
        assert decision.overridden is False


class TestHysteresis:
    def test_red_spike_shorter_than_threshold_does_not_block(self):
        """A transient RED that resolves before the threshold fires no block."""
        gate = _reset_gate()
        _seed_sla_color("AAPL", "red")
        import ingest_config
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 300.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                d = gate.evaluate_gate("AAPL")
        assert d.decision == "allow"

    def test_green_held_for_threshold_unblocks(self):
        """After BLOCK, GREEN held for SLA_GATE_GREEN_MIN exits BLOCKED."""
        gate = _reset_gate()
        _seed_sla_color("AAPL", "red")
        import ingest_config
        # Set both thresholds to 0 for instant transitions
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 0.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                with patch("ingest.sla._is_rth", return_value=True):
                    d1 = gate.evaluate_gate("AAPL")
        assert d1.decision == "block"
        # Now flip to green
        _seed_sla_color("AAPL", "green")
        with patch.object(ingest_config, "SLA_GATE_GREEN_MIN", 0.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                with patch("ingest.sla._is_rth", return_value=True):
                    d2 = gate.evaluate_gate("AAPL")
        assert d2.decision == "allow"

    def test_green_held_below_threshold_stays_blocked(self):
        """GREEN spike shorter than SLA_GATE_GREEN_MIN does not unblock."""
        gate = _reset_gate()
        _seed_sla_color("AAPL", "red")
        import ingest_config
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 0.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                with patch("ingest.sla._is_rth", return_value=True):
                    gate.evaluate_gate("AAPL")  # enter BLOCKED
        # Flip to green but green threshold is high
        _seed_sla_color("AAPL", "green")
        with patch.object(ingest_config, "SLA_GATE_GREEN_MIN", 9999.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                with patch("ingest.sla._is_rth", return_value=True):
                    d2 = gate.evaluate_gate("AAPL")
        assert d2.decision == "block"


class TestFailOpen:
    def test_sla_exception_returns_allow(self):
        """If get_health_state raises, gate fails open (allow)."""
        gate = _reset_gate()
        with patch("ingest.sla.get_health_state", side_effect=RuntimeError("sla down")):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                decision = gate.evaluate_gate("AAPL")
        assert decision.decision == "allow"


class TestOutsideRTH:
    def test_outside_rth_always_allows_regardless_of_color(self):
        """Decision P3: outside RTH, gate always allows (no gating)."""
        gate = _reset_gate()
        _seed_sla_color("AAPL", "red")
        import ingest_config
        from datetime import datetime
        from zoneinfo import ZoneInfo
        premarket = datetime(2026, 5, 4, 8, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 0.0):
            with patch("ingest.sla._is_rth", return_value=False):
                with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                    decision = gate.evaluate_gate("AAPL")
        assert decision.decision == "allow"
