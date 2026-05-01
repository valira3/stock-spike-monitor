# tests/test_v6_0_1_titan_universe.py
# v6.0.1 -- QBTS removed from the titan universe per user request
# ("we are not trading QBTS"). Locks down both the code-side default
# and the on-disk seed so a future bump cannot quietly add it back.
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations

import json
import os


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_qbts_not_in_tickers_default_source():
    """Read the source file directly so we do not pull the entire
    trade_genius runtime into the test (matches the pattern used by
    test_v5_27_0.test_tickers_default_source_includes_nflx_and_orcl).
    """
    src_path = os.path.join(REPO_ROOT, "trade_genius.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    start = src.index("TICKERS_DEFAULT = [")
    end = src.index("]", start)
    block = src[start:end]
    assert '"QBTS"' not in block, "QBTS must not be in TICKERS_DEFAULT"


def test_qbts_not_in_persisted_tickers_seed():
    path = os.path.join(REPO_ROOT, "tickers.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get("tickers") if isinstance(raw, dict) else raw
    assert isinstance(items, list)
    syms = {str(s).upper() for s in items}
    assert "QBTS" not in syms, "QBTS must not be in tickers.json seed"


def test_titan_set_still_present():
    """Sanity: the 10 actual titans plus the two pinned reference
    symbols (SPY, QQQ) must all still be present.
    """
    path = os.path.join(REPO_ROOT, "tickers.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    syms = {str(s).upper() for s in raw["tickers"]}
    expected = {
        "AAPL", "MSFT", "NVDA", "TSLA", "META",
        "GOOG", "AMZN", "AVGO", "NFLX", "ORCL",
        "SPY", "QQQ",
    }
    assert expected <= syms, "missing required tickers: " + repr(expected - syms)
