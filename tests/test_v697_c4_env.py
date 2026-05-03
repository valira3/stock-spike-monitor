"""tests/test_v697_c4_env.py

v6.9.7 -- verify that _V610_ATR_OR_BREAK_ENABLED and V610_OR_BREAK_K
are readable from environment variables with correct defaults.
"""
import importlib
import sys

import pytest


def _reload_trade_genius():
    """Force a fresh import of trade_genius, removing any cached module."""
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius as tg
    return tg


def test_defaults_without_env(monkeypatch):
    """With env vars unset, defaults must be False and 0.25."""
    monkeypatch.delenv("_V610_ATR_OR_BREAK_ENABLED", raising=False)
    monkeypatch.delenv("V610_OR_BREAK_K", raising=False)

    tg = _reload_trade_genius()

    assert tg._V610_ATR_OR_BREAK_ENABLED is False
    assert tg.V610_OR_BREAK_K == pytest.approx(0.25)


def test_env_override(monkeypatch):
    """With env vars set, values must reflect the overrides."""
    monkeypatch.setenv("_V610_ATR_OR_BREAK_ENABLED", "1")
    monkeypatch.setenv("V610_OR_BREAK_K", "0.40")

    tg = _reload_trade_genius()

    assert tg._V610_ATR_OR_BREAK_ENABLED is True
    assert tg.V610_OR_BREAK_K == pytest.approx(0.40)
