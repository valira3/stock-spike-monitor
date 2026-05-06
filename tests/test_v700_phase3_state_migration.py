"""tests/test_v700_phase3_state_migration.py

v7.0.0 Phase 3 -- paper_state migration tests.

Verifies:
- First boot: if paper_state_main.json is absent but paper_state.json
  exists, load_paper_state() copies it forward with a [V700-MIGRATE]
  log line and reads from the new path.
- Subsequent saves write to paper_state_main.json only (not the legacy
  path).
- Second boot: when paper_state_main.json already exists, no migration
  occurs and the file is read directly.
- Legacy paper_state.json is never deleted.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Minimal trade_genius stub
# ---------------------------------------------------------------------------

_KNOWN_COLLECTIONS = (
    "positions", "short_positions", "paper_trades", "paper_all_trades",
    "trade_history", "short_trade_history", "daily_entry_count",
    "daily_short_entry_count", "v5_long_tracks", "v5_short_tracks",
    "v5_active_direction",
)

_KNOWN_SCALARS = {
    "paper_cash": 100_000.0,
    "daily_entry_date": "",
    "daily_short_entry_date": "",
    "_scan_paused": False,
    "_trading_halted": False,
    "_trading_halted_reason": "",
    "or_high": {},
    "or_low": {},
    "pdc": {},
    "or_collected_date": "",
    "user_config": {},
    "_last_exit_time": {},
    "PAPER_STARTING_CAPITAL": 100_000.0,
}


def _make_tg_stub(legacy_path: str, main_path: str):
    """Build a minimal trade_genius module stub."""
    mod = types.ModuleType("trade_genius")
    mod.BOT_NAME = "TradeGenius"
    mod.PAPER_STATE_FILE = legacy_path
    mod.PAPER_STATE_MAIN_FILE = main_path

    for col in _KNOWN_COLLECTIONS:
        if col.endswith("_tracks") or col.endswith("_direction") or col.endswith("_count"):
            setattr(mod, col, {})
        elif "history" in col or "trades" in col:
            setattr(mod, col, [])
        else:
            setattr(mod, col, {})

    for k, v in _KNOWN_SCALARS.items():
        setattr(mod, k, v)

    def paper_log(msg):
        logging.getLogger("trade_genius").info(msg)

    def _now_et():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).astimezone()

    def _utc_now_iso():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    mod.paper_log = paper_log
    mod._now_et = _now_et
    mod._utc_now_iso = _utc_now_iso
    return mod


def _make_minimal_state(cash: float = 50_000.0) -> dict:
    """Return a minimal paper state dict that load_paper_state() can parse."""
    return {
        "paper_cash": cash,
        "positions": {},
        "short_positions": {},
        "paper_trades": [],
        "paper_all_trades": [],
        "daily_entry_count": {},
        "daily_entry_date": "",
        "or_high": {},
        "or_low": {},
        "pdc": {},
        "or_collected_date": "",
        "user_config": {},
        "trade_history": [],
        "short_trade_history": [],
        "daily_short_entry_count": {},
        "daily_short_entry_date": "",
        "last_exit_time": {},
        "_scan_paused": False,
        "_trading_halted": False,
        "_trading_halted_reason": "",
        "v5_active_direction": {},
        "saved_at": "2026-05-06T09:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Helper to import paper_state under a given stub
# ---------------------------------------------------------------------------

def _load_paper_state_module(tg_stub):
    """Import paper_state.py with the given trade_genius stub injected."""
    # Stub out heavy dependencies that paper_state.py may import.
    for mod_name in ("persistence", "tiger_buffalo_v5"):
        if mod_name not in sys.modules:
            stub = types.ModuleType(mod_name)
            stub.init_db = lambda: None
            stub.load_all_tracks = lambda direction: {}
            stub.replace_all_tracks = lambda a, b: None
            sys.modules[mod_name] = stub

    sys.modules["trade_genius"] = tg_stub

    # Force fresh import of paper_state.
    for key in list(sys.modules):
        if "paper_state" in key:
            del sys.modules[key]

    import paper_state as ps
    # Reset the module-level _state_loaded flag.
    ps._state_loaded = False
    return ps


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMigrationOnFirstBoot:
    def test_legacy_copied_to_main_path(self, tmp_path, caplog):
        """Legacy paper_state.json -> paper_state_main.json on first boot."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        state = _make_minimal_state(cash=42_000.0)
        legacy.write_text(json.dumps(state), encoding="utf-8")

        tg = _make_tg_stub(str(legacy), str(main))
        ps = _load_paper_state_module(tg)

        with caplog.at_level(logging.INFO, logger="paper_state"):
            ps.load_paper_state()

        assert main.exists(), "paper_state_main.json was not created by migration"

    def test_migrate_log_line_emitted(self, tmp_path, caplog):
        """[V700-MIGRATE] must appear in logs when migration runs."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        state = _make_minimal_state(cash=42_000.0)
        legacy.write_text(json.dumps(state), encoding="utf-8")

        tg = _make_tg_stub(str(legacy), str(main))
        ps = _load_paper_state_module(tg)

        with caplog.at_level(logging.INFO):
            ps.load_paper_state()

        assert any("[V700-MIGRATE]" in r.message for r in caplog.records), (
            "[V700-MIGRATE] log line not emitted during migration"
        )

    def test_migrated_content_matches_legacy(self, tmp_path):
        """The migrated file must have identical JSON content to the legacy file."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        state = _make_minimal_state(cash=77_777.0)
        legacy.write_text(json.dumps(state, indent=2), encoding="utf-8")

        tg = _make_tg_stub(str(legacy), str(main))
        ps = _load_paper_state_module(tg)
        ps.load_paper_state()

        migrated = json.loads(main.read_text(encoding="utf-8"))
        assert migrated["paper_cash"] == 77_777.0

    def test_legacy_file_not_deleted_after_migration(self, tmp_path):
        """Legacy paper_state.json must survive the migration (rollback path)."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        state = _make_minimal_state(cash=55_000.0)
        legacy.write_text(json.dumps(state), encoding="utf-8")

        tg = _make_tg_stub(str(legacy), str(main))
        ps = _load_paper_state_module(tg)
        ps.load_paper_state()

        assert legacy.exists(), "Legacy paper_state.json was deleted -- must be kept as rollback"

    def test_cash_loaded_correctly_after_migration(self, tmp_path):
        """After migration, the stub's paper_cash is set from the file content."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        state = _make_minimal_state(cash=88_888.0)
        legacy.write_text(json.dumps(state), encoding="utf-8")

        tg = _make_tg_stub(str(legacy), str(main))
        ps = _load_paper_state_module(tg)
        ps.load_paper_state()

        assert tg.paper_cash == 88_888.0, f"Expected 88888, got {tg.paper_cash}"


class TestSaveWritesToMainPath:
    def test_save_writes_main_path_not_legacy(self, tmp_path):
        """save_paper_state() must write paper_state_main.json, not paper_state.json."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        # Provide an existing main file so load reads from it cleanly.
        state = _make_minimal_state(cash=30_000.0)
        main.write_text(json.dumps(state), encoding="utf-8")

        tg = _make_tg_stub(str(legacy), str(main))
        # Extend stub with extra attributes save_paper_state() accesses.
        tg.or_high = {}
        tg.or_low = {}
        tg.pdc = {}
        tg.or_collected_date = ""
        tg.user_config = {}
        tg._last_exit_time = {}
        tg._scan_paused = False
        tg.v5_active_direction = {}
        tg.PAPER_STARTING_CAPITAL = 100_000.0

        ps = _load_paper_state_module(tg)
        ps.load_paper_state()

        # Mutate cash to ensure we can detect the write.
        tg.paper_cash = 12_345.0
        ps.save_paper_state()

        saved = json.loads(main.read_text(encoding="utf-8"))
        assert saved["paper_cash"] == 12_345.0, "Main path not updated by save"

    def test_save_does_not_overwrite_legacy(self, tmp_path):
        """Legacy paper_state.json must be unchanged after a save."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        original_cash = 99_000.0
        state = _make_minimal_state(cash=original_cash)
        legacy.write_text(json.dumps(state), encoding="utf-8")
        main.write_text(json.dumps(state), encoding="utf-8")

        tg = _make_tg_stub(str(legacy), str(main))
        tg.or_high = {}
        tg.or_low = {}
        tg.pdc = {}
        tg.or_collected_date = ""
        tg.user_config = {}
        tg._last_exit_time = {}
        tg._scan_paused = False
        tg.v5_active_direction = {}
        tg.PAPER_STARTING_CAPITAL = 100_000.0

        ps = _load_paper_state_module(tg)
        ps.load_paper_state()
        tg.paper_cash = 1.0
        ps.save_paper_state()

        # Legacy must still hold the original cash value.
        legacy_state = json.loads(legacy.read_text(encoding="utf-8"))
        assert legacy_state["paper_cash"] == original_cash, (
            "Legacy paper_state.json was mutated by save -- must be read-only"
        )


class TestSecondBootNoMigration:
    def test_no_migrate_log_when_main_already_exists(self, tmp_path, caplog):
        """If paper_state_main.json exists, no [V700-MIGRATE] line is logged."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        state = _make_minimal_state(cash=60_000.0)
        legacy.write_text(json.dumps(state), encoding="utf-8")
        main.write_text(json.dumps(state), encoding="utf-8")

        tg = _make_tg_stub(str(legacy), str(main))
        ps = _load_paper_state_module(tg)

        with caplog.at_level(logging.INFO):
            ps.load_paper_state()

        migrate_lines = [r for r in caplog.records if "[V700-MIGRATE]" in r.message]
        assert not migrate_lines, (
            f"[V700-MIGRATE] logged on second boot when it should not be: {migrate_lines}"
        )

    def test_main_file_read_directly_on_second_boot(self, tmp_path):
        """Second boot reads paper_state_main.json; legacy value is ignored."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        # Legacy has stale cash; main has updated cash.
        legacy.write_text(json.dumps(_make_minimal_state(cash=10_000.0)), encoding="utf-8")
        main.write_text(json.dumps(_make_minimal_state(cash=20_000.0)), encoding="utf-8")

        tg = _make_tg_stub(str(legacy), str(main))
        ps = _load_paper_state_module(tg)
        ps.load_paper_state()

        assert tg.paper_cash == 20_000.0, (
            f"Expected cash from main file (20000), got {tg.paper_cash}"
        )


class TestFreshStartNoFiles:
    def test_fresh_start_when_no_files_exist(self, tmp_path):
        """If neither file exists, boot proceeds with fresh $100k state."""
        legacy = tmp_path / "paper_state.json"
        main = tmp_path / "paper_state_main.json"

        tg = _make_tg_stub(str(legacy), str(main))
        ps = _load_paper_state_module(tg)
        ps.load_paper_state()

        # State_loaded should be set; cash at starting capital.
        assert ps._state_loaded is True
        assert tg.paper_cash == tg.PAPER_STARTING_CAPITAL
