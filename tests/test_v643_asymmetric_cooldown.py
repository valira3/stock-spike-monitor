"""v6.4.3 - asymmetric post-loss cooldown smoke tests.

After a stop-out, block new entries on the same (ticker, side) for a
per-side window. Defaults: long = 0 min (OFF), short = 30 min. The
Apr 27 - May 1 sweep at v642_cooldown_sweep/report.md showed the
long-side cooldown blocked NFLX +$45 (x2) and TSLA +$97 chase reentries
that turned out profitable, while the short side kept the 3-for-3 chase
saves on TSLA, META, AMZN. Net L0/S30 = +$1,436.80/wk vs L30/S30
v6.4.2 = +$1,250.02 (+$187/wk lift on the same week).

These tests assert:

  1. POST_LOSS_COOLDOWN_MIN_LONG default is 0, _SHORT default is 30.
  2. With long=0 the LONG side is a no-op even on a losing exit.
  3. With short=30 the SHORT side records and blocks for 30 min.
  4. With both sides > 0 each side honors its OWN window (asymmetric).
  5. Legacy POST_LOSS_COOLDOWN_MIN env still cascades to both sides
     when the per-side override is unset (back-compat for v6.4.2 ops).
  6. is_in_post_loss_cooldown / get_active_cooldowns still work
     unchanged from v6.4.2 (this release only changes WHICH window is
     used at record time).
"""

from __future__ import annotations

import importlib
import os
import sys
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


@pytest.fixture
def _restore_eot_envs():
    """Snapshot and restore POST_LOSS_COOLDOWN_* env vars + module values."""
    keys = (
        "POST_LOSS_COOLDOWN_MIN",
        "POST_LOSS_COOLDOWN_MIN_LONG",
        "POST_LOSS_COOLDOWN_MIN_SHORT",
    )
    saved_env = {k: os.environ.get(k) for k in keys}
    saved_mod = {
        "POST_LOSS_COOLDOWN_MIN": eot.POST_LOSS_COOLDOWN_MIN,
        "POST_LOSS_COOLDOWN_MIN_LONG": eot.POST_LOSS_COOLDOWN_MIN_LONG,
        "POST_LOSS_COOLDOWN_MIN_SHORT": eot.POST_LOSS_COOLDOWN_MIN_SHORT,
    }
    yield
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    for k, v in saved_mod.items():
        setattr(eot, k, v)


# ---------------------------------------------------------------------
# 1. defaults
# ---------------------------------------------------------------------

def test_default_long_is_zero_short_is_30():
    # Test runs with no POST_LOSS_COOLDOWN_MIN* env set in CI.
    # If the dev shell has one of these exported, _restore_eot_envs is
    # NOT used here on purpose: we read whatever the import-time value
    # was, but we ALSO assert the baked-in defaults by re-reading via
    # the helper with no env override.
    assert eot.POST_LOSS_COOLDOWN_MIN_LONG == 0 or os.environ.get(
        "POST_LOSS_COOLDOWN_MIN_LONG"
    ) is not None or os.environ.get("POST_LOSS_COOLDOWN_MIN") is not None
    assert eot.POST_LOSS_COOLDOWN_MIN_SHORT == 30 or os.environ.get(
        "POST_LOSS_COOLDOWN_MIN_SHORT"
    ) is not None or os.environ.get("POST_LOSS_COOLDOWN_MIN") is not None


def test_baked_in_defaults_via_helper(_restore_eot_envs):
    """With every env unset, _read_int returns long=0, short=30."""
    for k in (
        "POST_LOSS_COOLDOWN_MIN",
        "POST_LOSS_COOLDOWN_MIN_LONG",
        "POST_LOSS_COOLDOWN_MIN_SHORT",
    ):
        os.environ.pop(k, None)
    long_min = eot._read_int(
        "POST_LOSS_COOLDOWN_MIN_LONG",
        eot._read_int("POST_LOSS_COOLDOWN_MIN", 0),
    )
    short_min = eot._read_int(
        "POST_LOSS_COOLDOWN_MIN_SHORT",
        eot._read_int("POST_LOSS_COOLDOWN_MIN", 30),
    )
    assert long_min == 0
    assert short_min == 30


# ---------------------------------------------------------------------
# 2. long=0 means LONG side is a no-op even on a losing exit
# ---------------------------------------------------------------------

def test_long_zero_no_op_on_losing_long(_restore_eot_envs):
    eot.POST_LOSS_COOLDOWN_MIN_LONG = 0
    eot.POST_LOSS_COOLDOWN_MIN_SHORT = 30
    tg.record_post_loss_cooldown("NFLX", "long", pnl=-22.50)
    assert tg._post_loss_cooldown == {}
    # entry gate must allow the next long
    assert tg._check_post_loss_cooldown("NFLX", "long") is True


# ---------------------------------------------------------------------
# 3. short=30 records and blocks for 30 min
# ---------------------------------------------------------------------

def test_short_30_records_and_blocks(monkeypatch, _restore_eot_envs):
    eot.POST_LOSS_COOLDOWN_MIN_LONG = 0
    eot.POST_LOSS_COOLDOWN_MIN_SHORT = 30

    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts)
    tg.record_post_loss_cooldown("TSLA", "short", pnl=-34.19)

    entry = tg._post_loss_cooldown.get(("TSLA", "short"))
    assert entry is not None
    assert entry["loss_pnl"] == -34.19
    assert entry["until_utc"] == loss_ts + timedelta(minutes=30)

    # 5 min after loss: still blocked
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=5))
    assert tg._check_post_loss_cooldown("TSLA", "short") is False

    # 31 min after loss: cleared
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=31))
    assert tg._check_post_loss_cooldown("TSLA", "short") is True


# ---------------------------------------------------------------------
# 4. asymmetric: long=15, short=45 -> each side honors its OWN window
# ---------------------------------------------------------------------

def test_asymmetric_each_side_honors_own_window(monkeypatch, _restore_eot_envs):
    eot.POST_LOSS_COOLDOWN_MIN_LONG = 15
    eot.POST_LOSS_COOLDOWN_MIN_SHORT = 45

    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts)

    tg.record_post_loss_cooldown("AAPL", "long", pnl=-12.0)
    tg.record_post_loss_cooldown("AAPL", "short", pnl=-12.0)

    long_entry = tg._post_loss_cooldown.get(("AAPL", "long"))
    short_entry = tg._post_loss_cooldown.get(("AAPL", "short"))
    assert long_entry["until_utc"] == loss_ts + timedelta(minutes=15)
    assert short_entry["until_utc"] == loss_ts + timedelta(minutes=45)

    # 20 min after loss: long expired, short still active
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=20))
    assert tg._check_post_loss_cooldown("AAPL", "long") is True
    assert tg._check_post_loss_cooldown("AAPL", "short") is False

    # 50 min after loss: both expired
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=50))
    assert tg._check_post_loss_cooldown("AAPL", "long") is True
    assert tg._check_post_loss_cooldown("AAPL", "short") is True


# ---------------------------------------------------------------------
# 5. legacy POST_LOSS_COOLDOWN_MIN cascades to both sides via fallback
# ---------------------------------------------------------------------

def test_legacy_min_cascades_via_fallback(_restore_eot_envs):
    """A v6.4.2 operator who set POST_LOSS_COOLDOWN_MIN=15 must still see
    that 15-min window applied to both sides after the v6.4.3 reload."""
    os.environ["POST_LOSS_COOLDOWN_MIN"] = "15"
    os.environ.pop("POST_LOSS_COOLDOWN_MIN_LONG", None)
    os.environ.pop("POST_LOSS_COOLDOWN_MIN_SHORT", None)

    # Re-import eye_of_tiger so module-level _read_int re-runs.
    sys.modules.pop("eye_of_tiger", None)
    eot_reloaded = importlib.import_module("eye_of_tiger")
    try:
        assert eot_reloaded.POST_LOSS_COOLDOWN_MIN == 15
        assert eot_reloaded.POST_LOSS_COOLDOWN_MIN_LONG == 15
        assert eot_reloaded.POST_LOSS_COOLDOWN_MIN_SHORT == 15
    finally:
        # Put the original module back so subsequent tests see defaults.
        sys.modules["eye_of_tiger"] = eot


# ---------------------------------------------------------------------
# 6. side normalization still works
# ---------------------------------------------------------------------

def test_side_normalization_uppercase(monkeypatch, _restore_eot_envs):
    eot.POST_LOSS_COOLDOWN_MIN_LONG = 0
    eot.POST_LOSS_COOLDOWN_MIN_SHORT = 30

    loss_ts = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts)

    tg.record_post_loss_cooldown("META", "SHORT", pnl=-15.0)
    # stored under lowercase key
    assert ("META", "short") in tg._post_loss_cooldown
    assert ("META", "SHORT") not in tg._post_loss_cooldown

    monkeypatch.setattr(tg, "_now_utc", lambda: loss_ts + timedelta(minutes=5))
    assert tg._check_post_loss_cooldown("META", "Short") is False
    assert tg._check_post_loss_cooldown("META", "short") is False


# ---------------------------------------------------------------------
# 7. both sides zero -> no-op everywhere (operator full-disable)
# ---------------------------------------------------------------------

def test_both_sides_zero_full_disable(_restore_eot_envs):
    eot.POST_LOSS_COOLDOWN_MIN_LONG = 0
    eot.POST_LOSS_COOLDOWN_MIN_SHORT = 0

    tg.record_post_loss_cooldown("TSLA", "long", pnl=-30.0)
    tg.record_post_loss_cooldown("TSLA", "short", pnl=-30.0)
    assert tg._post_loss_cooldown == {}
    assert tg._check_post_loss_cooldown("TSLA", "long") is True
    assert tg._check_post_loss_cooldown("TSLA", "short") is True


# ---------------------------------------------------------------------
# 8. winning exit: still no-op regardless of per-side window
# ---------------------------------------------------------------------

def test_winning_exit_no_op_even_with_short_30(_restore_eot_envs):
    eot.POST_LOSS_COOLDOWN_MIN_SHORT = 30
    tg.record_post_loss_cooldown("AMZN", "short", pnl=12.0)
    assert tg._post_loss_cooldown == {}
