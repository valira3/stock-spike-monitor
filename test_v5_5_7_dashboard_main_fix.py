"""v5.5.7 \u2014 unit tests for Main-tab SHORT/COVER summary + last_signal.

Two surfaces under test:

1. The JS ``computeTradesSummary`` rule that the v5.5.7 patch installs in
   static/app.js. Pre-v5.5.7 it counted only BUY/SELL, so a SHORT entry
   followed by a COVER exit produced "0 opens 0 closes realized \u2014".
   The fix treats BUY/SHORT as opens and SELL/COVER as closes. This file
   ports that rule into Python so we can assert it directly without a
   browser harness.

2. The /api/state ``last_signal`` field added to the top-level snapshot.
   Pre-v5.5.7 only the per-executor payload exposed last_signal; Main's
   /api/state did not, so the Main tab had no way to render its own
   LAST SIGNAL card scoped to the paper executor.

Standalone runner, matching the v5.5.5/5.5.6 test style:

    python test_v5_5_7_dashboard_main_fix.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Mirror smoke_test.py / v5.5.6 tests: silence the Telegram + state-volume
# imports so importing trade_genius / dashboard_server is side-effect free.
os.environ["SSM_SMOKE_TEST"] = "1"
os.environ.setdefault("CHAT_ID", "999999999")
os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
os.environ.setdefault(
    "TELEGRAM_TOKEN",
    "0000000000:AAAA_smoke_placeholder_token_0000000",
)
_tmp_state = Path("/tmp/ssm_v557_state")
_tmp_state.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("STATE_DB_PATH", str(_tmp_state / "state.db"))


# Python mirror of the JS computeTradesSummary in static/app.js as
# rewritten by v5.5.7. Keep the two implementations in lockstep.
def compute_trades_summary(trades):
    opens = 0
    closes = 0
    wins = 0
    realized = 0.0
    have_pnl = 0
    for t in (trades or []):
        act = (t.get("action") or "").upper()
        is_open = act in ("BUY", "SHORT")
        is_close = act in ("SELL", "COVER")
        if is_open:
            opens += 1
        elif is_close:
            closes += 1
            pnl = t.get("pnl")
            if isinstance(pnl, (int, float)):
                realized += float(pnl)
                have_pnl += 1
                if pnl > 0:
                    wins += 1
    win_rate = (wins / have_pnl) if have_pnl > 0 else None
    return {
        "opens": opens,
        "closes": closes,
        "wins": wins,
        "realized": realized,
        "have_pnl": have_pnl,
        "win_rate": win_rate,
    }


def test_short_cover_pair_counts_as_open_and_close() -> None:
    # Live payload shape for a NVDA SHORT \u2192 COVER pair (today's bug
    # repro). Pre-fix this returned 0 opens / 0 closes / realized 0.
    trades = [
        {"action": "SHORT", "side": "SHORT", "ticker": "NVDA",
         "shares": 10, "entry_price": 209.50},
        {"action": "COVER", "side": "SHORT", "ticker": "NVDA",
         "shares": 10, "exit_price": 208.53,
         "pnl": -28.32, "pnl_pct": -0.46,
         "reason": "POLARITY_SHIFT"},
    ]
    s = compute_trades_summary(trades)
    assert s["opens"] == 1, s
    assert s["closes"] == 1, s
    assert s["have_pnl"] == 1, s
    assert abs(s["realized"] - (-28.32)) < 1e-6, s
    assert s["wins"] == 0, s
    assert s["win_rate"] == 0.0, s


def test_buy_sell_pair_still_counts() -> None:
    # Regression check: the old BUY/SELL behaviour must keep working.
    trades = [
        {"action": "BUY", "side": "LONG", "ticker": "AAPL",
         "shares": 5, "entry_price": 180.00},
        {"action": "SELL", "side": "LONG", "ticker": "AAPL",
         "shares": 5, "exit_price": 182.00, "pnl": 10.00, "pnl_pct": 1.11},
    ]
    s = compute_trades_summary(trades)
    assert s["opens"] == 1
    assert s["closes"] == 1
    assert s["wins"] == 1
    assert abs(s["realized"] - 10.00) < 1e-6
    assert s["win_rate"] == 1.0


def test_mixed_long_and_short_day() -> None:
    trades = [
        {"action": "BUY",   "ticker": "AAPL", "shares": 5},
        {"action": "SELL",  "ticker": "AAPL", "shares": 5, "pnl":  10.0},
        {"action": "SHORT", "ticker": "NVDA", "shares": 10},
        {"action": "COVER", "ticker": "NVDA", "shares": 10, "pnl": -28.32},
    ]
    s = compute_trades_summary(trades)
    assert s["opens"] == 2
    assert s["closes"] == 2
    assert s["wins"] == 1
    assert s["have_pnl"] == 2
    assert abs(s["realized"] - (10.0 + (-28.32))) < 1e-6
    assert s["win_rate"] == 0.5


def test_unknown_action_is_ignored() -> None:
    s = compute_trades_summary([
        {"action": "MARGIN_CALL"},
        {"action": ""},
        {},
    ])
    assert s["opens"] == 0
    assert s["closes"] == 0
    assert s["have_pnl"] == 0
    assert s["realized"] == 0.0
    assert s["win_rate"] is None


def test_empty_trades_list() -> None:
    s = compute_trades_summary([])
    assert s == {
        "opens": 0, "closes": 0, "wins": 0,
        "realized": 0.0, "have_pnl": 0, "win_rate": None,
    }


def test_close_without_pnl_does_not_explode() -> None:
    # Close with missing or non-numeric pnl: counts as a close but does
    # not contribute to realized / have_pnl.
    trades = [
        {"action": "COVER", "ticker": "NVDA", "shares": 10},  # no pnl
        {"action": "SELL",  "ticker": "AAPL", "shares": 5,
         "pnl": "broken"},  # not a number
    ]
    s = compute_trades_summary(trades)
    assert s["closes"] == 2
    assert s["have_pnl"] == 0
    assert s["realized"] == 0.0
    assert s["win_rate"] is None


def test_last_signal_surfaces_on_state_payload() -> None:
    """/api/state must expose last_signal at the top level (v5.5.7).

    Mocks the trade_genius module so dashboard_server.snapshot() reads
    a known last_signal dict and returns it under the same key.
    """
    import importlib

    # Stub a minimal trade_genius surface so dashboard_server.snapshot
    # can run without dragging in the live bot.
    sample_signal = {
        "kind": "EXIT_SHORT",
        "ticker": "NVDA",
        "price": 208.53,
        "reason": "POLARITY_SHIFT",
        "timestamp_utc": "2026-04-27T18:35:47Z",
    }

    import trade_genius as tg
    # Save & restore so a stray test run does not leave global state.
    saved = getattr(tg, "last_signal", None)
    try:
        tg.last_signal = sample_signal
        import dashboard_server as ds
        importlib.reload(ds)  # rebind _ssm() onto our patched module
        snap = ds.snapshot()
        assert isinstance(snap, dict), snap
        assert snap.get("ok") is True, snap
        assert "last_signal" in snap, "last_signal missing from /api/state"
        ls = snap["last_signal"]
        assert ls is not None, snap
        assert ls.get("kind") == "EXIT_SHORT"
        assert ls.get("ticker") == "NVDA"
        assert ls.get("reason") == "POLARITY_SHIFT"
    finally:
        tg.last_signal = saved


def test_last_signal_emit_records_module_level() -> None:
    """_emit_signal mirrors the most recent event into trade_genius.last_signal."""
    import trade_genius as tg
    saved = getattr(tg, "last_signal", None)
    try:
        tg.last_signal = None
        tg._emit_signal({
            "kind": "ENTRY_SHORT",
            "ticker": "NVDA",
            "price": 209.50,
            "reason": "BREAKDOWN",
            "timestamp_utc": "2026-04-27T18:30:01Z",
        })
        ls = tg.last_signal
        assert ls is not None
        assert ls["kind"] == "ENTRY_SHORT"
        assert ls["ticker"] == "NVDA"
        assert ls["price"] == 209.50
        assert ls["reason"] == "BREAKDOWN"
        assert ls["timestamp_utc"] == "2026-04-27T18:30:01Z"
    finally:
        tg.last_signal = saved


TESTS = [
    test_short_cover_pair_counts_as_open_and_close,
    test_buy_sell_pair_still_counts,
    test_mixed_long_and_short_day,
    test_unknown_action_is_ignored,
    test_empty_trades_list,
    test_close_without_pnl_does_not_explode,
    test_last_signal_surfaces_on_state_payload,
    test_last_signal_emit_records_module_level,
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
