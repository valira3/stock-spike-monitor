"""v7.102.0 -- signal-bus init audit (extracted from bootstrap.py).

The audit helper is kept in its own minimal-imports module so the
strategy-tests CI lane (which only installs `pytest tzdata` and
cannot import `executors.bootstrap` due to its transitive
`telegram` dep) can exercise the helper directly.

Behaviour and call site are unchanged: `executors.bootstrap`
re-exports `emit_signal_bus_init_complete` so existing imports
keep working.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("executors.bootstrap")


def emit_signal_bus_init_complete(
    *,
    val: Optional[Any] = None,
    gene: Optional[Any] = None,
) -> None:
    """One-shot startup boundary log confirming the signal bus has
    the listener count we expected.

    Each enabled executor (Val, Gene) calls
    `tg.register_signal_listener(self._on_signal)` from `start()`.
    If a build/start raised silently or `register_signal_listener`
    short-circuited, the bus is empty and Main's signal emits fire
    into the void -- the exact failure mode the v7.90.0 invariant
    chase has been targeting. Pre-v7.102.0 this only surfaced AFTER
    the first trade-count divergence (hours into RTH); now we get
    a `[SIGNAL-BUS-INIT-COMPLETE]` line at boot with expected vs
    actual counts, and an ERROR-level escalation if they mismatch.

    Logging only -- never raises. Best-effort: if signal_bus_status
    isn't importable for some reason (e.g. mid-refactor), we log a
    debug message and move on.
    """
    expected = 0
    if val is not None:
        expected += 1
    if gene is not None:
        expected += 1
    try:
        import trade_genius
        actual = int(trade_genius.signal_bus_status().get("n_listeners") or 0)
    except Exception as exc:
        logger.debug("[SIGNAL-BUS-INIT-COMPLETE] status lookup failed: %s", exc)
        return
    payload = (
        "expected=%d actual=%d val=%s gene=%s"
        % (
            expected,
            actual,
            "on" if val is not None else "off",
            "on" if gene is not None else "off",
        )
    )
    if actual < expected:
        logger.error(
            "[SIGNAL-BUS-INIT-COMPLETE] %s -- BUS LEAK: bus has fewer "
            "listeners than enabled executors. Main's _emit_signal "
            "calls will fire into the void for the missing leg(s). "
            "Likely cause: an executor's start() raised before "
            "register_signal_listener ran.",
            payload,
        )
    else:
        logger.info("[SIGNAL-BUS-INIT-COMPLETE] %s", payload)
