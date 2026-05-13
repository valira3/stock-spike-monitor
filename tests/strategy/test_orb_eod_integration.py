"""v9.1.0 -- EOD reversal integration tests.

Covers the boundary where the engine module meets the live runtime and
the scan loop:
  - bootstrap() builds _eod_engine alongside the morning engine
  - snapshot() includes eod block when bootstrapped
  - get_eod_engine() returns None when disabled
  - Full one-day simulation: reset -> select -> admit -> close -> snapshot
"""
import os
import pytest

from orb import live_runtime
from orb.eod_reversal import EodReversalConfig, EodReversalEngine


# ----- 1. Bootstrap integration ---------------------------------------


class TestBootstrapIntegration:
    def setup_method(self):
        live_runtime._reset_for_testing()

    def teardown_method(self):
        live_runtime._reset_for_testing()
        for k in ("ORB_EOD_REVERSAL_ENABLED",):
            os.environ.pop(k, None)

    def test_bootstrap_creates_eod_engine(self):
        live_runtime.bootstrap()
        eod = live_runtime.get_eod_engine()
        assert eod is not None
        assert isinstance(eod, EodReversalEngine)
        assert eod.cfg.enabled is True

    def test_disabled_via_env_returns_none_from_getter(self, monkeypatch):
        monkeypatch.setenv("ORB_EOD_REVERSAL_ENABLED", "0")
        live_runtime._reset_for_testing()
        live_runtime.bootstrap()
        # Engine is still constructed (so snapshot has consistent shape)
        # but get_eod_engine() returns None when disabled.
        assert live_runtime.get_eod_engine() is None

    def test_snapshot_includes_eod_block(self):
        live_runtime.bootstrap()
        snap = live_runtime.snapshot()
        assert "eod" in snap
        assert snap["eod"]["enabled"] is True
        assert "config" in snap["eod"]
        assert "per_portfolio" in snap["eod"]


# ----- 2. Full one-day flow -------------------------------------------


class TestOneDayFlow:
    def test_reset_then_admit_then_close_produces_leg(self):
        eng = EodReversalEngine(
            EodReversalConfig(), portfolio_ids=["main"],
        )
        eng.reset_for_session("2026-05-13")
        # Long pick: ORCL down -50bps from prior close
        pos = eng.admit(
            portfolio_id="main", ticker="ORCL", side="long",
            entry_price=200.0, equity=100_000.0,
            rod3_bps=-50.0, entry_iso="2026-05-13T19:30:00Z",
        )
        assert pos.shares == 175  # 35% notional / 200 = 175
        # Exit at 15:59 ET
        leg = eng.close(
            portfolio_id="main", ticker="ORCL",
            exit_price=200.50, exit_iso="2026-05-13T19:59:00Z",
            exit_reason="eod_window",
        )
        assert leg is not None
        assert leg["pnl"] == pytest.approx(175 * 0.50)   # +$87.50
        # State is consistent
        snap = eng.snapshot()
        main = snap["per_portfolio"]["main"]
        assert main["open_count"] == 0
        assert len(main["closed_legs"]) == 1
        assert main["realized_pnl_today"] == pytest.approx(87.50)
        assert main["entry_attempted"] is False  # we never called mark_attempted

    def test_select_admit_close_full_pipeline(self):
        eng = EodReversalEngine(
            EodReversalConfig(), portfolio_ids=["main"],
        )
        eng.reset_for_session("2026-05-13")
        prior = {
            "ORCL": 200, "AAPL": 200, "MSFT": 200,
            "AVGO": 200, "NFLX": 200,
        }
        # At 15:30, ORCL down 50bps, NFLX up 80bps, rest near prior
        current = {
            "ORCL": 199.0,    # -50 bps
            "AAPL": 200.20,   # +10 bps
            "MSFT": 199.80,   # -10 bps
            "AVGO": 200.10,   #  +5 bps
            "NFLX": 201.60,   # +80 bps
        }
        longs, shorts = eng.select_signals(
            current_prices=current, prior_closes=prior,
        )
        # ORCL is the most-negative -> long
        # NFLX is the most-positive (within short_tickers) -> short
        assert longs[0][0] == "ORCL"
        assert shorts[0][0] == "NFLX"

        # Admit both legs
        eng.admit(portfolio_id="main", ticker="ORCL", side="long",
                  entry_price=current["ORCL"], equity=100_000.0,
                  rod3_bps=longs[0][1], entry_iso="t1")
        eng.admit(portfolio_id="main", ticker="NFLX", side="short",
                  entry_price=current["NFLX"], equity=100_000.0,
                  rod3_bps=shorts[0][1], entry_iso="t1")
        eng.mark_attempted("main")

        snap = eng.snapshot()
        main = snap["per_portfolio"]["main"]
        assert main["open_count"] == 2
        # Pre-flatten snapshot has 0 realized P&L
        assert main["realized_pnl_today"] == 0

        # Simulate the reversal: ORCL bounces +60bps, NFLX fades -40bps
        exit_orcl = 199.0 * (1.0036)
        exit_nflx = 201.60 * (0.9960)
        eng.close(portfolio_id="main", ticker="ORCL",
                  exit_price=exit_orcl, exit_iso="t2")
        eng.close(portfolio_id="main", ticker="NFLX",
                  exit_price=exit_nflx, exit_iso="t2")

        snap = eng.snapshot()
        main = snap["per_portfolio"]["main"]
        assert main["open_count"] == 0
        assert len(main["closed_legs"]) == 2
        # Both legs should be positive (long bounced, short faded)
        assert main["realized_pnl_today"] > 0


# ----- 3. Multi-portfolio independence --------------------------------


class TestMultiPortfolio:
    def test_state_is_independent_per_portfolio(self):
        eng = EodReversalEngine(
            EodReversalConfig(), portfolio_ids=["main", "val", "gene"],
        )
        eng.reset_for_session("2026-05-13")
        # main admits, val + gene don't
        eng.admit(portfolio_id="main", ticker="ORCL", side="long",
                  entry_price=200.0, equity=100_000.0,
                  rod3_bps=-50.0, entry_iso="t")
        snap = eng.snapshot()
        assert snap["per_portfolio"]["main"]["open_count"] == 1
        assert snap["per_portfolio"]["val"]["open_count"] == 0
        assert snap["per_portfolio"]["gene"]["open_count"] == 0

    def test_mark_attempted_per_portfolio_independent(self):
        eng = EodReversalEngine(
            EodReversalConfig(), portfolio_ids=["main", "val"],
        )
        eng.reset_for_session("2026-05-13")
        eng.mark_attempted("main")
        assert eng.has_attempted("main") is True
        assert eng.has_attempted("val") is False


# ----- 4. Defaults-ON ship spec check ---------------------------------


class TestShipSpec:
    def test_v9_1_0_defaults_match_r17_winner(self):
        """The v9.1.0 ship spec (r17 backtest winner). Anyone modifying
        these defaults should re-validate the backtest first.
        """
        cfg = EodReversalConfig()
        assert cfg.enabled is True, "EOD should default ON for v9.1.0"
        assert set(cfg.universe) == {"ORCL", "AAPL", "MSFT", "AVGO", "NFLX"}
        assert set(cfg.long_tickers) == {"ORCL", "AAPL", "MSFT", "AVGO"}
        assert set(cfg.short_tickers) == {"ORCL", "NFLX", "AAPL", "MSFT"}
        assert cfg.top_n == 1
        assert cfg.notional_pct == 35.0
        assert cfg.entry_et_minutes == 15 * 60 + 30
        assert cfg.exit_et_minutes == 15 * 60 + 59
        # fire_broker defaults OFF for paper-fire-observation
        assert cfg.fire_broker is False
