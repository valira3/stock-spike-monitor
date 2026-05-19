"""v7.105.0 (Lesson 2) -- tests for RiskBook ticket persistence.

Two layers tested:

1. **RiskBook level** -- `serialize_tickets` / `restore_tickets`
   round-trip. Tickets restored correctly; aggregate counters
   (`_open_risk`, `_open_notional`, `open_count`) recompute.
   Defensive against malformed input.

2. **RiskBookRegistry level** -- `serialize_all_tickets` /
   `restore_all_tickets` for multi-portfolio bulk operations.
   Portfolio IDs not present in the registry are silently
   dropped (no phantom-book creation).

Today's (2026-05-11) monitor noise issues #532-#596 came from the
mechanical loss of `_open_tickets` across Railway redeploys. These
helpers fix that at the root: paper_state.json now round-trips the
ticket dict so the post-deploy RiskBook matches the pre-deploy one.
"""
from __future__ import annotations

from orb.risk_book import RiskBook, RiskBookRegistry, _Ticket


# ---------------------------------------------------------------------------
# RiskBook.serialize_tickets / restore_tickets
# ---------------------------------------------------------------------------


def _admit(rb: RiskBook, risk: float, notional: float) -> _Ticket | None:
    """Helper: admit a ticket with the given risk/notional."""
    return rb.try_admit(risk_dollars=risk, notional=notional)


def test_serialize_empty_book_returns_empty_list():
    rb = RiskBook("main", equity=100_000)
    assert rb.serialize_tickets() == []


def test_serialize_after_admit_returns_ticket_dicts():
    rb = RiskBook("main", equity=100_000)
    t1 = _admit(rb, risk=750, notional=15_000)
    t2 = _admit(rb, risk=500, notional=10_000)
    assert t1 is not None and t2 is not None
    serialized = rb.serialize_tickets()
    assert len(serialized) == 2
    # All items have the expected schema.
    for item in serialized:
        assert set(item.keys()) == {"ticket_id", "risk_dollars", "notional"}
        assert isinstance(item["ticket_id"], str)
        assert isinstance(item["risk_dollars"], float)
        assert isinstance(item["notional"], float)
    # IDs from serialize match the original tickets.
    ids = {item["ticket_id"] for item in serialized}
    assert t1.ticket_id in ids
    assert t2.ticket_id in ids


def test_restore_repopulates_counters_correctly():
    """After restore, _open_risk + _open_notional + open_count should
    all match what serialize captured."""
    src = RiskBook("main", equity=100_000)
    _admit(src, 750, 15_000)
    _admit(src, 500, 10_000)
    serialized = src.serialize_tickets()

    dst = RiskBook("main", equity=100_000)
    restored = dst.restore_tickets(serialized)
    assert restored == 2
    assert dst.open_count == 2
    # Aggregate budget counters re-derived from the restored tickets.
    assert abs(dst.open_risk - (750 + 500)) < 1e-9
    assert abs(dst.open_notional - (15_000 + 10_000)) < 1e-9


def test_restore_clears_existing_tickets_first():
    """restore is authoritative -- any pre-existing tickets are wiped
    so the post-restore state matches the saved snapshot exactly."""
    rb = RiskBook("main", equity=100_000)
    _admit(rb, 100, 1_000)
    assert rb.open_count == 1
    rb.restore_tickets([
        {"ticket_id": "abc", "risk_dollars": 250.0, "notional": 5_000.0},
    ])
    assert rb.open_count == 1
    # New ticket id is in the book (not the original).
    assert "abc" in rb._open_tickets


def test_restore_handles_empty_input():
    rb = RiskBook("main", equity=100_000)
    _admit(rb, 100, 1_000)
    rb.restore_tickets([])
    assert rb.open_count == 0
    assert rb.open_risk == 0.0
    assert rb.open_notional == 0.0


def test_restore_skips_malformed_items_without_raising():
    rb = RiskBook("main", equity=100_000)
    bad_payload = [
        {"ticket_id": "good", "risk_dollars": 100.0, "notional": 1000.0},  # ok
        {"risk_dollars": 50.0, "notional": 500.0},                          # no ticket_id
        {"ticket_id": "", "risk_dollars": 50.0, "notional": 500.0},         # empty id
        {"ticket_id": "neg", "risk_dollars": -10.0, "notional": 500.0},     # negative risk
        "not a dict",                                                       # wrong type
        {"ticket_id": "bad-types", "risk_dollars": "abc", "notional": "xyz"},  # bad types
        {"ticket_id": "ok2", "risk_dollars": 200.0, "notional": 2000.0},    # ok
    ]
    restored = rb.restore_tickets(bad_payload)
    assert restored == 2  # only "good" and "ok2"
    assert rb.open_count == 2


def test_round_trip_via_json_preserves_state():
    """Serialize -> json.dumps -> json.loads -> restore round-trip
    matches the original book (the path paper_state.json takes)."""
    import json
    src = RiskBook("main", equity=100_000)
    _admit(src, 750, 15_000)
    _admit(src, 250, 5_000)
    payload = src.serialize_tickets()

    encoded = json.dumps(payload)
    decoded = json.loads(encoded)

    dst = RiskBook("main", equity=100_000)
    dst.restore_tickets(decoded)
    assert dst.open_count == src.open_count
    assert abs(dst.open_risk - src.open_risk) < 1e-9
    assert abs(dst.open_notional - src.open_notional) < 1e-9


# ---------------------------------------------------------------------------
# RiskBookRegistry.serialize_all_tickets / restore_all_tickets
# ---------------------------------------------------------------------------


def test_registry_serialize_all_returns_per_portfolio_dict():
    reg = RiskBookRegistry()
    main = reg.register("main", equity=100_000)
    val = reg.register("val", equity=50_000)
    reg.register("gene", equity=0)  # disabled; no tickets

    _admit(main, 750, 15_000)
    _admit(main, 500, 10_000)
    _admit(val, 200, 4_000)

    out = reg.serialize_all_tickets()
    assert set(out.keys()) == {"main", "val", "gene"}
    assert len(out["main"]) == 2
    assert len(out["val"]) == 1
    assert out["gene"] == []


def test_registry_restore_all_only_touches_existing_books():
    """Phantom portfolio_ids in the payload are silently dropped --
    we don't auto-create a book for a pid the registry doesn't have."""
    reg = RiskBookRegistry()
    reg.register("main", equity=100_000)
    # No val/gene books registered.
    payload = {
        "main": [{"ticket_id": "m1", "risk_dollars": 500.0, "notional": 10_000.0}],
        "val": [{"ticket_id": "v1", "risk_dollars": 200.0, "notional": 4_000.0}],
        "gene": [{"ticket_id": "g1", "risk_dollars": 100.0, "notional": 2_000.0}],
    }
    counts = reg.restore_all_tickets(payload)
    # Only main was touched.
    assert counts == {"main": 1}
    assert reg.get("main").open_count == 1
    assert reg.get("val") is None
    assert reg.get("gene") is None


def test_registry_restore_all_handles_garbage_payload():
    """Non-dict input -> empty result, no exception."""
    reg = RiskBookRegistry()
    reg.register("main", equity=100_000)
    assert reg.restore_all_tickets(None) == {}
    assert reg.restore_all_tickets("not a dict") == {}
    assert reg.restore_all_tickets([]) == {}


def test_registry_full_round_trip():
    """End-to-end: two portfolios with active tickets -> serialize ->
    fresh registry -> restore -> all counts match."""
    src = RiskBookRegistry()
    src_main = src.register("main", equity=100_000)
    src_val = src.register("val", equity=50_000)
    _admit(src_main, 750, 15_000)
    _admit(src_main, 500, 10_000)
    _admit(src_val, 200, 4_000)
    payload = src.serialize_all_tickets()

    # Simulate post-deploy: fresh registry with empty books, restore.
    dst = RiskBookRegistry()
    dst.register("main", equity=100_000)
    dst.register("val", equity=50_000)
    counts = dst.restore_all_tickets(payload)
    assert counts == {"main": 2, "val": 1}
    assert dst.get("main").open_count == src_main.open_count
    assert dst.get("val").open_count == src_val.open_count
    assert abs(dst.get("main").open_risk - src_main.open_risk) < 1e-9
    assert abs(dst.get("val").open_notional - src_val.open_notional) < 1e-9
