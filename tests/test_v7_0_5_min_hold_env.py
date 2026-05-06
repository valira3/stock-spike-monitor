"""v7.0.5 \u2014 V644_MIN_HOLD_SEC env wiring.

Asserts that engine.sentinel._V644_MIN_HOLD_SECONDS:
  1. defaults to 120 when env is unset
  2. reads V644_MIN_HOLD_SEC when set to a valid int
  3. falls back to 120 on a malformed value
"""
from __future__ import annotations

import importlib
import os

import pytest


def _reload_sentinel():
    import engine.sentinel as s
    importlib.reload(s)
    return s


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("V644_MIN_HOLD_SEC", raising=False)
    yield


def test_default_is_120(monkeypatch):
    monkeypatch.delenv("V644_MIN_HOLD_SEC", raising=False)
    s = _reload_sentinel()
    assert s._V644_MIN_HOLD_SECONDS == 120


def test_env_override_zero(monkeypatch):
    monkeypatch.setenv("V644_MIN_HOLD_SEC", "0")
    s = _reload_sentinel()
    assert s._V644_MIN_HOLD_SECONDS == 0


def test_env_override_300(monkeypatch):
    monkeypatch.setenv("V644_MIN_HOLD_SEC", "300")
    s = _reload_sentinel()
    assert s._V644_MIN_HOLD_SECONDS == 300


def test_malformed_env_falls_back_to_120(monkeypatch):
    monkeypatch.setenv("V644_MIN_HOLD_SEC", "not_an_int")
    s = _reload_sentinel()
    assert s._V644_MIN_HOLD_SECONDS == 120


def test_empty_env_falls_back_to_120(monkeypatch):
    monkeypatch.setenv("V644_MIN_HOLD_SEC", "")
    s = _reload_sentinel()
    assert s._V644_MIN_HOLD_SECONDS == 120


def test_broker_positions_reads_module_constant(monkeypatch):
    """The broker reads via getattr(_sentinel_mod, ...). Smoke-test that
    flipping the constant is observable."""
    monkeypatch.setenv("V644_MIN_HOLD_SEC", "240")
    s = _reload_sentinel()
    import engine.sentinel as sentinel_mod
    assert getattr(sentinel_mod, "_V644_MIN_HOLD_SECONDS", -1) == 240
