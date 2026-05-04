"""v6.14.6 -- regression tests for extended-hours bucket support.

Both `volume_bucket._bucket_key` and `ingest.algo_plus._compute_et_bucket`
now accept the full Alpaca extended session (04:00-19:59 ET) instead of
RTH only. This lets the volume baseline index pre-market and post-market
minutes alongside RTH.
"""
from __future__ import annotations

from datetime import datetime, timezone

import bot_version
from ingest import algo_plus
from volume_bucket import _bucket_key


def test_bot_version_is_6_14_6_or_newer():
    parts = [int(p) for p in bot_version.BOT_VERSION.split(".")]
    assert parts >= [6, 14, 6]


# -------------------- _bucket_key -------------------------------------

def test_bucket_key_accepts_pre_market_open():
    assert _bucket_key("0400") == "04:00"


def test_bucket_key_accepts_pre_market_minute():
    assert _bucket_key("0530") == "05:30"


def test_bucket_key_accepts_rth_open():
    assert _bucket_key("0930") == "09:30"


def test_bucket_key_accepts_rth_close():
    assert _bucket_key("1559") == "15:59"


def test_bucket_key_accepts_post_market_open():
    assert _bucket_key("1601") == "16:01"


def test_bucket_key_accepts_post_market_close():
    assert _bucket_key("1959") == "19:59"


def test_bucket_key_rejects_before_extended_open():
    assert _bucket_key("0359") is None


def test_bucket_key_rejects_after_extended_close():
    assert _bucket_key("2000") is None
    assert _bucket_key("2300") is None


def test_bucket_key_rejects_invalid_format():
    assert _bucket_key("garbage") is None
    assert _bucket_key(None) is None


def test_bucket_key_idempotent_with_colon_form():
    assert _bucket_key("07:15") == "07:15"
    assert _bucket_key("18:30") == "18:30"


# -------------------- _compute_et_bucket ------------------------------

def _utc(year, month, day, hh, mm):
    """ET hour/minute -> UTC datetime. 2026-05-04 is EDT (UTC-4)."""
    # EDT: ET = UTC-4. So UTC = ET + 4h.
    return datetime(year, month, day, hh + 4, mm, tzinfo=timezone.utc)


def test_compute_et_bucket_pre_market_open():
    # 04:00 ET on 2026-05-04 (EDT) = 08:00 UTC
    ts = _utc(2026, 5, 4, 4, 0)
    assert algo_plus._compute_et_bucket(ts) == "0400"


def test_compute_et_bucket_pre_market_mid():
    ts = _utc(2026, 5, 4, 7, 15)
    assert algo_plus._compute_et_bucket(ts) == "0715"


def test_compute_et_bucket_rth_open_still_works():
    ts = _utc(2026, 5, 4, 9, 30)
    assert algo_plus._compute_et_bucket(ts) == "0930"


def test_compute_et_bucket_rth_close_print():
    ts = _utc(2026, 5, 4, 16, 0)
    assert algo_plus._compute_et_bucket(ts) == "1600"


def test_compute_et_bucket_post_market_mid():
    ts = _utc(2026, 5, 4, 17, 30)
    assert algo_plus._compute_et_bucket(ts) == "1730"


def test_compute_et_bucket_post_market_close():
    ts = _utc(2026, 5, 4, 19, 59)
    assert algo_plus._compute_et_bucket(ts) == "1959"


def test_compute_et_bucket_rejects_before_04_et():
    ts = _utc(2026, 5, 4, 3, 59)
    assert algo_plus._compute_et_bucket(ts) is None


def test_compute_et_bucket_rejects_after_19_59_et():
    # 20:00 ET = 00:00 UTC the NEXT day during EDT.
    ts = datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)
    assert algo_plus._compute_et_bucket(ts) is None
    # 03:00 ET (early morning, before extended open) = 07:00 UTC same day.
    ts2 = datetime(2026, 5, 4, 7, 0, tzinfo=timezone.utc)
    assert algo_plus._compute_et_bucket(ts2) is None


def test_compute_et_bucket_accepts_iso_string_pre_market():
    # 06:30 ET = 10:30 UTC during EDT
    assert algo_plus._compute_et_bucket("2026-05-04T10:30:00Z") == "0630"


def test_compute_et_bucket_accepts_iso_string_post_market():
    # 18:45 ET = 22:45 UTC during EDT
    assert algo_plus._compute_et_bucket("2026-05-04T22:45:00Z") == "1845"


def test_compute_et_bucket_returns_none_on_garbage():
    assert algo_plus._compute_et_bucket("not-a-date") is None
    assert algo_plus._compute_et_bucket(None) is None
