"""v7.0.1 -- per-book cooldown registry split.

Verifies:
  1. The legacy free-function path (tg.record_post_loss_cooldown without
     portfolio_id) still routes to main and is visible via main.in_cooldown(),
     main.is_in_post_loss_cooldown(), and the module-global _post_loss_cooldown.
  2. Recording a loss on main does NOT touch val's or gene's registry.
  3. Recording a loss on val does NOT touch main's or gene's registry.
  4. Explicit portfolio_id routing works for both record + read free functions.
  5. get_active_cooldowns(portfolio_id=...) returns only that book's entries.
  6. EOD-style prune via prune_expired_cooldowns clears expired entries
     on every book independently.
  7. Dashboard strip cooldowns counter reads the right book's registry.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
os.environ.setdefault("FMP_API_KEY", "test_dummy_key")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Force longs ON so we can assert the LONG cooldown branch too.
# NOTE: setdefault on os.environ is insufficient when eye_of_tiger has
# already been imported by an earlier test in the run (the module-level
# constants are frozen at import time). The fixture below re-binds the
# constants directly to defeat that ordering hazard.
os.environ.setdefault("POST_LOSS_COOLDOWN_MIN_LONG", "30")
os.environ.setdefault("POST_LOSS_COOLDOWN_MIN_SHORT", "30")


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Reset per-book cooldown dicts in-place so the test starts clean
    without disturbing other test modules that hold a stale ``tg``
    reference at import time. Touching sys.modules invalidates every
    other test file's module-level imports and is what broke v642 in
    the previous iteration.

    v7.0.1 fixture defense (1): prior tests in the run (e.g. v700
    phase3 registry) re-import ``engine.portfolio_book`` from
    ``sys.modules``, creating a brand-new ``PORTFOLIOS`` registry whose
    books carry their own fresh ``_post_loss_cooldown`` dicts. The
    cached identity binding inside ``trade_genius``
    (``tg._post_loss_cooldown is _MAIN_BOOK._post_loss_cooldown``)
    survives, but ``tg._MAIN_BOOK`` points to the *old* main book.
    Free-function callers that route via ``PORTFOLIOS.get('main')``
    then write to the *new* book, while legacy assertions read the
    *old* dict. We repair the binding by rebinding ``tg._MAIN_BOOK``
    (and the module-global dicts) to the currently-live registry's
    main book before each test runs.

    v7.0.1 fixture defense (2): tests/test_v700_phase3_state_migration
    replaces ``sys.modules['trade_genius']`` with a stub module and
    never restores it. If we naively ``import trade_genius`` after
    that test runs, we get the stub (no record_post_loss_cooldown,
    etc.). Detect that case and force a fresh import of the real
    module.
    """
    import sys
    if "trade_genius" in sys.modules and not hasattr(
        sys.modules["trade_genius"], "record_post_loss_cooldown"
    ):
        # A prior test replaced trade_genius with a stub. Drop it so
        # the next import loads the real module from disk.
        del sys.modules["trade_genius"]
    import trade_genius as tg
    from engine.portfolio_book import PORTFOLIOS

    main_book = PORTFOLIOS.get("main")
    # Repair identity binding if a prior test reloaded portfolio_book.
    if tg._MAIN_BOOK is not main_book:
        tg._MAIN_BOOK = main_book
    # The module globals must BE the live main book's dicts so the
    # legacy ``tg._post_loss_cooldown`` reads see what record_post_loss
    # writes through ``PORTFOLIOS.get('main').record_post_loss(...)``.
    tg._post_loss_cooldown = main_book._post_loss_cooldown
    tg._post_exit_cooldown = main_book._post_exit_cooldown

    # v7.0.1 fixture defense (3): if eye_of_tiger was imported before our
    # os.environ.setdefault (very likely \u2014 v700 tests transitively
    # import it), POST_LOSS_COOLDOWN_MIN_LONG is frozen at 0 and our
    # ``side='long'`` records become no-ops. Patch the module constants
    # directly so the long branch records under our test windows.
    import eye_of_tiger as _eot
    _eot.POST_LOSS_COOLDOWN_MIN_LONG = 30
    _eot.POST_LOSS_COOLDOWN_MIN_SHORT = 30

    for pid in ("main", "val", "gene"):
        book = PORTFOLIOS.get(pid)
        book._post_loss_cooldown.clear()
        book._post_exit_cooldown.clear()
    # Now identity must hold by construction.
    assert tg._post_loss_cooldown is main_book._post_loss_cooldown
    assert tg._post_exit_cooldown is main_book._post_exit_cooldown
    yield
    for pid in ("main", "val", "gene"):
        book = PORTFOLIOS.get(pid)
        book._post_loss_cooldown.clear()
        book._post_exit_cooldown.clear()


# ---------------------------------------------------------------------------
# 1. Legacy path: tg.record_post_loss_cooldown(...) routes to main.
# ---------------------------------------------------------------------------

def test_legacy_call_routes_to_main_and_is_visible_via_module_global():
    import trade_genius as tg
    from engine.portfolio_book import PORTFOLIOS

    main_book = PORTFOLIOS.get("main")

    assert tg._post_loss_cooldown == {}
    assert main_book._post_loss_cooldown == {}
    # Identity-bound: same object.
    assert tg._post_loss_cooldown is main_book._post_loss_cooldown

    tg.record_post_loss_cooldown("AAPL", "long", pnl=-15.0)

    assert ("AAPL", "long") in tg._post_loss_cooldown
    assert ("AAPL", "long") in main_book._post_loss_cooldown
    assert main_book.in_cooldown("AAPL", "long") is True
    assert main_book.is_in_post_loss_cooldown("AAPL", "long") is not None

    # Free-function read returns the same entry.
    assert tg.is_in_post_loss_cooldown("AAPL", "long") is not None


# ---------------------------------------------------------------------------
# 2 + 3. Cross-book isolation.
# ---------------------------------------------------------------------------

def test_main_loss_does_not_leak_into_val_or_gene():
    import trade_genius as tg
    from engine.portfolio_book import PORTFOLIOS

    tg.record_post_loss_cooldown("TSLA", "short", pnl=-50.0)

    val = PORTFOLIOS.get("val")
    gene = PORTFOLIOS.get("gene")

    assert val._post_loss_cooldown == {}
    assert gene._post_loss_cooldown == {}
    assert val.in_cooldown("TSLA", "short") is False
    assert gene.in_cooldown("TSLA", "short") is False
    assert tg.is_in_post_loss_cooldown("TSLA", "short", portfolio_id="val") is None
    assert tg.is_in_post_loss_cooldown("TSLA", "short", portfolio_id="gene") is None


def test_val_loss_does_not_leak_into_main_or_gene():
    import trade_genius as tg
    from engine.portfolio_book import PORTFOLIOS

    tg.record_post_loss_cooldown("META", "short", pnl=-30.0, portfolio_id="val")

    main = PORTFOLIOS.get("main")
    gene = PORTFOLIOS.get("gene")
    val = PORTFOLIOS.get("val")

    assert val._post_loss_cooldown != {}
    assert main._post_loss_cooldown == {}
    assert gene._post_loss_cooldown == {}
    assert tg.is_in_post_loss_cooldown("META", "short") is None  # default = main
    assert tg.is_in_post_loss_cooldown("META", "short", portfolio_id="val") is not None
    assert tg.is_in_post_loss_cooldown("META", "short", portfolio_id="gene") is None


# ---------------------------------------------------------------------------
# 4. Explicit portfolio_id kwarg routing for free functions.
# ---------------------------------------------------------------------------

def test_explicit_portfolio_id_routing_record_and_read():
    import trade_genius as tg
    from engine.portfolio_book import PORTFOLIOS

    tg.record_post_loss_cooldown("NVDA", "long", pnl=-22.0, portfolio_id="gene")

    assert tg.is_in_post_loss_cooldown("NVDA", "long", portfolio_id="gene") is not None
    assert tg.is_in_post_loss_cooldown("NVDA", "long", portfolio_id="main") is None
    assert tg.is_in_post_loss_cooldown("NVDA", "long", portfolio_id="val") is None

    main = PORTFOLIOS.get("main")
    gene = PORTFOLIOS.get("gene")
    assert "NVDA" not in {k[0] for k in main._post_loss_cooldown}
    assert ("NVDA", "long") in gene._post_loss_cooldown


# ---------------------------------------------------------------------------
# 5. get_active_cooldowns per-book snapshot.
# ---------------------------------------------------------------------------

def test_get_active_cooldowns_per_book():
    import trade_genius as tg

    tg.record_post_loss_cooldown("AAPL", "long", pnl=-10.0)                       # main
    tg.record_post_loss_cooldown("MSFT", "short", pnl=-12.0, portfolio_id="val")   # val
    tg.record_post_loss_cooldown("ORCL", "short", pnl=-8.0, portfolio_id="gene")  # gene

    main_cd = tg.get_active_cooldowns()  # default main
    val_cd = tg.get_active_cooldowns(portfolio_id="val")
    gene_cd = tg.get_active_cooldowns(portfolio_id="gene")

    assert {(c["ticker"], c["side"]) for c in main_cd} == {("AAPL", "long")}
    assert {(c["ticker"], c["side"]) for c in val_cd} == {("MSFT", "short")}
    assert {(c["ticker"], c["side"]) for c in gene_cd} == {("ORCL", "short")}

    # Each entry has all dashboard fields.
    for entry in (main_cd + val_cd + gene_cd):
        for field in ("ticker", "side", "until_utc", "remaining_sec",
                      "loss_pnl", "loss_ts_utc"):
            assert field in entry


# ---------------------------------------------------------------------------
# 6. prune_expired_cooldowns is per-book.
# ---------------------------------------------------------------------------

def test_prune_expired_cooldowns_per_book():
    import trade_genius as tg
    from engine.portfolio_book import PORTFOLIOS

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    future = datetime.now(timezone.utc) + timedelta(minutes=15)

    main = PORTFOLIOS.get("main")
    val = PORTFOLIOS.get("val")

    main._post_loss_cooldown[("AAPL", "long")] = {
        "until_utc": past, "loss_pnl": -10.0, "loss_ts_utc": past - timedelta(minutes=30),
    }
    main._post_loss_cooldown[("TSLA", "long")] = {
        "until_utc": future, "loss_pnl": -10.0, "loss_ts_utc": datetime.now(timezone.utc),
    }
    val._post_loss_cooldown[("MSFT", "short")] = {
        "until_utc": past, "loss_pnl": -10.0, "loss_ts_utc": past - timedelta(minutes=30),
    }

    n_main = main.prune_expired_cooldowns()
    n_val = val.prune_expired_cooldowns()

    assert n_main == (1, 0)
    assert n_val == (1, 0)
    assert ("AAPL", "long") not in main._post_loss_cooldown
    assert ("TSLA", "long") in main._post_loss_cooldown
    assert ("MSFT", "short") not in val._post_loss_cooldown


# ---------------------------------------------------------------------------
# 7. Dashboard strip helper sees the right book's registry.
# ---------------------------------------------------------------------------

def test_dashboard_strip_per_book_counts():
    import trade_genius as tg
    from engine.portfolio_book import PORTFOLIOS
    import dashboard_server as ds  # noqa

    tg.record_post_loss_cooldown("AAPL", "long", pnl=-10.0)
    tg.record_post_loss_cooldown("META", "short", pnl=-12.0, portfolio_id="val")

    main = PORTFOLIOS.get("main")
    val = PORTFOLIOS.get("val")
    gene = PORTFOLIOS.get("gene")

    main_strip = ds._build_portfolio_strip(main, executor=None)
    val_strip = ds._build_portfolio_strip(val, executor=None)
    gene_strip = ds._build_portfolio_strip(gene, executor=None)

    assert main_strip["cooldowns"] == {"long": 1, "short": 0, "total": 1}
    assert val_strip["cooldowns"] == {"long": 0, "short": 1, "total": 1}
    assert gene_strip["cooldowns"] == {"long": 0, "short": 0, "total": 0}


# ---------------------------------------------------------------------------
# 8. Side-normalization smoke (uppercase/title-case still maps to lowercase).
# ---------------------------------------------------------------------------

def test_side_normalization_round_trip():
    import trade_genius as tg

    tg.record_post_loss_cooldown("AAPL", "LONG", pnl=-15.0)
    tg.record_post_loss_cooldown("MSFT", "Short", pnl=-22.0, portfolio_id="val")

    assert tg.is_in_post_loss_cooldown("AAPL", "long") is not None
    assert tg.is_in_post_loss_cooldown("AAPL", "Long") is not None
    assert tg.is_in_post_loss_cooldown("MSFT", "SHORT", portfolio_id="val") is not None
