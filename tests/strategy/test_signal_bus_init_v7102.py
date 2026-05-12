"""v7.102.0 -- tests for executors.bootstrap.emit_signal_bus_init_complete.

The function emits one of:
  INFO  [SIGNAL-BUS-INIT-COMPLETE] expected=N actual=M val=on/off gene=on/off
  ERROR [SIGNAL-BUS-INIT-COMPLETE] ... -- BUS LEAK ...

Pure-function with one external dep (trade_genius.signal_bus_status).
We patch that out and assert the log level + message contents.
"""

import logging
from unittest.mock import patch, MagicMock

# v7.102.0 -- import from tools/signal_bus_audit.py, NOT
# executors/bootstrap.py. The latter triggers executors/__init__.py
# which eagerly imports executors.base, which imports `telegram` --
# not installed in the strategy-tests CI lane (`pip install pytest
# tzdata` only). tools.signal_bus_audit has no heavy imports.
from tools.signal_bus_audit import emit_signal_bus_init_complete


def _capture(caplog, level=logging.DEBUG):
    caplog.set_level(level, logger="executors.bootstrap")


def test_emit_happy_path_both_enabled_both_subscribed(caplog):
    _capture(caplog, logging.INFO)
    fake_tg = MagicMock()
    fake_tg.signal_bus_status.return_value = {"n_listeners": 2, "names": ["Val", "Gene"]}
    with patch.dict("sys.modules", {"trade_genius": fake_tg}):
        emit_signal_bus_init_complete(val=MagicMock(), gene=MagicMock())
    msgs = [r.getMessage() for r in caplog.records]
    info_msgs = [m for m in msgs if "[SIGNAL-BUS-INIT-COMPLETE]" in m]
    assert info_msgs
    assert any("expected=2 actual=2" in m for m in info_msgs)
    assert any("val=on" in m and "gene=on" in m for m in info_msgs)
    # No ERROR-level records.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors


def test_emit_bus_leak_when_listener_count_below_expected(caplog):
    """Both executors enabled but only one registered -> ERROR."""
    _capture(caplog, logging.INFO)
    fake_tg = MagicMock()
    fake_tg.signal_bus_status.return_value = {"n_listeners": 1, "names": ["Val"]}
    with patch.dict("sys.modules", {"trade_genius": fake_tg}):
        emit_signal_bus_init_complete(val=MagicMock(), gene=MagicMock())
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "BUS LEAK should be ERROR-level"
    msg = errors[0].getMessage()
    assert "[SIGNAL-BUS-INIT-COMPLETE]" in msg
    assert "expected=2 actual=1" in msg
    assert "BUS LEAK" in msg


def test_emit_no_executors_enabled_no_error(caplog):
    """Both executors None (disabled) -> 0 expected, 0 actual is OK."""
    _capture(caplog, logging.INFO)
    fake_tg = MagicMock()
    fake_tg.signal_bus_status.return_value = {"n_listeners": 0, "names": []}
    with patch.dict("sys.modules", {"trade_genius": fake_tg}):
        emit_signal_bus_init_complete(val=None, gene=None)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("expected=0 actual=0" in m for m in info_msgs)
    assert any("val=off" in m and "gene=off" in m for m in info_msgs)


def test_emit_status_lookup_failure_is_swallowed(caplog):
    """If signal_bus_status raises (mid-refactor, import error, etc.),
    the function logs at DEBUG and returns -- never raises."""
    _capture(caplog, logging.DEBUG)
    fake_tg = MagicMock()
    fake_tg.signal_bus_status.side_effect = RuntimeError("boom")
    with patch.dict("sys.modules", {"trade_genius": fake_tg}):
        # Must not raise.
        emit_signal_bus_init_complete(val=MagicMock(), gene=None)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors
    debugs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("status lookup failed" in m for m in debugs)
