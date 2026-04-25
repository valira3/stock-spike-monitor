"""runner — orchestrates scenarios end-to-end.

run_scenario(name)      -> dict of recorded outputs (the would-be golden)
record_scenario(name)   -> writes goldens/<name>.json
replay_scenario(name)   -> (ok, diff_text)
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import sys
from pathlib import Path

from synthetic_harness.clock import FrozenClock
from synthetic_harness.market import SyntheticMarket
from synthetic_harness.recorder import OutputRecorder
from synthetic_harness.install import install, uninstall
from synthetic_harness.state import state_snapshot, state_diff, CAPTURE_KEYS
from synthetic_harness.scenarios import (
    Scenario,
    Action,
    SCENARIOS,
    get_scenario,
)

GOLDENS_DIR = Path(__file__).parent / "goldens"
HARNESS_VERSION = 1


def _import_trade_genius():
    """Import trade_genius lazily so the harness is usable as a pure module."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # Make sure smoke-test mode is set so trade_genius doesn't try to
    # talk to Telegram at import time.
    os.environ.setdefault("SSM_SMOKE_TEST", "1")
    os.environ.setdefault("CHAT_ID", "999999999")
    os.environ.setdefault(
        "TELEGRAM_TOKEN",
        "0000000000:AAAA_smoke_placeholder_token_0000000",
    )
    os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
    return importlib.import_module("trade_genius")


def _reset_module_state(m, sc: Scenario) -> None:
    """Wipe module state to scenario.initial_state."""
    m.positions.clear()
    m.short_positions.clear()
    m.paper_trades.clear()
    m.paper_all_trades.clear()
    m.trade_history.clear()
    m.short_trade_history.clear()
    m.daily_entry_count.clear()
    m.daily_short_entry_count.clear()
    m.or_high.clear()
    m.or_low.clear()
    m.pdc.clear()
    m._last_exit_time.clear()
    m.paper_cash = m.PAPER_STARTING_CAPITAL
    m._trading_halted = False
    m._trading_halted_reason = ""
    m._scan_paused = False
    m.daily_entry_date = ""
    m.daily_short_entry_date = ""
    # v4.8.2 \u2014 reset module flags that scenario setup_callbacks may
    # toggle, so a flag flipped on by one scenario doesn't leak into the
    # next. Defaults mirror trade_genius env-var defaults at import time.
    m.TIGER_V2_REQUIRE_VOL = False

    init = sc.initial_state or {}
    for k, v in init.items():
        # Mutate in place for collections so other module references stay live.
        cur = getattr(m, k, None)
        if isinstance(cur, dict) and isinstance(v, dict):
            cur.clear()
            cur.update(copy.deepcopy(v))
        elif isinstance(cur, list) and isinstance(v, list):
            cur.clear()
            cur.extend(copy.deepcopy(v))
        else:
            setattr(m, k, copy.deepcopy(v))


def _setup_market(market: SyntheticMarket, sc: Scenario) -> None:
    for ticker, frame in (sc.initial_market or {}).items():
        # frames are TickerFrame instances or already-built dicts.
        market.set_frame(ticker, frame)


def _dispatch(action: Action, m, market: SyntheticMarket,
              clock: FrozenClock):
    """Run a single action against trade_genius. Returns rv."""
    k = action.kind
    args = action.args
    if k == "check_entry":
        return m.check_entry(*args)
    if k == "check_short_entry":
        return m.check_short_entry(*args)
    if k == "execute_entry":
        return m.execute_entry(*args)
    if k == "execute_short_entry":
        return m.execute_short_entry(*args)
    if k == "close_position":
        return m.close_position(*args)
    if k == "close_short_position":
        return m.close_short_position(*args)
    if k == "scan_loop":
        return m.scan_loop()
    if k == "manage_positions":
        return m.manage_positions()
    if k == "manage_short_positions":
        return m.manage_short_positions()
    if k == "eod_close":
        return m.eod_close()
    if k == "tick_minutes":
        clock.tick_minutes(args[0])
        return None
    if k == "tick_seconds":
        clock.tick_seconds(args[0])
        return None
    if k == "set_price":
        ticker, px = args
        market.update_price(ticker, px)
        return None
    if k == "set_frame":
        ticker, frame = args
        market.set_frame(ticker, frame)
        return None
    if k == "set_global":
        attr, val = args
        cur = getattr(m, attr, None)
        if isinstance(cur, dict) and isinstance(val, dict):
            cur.clear()
            cur.update(copy.deepcopy(val))
        elif isinstance(cur, list) and isinstance(val, list):
            cur.clear()
            cur.extend(copy.deepcopy(val))
        else:
            setattr(m, attr, copy.deepcopy(val))
        return None
    raise ValueError(f"unknown action kind: {k}")


def _action_to_jsonable(action: Action) -> dict:
    out = {"kind": action.kind, "label": action.label}
    safe_args = []
    for a in action.args:
        # Skip TickerFrame and other complex objects in the recorded args
        if hasattr(a, "bars_1min"):
            safe_args.append({"_TickerFrame": "set_frame"})
        elif isinstance(a, (str, int, float, bool, type(None))):
            safe_args.append(a)
        elif isinstance(a, (list, tuple)):
            safe_args.append(list(a))
        elif isinstance(a, dict):
            safe_args.append(a)
        else:
            safe_args.append(repr(a))
    out["args"] = safe_args
    return out


def _rv_to_jsonable(rv):
    if rv is None:
        return None
    if isinstance(rv, tuple):
        return [_rv_to_jsonable(v) for v in rv]
    if isinstance(rv, list):
        return [_rv_to_jsonable(v) for v in rv]
    if isinstance(rv, dict):
        return {k: _rv_to_jsonable(v) for k, v in rv.items()}
    if isinstance(rv, (str, int, float, bool)):
        return rv
    return repr(rv)


def run_scenario(name: str) -> dict:
    """Run a scenario and return the recorded-outputs dict (golden form)."""
    sc = get_scenario(name)
    m = _import_trade_genius()

    clock = FrozenClock(sc.initial_time)
    market = SyntheticMarket()
    recorder = OutputRecorder()

    saved = install(m, clock, market, recorder)
    try:
        _reset_module_state(m, sc)
        _setup_market(market, sc)
        for cb in (sc.setup_callbacks or []):
            cb(m, clock, market, recorder)

        actions_out = []
        for action in sc.actions:
            recorder.reset()
            before = state_snapshot(m)
            rv = _dispatch(action, m, market, clock)
            after = state_snapshot(m)
            delta = state_diff(before, after)
            entry = {
                "action": _action_to_jsonable(action),
                "return_value": _rv_to_jsonable(rv),
                "state_delta": delta,
            }
            entry.update(recorder.to_dict())
            actions_out.append(entry)

        bot_version = getattr(m, "BOT_VERSION", "unknown")
    finally:
        uninstall(m, saved)
        # Clear any state we left behind so the next scenario starts clean.
        _reset_module_state(m, Scenario(name="__cleanup__", description=""))

    return {
        "scenario": name,
        "harness_version": HARNESS_VERSION,
        "trade_genius_version": bot_version,
        "actions": actions_out,
    }


def record_scenario(name: str) -> Path:
    """Run a scenario and write its golden JSON file."""
    GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
    payload = run_scenario(name)
    path = GOLDENS_DIR / f"{name}.json"
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def replay_scenario(name: str) -> tuple[bool, str]:
    """Run a scenario and compare to its golden file. Returns (ok, diff)."""
    path = GOLDENS_DIR / f"{name}.json"
    if not path.exists():
        return False, f"no golden file at {path}"
    expected = json.loads(path.read_text(encoding="utf-8"))
    observed = run_scenario(name)
    # v4.11.5 - strip trade_genius_version from BOTH sides before
    # comparing so a bot version bump alone never invalidates 50 goldens.
    # record_scenario() still stamps the current version into freshly
    # recorded goldens; this only affects the replay/compare path.
    if isinstance(observed, dict):
        observed.pop("trade_genius_version", None)
    if isinstance(expected, dict):
        expected.pop("trade_genius_version", None)
    obs_text = json.dumps(observed, indent=2, sort_keys=True, default=str)
    exp_text = json.dumps(expected, indent=2, sort_keys=True, default=str)
    if obs_text == exp_text:
        return True, ""
    import difflib
    diff = "\n".join(difflib.unified_diff(
        exp_text.splitlines(),
        obs_text.splitlines(),
        fromfile=f"golden:{name}",
        tofile=f"observed:{name}",
        lineterm="",
    ))
    # Cap diff length so failure messages stay readable.
    if len(diff) > 4000:
        diff = diff[:4000] + "\n... (diff truncated)"
    return False, diff
