"""v5.20.5 \u2014 DI seed RTH fallback contract tests.

Covers ``engine.seeders.seed_di_buffer_with_rth_fallback``: a wrapper
that first runs the existing premarket-only seeder (08:00\u219209:30 ET) and,
if the result is insufficient AND we are at/past 09:30 ET, fetches
Alpaca IEX 1m bars from 09:30 ET up to the most recently completed 5m
boundary, buckets them into 5m OHLC, merges with whatever premarket
bars came back, and commits to ``_DI_SEED_CACHE`` only when the
combined count >= 15.

Forensic motivation: 2026-04-30 09:36 ET, the bot reached the entry
gate with 0/10 tickers DI-seeded \u2014 Alpaca IEX premarket is too thin
on most names (1\u20139 5m buckets vs the required 15). The pre-existing
``recompute_di_for_unseeded`` re-ran the same premarket-only seeder,
which never produced more bars on its own. This wrapper closes the gap
by extending the data window into RTH proper.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SSM_SMOKE_TEST", "1")

ET = ZoneInfo("America/New_York")


@pytest.fixture
def seeders_module(monkeypatch):
    """Import engine.seeders fresh and reset DI cache between tests."""
    import trade_genius as tg
    from engine import seeders

    tg._DI_SEED_CACHE.clear()
    monkeypatch.setattr(tg, "TRADE_TICKERS", ["AAPL"])
    return seeders, tg


def _patch_now(monkeypatch, seeders, hour, minute):
    """Force seeders.datetime.now() to return a fixed instant."""
    fake_now = datetime(2026, 4, 30, hour, minute, 0, tzinfo=ET)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(seeders, "datetime", FakeDateTime)
    return fake_now


def _build_alpaca_rows(start_et, n_minutes, base_price=100.0):
    """Build n_minutes synthetic 1m rows starting at start_et."""
    rows = []
    for i in range(n_minutes):
        ts_et = start_et.replace(
            hour=start_et.hour + (start_et.minute + i) // 60,
            minute=(start_et.minute + i) % 60,
        )
        ts_utc = ts_et.astimezone(timezone.utc)
        row = MagicMock()
        row.timestamp = ts_utc
        row.high = base_price + 0.10 + i * 0.01
        row.low = base_price - 0.10 + i * 0.005
        row.close = base_price + i * 0.01
        rows.append(row)
    return rows


def _make_alpaca_client(rows_by_window):
    """Build a fake Alpaca client. rows_by_window is a list, each call to
    get_stock_bars pops the next response in order. This lets the
    seed-then-RTH-fetch (and optional premarket re-fetch) be ordered.
    """
    queue = list(rows_by_window)

    def _get(*_a, **_k):
        if not queue:
            resp = MagicMock()
            resp.data = {"AAPL": []}
            return resp
        rows = queue.pop(0)
        resp = MagicMock()
        resp.data = {"AAPL": rows}
        return resp

    client = MagicMock()
    client.get_stock_bars.side_effect = _get
    return client


# ---------------------------------------------------------------------
# Behavior contract
# ---------------------------------------------------------------------


def test_rth_fallback_skipped_before_rth(seeders_module, monkeypatch):
    """At 09:00 ET (premarket) with thin data, the wrapper must NOT
    fetch any RTH window: rth_bars=0, sufficient=False, cache unset.
    """
    seeders, tg = seeders_module
    _patch_now(monkeypatch, seeders, 9, 0)
    # Premarket: 5 minutes \u2192 1 5m bucket. Insufficient.
    pre_rows = _build_alpaca_rows(datetime(2026, 4, 30, 8, 0, tzinfo=ET), 5)
    client = _make_alpaca_client([pre_rows])
    monkeypatch.setattr(tg, "_alpaca_data_client", lambda: client)

    out = seeders.seed_di_buffer_with_rth_fallback("AAPL")
    assert out["sufficient"] is False
    assert out.get("rth_bars", 0) == 0
    assert "AAPL" not in tg._DI_SEED_CACHE
    # Only the premarket fetch happened (no RTH call).
    assert client.get_stock_bars.call_count == 1


def test_rth_fallback_picks_up_rth_buckets_but_still_short(seeders_module, monkeypatch):
    """At 10:00 ET with premarket=5 and RTH 09:30\u219210:00 = 30 min = 6
    closed 5m buckets. Combined = 5 + 6 = 11 < 15 \u2192 still insufficient,
    cache stays unset.
    """
    seeders, tg = seeders_module
    _patch_now(monkeypatch, seeders, 10, 0)

    pre_rows = _build_alpaca_rows(datetime(2026, 4, 30, 8, 0, tzinfo=ET), 5)
    rth_rows = _build_alpaca_rows(datetime(2026, 4, 30, 9, 30, tzinfo=ET), 30)
    # Order: premarket fetch (seed_di_buffer), RTH fetch, premarket re-fetch.
    client = _make_alpaca_client([pre_rows, rth_rows, pre_rows])
    monkeypatch.setattr(tg, "_alpaca_data_client", lambda: client)

    out = seeders.seed_di_buffer_with_rth_fallback("AAPL")
    assert out["sufficient"] is False
    assert out["rth_bars"] >= 5  # at least 5 closed buckets in 9:30\u219210:00
    assert "AAPL" not in tg._DI_SEED_CACHE


def test_rth_fallback_commits_when_combined_sufficient(seeders_module, monkeypatch):
    """At 11:00 ET with thin premarket but a full 90 min of RTH (\u224818
    closed 5m buckets), combined >= 15 \u2192 cache committed.
    """
    seeders, tg = seeders_module
    _patch_now(monkeypatch, seeders, 11, 0)

    pre_rows = _build_alpaca_rows(datetime(2026, 4, 30, 8, 0, tzinfo=ET), 5)
    # 90 minutes \u2192 18 closed 5m buckets.
    rth_rows = _build_alpaca_rows(datetime(2026, 4, 30, 9, 30, tzinfo=ET), 90)
    client = _make_alpaca_client([pre_rows, rth_rows, pre_rows])
    monkeypatch.setattr(tg, "_alpaca_data_client", lambda: client)

    out = seeders.seed_di_buffer_with_rth_fallback("AAPL")
    assert out["sufficient"] is True
    assert "AAPL" in tg._DI_SEED_CACHE
    assert len(tg._DI_SEED_CACHE["AAPL"]) >= 15


def test_rth_fallback_idempotent_when_already_sufficient(seeders_module, monkeypatch):
    """If the premarket seeder already produced enough bars, the
    RTH-fallback path must short-circuit \u2014 no Alpaca RTH fetch made,
    rth_bars=0, sufficient=True.
    """
    seeders, tg = seeders_module
    _patch_now(monkeypatch, seeders, 10, 0)

    # 90 min of premarket (08:00\u219209:30) \u2192 18 closed 5m buckets.
    pre_rows = _build_alpaca_rows(datetime(2026, 4, 30, 8, 0, tzinfo=ET), 90)
    client = _make_alpaca_client([pre_rows])
    monkeypatch.setattr(tg, "_alpaca_data_client", lambda: client)

    out = seeders.seed_di_buffer_with_rth_fallback("AAPL")
    assert out["sufficient"] is True
    assert out.get("rth_bars", 0) == 0
    # Only the initial seed_di_buffer fetch happened \u2014 no RTH call.
    assert client.get_stock_bars.call_count == 1


def test_recompute_routes_to_rth_fallback(seeders_module, monkeypatch):
    """v5.20.5 wired ``recompute_di_for_unseeded`` to the new fallback
    helper. Verify the public name resolves and the routing happened
    by checking the function source / module attribute.
    """
    seeders, _tg = seeders_module
    assert hasattr(seeders, "seed_di_buffer_with_rth_fallback")
    assert "seed_di_buffer_with_rth_fallback" in getattr(seeders, "__all__", []) or callable(
        seeders.seed_di_buffer_with_rth_fallback
    )
    # Source-level routing check: recompute_di_for_unseeded should
    # dispatch via the new helper, not the legacy premarket-only one.
    import inspect

    src = inspect.getsource(seeders.recompute_di_for_unseeded)
    assert "seed_di_buffer_with_rth_fallback" in src
