"""v9.1.0 -- EOD reversal addon tests.

Covers:
  1. EodReversalConfig env parsing (defaults ON, overrides work)
  2. EodReversalEngine state lifecycle (reset, admit, close, snapshot)
  3. select_signals: ranking + per-side fence + top-N cap
  4. Entry / exit window detection
  5. Idempotent admit (no double-fire) + idempotent reset
  6. Snapshot shape matches dashboard expectations
"""

import os
import pytest

from orb.eod_reversal import (
    EodPosition,
    EodReversalConfig,
    EodReversalEngine,
    EodSessionState,
    _fmt_et,
)


# ----- 1. Config ------------------------------------------------------


class TestConfigDefaults:
    def test_defaults_match_r17_winner(self):
        cfg = EodReversalConfig()
        assert cfg.enabled is True
        assert cfg.universe == ("ORCL", "AAPL", "MSFT", "AVGO", "NFLX")
        assert cfg.long_tickers == ("ORCL", "AAPL", "MSFT", "AVGO")
        assert cfg.short_tickers == ("ORCL", "NFLX", "AAPL", "MSFT")
        assert cfg.top_n == 1
        assert cfg.notional_pct == 35.0
        # v9.1.2: entry moved 15:30 -> 15:00 per the R18c sweep.
        assert cfg.entry_et_minutes == 15 * 60
        # v9.1.109: exit moved 15:59 -> 15:58 to align with eod_close flush.
        assert cfg.exit_et_minutes == 15 * 60 + 58
        # v9.1.108/9: entry cutoff 15:51 (exclusive) = last valid entry 15:50.
        assert cfg.entry_cutoff_et_minutes == 15 * 60 + 51
        # v9.1.1: live broker firing is the default (was False in v9.1.0).
        assert cfg.fire_broker is True


class TestConfigFromEnv:
    def test_env_overrides_take_effect(self, monkeypatch):
        monkeypatch.setenv("ORB_EOD_REVERSAL_ENABLED", "0")
        monkeypatch.setenv("ORB_EOD_UNIVERSE", "AAPL,MSFT")
        monkeypatch.setenv("ORB_EOD_LONG_TICKERS", "AAPL")
        monkeypatch.setenv("ORB_EOD_SHORT_TICKERS", "MSFT")
        monkeypatch.setenv("ORB_EOD_TOP_N", "2")
        monkeypatch.setenv("ORB_EOD_NOTIONAL_PCT", "50")
        monkeypatch.setenv("ORB_EOD_ENTRY_ET", "15:00")
        monkeypatch.setenv("ORB_EOD_EXIT_ET", "16:00")
        monkeypatch.setenv("ORB_EOD_FIRE_BROKER", "1")
        cfg = EodReversalConfig.from_env()
        assert cfg.enabled is False
        assert cfg.universe == ("AAPL", "MSFT")
        assert cfg.long_tickers == ("AAPL",)
        assert cfg.short_tickers == ("MSFT",)
        assert cfg.top_n == 2
        assert cfg.notional_pct == 50.0
        assert cfg.entry_et_minutes == 15 * 60
        assert cfg.exit_et_minutes == 16 * 60
        assert cfg.fire_broker is True

    def test_env_missing_falls_back_to_defaults(self, monkeypatch):
        # Clear any inherited env vars
        for k in (
            "ORB_EOD_REVERSAL_ENABLED",
            "ORB_EOD_UNIVERSE",
            "ORB_EOD_LONG_TICKERS",
            "ORB_EOD_SHORT_TICKERS",
            "ORB_EOD_TOP_N",
            "ORB_EOD_NOTIONAL_PCT",
            "ORB_EOD_ENTRY_ET",
            "ORB_EOD_EXIT_ET",
            "ORB_EOD_FIRE_BROKER",
        ):
            monkeypatch.delenv(k, raising=False)
        cfg = EodReversalConfig.from_env()
        assert cfg.enabled is True
        assert cfg.fire_broker is True  # v9.1.1: live by default
        assert cfg.top_n == 1

    def test_env_can_disable_broker_fire(self, monkeypatch):
        """Operator escape hatch: setting ORB_EOD_FIRE_BROKER=0 reverts
        to paper-tracking mode without a deploy."""
        monkeypatch.setenv("ORB_EOD_FIRE_BROKER", "0")
        cfg = EodReversalConfig.from_env()
        assert cfg.fire_broker is False

    def test_malformed_et_falls_back(self, monkeypatch):
        monkeypatch.setenv("ORB_EOD_ENTRY_ET", "garbage")
        cfg = EodReversalConfig.from_env()
        assert cfg.entry_et_minutes == 15 * 60


# ----- 2. Engine lifecycle --------------------------------------------


def _eng() -> EodReversalEngine:
    return EodReversalEngine(EodReversalConfig(), portfolio_ids=["main", "val"])


class TestEngineLifecycle:
    def test_reset_initializes_state(self):
        e = _eng()
        e.reset_for_session("2026-05-13")
        assert e._session_date == "2026-05-13"
        for pid in ("main", "val"):
            st = e._states[pid]
            assert st.date_iso == "2026-05-13"
            assert st.entry_attempted is False
            assert st.open_positions == {}
            assert st.realized_pnl_today == 0.0
            assert st.closed_legs == []

    def test_reset_is_idempotent_same_date(self):
        e = _eng()
        e.reset_for_session("2026-05-13")
        # Mutate state
        e._states["main"].entry_attempted = True
        e.reset_for_session("2026-05-13")
        # State preserved (no reset since same date)
        assert e._states["main"].entry_attempted is True

    def test_reset_clears_on_new_date(self):
        e = _eng()
        e.reset_for_session("2026-05-13")
        e._states["main"].entry_attempted = True
        e.reset_for_session("2026-05-14")
        assert e._states["main"].entry_attempted is False


# ----- 3. Signal selection --------------------------------------------


class TestSelectSignals:
    def _setup(self) -> EodReversalEngine:
        e = _eng()
        e.reset_for_session("2026-05-13")
        return e

    def test_ranks_lowest_rod3_as_long_pick(self):
        e = self._setup()
        # ROD3: ORCL +50 bps, AAPL -100 bps, MSFT +20, AVGO -50, NFLX +30
        prior = {"ORCL": 100, "AAPL": 100, "MSFT": 100, "AVGO": 100, "NFLX": 100}
        current = {"ORCL": 100.5, "AAPL": 99.0, "MSFT": 100.2, "AVGO": 99.5, "NFLX": 100.3}
        longs, shorts = e.select_signals(
            current_prices=current,
            prior_closes=prior,
        )
        # AAPL is the lowest ROD3 (-100bps) -> long pick
        assert longs[0][0] == "AAPL"
        # ORCL is highest of short-eligible -> short pick (since ORCL = +50bps, NFLX = +30, AAPL = -100, MSFT = +20)
        # short_tickers = ORCL,NFLX,AAPL,MSFT -- excludes AVGO
        # highest of these by ROD3 = ORCL (+50)
        assert shorts[0][0] == "ORCL"

    def test_long_fence_excludes_non_listed(self):
        e = self._setup()
        # AVGO is in universe + long_tickers; NFLX in universe but not long
        prior = {"AAPL": 100, "AVGO": 100, "NFLX": 100, "ORCL": 100, "MSFT": 100}
        current = {"AAPL": 100, "AVGO": 100, "NFLX": 99.0, "ORCL": 100, "MSFT": 100}
        # NFLX is most-negative but not in long_tickers -> should NOT be picked
        longs, _ = e.select_signals(
            current_prices=current,
            prior_closes=prior,
        )
        assert all(t != "NFLX" for t, _ in longs)

    def test_short_fence_excludes_non_listed(self):
        e = self._setup()
        prior = {"AAPL": 100, "AVGO": 100, "NFLX": 100, "ORCL": 100, "MSFT": 100}
        current = {"AAPL": 100, "AVGO": 101.0, "NFLX": 100, "ORCL": 100, "MSFT": 100}
        # AVGO is most-positive but not in short_tickers -> should NOT be picked
        _, shorts = e.select_signals(
            current_prices=current,
            prior_closes=prior,
        )
        assert all(t != "AVGO" for t, _ in shorts)

    def test_insufficient_data_returns_empty(self):
        e = self._setup()
        longs, shorts = e.select_signals(
            current_prices={"ORCL": 100},
            prior_closes={"ORCL": 100},
        )
        assert longs == [] and shorts == []

    def test_missing_prior_close_skipped(self):
        e = self._setup()
        prior = {"ORCL": 100}  # only ORCL has a prior
        current = {"ORCL": 100, "AAPL": 101}
        longs, shorts = e.select_signals(
            current_prices=current,
            prior_closes=prior,
        )
        # Only 1 ticker eligible -> insufficient
        assert longs == [] and shorts == []


# ----- 4. Admission + close -------------------------------------------


class TestAdmission:
    def _seeded(self):
        e = _eng()
        e.reset_for_session("2026-05-13")
        return e

    def test_admit_computes_shares_from_notional(self):
        e = self._seeded()
        pos = e.admit(
            portfolio_id="main",
            ticker="ORCL",
            side="long",
            entry_price=200.0,
            equity=100_000.0,
            rod3_bps=-50.0,
            entry_iso="2026-05-13T15:30:00Z",
        )
        assert pos is not None
        # 35% notional of 100k = $35,000; 200 -> 175 shares
        assert pos.shares == 175
        assert pos.side == "long"
        assert pos.notional_at_entry == 200.0 * 175

    def test_admit_idempotent_same_ticker(self):
        e = self._seeded()
        p1 = e.admit(
            portfolio_id="main",
            ticker="ORCL",
            side="long",
            entry_price=200.0,
            equity=100_000.0,
            rod3_bps=-50.0,
            entry_iso="t",
        )
        p2 = e.admit(
            portfolio_id="main",
            ticker="ORCL",
            side="long",
            entry_price=205.0,
            equity=100_000.0,
            rod3_bps=-60.0,
            entry_iso="t",
        )
        assert p1 is p2  # same object, no overwrite

    def test_admit_zero_price_returns_none(self):
        e = self._seeded()
        pos = e.admit(
            portfolio_id="main",
            ticker="ORCL",
            side="long",
            entry_price=0.0,
            equity=100_000.0,
            rod3_bps=-50.0,
            entry_iso="t",
        )
        assert pos is None

    def test_close_long_pnl_correct(self):
        e = self._seeded()
        e.admit(
            portfolio_id="main",
            ticker="ORCL",
            side="long",
            entry_price=200.0,
            equity=100_000.0,
            rod3_bps=-50.0,
            entry_iso="t",
        )
        leg = e.close(portfolio_id="main", ticker="ORCL", exit_price=201.0, exit_iso="t2")
        assert leg is not None
        assert leg["pnl"] == pytest.approx(175 * (201.0 - 200.0))
        assert e._states["main"].realized_pnl_today == pytest.approx(175.0)

    def test_close_short_pnl_correct(self):
        e = self._seeded()
        e.admit(
            portfolio_id="main",
            ticker="NFLX",
            side="short",
            entry_price=500.0,
            equity=100_000.0,
            rod3_bps=80.0,
            entry_iso="t",
        )
        # 35% notional of 100k / 500 -> 70 shares
        leg = e.close(portfolio_id="main", ticker="NFLX", exit_price=498.0, exit_iso="t2")
        assert leg["pnl"] == pytest.approx(70 * (500.0 - 498.0))

    def test_close_nonexistent_returns_none(self):
        e = self._seeded()
        leg = e.close(portfolio_id="main", ticker="ORCL", exit_price=100.0, exit_iso="t")
        assert leg is None

    def test_mark_attempted_flag(self):
        e = self._seeded()
        assert e.has_attempted("main") is False
        e.mark_attempted("main")
        assert e.has_attempted("main") is True


# ----- 5. Time-window predicates --------------------------------------


class TestTimeWindows:
    def test_entry_window_default_15_00(self):
        # v9.1.2: entry default moved from 15:30 to 15:00.
        # v9.1.22: widened from a single minute to [entry_et, exit_et)
        # so a delayed scan-loop tick (deploy, cron miss, restart) can
        # still land the entry. Idempotency is via the per-portfolio
        # `entry_attempted` flag (scan.py:1390), not the time check.
        # v9.1.108/9: entry window [15:00, 15:51) -- 15:50 is last valid
        # entry minute; 15:51+ blocked. Exit at 15:58.
        e = _eng()
        # Boundary BELOW entry_et stays False.
        assert e.is_entry_window(14 * 60 + 59) is False
        # Entry-minute open is True.
        assert e.is_entry_window(15 * 60) is True
        # Any minute in [entry_et, entry_cutoff_et) is True.
        assert e.is_entry_window(15 * 60 + 1) is True
        assert e.is_entry_window(15 * 60 + 30) is True
        # 15:50 is the last valid entry minute (entry_cutoff exclusive = 15:51).
        assert e.is_entry_window(15 * 60 + 50) is True
        # 15:51+ is blocked.
        assert e.is_entry_window(15 * 60 + 51) is False
        assert e.is_entry_window(15 * 60 + 58) is False

    def test_exit_window_inclusive_after_15_58(self):
        # v9.1.109: exit_et moved 15:59 -> 15:58 to align with eod_close flush.
        e = _eng()
        assert e.is_exit_window(15 * 60 + 57) is False
        assert e.is_exit_window(15 * 60 + 58) is True
        assert e.is_exit_window(16 * 60) is True  # late ticks still flatten


# ----- 6. Snapshot ----------------------------------------------------


class TestSnapshot:
    def test_snapshot_has_required_keys(self):
        e = _eng()
        e.reset_for_session("2026-05-13")
        snap = e.snapshot()
        assert snap["enabled"] is True
        assert snap["session_date"] == "2026-05-13"
        assert "config" in snap and "per_portfolio" in snap
        for pid in ("main", "val"):
            assert pid in snap["per_portfolio"]
            p = snap["per_portfolio"][pid]
            assert "open_count" in p and "open_positions" in p
            assert "realized_pnl_today" in p
            assert "closed_legs" in p

    def test_snapshot_includes_open_position(self):
        e = _eng()
        e.reset_for_session("2026-05-13")
        e.admit(
            portfolio_id="main",
            ticker="ORCL",
            side="long",
            entry_price=200.0,
            equity=100_000.0,
            rod3_bps=-50.0,
            entry_iso="t",
        )
        snap = e.snapshot()
        positions = snap["per_portfolio"]["main"]["open_positions"]
        assert len(positions) == 1
        assert positions[0]["ticker"] == "ORCL"
        assert positions[0]["side"] == "long"
        assert positions[0]["rod3_bps"] == pytest.approx(-50.0)

    def test_snapshot_config_format(self):
        e = _eng()
        snap = e.snapshot()
        # v9.1.2: entry default moved from 15:30 to 15:00.
        assert snap["config"]["entry_et"] == "15:00"
        assert snap["config"]["exit_et"] == "15:58"
        # v9.1.1: live broker firing is the default
        assert snap["config"]["fire_broker"] is True
        assert isinstance(snap["config"]["universe"], list)


# ----- 7. Helpers -----------------------------------------------------


class TestFmtEt:
    def test_zero_padded(self):
        assert _fmt_et(9 * 60 + 5) == "09:05"
        assert _fmt_et(15 * 60 + 30) == "15:30"
        assert _fmt_et(0) == "00:00"
