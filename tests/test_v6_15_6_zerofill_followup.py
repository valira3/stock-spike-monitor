"""v6.15.6 \u2014 follow-up Telegram after a V6152-ZEROFILL reconcile.

The v6.15.2 reconcile path runs after every IOC zero-fill so a
late-grafted broker fill is not orphaned, but until v6.15.6 the
warning ``"unfilled, reconciling against broker"`` was the last word
Val saw. He had to inspect the dashboard or logs to learn whether the
reconcile grafted a late fill, found the broker truly flat, or hit an
inconclusive state. v6.15.6 closes that loop with a single follow-up
message reflecting one of three outcomes:

  * GRAFTED \u2014 reconcile installed a POST_RECONCILE-sourced row
    (the typical AAPL / ORCL late-fill pattern).
  * SYNCED  \u2014 a pre-existing row was updated from broker truth.
  * FLAT    \u2014 broker is genuinely flat; no position exists.

These tests exercise ``_emit_zerofill_reconcile_followup`` directly
because the production wiring already has v6.15.2 coverage; this file
focuses on ensuring the new follow-up routes cleanly for every
post-reconcile state.
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


@pytest.fixture
def base_cls(monkeypatch):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase
    return TradeGeniusBase


class _Stub:
    """Minimal stand-in that exposes the attributes the helper reads."""

    NAME = "TestExec"

    def __init__(self):
        self.positions: dict[str, dict] = {}
        self.sent: list[str] = []

    # The bound method under test only needs these two surfaces.
    def _send_own_telegram(self, text: str) -> None:
        self.sent.append(str(text))


def _bind(base_cls, stub):
    """Bind the unbound class method to our lightweight stub."""
    return base_cls._emit_zerofill_reconcile_followup.__get__(stub, stub.__class__)


# ---------------------------------------------------------------------------
# GRAFTED outcome \u2014 the AAPL / ORCL late-fill pattern.
# ---------------------------------------------------------------------------


def test_grafted_late_fill_emits_confirmation(base_cls):
    """Post-reconcile row with source=POST_RECONCILE \u2192 GRAFTED message.

    Mirrors the 2026-05-05 ORCL incident: IOC ack returned filled=0 but
    the reconcile a half-second later found 52 shares @ $185.51 on the
    broker book and grafted them.
    """
    stub = _Stub()
    stub.positions["ORCL"] = {
        "ticker": "ORCL",
        "side": "LONG",
        "qty": 52,
        "entry_price": 185.51,
        "source": "POST_RECONCILE",
    }
    fn = _bind(base_cls, stub)
    fn(label="Val paper", ticker="ORCL", side="LONG",
       requested_qty=52, order_id="c3e33525")
    assert len(stub.sent) == 1
    msg = stub.sent[0]
    assert "ORCL" in msg
    assert "LONG" in msg
    assert "late fill" in msg
    assert "52" in msg
    assert "185.51" in msg
    assert "c3e33525" in msg


def test_grafted_late_fill_short(base_cls):
    """SHORT side renders correctly."""
    stub = _Stub()
    stub.positions["MSFT"] = {
        "ticker": "MSFT",
        "side": "SHORT",
        "qty": 30,
        "entry_price": 412.07,
        "source": "POST_RECONCILE",
    }
    fn = _bind(base_cls, stub)
    fn(label="Gene paper", ticker="MSFT", side="SHORT",
       requested_qty=30, order_id="abc12345")
    msg = stub.sent[0]
    assert "MSFT SHORT" in msg
    assert "late fill" in msg
    assert "412.07" in msg


# ---------------------------------------------------------------------------
# SYNCED outcome \u2014 row already existed (rare on ZEROFILL, possible from
# a state.db rehydration race).
# ---------------------------------------------------------------------------


def test_synced_existing_row_emits_synced_message(base_cls):
    """Existing row whose source is NOT POST_RECONCILE \u2192 SYNCED."""
    stub = _Stub()
    stub.positions["NVDA"] = {
        "ticker": "NVDA",
        "side": "LONG",
        "qty": 10,
        "entry_price": 950.12,
        "source": "SIGNAL",  # rehydrated from state.db, not a post-reconcile graft
    }
    fn = _bind(base_cls, stub)
    fn(label="Val paper", ticker="NVDA", side="LONG",
       requested_qty=10, order_id="zyx99999")
    msg = stub.sent[0]
    assert "synced" in msg
    assert "grafted" not in msg
    assert "950.12" in msg


# ---------------------------------------------------------------------------
# FLAT outcome \u2014 the true zero-fill case (limit really did not cross).
# ---------------------------------------------------------------------------


def test_flat_no_position_emits_true_zerofill(base_cls):
    """Empty positions dict \u2192 broker truly flat, no graft happened."""
    stub = _Stub()
    fn = _bind(base_cls, stub)
    fn(label="Val paper", ticker="AAPL", side="LONG",
       requested_qty=24, order_id="o12345")
    assert len(stub.sent) == 1
    msg = stub.sent[0]
    assert "AAPL LONG" in msg
    assert "rejected" in msg
    assert "limit did not cross" in msg
    assert "no broker fill" in msg
    assert "o12345" in msg


def test_flat_short_side(base_cls):
    """SHORT FLAT renders the SHORT label."""
    stub = _Stub()
    fn = _bind(base_cls, stub)
    fn(label="Val paper", ticker="TSLA", side="SHORT",
       requested_qty=15, order_id="zz1")
    msg = stub.sent[0]
    assert "TSLA SHORT" in msg
    assert "rejected" in msg or "limit did not cross" in msg


# ---------------------------------------------------------------------------
# Robustness \u2014 follow-up MUST never raise into the entry path.
# ---------------------------------------------------------------------------


def test_followup_swallows_exceptions(base_cls, caplog):
    """If positions[ticker] is something unparseable (qty cannot int()),
    the helper must log+exception and return without raising."""
    stub = _Stub()
    stub.positions["AAPL"] = {
        "qty": object(),  # cannot int() this
        "entry_price": "not-a-float",
        "source": "POST_RECONCILE",
    }
    fn = _bind(base_cls, stub)
    # Must not raise.
    fn(label="Val paper", ticker="AAPL", side="LONG",
       requested_qty=10, order_id="oid")
    # No outbound message either, because the try-block aborted.
    assert stub.sent == []


def test_followup_handles_missing_send_telegram(base_cls):
    """If the executor lacks owner chats, _send_own_telegram is a noop;
    helper must still complete cleanly."""
    class _SilentStub(_Stub):
        def _send_own_telegram(self, text: str) -> None:
            # Mimic the real implementation: silently drop when no
            # chats are wired.
            return None

    stub = _SilentStub()
    fn = _bind(base_cls, stub)
    # FLAT path
    fn(label="Val paper", ticker="AAPL", side="LONG",
       requested_qty=10, order_id="oid")
    # Did not raise; no telegram was actually delivered.
    assert stub.sent == []


# ---------------------------------------------------------------------------
# Bot version sanity \u2014 keeps version-pin checks in lockstep.
# ---------------------------------------------------------------------------


def test_bot_version_is_7_0_0():
    """v7.0.0 Phase 5 \u2014 updated from v6.15.6 version pin."""
    import bot_version
    assert bot_version.BOT_VERSION == "7.0.0"


def test_trade_genius_version_matches():
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius
    assert trade_genius.BOT_VERSION == "7.0.0"
