"""v6.14.2 unit tests: BAR_ARCHIVE_RETAIN_DAYS env-overridable.

Verifies bar_archive.DEFAULT_RETAIN_DAYS reads from the environment
when present and falls back to the legacy 90-day default when unset
or when the value cannot be parsed as an int.

NOTE: this test file is intentionally em-dash free (escaped or
literal) per the project author guidelines.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _reload_bar_archive(monkeypatch, env_overrides):
    """Reload bar_archive with the given env state. ``None`` value
    means "delete this key from the env"."""
    for k, v in env_overrides.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    if "bar_archive" in sys.modules:
        del sys.modules["bar_archive"]
    return importlib.import_module("bar_archive")


def test_default_retain_days_when_env_absent(monkeypatch):
    ba = _reload_bar_archive(monkeypatch, {
        "BAR_ARCHIVE_RETAIN_DAYS": None,
    })
    assert ba.DEFAULT_RETAIN_DAYS == 90


def test_retain_days_env_override(monkeypatch):
    ba = _reload_bar_archive(monkeypatch, {
        "BAR_ARCHIVE_RETAIN_DAYS": "150",
    })
    assert ba.DEFAULT_RETAIN_DAYS == 150


def test_retain_days_env_override_lower(monkeypatch):
    ba = _reload_bar_archive(monkeypatch, {
        "BAR_ARCHIVE_RETAIN_DAYS": "30",
    })
    assert ba.DEFAULT_RETAIN_DAYS == 30


def test_retain_days_bogus_value_falls_back(monkeypatch):
    ba = _reload_bar_archive(monkeypatch, {
        "BAR_ARCHIVE_RETAIN_DAYS": "not_a_number",
    })
    assert ba.DEFAULT_RETAIN_DAYS == 90


def test_retain_days_empty_string_falls_back(monkeypatch):
    ba = _reload_bar_archive(monkeypatch, {
        "BAR_ARCHIVE_RETAIN_DAYS": "",
    })
    # Empty string short-circuits via the `or 90` fallback.
    assert ba.DEFAULT_RETAIN_DAYS == 90


def test_retain_days_constant_is_int(monkeypatch):
    ba = _reload_bar_archive(monkeypatch, {
        "BAR_ARCHIVE_RETAIN_DAYS": "120",
    })
    assert isinstance(ba.DEFAULT_RETAIN_DAYS, int)
    assert ba.DEFAULT_RETAIN_DAYS == 120


def test_bot_version_is_6_14_2():
    """Version-pin parity check (matches the per-version tests on main)."""
    if "bot_version" in sys.modules:
        del sys.modules["bot_version"]
    import bot_version
    assert bot_version.BOT_VERSION == "6.14.2"
