"""v7.0.2 -- per-book Alpaca equity wiring on dashboard.

Verifies:
  1. _alpaca_account_for_book returns None when env vars are missing.
  2. _alpaca_account_for_book returns None when the secret is too short
     (Gene's case: GENE_ALPACA_KEY=' ' placeholder, no SECRET).
  3. _alpaca_account_for_book returns None when the alpaca-py call raises.
  4. Successful fetch caches per-pid for ~30s (second call hits cache).
  5. main pid never goes through this path (returns None early).
  6. _build_portfolio_block uses Alpaca equity when creds resolve OK.
  7. _build_portfolio_block falls back to portfolio_equity_floor + day_pnl
     when no Alpaca creds are set (preserves v7.0.1 behavior for unset books).
  8. Block fallback equity also uses fallback when Alpaca raises.
  9. Day PNL is derived from Alpaca (equity minus last_equity) when both
     are non-zero, else from trade_history.

No em-dashes in new .py lines (forbidden chars constraint).
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import patch

import pytest

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
os.environ.setdefault("FMP_API_KEY", "test_dummy_key")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("DASHBOARD_PASSWORD", "test_dummy_password")


@pytest.fixture(autouse=True)
def _clean_cache_and_env():
    """Reset module-level Alpaca cache and any pid env vars between tests
    so prior fixtures or earlier tests can't leak through. Also defends
    against the v7.0.1 ordering hazard (tests that previously imported
    dashboard_server with creds already set)."""
    import dashboard_server as ds
    ds._ALPACA_ACCT_CACHE.clear()
    saved = {}
    for k in (
        "VAL_ALPACA_PAPER_KEY", "VAL_ALPACA_PAPER_SECRET",
        "GENE_ALPACA_PAPER_KEY", "GENE_ALPACA_PAPER_SECRET",
    ):
        saved[k] = os.environ.pop(k, None)
    yield ds
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    ds._ALPACA_ACCT_CACHE.clear()


def _install_fake_alpaca(monkeypatch, equity=110_000.0, last_equity=100_000.0,
                         cash=50_000.0, blocked=False, raises=None):
    """Plant a fake `alpaca.trading.client` in sys.modules so the helper's
    in-function import resolves to our stub instead of the real lib."""
    fake_account = types.SimpleNamespace(
        equity=str(equity),
        cash=str(cash),
        last_equity=str(last_equity),
        buying_power=str(cash * 4),
        account_blocked=blocked,
    )

    class _FakeClient:
        def __init__(self, key, secret, paper=True):
            self.key = key
            self.secret = secret
            self.paper = paper
            if raises is not None:
                raise raises

        def get_account(self):
            if raises is not None:
                raise raises
            return fake_account

    fake_module = types.ModuleType("alpaca.trading.client")
    fake_module.TradingClient = _FakeClient
    fake_pkg_trading = types.ModuleType("alpaca.trading")
    fake_pkg_trading.client = fake_module
    fake_pkg = types.ModuleType("alpaca")
    fake_pkg.trading = fake_pkg_trading
    monkeypatch.setitem(sys.modules, "alpaca", fake_pkg)
    monkeypatch.setitem(sys.modules, "alpaca.trading", fake_pkg_trading)
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", fake_module)
    return _FakeClient


# ---------- helper-level tests ----------

def test_returns_none_when_env_unset(_clean_cache_and_env):
    ds = _clean_cache_and_env
    assert ds._alpaca_account_for_book("val") is None
    assert ds._alpaca_account_for_book("gene") is None


def test_returns_none_when_secret_too_short(_clean_cache_and_env, monkeypatch):
    """Gene's real-world case: KEY is a 1-char placeholder, no SECRET."""
    ds = _clean_cache_and_env
    monkeypatch.setenv("GENE_ALPACA_PAPER_KEY", " ")
    monkeypatch.delenv("GENE_ALPACA_PAPER_SECRET", raising=False)
    assert ds._alpaca_account_for_book("gene") is None


def test_returns_none_for_main_pid(_clean_cache_and_env, monkeypatch):
    """main never uses this path; helper short-circuits even if env
    vars happen to be present (defensive)."""
    ds = _clean_cache_and_env
    monkeypatch.setenv("MAIN_ALPACA_PAPER_KEY", "PKABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setenv("MAIN_ALPACA_PAPER_SECRET", "x" * 40)
    assert ds._alpaca_account_for_book("main") is None


def test_successful_fetch_returns_floats(_clean_cache_and_env, monkeypatch):
    ds = _clean_cache_and_env
    monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "PKABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "y" * 40)
    _install_fake_alpaca(monkeypatch, equity=110_000.0, last_equity=100_000.0,
                         cash=50_000.0)
    snap = ds._alpaca_account_for_book("val")
    assert snap is not None
    assert isinstance(snap["equity"], float)
    assert snap["equity"] == pytest.approx(110_000.0)
    assert snap["last_equity"] == pytest.approx(100_000.0)
    assert snap["cash"] == pytest.approx(50_000.0)


def test_returns_none_when_alpaca_call_raises(_clean_cache_and_env, monkeypatch):
    ds = _clean_cache_and_env
    monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "PKABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "y" * 40)
    _install_fake_alpaca(monkeypatch, raises=RuntimeError("alpaca down"))
    assert ds._alpaca_account_for_book("val") is None


def test_returns_none_when_account_blocked(_clean_cache_and_env, monkeypatch):
    ds = _clean_cache_and_env
    monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "PKABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "y" * 40)
    _install_fake_alpaca(monkeypatch, blocked=True)
    assert ds._alpaca_account_for_book("val") is None


def test_cache_hit_avoids_second_fetch(_clean_cache_and_env, monkeypatch):
    """Second call within TTL should NOT instantiate a new client."""
    ds = _clean_cache_and_env
    monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "PKABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "y" * 40)
    init_count = {"n": 0}

    class _CountingClient:
        def __init__(self, key, secret, paper=True):
            init_count["n"] += 1

        def get_account(self):
            return types.SimpleNamespace(
                equity="100000.0", cash="50000.0",
                last_equity="100000.0", buying_power="200000.0",
                account_blocked=False,
            )

    fake_mod = types.ModuleType("alpaca.trading.client")
    fake_mod.TradingClient = _CountingClient
    fake_trading = types.ModuleType("alpaca.trading")
    fake_trading.client = fake_mod
    fake_root = types.ModuleType("alpaca")
    fake_root.trading = fake_trading
    monkeypatch.setitem(sys.modules, "alpaca", fake_root)
    monkeypatch.setitem(sys.modules, "alpaca.trading", fake_trading)
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", fake_mod)

    snap1 = ds._alpaca_account_for_book("val")
    snap2 = ds._alpaca_account_for_book("val")
    assert snap1 is not None
    assert snap2 is not None
    assert init_count["n"] == 1, "cache should prevent second client init"


# ---------- _build_portfolio_block integration tests ----------

class _FakeBookConfig:
    def __init__(self, floor=100_000.0):
        self.portfolio_equity_floor = floor


class _FakeBook:
    def __init__(self, pid, day_pnl_today=0.0, floor=100_000.0):
        self.portfolio_id = pid
        self.config = _FakeBookConfig(floor=floor)
        self.positions = {}
        self.short_positions = {}
        self.trade_history = []
        self.short_trade_history = []
        self._day_pnl_today = day_pnl_today
        # Internal cooldown registries (so _build_portfolio_strip works).
        self._post_loss_cooldown = {}
        self._post_exit_cooldown = {}

    def get_active_cooldowns(self):
        return {"long": [], "short": [], "post_exit": []}


def _seed_today_trade(book, today_s, pnl):
    book.trade_history.append({"date": today_s, "pnl": pnl, "ticker": "AAPL"})


def test_build_block_uses_alpaca_equity_when_creds_set(_clean_cache_and_env,
                                                      monkeypatch):
    ds = _clean_cache_and_env
    monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "PKABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "y" * 40)
    _install_fake_alpaca(monkeypatch, equity=112_345.67,
                         last_equity=110_000.00, cash=60_000.0)

    book = _FakeBook("val", floor=100_000.0)

    block = ds._build_portfolio_block(book, executor=None, prices={})
    assert block["portfolio_id"] == "val"
    assert block["equity"] == pytest.approx(112_345.67)
    # day_pnl = equity - last_equity when both > 0
    assert block["day_pnl"] == pytest.approx(2_345.67)


def test_build_block_falls_back_when_no_creds(_clean_cache_and_env, monkeypatch):
    """No env vars at all - should use portfolio_equity_floor + day_pnl."""
    ds = _clean_cache_and_env
    # ensure no alpaca lib confusion - we don't call it on the fallback path
    book = _FakeBook("gene", floor=100_000.0)
    # Seed a today trade so day_pnl is non-zero in the fallback path.
    today_s = ""
    try:
        m = ds._ssm()
        today_s = m._now_et().strftime("%Y-%m-%d")
    except Exception:
        pass
    if today_s:
        _seed_today_trade(book, today_s, 250.0)

    block = ds._build_portfolio_block(book, executor=None, prices={})
    assert block["portfolio_id"] == "gene"
    if today_s:
        assert block["equity"] == pytest.approx(100_250.0)
        assert block["day_pnl"] == pytest.approx(250.0)
    else:
        # Edge case: _ssm() failed -> day_pnl=0 -> equity=floor
        assert block["equity"] == pytest.approx(100_000.0)


def test_build_block_falls_back_when_alpaca_raises(_clean_cache_and_env,
                                                   monkeypatch):
    ds = _clean_cache_and_env
    monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "PKABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "y" * 40)
    _install_fake_alpaca(monkeypatch, raises=RuntimeError("nope"))

    book = _FakeBook("val", floor=100_000.0)
    block = ds._build_portfolio_block(book, executor=None, prices={})
    assert block["portfolio_id"] == "val"
    # Falls back to floor (+ 0 day_pnl since no trade_history seeded).
    assert block["equity"] == pytest.approx(100_000.0)


def test_build_block_keeps_history_pnl_when_alpaca_last_equity_zero(
        _clean_cache_and_env, monkeypatch):
    """When Alpaca is reachable but last_equity=0 (e.g. brand-new account),
    day_pnl should retain the trade_history-derived value rather than
    becoming equity - 0 = equity."""
    ds = _clean_cache_and_env
    monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "PKABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "y" * 40)
    _install_fake_alpaca(monkeypatch, equity=100_500.0, last_equity=0.0,
                         cash=60_000.0)

    book = _FakeBook("val", floor=100_000.0)
    today_s = ""
    try:
        m = ds._ssm()
        today_s = m._now_et().strftime("%Y-%m-%d")
    except Exception:
        pass
    if today_s:
        _seed_today_trade(book, today_s, 500.0)

    block = ds._build_portfolio_block(book, executor=None, prices={})
    assert block["equity"] == pytest.approx(100_500.0)
    # equity comes from Alpaca; day_pnl stays from history (last_equity=0).
    if today_s:
        assert block["day_pnl"] == pytest.approx(500.0)
