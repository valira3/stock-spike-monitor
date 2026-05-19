"""v10.0.1 -- orb.signal_bus extracted from trade_genius.py.

These tests cover the carved module in isolation. The pre-existing
test_v5_5_7_dashboard_main_fix.py (at the repo root) still exercises
the trade_genius re-export shim end-to-end; this file pins the
internal contract of the new module so a future refactor can't
silently change the dispatch semantics.

Covered:
  - register_signal_listener: idempotent under repeat registration
  - _emit_signal: writes last_signal BEFORE dispatch, even on empty
    listener list, even when a listener raises
  - _emit_signal: calls every listener once per event, in daemon threads
  - _emit_signal: empty listener list emits the [SIGNAL-BUS-EMIT-VOID]
    WARNING (not the regular EMIT log)
  - signal_bus_status: returns runtime-class names for bound methods
  - set_last_signal_setter: injected setter receives the captured event
"""
from __future__ import annotations

import logging
import threading
import time

import orb.signal_bus as sb


def _reset():
    sb._clear_listeners_for_tests()
    sb.set_last_signal_setter(None)


# ---------------------------------------------------------------------------
# Listener registration
# ---------------------------------------------------------------------------


def test_register_signal_listener_appends_once():
    _reset()
    def f(e): pass
    sb.register_signal_listener(f)
    assert sb.signal_bus_status()["n_listeners"] == 1


def test_register_signal_listener_is_idempotent():
    """Re-registering the same callable must be a no-op (prevents
    double-fire of ENTRY/EXIT against Alpaca when start() is called
    twice -- supervisor re-spawn / paranoid init-retry)."""
    _reset()
    def f(e): pass
    sb.register_signal_listener(f)
    sb.register_signal_listener(f)
    sb.register_signal_listener(f)
    assert sb.signal_bus_status()["n_listeners"] == 1


def test_register_multiple_distinct_listeners():
    _reset()
    sb.register_signal_listener(lambda e: None)
    sb.register_signal_listener(lambda e: None)
    sb.register_signal_listener(lambda e: None)
    assert sb.signal_bus_status()["n_listeners"] == 3


# ---------------------------------------------------------------------------
# _emit_signal -- happy path + listener-less + raising-listener
# ---------------------------------------------------------------------------


def test_emit_signal_writes_last_signal_before_dispatch():
    """v5.5.7 invariant: last_signal must be updated even if no
    listener exists (so the dashboard renders the most recent event
    on the Main tab regardless of subscription state)."""
    _reset()
    sb._emit_signal({
        "kind": "ENTRY_LONG", "ticker": "AAPL", "price": 100.5,
        "reason": "BREAKOUT", "timestamp_utc": "2026-05-20T13:31:00Z",
    })
    assert sb.last_signal is not None
    assert sb.last_signal["kind"] == "ENTRY_LONG"
    assert sb.last_signal["ticker"] == "AAPL"
    assert sb.last_signal["price"] == 100.5


def test_emit_signal_void_logs_warning(caplog):
    _reset()
    with caplog.at_level(logging.WARNING, logger="orb.signal_bus"):
        sb._emit_signal({"kind": "ENTRY_LONG", "ticker": "AAPL", "price": 1.0})
    msgs = [r.message for r in caplog.records if "[SIGNAL-BUS-EMIT-VOID]" in r.message]
    assert len(msgs) == 1, "void emit must log exactly once at WARNING"


def test_emit_signal_dispatches_to_every_listener_in_thread():
    _reset()
    got: list[dict] = []
    received = threading.Event()
    def listener(e):
        got.append(e)
        if len(got) >= 3:
            received.set()
    sb.register_signal_listener(listener)
    sb.register_signal_listener(listener)  # idempotent -> still 1 reg
    # Add 2 more distinct listeners
    sb.register_signal_listener(lambda e: got.append(e))
    sb.register_signal_listener(lambda e: got.append(e))
    sb._emit_signal({"kind": "ENTRY_LONG", "ticker": "AAPL", "price": 1.0})
    received.wait(timeout=2.0)
    # 3 distinct listeners, 1 event each -> 3 entries
    assert len(got) == 3


def test_emit_signal_listener_exception_does_not_break_bus(caplog):
    """A buggy listener must not stop other listeners or crash the bus.
    The bus must log the exception and keep dispatching."""
    _reset()
    good_got = threading.Event()
    bad_done = threading.Event()
    def bad(e):
        try:
            raise RuntimeError("boom")
        finally:
            bad_done.set()
    sb.register_signal_listener(bad)
    sb.register_signal_listener(lambda e: good_got.set())
    with caplog.at_level(logging.ERROR, logger="orb.signal_bus"):
        sb._emit_signal({"kind": "ENTRY_LONG", "ticker": "AAPL", "price": 1.0})
        assert good_got.wait(timeout=2.0), "well-behaved listener didn't fire"
        assert bad_done.wait(timeout=2.0), "bad listener didn't run"
        # The logger.exception in _wrap runs AFTER the raise; give the
        # daemon thread a final tick to flush the record into caplog.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            err = [r.message for r in caplog.records
                   if "raised on event" in r.message]
            if err:
                break
            time.sleep(0.02)
    err_msgs = [r.message for r in caplog.records
                if "raised on event" in r.message]
    assert len(err_msgs) >= 1, (
        f"expected at least one 'raised on event' log, got "
        f"{[r.message[:80] for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# set_last_signal_setter injection
# ---------------------------------------------------------------------------


def test_set_last_signal_setter_receives_each_event():
    """The setter is how trade_genius.last_signal stays in sync with
    the carved bus's view."""
    _reset()
    captured: list = []
    sb.set_last_signal_setter(captured.append)
    sb._emit_signal({"kind": "EXIT_LONG", "ticker": "TSLA", "price": 250.0})
    assert len(captured) == 1
    assert captured[0]["kind"] == "EXIT_LONG"
    assert captured[0]["ticker"] == "TSLA"


def test_set_last_signal_setter_can_be_unset():
    _reset()
    sb.set_last_signal_setter(lambda v: None)
    sb.set_last_signal_setter(None)  # clear
    # _emit_signal should still work; setter is optional
    sb._emit_signal({"kind": "ENTRY_LONG", "ticker": "AAPL", "price": 1.0})
    assert sb.last_signal is not None  # module-local copy still updated


def test_set_last_signal_setter_exception_does_not_break_bus(caplog):
    """A broken setter must not strand the bus -- only log."""
    _reset()
    def broken_setter(v):
        raise RuntimeError("setter blew up")
    sb.set_last_signal_setter(broken_setter)
    with caplog.at_level(logging.ERROR, logger="orb.signal_bus"):
        sb._emit_signal({"kind": "ENTRY_LONG", "ticker": "AAPL", "price": 1.0})
    err = [r.message for r in caplog.records if "last_signal_setter raised" in r.message]
    assert len(err) == 1


# ---------------------------------------------------------------------------
# signal_bus_status
# ---------------------------------------------------------------------------


def test_signal_bus_status_uses_runtime_class_name_for_bound_method():
    """v8.3.19 invariant: bound-method listeners should report their
    RUNTIME instance class, not the inherited __qualname__. Otherwise
    a subclassed _on_signal renders as the base class and breaks the
    v8.3.13 subscription probe."""
    _reset()

    class Base:
        def _on_signal(self, e): pass

    class TradeGeniusValStub(Base):
        pass  # inherits Base._on_signal; bound method's class is TradeGeniusValStub

    inst = TradeGeniusValStub()
    sb.register_signal_listener(inst._on_signal)
    names = sb.signal_bus_status()["names"]
    assert names == ["TradeGeniusValStub._on_signal"], names


def test_signal_bus_status_uses_qualname_for_free_function():
    _reset()
    def my_listener(e): pass
    sb.register_signal_listener(my_listener)
    names = sb.signal_bus_status()["names"]
    assert "my_listener" in names[0]


def test_signal_bus_status_handles_zero_listeners():
    _reset()
    s = sb.signal_bus_status()
    assert s == {"n_listeners": 0, "names": []}


# ---------------------------------------------------------------------------
# _clear_listeners_for_tests
# ---------------------------------------------------------------------------


def test_clear_listeners_for_tests_resets_state():
    _reset()
    sb.register_signal_listener(lambda e: None)
    sb._emit_signal({"kind": "ENTRY_LONG", "ticker": "AAPL", "price": 1.0})
    assert sb.signal_bus_status()["n_listeners"] == 1
    assert sb.last_signal is not None
    sb._clear_listeners_for_tests()
    assert sb.signal_bus_status()["n_listeners"] == 0
    assert sb.last_signal is None
