"""v6.14.9 tests: VOLUME_BUCKET_THRESHOLD_RATIO is env-tunable.

Covers:
  1. Default: when env var is unset, the constant is 1.00.
  2. Env override: setting the env var (via reload) changes the
     module attribute to the parsed float.
  3. Evaluator at default reads the runtime module attribute and
     enforces ratio >= 1.0 (matches prior literal behavior).
  4. Evaluator at relaxed threshold (0.85) admits a ratio that
     would have been rejected at the strict default (e.g. 0.92).
  5. Evaluator at strict threshold (1.05) rejects a ratio (1.00)
     that the default would have admitted.
"""
from datetime import datetime, time as dtime
from importlib import reload
import os
from unittest.mock import patch

import pytest


def _et_after_10am():
    """Return a datetime with .time() >= 10:00 ET so vAA-1 path engages."""
    return datetime(2026, 5, 5, 10, 30, 0)


def test_default_threshold_is_one():
    """With no env var set, VOLUME_BUCKET_THRESHOLD_RATIO is 1.00."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VOLUME_BUCKET_THRESHOLD_RATIO", None)
        import volume_bucket
        reload(volume_bucket)
        assert volume_bucket.VOLUME_BUCKET_THRESHOLD_RATIO == 1.00


def test_env_override_changes_constant():
    """Setting env var changes the parsed float on reload."""
    with patch.dict(os.environ, {"VOLUME_BUCKET_THRESHOLD_RATIO": "0.85"}):
        import volume_bucket
        reload(volume_bucket)
        assert volume_bucket.VOLUME_BUCKET_THRESHOLD_RATIO == 0.85
    # Restore default for downstream tests.
    os.environ.pop("VOLUME_BUCKET_THRESHOLD_RATIO", None)
    import volume_bucket
    reload(volume_bucket)


def test_evaluator_at_default_threshold():
    """At default 1.00, ratio 1.00 passes, ratio 0.92 fails."""
    os.environ.pop("VOLUME_BUCKET_THRESHOLD_RATIO", None)
    import volume_bucket
    reload(volume_bucket)
    import eye_of_tiger
    reload(eye_of_tiger)

    # ratio == threshold passes.
    cr_pass = {"ratio_to_55bar_avg": 1.00}
    assert eye_of_tiger.evaluate_volume_bucket(
        cr_pass, now_et=_et_after_10am()
    ) is True

    # ratio below threshold fails.
    cr_fail = {"ratio_to_55bar_avg": 0.92}
    assert eye_of_tiger.evaluate_volume_bucket(
        cr_fail, now_et=_et_after_10am()
    ) is False


def test_evaluator_at_relaxed_threshold():
    """At 0.85, ratio 0.92 passes (would have failed at default 1.00)."""
    import volume_bucket
    import eye_of_tiger

    # Patch the module attribute directly (matches how evaluator reads it).
    with patch.object(volume_bucket, "VOLUME_BUCKET_THRESHOLD_RATIO", 0.85):
        cr = {"ratio_to_55bar_avg": 0.92}
        assert eye_of_tiger.evaluate_volume_bucket(
            cr, now_et=_et_after_10am()
        ) is True

        # Below the relaxed threshold still fails.
        cr_low = {"ratio_to_55bar_avg": 0.80}
        assert eye_of_tiger.evaluate_volume_bucket(
            cr_low, now_et=_et_after_10am()
        ) is False


def test_evaluator_at_strict_threshold():
    """At 1.05, ratio 1.00 fails (would have passed at default 1.00)."""
    import volume_bucket
    import eye_of_tiger

    with patch.object(volume_bucket, "VOLUME_BUCKET_THRESHOLD_RATIO", 1.05):
        cr = {"ratio_to_55bar_avg": 1.00}
        assert eye_of_tiger.evaluate_volume_bucket(
            cr, now_et=_et_after_10am()
        ) is False

        # Above the strict threshold passes.
        cr_high = {"ratio_to_55bar_avg": 1.10}
        assert eye_of_tiger.evaluate_volume_bucket(
            cr_high, now_et=_et_after_10am()
        ) is True
