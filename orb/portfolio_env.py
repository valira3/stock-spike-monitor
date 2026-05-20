"""v10.0.1 -- per-portfolio env-var resolution.

The operator wants to set tighter risk caps on Val than on Main without
forking the global config. This helper looks up `<PID_UPPER>_<BASE>`
first (e.g. `VAL_ORB_MAX_CONCURRENT_RISK_DOLLARS=1500`) and falls back
to the global `<BASE>` (e.g. `ORB_MAX_CONCURRENT_RISK_DOLLARS=2000`),
which itself falls back to the supplied default.

Scope (v10.0.1): wired into `OrbEngine.__init__`'s RiskBook registration
loop for the three admission caps:
  - max_concurrent_risk_dollars
  - max_concurrent_notional_mult
  - daily_loss_kill_pct

Other strategy levers (RR, range, ATR, VWAP fence) stay global because
they are strategy decisions, not portfolio policy. A future PR can
extend per-portfolio coverage by calling `resolve_*` on additional
fields; the env convention is the same.
"""
from __future__ import annotations

import os
from typing import Optional


def resolve_str(portfolio_id: Optional[str], base_name: str, default: str) -> str:
    """Read `<PID_UPPER>_<BASE>` env, fallback to `<BASE>` env, fallback
    to `default`. portfolio_id=None or "" skips the per-portfolio lookup."""
    if portfolio_id:
        per = f"{portfolio_id.upper()}_{base_name}"
        v = os.environ.get(per)
        if v is not None and v.strip():
            return v
    v = os.environ.get(base_name)
    if v is not None:
        return v
    return default


def resolve_float(portfolio_id: Optional[str], base_name: str, default: float) -> float:
    raw = resolve_str(portfolio_id, base_name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def resolve_int(portfolio_id: Optional[str], base_name: str, default: int) -> int:
    raw = resolve_str(portfolio_id, base_name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def resolve_bool(portfolio_id: Optional[str], base_name: str, default: bool) -> bool:
    raw = resolve_str(portfolio_id, base_name, "")
    if not raw:
        return default
    return raw.strip() in ("1", "true", "True", "yes", "YES")
