"""v5.12.0 PR1 regression: executors package public surface is reachable.

Cheap import-time guard \u2014 ensures the executors/base extraction did not
break the public name (`TradeGeniusBase`) on either the new home
(`executors`) or the back-compat re-export inside `trade_genius`.
"""
import os
import sys


def test_import_tradegeniusbase_from_executors():
    """`from executors import TradeGeniusBase` works post-extraction."""
    from executors import TradeGeniusBase
    assert TradeGeniusBase is not None
    assert TradeGeniusBase.__module__ == "executors.base"


def test_tradegenius_base_reexported_in_main_module(monkeypatch):
    """trade_genius.TradeGeniusBase must be the same class as
    executors.base.TradeGeniusBase. The deprecation alias in
    trade_genius.py is removed in v5.12.0 PR 5; until then both names
    resolve to the same object."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import executors.base
    import trade_genius
    assert trade_genius.TradeGeniusBase is executors.base.TradeGeniusBase


def test_tradegenius_base_constructible(monkeypatch):
    """Instantiating a minimal subclass does not crash. Mirrors the
    Val/Gene smoke patterns (smoke_test.py:~662): set the required
    Alpaca env vars on a per-prefix basis, then construct."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    from executors import TradeGeniusBase

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    assert inst.NAME == "TestStub"
    assert inst.mode == "paper"
    assert inst.paper_key == "dummy_paper_key"
