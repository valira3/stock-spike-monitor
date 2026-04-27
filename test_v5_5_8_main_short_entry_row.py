"""v5.5.8 \u2014 unit tests for Main-tab SHORT entry-row synthesis.

The dashboard's Today's Trades panel needs to show *two* rows for a
closed short trade (entry + exit) just like longs do, but short entries
are intentionally NOT persisted to any trade list (the storage invariant
in trade_genius.py: short_trade_history is the single source of truth
for shorts and avoids double-counting on /trades). The fix lives in
``dashboard_server._today_trades`` \u2014 it synthesizes a SHORT entry row
from the cover's embedded entry_* fields, plus sweeps live
``short_positions`` for OPEN shorts (entered today with no cover yet)
and emits a synthesized entry row for those too.

Standalone runner, matching the v5.5.7 test style:

    python test_v5_5_8_main_short_entry_row.py
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Mirror smoke_test.py / v5.5.7 tests: silence the Telegram + state-volume
# imports so importing trade_genius / dashboard_server is side-effect free.
os.environ["SSM_SMOKE_TEST"] = "1"
os.environ.setdefault("CHAT_ID", "999999999")
os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
os.environ.setdefault(
    "TELEGRAM_TOKEN",
    "0000000000:AAAA_smoke_placeholder_token_0000000",
)
_tmp_state = Path("/tmp/ssm_v558_state")
_tmp_state.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("STATE_DB_PATH", str(_tmp_state / "state.db"))


def _reset_module_state(tg, today: str = "2026-04-27") -> None:
    """Wipe the trade-list globals so each test starts from a clean slate."""
    tg.paper_trades = []
    if hasattr(tg, "paper_all_trades"):
        tg.paper_all_trades = []
    tg.short_trade_history = []
    tg.short_positions = {}
    tg.positions = {}

    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime(2026, 4, 27, 10, 35, 0, tzinfo=ZoneInfo("America/New_York"))
    except Exception:
        et_now = datetime(2026, 4, 27, 10, 35, 0)
    tg._now_et = lambda: et_now  # type: ignore[attr-defined]


def _bootstrap():
    import trade_genius as tg
    import dashboard_server as ds
    importlib.reload(ds)
    return tg, ds


def test_closed_short_emits_two_rows() -> None:
    tg, ds = _bootstrap()
    _reset_module_state(tg)
    tg.short_trade_history = [{
        "ticker": "NVDA",
        "side": "SHORT",
        "action": "COVER",
        "shares": 48,
        "entry_price": 207.94,
        "exit_price": 208.53,
        "pnl": -28.32,
        "pnl_pct": -0.28,
        "reason": "POLARITY_SHIFT",
        "entry_time": "09:31 CDT",
        "exit_time": "09:35 CDT",
        "entry_time_iso": "2026-04-27T14:31:37+00:00",
        "exit_time_iso": "2026-04-27T14:35:47+00:00",
        "entry_num": 1,
        "date": "2026-04-27",
    }]

    rows = ds._today_trades()

    assert len(rows) == 2, rows
    entry, cover = rows[0], rows[1]

    assert entry["action"] == "SHORT"
    assert entry["side"] == "SHORT"
    assert entry["ticker"] == "NVDA"
    assert entry["shares"] == 48
    assert entry["price"] == 207.94
    assert entry["entry_price"] == 207.94
    assert entry["time"] == "09:31 CDT"
    assert entry["entry_time"] == "09:31 CDT"
    assert entry["entry_time_iso"] == "2026-04-27T14:31:37+00:00"
    assert entry["entry_num"] == 1
    assert entry["date"] == "2026-04-27"
    assert abs(entry["cost"] - (48 * 207.94)) < 1e-6
    assert entry["portfolio"] == "paper"
    assert "pnl" not in entry
    assert "exit_price" not in entry

    assert cover["action"] == "COVER"
    assert cover["side"] == "SHORT"
    assert cover["ticker"] == "NVDA"
    assert cover["pnl"] == -28.32

    # Sort order: entry must precede cover (entry_time HH:MM CDT < exit_time).
    assert rows[0]["action"] == "SHORT"
    assert rows[1]["action"] == "COVER"


def test_open_short_emits_entry_row_only() -> None:
    tg, ds = _bootstrap()
    _reset_module_state(tg)
    tg.short_positions = {
        "TSLA": {
            "entry_price": 245.10,
            "shares": 40,
            "stop": 247.00,
            "trail_active": False,
            "entry_count": 1,
            "entry_time": "09:32:11",
            "entry_ts_utc": "2026-04-27T14:32:11+00:00",
            "date": "2026-04-27",
            "side": "SHORT",
        },
    }

    rows = ds._today_trades()

    assert len(rows) == 1, rows
    r = rows[0]
    assert r["action"] == "SHORT"
    assert r["side"] == "SHORT"
    assert r["ticker"] == "TSLA"
    assert r["shares"] == 40
    assert r["entry_price"] == 245.10
    assert r["price"] == 245.10
    # entry_time is normalized into the same HH:MM CDT format covers use.
    assert r["entry_time"] == "09:32 CDT", r["entry_time"]
    assert r["time"] == "09:32 CDT", r["time"]
    assert r["entry_time_iso"] == "2026-04-27T14:32:11+00:00"
    assert r["entry_num"] == 1
    assert abs(r["cost"] - (40 * 245.10)) < 1e-6
    assert r["portfolio"] == "paper"


def test_open_short_dated_yesterday_is_skipped() -> None:
    tg, ds = _bootstrap(); _reset_module_state(tg)
    tg.short_positions = {
        "TSLA": {
            "entry_price": 245.10,
            "shares": 40,
            "entry_time": "09:32:11",
            "entry_ts_utc": "2026-04-26T14:32:11+00:00",
            "date": "2026-04-26",
        },
    }
    rows = ds._today_trades()
    assert rows == [], rows


def test_long_trade_still_emits_two_rows_unchanged() -> None:
    tg, ds = _bootstrap()
    _reset_module_state(tg)
    tg.paper_trades = [
        {
            "action": "BUY",
            "ticker": "AAPL",
            "price": 180.00,
            "shares": 30,
            "cost": 5400.00,
            "entry_num": 1,
            "time": "09:33 CDT",
            "date": "2026-04-27",
        },
        {
            "action": "SELL",
            "ticker": "AAPL",
            "price": 182.00,
            "shares": 30,
            "pnl": 60.00,
            "pnl_pct": 1.11,
            "reason": "TRAIL",
            "entry_price": 180.00,
            "time": "09:40 CDT",
            "date": "2026-04-27",
        },
    ]

    rows = ds._today_trades()

    assert len(rows) == 2, rows
    actions = [r["action"] for r in rows]
    assert actions == ["BUY", "SELL"], actions
    assert all(r["side"] == "LONG" for r in rows)


def test_mixed_day_correct_count_and_sort() -> None:
    tg, ds = _bootstrap()
    _reset_module_state(tg)
    tg.paper_trades = [
        {"action": "BUY", "ticker": "AAPL", "price": 180.00, "shares": 30,
         "cost": 5400.00, "time": "09:33 CDT", "date": "2026-04-27"},
        {"action": "SELL", "ticker": "AAPL", "price": 182.00, "shares": 30,
         "pnl": 60.00, "entry_price": 180.00,
         "time": "09:40 CDT", "date": "2026-04-27"},
    ]
    tg.short_trade_history = [{
        "ticker": "NVDA", "side": "SHORT", "action": "COVER",
        "shares": 48, "entry_price": 207.94, "exit_price": 208.53,
        "pnl": -28.32, "pnl_pct": -0.28, "reason": "POLARITY_SHIFT",
        "entry_time": "09:31 CDT", "exit_time": "09:35 CDT",
        "entry_time_iso": "2026-04-27T14:31:37+00:00",
        "entry_num": 1, "date": "2026-04-27",
    }]
    tg.short_positions = {
        "TSLA": {
            "entry_price": 245.10, "shares": 40,
            "entry_time": "09:36:00",
            "entry_ts_utc": "2026-04-27T14:36:00+00:00",
            "date": "2026-04-27", "entry_count": 1,
        },
    }

    rows = ds._today_trades()

    assert len(rows) == 5, [r["action"] + ":" + (r.get("ticker") or "") for r in rows]

    actions = [(r["action"], r.get("ticker")) for r in rows]
    # Sort by time ascending: NVDA SHORT 09:31, NVDA COVER 09:35,
    # TSLA SHORT 09:36, AAPL BUY 09:33? Wait, BUY is 09:33 \u2014 that
    # actually sorts before NVDA COVER (09:35). Sequence by time:
    #   09:31 NVDA SHORT (synth entry)
    #   09:33 AAPL BUY
    #   09:35 NVDA COVER
    #   09:36 TSLA SHORT (open)
    #   09:40 AAPL SELL
    expected = [
        ("SHORT", "NVDA"),
        ("BUY", "AAPL"),
        ("COVER", "NVDA"),
        ("SHORT", "TSLA"),
        ("SELL", "AAPL"),
    ]
    assert actions == expected, actions


def test_stray_cover_in_paper_trades_does_not_produce_third_row() -> None:
    """Defensive: if some future bug double-writes the COVER into paper_trades,
    the dedup key (action=COVER, side=SHORT, ticker, time) collapses it
    so we still get exactly 2 rows (synth SHORT entry + COVER), not 3."""
    tg, ds = _bootstrap()
    _reset_module_state(tg)
    cover_payload = {
        "ticker": "NVDA",
        "side": "SHORT",
        "action": "COVER",
        "shares": 48,
        "entry_price": 207.94,
        "exit_price": 208.53,
        "pnl": -28.32,
        "pnl_pct": -0.28,
        "reason": "POLARITY_SHIFT",
        "entry_time": "09:31 CDT",
        "exit_time": "09:35 CDT",
        "time": "09:35 CDT",
        "entry_time_iso": "2026-04-27T14:31:37+00:00",
        "entry_num": 1,
        "date": "2026-04-27",
    }
    # Stray dupe in paper_trades (the invariant violation v4.1.7-dash dedup
    # was designed to absorb).
    tg.paper_trades = [cover_payload]
    tg.short_trade_history = [cover_payload]

    rows = ds._today_trades()

    actions = [r["action"] for r in rows]
    # Exactly: synth SHORT entry, then a single COVER (the stray + the
    # canonical dedup to one row).
    assert actions.count("COVER") == 1, rows
    assert actions.count("SHORT") == 1, rows
    assert len(rows) == 2, rows


def test_cover_then_open_short_same_ticker_no_duplicate_entry() -> None:
    """If a ticker covered earlier in the day AND has a fresh open short
    later, the open-short sweep must not re-emit a SHORT entry row whose
    (ticker, entry_time) matches the just-synthesized covered one."""
    tg, ds = _bootstrap()
    _reset_module_state(tg)
    tg.short_trade_history = [{
        "ticker": "NVDA", "side": "SHORT", "action": "COVER",
        "shares": 48, "entry_price": 207.94, "exit_price": 208.53,
        "pnl": -28.32, "entry_time": "09:31 CDT",
        "exit_time": "09:35 CDT",
        "entry_num": 1, "date": "2026-04-27",
    }]
    # Fresh re-entry (different time).
    tg.short_positions = {
        "NVDA": {
            "entry_price": 209.00, "shares": 50,
            "entry_time": "09:42:00",
            "entry_ts_utc": "2026-04-27T14:42:00+00:00",
            "date": "2026-04-27", "entry_count": 2,
        },
    }
    rows = ds._today_trades()
    actions = [(r["action"], r.get("entry_time"), r.get("shares")) for r in rows]
    # Should be: synth entry @ 09:31 (48 sh), cover @ 09:35, open synth @ 09:42 (50 sh)
    assert len(rows) == 3, actions
    assert actions[0] == ("SHORT", "09:31 CDT", 48)
    assert actions[1][0] == "COVER"
    assert actions[2] == ("SHORT", "09:42 CDT", 50)


TESTS = [
    test_closed_short_emits_two_rows,
    test_open_short_emits_entry_row_only,
    test_open_short_dated_yesterday_is_skipped,
    test_long_trade_still_emits_two_rows_unchanged,
    test_mixed_day_correct_count_and_sort,
    test_stray_cover_in_paper_trades_does_not_produce_third_row,
    test_cover_then_open_short_same_ticker_no_duplicate_entry,
]


def main() -> int:
    fails = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  +  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  X  {fn.__name__}: {type(e).__name__}: {e}")
    total = len(TESTS)
    print(f"\n  {total - fails} passed \u00b7 {fails} failed \u00b7 {total} total\n")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
