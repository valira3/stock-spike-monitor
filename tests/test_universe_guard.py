"""v5.8.0 \u2014 unit tests for the [UNIVERSE_GUARD] startup helper.

Covers four cases:
  1. missing file        \u2014 guard creates it
  2. corrupt JSON        \u2014 guard rewrites it
  3. drift detected      \u2014 guard rewrites to match TICKERS_DEFAULT
  4. consistent on disk  \u2014 guard does NOT rewrite (mtime preserved)

The guard reads its target path from the ``UNIVERSE_GUARD_PATH`` env
var (added in v5.8.0 specifically so the test can redirect away from
the production ``/data/tickers.json`` location).
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Import trade_genius under SSM_SMOKE_TEST=1 so the Telegram client,
# scheduler, OR-collector, and dashboard never boot during tests.
os.environ.setdefault("SSM_SMOKE_TEST", "1")

# Make repo root importable when pytest is launched from tests/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import trade_genius  # noqa: E402


def _redirect(monkeypatch, tmp_path):
    """Point the guard at a tmp file and return its Path."""
    target = tmp_path / "tickers.json"
    monkeypatch.setenv("UNIVERSE_GUARD_PATH", str(target))
    return target


def _expected():
    return sorted(set(trade_genius.TICKERS_DEFAULT))


def _read_tickers(path: Path):
    """Parse the on-disk file (envelope or flat list) into a sorted
    list of upper-case tickers \u2014 matches the guard's own tolerance.
    """
    raw = json.loads(path.read_text())
    items = raw.get("tickers") if isinstance(raw, dict) else raw
    return sorted({str(s).upper() for s in items if str(s).strip()})


# --------------------------------------------------------------------
# Case 1: missing file \u2014 guard creates it.
# --------------------------------------------------------------------
def test_missing_file_creates_with_canonical_universe(tmp_path, monkeypatch):
    target = _redirect(monkeypatch, tmp_path)
    assert not target.exists()

    trade_genius._ensure_universe_consistency()

    assert target.exists(), "guard must create the file when it is missing"
    assert _read_tickers(target) == _expected()


# --------------------------------------------------------------------
# Case 2: corrupt JSON \u2014 guard rewrites it.
# --------------------------------------------------------------------
def test_corrupt_json_is_rewritten(tmp_path, monkeypatch):
    target = _redirect(monkeypatch, tmp_path)
    target.write_text("{this is not valid json")

    trade_genius._ensure_universe_consistency()

    assert _read_tickers(target) == _expected()


# --------------------------------------------------------------------
# Case 3: missing defaults \u2014 guard tops up but preserves extras.
# v7.2.5: superset semantics. The guard no longer deletes manually-
# added tickers; it only ensures all code-side defaults are present.
# --------------------------------------------------------------------
def test_missing_defaults_are_topped_up_and_extras_preserved(tmp_path, monkeypatch):
    target = _redirect(monkeypatch, tmp_path)
    # Disk: missing several defaults, plus one manually-added ticker.
    on_disk = ["AAPL", "MSFT", "DDOG"]
    target.write_text(json.dumps({"tickers": on_disk}))

    trade_genius._ensure_universe_consistency()

    final = _read_tickers(target)
    # All code defaults must now be present.
    for sym in _expected():
        assert sym in final, f"{sym} missing from union"
    # The manual addition must survive.
    assert "DDOG" in final, "manual extra was dropped (regression)"


# --------------------------------------------------------------------
# Case 3b: disk is a strict superset of defaults \u2014 guard does NOT
# rewrite (mtime preserved). Pure-extras case.
# --------------------------------------------------------------------
def test_extras_only_disk_is_not_rewritten(tmp_path, monkeypatch):
    target = _redirect(monkeypatch, tmp_path)
    payload = {
        "tickers": _expected() + ["DDOG", "CRWD"],
        "updated_utc": "test",
        "bot_version": "test",
    }
    target.write_text(json.dumps(payload))
    original_mtime = target.stat().st_mtime
    time.sleep(0.05)

    trade_genius._ensure_universe_consistency()

    final = _read_tickers(target)
    assert "DDOG" in final and "CRWD" in final
    for sym in _expected():
        assert sym in final
    assert target.stat().st_mtime == original_mtime, (
        "guard rewrote a file whose disk was already a superset of defaults"
    )


# --------------------------------------------------------------------
# Case 4: consistent file \u2014 guard does NOT rewrite.
# --------------------------------------------------------------------
def test_consistent_file_not_rewritten(tmp_path, monkeypatch):
    target = _redirect(monkeypatch, tmp_path)
    # Write a file that already matches code (envelope format, sorted).
    payload = {"tickers": _expected(), "updated_utc": "test", "bot_version": "test"}
    target.write_text(json.dumps(payload))
    original_mtime = target.stat().st_mtime

    # Sleep a hair so any rewrite would bump mtime detectably.
    time.sleep(0.05)

    trade_genius._ensure_universe_consistency()

    # Content still matches.
    assert _read_tickers(target) == _expected()
    # mtime unchanged \u2014 proves the guard did NOT rewrite.
    assert target.stat().st_mtime == original_mtime, (
        "guard rewrote a file that was already consistent"
    )
