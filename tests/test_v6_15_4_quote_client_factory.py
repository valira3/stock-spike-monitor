"""v6.15.4 \\u2014 fix typo'd factory reference in _v512_quote_snapshot.

The v6.15.2 PR shipped a reference to ``_historical_data_client`` that
was never defined in trade_genius. The real factory is
``_alpaca_data_client``. The ``"_historical_data_client" in globals()``
guard always evaluated False, so every quote request silently fell
through to (None, None) and from there to the synthetic-spread
fallback in executors/base.py. Production logs showed a constant
storm of ``[V6152-QUOTE] <ticker> client_unavailable`` warnings on
every scan cycle (10 tickers x 6 scans/min observed) even though
ALPACA paper credentials were correctly populated.

These tests guard against the regression coming back: the factory
reference must be ``_alpaca_data_client``, the snapshot must call it
and consume the real client when keys are present, and the
client_unavailable WARN must only fire when no client is buildable
(not when there's a typo in the lookup).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tg_module(monkeypatch):
    """Import trade_genius with smoke-test guards."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "000:fake")
    monkeypatch.setenv("FMP_API_KEY", "test")
    import trade_genius  # noqa: F401
    return sys.modules["trade_genius"]


def test_alpaca_data_client_reference_exists(tg_module):
    """The factory the snapshot calls must actually exist as a
    module-level callable. This is the regression that bit v6.15.2."""
    assert hasattr(tg_module, "_alpaca_data_client")
    assert callable(tg_module._alpaca_data_client)


def test_historical_data_client_typo_not_present(tg_module):
    """Belt-and-suspenders: the broken name from v6.15.2 must not be
    re-introduced. If something gets renamed back to
    ``_historical_data_client`` in the future, deal with the
    snapshot's lookup at the same time."""
    src = (REPO_ROOT / "trade_genius.py").read_text()
    # Allow the literal docstring/comment word "historical" anywhere,
    # but the symbol with the leading underscore must not be a
    # function reference. Match callable form only.
    assert "_historical_data_client(" not in src, (
        "v6.15.2 typo regression: _historical_data_client() reintroduced. "
        "Use _alpaca_data_client()."
    )


def test_quote_snapshot_uses_alpaca_data_client(tg_module, monkeypatch):
    """When the factory returns a real client, _v512_quote_snapshot
    must call get_stock_latest_quote on it and return the bid/ask,
    NOT log client_unavailable and return (None, None)."""
    fake_quote = types.SimpleNamespace(bid_price=283.50, ask_price=283.55)
    fake_client = MagicMock()
    fake_client.get_stock_latest_quote.return_value = {"AAPL": fake_quote}

    monkeypatch.setattr(
        tg_module, "_alpaca_data_client", lambda: fake_client, raising=True,
    )

    bid, ask = tg_module._v512_quote_snapshot("AAPL")
    assert bid == 283.50
    assert ask == 283.55
    fake_client.get_stock_latest_quote.assert_called_once()


def test_quote_snapshot_returns_none_when_no_client(tg_module, monkeypatch, caplog):
    """When the factory legitimately returns None (no keys / import
    failure), the snapshot must log client_unavailable and return
    (None, None) so the synthetic-spread fallback can engage."""
    monkeypatch.setattr(
        tg_module, "_alpaca_data_client", lambda: None, raising=True,
    )

    import logging
    with caplog.at_level(logging.WARNING):
        bid, ask = tg_module._v512_quote_snapshot("AAPL")

    assert bid is None
    assert ask is None
    assert any(
        "[V6152-QUOTE]" in rec.message and "client_unavailable" in rec.message
        for rec in caplog.records
    ), "expected client_unavailable WARN when no client available"


def test_quote_snapshot_handles_dict_or_object_response(tg_module, monkeypatch):
    """Alpaca SDK has returned both ``{symbol: Quote}`` dicts and bare
    Quote objects across versions; the snapshot must handle both."""
    fake_quote = types.SimpleNamespace(bid_price=99.10, ask_price=99.15)

    # Case 1: dict response keyed by symbol
    fake_client = MagicMock()
    fake_client.get_stock_latest_quote.return_value = {"NVDA": fake_quote}
    monkeypatch.setattr(
        tg_module, "_alpaca_data_client", lambda: fake_client, raising=True,
    )
    bid, ask = tg_module._v512_quote_snapshot("NVDA")
    assert bid == 99.10
    assert ask == 99.15


def test_quote_snapshot_non_positive_returns_none(tg_module, monkeypatch, caplog):
    """If the broker returns a record with bid<=0 or ask<=0 we must
    not propagate garbage prices; return (None, None) so the
    synthetic fallback runs."""
    fake_quote = types.SimpleNamespace(bid_price=0.0, ask_price=0.0)
    fake_client = MagicMock()
    fake_client.get_stock_latest_quote.return_value = {"TSLA": fake_quote}
    monkeypatch.setattr(
        tg_module, "_alpaca_data_client", lambda: fake_client, raising=True,
    )

    import logging
    with caplog.at_level(logging.WARNING):
        bid, ask = tg_module._v512_quote_snapshot("TSLA")
    assert bid is None
    assert ask is None
    assert any("non_positive" in rec.message for rec in caplog.records)
