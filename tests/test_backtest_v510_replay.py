"""v5.10.4 \u2014 smoke test for backtest_v510 full replay.

Ensures the replay module imports cleanly and that calling
`replay()` against an empty bars directory returns a well-formed
summary. The full algorithm correctness is exercised by the
eye_of_tiger / volume_bucket / forensic_stop suites.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backtest_v510 import replay_v510_full  # noqa: E402


def test_replay_empty_bars_returns_zero_trades(tmp_path):
    out = replay_v510_full.replay(str(tmp_path), "2026-04-21", "2026-04-22")
    assert out["trades"] == 0
    assert out["wins"] == 0
    assert out["losses"] == 0
    assert out["pnl_total"] == 0.0
    assert out["closed"] == []


def test_replay_main_no_args_exits_nonzero(capsys):
    import pytest
    with pytest.raises(SystemExit):
        replay_v510_full.main([])
