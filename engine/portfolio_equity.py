"""engine.portfolio_equity -- single source of truth for per-portfolio
equity used by RiskBook sizing and dashboard display.

v7.76.0 motivation:
  The 2026-05-11 production incident showed Val rejecting every entry
  with ``risk_reject:notional_cap (would-be $293 > $0)``. Root cause:
  ``RiskBook.equity`` was seeded from ``PortfolioBook.current_equity()``
  which returns ``paper_cash + long_mv - short_liab``. For Val/Gene
  books, ``paper_cash`` defaults to 0 and is never bridged from
  Alpaca's actual account equity. So the RiskBook computed a notional
  cap of 0 and rejected every entry.

  The fix: a single ``resolve_equity(pid)`` helper that knows where to
  source equity for each portfolio:

    main      -> tg.paper_cash (after v7.72.0's sync bridge).
    val,gene  -> Alpaca paper account ``get_account().equity`` via
                 ``<PID>_ALPACA_PAPER_KEY/_SECRET`` env. Cached 30s
                 to avoid hammering Alpaca on every scan cycle.

  Both engine/scan.py (RiskBook seeding) and dashboard_server.py
  (per-portfolio equity display) import from here.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Per-pid Alpaca account snapshot cache. Successful snapshots only;
# transient failures aren't cached so they recover on the next poll.
_ALPACA_ACCT_CACHE: dict[str, tuple[float, dict]] = {}
_ALPACA_ACCT_TTL_S = 30.0


def alpaca_account_for_book(pid: str) -> Optional[dict]:
    """Fetch (cached) Alpaca paper account for a non-main book.

    Resolves ``<PID>_ALPACA_PAPER_KEY`` and ``<PID>_ALPACA_PAPER_SECRET``
    from env (case-insensitive on the pid -- we uppercase). Returns a
    dict with ``equity``, ``cash``, ``last_equity``, ``buying_power``
    (all float) on success. Returns ``None`` when:
      - either env var is missing/blank
      - the alpaca-py import fails
      - the API call raises
      - the account is blocked

    Callers MUST treat ``None`` as "no live equity available, fall back".

    Cached for ``_ALPACA_ACCT_TTL_S`` seconds per pid. Failures NOT
    cached so a transient Alpaca blip recovers on the next call.
    """
    if not pid:
        return None
    pid_norm = str(pid).strip().lower()
    if not pid_norm or pid_norm == "main":
        # main reads tg.paper_cash directly; this helper doesn't apply.
        return None

    now = time.monotonic()
    cached = _ALPACA_ACCT_CACHE.get(pid_norm)
    if cached is not None:
        ts, snap = cached
        if (now - ts) < _ALPACA_ACCT_TTL_S:
            return snap

    pid_up = pid_norm.upper()
    key = (os.getenv("%s_ALPACA_PAPER_KEY" % pid_up, "") or "").strip()
    secret = (os.getenv("%s_ALPACA_PAPER_SECRET" % pid_up, "") or "").strip()
    if len(key) < 8 or len(secret) < 16:
        return None

    try:
        from alpaca.trading.client import TradingClient as _ATC  # type: ignore
        tc = _ATC(key, secret, paper=True)
        acct = tc.get_account()
        if getattr(acct, "account_blocked", False):
            return None
        snap = {
            "equity": float(getattr(acct, "equity", 0) or 0),
            "cash": float(getattr(acct, "cash", 0) or 0),
            "last_equity": float(getattr(acct, "last_equity", 0) or 0),
            "buying_power": float(getattr(acct, "buying_power", 0) or 0),
        }
        _ALPACA_ACCT_CACHE[pid_norm] = (now, snap)
        return snap
    except Exception as exc:
        logger.debug(
            "[v7.76.0] alpaca account fetch failed for %s: %s: %s",
            pid_norm, type(exc).__name__, str(exc)[:120],
        )
        return None


def resolve_equity(pid: str, default_main_equity: float = 100_000.0) -> float:
    """Return the operative equity for `pid` in dollars.

    main      -> tg.paper_cash (legacy paper book global; kept in sync
                 with _MAIN_BOOK.paper_cash via tg._sync_main_book_cash
                 since v7.72.0).
    val,gene  -> Alpaca paper account's ``equity`` field via
                 ``alpaca_account_for_book(pid)``. Falls back to the
                 PortfolioBook's ``current_equity()`` (paper_cash+MTM)
                 if Alpaca is unreachable.

    `default_main_equity` is used only if importing trade_genius fails
    (e.g. during partial init or in tests).
    """
    pid_norm = str(pid).strip().lower()
    if pid_norm in ("", "main"):
        try:
            import trade_genius as tg  # local import to avoid cycles
            return float(getattr(tg, "paper_cash", default_main_equity))
        except Exception:
            return float(default_main_equity)
    # val / gene -- prefer Alpaca live equity, fall back to book's MTM.
    acct = alpaca_account_for_book(pid_norm)
    if acct is not None:
        eq = float(acct.get("equity", 0.0) or 0.0)
        if eq > 0:
            return eq
    # Alpaca unreachable or zero -- fall back to the book's own view.
    try:
        from engine.portfolio_book import PORTFOLIOS
        book = PORTFOLIOS.get(pid_norm)
        if book is not None:
            return float(book.current_equity())
    except Exception:
        pass
    return 0.0
