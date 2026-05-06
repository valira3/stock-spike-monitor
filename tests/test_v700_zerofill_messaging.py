"""v7.0.0 Phase 5 \u2014 Quiet ZEROFILL messaging tests.

Spec D: collapse the v6.15.6 two-ping pattern (initial \u26a0\ufe0f "unfilled,
reconciling..." + follow-up outcome) into ONE final Telegram per entry.

Outcomes table (spec D):
  1. Synchronous fill (filled==requested): one \u2705 BUY/SELL N shares @ descr
  2. Late graft (IOC ack=0, reconcile grafts): ONE \u2705 "... (late fill, ...)"
  3. Broker flat (IOC ack=0, reconcile flat): ONE \u26a0\ufe0f "... rejected \u2014 limit did not cross ..."
  4. Reconcile raises: ONE \u26a0\ufe0f "... reconcile inconclusive \u2014 verify on broker ..."
  5. Partial fill (software AON): ONE \u26a0\ufe0f "... partial N/M \u2014 keeping partial, sentinels engaged ..."

Key assertion for every outcome: total Telegram send count == 1.
No "unfilled, reconciling..." first ping in any scenario.

Tests (10):
  1. _emit_zerofill_reconcile_followup: grafted (POST_RECONCILE) \u2192 \u2705 (late fill)
  2. _emit_zerofill_reconcile_followup: synced existing row \u2192 \u2705 synced
  3. _emit_zerofill_reconcile_followup: broker flat (empty positions) \u2192 \u26a0\ufe0f rejected
  4. _emit_zerofill_reconcile_followup: reconcile_raised=True \u2192 \u26a0\ufe0f inconclusive
  5. _emit_zerofill_reconcile_followup: exception in helper swallowed (no raise)
  6. End-to-end: synchronous full fill \u2192 exactly 1 \u2705 Telegram total
  7. End-to-end: IOC zero fill + broker flat \u2192 exactly 1 \u26a0\ufe0f Telegram (no first ping)
  8. End-to-end: IOC zero fill + late graft \u2192 exactly 1 \u2705 Telegram (no first ping)
  9. End-to-end: IOC zero fill + reconcile raises \u2192 exactly 1 \u26a0\ufe0f Telegram
  10. End-to-end: software AON partial \u2192 exactly 1 \u26a0\ufe0f "keeping partial" Telegram
"""
from __future__ import annotations

import os
import sys
import types

import pytest

# Minimal env so trade_genius imports cleanly in the test harness.
os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "0000000000:AAAA_smoke_placeholder_token_0000000")
os.environ.setdefault("CHAT_ID", "999999999")
os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
os.environ.setdefault("FMP_API_KEY", "fake_fmp_key_for_tests")


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def base_cls(monkeypatch):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase
    return TradeGeniusBase


class _Stub:
    """Lightweight stand-in for _emit_zerofill_reconcile_followup unit tests."""
    NAME = "TestExec700ZF"

    def __init__(self):
        self.positions: dict = {}
        self.sent: list = []

    def _send_own_telegram(self, text: str) -> None:
        self.sent.append(str(text))


def _bind(base_cls, stub):
    return base_cls._emit_zerofill_reconcile_followup.__get__(stub, stub.__class__)


def _make_executor(monkeypatch, *, filled_qty: str, aon_mode: str = "software",
                   reconcile_side_effect=None, reconcile_position=None):
    """Executor stub for end-to-end ZEROFILL messaging tests."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub700ZF"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst.dollars_per_entry = 10000.0
    inst._aon_mode = aon_mode

    telegrams: list = []

    class _FakeAcct:
        equity = 200_000.0
        cash = 200_000.0
        buying_power = 400_000.0

    class _FakeClient:
        def get_account(self):
            return _FakeAcct()

        def submit_order(self, req):
            ack = types.SimpleNamespace(id="zf-oid-700")
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
    inst._last_open_pnl_ts = float("inf")  # type: ignore

    # Override reconcile to control post-reconcile state for ZEROFILL tests.
    if reconcile_side_effect is not None:
        def _fake_reconcile(ticker, expect="any"):
            if reconcile_position is not None:
                inst.positions[ticker] = reconcile_position
            raise reconcile_side_effect
        inst._reconcile_position_with_broker = _fake_reconcile  # type: ignore
    elif reconcile_position is not None:
        def _fake_reconcile(ticker, expect="any"):
            inst.positions[ticker] = reconcile_position
        inst._reconcile_position_with_broker = _fake_reconcile  # type: ignore
    else:
        # Default: broker flat (position not found after reconcile)
        inst._reconcile_position_with_broker = lambda ticker, expect="any": None  # type: ignore

    return inst, telegrams


# ---------------------------------------------------------------------------
# 1\u20135. _emit_zerofill_reconcile_followup unit tests
# ---------------------------------------------------------------------------


def test_followup_grafted_post_reconcile(base_cls):
    """POST_RECONCILE source \u2192 \u2705 (late fill) message with qty, price, order_id."""
    stub = _Stub()
    stub.positions["ORCL"] = {
        "qty": 52, "entry_price": 185.51, "source": "POST_RECONCILE",
    }
    fn = _bind(base_cls, stub)
    fn(label="Val paper", ticker="ORCL", side="LONG",
       requested_qty=52, order_id="c3e33525", reconcile_raised=False)
    assert len(stub.sent) == 1
    msg = stub.sent[0]
    assert "\u2705" in msg, f"grafted late fill must be \u2705; got {msg!r}"
    assert "late fill" in msg, f"expected 'late fill' in msg: {msg!r}"
    assert "52" in msg
    assert "185.51" in msg
    assert "c3e33525" in msg
    assert "ORCL" in msg
    assert "LONG" in msg


def test_followup_synced_existing_row(base_cls):
    """Existing row with non-POST_RECONCILE source \u2192 \u2705 synced message."""
    stub = _Stub()
    stub.positions["NVDA"] = {
        "qty": 10, "entry_price": 950.12, "source": "SIGNAL",
    }
    fn = _bind(base_cls, stub)
    fn(label="Val paper", ticker="NVDA", side="LONG",
       requested_qty=10, order_id="zyx99999", reconcile_raised=False)
    msg = stub.sent[0]
    assert "\u2705" in msg
    assert "synced" in msg
    assert "late fill" not in msg
    assert "950.12" in msg


def test_followup_broker_flat(base_cls):
    """Empty positions dict \u2192 \u26a0\ufe0f 'rejected \u2014 limit did not cross' message."""
    stub = _Stub()
    fn = _bind(base_cls, stub)
    fn(label="Val paper", ticker="AAPL", side="LONG",
       requested_qty=24, order_id="o12345", reconcile_raised=False)
    assert len(stub.sent) == 1
    msg = stub.sent[0]
    assert "\u26a0" in msg, f"broker flat must be \u26a0\ufe0f; got {msg!r}"
    assert "rejected" in msg
    assert "limit did not cross" in msg
    assert "no broker fill" in msg
    assert "o12345" in msg


def test_followup_reconcile_raised(base_cls):
    """reconcile_raised=True \u2192 \u26a0\ufe0f 'reconcile inconclusive' message."""
    stub = _Stub()
    fn = _bind(base_cls, stub)
    fn(label="Val paper", ticker="MSFT", side="SHORT",
       requested_qty=30, order_id="inc-001", reconcile_raised=True)
    assert len(stub.sent) == 1
    msg = stub.sent[0]
    assert "\u26a0" in msg
    assert "inconclusive" in msg
    assert "verify on broker" in msg
    assert "inc-001" in msg
    assert "MSFT" in msg
    assert "SHORT" in msg


def test_followup_exception_swallowed(base_cls, caplog):
    """If positions[ticker] is unparseable, helper must not raise."""
    stub = _Stub()
    stub.positions["AAPL"] = {"qty": object(), "entry_price": "bad", "source": "POST_RECONCILE"}
    fn = _bind(base_cls, stub)
    fn(label="Val paper", ticker="AAPL", side="LONG",
       requested_qty=10, order_id="oid")
    # No telegram sent (exception in try block)
    assert stub.sent == []


# ---------------------------------------------------------------------------
# 6. End-to-end: synchronous full fill \u2192 exactly 1 \u2705 Telegram
# ---------------------------------------------------------------------------


def test_e2e_full_fill_one_telegram(monkeypatch):
    """Full fill (filled==requested) \u2192 single \u2705 Telegram, no double-ping."""
    inst, telegrams = _make_executor(monkeypatch, filled_qty="20")
    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "GOOG",
        "price": 175.00,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 20,
    })
    assert len(telegrams) == 1, f"expected 1 telegram, got {len(telegrams)}: {telegrams!r}"
    msg = telegrams[0]
    assert "\u2705" in msg
    assert "BUY" in msg or "SELL" in msg
    assert "20" in msg
    assert "unfilled" not in msg.lower()
    assert "reconciling" not in msg.lower()


# ---------------------------------------------------------------------------
# 7. End-to-end: IOC zero fill + broker flat \u2192 exactly 1 \u26a0\ufe0f Telegram
# ---------------------------------------------------------------------------


def test_e2e_zero_fill_broker_flat_one_telegram(monkeypatch):
    """Zero fill + broker flat \u2192 single \u26a0\ufe0f 'rejected \u2014 limit did not cross'.
    No initial 'unfilled, reconciling' ping."""
    inst, telegrams = _make_executor(monkeypatch, filled_qty="0",
                                     reconcile_position=None)
    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "AAPL",
        "price": 285.00,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 18,
    })
    assert len(telegrams) == 1, (
        f"expected exactly 1 Telegram (no first-ping); got {len(telegrams)}: {telegrams!r}"
    )
    msg = telegrams[0]
    assert "unfilled" not in msg.lower() or "reconciling" not in msg.lower(), (
        f"initial 'unfilled, reconciling' ping must be suppressed; got {msg!r}"
    )
    assert "\u26a0" in msg
    assert "rejected" in msg or "inconclusive" in msg or "limit did not cross" in msg


# ---------------------------------------------------------------------------
# 8. End-to-end: IOC zero fill + late graft \u2192 exactly 1 \u2705 Telegram
# ---------------------------------------------------------------------------


def test_e2e_zero_fill_late_graft_one_telegram(monkeypatch):
    """Zero fill + reconcile grafts late fill \u2192 single \u2705 'late fill' Telegram.
    The old v6.15.6 \\u26a0\\ufe0f first-ping must NOT appear."""
    late_position = {
        "ticker": "NVDA", "side": "LONG", "qty": 15,
        "entry_price": 920.50, "source": "POST_RECONCILE",
    }
    inst, telegrams = _make_executor(monkeypatch, filled_qty="0",
                                     reconcile_position=late_position)
    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "NVDA",
        "price": 920.00,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 15,
    })
    assert len(telegrams) == 1, (
        f"expected exactly 1 Telegram (no first-ping + one followup); "
        f"got {len(telegrams)}: {telegrams!r}"
    )
    msg = telegrams[0]
    assert "\u2705" in msg, f"late graft must be \u2705; got {msg!r}"
    assert "late fill" in msg, f"must say 'late fill'; got {msg!r}"
    # Ensure the old first-ping text is absent.
    assert "reconciling against broker" not in msg


# ---------------------------------------------------------------------------
# 9. End-to-end: IOC zero fill + reconcile raises \u2192 exactly 1 \u26a0\ufe0f Telegram
# ---------------------------------------------------------------------------


def test_e2e_zero_fill_reconcile_raises_one_telegram(monkeypatch):
    """Zero fill + reconcile raises \u2192 single \u26a0\ufe0f 'reconcile inconclusive' Telegram."""
    inst, telegrams = _make_executor(
        monkeypatch, filled_qty="0",
        reconcile_side_effect=RuntimeError("broker timeout"),
    )
    inst._on_signal({
        "kind": "ENTRY_SHORT",
        "ticker": "TSLA",
        "price": 250.00,
        "reason": "ENTRY_1_SHORT",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 12,
    })
    assert len(telegrams) == 1, (
        f"expected exactly 1 Telegram; got {len(telegrams)}: {telegrams!r}"
    )
    msg = telegrams[0]
    assert "\u26a0" in msg
    assert "inconclusive" in msg
    assert "verify on broker" in msg
    assert "reconciling against broker" not in msg


# ---------------------------------------------------------------------------
# 10. End-to-end: software AON partial \u2192 exactly 1 \u26a0\ufe0f "keeping partial"
# ---------------------------------------------------------------------------


def test_e2e_software_aon_partial_one_telegram(monkeypatch):
    """Software AON + partial fill \u2192 single \u26a0\ufe0f 'keeping partial, sentinels engaged'."""
    inst, telegrams = _make_executor(monkeypatch, filled_qty="6", aon_mode="software")
    inst._on_signal({
        "kind": "ENTRY_LONG",
        "ticker": "AAPL",
        "price": 285.00,
        "reason": "ENTRY_1",
        "timestamp_utc": "2026-05-06T15:00:00Z",
        "main_shares": 34,
    })
    assert len(telegrams) == 1, (
        f"expected exactly 1 Telegram; got {len(telegrams)}: {telegrams!r}"
    )
    msg = telegrams[0]
    assert "\u26a0" in msg
    assert "partial" in msg.lower()
    assert "6" in msg and "34" in msg
    assert "keeping partial" in msg.lower() or "sentinels engaged" in msg.lower()
    # No "closed back to flat" (Val's revision: keep the partial)
    assert "closed back to flat" not in msg.lower()
