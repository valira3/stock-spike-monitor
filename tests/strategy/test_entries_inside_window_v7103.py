"""v7.103.0 -- tests for inv_entries_inside_window.

Verifies the entry-time-vs-eligible-window invariant flags the
late-entry / EOD-cutoff violations and handles the time-string
parsing edge cases (HH:MM ET, HH:MM CDT legacy, ISO, bare HH:MM).
"""

from tools.dashboard_monitor_invariants import (
    _parse_trade_time_to_et_minutes,
    inv_entries_inside_window,
)


class _Ctx:
    """Minimal InvariantContext shim that matches _state(ctx) /
    _v10(ctx) lookups -- both read from ctx.payloads dict."""

    def __init__(self, state=None, v10=None):
        self.payloads = {}
        if state is not None:
            self.payloads["state"] = state
        if v10 is not None:
            # _v10(ctx) reads state.payloads["state"]["v10"]
            if "state" not in self.payloads:
                self.payloads["state"] = {}
            self.payloads["state"]["v10"] = v10


# ---------------------------------------------------------------------------
# _parse_trade_time_to_et_minutes
# ---------------------------------------------------------------------------


def test_parse_hh_mm_et_post_v7890():
    assert _parse_trade_time_to_et_minutes("13:30 ET") == 13 * 60 + 30


def test_parse_hh_mm_cdt_legacy_converts_to_et():
    # 09:30 CDT == 10:30 ET (CDT is 1h behind ET)
    assert _parse_trade_time_to_et_minutes("09:30 CDT") == 10 * 60 + 30


def test_parse_bare_hh_mm_treated_as_et():
    assert _parse_trade_time_to_et_minutes("14:05") == 14 * 60 + 5


def test_parse_iso_utc_converts_to_et():
    # 2026-05-11T14:30:00Z = 10:30 EDT (May = DST)
    result = _parse_trade_time_to_et_minutes("2026-05-11T14:30:00Z")
    assert result == 10 * 60 + 30


def test_parse_invalid_returns_none():
    assert _parse_trade_time_to_et_minutes("garbage") is None
    assert _parse_trade_time_to_et_minutes("") is None
    assert _parse_trade_time_to_et_minutes(None) is None


# ---------------------------------------------------------------------------
# inv_entries_inside_window
# ---------------------------------------------------------------------------


def _trade(action, ticker, time_str):
    return {"action": action, "ticker": ticker, "time": time_str}


def test_skipped_when_no_state():
    ctx = _Ctx(state=None)
    out = inv_entries_inside_window(ctx)
    assert out["ok"] is True
    assert "skipped" in (out.get("summary") or "")


def test_skipped_when_no_trades():
    ctx = _Ctx(state={"trades_today": []})
    out = inv_entries_inside_window(ctx)
    assert out["ok"] is True


def test_entries_inside_window_pass():
    """Entries at 10:30 and 14:45 ET should both be inside the default
    eligible window [10:00, 15:55]."""
    state = {
        "trades_today": [
            _trade("BUY", "AAPL", "10:30 ET"),
            _trade("SHORT", "TSLA", "14:45 ET"),
        ],
    }
    out = inv_entries_inside_window(_Ctx(state=state))
    assert out["ok"] is True


def test_entries_before_or_close_flagged():
    """An entry at 09:45 ET fires BEFORE the OR window closes (10:00)
    and should be flagged."""
    state = {
        "trades_today": [_trade("BUY", "AAPL", "09:45 ET")],
    }
    out = inv_entries_inside_window(_Ctx(state=state))
    assert out["ok"] is False
    assert "AAPL" in (out.get("detail") or "")
    assert "BUY" in (out.get("detail") or "")


def test_entries_after_eod_cutoff_flagged():
    """An entry at 15:58 ET fires AFTER the eod_cutoff (15:55) and
    should be flagged."""
    state = {
        "trades_today": [_trade("SHORT", "META", "15:58 ET")],
    }
    out = inv_entries_inside_window(_Ctx(state=state))
    assert out["ok"] is False
    assert "META" in (out.get("detail") or "")


def test_exits_not_counted_as_entries():
    """SELL / COVER actions are exits, not entries -- they can fire
    any time (including in the EOD cutoff zone for forced closes).
    They should be silently ignored by this invariant."""
    state = {
        "trades_today": [
            _trade("SELL", "AAPL", "15:59 ET"),   # exit at 15:59 -- fine
            _trade("COVER", "TSLA", "15:59 ET"),  # exit at 15:59 -- fine
        ],
    }
    out = inv_entries_inside_window(_Ctx(state=state))
    assert out["ok"] is True


def test_uses_v10_config_when_provided():
    """Custom session_start_minutes / or_minutes / eod_cutoff override
    the defaults so the invariant adapts to a config change."""
    state = {
        "trades_today": [_trade("BUY", "AAPL", "10:00 ET")],
        # _v10(ctx) requires available!=False AND bootstrapped is truthy
        # for the v10 block to be consumed.
        "v10": {
            "available": True,
            "bootstrapped": True,
            "config": {
                "session_start_minutes": 9 * 60 + 30,
                "or_minutes": 60,  # extended OR -- window opens at 10:30 ET
                "eod_cutoff_minutes": 15 * 60 + 55,
            },
        },
    }
    # 10:00 ET is BEFORE the (extended) eligible_start=10:30
    out = inv_entries_inside_window(_Ctx(state=state))
    assert out["ok"] is False


def test_late_first_entry_pattern_from_today():
    """Regression: 2026-05-11 first entry at 12:14 ET. This is INSIDE
    the eligible window (eligible_start=10:00, eligible_end=15:55)
    so it does NOT trip the invariant. The invariant catches
    out-of-window violations, not just-late-but-eligible ones. A
    separate `inv_first_entry_promptness` could layer on top later;
    keeping this invariant narrowly focused for now.
    """
    state = {
        "trades_today": [_trade("SHORT", "META", "12:14 ET")],
    }
    out = inv_entries_inside_window(_Ctx(state=state))
    # 12:14 ET is inside [10:00, 15:55] -> pass
    assert out["ok"] is True
