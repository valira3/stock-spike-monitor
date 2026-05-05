"""v6.14.10 tests: live volume-bucket evaluator wired into entry path.

Covers ``v5_10_1_integration.evaluate_volume_bucket_live`` paths:

  1. VOLUME_GATE_ENABLED=False -> ok=True, reason='disabled'.
  2. VOLUME_GATE_LIVE_ENFORCE=false -> ok=True, reason='live_enforce_off'
     (even when VOLUME_GATE_ENABLED is True).
  3. now_et < 10:00 ET -> ok=True, reason='pre_10am_passthrough'.
  4. bucket lookup returns None -> ok=True, reason='no_bucket'.
  5. baseline COLDSTART -> ok=True, reason='coldstart'.
  6. baseline PASS -> ok=True, reason='pass'.
  7. baseline FAIL -> ok=False, reason='fail' (the only rejection path).

The helper signature is:
    evaluate_volume_bucket_live(ticker, now_et, bars) -> dict

The dict always carries keys: ok, reason, gate, ratio, bucket.
"""
from __future__ import annotations

from datetime import datetime, time as dtime
import os
from unittest.mock import patch

import pytest


def _et(h: int, m: int = 0) -> datetime:
    return datetime(2026, 5, 5, h, m, 0)


def _bars(vol_last_closed: int) -> dict:
    """Return a bars dict whose volumes[-2] is the just-closed minute."""
    return {
        "volumes": [100_000, vol_last_closed, 50_000],
        "closes": [10.0, 10.5, 10.6],
    }


@pytest.fixture(autouse=True)
def _reset_env_and_modules():
    """Clean env between tests so layered flags do not leak."""
    saved = {
        k: os.environ.get(k)
        for k in ("VOLUME_GATE_ENABLED", "VOLUME_GATE_LIVE_ENFORCE",
                  "VOLUME_BUCKET_THRESHOLD_RATIO")
    }
    for k in saved:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _import_helper(volume_gate_enabled: bool):
    """Reload feature_flags with the requested env, return helper."""
    if volume_gate_enabled:
        os.environ["VOLUME_GATE_ENABLED"] = "true"
    else:
        os.environ.pop("VOLUME_GATE_ENABLED", None)
    from importlib import reload
    from engine import feature_flags as ff
    reload(ff)
    import v5_10_1_integration as eot_glue
    reload(eot_glue)
    return eot_glue


def test_disabled_flag_passthrough():
    eot_glue = _import_helper(volume_gate_enabled=False)
    res = eot_glue.evaluate_volume_bucket_live("AAPL", _et(11, 0), _bars(1000))
    assert res["ok"] is True
    assert res["reason"] == "disabled"
    assert res["gate"] is None


def test_live_enforce_off_passthrough():
    eot_glue = _import_helper(volume_gate_enabled=True)
    os.environ["VOLUME_GATE_LIVE_ENFORCE"] = "false"
    res = eot_glue.evaluate_volume_bucket_live("AAPL", _et(11, 0), _bars(1000))
    assert res["ok"] is True
    assert res["reason"] == "live_enforce_off"


def test_pre_10am_passthrough():
    eot_glue = _import_helper(volume_gate_enabled=True)
    os.environ["VOLUME_GATE_LIVE_ENFORCE"] = "true"
    res = eot_glue.evaluate_volume_bucket_live("AAPL", _et(9, 45), _bars(1000))
    assert res["ok"] is True
    assert res["reason"] == "pre_10am_passthrough"


def test_no_bucket_passthrough():
    eot_glue = _import_helper(volume_gate_enabled=True)
    os.environ["VOLUME_GATE_LIVE_ENFORCE"] = "true"

    with patch("volume_profile.previous_session_bucket", return_value=None):
        res = eot_glue.evaluate_volume_bucket_live(
            "AAPL", _et(11, 0), _bars(1000)
        )
    assert res["ok"] is True
    assert res["reason"] == "no_bucket"


def test_coldstart_passthrough():
    eot_glue = _import_helper(volume_gate_enabled=True)
    os.environ["VOLUME_GATE_LIVE_ENFORCE"] = "true"

    class _Stub:
        def check(self, ticker, bucket, cv):
            return {"gate": "COLDSTART", "ratio": None}

    with patch("volume_profile.previous_session_bucket",
               return_value="10:00"), \
         patch.object(eot_glue, "get_volume_baseline",
                      return_value=_Stub()):
        res = eot_glue.evaluate_volume_bucket_live(
            "AAPL", _et(11, 0), _bars(1000)
        )
    assert res["ok"] is True
    assert res["reason"] == "coldstart"
    assert res["gate"] == "COLDSTART"


def test_pass_passthrough():
    eot_glue = _import_helper(volume_gate_enabled=True)
    os.environ["VOLUME_GATE_LIVE_ENFORCE"] = "true"

    class _Stub:
        def check(self, ticker, bucket, cv):
            return {"gate": "PASS", "ratio": 1.20}

    with patch("volume_profile.previous_session_bucket",
               return_value="10:00"), \
         patch.object(eot_glue, "get_volume_baseline",
                      return_value=_Stub()):
        res = eot_glue.evaluate_volume_bucket_live(
            "AAPL", _et(11, 0), _bars(1500)
        )
    assert res["ok"] is True
    assert res["reason"] == "pass"
    assert res["gate"] == "PASS"
    assert res["ratio"] == 1.20


def test_fail_rejects():
    eot_glue = _import_helper(volume_gate_enabled=True)
    os.environ["VOLUME_GATE_LIVE_ENFORCE"] = "true"

    class _Stub:
        def check(self, ticker, bucket, cv):
            return {"gate": "FAIL", "ratio": 0.55}

    with patch("volume_profile.previous_session_bucket",
               return_value="10:00"), \
         patch.object(eot_glue, "get_volume_baseline",
                      return_value=_Stub()):
        res = eot_glue.evaluate_volume_bucket_live(
            "AAPL", _et(11, 0), _bars(500)
        )
    assert res["ok"] is False
    assert res["reason"] == "fail"
    assert res["gate"] == "FAIL"
    assert res["ratio"] == 0.55


def test_bars_none_still_evaluates():
    """Even with bars=None the helper resolves to no_bucket or evaluator
    cleanly; it must not raise."""
    eot_glue = _import_helper(volume_gate_enabled=True)
    os.environ["VOLUME_GATE_LIVE_ENFORCE"] = "true"

    class _Stub:
        def check(self, ticker, bucket, cv):
            # cv should be 0 when bars is None.
            assert cv == 0
            return {"gate": "PASS", "ratio": 0.0}

    with patch("volume_profile.previous_session_bucket",
               return_value="10:00"), \
         patch.object(eot_glue, "get_volume_baseline",
                      return_value=_Stub()):
        res = eot_glue.evaluate_volume_bucket_live(
            "AAPL", _et(11, 0), None
        )
    assert res["ok"] is True
    assert res["reason"] == "pass"
