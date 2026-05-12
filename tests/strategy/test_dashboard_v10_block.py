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
        # v8.1.5 -- field renamed trades_per_124d -> trades_per_year
        # since the baseline is now the 251-day FY backtest, not the
        # 124-day v11 in-sample window.
        required = {
            "in_sample_cagr_pct",
            "honest_cagr_low_pct",
            "honest_cagr_mid_pct",
            "honest_cagr_high_pct",
            "sharpe_ann",
            "max_drawdown_pct",
            "win_rate_pct",
            "trades_per_year",
            "worst_day_dollars",
            "starting_balance",
            "in_sample_ending_balance",
            "in_sample_period_days",
        }
        assert set(_V10_PROJECTION_KEYSTONE.keys()) >= required

    def test_keystone_values_match_v10_canonical(self, dashboard_module):
        """v8.1.5 -- reflects the v8.1.3-active config (risk=1.0% +
        atr_stop_mult=1.75 + partial_profit_at_1r=True) over the
        full 251-day RTH corpus per
        docs/pl_optimization_final_report_v12.md R8 winner."""
        from dashboard_server import _V10_PROJECTION_KEYSTONE
        assert _V10_PROJECTION_KEYSTONE["in_sample_cagr_pct"] == 44.4
        # Sharpe deliberately nulled until recomputed for v8.1.3
        # config (old 2.85 was pre-v7.109 and would mislead).
        # renderV10Projection renders None as "-".
        assert _V10_PROJECTION_KEYSTONE["sharpe_ann"] is None
        assert _V10_PROJECTION_KEYSTONE["max_drawdown_pct"] == 3.20
        assert _V10_PROJECTION_KEYSTONE["win_rate_pct"] == 59.0
        assert _V10_PROJECTION_KEYSTONE["starting_balance"] == 100_000.0
        assert _V10_PROJECTION_KEYSTONE["in_sample_ending_balance"] == 144_431.0
        assert _V10_PROJECTION_KEYSTONE["in_sample_period_days"] == 251

    def test_keystone_low_lt_mid_lt_high(self, dashboard_module):
        from dashboard_server import _V10_PROJECTION_KEYSTONE
        assert (_V10_PROJECTION_KEYSTONE["honest_cagr_low_pct"]
                < _V10_PROJECTION_KEYSTONE["honest_cagr_mid_pct"]
                < _V10_PROJECTION_KEYSTONE["honest_cagr_high_pct"])
