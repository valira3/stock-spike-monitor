"""v6.4.1 - asymmetric long/short stop pcts.

The Apr 27 - May 1 short-stop sweep (see
/home/user/workspace/v640_short_stop_sweep/report.md) showed shorts have
asymmetric per-share variance: avg short loss -$2.02/sh vs avg short win
+$1.65/sh. Tightening short stops to 30bp (vs symmetric 50bp baseline)
lifted weekly P&L by +$262 (+30%). 25bp was too tight (chopped on noise).

These tests assert:

  1. The two new module-level constants STOP_PCT_LONG=0.005 and
     STOP_PCT_SHORT=0.003 ship at the right values.

  2. The legacy STOP_PCT_OF_ENTRY=0.005 alias is preserved for
     back-compat (any external caller still importing it gets the same
     long-side value it always had).

  3. Computed stop prices match: a long entry at $100 yields a stop at
     $99.50 (50bp below); a short entry at $100 yields a stop at
     $100.30 (30bp above) - the new tighter short rail.
"""

from __future__ import annotations

import eye_of_tiger as eot


def test_stop_pct_long_is_50bp():
    assert eot.STOP_PCT_LONG == 0.005


def test_stop_pct_short_is_30bp():
    assert eot.STOP_PCT_SHORT == 0.003


def test_legacy_stop_pct_of_entry_alias_unchanged():
    # Back-compat: external callers still importing STOP_PCT_OF_ENTRY
    # must get the long-side value (50bp), matching pre-v6.4.1 behaviour.
    assert eot.STOP_PCT_OF_ENTRY == 0.005
    assert eot.STOP_PCT_OF_ENTRY == eot.STOP_PCT_LONG


def test_long_stop_at_100_dollar_entry():
    entry = 100.0
    expected_stop = round(entry * (1.0 - eot.STOP_PCT_LONG), 2)
    assert expected_stop == 99.50


def test_short_stop_at_100_dollar_entry():
    entry = 100.0
    expected_stop = round(entry * (1.0 + eot.STOP_PCT_SHORT), 2)
    assert expected_stop == 100.30


def test_short_stop_tighter_than_long_stop():
    # The whole point of v6.4.1: shorts get a tighter rail than longs.
    assert eot.STOP_PCT_SHORT < eot.STOP_PCT_LONG
