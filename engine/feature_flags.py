"""engine.feature_flags \u2014 runtime feature flag shim (v6.14.1).

Restored in v6.14.1 after the legacy module was removed in v5.26.0.
Several callers still expect ``from engine import feature_flags`` to
resolve and read ``VOLUME_GATE_ENABLED`` off it:

  * ``trade_genius.py`` startup banner (logs gate state on boot).
  * ``eye_of_tiger.evaluate_volume_gate`` legacy v5.10.x path (the new
    vAA-1 time-conditional path is unaffected because it does not
    consult this flag).
  * ``dashboard_server`` ``feature_flags`` block (drives the Permit
    Matrix volume column / card visibility on the dashboard).
  * ``v5_13_2_snapshot`` per-ticker Phase 2 row builder (vol_gate_status
    "OFF" override).

The shim reads ``VOLUME_GATE_ENABLED`` from the process environment at
import time so the dashboard, the bot, and the snapshot builder all see
the same value as the gate evaluator. Truthiness rules match
``eye_of_tiger._read_bool``:

  Truthy: "1", "true", "yes", "on" (case-insensitive, surrounding
          whitespace stripped).
  Falsy:  anything else, including unset.

This module is intentionally trivial. If a future flag needs hot-reload
semantics, switch the attribute to a property on a singleton object.
"""

from __future__ import annotations

import os


def _read_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Live runtime flag. Default False to match production behaviour from
# v5.13.1 through v6.13.x. v6.14.0 re-enabled the flag end-to-end; the
# Railway environment variable VOLUME_GATE_ENABLED=true flips it on.
VOLUME_GATE_ENABLED: bool = _read_bool("VOLUME_GATE_ENABLED", False)


__all__ = ["VOLUME_GATE_ENABLED"]
