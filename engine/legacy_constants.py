"""Re-export layer for legacy eye_of_tiger constants that broker/* still
uses for live trade execution.

v10.0.1 Tier-B step 1. The actual definitions still live in
`eye_of_tiger.py`; this module is a thin re-export so the broker code
no longer has direct module-level coupling to the legacy file. The
multi-step retirement plan is:

  Step 1 (this commit) -- introduce this re-export shim; flip broker/
    imports. Zero behavior change.

  Step 2 (future PR, after 24h+ paper observation) -- move the const
    *definitions* from eye_of_tiger.py into this module. Replace the
    re-exports here with the literal values; eye_of_tiger.py then
    re-imports them for any remaining back-compat callers.

  Step 3 (future PR) -- delete eye_of_tiger.py + v5_10_1_integration.py
    once nothing references them.

What re-exports here (used by broker/orders.py + broker/positions.py):
  - CANCEL_ACK_TIMEOUT_MS        broker/orders.py:226
  - ENTRY_1_SIZE_PCT             broker/orders.py:1086,1095; positions:895
  - ENTRY_2_SIZE_PCT             broker/positions.py:895
  - V611_REGIME_B_ENABLED        broker/orders.py:1122
  - V611_REGIME_B_SHORT_SCALE_MULT, V611_REGIME_B_SHORT_ARM_HHMM_ET,
    V611_REGIME_B_SHORT_DISARM_HHMM_ET    broker/orders.py:1371
  - STOP_PCT_LONG, STOP_PCT_SHORT broker/orders.py:1257
  - evaluate_strike_sizing       broker/orders.py:1316
  - SIDE_LONG, SIDE_SHORT        trade_genius eot proxy callers
"""
from __future__ import annotations

# Source of truth (for now) -- attribute access proxies through to
# eye_of_tiger at call time. Why not `from eye_of_tiger import NAME`?
# That creates a NEW binding in this module's namespace frozen at import,
# so monkey-patches against `eye_of_tiger.NAME` (used widely in tests
# e.g. `mock.patch("eye_of_tiger.V611_REGIME_B_ENABLED", False)`) do
# NOT propagate to importers of legacy_constants. The PEP 562
# __getattr__ pattern below makes every legacy_constants.NAME lookup
# resolve via eye_of_tiger's namespace at call time -- so patches
# work transparently, and importers can still write `from
# engine.legacy_constants import NAME` (which captures the value once,
# matching the pre-shim semantics for non-patched call sites).
import eye_of_tiger as _eot

_PROXIED = frozenset({
    "CANCEL_ACK_TIMEOUT_MS",
    "ENTRY_1_SIZE_PCT",
    "ENTRY_2_SIZE_PCT",
    "V611_REGIME_B_ENABLED",
    "V611_REGIME_B_SHORT_SCALE_MULT",
    "V611_REGIME_B_SHORT_ARM_HHMM_ET",
    "V611_REGIME_B_SHORT_DISARM_HHMM_ET",
    "STOP_PCT_LONG",
    "STOP_PCT_SHORT",
    "SIDE_LONG",
    "SIDE_SHORT",
    "POST_EXIT_SAME_TICKER_COOLDOWN_SEC",
    "evaluate_strike_sizing",
    "scaled_sovereign_brake_dollars",
})


def __getattr__(name):
    if name in _PROXIED:
        return getattr(_eot, name)
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )


def __dir__():
    return sorted(_PROXIED | set(globals().keys()))
