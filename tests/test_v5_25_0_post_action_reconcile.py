"""v5.25.0 \u2014 post-action reconcile + enabled-exec chips.

Three behavioural changes covered here:

1. ``TradeGeniusBase._reconcile_position_with_broker(ticker)`` syncs a
   single ticker's local state from Alpaca's authoritative book using
   ``client.get_open_position(ticker)``. Three outcomes are exercised:
   broker-has-position (overwrite qty/entry_price), 40410000 (drop local
   row), and other API errors (leave state untouched).

2. ``_on_signal`` calls the post-action reconcile after every successful
   ENTRY_LONG / ENTRY_SHORT / EXIT_LONG / EXIT_SHORT submit, and runs a
   full-sweep ``_reconcile_broker_positions`` after EOD_CLOSE_ALL.

3. ``dashboard_server._executors_status_snapshot`` exposes the val/gene
   enabled flags on /api/state for the dashboard header chips.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Shared executor harness (mirrors test_v5_24_0 pattern)
# ---------------------------------------------------------------------------


def _make_executor(monkeypatch):
    """Construct a stub TradeGeniusBase subclass with a programmable
    fake Alpaca client. Returns ``(inst, submits, closes, broker_book,
    fake_client)`` where ``broker_book`` is a mutable dict the tests
    can preload with broker-side positions before driving signals."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst.dollars_per_entry = 10_000.0

    submits: list = []
    closes: list = []
    # Mutable broker-side book the tests can poke at.
    broker_book: dict = {}
    # If a ticker is in this set, get_open_position raises 40410000.
    raise_404_on_get: set = set()
    # If non-empty, get_open_position raises this exception type for any
    # ticker (used to test "other error" path).
    raise_other_on_get: dict = {"exc": None}

    class _FakeAcct:
        equity = 200_000.0
        cash = 200_000.0
        buying_power = 400_000.0

    class _FakePos:
        def __init__(self, symbol, qty, avg_entry_price):
            self.symbol = symbol
            self.qty = qty
            self.avg_entry_price = avg_entry_price

    class _FakeClient:
        def get_account(self):
            return _FakeAcct()

        def submit_order(self, req):
            submits.append(req)
            return types.SimpleNamespace(id="fake-order-id")

        def close_position(self, ticker):
            closes.append(ticker)
            broker_book.pop(ticker, None)

        def close_all_positions(self, cancel_orders=False):
            closes.append(("ALL", cancel_orders))
            broker_book.clear()

        def get_open_position(self, ticker):
            if raise_other_on_get["exc"] is not None:
                raise raise_other_on_get["exc"]
            if ticker in raise_404_on_get or ticker not in broker_book:
                raise Exception('{"code":40410000,"message":"position not found: ' + ticker + '"}')
            spec = broker_book[ticker]
            return _FakePos(ticker, spec["qty"], spec["avg_entry_price"])

        def get_all_positions(self):
            return [
                _FakePos(t, spec["qty"], spec["avg_entry_price"]) for t, spec in broker_book.items()
            ]

    fake_client = _FakeClient()
    inst._ensure_client = lambda: fake_client  # type: ignore
    inst._send_own_telegram = lambda _msg: None  # type: ignore
    inst._submit_order_idempotent = lambda client, req, coid: client.submit_order(req)  # type: ignore
    inst._build_client_order_id = lambda ticker, side: f"{ticker}-{side}-coid"  # type: ignore
    inst._persist_position = lambda _t: None  # type: ignore
    inst._delete_persisted_position = lambda _t: None  # type: ignore

    return (
        inst,
        submits,
        closes,
        broker_book,
        raise_404_on_get,
        raise_other_on_get,
        fake_client,
    )


# ---------------------------------------------------------------------------
# _reconcile_position_with_broker \u2014 direct unit tests
# ---------------------------------------------------------------------------


def test_post_reconcile_overwrites_qty_and_entry_from_broker(monkeypatch):
    """Broker has the position \u2014 helper must overwrite local qty and
    entry_price with broker-authoritative values."""
    inst, _s, _c, broker_book, _r404, _rother, _fc = _make_executor(monkeypatch)
    # Local row says 9 shares @ 270.00 \u2014 simulates a partial-fill drift.
    inst._record_position("AAPL", "LONG", 9, 270.00)
    # Broker actually has 18 shares @ 274.26.
    broker_book["AAPL"] = {"qty": 18, "avg_entry_price": 274.26}

    inst._reconcile_position_with_broker("AAPL")

    assert inst.positions["AAPL"]["qty"] == 18
    assert abs(inst.positions["AAPL"]["entry_price"] - 274.26) < 1e-6
    assert inst.positions["AAPL"]["side"] == "LONG"


def test_post_reconcile_drops_local_row_on_40410000(monkeypatch):
    """Broker reports 40410000 (flat) \u2014 helper must drop the local row."""
    inst, _s, _c, _broker_book, raise_404, _rother, _fc = _make_executor(monkeypatch)
    inst._record_position("MSFT", "LONG", 12, 410.00)
    raise_404.add("MSFT")  # broker says position not found

    inst._reconcile_position_with_broker("MSFT")

    assert "MSFT" not in inst.positions, "helper must drop local row when broker returns 40410000"


def test_post_reconcile_leaves_state_untouched_on_other_errors(monkeypatch):
    """Non-404 API errors must NOT corrupt local state \u2014 we just WARN
    and rely on the next signal / boot reconcile to heal divergence."""
    inst, _s, _c, _broker_book, _r404, raise_other, _fc = _make_executor(monkeypatch)
    inst._record_position("GOOG", "LONG", 5, 180.00)
    raise_other["exc"] = Exception("500 Internal Server Error")

    inst._reconcile_position_with_broker("GOOG")

    # Local row preserved despite the broker error.
    assert "GOOG" in inst.positions
    assert inst.positions["GOOG"]["qty"] == 5


def test_post_reconcile_grafts_untracked_broker_row(monkeypatch):
    """Broker has the position but local doesn't \u2014 helper grafts it
    with source=POST_RECONCILE so the next reboot stays silent."""
    inst, _s, _c, broker_book, _r404, _rother, _fc = _make_executor(monkeypatch)
    # No local row.
    assert "TSLA" not in inst.positions
    broker_book["TSLA"] = {"qty": -7, "avg_entry_price": 199.99}  # SHORT

    inst._reconcile_position_with_broker("TSLA")

    assert "TSLA" in inst.positions
    assert inst.positions["TSLA"]["qty"] == 7
    assert inst.positions["TSLA"]["side"] == "SHORT"
    assert inst.positions["TSLA"]["source"] == "POST_RECONCILE"


# ---------------------------------------------------------------------------
# _on_signal wiring \u2014 reconcile fires after each ENTRY/EXIT/EOD
# ---------------------------------------------------------------------------


def test_entry_long_triggers_post_reconcile(monkeypatch):
    """ENTRY_LONG must call get_open_position(ticker) after submit so
    self.positions reflects the broker's authoritative qty."""
    inst, _s, _c, broker_book, _r404, _rother, fc = _make_executor(monkeypatch)
    # Pre-seed broker book so the post-reconcile sees the freshly opened pos.
    broker_book["AAPL"] = {"qty": 18, "avg_entry_price": 274.26}

    seen: list = []
    real_get = fc.get_open_position

    def _spy(ticker):
        seen.append(ticker)
        return real_get(ticker)

    fc.get_open_position = _spy

    inst._on_signal(
        {
            "kind": "ENTRY_LONG",
            "ticker": "AAPL",
            "price": 270.00,  # stale local price
            "reason": "ENTRY_1",
            "timestamp_utc": "2026-04-30T15:04:42Z",
            "main_shares": 18,
        }
    )

    assert seen == ["AAPL"], f"post-reconcile must fire on ENTRY_LONG; spy={seen!r}"
    # Entry price was overwritten from broker (274.26), not the stale 270.00.
    assert abs(inst.positions["AAPL"]["entry_price"] - 274.26) < 1e-6


def test_entry_short_triggers_post_reconcile(monkeypatch):
    inst, _s, _c, broker_book, _r404, _rother, fc = _make_executor(monkeypatch)
    broker_book["TSLA"] = {"qty": -10, "avg_entry_price": 200.00}

    seen: list = []
    real_get = fc.get_open_position
    fc.get_open_position = lambda t: (seen.append(t), real_get(t))[1]

    inst._on_signal(
        {
            "kind": "ENTRY_SHORT",
            "ticker": "TSLA",
            "price": 199.50,
            "reason": "ENTRY_1",
            "timestamp_utc": "2026-04-30T15:04:42Z",
            "main_shares": 10,
        }
    )

    assert seen == ["TSLA"]
    assert inst.positions["TSLA"]["side"] == "SHORT"


def test_exit_long_triggers_post_reconcile(monkeypatch):
    """After an EXIT_LONG submit, the helper must run so a partial close
    or 40410000 race leaves self.positions in sync with the broker."""
    inst, _s, _c, _broker_book, _r404, _rother, fc = _make_executor(monkeypatch)
    inst._record_position("AAPL", "LONG", 18, 274.26)

    seen: list = []
    real_get = fc.get_open_position
    fc.get_open_position = lambda t: (seen.append(t), real_get(t))[1]

    inst._on_signal(
        {
            "kind": "EXIT_LONG",
            "ticker": "AAPL",
            "price": 280.00,
            "reason": "STOP",
            "timestamp_utc": "2026-04-30T19:49:59Z",
        }
    )

    assert seen == ["AAPL"], f"post-reconcile must fire on EXIT_LONG; spy={seen!r}"
    # close_position succeeded \u2014 broker_book stays empty \u2014 row dropped.
    assert "AAPL" not in inst.positions


def test_exit_long_post_reconcile_drops_row_on_40410000(monkeypatch):
    """If close_position races a prior flatten and the post-reconcile
    sees 40410000, the local row must still be dropped (idempotent)."""
    inst, _s, _c, broker_book, raise_404, _rother, _fc = _make_executor(monkeypatch)
    inst._record_position("AAPL", "LONG", 18, 274.26)
    # close_position works fine; but the post-reconcile sees 404 \u2014 still drop.
    raise_404.add("AAPL")
    broker_book.pop("AAPL", None)

    inst._on_signal(
        {
            "kind": "EXIT_LONG",
            "ticker": "AAPL",
            "price": 280.00,
            "reason": "STOP",
            "timestamp_utc": "2026-04-30T19:49:59Z",
        }
    )

    assert "AAPL" not in inst.positions


def test_eod_close_all_runs_full_sweep(monkeypatch):
    """EOD_CLOSE_ALL must call close_all_positions then run the boot-
    style full reconcile so any laggard local row gets cleaned up."""
    inst, _s, closes, broker_book, _r404, _rother, fc = _make_executor(monkeypatch)
    # Two local rows; broker has neither (the close_all wiped its book).
    inst._record_position("AAPL", "LONG", 18, 274.26)
    inst._record_position("MSFT", "LONG", 12, 410.00)

    seen_get_all: list = [0]
    real_get_all = fc.get_all_positions

    def _spy_all():
        seen_get_all[0] += 1
        return real_get_all()

    fc.get_all_positions = _spy_all

    inst._on_signal(
        {
            "kind": "EOD_CLOSE_ALL",
            "ticker": "",
            "price": 0.0,
            "reason": "EOD",
            "timestamp_utc": "2026-04-30T20:00:00Z",
        }
    )

    # close_all_positions ran.
    assert any(c == ("ALL", True) for c in closes), f"closes={closes!r}"
    # Local rows wiped by the existing _remove_position loop.
    assert inst.positions == {}
    # Full sweep ran (the v5.25.0 addition) at least once.
    assert seen_get_all[0] >= 1, "EOD must trigger full _reconcile_broker_positions sweep"


# ---------------------------------------------------------------------------
# dashboard_server._executors_status_snapshot \u2014 chip data source
# ---------------------------------------------------------------------------


def test_executors_status_snapshot_both_enabled():
    """Both val_executor and gene_executor present \u2014 both enabled."""
    import dashboard_server as ds

    fake_val = types.SimpleNamespace(mode="paper")
    fake_gene = types.SimpleNamespace(mode="paper")
    m = types.SimpleNamespace(val_executor=fake_val, gene_executor=fake_gene)

    out = ds._executors_status_snapshot(m)
    assert out == {
        "val": {"enabled": True, "mode": "paper"},
        "gene": {"enabled": True, "mode": "paper"},
    }


def test_executors_status_snapshot_gene_disabled():
    """val_executor present, gene_executor None \u2014 only Val enabled."""
    import dashboard_server as ds

    fake_val = types.SimpleNamespace(mode="paper")
    m = types.SimpleNamespace(val_executor=fake_val, gene_executor=None)

    out = ds._executors_status_snapshot(m)
    assert out["val"] == {"enabled": True, "mode": "paper"}
    assert out["gene"] == {"enabled": False, "mode": None}


def test_executors_status_snapshot_attrs_missing():
    """A bare module with no val/gene attrs degrades cleanly to disabled."""
    import dashboard_server as ds

    m = types.SimpleNamespace()
    out = ds._executors_status_snapshot(m)
    assert out == {
        "val": {"enabled": False, "mode": None},
        "gene": {"enabled": False, "mode": None},
    }


def test_snapshot_includes_executors_status_key():
    """The full /api/state snapshot must surface executors_status so the
    dashboard chip-renderer has a stable contract to bind to."""
    import dashboard_server as ds

    snap = ds.snapshot()
    # snapshot() never raises \u2014 it returns either the full dict (ok=True)
    # or {ok: False, error: ...}. In SSM_SMOKE_TEST mode it should succeed.
    assert isinstance(snap, dict)
    if snap.get("ok"):
        assert "executors_status" in snap
        assert "val" in snap["executors_status"]
        assert "gene" in snap["executors_status"]
        for name in ("val", "gene"):
            entry = snap["executors_status"][name]
            assert "enabled" in entry
            assert "mode" in entry
