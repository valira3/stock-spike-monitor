"""v6.4.2 - post-loss cooldown smoke tests.

After a stop-out, block new entries on the same (ticker, side) for
POST_LOSS_COOLDOWN_MIN minutes. The Apr 27 - May 1 backtest showed
3-for-3 losing same-side same-ticker reentries within 30 min of a stop
(TSLA, META, AMZN shorts, all lost again). 30-min cooldown captures all
three for +$107/wk lift without blocking productive post-WIN reentry
chains (NVDA, MSFT, ORCL).

These tests assert:

  1. POST_LOSS_COOLDOWN_MIN default is 30.
  2. record_post_loss_cooldown is a no-op on a winning exit (pnl >= 0).
  3. record_post_loss_cooldown stores an entry on a losing exit and
     _check_post_loss_cooldown returns False during the window, True
     after the window expires.
  4. get_active_cooldowns returns dicts with the fields the dashboard
     reads (ticker, side, until_utc, remaining_sec, loss_pnl,
     loss_ts_utc).
  5. is_in_post_loss_cooldown auto-prunes expired entries on read.
  6. Side normalization: 'LONG' / 'Short' map to the same key as
     'long' / 'short'.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")

import eye_of_tiger as eot  # noqa: E402
import trade_genius as tg  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    """Wipe the module-level cooldown map before and after each test."""
    tg._post_loss_cooldown.clear()
    yield
    tg._post_loss_cooldown.clear()


def test_post_loss_cooldown_min_default_is_30():
    assert eot.POST_LOSS_COOLDOWN_MIN == 30


def test_record_no_op_on_winning_exit():
    tg.record_post_loss_cooldown("AAPL", "long", pnl=12.50)
    assert tg._post_loss_cooldown == {}
    assert tg._check_post_loss_cooldown("AAPL", "long") is True


def test_record_no_op_on_zero_pnl():
    tg.record_post_loss_cooldown("AAPL", "long", pnl=0.0)
    assert tg._post_loss_cooldown == {}


def test_record_stores_entry_on_losing_exit():
    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    tg.record_post_loss_cooldown("TSLA", "short", pnl=-34.19, exit_ts_utc=loss_ts)
    entry = tg._post_loss_cooldown.get(("TSLA", "short"))
    assert entry is not None
    assert entry["loss_pnl"] == -34.19
    assert entry["loss_ts_utc"] == loss_ts
    assert entry["until_utc"] == loss_ts + timedelta(minutes=30)


def test_check_blocks_during_window_and_allows_after(monkeypatch):
    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)

    # Pretend "now" is the loss instant when we record.
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts)
    tg.record_post_loss_cooldown("META", "short", pnl=-30.0)

    # 5 minutes after loss: still inside the 30-min window.
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=5))
    assert tg._check_post_loss_cooldown("META", "short") is False

    # 31 minutes after loss: outside the window, entry allowed; entry
    # auto-pruned on the next read.
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=31))
    assert tg._check_post_loss_cooldown("META", "short") is True
    assert ("META", "short") not in tg._post_loss_cooldown


def test_check_does_not_block_other_side_or_ticker(monkeypatch):
    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts)
    tg.record_post_loss_cooldown("TSLA", "short", pnl=-50.0)

    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=5))
    # Same ticker, opposite side: not blocked.
    assert tg._check_post_loss_cooldown("TSLA", "long") is True
    # Different ticker, same side: not blocked.
    assert tg._check_post_loss_cooldown("AAPL", "short") is True
    # The original key is still blocked.
    assert tg._check_post_loss_cooldown("TSLA", "short") is False


def test_get_active_cooldowns_shape(monkeypatch):
    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts)
    tg.record_post_loss_cooldown("AMZN", "short", pnl=-43.0)

    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=10))
    active = tg.get_active_cooldowns()
    assert isinstance(active, list)
    assert len(active) == 1
    row = active[0]
    required_fields = {
        "ticker", "side", "until_utc", "remaining_sec",
        "loss_pnl", "loss_ts_utc",
    }
    assert required_fields.issubset(row.keys())
    assert row["ticker"] == "AMZN"
    assert row["side"] == "short"
    assert row["loss_pnl"] == -43.0
    # 30 min window, 10 min elapsed, so ~20 min = 1200s remaining.
    assert 1190 <= row["remaining_sec"] <= 1200
    assert row["until_utc"].endswith("Z")
    assert row["loss_ts_utc"].endswith("Z")


def test_is_in_post_loss_cooldown_auto_prunes(monkeypatch):
    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts)
    tg.record_post_loss_cooldown("NVDA", "long", pnl=-15.0)
    assert ("NVDA", "long") in tg._post_loss_cooldown

    # Jump past the window. is_in_post_loss_cooldown should return None
    # AND remove the stale entry from the map on read.
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=45))
    assert tg.is_in_post_loss_cooldown("NVDA", "long") is None
    assert ("NVDA", "long") not in tg._post_loss_cooldown


def test_get_active_cooldowns_prunes_expired(monkeypatch):
    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts)
    tg.record_post_loss_cooldown("ORCL", "long", pnl=-20.0)
    tg.record_post_loss_cooldown("MSFT", "short", pnl=-25.0)

    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=45))
    active = tg.get_active_cooldowns()
    assert active == []
    assert tg._post_loss_cooldown == {}


def test_side_normalization_uppercase(monkeypatch):
    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts)
    # Recorded with uppercase, checked with lowercase: same key.
    tg.record_post_loss_cooldown("GOOG", "SHORT", pnl=-40.0)
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=5))
    assert tg._check_post_loss_cooldown("GOOG", "short") is False
    assert tg._check_post_loss_cooldown("GOOG", "Short") is False


def test_record_no_op_when_disabled(monkeypatch):
    """POST_LOSS_COOLDOWN_MIN=0 disables the feature: record is a no-op."""
    monkeypatch.setattr(eot, "POST_LOSS_COOLDOWN_MIN", 0)
    tg.record_post_loss_cooldown("META", "short", pnl=-30.0)
    assert tg._post_loss_cooldown == {}
    assert tg._check_post_loss_cooldown("META", "short") is True


def test_record_overwrites_back_to_back_loss(monkeypatch):
    """A second loss on the same key extends the window from the newer
    stop. This is the chase-pattern guard: the cooldown should reset to
    the most recent loss, not the original."""
    first_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    second_ts = first_ts + timedelta(minutes=10)

    monkeypatch.setattr(tg, "_now_utc", lambda: first_ts)
    tg.record_post_loss_cooldown("TSLA", "short", pnl=-34.0)

    monkeypatch.setattr(tg, "_now_utc", lambda: second_ts)
    tg.record_post_loss_cooldown("TSLA", "short", pnl=-22.0)

    entry = tg._post_loss_cooldown[("TSLA", "short")]
    assert entry["loss_pnl"] == -22.0
    assert entry["loss_ts_utc"] == second_ts
    assert entry["until_utc"] == second_ts + timedelta(minutes=30)
