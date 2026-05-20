"""v8.3.22 -- OrbEngine.purge_non_recover_tickets tests.

Operator screenshot post-v8.3.20 deploy showed risk_reject:notional_cap
STILL firing (would-be $300K > cap $96K) because the leftover tickets
were uuid-style from failed try_admit/rollback paths, NOT
`recover-{pid}-{ticker}` ids. v8.3.20's sweep was scope-limited to
recover-* (uuid->ticker mapping isn't safe in the general case). v8.3.22
nukes ALL non-recover tickets ONCE at boot, then v8.3.6 mirror re-adds
clean `recover-*` tickets from held positions.

Boot-only because uuid tickets are legitimately created by try_admit
during normal trading; per-cycle purge would clobber in-flight admits.
"""
from __future__ import annotations

import pytest

from orb.engine import OrbConfig, OrbEngine
import orb.risk_book as _rb_mod


def _cfg():
    return OrbConfig(
        or_minutes=30,
        skip_earnings_window=False,
        fail_closed_on_missing_vix=False,
        ticker_side_blocklist=None,
    )


def _insert_ticket(eng, pid, ticket_id, risk=150.0, notional=15000.0):
    rb = eng._risk.get(pid)
    if rb is None:
        return None
    with rb._lock:
        rb._open_tickets[ticket_id] = _rb_mod._Ticket(
            ticket_id=ticket_id, risk_dollars=risk, notional=notional,
        )
        rb._open_risk += risk
        rb._open_notional += notional
    return ticket_id


class TestPurgeNonRecoverTickets:

    def test_empty_no_purge(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        assert eng.purge_non_recover_tickets() == {}
        rb = eng._risk.get("main")
        assert rb.open_count == 0

    def test_recover_tickets_kept(self):
        """recover-{pid}-{ticker} tickets are legitimate v8.3.6 mirrors
        of held positions -- must NOT be purged."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_ticket(eng, "main", "recover-main-GOOG", risk=100, notional=73000)
        _insert_ticket(eng, "main", "recover-main-NFLX", risk=80, notional=60000)
        out = eng.purge_non_recover_tickets()
        assert out == {}
        rb = eng._risk.get("main")
        assert rb.open_count == 2
        assert rb.open_notional == pytest.approx(133000.0)

    def test_uuid_tickets_purged(self):
        """uuid tickets surviving across boot are orphans from
        try_admit that wasn't released."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_ticket(eng, "main", "abc123def456", risk=100, notional=45000)
        _insert_ticket(eng, "main", "xyz789ghi012", risk=120, notional=48000)
        _insert_ticket(eng, "main", "uvw345mno678", risk=110, notional=40000)
        rb = eng._risk.get("main")
        assert rb.open_count == 3
        assert rb.open_notional == pytest.approx(133000.0)
        out = eng.purge_non_recover_tickets()
        assert out == {"main": 3}
        assert rb.open_count == 0
        assert rb.open_risk == pytest.approx(0.0)
        assert rb.open_notional == pytest.approx(0.0)

    def test_mixed_keeps_recover_purges_uuid(self):
        """Operator's exact scenario: 1 real recover-main-GOOG + 3
        uuid orphans. Purge clears the 3 uuid; GOOG ticket survives."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_ticket(eng, "main", "recover-main-GOOG", risk=190, notional=73000)
        _insert_ticket(eng, "main", "uuid111aaa", risk=100, notional=43000)
        _insert_ticket(eng, "main", "uuid222bbb", risk=110, notional=44000)
        _insert_ticket(eng, "main", "uuid333ccc", risk=120, notional=43000)
        rb = eng._risk.get("main")
        # Pre-purge: 4 tickets, notional ~$203K (matches operator's
        # `would-be $300K > $96K cap` because new admit ~$96K + open $203K = $299K)
        assert rb.open_count == 4
        assert rb.open_notional == pytest.approx(203000.0)
        # Run the purge
        out = eng.purge_non_recover_tickets()
        assert out == {"main": 3}
        # Post-purge: 1 ticket (GOOG real), notional ~$73K
        assert rb.open_count == 1
        assert rb.open_notional == pytest.approx(73000.0)
        # New admit of ~$96K would now make would-be ~$169K which
        # exceeds the $96K cap legitimately. Operator needs to wait
        # for GOOG to close before another big-notional admit.
        # BUT the rejects on signals smaller than ~$23K (= cap - GOOG)
        # will stop firing -- that's the immediate win.

    def test_purge_across_multiple_pids(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main", "val", "gene"])
        _insert_ticket(eng, "main", "recover-main-AAPL", notional=15000)
        _insert_ticket(eng, "main", "uuid_orphan_1", notional=20000)
        _insert_ticket(eng, "val", "recover-val-NVDA", notional=8000)
        _insert_ticket(eng, "val", "uuid_orphan_2", notional=18000)
        _insert_ticket(eng, "gene", "uuid_orphan_3", notional=12000)
        out = eng.purge_non_recover_tickets()
        assert out == {"main": 1, "val": 1, "gene": 1}
        assert eng._risk.get("main").open_count == 1  # recover- survives
        assert eng._risk.get("val").open_count == 1
        assert eng._risk.get("gene").open_count == 0

    def test_purge_does_not_touch_fsm(self):
        """The purge only touches RiskBook tickets. FSM state
        (day_states, OR windows) is not modified -- that's v8.3.15's
        territory."""
        from orb.state import PHASE_IN_POS
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        ds = eng._state.get_day_state("main", "GOOG")
        ds.in_position = True
        ds.transition(PHASE_IN_POS)
        _insert_ticket(eng, "main", "uuid_orphan", notional=20000)
        eng.purge_non_recover_tickets()
        ds_after = eng._state.get_day_state("main", "GOOG")
        assert ds_after.in_position is True  # unchanged
        assert ds_after.phase == PHASE_IN_POS

    def test_purge_clamps_negative_risk_notional(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_ticket(eng, "main", "uuid_orphan", risk=100, notional=10000)
        rb = eng._risk.get("main")
        # Manually drift the totals to simulate fp accumulation noise
        with rb._lock:
            rb._open_risk = 99.99
            rb._open_notional = 9999.99
        eng.purge_non_recover_tickets()
        assert rb.open_risk == 0.0
        assert rb.open_notional == 0.0

    # v9.1.140 -- V834-PERSIST section G builds tickets as
    # `recover-{original_tid}` (i.e. `recover-<uuid>`). The
    # pre-v9.1.140 V8322 only matched `recover-{pid}-` which deleted
    # these legitimate restored tickets and produced the 2026-05-20
    # NVDA phantom (positions held, RiskBook empty).

    def test_recover_uuid_prefix_kept(self):
        """V834-PERSIST section G shape: recover-<original_uuid>.
        Must survive V8322 even though it doesn't carry a pid hint."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_ticket(eng, "main", "recover-abc123def456", risk=100, notional=73000)
        out = eng.purge_non_recover_tickets()
        assert out == {}
        rb = eng._risk.get("main")
        assert rb.open_count == 1
        assert rb.open_notional == pytest.approx(73000.0)

    def test_recover_recover_uuid_prefix_kept(self):
        """Multi-boot V834-PERSIST shape: recover-recover-<uuid>.
        Each rehydrate cycle wraps the persisted tid with another
        `recover-` prefix, so a position that survives 2+ deploys
        accumulates the doubled prefix."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_ticket(eng, "main", "recover-recover-abc123def456", risk=100, notional=60000)
        out = eng.purge_non_recover_tickets()
        assert out == {}
        rb = eng._risk.get("main")
        assert rb.open_count == 1

    def test_v834_persist_section_g_roundtrip_survives_purge(self):
        """Integration: simulate the exact post-rehydrate state from
        the 2026-05-20 NVDA incident. Section C loaded the raw uuid
        ticket (no prefix) AND section G layered a
        `recover-{original_tid}` ticket on top with the same risk.
        V8322 should purge only the bare uuid, leaving the recover-
        ticket and its risk_dollars intact."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        # Section C: raw uuid ticket -- to be purged.
        _insert_ticket(eng, "main", "abc123def456uuid", risk=150, notional=77000)
        # Section G: recover-<uuid> ticket -- to be kept.
        _insert_ticket(eng, "main", "recover-abc123def456uuid", risk=150, notional=77000)
        rb = eng._risk.get("main")
        # Pre-purge double-count (the design comment in apply_loaded_state):
        # _open_risk = C_persisted + G_recover.
        assert rb.open_count == 2
        assert rb.open_risk == pytest.approx(300.0)
        out = eng.purge_non_recover_tickets()
        # Only section C (uuid) is purged.
        assert out == {"main": 1}
        assert rb.open_count == 1
        # Section G ticket survives with its risk_dollars intact.
        assert rb.open_risk == pytest.approx(150.0)
        assert rb.open_notional == pytest.approx(77000.0)
        with rb._lock:
            assert "recover-abc123def456uuid" in rb._open_tickets
            assert "abc123def456uuid" not in rb._open_tickets


class _FakeAdapter:
    """Stand-in for `orb.live_adapter.LiveAdapter` exposing only the
    `_open_positions` dict that V8322 inspects in adapter-aware mode."""

    def __init__(self, tids):
        self._open_positions = {tid: object() for tid in tids}


class _FakeAdapterRegistry:
    """Stand-in for `orb.live_adapter.LiveAdapters` with a `.get(pid)`."""

    def __init__(self, by_pid):
        self._by_pid = dict(by_pid)

    def get(self, pid):
        return self._by_pid.get(pid)


class TestPurgePositionAware:
    """v9.1.141 -- adapter-aware mode tests. When the caller passes
    `adapters`, V8322's invariant flips from prefix-matching to
    "ticket must have a backing position". This catches the
    section-C / section-G double-load case that v9.1.140 could not."""

    def test_backed_ticket_kept_regardless_of_prefix(self):
        """A bare uuid ticket WITH a matching adapter position must
        survive when the adapter-aware path is active. It represents
        a real position whose ticket happens not to carry a recover-
        prefix (e.g. fresh try_admit on the current session)."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_ticket(eng, "main", "live_uuid_no_prefix", risk=180, notional=72000)
        adapters = _FakeAdapterRegistry({
            "main": _FakeAdapter(["live_uuid_no_prefix"]),
        })
        out = eng.purge_non_recover_tickets(adapters=adapters)
        assert out == {}
        rb = eng._risk.get("main")
        assert rb.open_count == 1
        assert rb.open_notional == pytest.approx(72000.0)

    def test_recover_ticket_without_position_purged(self):
        """A recover- prefixed ticket without a matching adapter
        position is still an orphan -- purge it. This is the bug
        v9.1.140 introduced: section C duplicates that happen to
        start with `recover-` survived the prefix-only check."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_ticket(eng, "main", "recover-orphan-no-position", risk=200, notional=80000)
        adapters = _FakeAdapterRegistry({
            "main": _FakeAdapter([]),  # adapter has zero positions
        })
        out = eng.purge_non_recover_tickets(adapters=adapters)
        assert out == {"main": 1}
        rb = eng._risk.get("main")
        assert rb.open_count == 0
        assert rb.open_risk == pytest.approx(0.0)

    def test_section_c_purged_section_g_kept_with_adapter(self):
        """The exact post-rehydrate shape that produced the
        2026-05-20 `open_count=2` CRIT.

        Pre-state: 2 tickets for NVDA -- `recover-<uuid>` (section C
        from prior boot's persisted open_tickets) AND
        `recover-recover-<uuid>` (section G's wrap of the same
        persisted tid, which IS the adapter's keyed position).

        Adapter has 1 position, keyed by section G's
        `recover-recover-<uuid>`. V8322 must purge the section C
        ticket (no position backing) and keep the section G ticket."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        # Section C, no adapter backing.
        _insert_ticket(eng, "main", "recover-abc123", risk=140, notional=77000)
        # Section G, mirrors adapter position.
        _insert_ticket(eng, "main", "recover-recover-abc123", risk=140, notional=77000)
        rb = eng._risk.get("main")
        assert rb.open_count == 2
        assert rb.open_risk == pytest.approx(280.0)
        adapters = _FakeAdapterRegistry({
            "main": _FakeAdapter(["recover-recover-abc123"]),
        })
        out = eng.purge_non_recover_tickets(adapters=adapters)
        assert out == {"main": 1}
        assert rb.open_count == 1
        assert rb.open_risk == pytest.approx(140.0)
        with rb._lock:
            assert "recover-recover-abc123" in rb._open_tickets
            assert "recover-abc123" not in rb._open_tickets

    def test_adapter_none_falls_back_to_prefix_only(self):
        """When `adapters` is None (legacy callers / unit tests that
        haven't migrated), the v9.1.140 prefix-only contract holds.
        This keeps the existing test suite + any out-of-tree callers
        working without modification."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _insert_ticket(eng, "main", "recover-keep-me", risk=100, notional=50000)
        _insert_ticket(eng, "main", "bare_uuid_orphan", risk=110, notional=44000)
        out = eng.purge_non_recover_tickets(adapters=None)
        assert out == {"main": 1}
        rb = eng._risk.get("main")
        assert rb.open_count == 1
        with rb._lock:
            assert "recover-keep-me" in rb._open_tickets
            assert "bare_uuid_orphan" not in rb._open_tickets
