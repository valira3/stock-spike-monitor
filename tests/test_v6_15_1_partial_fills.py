"""v6.15.1 \u2014 partial-fill handling in the entry path.

When an IOC LIMIT entry partially fills (e.g. requested 24 shares,
filled 18), the position row must be booked at the ACTUAL fill qty,
not the requested qty. Pre-v6.15.1 the executor recorded the request,
so a stop signal moments later sized against phantom shares and
Alpaca returned 40410000 on the missing 6.

Cases covered (10):
  _extract_filled_qty helper (5):
    1. full fill (filled == requested)
    2. partial fill (filled < requested)
    3. zero fill (filled == 0)
    4. missing attribute (legacy mock) \u2192 fall back to requested
    5. broker-bug overfill (filled > requested) \u2192 clamp to requested
    6. negative filled_qty (broker noise) \u2192 clamp to 0
    7. non-numeric filled_qty (str junk) \u2192 fall back to requested
    8. order is None \u2192 fall back to requested

  ENTRY dispatch path (3):
    9.  partial fill on ENTRY_LONG records filled qty, not requested
    10. zero fill on ENTRY_LONG records NO position
    11. partial fill on ENTRY_SHORT records filled qty, not requested
"""
from __future__ import annotations

import sys
import types

import pytest


# ---------------------------------------------------------------------------
# _extract_filled_qty unit tests \u2014 pure helper, no executor needed.
# ---------------------------------------------------------------------------


@pytest.fixture
def base_cls(monkeypatch):
    """Import TradeGeniusBase. The helper is a @staticmethod, so we
    can call it without instantiating an executor.
    """
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase
    return TradeGeniusBase


def test_extract_filled_qty_full_fill(base_cls):
    order = types.SimpleNamespace(id="o1", filled_qty="24")
    assert base_cls._extract_filled_qty(order, 24) == 24


def test_extract_filled_qty_partial(base_cls):
    """The smoking gun case: requested 24, filled 18."""
    order = types.SimpleNamespace(id="o1", filled_qty="18")
    assert base_cls._extract_filled_qty(order, 24) == 18


def test_extract_filled_qty_zero_fill(base_cls):
    """IOC limit unfilled \u2014 ack carries filled_qty=0."""
    order = types.SimpleNamespace(id="o1", filled_qty="0")
    assert base_cls._extract_filled_qty(order, 24) == 0


def test_extract_filled_qty_missing_attr_legacy_mock(base_cls):
    """Legacy unit-test mocks don't set filled_qty. Pre-v6.15.1
    behaviour must be preserved: fall back to requested."""
    order = types.SimpleNamespace(id="o1")
    assert base_cls._extract_filled_qty(order, 24) == 24


def test_extract_filled_qty_overfill_clamps_to_requested(base_cls):
    """Defensive: a broker bug or test mock returning > requested
    must not let us book more shares than we asked for."""
    order = types.SimpleNamespace(id="o1", filled_qty="30")
    assert base_cls._extract_filled_qty(order, 24) == 24


def test_extract_filled_qty_negative_clamps_to_zero(base_cls):
    """Defensive: negative filled_qty \u2014 should never happen, but
    if it does, treat as zero-fill (no row, not a phantom short)."""
    order = types.SimpleNamespace(id="o1", filled_qty="-3")
    assert base_cls._extract_filled_qty(order, 24) == 0


def test_extract_filled_qty_non_numeric_falls_back(base_cls):
    """Garbage string in filled_qty \u2014 fall back to requested
    rather than crash the entry path."""
    order = types.SimpleNamespace(id="o1", filled_qty="abc")
    assert base_cls._extract_filled_qty(order, 24) == 24


def test_extract_filled_qty_none_order_falls_back(base_cls):
    """If something upstream returns None for the order ack, fall
    back to requested (legacy behaviour)."""
    assert base_cls._extract_filled_qty(None, 24) == 24


# ---------------------------------------------------------------------------
# ENTRY dispatch tests \u2014 drive _on_signal end-to-end with a fake
# Alpaca client whose submit_order returns an ack with filled_qty.
# ---------------------------------------------------------------------------


def _make_executor(monkeypatch, filled_qty: str | None):
    """Minimal stub executor whose submit_order returns an ack with
    the given filled_qty (None = legacy mock with no filled_qty attr).
    """
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub615"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst.dollars_per_entry = 10000.0

    submits: list = []
    telegrams: list = []

    class _FakeAcct:
        equity = 200_000.0
        cash = 200_000.0
        buying_power = 400_000.0

    class _FakeClient:
        def get_account(self):
            return _FakeAcct()

        def submit_order(self, req):
            submits.append(req)
            ack = types.SimpleNamespace(id="fake-order-id")
            if filled_qty is not None:
                ack.filled_qty = filled_qty
            return ack

        # Reconcile path \u2014 just say flat so we don't graft anything.
        def get_open_position(self, ticker):
            raise Exception('{"code":40410000,"message":"position not found"}')

    fake_client = _FakeClient()
    inst._ensure_client = lambda: fake_client  # type: ignore
    inst._send_own_telegram = lambda msg: telegrams.append(msg)  # type: ignore
    inst._submit_order_idempotent = lambda client, req, coid: client.submit_order(req)  # type: ignore
    inst._build_client_order_id = lambda ticker, side: f"{ticker}-{side}-coid"  # type: ignore
    inst._persist_position = lambda _t: None  # type: ignore
    inst._delete_persisted_position = lambda _t: None  # type: ignore
    # Skip the open-PnL snapshot (needs a real broker client).
    inst._last_open_pnl_ts = float("inf")  # type: ignore
    return inst, submits, telegrams


def test_entry_long_partial_fill_records_filled_qty(monkeypatch):
    """The smoking-gun case end-to-end. Request 24, IOC fills 18.
    Local position row must be qty=18, not 24."""
    inst, submits, _tg = _make_executor(monkeypatch, filled_qty="18")
    inst._on_signal(
        {
            "kind": "ENTRY_LONG",
            "ticker": "MSFT",
            "price": 415.40,
            "reason": "ENTRY_1",
            "timestamp_utc": "2026-05-05T15:04:42Z",
            "main_shares": 24,
        }
    )
    assert len(submits) == 1
    assert submits[0].qty == 24, "the SUBMIT must request what the signal said"
    assert "MSFT" in inst.positions
    assert inst.positions["MSFT"]["qty"] == 18, (
        f"position must be booked at the FILL (18), not the request (24); "
        f"got {inst.positions['MSFT']['qty']}"
    )
    assert inst.positions["MSFT"]["side"] == "LONG"


def test_entry_long_zero_fill_records_no_position(monkeypatch):
    """IOC LIMIT priced through the book \u2014 zero fill. No row written,
    no orphan stop, but a warning telegram must fire so Val sees it."""
    inst, submits, telegrams = _make_executor(monkeypatch, filled_qty="0")
    inst._on_signal(
        {
            "kind": "ENTRY_LONG",
            "ticker": "AAPL",
            "price": 274.26,
            "reason": "ENTRY_1",
            "timestamp_utc": "2026-05-05T15:04:42Z",
            "main_shares": 18,
        }
    )
    assert len(submits) == 1, "submit still happens \u2014 we don't know the fill until after"
    assert "AAPL" not in inst.positions, "zero-fill must NOT leave a phantom row"
    # Warning telegram fired so Val sees the unfilled IOC.
    assert any("unfilled" in m or "no position recorded" in m for m in telegrams), (
        f"expected an unfilled-warning telegram; got {telegrams!r}"
    )


def test_entry_short_partial_fill_records_filled_qty(monkeypatch):
    """Same partial-fill semantics on the short side \u2014 request 30,
    fill 22, book 22."""
    inst, submits, _tg = _make_executor(monkeypatch, filled_qty="22")
    inst._on_signal(
        {
            "kind": "ENTRY_SHORT",
            "ticker": "TSLA",
            "price": 250.00,
            "reason": "ENTRY_1_SHORT",
            "timestamp_utc": "2026-05-05T15:04:42Z",
            "main_shares": 30,
        }
    )
    assert len(submits) == 1
    assert submits[0].qty == 30
    assert "TSLA" in inst.positions
    assert inst.positions["TSLA"]["qty"] == 22, (
        f"short position must be booked at the FILL (22), not the request (30); "
        f"got {inst.positions['TSLA']['qty']}"
    )
    assert inst.positions["TSLA"]["side"] == "SHORT"
