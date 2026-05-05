"""v6.15.2 \u2014 TIF-aware fill extraction, zero-fill reconcile, and
quote-snapshot hardening with a synthetic-spread fallback.

Triggered by the AAPL 2026-05-05 incident: at 17:19:58 UTC,
``_v512_quote_snapshot(AAPL)`` returned ``(None, None)`` silently,
``_build_entry_request`` fell to a MARKET DAY order, the synchronous
ack returned ``filled_qty=0`` (pending_new state, NOT terminal), and
v6.15.1's helper aborted with "no position recorded" \u2014 even though
the MARKET order filled milliseconds later on the broker book.

Three fixes are validated here:

  Fix A: ``_extract_filled_qty`` is TIF-aware. Only IOC zero-fills are
         treated as terminal; MARKET / DAY / GTC zeros fall back to
         requested qty so the post-action reconcile can sync from
         broker truth.

  Fix B: When IOC truly returns filled_qty=0, the entry path now runs
         ``_reconcile_position_with_broker(expect="present")`` BEFORE
         returning, so a late-grafted Alpaca-side fill still ends up
         with a local row.

  Fix C: ``_v512_quote_snapshot`` logs every failure mode with a
         ``[V6152-QUOTE]`` tag, retries once on exception, and returns
         (None, None) only after both retries fail. A new
         ``_v512_synthetic_quote(ticker, anchor_price)`` returns a
         5bps spread that ``_build_entry_request`` uses to build an
         IOC LIMIT instead of falling all the way to MARKET when the
         real quote is null.
"""
from __future__ import annotations

import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Fix A \u2014 _is_ioc_request + TIF-aware _extract_filled_qty.
# ---------------------------------------------------------------------------


@pytest.fixture
def base_cls(monkeypatch):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase
    return TradeGeniusBase


def test_is_ioc_request_string_ioc(base_cls):
    req = types.SimpleNamespace(time_in_force="IOC")
    assert base_cls._is_ioc_request(req) is True


def test_is_ioc_request_lowercase_ioc(base_cls):
    req = types.SimpleNamespace(time_in_force="ioc")
    assert base_cls._is_ioc_request(req) is True


def test_is_ioc_request_enum_value_ioc(base_cls):
    """alpaca-py exposes TimeInForce.IOC as an enum with .value='ioc'."""
    enum_like = types.SimpleNamespace(value="ioc")
    req = types.SimpleNamespace(time_in_force=enum_like)
    assert base_cls._is_ioc_request(req) is True


def test_is_ioc_request_day(base_cls):
    req = types.SimpleNamespace(time_in_force="DAY")
    assert base_cls._is_ioc_request(req) is False


def test_is_ioc_request_none_req(base_cls):
    assert base_cls._is_ioc_request(None) is False


def test_is_ioc_request_no_attr(base_cls):
    """If the request object has no time_in_force attribute, default
    to False so legacy callers stay on the v6.15.1 'trust request' path."""
    req = types.SimpleNamespace(symbol="AAPL")
    assert base_cls._is_ioc_request(req) is False


def test_extract_filled_qty_zero_with_ioc_is_terminal(base_cls):
    """IOC + filled_qty=0 \u2192 0 (true unfilled)."""
    order = types.SimpleNamespace(id="o1", filled_qty="0")
    ioc_req = types.SimpleNamespace(time_in_force="IOC")
    assert base_cls._extract_filled_qty(order, 24, req=ioc_req) == 0


def test_extract_filled_qty_zero_with_day_falls_back(base_cls):
    """DAY + filled_qty=0 \u2192 fall back to requested. The MARKET ack
    is pending_new; trusting 0 would silently drop a live order
    (the AAPL incident pattern)."""
    order = types.SimpleNamespace(id="o1", filled_qty="0")
    day_req = types.SimpleNamespace(time_in_force="DAY")
    assert base_cls._extract_filled_qty(order, 24, req=day_req) == 24


def test_extract_filled_qty_zero_no_req_falls_back(base_cls):
    """No req at all \u2192 fall back. We can't prove it was IOC, so
    don't drop the order from local tracking."""
    order = types.SimpleNamespace(id="o1", filled_qty="0")
    assert base_cls._extract_filled_qty(order, 24) == 24


def test_extract_filled_qty_partial_with_ioc_unchanged(base_cls):
    """Partial-fill semantics from v6.15.1 must be preserved when req is IOC."""
    order = types.SimpleNamespace(id="o1", filled_qty="18")
    ioc_req = types.SimpleNamespace(time_in_force="IOC")
    assert base_cls._extract_filled_qty(order, 24, req=ioc_req) == 18


def test_extract_filled_qty_partial_with_day_unchanged(base_cls):
    """Even on a DAY order, a non-zero partial fill is still booked
    as that partial. Only zero is special-cased."""
    order = types.SimpleNamespace(id="o1", filled_qty="18")
    day_req = types.SimpleNamespace(time_in_force="DAY")
    assert base_cls._extract_filled_qty(order, 24, req=day_req) == 18


# ---------------------------------------------------------------------------
# Fix C \u2014 _v512_synthetic_quote helper.
# ---------------------------------------------------------------------------


@pytest.fixture
def tg_module(monkeypatch):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius
    return trade_genius


def test_synthetic_quote_basic_5bps_spread(tg_module):
    """5 bps each side off $283.88 anchor \u2014 actual AAPL incident price."""
    bid, ask = tg_module._v512_synthetic_quote("AAPL", 283.88)
    assert bid is not None and ask is not None
    assert bid < 283.88 < ask
    # 5 bps half-width = 0.05% of 283.88 = 0.142
    assert abs((283.88 - bid) - 0.14194) < 0.001
    assert abs((ask - 283.88) - 0.14194) < 0.001
    # Sanity: spread is symmetric and bid < ask.
    assert bid < ask


def test_synthetic_quote_zero_anchor_returns_none(tg_module):
    bid, ask = tg_module._v512_synthetic_quote("AAPL", 0.0)
    assert (bid, ask) == (None, None)


def test_synthetic_quote_negative_anchor_returns_none(tg_module):
    bid, ask = tg_module._v512_synthetic_quote("AAPL", -1.0)
    assert (bid, ask) == (None, None)


def test_synthetic_quote_non_numeric_returns_none(tg_module):
    bid, ask = tg_module._v512_synthetic_quote("AAPL", "not-a-number")  # type: ignore
    assert (bid, ask) == (None, None)


# ---------------------------------------------------------------------------
# Fix C \u2014 _v512_quote_snapshot logs failure modes & retries.
# ---------------------------------------------------------------------------


def test_quote_snapshot_logs_client_unavailable(tg_module, caplog):
    """No data client \u2192 [V6152-QUOTE] client_unavailable WARN."""
    # _historical_data_client returning None already simulated by the
    # test environment (no real Alpaca credentials in SMOKE mode).
    caplog.set_level("WARNING")
    bid, ask = tg_module._v512_quote_snapshot("AAPL")
    assert (bid, ask) == (None, None)
    # In SMOKE mode the historical data client returns None, so we
    # should see the client_unavailable warn.
    assert any(
        "[V6152-QUOTE]" in r.getMessage() and "client_unavailable" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_quote_snapshot_retries_on_api_error(tg_module, monkeypatch, caplog):
    """First call raises, second succeeds \u2192 we get the second result
    back AND a [V6152-QUOTE] api_error attempt=1 warn was logged."""
    caplog.set_level("WARNING")

    class _FlakyClient:
        def __init__(self):
            self.calls = 0

        def get_stock_latest_quote(self, _req):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient 502")
            return {"AAPL": types.SimpleNamespace(bid_price=283.74, ask_price=284.02)}

    flaky = _FlakyClient()
    monkeypatch.setattr(tg_module, "_historical_data_client", lambda: flaky, raising=False)

    bid, ask = tg_module._v512_quote_snapshot("AAPL")
    assert flaky.calls == 2, "must have retried once after the 502"
    assert bid == 283.74 and ask == 284.02
    assert any("api_error attempt=1" in r.getMessage() for r in caplog.records)


def test_quote_snapshot_logs_no_record(tg_module, monkeypatch, caplog):
    """Client returns dict but ticker missing \u2192 no_record warn."""
    caplog.set_level("WARNING")

    class _EmptyClient:
        def get_stock_latest_quote(self, _req):
            return {}

    monkeypatch.setattr(tg_module, "_historical_data_client", lambda: _EmptyClient(), raising=False)
    bid, ask = tg_module._v512_quote_snapshot("AAPL")
    assert (bid, ask) == (None, None)
    assert any("no_record" in r.getMessage() for r in caplog.records)


def test_quote_snapshot_logs_non_positive(tg_module, monkeypatch, caplog):
    """bid=0 or ask=0 \u2192 non_positive warn, returns (None, None)."""
    caplog.set_level("WARNING")

    class _ZeroClient:
        def get_stock_latest_quote(self, _req):
            return {"AAPL": types.SimpleNamespace(bid_price=0.0, ask_price=283.50)}

    monkeypatch.setattr(tg_module, "_historical_data_client", lambda: _ZeroClient(), raising=False)
    bid, ask = tg_module._v512_quote_snapshot("AAPL")
    assert (bid, ask) == (None, None)
    assert any("non_positive" in r.getMessage() for r in caplog.records)


def test_quote_snapshot_happy_path(tg_module, monkeypatch):
    """Real bid/ask \u2192 returns them, no warn."""

    class _GoodClient:
        def get_stock_latest_quote(self, _req):
            return {"AAPL": types.SimpleNamespace(bid_price=283.74, ask_price=284.02)}

    monkeypatch.setattr(tg_module, "_historical_data_client", lambda: _GoodClient(), raising=False)
    bid, ask = tg_module._v512_quote_snapshot("AAPL")
    assert bid == 283.74 and ask == 284.02


# ---------------------------------------------------------------------------
# End-to-end: ENTRY path uses synthetic quote when real quote is null,
# and zero-fill IOC entries trigger the post-action reconcile (Fix B).
# ---------------------------------------------------------------------------


def _make_executor(monkeypatch, *, filled_qty: str | None, broker_position=None):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub6152"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst.dollars_per_entry = 10000.0
    submits: list = []
    telegrams: list = []
    reconcile_calls: list = []

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

        def get_open_position(self, ticker):
            if broker_position is None:
                raise Exception('{"code":40410000,"message":"position not found"}')
            return broker_position

    fake_client = _FakeClient()
    inst._ensure_client = lambda: fake_client  # type: ignore
    inst._send_own_telegram = lambda msg: telegrams.append(msg)  # type: ignore
    inst._submit_order_idempotent = lambda client, req, coid: client.submit_order(req)  # type: ignore
    inst._build_client_order_id = lambda ticker, side: f"{ticker}-{side}-coid"  # type: ignore
    inst._persist_position = lambda _t: None  # type: ignore
    inst._delete_persisted_position = lambda _t: None  # type: ignore
    inst._last_open_pnl_ts = float("inf")  # type: ignore

    # Spy on reconcile so we can assert Fix B fired.
    real_reconcile = inst._reconcile_position_with_broker

    def _spy(ticker, expect):
        reconcile_calls.append((ticker, expect))
        return real_reconcile(ticker, expect)

    inst._reconcile_position_with_broker = _spy  # type: ignore
    return inst, submits, telegrams, reconcile_calls


def test_entry_long_uses_synthetic_quote_when_real_quote_null(monkeypatch):
    """Fix C end-to-end. Real quote null \u2192 synthetic IOC LIMIT, NOT MARKET DAY."""
    inst, submits, _tg, _rc = _make_executor(monkeypatch, filled_qty="24")
    inst._on_signal(
        {
            "kind": "ENTRY_LONG",
            "ticker": "AAPL",
            "price": 283.88,
            "reason": "ENTRY_1",
            "timestamp_utc": "2026-05-05T17:19:58Z",
            "main_shares": 24,
        }
    )
    assert len(submits) == 1
    req = submits[0]
    # Synthetic quote should produce an IOC LIMIT, not a MARKET DAY.
    assert hasattr(req, "limit_price"), (
        f"expected IOC LIMIT via synthetic quote, got {type(req).__name__} "
        f"({req!r})"
    )
    # IOC, not DAY.
    tif = getattr(req, "time_in_force", None)
    tif_val = getattr(tif, "value", None) or str(tif)
    assert str(tif_val).lower().endswith("ioc"), (
        f"expected IOC, got {tif_val!r}"
    )
    # Position booked at full fill.
    assert inst.positions["AAPL"]["qty"] == 24


def test_entry_long_zero_fill_ioc_runs_reconcile(monkeypatch):
    """Fix B end-to-end. IOC zero-fill \u2192 reconcile is called even
    though the local row stays empty when the broker is also flat."""
    inst, submits, telegrams, reconcile_calls = _make_executor(
        monkeypatch, filled_qty="0", broker_position=None,
    )
    inst._on_signal(
        {
            "kind": "ENTRY_LONG",
            "ticker": "AAPL",
            "price": 283.88,
            "reason": "ENTRY_1",
            "timestamp_utc": "2026-05-05T17:19:58Z",
            "main_shares": 24,
        }
    )
    assert len(submits) == 1
    # Reconcile MUST have been called \u2014 this is the Fix B contract.
    assert ("AAPL", "present") in reconcile_calls, (
        f"expected post-action reconcile on AAPL after zero-fill IOC; "
        f"got {reconcile_calls!r}"
    )
    # Broker is also flat (40410000), so no row gets grafted.
    assert "AAPL" not in inst.positions
    # Telegram still fires so Val sees the unfilled IOC.
    assert any("unfilled" in m for m in telegrams), telegrams


def test_entry_long_zero_fill_market_does_not_abort(monkeypatch):
    """Fix A end-to-end. If the order somehow was a MARKET (DAY) and
    the synchronous ack returned filled_qty=0 (pending_new), we must
    NOT abort with no-position. Instead we book the requested qty,
    trusting the post-action reconcile to sync to broker truth."""
    inst, submits, _tg, _rc = _make_executor(monkeypatch, filled_qty="0")

    # Force the MARKET-DAY path by killing both the real and synthetic
    # quote sources.
    import trade_genius as tg
    monkeypatch.setattr(tg, "_v512_quote_snapshot", lambda _t: (None, None))
    monkeypatch.setattr(tg, "_v512_synthetic_quote", lambda _t, _p: (None, None))

    inst._on_signal(
        {
            "kind": "ENTRY_LONG",
            "ticker": "MSFT",
            "price": 415.40,
            "reason": "ENTRY_1",
            "timestamp_utc": "2026-05-05T15:04:42Z",
            "main_shares": 12,
        }
    )
    assert len(submits) == 1
    req = submits[0]
    # Confirm we ended up on the MARKET DAY path.
    assert not hasattr(req, "limit_price")
    tif = getattr(req, "time_in_force", None)
    tif_val = getattr(tif, "value", None) or str(tif)
    assert str(tif_val).lower().endswith("day")
    # MARKET zero-fill must NOT abort \u2014 row was booked at requested qty,
    # post-action reconcile will sync it to broker truth a beat later.
    assert "MSFT" in inst.positions, (
        "MARKET DAY zero-fill must fall back to requested qty, not abort"
    )
    assert inst.positions["MSFT"]["qty"] == 12
