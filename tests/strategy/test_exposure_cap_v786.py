"""v7.86.0 -- total-exposure cap in broker.orders.execute_breakout.

Verifies that the 95%-of-equity exposure cap blocks new entries that
would push (longs_MV + shorts_liability + new_notional) above the
threshold. Pre-v7.86.0 the only pre-trade cap was the long-only
"notional <= cash" check, which let shorts expand unboundedly.

Tests target the cap math via a minimal `tg` shim -- full
execute_breakout invocation requires too many side-effect imports
(telegram, alpaca, paper-state persistence, etc.) for a focused unit
test. The cap clause is a guard block at the top of execute_breakout
that depends only on `tg.paper_cash`, `tg.positions`, `tg.short_positions`.
We exercise the clause's logic in isolation by replicating its arithmetic.
"""
from __future__ import annotations

import pytest


def _compute_exposure_check(*, paper_cash, positions, short_positions,
                            notional_new, cap_pct=0.95):
    """Mirror the v7.86.0 exposure-cap math from broker/orders.py.

    Returns (should_block, debug_dict) so tests can assert both the
    decision and the intermediate values.
    """
    long_mv = sum(p["entry_price"] * p["shares"] for p in positions.values())
    short_liab = sum(p["entry_price"] * p["shares"]
                     for p in short_positions.values())
    equity = paper_cash + long_mv - short_liab
    exposure_cap = cap_pct * equity
    new_exposure = long_mv + short_liab + notional_new
    should_block = equity > 0 and new_exposure > exposure_cap
    return should_block, {
        "long_mv": long_mv, "short_liab": short_liab,
        "equity": equity, "exposure_cap": exposure_cap,
        "new_exposure": new_exposure,
    }


class TestExposureCapMath:

    def test_clean_book_allows_long_under_cap(self):
        """Fresh book, equity=$100K, cap=$95K. Long $50K notional -> admit."""
        block, dbg = _compute_exposure_check(
            paper_cash=100_000.0, positions={}, short_positions={},
            notional_new=50_000.0,
        )
        assert not block
        assert dbg["equity"] == 100_000.0
        assert dbg["exposure_cap"] == 95_000.0
        assert dbg["new_exposure"] == 50_000.0

    def test_clean_book_blocks_long_over_cap(self):
        """Fresh book, $100K equity. Long $99K notional -> over $95K cap."""
        block, dbg = _compute_exposure_check(
            paper_cash=100_000.0, positions={}, short_positions={},
            notional_new=99_000.0,
        )
        assert block
        assert dbg["new_exposure"] > dbg["exposure_cap"]

    def test_clean_book_blocks_short_over_cap(self):
        """Fresh book, $100K equity. Short $96K notional -> over cap."""
        block, dbg = _compute_exposure_check(
            paper_cash=100_000.0, positions={}, short_positions={},
            notional_new=96_000.0,
        )
        assert block

    def test_existing_shorts_still_at_equity_block_new_short(self):
        """The 2026-05-11 production scenario: $100K equity, $223K shorts
        already open. Equity computed as cash + long_mv - short_liab.

        State: paper_cash=$300K, no longs, $200K shorts (entry).
          equity = 300 + 0 - 200 = $100K
          cap = $95K
          existing exposure = 200K (longs + shorts)
          new short $50K -> new_exposure = 200 + 50 = $250K > $95K -> BLOCK
        """
        block, dbg = _compute_exposure_check(
            paper_cash=300_000.0,
            positions={},
            short_positions={
                "TSLA": {"entry_price": 400.0, "shares": 500},  # $200K
            },
            notional_new=50_000.0,
        )
        assert block
        assert dbg["equity"] == 100_000.0
        assert dbg["short_liab"] == 200_000.0
        # New_exposure $250K (existing 200K + new 50K) >> $95K cap
        assert dbg["new_exposure"] == 250_000.0

    def test_combined_long_and_short_block(self):
        """$100K equity, $20K longs, $30K shorts. Total exposure $50K.
        Cap = $95K. New $50K long -> total $100K > $95K -> block."""
        block, dbg = _compute_exposure_check(
            paper_cash=110_000.0,
            positions={"AAPL": {"entry_price": 200.0, "shares": 100}},  # $20K
            short_positions={
                "META": {"entry_price": 300.0, "shares": 100},  # $30K
            },
            notional_new=50_000.0,
        )
        assert block
        assert dbg["equity"] == 100_000.0  # 110 + 20 - 30
        assert dbg["new_exposure"] == 100_000.0  # 20 + 30 + 50

    def test_combined_long_and_short_admit(self):
        """Same setup but smaller new entry -> total $80K < $95K -> admit."""
        block, _ = _compute_exposure_check(
            paper_cash=110_000.0,
            positions={"AAPL": {"entry_price": 200.0, "shares": 100}},
            short_positions={
                "META": {"entry_price": 300.0, "shares": 100},
            },
            notional_new=30_000.0,  # total = 20 + 30 + 30 = 80K < 95K
        )
        assert not block

    def test_zero_equity_skips_check(self):
        """Edge case: equity=0 (just-starting empty book + negative cash drift).
        Cap check skipped (returns admit) so a fresh bot can take its first
        entry even before paper_cash is fully synced."""
        block, _ = _compute_exposure_check(
            paper_cash=0.0, positions={}, short_positions={},
            notional_new=10_000.0,
        )
        # equity <= 0 short-circuits the block decision
        assert not block

    def test_negative_equity_skips_check(self):
        """Worst-case drift: equity went negative (book is underwater).
        We skip blocking new entries here -- the daily-loss kill should
        have fired long before this state and that's the real recovery
        gate. This branch just keeps the cap from being a deadlock."""
        block, _ = _compute_exposure_check(
            paper_cash=0.0,
            positions={},
            short_positions={
                "TSLA": {"entry_price": 100.0, "shares": 100},  # $10K liab
            },
            notional_new=5_000.0,
        )
        # equity = 0 + 0 - 10K = -10K, < 0 → skip the block
        assert not block

    def test_exact_cap_boundary_admits(self):
        """At exactly the cap line, admit (we use `>` not `>=`)."""
        block, dbg = _compute_exposure_check(
            paper_cash=100_000.0, positions={}, short_positions={},
            notional_new=95_000.0,
        )
        assert not block  # 95K == cap, not over
        assert dbg["new_exposure"] == dbg["exposure_cap"]

    def test_one_cent_over_cap_blocks(self):
        """One cent over: block."""
        block, _ = _compute_exposure_check(
            paper_cash=100_000.0, positions={}, short_positions={},
            notional_new=95_000.01,
        )
        assert block
