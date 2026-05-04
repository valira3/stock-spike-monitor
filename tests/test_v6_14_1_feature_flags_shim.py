"""Tests for the v6.14.1 engine.feature_flags shim.

The shim was restored in v6.14.1 after being removed in v5.26.0.
Several callers (dashboard_server, trade_genius startup, v5_13_2_snapshot,
eye_of_tiger legacy path) read VOLUME_GATE_ENABLED off this module.

Each test runs the shim in a subprocess so the env var is captured at
module import time and the parent test process state stays clean.
"""

from __future__ import annotations

import subprocess
import sys


def _read_flag_in_subprocess(env_value: str | None) -> str:
    cmd = [sys.executable, "-c", "from engine.feature_flags import VOLUME_GATE_ENABLED as v; print(v)"]
    env_extra: dict[str, str] = {}
    if env_value is not None:
        env_extra["VOLUME_GATE_ENABLED"] = env_value
    import os
    full_env = {**os.environ, **env_extra}
    if env_value is None:
        full_env.pop("VOLUME_GATE_ENABLED", None)
    out = subprocess.check_output(cmd, env=full_env, text=True, timeout=15).strip()
    return out


def test_unset_defaults_to_false():
    assert _read_flag_in_subprocess(None) == "False"


def test_true_truthy():
    assert _read_flag_in_subprocess("true") == "True"


def test_one_truthy():
    assert _read_flag_in_subprocess("1") == "True"


def test_yes_truthy():
    assert _read_flag_in_subprocess("yes") == "True"


def test_on_truthy():
    assert _read_flag_in_subprocess("on") == "True"


def test_false_falsy():
    assert _read_flag_in_subprocess("False") == "False"


def test_zero_falsy():
    assert _read_flag_in_subprocess("0") == "False"


def test_empty_string_falsy():
    assert _read_flag_in_subprocess("") == "False"


def test_module_exports_only_volume_gate_enabled():
    cmd = [sys.executable, "-c", "import engine.feature_flags as ff; print(sorted(ff.__all__))"]
    out = subprocess.check_output(cmd, text=True, timeout=15).strip()
    assert out == "['VOLUME_GATE_ENABLED']"
