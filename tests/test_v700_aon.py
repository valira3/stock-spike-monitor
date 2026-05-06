"""v7.0.0 Phase 5 \u2014 AON (All-or-Nothing) entry tests.

Spec C: every IOC LIMIT entry becomes all-or-nothing. Two paths:
  - Native: SDK accepts all_or_none=True at construction time.
  - Software: SDK rejects with TypeError; use soft partial detection.

Software AON partial-fill behavior (Val 2026-05-06 revision):
  Keep the partial alive, emit a single \u26a0\ufe0f Telegram, and let normal
  sentinels manage the position. Do NOT force-flatten with a MARKET order.

Tests (10):
  1. _probe_aon_support returns "native" when SDK accepts all_or_none
  2. _probe_aon_support returns "software" when SDK raises TypeError
  3. _probe_aon_support returns "software" on any other exception
  4. Native mode: _build_entry_request injects all_or_none=True kwarg
  5. Software mode: _build_entry_request does NOT inject all_or_none
  6. Software + partial fill: single \u26a0\ufe0f Telegram with "keeping partial"
  7. Software + partial fill: _record_position called with filled_qty (not qty)
  8. Software + partial fill: NO extra order submission (only original LIMIT)
  9. Software + full fill: normal \u2705 path, no software-AON Telegram
  10. Software + zero fill: routes through ZEROFILL followup, not AON path
  11. Boot log: [V700-AON] logged via logger.info in start()
"""
from __future__ import annotations

import os
import sys
import types
import logging

import pytest

# Minimal env so trade_genius imports cleanly in the test harness.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "0000000000:AAAA_smoke_placeholder_token_0000000")
os.environ.setdefault("CHAT_ID", "999999999")
os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
os.environ.setdefault("FMP_API_KEY", "fake_fmp_key_for_tests")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_cls(monkeypatch):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase
    return TradeGeniusBase


def _make_executor(monkeypatch, *, filled_qty: str, aon_mode: str = "software"):
    """Return a minimal executor stub wired for entry-dispatch tests."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub700AON"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst.dollars_per_entry = 10000.0
    # Inject aon_mode directly (bypasses start() / network probe).
    inst._aon_mode = aon_mode

    submits: list = []
    telegrams: list = []
    recorded: list = []

    class _FakeAcct:
        equity = 200_000.0
        cash = 200_000.0
        buying_power = 400_000.0

    class _FakeClient:
        def get_account(self):
            return _FakeAcct()

        def submit_order(self, req):
            submits.append(req)
            ack = types.SimpleNamespace(id="test-oid-700")
            ack.filled_qty = filled_qty
            return ack

        def get_open_position(self, ticker):
            raise Exception('{"code":40410000,"message":"position not found"}')

    fake_client = _FakeClient()
    inst._ensure_client = lambda: fake_client  # type: ignore
    inst._send_own_telegram = lambda msg: telegrams.append(msg)  # type: ignore
    inst._submit_order_idempotent = lambda client, req, coid: client.submit_order(req)  # type: ignore
    inst._build_client_order_id = lambda ticker, side: f"{ticker}-{side}-coid"  # type: ignore
    inst._persist_position = lambda _t: None  # type: ignore
    inst._delete_persisted_position = lambda _t: None  # type: ignore
    # Track _record_position calls
    _orig_record = inst._record_position.__func__

    def _fake_record(self_inner, ticker, side, qty, price):
        recorded.append({"ticker": ticker, "side": side, "qty": qty, "price": price})
        _orig_record(self_inner, ticker, side, qty, price)

    inst._record_position = lambda ticker, side, qty, price: _fake_record(  # type: ignore
        inst, ticker, side, qty, price
    )
    inst._last_open_pnl_ts = float("inf")  # type: ignore
    return inst, submits, telegrams, recorded


# ---------------------------------------------------------------------------
# 1\u20133. _probe_aon_support unit tests
# ---------------------------------------------------------------------------


def test_probe_returns_native_when_sdk_accepts(base_cls, monkeypatch):
    """SDK accepts all_or_none=True without raising \u2192 mode=native."""
    inst = object.__new__(base_cls)
    result = inst._probe_aon_support()
    # Current alpaca-py 0.43.2 does not raise TypeError on extra kwargs.
    assert result in ("native", "software"), f"unexpected: {result!r}"
    # Document the actual finding: 0.43.2 silently accepts the kwarg.
    # The probe correctly returns native (no TypeError raised).
    assert result == "native", (
        "alpaca-py 0.43.2 silently accepts all_or_none=True without TypeError; "
        "probe should return 'native' per spec"
    )


def test_probe_returns_software_on_type_error(base_cls, monkeypatch):
    """If SDK raises TypeError for all_or_none kwarg \u2192 mode=software."""
    import executors.base as base_mod

    # Patch LimitOrderRequest inside the probe so it raises TypeError.
    class _StrictLimitOrderRequest:
        def __init__(self, **kwargs):
            if "all_or_none" in kwargs:
                raise TypeError("unexpected keyword argument 'all_or_none'")

    monkeypatch.setattr(
        "alpaca.trading.requests.LimitOrderRequest",
        _StrictLimitOrderRequest,
        raising=False,
    )
    inst = object.__new__(base_cls)
    result = inst._probe_aon_support()
    assert result == "software"


def test_probe_returns_software_on_any_exception(base_cls, monkeypatch):
    """Any non-TypeError exception from the probe \u2192 mode=software (safety)."""
    import alpaca.trading.requests as atr

    orig = atr.LimitOrderRequest

    class _BrokenLimitOrderRequest:
        def __init__(self, **kwargs):
            raise RuntimeError("network blip during import")

    monkeypatch.setattr(atr, "LimitOrderRequest", _BrokenLimitOrderRequest)
    inst = object.__new__(base_cls)
    result = inst._probe_aon_support()
    assert result == "software"
    monkeypatch.setattr(atr, "LimitOrderRequest", orig)


# ---------------------------------------------------------------------------
# 4\u20135. _build_entry_request AON kwarg injection
# ---------------------------------------------------------------------------


def test_native_mode_limit_request_has_all_or_none(monkeypatch):
    """In native mode, the LimitOrderRequest should be built with
    all_or_none=True injected via **_limit_kwargs."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase

    # Intercept LimitOrderRequest construction to capture kwargs.
    captured_kwargs: list = []
    import alpaca.trading.requests as atr
    OrigLOR = atr.LimitOrderRequest

    class _CapturingLOR(OrigLOR):
        def __init__(self, **kwargs):
            captured_kwargs.append(dict(kwargs))
            super().__init__(**{k: v for k, v in kwargs.items() if k != "all_or_none"})

    monkeypatch.setattr(atr, "LimitOrderRequest", _CapturingLOR)

    class _StubExec(TradeGeniusBase):
        NAME = "TestNative700"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst._aon_mode = "native"
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
            ack = types.SimpleNamespace(id="native-oid")
            ack.filled_qty = "5"
            return ack

        def get_open_position(self, ticker):
            import types as t
            pos = t.SimpleNamespace(qty="5", side="long", avg_entry_price="100.0",
                                    current_price="100.0", unrealized_pl="0.0",
                                    market_value="500.0")
            return pos

    fake_client = _FakeClient()
    inst._ensure_client = lambda: fake_client  # type: ignore
    inst._send_own_telegram = lambda msg: telegrams.append(msg)  # type: ignore
    inst._submit_order_idempotent = lambda client, req, coid: client.submit_order(req)  # type: ignore
    inst._build_client_order_id = lambda ticker, side: f"{ticker}-{side}-coid"  # type: ignore
    inst._persist_position = lambda _t: None  # type: ignore
    inst._delete_persisted_position = lambda _t: None  # type: ignore
    inst._last_open_pnl_ts = float("inf")  # type: ignore

    # We need to mock the quote snapshot so _build_entry_request goes LIMIT path.
    tg_mod = types.ModuleType("trade_genius")
    tg_mod.BOT_VERSION = "7.0.0"  # type: ignore
    tg_mod.TRADEGENIUS_OWNER_IDS = set()  # type: ignore
    tg_mod.register_signal_listener = lambda _: None  # type: ignore
    tg_mod._v512_quote_snapshot = lambda ticker: (99.90, 100.10)  # type: ignore
    tg_mod._utc_now_iso = lambda: "2026-05-06T15:00:00Z"  # type: ignore
    sys.modules["trade_genius"] = tg_mod

    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "SPY",
        "price": 100.0,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 5,
    })

    # Filter to only entry LimitOrderRequests (not the probe one).
    entry_kwargs = [k for k in captured_kwargs if k.get("symbol") == "SPY" and k.get("qty") == 5]
    assert entry_kwargs, f"no entry LimitOrderRequest captured; all captures: {captured_kwargs}"
    assert entry_kwargs[-1].get("all_or_none") is True, (
        f"native mode must inject all_or_none=True; got {entry_kwargs[-1]}"
    )


def test_software_mode_limit_request_no_all_or_none(monkeypatch):
    """In software mode, LimitOrderRequest must NOT receive all_or_none."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase

    captured_kwargs: list = []
    import alpaca.trading.requests as atr
    OrigLOR = atr.LimitOrderRequest

    class _CapturingLOR(OrigLOR):
        def __init__(self, **kwargs):
            captured_kwargs.append(dict(kwargs))
            super().__init__(**{k: v for k, v in kwargs.items() if k != "all_or_none"})

    monkeypatch.setattr(atr, "LimitOrderRequest", _CapturingLOR)

    class _StubExec(TradeGeniusBase):
        NAME = "TestSoftware700"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst._aon_mode = "software"
    inst.dollars_per_entry = 10000.0

    class _FakeAcct:
        equity = 200_000.0
        cash = 200_000.0
        buying_power = 400_000.0

    class _FakeClient:
        def get_account(self):
            return _FakeAcct()

        def submit_order(self, req):
            ack = types.SimpleNamespace(id="sw-oid")
            ack.filled_qty = "5"
            return ack

        def get_open_position(self, ticker):
            import types as t
            return t.SimpleNamespace(qty="5", side="long", avg_entry_price="100.0",
                                     current_price="100.0", unrealized_pl="0.0",
                                     market_value="500.0")

    fake_client = _FakeClient()
    inst._ensure_client = lambda: fake_client  # type: ignore
    inst._send_own_telegram = lambda msg: None  # type: ignore
    inst._submit_order_idempotent = lambda client, req, coid: client.submit_order(req)  # type: ignore
    inst._build_client_order_id = lambda ticker, side: f"{ticker}-{side}-coid"  # type: ignore
    inst._persist_position = lambda _t: None  # type: ignore
    inst._delete_persisted_position = lambda _t: None  # type: ignore
    inst._last_open_pnl_ts = float("inf")  # type: ignore

    tg_mod = types.ModuleType("trade_genius")
    tg_mod.BOT_VERSION = "7.0.0"  # type: ignore
    tg_mod.TRADEGENIUS_OWNER_IDS = set()  # type: ignore
    tg_mod.register_signal_listener = lambda _: None  # type: ignore
    tg_mod._v512_quote_snapshot = lambda ticker: (99.90, 100.10)  # type: ignore
    tg_mod._utc_now_iso = lambda: "2026-05-06T15:00:00Z"  # type: ignore
    sys.modules["trade_genius"] = tg_mod

    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "SPY",
        "price": 100.0,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 5,
    })

    entry_kwargs = [k for k in captured_kwargs if k.get("symbol") == "SPY" and k.get("qty") == 5]
    assert entry_kwargs, "no entry LimitOrderRequest captured"
    assert "all_or_none" not in entry_kwargs[-1], (
        f"software mode must NOT inject all_or_none; got {entry_kwargs[-1]}"
    )


# ---------------------------------------------------------------------------
# 6\u20138. Software AON partial fill \u2014 keep partial, no market close
# ---------------------------------------------------------------------------


def test_software_partial_emits_single_telegram_keeping_partial(monkeypatch):
    """Software AON + partial fill \u2192 exactly ONE \u26a0\ufe0f 'keeping partial' Telegram."""
    inst, submits, telegrams, recorded = _make_executor(
        monkeypatch, filled_qty="6", aon_mode="software"
    )
    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "AAPL",
        "price": 285.00,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 34,
    })
    assert len(telegrams) == 1, f"expected exactly 1 telegram; got {telegrams!r}"
    msg = telegrams[0]
    assert "partial" in msg.lower(), f"expected 'partial' in msg: {msg!r}"
    assert "6" in msg and "34" in msg, f"fill counts missing: {msg!r}"
    assert "keeping partial" in msg.lower() or "sentinels engaged" in msg.lower(), (
        f"expected 'keeping partial'/'sentinels engaged' in msg: {msg!r}"
    )
    assert "\u26a0" in msg, f"expected \u26a0\ufe0f glyph in msg: {msg!r}"


def test_software_partial_records_position_at_filled_qty(monkeypatch):
    """Software AON + partial fill \u2192 position booked at filled_qty (6), not requested (34)."""
    inst, submits, telegrams, recorded = _make_executor(
        monkeypatch, filled_qty="6", aon_mode="software"
    )
    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "AAPL",
        "price": 285.00,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 34,
    })
    assert "AAPL" in inst.positions, "partial fill must be recorded as a position"
    assert inst.positions["AAPL"]["qty"] == 6, (
        f"position must be booked at fill qty=6, not request qty=34; "
        f"got {inst.positions['AAPL']['qty']}"
    )


def test_software_partial_no_extra_order_submission(monkeypatch):
    """Software AON + partial fill \u2192 only ONE order submitted (the original LIMIT).
    No MARKET close-to-flat order per Val 2026-05-06 revision."""
    inst, submits, telegrams, recorded = _make_executor(
        monkeypatch, filled_qty="6", aon_mode="software"
    )
    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "AAPL",
        "price": 285.00,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 34,
    })
    assert len(submits) == 1, (
        f"expected exactly 1 order submission (the entry LIMIT); got {len(submits)}: {submits!r}"
    )


# ---------------------------------------------------------------------------
# 9. Software + full fill \u2014 normal \u2705 path, no AON telegram
# ---------------------------------------------------------------------------


def test_software_full_fill_no_aon_telegram(monkeypatch):
    """Full fill in software mode \u2192 normal \u2705 Telegram, no \u26a0\ufe0f AON message."""
    inst, submits, telegrams, recorded = _make_executor(
        monkeypatch, filled_qty="34", aon_mode="software"
    )
    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "MSFT",
        "price": 415.00,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 34,
    })
    assert len(telegrams) == 1, f"expected exactly 1 telegram; got {telegrams!r}"
    msg = telegrams[0]
    assert "\u2705" in msg, f"full fill must produce \u2705 message; got {msg!r}"
    assert "partial" not in msg.lower(), f"no 'partial' in full-fill message; got {msg!r}"
    assert "keeping" not in msg.lower(), f"no 'keeping' in full-fill message; got {msg!r}"


# ---------------------------------------------------------------------------
# 10. Software + zero fill \u2014 ZEROFILL followup path, not AON path
# ---------------------------------------------------------------------------


def test_software_zero_fill_routes_to_zerofill_not_aon(monkeypatch):
    """Zero fill in software mode \u2192 ZEROFILL followup path (no position), not AON."""
    inst, submits, telegrams, recorded = _make_executor(
        monkeypatch, filled_qty="0", aon_mode="software"
    )
    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "TSLA",
        "price": 200.00,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 10,
    })
    # Zero fill \u2192 ZEROFILL path emits ONE followup telegram (not an AON telegram)
    assert len(telegrams) == 1, f"expected exactly 1 followup telegram; got {telegrams!r}"
    msg = telegrams[0]
    # Should be the ZEROFILL followup (rejected / inconclusive / late fill) not AON keeping
    assert "keeping partial" not in msg.lower(), (
        f"zero fill must not produce AON 'keeping partial' msg; got {msg!r}"
    )
    # No position recorded on zero fill
    assert "TSLA" not in inst.positions, "zero fill must NOT leave a phantom row"


# ---------------------------------------------------------------------------
# 11. Boot log \u2014 [V700-AON] logged in start()
# ---------------------------------------------------------------------------


def test_start_logs_v700_aon_line(monkeypatch, caplog):
    """start() must log exactly one [V700-AON] line per executor."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_secret")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]

    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase

    class _StubExec(TradeGeniusBase):
        NAME = "TestBoot700"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()

    # Wire up minimal start() dependencies.
    registered = []
    tg_mod = sys.modules.get("trade_genius") or sys.modules.get("__main__")
    original_register = getattr(tg_mod, "register_signal_listener", None)
    tg_mod.register_signal_listener = lambda fn: registered.append(fn)  # type: ignore

    class _FakeAcct:
        equity = 50_000.0
        cash = 50_000.0
        buying_power = 100_000.0

    class _FakeClient:
        def get_account(self):
            return _FakeAcct()

    fake_client = _FakeClient()
    inst._ensure_client = lambda: fake_client  # type: ignore
    inst._reconcile_broker_positions = lambda: None  # type: ignore
    inst._run_tg_loop = lambda: None  # type: ignore

    import threading
    monkeypatch.setattr(threading, "Thread", lambda target, daemon, name: types.SimpleNamespace(start=lambda: None))

    with caplog.at_level(logging.INFO):
        inst.start()

    aon_logs = [r for r in caplog.records if "V700-AON" in r.getMessage()]
    assert len(aon_logs) >= 1, f"expected [V700-AON] log in start(); got records: {[r.getMessage() for r in caplog.records]}"
    aon_msg = aon_logs[0].getMessage()
    assert "mode=" in aon_msg, f"[V700-AON] log must include mode=; got {aon_msg!r}"
    assert "native" in aon_msg or "software" in aon_msg, (
        f"[V700-AON] log must say mode=native or mode=software; got {aon_msg!r}"
    )

    if original_register is not None:
        tg_mod.register_signal_listener = original_register  # type: ignore
