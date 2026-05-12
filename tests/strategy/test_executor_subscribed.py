"""v8.3.13 -- /api/state.portfolios.{pid}.subscribed tests.

The watchdog's val_gene_trades_match_main invariant fires when Val's
broker trade count diverges from Main's. The most common root cause
is a Val executor that failed to register its _on_signal callback
on the signal bus -- making Main's emits go into the void. Pre-
v8.3.13 this required grep-the-Railway-logs archaeology to diagnose.
v8.3.13 surfaces a `subscribed` boolean per executor on /api/state.

These tests exercise the _is_executor_subscribed helper in isolation
against a fake _ssm() return value.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dashboard_server import _is_executor_subscribed


@pytest.fixture
def fake_ssm(monkeypatch):
    """Patch dashboard_server._ssm to return a configurable fake module
    whose signal_bus_status() yields configurable listener names."""
    import dashboard_server
    fake = SimpleNamespace()
    fake.signal_bus_status = lambda: {"n_listeners": 0, "names": []}
    monkeypatch.setattr(dashboard_server, "_ssm", lambda: fake)
    return fake


class TestIsExecutorSubscribed:

    def test_main_always_subscribed(self, fake_ssm):
        """Main is the emitter, not a listener; we surface True for
        shape symmetry."""
        assert _is_executor_subscribed("main") is True

    def test_val_subscribed_when_listener_registered(self, fake_ssm):
        fake_ssm.signal_bus_status = lambda: {
            "n_listeners": 1,
            "names": ["TradeGeniusVal._on_signal"],
        }
        assert _is_executor_subscribed("val") is True

    def test_gene_subscribed_when_listener_registered(self, fake_ssm):
        fake_ssm.signal_bus_status = lambda: {
            "n_listeners": 1,
            "names": ["TradeGeniusGene._on_signal"],
        }
        assert _is_executor_subscribed("gene") is True

    def test_val_not_subscribed_when_only_gene_listening(self, fake_ssm):
        """The exact operator scenario: Gene mirrored fine but Val
        skipped (missing VAL_ALPACA_PAPER_KEY)."""
        fake_ssm.signal_bus_status = lambda: {
            "n_listeners": 1,
            "names": ["TradeGeniusGene._on_signal"],
        }
        assert _is_executor_subscribed("val") is False
        assert _is_executor_subscribed("gene") is True

    def test_both_subscribed(self, fake_ssm):
        fake_ssm.signal_bus_status = lambda: {
            "n_listeners": 2,
            "names": [
                "TradeGeniusVal._on_signal",
                "TradeGeniusGene._on_signal",
            ],
        }
        assert _is_executor_subscribed("val") is True
        assert _is_executor_subscribed("gene") is True

    def test_neither_subscribed(self, fake_ssm):
        """Boot failure where neither Val nor Gene start()ed."""
        # signal_bus_status returns empty names list by default in fixture
        assert _is_executor_subscribed("val") is False
        assert _is_executor_subscribed("gene") is False

    def test_unknown_pid_returns_false(self, fake_ssm):
        assert _is_executor_subscribed("alice") is False
        assert _is_executor_subscribed("") is False

    def test_robust_to_ssm_returning_none(self, monkeypatch):
        import dashboard_server
        monkeypatch.setattr(dashboard_server, "_ssm", lambda: None)
        assert _is_executor_subscribed("val") is False
        # Main short-circuits before consulting _ssm
        assert _is_executor_subscribed("main") is True

    def test_robust_to_signal_bus_status_raising(self, fake_ssm):
        def _boom():
            raise RuntimeError("simulated failure")
        fake_ssm.signal_bus_status = _boom
        assert _is_executor_subscribed("val") is False

    def test_robust_to_missing_signal_bus_status(self, monkeypatch):
        """Old trade_genius without signal_bus_status (pre-v7.90.0)."""
        import dashboard_server
        # No signal_bus_status attr at all on the fake module
        fake = SimpleNamespace()
        monkeypatch.setattr(dashboard_server, "_ssm", lambda: fake)
        assert _is_executor_subscribed("val") is False

    def test_qualname_match_is_strict(self, fake_ssm):
        """Names that start with the class prefix but aren't
        _on_signal don't count (defensive against future helper
        methods like TradeGeniusVal.start landing on the bus by
        mistake)."""
        fake_ssm.signal_bus_status = lambda: {
            "n_listeners": 1,
            "names": ["TradeGeniusVal.start"],
        }
        assert _is_executor_subscribed("val") is False
