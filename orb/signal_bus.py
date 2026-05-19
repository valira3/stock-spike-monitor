"""Signal bus -- async fire-and-forget dispatch from Main's paper book
to executor listeners (TradeGeniusVal / TradeGeniusGene / synthetic
harness).

History. Lived inside trade_genius.py for v4.0.0-alpha through v9.1.140.
Carved out to its own module in v10.0.1 per the post-v10.0.0
architectural review: the bus is one of the few self-contained
sub-systems inside the monolith and re-including the new module in
lint is a real win. trade_genius.py keeps back-compat re-exports so
synthetic_harness + smoke_test + tests don't need touching.

Event schema (dict; same as before):
    {
      "kind": "ENTRY_LONG" | "ENTRY_SHORT" | "EXIT_LONG"
              | "EXIT_SHORT" | "EOD_CLOSE_ALL",
      "ticker": "AAPL",               # omitted on EOD_CLOSE_ALL
      "price": 175.42,                # main's reference price
      "reason": "BREAKOUT" | "STOP" | "TRAIL" | "RED_CANDLE" | ... ,
      "timestamp_utc": "2026-04-24T13:45:12Z",
      "main_shares": 57,              # audit-only: shares main paper book traded
    }
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional


logger = logging.getLogger(__name__)


_signal_listeners: list = []
_signal_listeners_lock = threading.Lock()

# Most recent signal Main's paper book emitted. The CANONICAL copy lives
# in trade_genius.last_signal (where it always has, so direct test writes
# like `tg.last_signal = X` continue working). _emit_signal updates that
# canonical copy via the optional injected setter below. We also keep a
# local mirror so tests that exercise this module in isolation
# (without trade_genius imported) can still observe the value.
last_signal: Optional[dict] = None

_last_signal_setter: Optional[Callable[[Optional[dict]], None]] = None


def set_last_signal_setter(fn: Optional[Callable[[Optional[dict]], None]]) -> None:
    """trade_genius.py calls this at import time so the bus can update
    the module-level `last_signal` attribute that the dashboard +
    test suite still expect to find on trade_genius itself."""
    global _last_signal_setter
    _last_signal_setter = fn


def _utc_now_iso() -> str:
    """ISO 8601 UTC timestamp. Inlined here to avoid a circular import
    with trade_genius.py (which previously hosted the bus + the helper)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def signal_bus_status() -> dict:
    """Snapshot of registered signal-bus listeners.

    Shape: {"n_listeners": int, "names": [str, ...]}.

    Names use the RUNTIME instance class name when the listener is a
    bound method (so TradeGeniusVal._on_signal renders as
    "TradeGeniusVal._on_signal" and not the inherited base's qualname,
    which broke v8.3.13's subscription probe). Falls back to qualname
    for free-function listeners.
    """
    with _signal_listeners_lock:
        listeners = list(_signal_listeners)
    names: list[str] = []
    for fn in listeners:
        try:
            inst = getattr(fn, "__self__", None)
            if inst is not None:
                cls_name = type(inst).__name__
                meth_name = getattr(fn, "__name__", "_on_signal")
                names.append(f"{cls_name}.{meth_name}")
            else:
                names.append(getattr(fn, "__qualname__", repr(fn)))
        except Exception:
            names.append(repr(fn))
    return {"n_listeners": len(names), "names": names}


def register_signal_listener(fn) -> None:
    """Subscribe a callable fn(event: dict) -> None to the bus.

    Idempotent: re-registering the same callable is a no-op so a
    supervisor re-spawn / module reload / paranoid init-retry can't
    double-fire ENTRY/EXIT against Alpaca. The read-test-append is
    held under _signal_listeners_lock so two concurrent start() calls
    can't both observe "not present" and append.
    """
    with _signal_listeners_lock:
        if fn in _signal_listeners:
            logger.info(
                "signal_bus: listener already registered, skipping (%s) total=%d",
                getattr(fn, "__qualname__", repr(fn)), len(_signal_listeners),
            )
            return
        _signal_listeners.append(fn)
        total = len(_signal_listeners)
    logger.info(
        "signal_bus: listener registered (%s) total=%d",
        getattr(fn, "__qualname__", repr(fn)), total,
    )


def _emit_signal(event: dict) -> None:
    """Fire an event to every listener in its own daemon thread.

    Async fire-and-forget: main's paper book never blocks on Alpaca.
    Per-listener exceptions are logged but never break the bus.

    Forensic tags:
        [SIGNAL-BUS-EMIT]     -- once per call, with kind+ticker
        [SIGNAL-BUS-DISPATCH] -- once per listener, before thread.start
        [SIGNAL-BUS-EMIT-VOID] -- WARNING when n_listeners=0 (most
            common root cause of val_gene_trades_match_main monitor
            violations -- main fired into the void, no executor mirrors)
    """
    global last_signal
    # v5.5.7 -- capture the latest event for the Main-tab LAST SIGNAL
    # card BEFORE dispatching, so a listener-less moment or a crashing
    # listener still updates what the dashboard renders.
    try:
        captured: Optional[dict] = {
            "kind": event.get("kind", ""),
            "ticker": event.get("ticker", ""),
            "price": float(event.get("price", 0.0) or 0.0),
            "reason": event.get("reason", ""),
            "timestamp_utc": event.get("timestamp_utc", _utc_now_iso()),
        }
    except Exception:
        captured = None
    last_signal = captured
    # Mirror into trade_genius.last_signal via the injected setter so
    # the dashboard's `getattr(trade_genius, "last_signal")` keeps
    # finding the fresh value.
    if _last_signal_setter is not None:
        try:
            _last_signal_setter(captured)
        except Exception:
            logger.exception("signal_bus: last_signal_setter raised")

    # Snapshot the listener list under the lock so concurrent
    # register/unregister can't mutate what we iterate.
    with _signal_listeners_lock:
        listeners = list(_signal_listeners)

    if not listeners:
        logger.warning(
            "[SIGNAL-BUS-EMIT-VOID] kind=%s ticker=%s n_listeners=0 "
            "-- Main emitted a signal but no executor is subscribed",
            event.get("kind", ""), event.get("ticker", ""),
        )
        return

    logger.info(
        "[SIGNAL-BUS-EMIT] kind=%s ticker=%s n_listeners=%d",
        event.get("kind", ""), event.get("ticker", ""), len(listeners),
    )

    def _wrap(fn, ev):
        try:
            fn(ev)
        except Exception:
            logger.exception(
                "signal_bus: listener %s raised on event %r",
                getattr(fn, "__qualname__", repr(fn)),
                ev.get("kind"),
            )

    for fn in listeners:
        logger.info(
            "[SIGNAL-BUS-DISPATCH] kind=%s ticker=%s listener=%s",
            event.get("kind", ""), event.get("ticker", ""),
            getattr(fn, "__qualname__", repr(fn)),
        )
        threading.Thread(
            target=_wrap, args=(fn, event), daemon=True,
        ).start()


def _clear_listeners_for_tests() -> None:
    """Test-only helper: reset bus state. Production code never calls this.
    Used by tests that need a fresh listener list per assertion."""
    global last_signal
    with _signal_listeners_lock:
        _signal_listeners.clear()
    last_signal = None
