"""tests/strategy/test_simulator.py -- smoke tests for the simulator.

These are NOT a substitute for the rule-level coverage in
tests/strategy/test_orb_session_sim.py. They prove the simulator
plumbing works end-to-end: mocks install / uninstall cleanly, the
clock advances, the bar feeder serves bars, scenarios complete
without raising.
"""
from __future__ import annotations

import os

import pytest


def _runner(name: str):
    from simulator import SimulatorRunner
    return SimulatorRunner.from_scenario(name)


def test_runner_imports_clean():
    """The simulator package must import without raising even when
    optional deps (alpaca-py, telegram) are missing -- the mocks inject
    synthetic modules into sys.modules at install time."""
    from simulator import SimulatorRunner  # noqa: F401
    from simulator.bar_feeder import BarFeeder, make_bar  # noqa: F401
    from simulator.clock import SimulatedClock  # noqa: F401
    from simulator.scenarios import list_scenarios

    assert "golden_orb_long" in list_scenarios()
    assert "gap_skip" in list_scenarios()
    assert "range_too_narrow" in list_scenarios()


def test_clock_advances():
    from simulator.clock import SimulatedClock

    c = SimulatedClock.at_et(date="2026-05-15", hour=9, minute=30)
    c.install()
    try:
        et = c.now_et
        assert et.hour == 9 and et.minute == 30
        c.advance(minutes=5)
        et2 = c.now_et
        assert et2.hour == 9 and et2.minute == 35
        # The bot does `import datetime; datetime.datetime.now()` at
        # call time, so module-attribute swap takes effect. Code that
        # does `from datetime import datetime` at import time keeps a
        # local binding and is NOT redirected -- a fundamental Python
        # constraint, not a simulator bug.
        import datetime as _dt
        live_now = _dt.datetime.now()
        assert live_now.year == 2026
        assert live_now.month == 5
        assert live_now.day == 15
    finally:
        c.uninstall()


def test_bar_feeder_synthetic():
    from simulator.bar_feeder import BarFeeder, make_bar

    bars = [
        make_bar("2026-05-15", 9, 30, 100.0, 100.5, 99.8, 100.3),
        make_bar("2026-05-15", 9, 31, 100.3, 100.6, 100.1, 100.4),
        make_bar("2026-05-15", 9, 35, 100.4, 100.7, 100.2, 100.5),
    ]
    feeder = BarFeeder.from_synthetic("2026-05-15", {"AAPL": bars})

    assert feeder.tickers() == ["AAPL"]
    assert feeder.bar_at("AAPL", 9 * 60 + 30) is not None
    assert feeder.bar_at("AAPL", 9 * 60 + 32) is None  # no bar at 09:32
    up_to_31 = feeder.bars_up_to("AAPL", 9 * 60 + 31)
    assert len(up_to_31) == 2  # 09:30 + 09:31


@pytest.mark.parametrize("scenario", ["golden_orb_long", "gap_skip", "range_too_narrow"])
def test_built_in_scenarios_run_clean(scenario, tmp_path):
    """Every built-in scenario completes without raising and produces a
    well-shaped state dict. The exact entry/exit counts are NOT asserted
    here because they depend on v10 keystone gates that need real SPY
    regime data; the goal is plumbing-level fidelity, not strategy
    fidelity."""
    os.environ["TG_DATA_ROOT"] = str(tmp_path)
    runner = _runner(scenario)
    state = runner.run()

    # Plumbing assertions.
    assert "entries" in state
    assert "exits" in state
    assert "telegram_sends" in state
    assert "alpaca_orders" in state
    assert "alpaca_positions" in state
    assert "fmp_calls" in state
    assert "yahoo_calls" in state
    assert isinstance(state["entries"], list)
    assert isinstance(state["exits"], list)
    assert isinstance(state["telegram_sends"], list)


def test_mocks_capture_telegram(tmp_path):
    """If the bot tries to send a Telegram message during a scenario,
    the mock captures it instead of hitting the real api.telegram.org."""
    import urllib.request

    from simulator import SimulatorRunner

    os.environ["TG_DATA_ROOT"] = str(tmp_path)
    runner = SimulatorRunner.from_scenario("golden_orb_long")
    runner.setup()
    try:
        # Manually fire a telegram send through the patched urlopen.
        req = urllib.request.Request(
            "https://api.telegram.org/bot999/sendMessage",
            data=b"chat_id=123&text=hello",
        )
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            assert b'"ok": true' in body
        assert len(runner.state["telegram_sends"]) == 1
        assert runner.state["telegram_sends"][0]["text"] == "hello"
        assert runner.state["telegram_sends"][0]["chat_id"] == "123"
    finally:
        runner.teardown()


def test_mocks_capture_fmp(tmp_path):
    """FMP quote endpoint is intercepted; the mock returns synthetic
    data sourced from the bar feeder at the current clock bucket."""
    import json
    import urllib.request

    from simulator import SimulatorRunner

    os.environ["TG_DATA_ROOT"] = str(tmp_path)
    runner = SimulatorRunner.from_scenario("golden_orb_long")
    runner.setup()
    try:
        req = urllib.request.Request(
            "https://financialmodelingprep.com/api/v3/quote/AAPL?apikey=fake",
        )
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read())
        assert isinstance(payload, list)
        assert payload[0]["symbol"] == "AAPL"
        assert "price" in payload[0]
        assert len(runner.state["fmp_calls"]) == 1
    finally:
        runner.teardown()


# ----- error simulation ---------------------------------------------


def test_alpaca_rejects_zero_qty(tmp_path):
    """Alpaca's real API returns 422 on qty=0. The mock must match so
    the bot's order-submit error path gets exercised."""
    from simulator import SimulatorRunner
    from simulator.mocks.mock_errors import MockAlpacaAPIError

    os.environ["TG_DATA_ROOT"] = str(tmp_path)
    runner = SimulatorRunner.from_scenario("golden_orb_long")
    runner.setup()
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide

        tc = TradingClient("key", "secret", paper=True)
        req = LimitOrderRequest(
            symbol="AAPL", qty=0, side=OrderSide.BUY,
            type="limit", limit_price=100.0,
        )
        with pytest.raises(MockAlpacaAPIError) as exc:
            tc.submit_order(req)
        assert exc.value.status_code == 422
    finally:
        runner.teardown()


def test_alpaca_rejects_missing_position(tmp_path):
    """close_position on a non-existent symbol must raise 404."""
    from simulator import SimulatorRunner
    from simulator.mocks.mock_errors import MockAlpacaAPIError

    os.environ["TG_DATA_ROOT"] = str(tmp_path)
    runner = SimulatorRunner.from_scenario("golden_orb_long")
    runner.setup()
    try:
        from alpaca.trading.client import TradingClient

        tc = TradingClient("key", "secret", paper=True)
        with pytest.raises(MockAlpacaAPIError) as exc:
            tc.close_position("NEVERHELD")
        assert exc.value.status_code == 404
    finally:
        runner.teardown()


def test_fmp_quote_returns_504_when_injected(tmp_path):
    """fmp_quote_timeout injection turns /quote calls into 504."""
    import urllib.error
    import urllib.request
    from simulator import SimulatorRunner

    os.environ["TG_DATA_ROOT"] = str(tmp_path)
    runner = SimulatorRunner.from_scenario("fmp_quote_timeout")
    runner.setup()
    try:
        req = urllib.request.Request(
            "https://financialmodelingprep.com/api/v3/quote/AAPL",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 504
    finally:
        runner.teardown()


def test_telegram_unauthorized_when_injected(tmp_path):
    """telegram_unauthorized injection makes every send return 401."""
    import urllib.error
    import urllib.request
    from simulator import SimulatorRunner

    os.environ["TG_DATA_ROOT"] = str(tmp_path)
    runner = SimulatorRunner.from_scenario("telegram_unauthorized")
    runner.setup()
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot999/sendMessage",
            data=b"chat_id=1&text=test",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 401
        # The send was recorded with the failure status.
        sends = runner.state["telegram_sends"]
        assert sends and sends[-1].get("status") == 401
    finally:
        runner.teardown()


def test_alpaca_rate_limit_decrements(tmp_path):
    """alpaca_rate_limited=N injects 429 for the first N submit_order
    calls, then succeeds."""
    from simulator import SimulatorRunner
    from simulator.mocks.mock_errors import MockAlpacaAPIError

    os.environ["TG_DATA_ROOT"] = str(tmp_path)
    runner = SimulatorRunner.from_scenario("alpaca_rate_limited")
    runner.setup()
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide

        tc = TradingClient("key", "secret", paper=True)
        # First 3 calls should raise 429 (per scenario inject_failures).
        for _ in range(3):
            req = LimitOrderRequest(
                symbol="AAPL", qty=10, side=OrderSide.BUY,
                type="limit", limit_price=100.0,
            )
            with pytest.raises(MockAlpacaAPIError) as exc:
                tc.submit_order(req)
            assert exc.value.status_code == 429
        # 4th call must succeed.
        req = LimitOrderRequest(
            symbol="AAPL", qty=10, side=OrderSide.BUY,
            type="limit", limit_price=100.0,
        )
        order = tc.submit_order(req)
        assert order.status == "filled"
    finally:
        runner.teardown()
