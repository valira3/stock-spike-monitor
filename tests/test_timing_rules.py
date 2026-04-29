"""v5.13.0 PR-5 timing-rule tests.

Locks in:
- SHARED-CUTOFF (15:44:59 ET new-position cutoff)
- SHARED-EOD    (15:49:59 ET force-flush)
- SHARED-CB     (-$1,500 daily circuit breaker)
- SHARED-HUNT   (unlimited hunting until cutoff)

Plus a DST sanity test on the November DST end.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("SSM_SMOKE_TEST", "1")

from engine.timing import (
    NEW_POSITION_CUTOFF_ET,
    EOD_FLUSH_ET,
    HUNT_END_ET,
    HUNT_START_ET,
    is_after_cutoff_et,
    is_after_eod_et,
    is_in_hunt_window,
)


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# SHARED-CUTOFF — new-position cutoff at 15:44:59 ET
# ---------------------------------------------------------------------------


def test_cutoff_constant_value():
    assert NEW_POSITION_CUTOFF_ET == time(15, 44, 59)


def test_cutoff_15_44_58_allows_entry():
    """One second before cutoff: entries still allowed."""
    now = datetime(2026, 4, 29, 15, 44, 58, tzinfo=ET)
    assert is_after_cutoff_et(now) is False
    assert is_in_hunt_window(now) is True


def test_cutoff_15_44_59_blocks_entry():
    """At cutoff: entries blocked."""
    now = datetime(2026, 4, 29, 15, 44, 59, tzinfo=ET)
    assert is_after_cutoff_et(now) is True
    assert is_in_hunt_window(now) is False


def test_cutoff_15_45_00_blocks_entry():
    """One second after cutoff: entries blocked."""
    now = datetime(2026, 4, 29, 15, 45, 0, tzinfo=ET)
    assert is_after_cutoff_et(now) is True
    assert is_in_hunt_window(now) is False


# ---------------------------------------------------------------------------
# SHARED-EOD — EOD flush at 15:49:59 ET
# ---------------------------------------------------------------------------


def test_eod_constant_value():
    assert EOD_FLUSH_ET == time(15, 49, 59)


def test_eod_15_49_58_no_flush():
    now = datetime(2026, 4, 29, 15, 49, 58, tzinfo=ET)
    assert is_after_eod_et(now) is False


def test_eod_15_49_59_triggers_flush():
    now = datetime(2026, 4, 29, 15, 49, 59, tzinfo=ET)
    assert is_after_eod_et(now) is True


def test_eod_15_50_00_triggers_flush():
    now = datetime(2026, 4, 29, 15, 50, 0, tzinfo=ET)
    assert is_after_eod_et(now) is True


# ---------------------------------------------------------------------------
# DST sanity — verify ET conversion behaves correctly across DST end
# (US DST ends first Sunday of November; in 2026 that is Nov 1.)
# At 06:00 UTC on Nov 1 2026, ET is 02:00 EDT (pre-fallback).
# At 06:00 UTC on Nov 2 2026, ET is 01:00 EST  (post-fallback).
# ---------------------------------------------------------------------------


def test_dst_end_transition_utc_input():
    """A UTC datetime is converted into ET correctly across DST end.

    US DST ends first Sunday of November (2026-11-01 02:00 local). Pick
    one timestamp clearly in EDT (Oct 31) and one clearly in EST (Nov 5)
    to verify both UTC offsets resolve correctly.
    """
    edt = datetime(2026, 10, 31, 19, 30, tzinfo=UTC).astimezone(ET)
    est = datetime(2026, 11, 5, 19, 30, tzinfo=UTC).astimezone(ET)
    # 19:30 UTC, EDT (UTC-4) -> 15:30 ET
    assert edt.hour == 15 and edt.minute == 30
    # 19:30 UTC, EST (UTC-5) -> 14:30 ET
    assert est.hour == 14 and est.minute == 30
    # Neither timestamp is at/after the 15:44:59 cutoff.
    assert is_after_cutoff_et(edt) is False
    assert is_after_cutoff_et(est) is False


def test_dst_cutoff_held_after_fallback():
    """Cutoff still fires at 15:44:59 ET on a winter (EST) date."""
    just_before = datetime(2026, 11, 3, 15, 44, 58, tzinfo=ET)
    at_cutoff = datetime(2026, 11, 3, 15, 44, 59, tzinfo=ET)
    assert is_after_cutoff_et(just_before) is False
    assert is_after_cutoff_et(at_cutoff) is True


# ---------------------------------------------------------------------------
# SHARED-CB — daily circuit breaker at -$1,500
# ---------------------------------------------------------------------------


def test_daily_loss_limit_constant():
    """trade_genius.DAILY_LOSS_LIMIT_DOLLARS is exactly -1500.0."""
    import trade_genius

    assert trade_genius.DAILY_LOSS_LIMIT_DOLLARS == -1500.0


def test_daily_breaker_blocks_at_threshold(monkeypatch):
    """day_pnl <= -1500 blocks entry; day_pnl just above passes the gate."""
    import trade_genius

    sent = []
    monkeypatch.setattr(trade_genius, "send_telegram", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(trade_genius, "v5_lock_all_tracks", lambda *a, **k: None)
    monkeypatch.setattr(trade_genius, "get_fmp_quote", lambda *a, **k: {"price": 0})
    monkeypatch.setattr(trade_genius, "_is_today", lambda *a, **k: False)
    monkeypatch.setattr(trade_genius, "DAILY_LOSS_LIMIT", -1500.0)

    # Reset halted flag and seed paper_trades with -1499.99 of realized losses.
    trade_genius._trading_halted = False
    today = trade_genius._now_et().strftime("%Y-%m-%d")
    monkeypatch.setattr(trade_genius, "paper_trades", [
        {"date": today, "action": "SELL", "pnl": -1499.99},
    ])
    monkeypatch.setattr(trade_genius, "short_trade_history", [])
    monkeypatch.setattr(trade_genius, "positions", {})
    monkeypatch.setattr(trade_genius, "short_positions", {})

    # Just above the floor — entry allowed.
    assert trade_genius._check_daily_loss_limit("AAPL") is True
    assert trade_genius._trading_halted is False

    # At/below the floor — entry blocked.
    monkeypatch.setattr(trade_genius, "paper_trades", [
        {"date": today, "action": "SELL", "pnl": -1500.0},
    ])
    assert trade_genius._check_daily_loss_limit("AAPL") is False
    assert trade_genius._trading_halted is True

    # Cleanup so other tests don't see a halted bot.
    trade_genius._trading_halted = False


def test_daily_breaker_does_not_force_exit_existing(monkeypatch):
    """SHARED-CB blocks NEW entries; existing positions are not exited here."""
    import trade_genius

    sent_signals = []

    def _capture_signal(payload):
        sent_signals.append(payload)

    # Make sure the breaker check itself does not synthesize EOD/exit events.
    monkeypatch.setattr(trade_genius, "send_telegram", lambda *a, **k: None)
    monkeypatch.setattr(trade_genius, "v5_lock_all_tracks", lambda *a, **k: None)
    monkeypatch.setattr(trade_genius, "_emit_signal", _capture_signal,
                        raising=False)
    monkeypatch.setattr(trade_genius, "get_fmp_quote", lambda *a, **k: {"price": 0})
    monkeypatch.setattr(trade_genius, "_is_today", lambda *a, **k: False)
    monkeypatch.setattr(trade_genius, "DAILY_LOSS_LIMIT", -1500.0)

    trade_genius._trading_halted = False
    today = trade_genius._now_et().strftime("%Y-%m-%d")
    monkeypatch.setattr(trade_genius, "paper_trades", [
        {"date": today, "action": "SELL", "pnl": -1500.0},
    ])
    monkeypatch.setattr(trade_genius, "short_trade_history", [])
    monkeypatch.setattr(trade_genius, "positions", {})
    monkeypatch.setattr(trade_genius, "short_positions", {})

    assert trade_genius._check_daily_loss_limit("AAPL") is False
    # The breaker must not emit any EXIT/EOD_CLOSE_ALL signals.
    exit_kinds = [s.get("kind") for s in sent_signals
                  if isinstance(s, dict)
                  and s.get("kind") in ("EXIT_LONG", "EXIT_SHORT", "EOD_CLOSE_ALL")]
    assert exit_kinds == []
    trade_genius._trading_halted = False


# ---------------------------------------------------------------------------
# SHARED-HUNT — unlimited hunting until cutoff (no cooldown / per-day cap)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hh,mm,ss,expect_in_window", [
    (9, 35, 0, True),     # session open
    (12, 0, 0, True),     # midday
    (15, 30, 0, True),    # was the old cutoff — still in window now
    (15, 44, 58, True),   # one second before cutoff
    (15, 44, 59, False),  # at cutoff
    (15, 45, 0, False),   # one second after cutoff
    (9, 34, 59, False),   # before session open
])
def test_hunt_window(hh, mm, ss, expect_in_window):
    now = datetime(2026, 4, 29, hh, mm, ss, tzinfo=ET)
    assert is_in_hunt_window(now) is expect_in_window


def test_hunt_end_aligns_with_cutoff():
    """SHARED-HUNT explicitly extends through the cutoff itself."""
    assert HUNT_END_ET == NEW_POSITION_CUTOFF_ET
    assert HUNT_START_ET == time(9, 35, 0)


# ---------------------------------------------------------------------------
# Spec-text presence — the trade_genius source must mention the new clocks.
# (Mirrors the assertions in test_tiger_sovereign_spec.py so a future refactor
# that drops the constants is caught here too.)
# ---------------------------------------------------------------------------


def test_trade_genius_mentions_cutoff_and_eod():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "trade_genius.py").read_text(
        encoding="utf-8"
    )
    assert "15:44" in src
    assert "15:49" in src
    assert "SHARED-CUTOFF" in src
    assert "SHARED-EOD" in src
