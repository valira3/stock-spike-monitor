"""earnings_watcher/portfolio_bridge.py \u2014 v7.2.2

Bridge layer that fans every EW (PMR / PMC / DMI) entry and exit into
the same accounting structures RTH writes to, so the dashboard surfaces
EW trades in:

  * portfolio.day_pnl, portfolio.day_pnl_realized, portfolio.day_pnl_unrealized
  * trades_today (top-level + per-portfolio)
  * positions / short_positions (top-level + per-portfolio)
  * portfolios.main / portfolios.val / portfolios.gene (all three books)

Why a bridge instead of inline writes? EW runs in its own thread and
imports trade_genius lazily; writing through one shared funnel keeps
the contract auditable and gives us a single failure point that can
never raise into the trading path.

Design contract (per Val, 2026-05-07):
  * Every EW entry/exit fans into ALL three PortfolioBooks (main, val, gene)
    \u2014 same as RTH \u2014 so each tab shows the same trade.
  * EW broker mode mirrors the destination book's mode (paper vs live).
    The book carries the authoritative mode; runner reads it via
    ``select_alpaca_creds``.

Schema parity with RTH (broker/orders.py):
  * paper_trades entry: {action: "BUY", ticker, price, limit_price,
        shares, cost, stop, entry_num, time, date}
  * paper_trades exit:  {action: "SELL", ticker, price, shares, pnl,
        pnl_pct, reason, entry_price, time, date}
  * short_trade_history: {ticker, side: "SHORT", action: "COVER", shares,
        entry_price, exit_price, pnl, pnl_pct, reason, entry_time,
        exit_time, entry_time_iso, exit_time_iso, entry_num, date}
  * positions / short_positions dict shape from executors/base.py:
        {ticker, side, qty/shares, entry_price, entry_ts_utc,
         entry_time, source: "EW_PMR"/"EW_PMC"/"EW_DMI", date,
         strategy, entry_count}

All writes are guarded with a top-level try/except \u2014 a bridge failure
must NEVER block the EW order path.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Source tag mapping per EW strategy. Used as ``source`` field on the
# position dict so the dashboard can render an "EW" badge.
_SOURCE_BY_STRATEGY = {
    "dmi": "EW_DMI",
    "pmr": "EW_PMR",
    "pmc": "EW_PMC",
}

# Strategy label used on the trade history record so /trades can
# attribute a row to PMR vs PMC vs DMI without parsing the reason.
_LABEL_BY_STRATEGY = {
    "dmi": "ew_dmi",
    "pmr": "ew_pmr",
    "pmc": "ew_pmc",
}


def _now_et() -> datetime:
    try:
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc)


def _now_cdt_hhmm() -> str:
    try:
        return datetime.now(ZoneInfo("America/Chicago")).strftime("%H:%M CDT")
    except Exception:
        return datetime.now(timezone.utc).strftime("%H:%M UTC")


def _today_et() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strategy_from_intent(intent: Dict[str, Any]) -> str:
    """Pull the strategy tag (dmi / pmr / pmc) out of the EW intent.

    The runner stamps ``intent['strategy']`` for PMR/PMC; DMI intents
    historically don't, so we fall back to ``"dmi"``.
    """
    s = (intent.get("strategy") or "").lower()
    if s in _SOURCE_BY_STRATEGY:
        return s
    return "dmi"


# ---------------------------------------------------------------------------
# Portfolio resolution
# ---------------------------------------------------------------------------

def _all_books() -> list:
    """Return [main_book, val_book, gene_book] \u2014 best effort.

    Any book that fails to resolve is skipped silently; the bridge
    continues writing to the remaining books. Importing engine.portfolio_book
    inside the function (not at module import) keeps this file safe to
    import in any test context that doesn't have the engine package.
    """
    books: list = []
    try:
        from engine.portfolio_book import (
            PORTFOLIOS,
            PORTFOLIO_MAIN,
            PORTFOLIO_VAL,
            PORTFOLIO_GENE,
        )
        for pid in (PORTFOLIO_MAIN, PORTFOLIO_VAL, PORTFOLIO_GENE):
            try:
                book = PORTFOLIOS.get(pid)
                if book is not None:
                    books.append(book)
            except Exception as exc:
                logger.warning("[EW-BRIDGE] book lookup failed pid=%s: %s", pid, exc)
    except Exception as exc:
        logger.warning("[EW-BRIDGE] PORTFOLIOS import failed: %s", exc)
    return books


def _book_mode(book: Any) -> str:
    """Resolve the book's broker mode \u2014 'live' or 'paper'.

    Each book's mode is authoritative on the executor that owns it.
    For main we treat the mode as 'paper' (the legacy main book is
    always paper accounting). Failure falls through to 'paper' so EW
    can never inadvertently route to live.
    """
    pid = getattr(book, "portfolio_id", "main")
    if pid == "main":
        return "paper"
    try:
        import trade_genius as tg
        execs = {
            "val": getattr(tg, "val_executor", None),
            "gene": getattr(tg, "gene_executor", None),
        }
        ex = execs.get(pid)
        if ex is not None:
            mode = getattr(ex, "mode", None)
            if mode in ("paper", "live"):
                return mode
    except Exception:
        pass
    return "paper"


# ---------------------------------------------------------------------------
# Public API \u2014 entry / exit fanout
# ---------------------------------------------------------------------------

def record_ew_entry(intent: Dict[str, Any], fill_price: Optional[float] = None) -> int:
    """Fan an EW entry into all 3 books + main paper_trades.

    Idempotent at the position level: if a book already has an open
    position for the ticker on the same side, we replace its dict
    in-place rather than stacking a duplicate.

    Returns the count of books successfully updated.
    """
    try:
        ticker = str(intent.get("ticker") or "").upper()
        if not ticker:
            return 0
        side = str(intent.get("side") or "BUY").upper()
        long_side = side == "BUY"
        qty = int(intent.get("qty") or 0)
        if qty <= 0:
            return 0
        entry_price = float(fill_price) if fill_price else float(intent.get("limit_price") or 0.0)
        if entry_price <= 0:
            return 0

        notional = float(intent.get("notional") or (qty * entry_price))
        strategy = _strategy_from_intent(intent)
        source_tag = _SOURCE_BY_STRATEGY[strategy]
        label = _LABEL_BY_STRATEGY[strategy]
        today = _today_et()
        now_hhmm = _now_cdt_hhmm()
        entry_iso = _utc_now_iso()

        pos_dict = {
            "ticker": ticker,
            "side": "LONG" if long_side else "SHORT",
            "qty": qty,
            "shares": qty,  # main book uses 'shares'; val/gene use 'qty'
            "entry_price": entry_price,
            "entry_ts_utc": entry_iso,
            "entry_time": now_hhmm,
            "entry_count": int(intent.get("entry_num") or 1),
            "date": today,
            "source": source_tag,
            "strategy": label,
            "stop": None,
            "trail": None,
            "notional": notional,
            "ew": True,
        }

        # Long BUYs also get a paper_trades row on the main book per
        # the documented invariant (longs in paper_trades, shorts in
        # short_trade_history at exit only).
        paper_trade_row: Optional[Dict[str, Any]] = None
        if long_side:
            paper_trade_row = {
                "action": "BUY",
                "ticker": ticker,
                "price": entry_price,
                "limit_price": float(intent.get("limit_price") or entry_price),
                "shares": qty,
                "cost": round(qty * entry_price, 2),
                "stop": None,
                "entry_num": pos_dict["entry_count"],
                "time": now_hhmm,
                "date": today,
                "strategy": label,
                "source": source_tag,
                "portfolio": "paper",
                "ew": True,
            }

        n_books = 0
        for book in _all_books():
            try:
                if long_side:
                    book.positions[ticker] = dict(pos_dict)
                else:
                    book.short_positions[ticker] = dict(pos_dict)
                # Stamp chandelier trail state via the book's own helper
                # so trail logic still works for EW positions.
                try:
                    book.record_entry(
                        ticker=ticker,
                        side="LONG" if long_side else "SHORT",
                        entry_price=entry_price,
                        entry_count=pos_dict["entry_count"],
                    )
                except Exception:
                    pass
                # Long BUY -> paper_trades on each book (matches RTH).
                if long_side and paper_trade_row is not None:
                    try:
                        book.paper_trades.append(dict(paper_trade_row))
                        book.paper_all_trades.append(dict(paper_trade_row))
                    except Exception:
                        pass
                n_books += 1
            except Exception as exc:
                logger.warning(
                    "[EW-BRIDGE] entry book=%s ticker=%s error: %s",
                    getattr(book, "portfolio_id", "?"), ticker, exc,
                )

        # Mirror onto trade_genius module-level globals so the legacy
        # main view (_today_trades, _live_positions) sees the row even
        # if the main PortfolioBook identity-binding ever drifts.
        try:
            import trade_genius as tg
            if long_side:
                tg.positions[ticker] = dict(pos_dict)
                if paper_trade_row is not None:
                    tg.paper_trades.append(dict(paper_trade_row))
                    tg.paper_all_trades.append(dict(paper_trade_row))
            else:
                tg.short_positions[ticker] = dict(pos_dict)
        except Exception as exc:
            logger.warning("[EW-BRIDGE] tg-mirror entry %s error: %s", ticker, exc)

        logger.info(
            "[EW-BRIDGE] entry ticker=%s side=%s qty=%d entry=%.4f "
            "strategy=%s books=%d",
            ticker, "LONG" if long_side else "SHORT", qty, entry_price,
            label, n_books,
        )
        return n_books
    except Exception as exc:
        logger.warning("[EW-BRIDGE] record_ew_entry top-level error: %s", exc)
        return 0


def record_ew_exit(
    ticker: str,
    side: str,
    exit_price: float,
    reason: str,
    pos: Optional[Dict[str, Any]] = None,
) -> int:
    """Fan an EW exit into all 3 books.

    For longs: appends a SELL row to paper_trades and a closing record
    to trade_history.
    For shorts: appends a COVER record to short_trade_history (the
    documented single source of truth for shorts).

    Returns the count of books successfully updated. Removes the open
    position from each book's positions/short_positions dict regardless
    of which append path fired.
    """
    try:
        ticker = str(ticker or "").upper()
        if not ticker:
            return 0
        side_u = str(side or "").upper()
        long_side = side_u == "LONG"

        # If we weren't given the originating position dict, recover it
        # from the main book (every book has the same row). This makes
        # the bridge resilient if the EW state file is the only source.
        recovered = pos or {}
        if not recovered:
            try:
                import trade_genius as tg
                if long_side:
                    recovered = dict((tg.positions or {}).get(ticker) or {})
                else:
                    recovered = dict((tg.short_positions or {}).get(ticker) or {})
            except Exception:
                recovered = {}

        try:
            shares = int(recovered.get("shares") or recovered.get("qty") or 0)
        except (TypeError, ValueError):
            shares = 0
        try:
            entry_price = float(recovered.get("entry_price") or 0.0)
        except (TypeError, ValueError):
            entry_price = 0.0
        try:
            exit_price_f = float(exit_price or 0.0)
        except (TypeError, ValueError):
            exit_price_f = 0.0

        if shares <= 0 or entry_price <= 0 or exit_price_f <= 0:
            logger.warning(
                "[EW-BRIDGE] exit ticker=%s side=%s skipped \u2014 missing fields "
                "shares=%s entry=%s exit=%s",
                ticker, side_u, shares, entry_price, exit_price_f,
            )
            # Still try to clear the rows so the dashboard doesn't
            # show a phantom open position.
            for book in _all_books():
                try:
                    if long_side:
                        book.positions.pop(ticker, None)
                    else:
                        book.short_positions.pop(ticker, None)
                except Exception:
                    pass
            return 0

        if long_side:
            pnl_val = (exit_price_f - entry_price) * shares
        else:
            pnl_val = (entry_price - exit_price_f) * shares
        pnl_pct = (pnl_val / (entry_price * shares)) * 100.0 if entry_price * shares else 0.0

        today = _today_et()
        now_hhmm = _now_cdt_hhmm()
        entry_time_str = recovered.get("entry_time") or ""
        entry_iso = recovered.get("entry_ts_utc") or ""
        strategy = (recovered.get("strategy") or "ew_dmi").lower()
        source_tag = recovered.get("source") or "EW_DMI"

        if long_side:
            paper_sell = {
                "action": "SELL",
                "ticker": ticker,
                "price": exit_price_f,
                "shares": shares,
                "pnl": round(pnl_val, 2),
                "pnl_pct": round(pnl_pct, 2),
                "reason": reason,
                "entry_price": entry_price,
                "entry_time": entry_time_str,
                "entry_time_iso": entry_iso,
                "exit_time_iso": _utc_now_iso(),
                "time": now_hhmm,
                "exit_time": now_hhmm,
                "date": today,
                "strategy": strategy,
                "source": source_tag,
                "portfolio": "paper",
                "ew": True,
            }
            history_record = {
                "ticker": ticker,
                "side": "LONG",
                "action": "SELL",
                "shares": shares,
                "entry_price": entry_price,
                "exit_price": exit_price_f,
                "pnl": round(pnl_val, 2),
                "pnl_pct": round(pnl_pct, 2),
                "reason": reason,
                "entry_time": entry_time_str,
                "exit_time": now_hhmm,
                "entry_time_iso": entry_iso,
                "exit_time_iso": _utc_now_iso(),
                "entry_num": int(recovered.get("entry_count", 1)),
                "date": today,
                "strategy": strategy,
                "source": source_tag,
                "ew": True,
            }
        else:
            paper_sell = None
            history_record = {
                "ticker": ticker,
                "side": "SHORT",
                "action": "COVER",
                "shares": shares,
                "entry_price": entry_price,
                "exit_price": exit_price_f,
                "pnl": round(pnl_val, 2),
                "pnl_pct": round(pnl_pct, 2),
                "reason": reason,
                "entry_time": entry_time_str,
                "exit_time": now_hhmm,
                "entry_time_iso": entry_iso,
                "exit_time_iso": _utc_now_iso(),
                "entry_num": int(recovered.get("entry_count", 1)),
                "date": today,
                "strategy": strategy,
                "source": source_tag,
                "portfolio": "paper",
                "ew": True,
            }

        n_books = 0
        for book in _all_books():
            try:
                if long_side:
                    book.positions.pop(ticker, None)
                    if paper_sell is not None:
                        book.paper_trades.append(dict(paper_sell))
                        book.paper_all_trades.append(dict(paper_sell))
                    book.trade_history.append(dict(history_record))
                else:
                    book.short_positions.pop(ticker, None)
                    book.short_trade_history.append(dict(history_record))
                # Update the session ratchet so re-entries respect prior leg.
                try:
                    if long_side:
                        book.record_exit(ticker=ticker, side="LONG",
                                         leg_high=exit_price_f)
                    else:
                        book.record_exit(ticker=ticker, side="SHORT",
                                         leg_low=exit_price_f)
                except Exception:
                    pass
                n_books += 1
            except Exception as exc:
                logger.warning(
                    "[EW-BRIDGE] exit book=%s ticker=%s error: %s",
                    getattr(book, "portfolio_id", "?"), ticker, exc,
                )

        # Mirror to legacy module globals.
        try:
            import trade_genius as tg
            if long_side:
                if hasattr(tg, "positions") and tg.positions is not None:
                    tg.positions.pop(ticker, None)
                if paper_sell is not None:
                    tg.paper_trades.append(dict(paper_sell))
                    tg.paper_all_trades.append(dict(paper_sell))
                if hasattr(tg, "trade_history") and tg.trade_history is not None:
                    tg.trade_history.append(dict(history_record))
            else:
                if hasattr(tg, "short_positions") and tg.short_positions is not None:
                    tg.short_positions.pop(ticker, None)
                if hasattr(tg, "short_trade_history") and tg.short_trade_history is not None:
                    tg.short_trade_history.append(dict(history_record))
        except Exception as exc:
            logger.warning("[EW-BRIDGE] tg-mirror exit %s error: %s", ticker, exc)

        logger.info(
            "[EW-BRIDGE] exit ticker=%s side=%s shares=%d exit=%.4f pnl=%.2f "
            "reason=%s books=%d",
            ticker, side_u, shares, exit_price_f, pnl_val, reason, n_books,
        )
        return n_books
    except Exception as exc:
        logger.warning("[EW-BRIDGE] record_ew_exit top-level error: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Per-portfolio Alpaca creds resolver
# ---------------------------------------------------------------------------

def select_alpaca_creds(portfolio_id: str = "val") -> Tuple[str, str, bool]:
    """Resolve (key, secret, paper) for a given portfolio book.

    Looks up the named book's mode (paper/live) and returns the matching
    credential pair. Defaults to Val paper when the named book is not
    resolvable, to preserve historical EW behavior.

    Env var convention (matches the rest of the codebase):
        VAL_ALPACA_PAPER_KEY/SECRET, VAL_ALPACA_LIVE_KEY/SECRET
        GENE_ALPACA_PAPER_KEY/SECRET, GENE_ALPACA_LIVE_KEY/SECRET

    For ``main`` we use Val's paper creds (main is the internal paper
    book; it has no Alpaca account of its own).
    """
    pid = (portfolio_id or "val").lower()
    # Resolve mode from the named executor; main always paper.
    mode = "paper"
    try:
        from engine.portfolio_book import PORTFOLIOS
        book = PORTFOLIOS.get(pid)
        if book is not None:
            mode = _book_mode(book)
    except Exception:
        pass

    prefix = "VAL" if pid in ("val", "main") else "GENE"
    suffix = "LIVE" if mode == "live" else "PAPER"
    key = (os.getenv(f"{prefix}_ALPACA_{suffix}_KEY") or "").strip()
    secret = (os.getenv(f"{prefix}_ALPACA_{suffix}_SECRET") or "").strip()

    if not key or not secret:
        # Hard fallback: Val paper creds. Better to route to a known
        # paper account than fail an EW order outright.
        key = (os.getenv("VAL_ALPACA_PAPER_KEY") or "").strip()
        secret = (os.getenv("VAL_ALPACA_PAPER_SECRET") or "").strip()
        return key, secret, True

    return key, secret, mode == "paper"


def reconcile_ew_books_with_state() -> int:
    """Back-fill orphan EW positions into all 3 PortfolioBooks.

    Why this exists: ``record_ew_entry`` only fans into the books for
    entries that occur after v7.2.2 boot. Any EW position that was
    opened before boot (or by a prior version that didn't have the
    bridge) lives only in /data/earnings_watcher/open_positions.json
    and never reaches the dashboard's positions/trades_today surface.

    This reconciler reads the EW state file and grafts any missing
    rows into ``book.positions`` / ``book.short_positions`` for all 3
    books, tagged with ``source=EW_*`` and ``ew=True`` so the dashboard
    can render the same EW badge it does for fresh entries.

    Idempotent: rows already present in a book are left alone
    (we never overwrite live executor state). Returns the count of
    grafts performed across all books.

    Called at the top of run_window_cycle; failure is logged and
    swallowed so it never blocks the trading path.
    """
    n_grafts = 0
    try:
        from earnings_watcher.state import load_open_positions
        ew_positions = load_open_positions() or {}
        if not ew_positions:
            return 0

        books = _all_books()
        if not books:
            return 0

        try:
            import trade_genius as tg
        except Exception:
            tg = None  # type: ignore

        today = _today_et()

        for ticker_raw, ewp in ew_positions.items():
            try:
                ticker = str(ticker_raw or "").upper()
                if not ticker:
                    continue
                side_raw = str(ewp.get("side") or "long").lower()
                long_side = side_raw == "long"
                qty = int(ewp.get("qty") or 0)
                entry_price = float(ewp.get("entry_px") or 0.0)
                if qty <= 0 or entry_price <= 0:
                    continue

                strategy = str(ewp.get("strategy") or "dmi").lower()
                source_tag = _SOURCE_BY_STRATEGY.get(strategy, "EW_DMI")
                label = _LABEL_BY_STRATEGY.get(strategy, "ew_dmi")
                entry_iso = ewp.get("entry_ts_utc") or _utc_now_iso()
                # Convert UTC ISO to CDT HH:MM for entry_time field.
                entry_time = ""
                try:
                    from datetime import datetime as _dt
                    _ent = _dt.fromisoformat(str(entry_iso).replace("Z", "+00:00"))
                    entry_time = _ent.astimezone(
                        ZoneInfo("America/Chicago")
                    ).strftime("%H:%M CDT")
                except Exception:
                    entry_time = _now_cdt_hhmm()

                pos_dict = {
                    "ticker": ticker,
                    "side": "LONG" if long_side else "SHORT",
                    "qty": qty,
                    "shares": qty,
                    "entry_price": entry_price,
                    "entry_ts_utc": entry_iso,
                    "entry_time": entry_time,
                    "entry_count": 1,
                    "date": today,
                    "source": source_tag,
                    "strategy": label,
                    "stop": None,
                    "trail": None,
                    "notional": float(ewp.get("notional") or qty * entry_price),
                    "ew": True,
                    "reconciled": True,
                }

                # Graft into each book if not already present.
                for book in books:
                    pid = getattr(book, "portfolio_id", "?")
                    try:
                        target_dict = (book.short_positions if not long_side
                                       else book.positions)
                        if ticker in target_dict:
                            # Already tracked in this book -- leave it alone.
                            # (executor-managed RTH rows take precedence.)
                            continue
                        target_dict[ticker] = dict(pos_dict)
                        try:
                            book.record_entry(
                                ticker=ticker,
                                side="LONG" if long_side else "SHORT",
                                entry_price=entry_price,
                                entry_count=1,
                            )
                        except Exception:
                            pass
                        n_grafts += 1
                        logger.info(
                            "[EW-RECONCILE] grafted ticker=%s side=%s qty=%d "
                            "entry=%.4f book=%s strategy=%s",
                            ticker, "LONG" if long_side else "SHORT",
                            qty, entry_price, pid, label,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[EW-RECONCILE] graft error book=%s ticker=%s: %s",
                            pid, ticker, exc,
                        )

                # Mirror into trade_genius module globals so the legacy
                # main view (_today_trades, _live_positions) sees it too.
                if tg is not None:
                    try:
                        if long_side:
                            if ticker not in (tg.positions or {}):
                                tg.positions[ticker] = dict(pos_dict)
                        else:
                            if ticker not in (tg.short_positions or {}):
                                tg.short_positions[ticker] = dict(pos_dict)
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning(
                    "[EW-RECONCILE] per-ticker error ticker=%s: %s",
                    ticker_raw, exc,
                )

        if n_grafts > 0:
            logger.info("[EW-RECONCILE] cycle complete grafted=%d", n_grafts)
        return n_grafts
    except Exception as exc:
        logger.warning("[EW-RECONCILE] top-level error: %s", exc)
        return 0


def resolve_ew_target_portfolio() -> str:
    """Return the EW target portfolio id.

    Priority: env ``EW_PORTFOLIO`` (val | gene | main) -> 'val' default.
    Multi-book fanout still happens for accounting; this only governs
    which broker account actually fills the order.
    """
    pid = (os.getenv("EW_PORTFOLIO") or "val").lower()
    if pid not in ("val", "gene", "main"):
        return "val"
    return pid
