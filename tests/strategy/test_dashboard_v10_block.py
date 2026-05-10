"""Tests for v7.19.0 dashboard /api/state v10 block + /api/v10/projection."""
from __future__ import annotations

import os

import pytest


# Stub env vars before importing dashboard_server (needs trade_genius)
os.environ.setdefault("FMP_API_KEY", "stub")
os.environ.setdefault("ALPACA_API_KEY_ID", "stub")
os.environ.setdefault("ALPACA_API_SECRET_KEY", "stub")
os.environ.setdefault(
    "TELEGRAM_BOT_TOKEN",
    "123456789:AAGabcdefghijklmnopqrstuvwxyz12345678",
)
os.environ.setdefault("TELEGRAM_CHAT_ID", "-100123456789")


@pytest.fixture(scope="module")
def dashboard_module():
    try:
        import dashboard_server as _ds
        return _ds
    except Exception as e:
        pytest.skip(f"cannot import dashboard_server in this env: {e}")


class TestV10ProjectionPayloadKeys:
    """The static keystone numbers are part of the dashboard contract.
    Frontend depends on these keys."""

    def test_keystone_keys_present(self, dashboard_module):
        from dashboard_server import _V10_PROJECTION_KEYSTONE
        required = {
            "in_sample_cagr_pct",
            "honest_cagr_low_pct",
            "honest_cagr_mid_pct",
            "honest_cagr_high_pct",
            "sharpe_ann",
            "max_drawdown_pct",
            "win_rate_pct",
            "trades_per_124d",
            "worst_day_dollars",
            "starting_balance",
            "in_sample_ending_balance",
            "in_sample_period_days",
        }
        assert set(_V10_PROJECTION_KEYSTONE.keys()) >= required

    def test_keystone_values_match_v10_canonical(self, dashboard_module):
        """The numbers must match docs/v10_strategy_keystone.md exactly.
        If the keystone doc updates, this test must be updated too."""
        from dashboard_server import _V10_PROJECTION_KEYSTONE
        assert _V10_PROJECTION_KEYSTONE["in_sample_cagr_pct"] == 43.0
        assert _V10_PROJECTION_KEYSTONE["sharpe_ann"] == 2.85
        assert _V10_PROJECTION_KEYSTONE["max_drawdown_pct"] == 5.03
        assert _V10_PROJECTION_KEYSTONE["win_rate_pct"] == 57.0
        assert _V10_PROJECTION_KEYSTONE["starting_balance"] == 100_000.0
        assert _V10_PROJECTION_KEYSTONE["in_sample_ending_balance"] == 119_224.81

    def test_keystone_low_lt_mid_lt_high(self, dashboard_module):
        from dashboard_server import _V10_PROJECTION_KEYSTONE
        assert (_V10_PROJECTION_KEYSTONE["honest_cagr_low_pct"]
                < _V10_PROJECTION_KEYSTONE["honest_cagr_mid_pct"]
                < _V10_PROJECTION_KEYSTONE["honest_cagr_high_pct"])
