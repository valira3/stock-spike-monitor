"""v6.10.0 -- default flip regression tests (C5 + C10).

Wave 2 entry-ROI sweep shipped two new defaults:
- C5: V620_FAST_BOUNDARY_CUTOFF_HHMM_ET 10:30 -> 12:00
- C10: POST_LOSS_COOLDOWN_MIN_SHORT 30 -> 60

These tests pin both defaults and verify the C10 env override path
still works after the reload, so a production operator can revert
without a code change.

No em-dashes in this file.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
os.environ.setdefault("FMP_API_KEY", "sweep_dummy_key")


def test_c5_default_cutoff_is_12_00() -> None:
    """C5: fast-boundary cutoff default must be 12:00 after the v6.10.0 flip."""
    import v5_10_1_integration as v

    assert v.V620_FAST_BOUNDARY_CUTOFF_HHMM_ET == "12:00", (
        f"Expected '12:00' but got '{v.V620_FAST_BOUNDARY_CUTOFF_HHMM_ET}'. "
        "Did the v6.10.0 C5 default flip land in v5_10_1_integration.py?"
    )


def test_c10_default_cooldown_is_60(monkeypatch: pytest.MonkeyPatch) -> None:
    """C10: POST_LOSS_COOLDOWN_MIN_SHORT default must be 60 when env is unset."""
    monkeypatch.delenv("POST_LOSS_COOLDOWN_MIN_SHORT", raising=False)
    monkeypatch.delenv("POST_LOSS_COOLDOWN_MIN", raising=False)

    import eye_of_tiger as eot

    eot_mod = importlib.reload(eot)
    assert eot_mod.POST_LOSS_COOLDOWN_MIN_SHORT == 60, (
        f"Expected 60 but got {eot_mod.POST_LOSS_COOLDOWN_MIN_SHORT}. "
        "Did the v6.10.0 C10 default flip land in eye_of_tiger.py?"
    )


def test_c10_env_override_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """C10: operator env override must still take precedence over the new default."""
    monkeypatch.setenv("POST_LOSS_COOLDOWN_MIN_SHORT", "45")
    monkeypatch.delenv("POST_LOSS_COOLDOWN_MIN", raising=False)

    import eye_of_tiger as eot

    eot_mod = importlib.reload(eot)
    assert eot_mod.POST_LOSS_COOLDOWN_MIN_SHORT == 45, (
        f"Expected 45 (env override) but got {eot_mod.POST_LOSS_COOLDOWN_MIN_SHORT}. "
        "POST_LOSS_COOLDOWN_MIN_SHORT env override is broken."
    )
