# tests/test_v6_0_8_session_persist.py
# v6.0.8 -- session-state SQLite persistence.
#
# Background: trade_genius.py kept _v570_strike_counts, _v570_session_hod,
# _v570_session_lod, _v570_daily_realized_pnl, and _v570_kill_switch_latched
# in module-level dicts. On Apr 30 a 9-redeploy day caused NVDA strike 2/3
# to fire off shallow LODs because the in-memory _v570_session_lod for NVDA
# had been cleared mid-RTH despite the real session LOD already being set.
#
# v6.0.8 mirrors all five values to two new SQLite tables (session_state and
# session_globals) on every mutation, and rehydrates from disk on the first
# call to _v570_reset_if_new_session() per ET date in the process.
#
# These tests verify the helpers directly + the end-to-end rehydrate path
# through trade_genius.
#
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations

import importlib

import persistence


def _reset_persistence(tmp_path):
    """Point persistence.STATE_DB_PATH at a fresh per-test tmp file."""
    db = tmp_path / "state.db"
    persistence._close_for_tests()
    persistence.init_db(str(db))
    return str(db)


def test_session_state_table_is_created(tmp_path):
    _reset_persistence(tmp_path)
    c = persistence._conn()
    cur = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_state'")
    assert cur.fetchone() is not None


def test_session_globals_table_is_created(tmp_path):
    _reset_persistence(tmp_path)
    c = persistence._conn()
    cur = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_globals'")
    assert cur.fetchone() is not None


def test_save_and_load_session_state_roundtrip(tmp_path):
    _reset_persistence(tmp_path)
    persistence.save_session_state(
        "NVDA",
        "2026-04-30",
        session_hod=205.50,
        session_lod=197.38,
        strike_count=2,
    )
    rows = persistence.load_session_state_for_date("2026-04-30")
    assert "NVDA" in rows
    nvda = rows["NVDA"]
    assert nvda["session_hod"] == 205.50
    assert nvda["session_lod"] == 197.38
    assert nvda["strike_count"] == 2


def test_save_session_state_coalesce_preserves_unchanged_fields(tmp_path):
    _reset_persistence(tmp_path)
    persistence.save_session_state(
        "NVDA",
        "2026-04-30",
        session_hod=205.50,
        session_lod=197.38,
        strike_count=2,
    )
    # Update only strike_count; HOD and LOD should be preserved.
    persistence.save_session_state("NVDA", "2026-04-30", strike_count=3)
    rows = persistence.load_session_state_for_date("2026-04-30")
    assert rows["NVDA"]["session_hod"] == 205.50
    assert rows["NVDA"]["session_lod"] == 197.38
    assert rows["NVDA"]["strike_count"] == 3


def test_session_state_keyed_by_et_date(tmp_path):
    _reset_persistence(tmp_path)
    persistence.save_session_state(
        "NVDA",
        "2026-04-30",
        session_hod=205.0,
        session_lod=197.0,
    )
    persistence.save_session_state(
        "NVDA",
        "2026-05-01",
        session_hod=210.0,
        session_lod=204.0,
    )
    apr30 = persistence.load_session_state_for_date("2026-04-30")
    may01 = persistence.load_session_state_for_date("2026-05-01")
    assert apr30["NVDA"]["session_hod"] == 205.0
    assert may01["NVDA"]["session_hod"] == 210.0


def test_prune_session_state_removes_other_dates(tmp_path):
    _reset_persistence(tmp_path)
    persistence.save_session_state(
        "NVDA",
        "2026-04-29",
        session_hod=200.0,
        session_lod=195.0,
    )
    persistence.save_session_state(
        "NVDA",
        "2026-04-30",
        session_hod=205.0,
        session_lod=197.0,
    )
    deleted = persistence.prune_session_state("2026-04-30")
    assert deleted == 1
    apr29 = persistence.load_session_state_for_date("2026-04-29")
    apr30 = persistence.load_session_state_for_date("2026-04-30")
    assert apr29 == {}
    assert "NVDA" in apr30


def test_save_and_load_session_globals_roundtrip(tmp_path):
    _reset_persistence(tmp_path)
    persistence.save_session_global(
        "daily_realized_pnl",
        "2026-04-30",
        value_real=-450.25,
    )
    persistence.save_session_global(
        "kill_switch_latched",
        "2026-04-30",
        value_int=1,
    )
    rows = persistence.load_session_globals_for_date("2026-04-30")
    assert rows["daily_realized_pnl"]["value_real"] == -450.25
    assert rows["kill_switch_latched"]["value_int"] == 1


def test_session_globals_coalesce_preserves_unchanged_field(tmp_path):
    _reset_persistence(tmp_path)
    persistence.save_session_global(
        "daily_realized_pnl",
        "2026-04-30",
        value_real=-100.0,
        value_int=0,
    )
    # Update only value_real; value_int should be preserved at 0.
    persistence.save_session_global(
        "daily_realized_pnl",
        "2026-04-30",
        value_real=-200.0,
    )
    rows = persistence.load_session_globals_for_date("2026-04-30")
    pnl = rows["daily_realized_pnl"]
    assert pnl["value_real"] == -200.0
    assert pnl["value_int"] == 0


def test_trade_genius_rehydrates_session_lod_from_disk(tmp_path, monkeypatch):
    """Regression test for the Apr 30 NVDA strike 2/3 bug. After a
    Railway redeploy, _v570_session_lod for NVDA must be re-seeded
    from the on-disk row so a shallow follow-on print does not
    register as a fresh lod_break."""
    _reset_persistence(tmp_path)
    # Pre-seed disk with NVDA's real Apr 30 LOD.
    persistence.save_session_state(
        "NVDA",
        "2026-04-30",
        session_hod=205.50,
        session_lod=197.38,
        strike_count=1,
    )
    # Simulate a fresh process boot: re-import trade_genius so all the
    # _v570_* module-level dicts start empty.
    import trade_genius

    importlib.reload(trade_genius)
    # Force "today" to match the seeded ET date.
    monkeypatch.setattr(trade_genius, "_v570_session_today_str", lambda: "2026-04-30")
    # First call into the gate triggers rehydrate_from_disk.
    trade_genius._v570_reset_if_new_session()
    assert trade_genius._v570_session_lod.get("NVDA") == 197.38
    assert trade_genius._v570_session_hod.get("NVDA") == 205.50
    assert trade_genius._v570_strike_counts.get("NVDA") == 1


def test_trade_genius_rehydrate_blocks_spurious_lod_break(tmp_path, monkeypatch):
    """End-to-end: with a persisted NVDA LOD of 197.38, a 198.57 print
    after redeploy must NOT register as a new lod_break (which would
    have triggered the spurious strike 2/3 entry on Apr 30)."""
    _reset_persistence(tmp_path)
    persistence.save_session_state(
        "NVDA",
        "2026-04-30",
        session_hod=205.50,
        session_lod=197.38,
        strike_count=1,
    )
    import trade_genius

    importlib.reload(trade_genius)
    monkeypatch.setattr(trade_genius, "_v570_session_today_str", lambda: "2026-04-30")
    monkeypatch.setattr(trade_genius, "_v570_is_session_open", lambda: True)
    prev_hod, prev_lod, hod_break, lod_break = trade_genius._v570_update_session_hod_lod(
        "NVDA", 198.57
    )
    assert prev_lod == 197.38
    # 198.57 is ABOVE the persisted 197.38 LOD; no fresh lod_break.
    assert lod_break is False
    # And no fresh hod_break either (198.57 < persisted HOD 205.50).
    assert hod_break is False
