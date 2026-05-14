"""v7.106.0 -- regression tests for the audit's SEV-1 finding.

v7.105.0 shipped paper_state helpers that reached for the
module-level singleton `orb.risk_book.REGISTRY`. But production
never registers books into that singleton -- the engine
(`orb.engine.OrbEngine`) creates its own private
`RiskBookRegistry` at `self._risk`. The result: v7.105.0's
helpers were dead code (returned `{}` on save, no-op on restore).
The phantom-position pattern v7.105.0 was meant to fix was NOT
actually fixed.

These tests assert that `_risk_book_tickets_for_save` and
`_risk_book_tickets_restore` route through the engine's
ACTUAL registry. They construct a real OrbEngine and verify
round-trip behavior through `live_runtime.get_engine()`.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from orb.engine import OrbConfig, OrbEngine
from orb import live_runtime
import paper_state as _ps


def _make_engine(portfolio_ids=("main",)) -> OrbEngine:
    """Construct a real OrbEngine with the production wiring shape.

    OrbEngine seeds each portfolio's RiskBook with equity=100_000
    by default; callers refresh via `update_equity` once Alpaca
    reports actual balance. For these tests the default equity is
    fine -- we only care about ticket round-trips, not cap math.
    """
    cfg = OrbConfig()
    return OrbEngine(cfg, portfolio_ids=list(portfolio_ids))


def _patch_live_engine(monkeypatch, engine):
    """Monkey-patch live_runtime._engine so the production accessor
    `get_engine()` returns the test fixture engine. paper_state
    helpers go through `live_runtime.get_engine()` -- this is the
    exact integration point that v7.105.0 missed.
    """
    monkeypatch.setattr(live_runtime, "_engine", engine, raising=False)


def _admit_via_engine(engine, pid: str, risk: float, notional: float):
    """Use the engine's actual registry to admit a ticket -- mirrors
    the path try_enter() goes through in production."""
    return engine._risk.get(pid).try_admit(risk_dollars=risk, notional=notional)


# ---------------------------------------------------------------------------
# SEV-1 fix: save/restore route through engine._risk, not module REGISTRY
# ---------------------------------------------------------------------------


def test_save_helper_reads_engine_registry_not_module_registry(monkeypatch):
    """v7.106.0 regression: with an engine bootstrapped and a ticket
    admitted into engine._risk, the save helper must return the
    ticket (NOT an empty dict, which is what v7.105.0 did)."""
    engine = _make_engine()
    _patch_live_engine(monkeypatch, engine)

    ticket = _admit_via_engine(engine, "main", risk=750, notional=15_000)
    assert ticket is not None

    payload = _ps._risk_book_tickets_for_save()
    assert "main" in payload
    assert len(payload["main"]) == 1
    assert payload["main"][0]["risk_dollars"] == 750.0
    assert payload["main"][0]["notional"] == 15_000.0


def test_save_helper_returns_empty_when_engine_not_bootstrapped(monkeypatch):
    """When the engine isn't yet bootstrapped, save returns {} (silent
    no-op). This is the boot-path race we want to NOT crash on."""
    _patch_live_engine(monkeypatch, None)
    assert _ps._risk_book_tickets_for_save() == {}


def test_restore_helper_writes_to_engine_registry(monkeypatch, caplog):
    """The inverse: restore must populate engine._risk, not module
    REGISTRY. Provide a saved-at ISO for today so the date guard
    accepts the payload."""
    import logging

    caplog.set_level(logging.INFO, logger="paper_state")

    engine = _make_engine()
    _patch_live_engine(monkeypatch, engine)

    saved_payload = {
        "main": [
            {"ticket_id": "abc", "risk_dollars": 500.0, "notional": 10_000.0},
            {"ticket_id": "def", "risk_dollars": 250.0, "notional": 5_000.0},
        ],
    }
    # Today's saved_at -- date guard must accept.
    now_iso = datetime.now(timezone.utc).isoformat()
    _ps._risk_book_tickets_restore(saved_payload, saved_at_iso=now_iso)

    book = engine._risk.get("main")
    assert book.open_count == 2
    assert abs(book.open_risk - 750.0) < 1e-9
    assert abs(book.open_notional - 15_000.0) < 1e-9
    # INFO log was emitted with the count.
    assert any("[V79-ORB-PERSIST] restored 2 tickets" in r.getMessage() for r in caplog.records)


def test_restore_helper_warns_when_engine_not_bootstrapped(monkeypatch, caplog):
    """Without an engine, restore must log a WARNING (so the operator
    notices the data loss) but never raise."""
    import logging

    caplog.set_level(logging.WARNING, logger="paper_state")
    _patch_live_engine(monkeypatch, None)
    now_iso = datetime.now(timezone.utc).isoformat()
    _ps._risk_book_tickets_restore(
        {"main": [{"ticket_id": "x", "risk_dollars": 100.0, "notional": 2000.0}]},
        saved_at_iso=now_iso,
    )
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("live engine not bootstrapped" in r.getMessage() for r in warnings)


def test_restore_helper_warns_when_no_book_matches_saved_pid(monkeypatch, caplog):
    """Engine bootstrapped with ONLY 'main' book, but saved payload
    has 'val' tickets. After v7.105.0 those tickets are silently
    dropped (correct). v7.106.0 adds a WARNING so the drop is
    visible in deploy logs."""
    import logging

    caplog.set_level(logging.WARNING, logger="paper_state")

    engine = _make_engine(portfolio_ids=("main",))
    _patch_live_engine(monkeypatch, engine)

    now_iso = datetime.now(timezone.utc).isoformat()
    _ps._risk_book_tickets_restore(
        {"val": [{"ticket_id": "x", "risk_dollars": 100.0, "notional": 2000.0}]},
        saved_at_iso=now_iso,
    )
    assert any(
        "no live book matched any pid" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )


# ---------------------------------------------------------------------------
# SEV-3 fix: cross-day boot refuses to restore stale tickets
# ---------------------------------------------------------------------------


def test_cross_day_restore_refused(monkeypatch, caplog):
    """A saved_at from yesterday must NOT restore tickets. The v10
    session-start reset happens at 09:25 ET; a boot between 00:00
    and 09:25 ET would otherwise see stale tickets poisoning today's
    risk-cap math."""
    import logging

    caplog.set_level(logging.INFO, logger="paper_state")

    engine = _make_engine()
    _patch_live_engine(monkeypatch, engine)

    # Saved_at = 2 days ago. Date guard must refuse.
    yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    saved_payload = {
        "main": [{"ticket_id": "stale", "risk_dollars": 999.0, "notional": 99_999.0}],
    }
    _ps._risk_book_tickets_restore(saved_payload, saved_at_iso=yesterday_iso)

    book = engine._risk.get("main")
    assert book.open_count == 0  # nothing restored
    assert any("cross-day boot" in r.getMessage() for r in caplog.records)


def test_same_day_restore_accepted(monkeypatch):
    """The inverse: a saved_at from earlier today must restore
    normally."""
    engine = _make_engine()
    _patch_live_engine(monkeypatch, engine)

    # Use noon ET on today's ET date so midnight-crossing runs don't
    # accidentally land on a different ET calendar day (cross-day guard
    # would reject them).
    _et = ZoneInfo("America/New_York")
    minutes_ago = (
        datetime.now(timezone.utc).astimezone(_et).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    )
    saved_payload = {
        "main": [{"ticket_id": "fresh", "risk_dollars": 500.0, "notional": 10_000.0}],
    }
    _ps._risk_book_tickets_restore(saved_payload, saved_at_iso=minutes_ago)

    book = engine._risk.get("main")
    assert book.open_count == 1


def test_missing_saved_at_proceeds_with_restore(monkeypatch):
    """If saved_at is empty/missing (legacy paper_state from before
    v7.106), the date guard is bypassed and restore proceeds. Avoids
    a back-compat break on first boot after upgrade."""
    engine = _make_engine()
    _patch_live_engine(monkeypatch, engine)

    saved_payload = {
        "main": [{"ticket_id": "legacy", "risk_dollars": 200.0, "notional": 4_000.0}],
    }
    _ps._risk_book_tickets_restore(saved_payload, saved_at_iso="")

    book = engine._risk.get("main")
    assert book.open_count == 1
