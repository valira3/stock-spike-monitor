"""v5.9.4 \u2014 unit tests for the Titan bypass in _tiger_hard_eject_check.

Covers the v5.7.1 implementation gap that v5.9.4 patched. v5.7.1 specced
that Titan tickers (AAPL, AMZN, AVGO, GOOG, META, MSFT, NFLX, NVDA, ORCL,
TSLA) bypass the legacy DI<25 hard-eject and exit only via the
Bison/Buffalo FSM, but the guard was never wired into the live function.

Cases:
  1. Titan (MSFT) with DI+ below threshold \u2014 NOT flushed (bypass fires).
  2. Non-Titan (PLTR) with DI+ below threshold \u2014 IS flushed (legacy gate).
  3. Same shape mirrored on the short side.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Boot trade_genius under the smoke flag so the Telegram client,
# scheduler, OR-collector, and dashboard never start during tests.
os.environ.setdefault("SSM_SMOKE_TEST", "1")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import trade_genius as m  # noqa: E402


def _stub_dependencies(monkeypatch, di_value):
    """Make tiger_di always return (di_value, di_value), no bar fetches,
    no rehunt arming, no real close_position side effects. Returns the
    list closures will append to so tests can assert who got flushed.
    """
    flushed_long: list[tuple[str, str]] = []
    flushed_short: list[tuple[str, str]] = []

    monkeypatch.setattr(m, "tiger_di", lambda t: (di_value, di_value))
    monkeypatch.setattr(m, "fetch_1min_bars", lambda t: None)
    monkeypatch.setattr(m, "_v519_arm_rehunt_watch", lambda *a, **kw: None)
    monkeypatch.setattr(
        m,
        "close_position",
        lambda ticker, price, reason="STOP": flushed_long.append((ticker, reason)),
    )
    monkeypatch.setattr(
        m,
        "close_short_position",
        lambda ticker, price, reason="STOP": flushed_short.append((ticker, reason)),
    )
    return flushed_long, flushed_short


def _seed_positions(monkeypatch, longs=None, shorts=None):
    """Replace the module-level positions / short_positions dicts so the
    test does not stomp live paper state.
    """
    monkeypatch.setattr(m, "positions", dict(longs or {}))
    monkeypatch.setattr(m, "short_positions", dict(shorts or {}))


# --------------------------------------------------------------------
# Long-side cases.
# --------------------------------------------------------------------
def test_titan_long_msft_bypassed(monkeypatch):
    """MSFT is a Titan. DI+ below threshold must NOT trigger HARD_EJECT_TIGER."""
    weak_di = m.TIGER_V2_DI_THRESHOLD - 1.45  # well below threshold
    flushed_long, _ = _stub_dependencies(monkeypatch, weak_di)
    _seed_positions(monkeypatch, longs={"MSFT": {"entry_price": 410.0}})

    m._tiger_hard_eject_check()

    assert flushed_long == [], (
        "Titan MSFT must not be flushed by _tiger_hard_eject_check; "
        "the v5.9.4 bypass guard is missing. Got: %r" % (flushed_long,)
    )


def test_non_titan_long_pltr_still_ejected(monkeypatch):
    """PLTR is not a Titan. DI+ below threshold must still trigger HARD_EJECT_TIGER."""
    weak_di = m.TIGER_V2_DI_THRESHOLD - 1.45
    flushed_long, _ = _stub_dependencies(monkeypatch, weak_di)
    _seed_positions(monkeypatch, longs={"PLTR": {"entry_price": 24.0}})

    m._tiger_hard_eject_check()

    assert flushed_long == [("PLTR", "HARD_EJECT_TIGER")], (
        "Non-Titan PLTR must still be flushed by the legacy DI<25 hard-eject. "
        "Got: %r" % (flushed_long,)
    )


def test_mixed_long_universe_only_non_titans_ejected(monkeypatch):
    """Half the open longs are Titans, half are not. Only non-Titans get flushed."""
    weak_di = m.TIGER_V2_DI_THRESHOLD - 2.0
    flushed_long, _ = _stub_dependencies(monkeypatch, weak_di)
    _seed_positions(
        monkeypatch,
        longs={
            "MSFT": {"entry_price": 410.0},
            "TSLA": {"entry_price": 240.0},
            "PLTR": {"entry_price": 24.0},
            "SOFI": {"entry_price": 8.0},
        },
    )

    m._tiger_hard_eject_check()

    flushed_set = {t for t, _ in flushed_long}
    assert flushed_set == {"PLTR", "SOFI"}, (
        "Expected only PLTR and SOFI to be flushed; MSFT and TSLA must be bypassed. "
        "Got: %r" % (flushed_long,)
    )


# --------------------------------------------------------------------
# Short-side mirror.
# --------------------------------------------------------------------
def test_titan_short_msft_bypassed(monkeypatch):
    """MSFT is a Titan. DI- below threshold must NOT trigger HARD_EJECT_TIGER on the short side."""
    weak_di = m.TIGER_V2_DI_THRESHOLD - 1.45
    _, flushed_short = _stub_dependencies(monkeypatch, weak_di)
    _seed_positions(monkeypatch, shorts={"MSFT": {"entry_price": 410.0}})

    m._tiger_hard_eject_check()

    assert flushed_short == [], (
        "Titan MSFT must not be flushed by _tiger_hard_eject_check on the short side. "
        "Got: %r" % (flushed_short,)
    )


def test_non_titan_short_pltr_still_ejected(monkeypatch):
    """PLTR is not a Titan. Short side legacy DI<25 hard-eject still fires."""
    weak_di = m.TIGER_V2_DI_THRESHOLD - 1.45
    _, flushed_short = _stub_dependencies(monkeypatch, weak_di)
    _seed_positions(monkeypatch, shorts={"PLTR": {"entry_price": 24.0}})

    m._tiger_hard_eject_check()

    assert flushed_short == [("PLTR", "HARD_EJECT_TIGER")], (
        "Non-Titan PLTR must still be flushed by the legacy short-side DI<25 hard-eject. "
        "Got: %r" % (flushed_short,)
    )


# --------------------------------------------------------------------
# Sanity guard \u2014 every Titan in the canonical list must be honored.
# --------------------------------------------------------------------
def test_every_titan_is_bypassed_long(monkeypatch):
    weak_di = m.TIGER_V2_DI_THRESHOLD - 5.0
    flushed_long, _ = _stub_dependencies(monkeypatch, weak_di)
    _seed_positions(monkeypatch, longs={t: {"entry_price": 100.0} for t in m.TITAN_TICKERS})

    m._tiger_hard_eject_check()

    assert flushed_long == [], "Every Titan must be bypassed; got flushed=%r" % (flushed_long,)
