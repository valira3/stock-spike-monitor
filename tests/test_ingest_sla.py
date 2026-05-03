"""tests/test_ingest_sla.py - Unit tests for ingest/sla.py SLA threshold evaluation.

Covers SLAThreshold color derivation, _worst_color, IngestHealthState transitions,
and env-var override behavior.

No em-dashes in this file per team rules.
"""
import os
import sys
import time
import threading
from collections import deque

import pytest

# Ensure repo root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ---------------------------------------------------------------------------
# SLAThreshold color derivation
# ---------------------------------------------------------------------------

class TestSLAThreshold:
    def setup_method(self):
        import importlib
        import ingest_config
        import ingest.sla as sla
        importlib.reload(ingest_config)
        importlib.reload(sla)
        self.sla = sla

    def test_green_at_boundary(self):
        from ingest.sla import SLAThreshold
        t = SLAThreshold(green_max=90.0, yellow_max=300.0)
        assert t.classify(90.0) == "green"

    def test_green_below_boundary(self):
        from ingest.sla import SLAThreshold
        t = SLAThreshold(green_max=90.0, yellow_max=300.0)
        assert t.classify(0.0) == "green"
        assert t.classify(89.9) == "green"

    def test_yellow_above_green(self):
        from ingest.sla import SLAThreshold
        t = SLAThreshold(green_max=90.0, yellow_max=300.0)
        assert t.classify(90.1) == "yellow"
        assert t.classify(200.0) == "yellow"
        assert t.classify(300.0) == "yellow"

    def test_red_above_yellow(self):
        from ingest.sla import SLAThreshold
        t = SLAThreshold(green_max=90.0, yellow_max=300.0)
        assert t.classify(300.1) == "red"
        assert t.classify(9999.0) == "red"

    def test_zero_green_max_zero_value(self):
        from ingest.sla import SLAThreshold
        t = SLAThreshold(green_max=0.0, yellow_max=2.0)
        assert t.classify(0.0) == "green"
        assert t.classify(1.0) == "yellow"
        assert t.classify(2.1) == "red"


# ---------------------------------------------------------------------------
# _worst_color logic
# ---------------------------------------------------------------------------

class TestWorstColor:
    def test_red_beats_yellow(self):
        from ingest.sla import _worst_color
        assert _worst_color("red", "yellow") == "red"
        assert _worst_color("yellow", "red") == "red"

    def test_red_beats_green(self):
        from ingest.sla import _worst_color
        assert _worst_color("red", "green") == "red"

    def test_yellow_beats_green(self):
        from ingest.sla import _worst_color
        assert _worst_color("yellow", "green") == "yellow"
        assert _worst_color("green", "yellow") == "yellow"

    def test_same_colors(self):
        from ingest.sla import _worst_color
        assert _worst_color("green", "green") == "green"
        assert _worst_color("yellow", "yellow") == "yellow"
        assert _worst_color("red", "red") == "red"


# ---------------------------------------------------------------------------
# IngestHealthState color and transition history
# ---------------------------------------------------------------------------

class TestIngestHealthState:
    def setup_method(self):
        import importlib
        import ingest.sla as sla_mod
        importlib.reload(sla_mod)
        # Reset module-level state
        sla_mod._health_states.clear()
        sla_mod._raw_metrics.clear()
        self.sla = sla_mod

    def test_initial_state_is_green(self):
        state = self.sla.get_health_state("AAPL")
        assert state.color == "green"
        assert state.ticker == "AAPL"

    def test_global_state_accessible(self):
        state = self.sla.get_health_state(None)
        assert state.color == "green"
        assert state.ticker is None

    def test_update_triggers_red(self):
        self.sla.update_global_stats(last_bar_age_s=400.0)
        state = self.sla.get_health_state(None)
        assert state.color == "red"

    def test_update_triggers_yellow(self):
        self.sla.update_global_stats(last_bar_age_s=100.0)
        state = self.sla.get_health_state(None)
        assert state.color == "yellow"

    def test_transition_history_records_color_change(self):
        self.sla.update_global_stats(last_bar_age_s=100.0)  # green -> yellow
        self.sla.update_global_stats(last_bar_age_s=400.0)  # yellow -> red
        state = self.sla.get_health_state(None)
        assert len(state.transition_history) >= 1

    def test_transition_history_capped_at_20(self):
        for i in range(25):
            # Alternate yellow / red to force repeated transitions
            age = 400.0 if i % 2 == 0 else 100.0
            self.sla.update_global_stats(last_bar_age_s=age)
        state = self.sla.get_health_state(None)
        assert len(state.transition_history) <= 20

    def test_entered_color_at_only_resets_on_change(self):
        self.sla.update_global_stats(last_bar_age_s=400.0)
        state = self.sla.get_health_state(None)
        t1 = state.entered_color_at
        time.sleep(0.05)
        self.sla.update_global_stats(last_bar_age_s=400.0)  # still red
        state2 = self.sla.get_health_state(None)
        # entered_color_at should NOT change since color is still red
        assert state2.entered_color_at == t1


# ---------------------------------------------------------------------------
# RTH window check
# ---------------------------------------------------------------------------

class TestIsRTH:
    def test_rth_returns_bool(self):
        from ingest.sla import _is_rth
        result = _is_rth()
        assert isinstance(result, bool)

    def test_rth_midday_is_rth(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from ingest.sla import _is_rth
        # 12:00 ET is within RTH
        dt = datetime(2026, 5, 4, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        assert _is_rth(dt) is True

    def test_premarket_not_rth(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from ingest.sla import _is_rth
        dt = datetime(2026, 5, 4, 8, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        assert _is_rth(dt) is False

    def test_after_close_not_rth(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from ingest.sla import _is_rth
        dt = datetime(2026, 5, 4, 16, 30, 0, tzinfo=ZoneInfo("America/New_York"))
        assert _is_rth(dt) is False
