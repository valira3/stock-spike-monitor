"""tests/test_gate_smoke.py - Smoke test: gate in dry_run logs ALLOW/BLOCK without affecting trading.

Scenario: SSM_INGEST_GATE_MODE=enforce, green ingest health -> decision=allow.
Also verifies dry_run mode never blocks even when SLA is RED.

No em-dashes in this file per team rules.
"""
import os
import sys
import importlib
import time
from collections import deque
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def reset_modules():
    """Reload gate + sla modules for a clean state."""
    import ingest.sla as sla_mod
    import engine.ingest_gate as gate_mod
    importlib.reload(sla_mod)
    importlib.reload(gate_mod)
    sla_mod._health_states.clear()
    sla_mod._raw_metrics.clear()
    yield
    sla_mod._health_states.clear()
    sla_mod._raw_metrics.clear()


def _seed_green(ticker):
    import ingest.sla as sla_mod
    from ingest.sla import IngestHealthState
    sla_mod._health_states[ticker] = IngestHealthState(
        ticker=ticker,
        color="green",
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


def _seed_red(ticker):
    import ingest.sla as sla_mod
    from ingest.sla import IngestHealthState
    sla_mod._health_states[ticker] = IngestHealthState(
        ticker=ticker,
        color="red",
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


class TestGateSmokeGreenAllow:
    def test_enforce_green_ingest_returns_allow(self):
        """Core smoke test: enforce mode + green ingest = allow decision."""
        _seed_green("AAPL")
        from engine.ingest_gate import evaluate_gate
        with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
            decision = evaluate_gate("AAPL")
        assert decision.decision == "allow"
        assert decision.gate_mode == "enforce"
        assert decision.ticker == "AAPL"

    def test_default_dry_run_green_ingest_returns_allow(self):
        """Default mode (dry_run) + green ingest = allow."""
        _seed_green("TSLA")
        env = {k: v for k, v in os.environ.items() if k not in (
            "SSM_INGEST_GATE_MODE", "SSM_INGEST_GATE_DISABLED"
        )}
        with patch.dict(os.environ, env, clear=True):
            from engine.ingest_gate import evaluate_gate
            decision = evaluate_gate("TSLA")
        assert decision.decision == "allow"


class TestGateSmokeRedDryRun:
    def test_dry_run_red_ingest_never_blocks(self):
        """Decision A5: dry_run never blocks even when ingest is RED past threshold."""
        _seed_red("NVDA")
        import ingest_config
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 0.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "dry_run"}):
                with patch("ingest.sla._is_rth", return_value=True):
                    from engine.ingest_gate import evaluate_gate
                    decision = evaluate_gate("NVDA")
        assert decision.decision == "allow"
        assert decision.gate_mode == "dry_run"
        assert decision.overridden is True

    def test_dry_run_block_decision_is_logged_not_returned(self):
        """In dry_run mode the raw decision would be block, but decision field is allow."""
        _seed_red("NVDA")
        import ingest_config
        import logging
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 0.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "dry_run"}):
                with patch("ingest.sla._is_rth", return_value=True):
                    with patch("engine.ingest_gate._write_audit") as mock_audit:
                        from engine.ingest_gate import evaluate_gate
                        decision = evaluate_gate("NVDA")
                        # Audit SHOULD have been written (with block)
                        mock_audit.assert_called_once()
                        audit_arg = mock_audit.call_args[0][0]
                        assert audit_arg.decision == "block"
        # But the returned decision is allow
        assert decision.decision == "allow"


class TestGateSmokeEnforceBlocks:
    def test_enforce_mode_red_above_threshold_blocks(self):
        """enforce + RED past threshold = block returned to caller."""
        _seed_red("SPY")
        import ingest_config
        with patch.object(ingest_config, "SLA_GATE_RED_MIN", 0.0):
            with patch.dict(os.environ, {"SSM_INGEST_GATE_MODE": "enforce"}):
                with patch("ingest.sla._is_rth", return_value=True):
                    from engine.ingest_gate import evaluate_gate
                    decision = evaluate_gate("SPY")
        assert decision.decision == "block"
        assert decision.gate_mode == "enforce"
        assert decision.overridden is False
