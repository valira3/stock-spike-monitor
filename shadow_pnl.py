"""Shadow strategy P&L tracker (v5.2.0).

Each of the 7 SHADOW_CONFIGS becomes a virtual portfolio. When a config
emits a would-have-entered decision, `open_position` records a virtual
position sized via the same v5.1.4 equity-aware formula the live
executor uses. Every scan cycle, `mark_to_market` updates unrealized
P&L for every open position on a given ticker. When the bot's exit
logic decides the position should close (HARD_EJECT_TIGER, trail,
structural stop, EOD), `close_position` realizes the P&L.

State is persisted via the persistence.py SQLite store (table
shadow_positions); in-memory state is rehydrated at boot via
`reload_from_db()`. All public methods are failure-tolerant: a crashed
caller (a missing equity snapshot, a bad price) must NEVER take down
the live trading path.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import persistence

logger = logging.getLogger(__name__)

DEPLOY_TS_UTC = "2026-04-26T00:00:00+00:00"  # v5.2.0 deploy cutoff for "Cumulative"

_SIDE_LONG = "long"
_SIDE_SHORT = "short"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _et_today_str() -> str:
    """Return the current US/Eastern date as YYYY-MM-DD.

    Used to filter "today" rows. Falls back to UTC date if zoneinfo is
    unavailable \u2014 the dashboard column will be slightly off in that
    case but the filter still bounds the row set.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def compute_shadow_qty(
    price: float,
    dollars_per_entry: float,
    equity: float,
    cash: float,
    max_pct_per_entry: float,
    min_reserve_cash: float,
) -> int:
    """v5.1.4 equity-aware sizing formula, isolated for reuse by
    shadow positions.

      effective = min(dollars_per_entry,
                      equity * max_pct/100,
                      cash - min_reserve)
      qty = floor(effective / price)

    Returns 0 when price <= 0 or effective < price (no shares
    affordable). All inputs are best-effort; bad floats return 0.
    """
    try:
        if price is None or price <= 0:
            return 0
        equity = float(equity or 0)
        cash = float(cash or 0)
        equity_cap = equity * (float(max_pct_per_entry) / 100.0)
        cash_available = max(0.0, cash - float(min_reserve_cash))
        effective = min(float(dollars_per_entry), equity_cap, cash_available)
        if effective < float(price):
            return 0
        return max(1, int(effective // float(price)))
    except Exception as e:
        logger.warning("compute_shadow_qty: bad inputs (%s)", e)
        return 0


class _ShadowPosition:
    __slots__ = (
        "row_id", "config_name", "ticker", "side", "qty",
        "entry_ts_utc", "entry_price", "last_mark_price",
        "last_mark_ts_utc",
    )

    def __init__(self, row_id, config_name, ticker, side, qty,
                 entry_ts_utc, entry_price):
        self.row_id = row_id
        self.config_name = config_name
        self.ticker = ticker
        self.side = side
        self.qty = int(qty)
        self.entry_ts_utc = entry_ts_utc
        self.entry_price = float(entry_price)
        self.last_mark_price: Optional[float] = None
        self.last_mark_ts_utc: Optional[str] = None

    def unrealized(self) -> float:
        if self.last_mark_price is None:
            return 0.0
        if self.side == _SIDE_LONG:
            return (self.last_mark_price - self.entry_price) * self.qty
        return (self.entry_price - self.last_mark_price) * self.qty


class ShadowPnL:
    """Per-process shadow portfolio store.

    Thread-safe (all public methods take a lock). State is mirrored to
    SQLite: open_position INSERTs a row, close_position UPDATEs it, and
    mark_to_market is purely in-memory (unrealized snapshot).
    """

    def __init__(self):
        self._lock = threading.RLock()
        # config_name -> list[_ShadowPosition]  (open positions only)
        self._open: dict[str, list[_ShadowPosition]] = {}
        # config_name -> list[dict] of closed positions (realized).
        # Each row: {ticker, side, qty, entry_ts_utc, entry_price,
        #            exit_ts_utc, exit_price, exit_reason, realized_pnl}
        self._closed: dict[str, list[dict]] = {}

    # ---------------------------------------------------------------
    # Persistence rehydration
    # ---------------------------------------------------------------
    def reload_from_db(self) -> None:
        """Load every shadow row from SQLite into memory.

        Open rows go into self._open. Closed rows whose entry_ts_utc is
        on/after DEPLOY_TS_UTC go into self._closed (so cumulative
        totals survive restarts).
        """
        with self._lock:
            self._open.clear()
            self._closed.clear()
            try:
                opens = persistence.load_open_shadow_positions()
            except Exception as e:
                logger.warning("ShadowPnL.reload: load_open failed: %s", e)
                opens = []
            for row in opens:
                p = _ShadowPosition(
                    row_id=row["id"], config_name=row["config_name"],
                    ticker=row["ticker"], side=row["side"],
                    qty=row["qty"], entry_ts_utc=row["entry_ts_utc"],
                    entry_price=row["entry_price"],
                )
                self._open.setdefault(row["config_name"], []).append(p)
            try:
                hist = persistence.load_shadow_positions_since(DEPLOY_TS_UTC)
            except Exception as e:
                logger.warning("ShadowPnL.reload: load_since failed: %s", e)
                hist = []
            for row in hist:
                if row.get("exit_ts_utc") is None:
                    continue
                self._closed.setdefault(row["config_name"], []).append({
                    "ticker": row["ticker"], "side": row["side"],
                    "qty": row["qty"],
                    "entry_ts_utc": row["entry_ts_utc"],
                    "entry_price": row["entry_price"],
                    "exit_ts_utc": row["exit_ts_utc"],
                    "exit_price": row["exit_price"],
                    "exit_reason": row["exit_reason"],
                    "realized_pnl": float(row.get("realized_pnl") or 0.0),
                })

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------
    def open_position(
        self,
        config_name: str,
        ticker: str,
        side: str,
        entry_ts_utc,
        entry_price: float,
        equity_snapshot: dict,
    ) -> Optional[int]:
        """Size + persist a new open position. Returns the row id, or
        None if sizing returned 0 shares or a duplicate row was hit.

        equity_snapshot keys: equity, cash, dollars_per_entry,
        max_pct_per_entry, min_reserve_cash.
        """
        if side not in (_SIDE_LONG, _SIDE_SHORT):
            logger.warning("ShadowPnL.open: bad side %r", side)
            return None
        try:
            entry_price_f = float(entry_price)
        except Exception:
            logger.warning("ShadowPnL.open: bad price %r", entry_price)
            return None
        qty = compute_shadow_qty(
            price=entry_price_f,
            dollars_per_entry=equity_snapshot.get("dollars_per_entry", 0.0),
            equity=equity_snapshot.get("equity", 0.0),
            cash=equity_snapshot.get("cash", 0.0),
            max_pct_per_entry=equity_snapshot.get("max_pct_per_entry", 0.0),
            min_reserve_cash=equity_snapshot.get("min_reserve_cash", 0.0),
        )
        if qty <= 0:
            return None
        if isinstance(entry_ts_utc, datetime):
            ts_iso = entry_ts_utc.astimezone(timezone.utc).isoformat()
        else:
            ts_iso = str(entry_ts_utc) if entry_ts_utc else _utc_now_iso()
        with self._lock:
            # In-memory dedup against (config, ticker, ts_iso) before
            # we even hit SQLite \u2014 cheaper and avoids logging a
            # phantom open on the rapid path.
            for p in self._open.get(config_name, []):
                if p.ticker == ticker and p.entry_ts_utc == ts_iso:
                    return None
            try:
                row_id = persistence.save_shadow_position(
                    config_name=config_name, ticker=ticker, side=side,
                    qty=qty, entry_ts_utc=ts_iso,
                    entry_price=entry_price_f,
                )
            except Exception as e:
                logger.warning("ShadowPnL.open: save failed cfg=%s t=%s: %s",
                               config_name, ticker, e)
                return None
            if row_id is None:
                return None
            p = _ShadowPosition(
                row_id=row_id, config_name=config_name, ticker=ticker,
                side=side, qty=qty, entry_ts_utc=ts_iso,
                entry_price=entry_price_f,
            )
            self._open.setdefault(config_name, []).append(p)
            return row_id

    def mark_to_market(self, ticker: str, current_price: float,
                       current_ts=None) -> int:
        """Update every open position on `ticker` (across all configs)
        with a fresh mark price. Returns the count of positions
        updated.
        """
        try:
            px = float(current_price)
            if px <= 0:
                return 0
        except Exception:
            return 0
        if isinstance(current_ts, datetime):
            ts_iso = current_ts.astimezone(timezone.utc).isoformat()
        else:
            ts_iso = str(current_ts) if current_ts else _utc_now_iso()
        n = 0
        with self._lock:
            for positions in self._open.values():
                for p in positions:
                    if p.ticker == ticker:
                        p.last_mark_price = px
                        p.last_mark_ts_utc = ts_iso
                        n += 1
        return n

    def close_position(
        self,
        config_name: str,
        ticker: str,
        exit_ts_utc,
        exit_price: float,
        exit_reason: str,
    ) -> Optional[float]:
        """Close every open position for (config_name, ticker). Returns
        the summed realized P&L, or None if no open position matched.
        """
        try:
            exit_px = float(exit_price)
        except Exception:
            return None
        if isinstance(exit_ts_utc, datetime):
            ts_iso = exit_ts_utc.astimezone(timezone.utc).isoformat()
        else:
            ts_iso = str(exit_ts_utc) if exit_ts_utc else _utc_now_iso()
        with self._lock:
            bucket = self._open.get(config_name, [])
            keep: list[_ShadowPosition] = []
            total = 0.0
            closed_any = False
            for p in bucket:
                if p.ticker != ticker:
                    keep.append(p)
                    continue
                if p.side == _SIDE_LONG:
                    realized = (exit_px - p.entry_price) * p.qty
                else:
                    realized = (p.entry_price - exit_px) * p.qty
                try:
                    persistence.update_shadow_position_close(
                        row_id=p.row_id,
                        exit_ts_utc=ts_iso,
                        exit_price=exit_px,
                        exit_reason=exit_reason,
                        realized_pnl=realized,
                    )
                except Exception as e:
                    logger.warning(
                        "ShadowPnL.close: persist failed cfg=%s t=%s: %s",
                        config_name, ticker, e,
                    )
                self._closed.setdefault(config_name, []).append({
                    "ticker": ticker, "side": p.side, "qty": p.qty,
                    "entry_ts_utc": p.entry_ts_utc,
                    "entry_price": p.entry_price,
                    "exit_ts_utc": ts_iso,
                    "exit_price": exit_px,
                    "exit_reason": exit_reason,
                    "realized_pnl": realized,
                })
                total += realized
                closed_any = True
            self._open[config_name] = keep
            return total if closed_any else None

    def close_all_for_eod(self, prices: dict[str, float]) -> int:
        """Close every open position with an EOD reason at the given
        per-ticker prices. Returns the number of positions closed.

        v5.2.1 H2: tickers missing from `prices` are no longer left
        open. Each orphan position is force-closed at its own
        ``entry_price`` (realized P&L = 0 by definition) with
        ``exit_reason="EOD_NO_MARK"`` and a WARN log naming the
        config + ticker + entry_price. This prevents stale shadow
        positions from persisting across sessions and bleeding bogus
        realized P&L into next day's EOD via stale entry-price marks.
        """
        n = 0
        with self._lock:
            configs = list(self._open.keys())
        for cfg in configs:
            with self._lock:
                # Snapshot (ticker, entry_price) per still-open position
                # so we can decide between the live mark and the
                # entry-price fallback per row outside the lock.
                snapshot = [
                    (p.ticker, float(p.entry_price))
                    for p in self._open.get(cfg, [])
                ]
            seen: set[str] = set()
            for ticker, entry_price in snapshot:
                if ticker in seen:
                    continue
                seen.add(ticker)
                px = prices.get(ticker)
                if px is None:
                    logger.warning(
                        "ShadowPnL.eod: orphan force-close cfg=%s "
                        "ticker=%s entry_price=%s reason=EOD_NO_MARK",
                        cfg, ticker, entry_price,
                    )
                    if self.close_position(
                        config_name=cfg, ticker=ticker,
                        exit_ts_utc=_utc_now_iso(),
                        exit_price=entry_price,
                        exit_reason="EOD_NO_MARK",
                    ) is not None:
                        n += 1
                    continue
                if self.close_position(
                    config_name=cfg, ticker=ticker,
                    exit_ts_utc=_utc_now_iso(),
                    exit_price=px, exit_reason="EOD",
                ) is not None:
                    n += 1
        return n

    # ---------------------------------------------------------------
    # Aggregation
    # ---------------------------------------------------------------
    def summary(self, today_str: Optional[str] = None) -> dict[str, dict]:
        """Return per-config rollups: today + cumulative realized,
        unrealized, total, n_trades, wins.
        """
        if today_str is None:
            today_str = _et_today_str()
        out: dict[str, dict] = {}
        with self._lock:
            all_configs = set(self._open) | set(self._closed)
            for cfg in all_configs:
                today_realized = 0.0
                today_n = 0
                today_wins = 0
                cum_realized = 0.0
                cum_n = 0
                cum_wins = 0
                for row in self._closed.get(cfg, []):
                    pnl = float(row.get("realized_pnl") or 0.0)
                    cum_realized += pnl
                    cum_n += 1
                    if pnl > 0:
                        cum_wins += 1
                    if (row.get("entry_ts_utc") or "")[:10] >= today_str:
                        # Lexical YYYY-MM-DD compare; UTC date may lag
                        # ET by a few hours but the dashboard refreshes
                        # the moment a new ET trade is closed so this
                        # column stabilizes intraday.
                        today_realized += pnl
                        today_n += 1
                        if pnl > 0:
                            today_wins += 1
                today_unrealized = 0.0
                cum_unrealized = 0.0
                for p in self._open.get(cfg, []):
                    u = p.unrealized()
                    cum_unrealized += u
                    if (p.entry_ts_utc or "")[:10] >= today_str:
                        today_unrealized += u
                        today_n += 1
                    cum_n += 1
                out[cfg] = {
                    "today_realized": round(today_realized, 2),
                    "today_unrealized": round(today_unrealized, 2),
                    "today_total": round(today_realized + today_unrealized, 2),
                    "today_n_trades": today_n,
                    "today_wins": today_wins,
                    "cumulative_realized": round(cum_realized, 2),
                    "cumulative_unrealized": round(cum_unrealized, 2),
                    "cumulative_total": round(
                        cum_realized + cum_unrealized, 2),
                    "cumulative_n_trades": cum_n,
                    "cumulative_wins": cum_wins,
                }
        return out

    def open_count(self, config_name: Optional[str] = None) -> int:
        with self._lock:
            if config_name is None:
                return sum(len(v) for v in self._open.values())
            return len(self._open.get(config_name, []))

    # v5.3.0 \u2014 detail-view helpers for the Shadow tab. These expose
    # per-config open positions + recent closed trades as plain dicts
    # so the dashboard snapshot can serialize them without touching
    # private fields.
    def open_positions_for(self, config_name: str) -> list[dict]:
        """Return all open shadow positions for ``config_name`` as
        snapshot dicts (ticker, side, qty, entry_price, current_mark,
        unrealized, entry_ts_utc). Safe to call from any thread."""
        out: list[dict] = []
        with self._lock:
            for p in self._open.get(config_name, []):
                out.append({
                    "ticker": p.ticker,
                    "side": p.side,
                    "qty": int(p.qty),
                    "entry_price": float(p.entry_price),
                    "entry_ts_utc": p.entry_ts_utc,
                    "current_mark": (
                        float(p.last_mark_price)
                        if p.last_mark_price is not None else None),
                    "unrealized": float(p.unrealized()),
                })
        return out

    def recent_closed_for(
        self, config_name: str, limit: int = 10,
    ) -> list[dict]:
        """Return the most recent ``limit`` closed trades for
        ``config_name`` (newest first by exit_ts_utc lexical order).
        Each row carries ticker, side, qty, entry_price, exit_price,
        realized_pnl, exit_reason, entry_ts_utc, exit_ts_utc."""
        try:
            n = max(0, int(limit))
        except Exception:
            n = 10
        with self._lock:
            rows = list(self._closed.get(config_name, []))
        rows.sort(
            key=lambda r: (r.get("exit_ts_utc") or ""), reverse=True,
        )
        out: list[dict] = []
        for r in rows[:n]:
            out.append({
                "ticker": r.get("ticker"),
                "side": r.get("side"),
                "qty": int(r.get("qty") or 0),
                "entry_price": float(r.get("entry_price") or 0.0),
                "exit_price": float(r.get("exit_price") or 0.0),
                "realized_pnl": float(r.get("realized_pnl") or 0.0),
                "exit_reason": r.get("exit_reason"),
                "entry_ts_utc": r.get("entry_ts_utc"),
                "exit_ts_utc": r.get("exit_ts_utc"),
            })
        return out


# ----------------------------------------------------------------------
# Module-level singleton \u2014 the bot uses one shared instance.
# ----------------------------------------------------------------------
_singleton_lock = threading.Lock()
_singleton: Optional[ShadowPnL] = None


def tracker() -> ShadowPnL:
    """Return the process-wide ShadowPnL singleton, creating it on
    first access. The first caller pays the SQLite reload cost."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            inst = ShadowPnL()
            try:
                inst.reload_from_db()
            except Exception as e:
                logger.warning("ShadowPnL: reload_from_db failed: %s", e)
            _singleton = inst
        return _singleton


def reset_for_tests() -> None:
    """Test-only: drop the singleton so tests can rebuild from a fresh
    SQLite path."""
    global _singleton
    with _singleton_lock:
        _singleton = None
