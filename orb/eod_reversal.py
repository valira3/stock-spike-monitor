"""v9.1.0 -- End-of-Day Reversal addon strategy.

Cross-sectional last-30-min reversal on a fenced subset of mega-caps.
Implements the R17 backtest finding (docs/r17_afternoon_backtest_report.md):

  Universe (default):        ORCL, AAPL, MSFT, AVGO, NFLX
  Long-side eligible:        ORCL, AAPL, MSFT, AVGO
  Short-side eligible:       ORCL, NFLX, AAPL, MSFT
  Entry:                     15:30 ET (signal at this minute's start)
  Exit:                      15:59 ET (close of last regular bar)
  Sizing:                    35% notional per leg, fixed
  Selection:                 top-1 long (lowest ROD3) + top-1 short
                             (highest ROD3) of the eligible per-side
                             universes

Mechanism: Baltussen, Da, Soebhag (2024) "End-of-Day Reversal".
Retail-attention buying of intraday losers drives a mean-reversion
pattern in the final 30 min. Effect concentrates on "institutional"
mega-caps; FAILS on retail-momentum names (META, GOOG, TSLA, AMZN,
NVDA) -- those names are EXCLUDED from the default universe.

This module is independent of `orb/engine.py` (the morning ORB).
Live-runtime wires both engines side-by-side. Snapshot fields are
namespaced under `v10.eod` to avoid collisions.

Look-ahead audit (rule #7b):
  - ROD3 = (price at 15:30 minus prior_session_close) / prior_session_close.
    Prior close is sourced from `/data/bars/<D-1>/<TICKER>.jsonl` written by
    `bar_archive.py`. No future data is consulted.
  - Selection runs at 15:30 ET; entries fill on the 15:30 bar open.
  - Exit fires at 15:59 ET on the close print of that minute.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------- config ----------


@dataclass
class EodReversalConfig:
    """All knobs for v9.1.0 EOD reversal. Defaults are the v13-backtest
    winning config (per r17 report); operator can override every field
    via env."""

    enabled: bool = True

    # Universe: every ticker eligible for ROD3 ranking
    universe: tuple = ("ORCL", "AAPL", "MSFT", "AVGO", "NFLX")

    # Per-side fence: only these (ticker, side) pairs admitted. Empty
    # tuple = use the full universe for that side.
    long_tickers: tuple = ("ORCL", "AAPL", "MSFT", "AVGO")
    short_tickers: tuple = ("ORCL", "NFLX", "AAPL", "MSFT")

    # Selection: top-N losers (long) + top-N winners (short). 1 = single
    # best per side per day.
    top_n: int = 1

    # Sizing: fixed notional fraction of equity per leg. 35% is the
    # r17 sweet spot; 25% safer, 50% adds drawdown risk.
    notional_pct: float = 35.0

    # Time anchors in ET minutes-from-midnight
    entry_et_minutes: int = 15 * 60 + 30   # 15:30 ET
    exit_et_minutes: int = 15 * 60 + 59    # 15:59 ET

    # v9.1.0 paper-fire-observation gate. When False (default), the
    # engine TRACKS entries + exits + P&L for the dashboard but does
    # not place real broker orders. Operator flips to True after 5+
    # clean paper-observation days. Mirrors the v8.3.23 fire flag.
    fire_broker: bool = False

    @classmethod
    def from_env(cls) -> "EodReversalConfig":
        """Read config from env vars. All defaults ON per v9.1.0 ship spec.

        Env vars:
          ORB_EOD_REVERSAL_ENABLED=1
          ORB_EOD_UNIVERSE=ORCL,AAPL,MSFT,AVGO,NFLX
          ORB_EOD_LONG_TICKERS=ORCL,AAPL,MSFT,AVGO
          ORB_EOD_SHORT_TICKERS=ORCL,NFLX,AAPL,MSFT
          ORB_EOD_TOP_N=1
          ORB_EOD_NOTIONAL_PCT=35
          ORB_EOD_ENTRY_ET=15:30
          ORB_EOD_EXIT_ET=15:59
        """
        def _b(name: str, default: bool) -> bool:
            v = os.environ.get(name)
            if v is None:
                return default
            return v.strip() in ("1", "true", "True", "yes", "YES")

        def _i(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        def _f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        def _t(name: str, default: tuple) -> tuple:
            v = os.environ.get(name, "").strip()
            if not v:
                return default
            return tuple(t.strip().upper() for t in v.split(",") if t.strip())

        def _et(name: str, default_min: int) -> int:
            v = os.environ.get(name, "").strip()
            if not v:
                return default_min
            try:
                h, m = v.split(":")
                return int(h) * 60 + int(m)
            except Exception:
                return default_min

        return cls(
            enabled=_b("ORB_EOD_REVERSAL_ENABLED", True),
            universe=_t("ORB_EOD_UNIVERSE",
                       ("ORCL", "AAPL", "MSFT", "AVGO", "NFLX")),
            long_tickers=_t("ORB_EOD_LONG_TICKERS",
                            ("ORCL", "AAPL", "MSFT", "AVGO")),
            short_tickers=_t("ORB_EOD_SHORT_TICKERS",
                             ("ORCL", "NFLX", "AAPL", "MSFT")),
            top_n=_i("ORB_EOD_TOP_N", 1),
            notional_pct=_f("ORB_EOD_NOTIONAL_PCT", 35.0),
            entry_et_minutes=_et("ORB_EOD_ENTRY_ET", 15 * 60 + 30),
            exit_et_minutes=_et("ORB_EOD_EXIT_ET", 15 * 60 + 59),
            fire_broker=_b("ORB_EOD_FIRE_BROKER", False),
        )


# ---------- state ----------


@dataclass
class EodPosition:
    """A single open EOD reversal leg. Tracked per-portfolio."""
    portfolio_id: str
    ticker: str
    side: str                   # "long" | "short"
    entry_price: float
    shares: int
    entry_iso: str
    rod3_bps: float             # signal magnitude at entry
    notional_at_entry: float


@dataclass
class EodSessionState:
    """Per-portfolio state for one trading session."""
    portfolio_id: str
    date_iso: str = ""
    entry_attempted: bool = False    # set once we've evaluated 15:30 signal
    open_positions: dict = field(default_factory=dict)  # ticker -> EodPosition
    realized_pnl_today: float = 0.0
    closed_legs: list = field(default_factory=list)     # exit records
    rejected_count: int = 0          # tickers that signaled but didn't fire


# ---------- engine ----------


class EodReversalEngine:
    """Per-process EOD reversal engine. Mirrors `orb.engine.OrbEngine`
    surface: per-portfolio state, snapshot for dashboard, idempotent
    session lifecycle.
    """

    def __init__(self, cfg: EodReversalConfig, portfolio_ids: list[str]) -> None:
        self.cfg = cfg
        self.portfolio_ids = list(portfolio_ids)
        self._states: dict[str, EodSessionState] = {
            pid: EodSessionState(portfolio_id=pid)
            for pid in self.portfolio_ids
        }
        self._session_date: str = ""

    def reset_for_session(self, date_iso: str) -> None:
        """Idempotent reset: clear state for a new trading day."""
        if self._session_date == date_iso:
            return
        self._session_date = date_iso
        for pid in self.portfolio_ids:
            self._states[pid] = EodSessionState(
                portfolio_id=pid, date_iso=date_iso,
            )
        logger.info("[V910-EOD-RESET] date=%s portfolios=%s",
                    date_iso, self.portfolio_ids)

    def is_entry_window(self, current_et_minutes: int) -> bool:
        """True iff current ET minute is the entry tick (15:30 by default).

        We use a single-minute window so the entry fires once per session
        even if the scan loop spins multiple times within that minute.
        Re-entry guard is on `entry_attempted` per-portfolio.
        """
        return current_et_minutes == self.cfg.entry_et_minutes

    def is_exit_window(self, current_et_minutes: int) -> bool:
        """True if at-or-past the exit minute. Allows for late ticks to
        still flatten if the engine was paused at 15:59.
        """
        return current_et_minutes >= self.cfg.exit_et_minutes

    def select_signals(self, *, current_prices: dict[str, float],
                       prior_closes: dict[str, float],
                       ) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
        """Compute ROD3 for each universe ticker, rank, and return the
        per-side (ticker, rod3_bps) selections.

        Returns:
            (long_picks, short_picks) -- each up to top_n entries.
            Empty when insufficient data.
        """
        rod_signals: list[tuple[str, float]] = []
        for tk in self.cfg.universe:
            cur = current_prices.get(tk)
            pc = prior_closes.get(tk)
            if cur is None or pc is None or pc <= 0:
                continue
            rod_bps = (cur - pc) / pc * 10000.0
            rod_signals.append((tk, rod_bps))
        if len(rod_signals) < 2:
            return [], []

        # Lowest ROD3 = top loser (-> LONG); highest = top winner (-> SHORT).
        rod_signals.sort(key=lambda x: x[1])

        if self.cfg.long_tickers:
            eligible_long = [r for r in rod_signals if r[0] in self.cfg.long_tickers]
        else:
            eligible_long = list(rod_signals)
        if self.cfg.short_tickers:
            eligible_short = [r for r in rod_signals if r[0] in self.cfg.short_tickers]
        else:
            eligible_short = list(rod_signals)

        long_picks = eligible_long[: self.cfg.top_n]
        short_picks = eligible_short[-self.cfg.top_n:][::-1]  # highest first
        return long_picks, short_picks

    def admit(self, *, portfolio_id: str, ticker: str, side: str,
              entry_price: float, equity: float, rod3_bps: float,
              entry_iso: str) -> Optional[EodPosition]:
        """Compute shares + record the open position. Returns None on
        bad geometry (e.g. zero price). Idempotent per (portfolio, ticker):
        if a position already exists, returns it unchanged.
        """
        if entry_price <= 0 or equity <= 0:
            return None
        st = self._states.get(portfolio_id)
        if st is None:
            return None
        if ticker in st.open_positions:
            return st.open_positions[ticker]
        notional_target = equity * self.cfg.notional_pct / 100.0
        shares = max(1, int(notional_target / entry_price))
        notional = entry_price * shares
        pos = EodPosition(
            portfolio_id=portfolio_id,
            ticker=ticker,
            side=side,
            entry_price=entry_price,
            shares=shares,
            entry_iso=entry_iso,
            rod3_bps=rod3_bps,
            notional_at_entry=notional,
        )
        st.open_positions[ticker] = pos
        logger.info(
            "[V910-EOD-ENTRY] %s %s %s shares=%d entry=%.4f rod3=%.1fbps "
            "notional=$%.0f",
            portfolio_id, ticker, side, shares, entry_price, rod3_bps,
            notional,
        )
        return pos

    def close(self, *, portfolio_id: str, ticker: str, exit_price: float,
              exit_iso: str, exit_reason: str = "eod") -> Optional[dict]:
        """Close an open position. Returns a closed-leg dict or None when
        no matching position exists.
        """
        st = self._states.get(portfolio_id)
        if st is None:
            return None
        pos = st.open_positions.pop(ticker, None)
        if pos is None:
            return None
        if pos.side == "long":
            pnl = (exit_price - pos.entry_price) * pos.shares
        else:
            pnl = (pos.entry_price - exit_price) * pos.shares
        st.realized_pnl_today += pnl
        leg = {
            "portfolio_id": portfolio_id,
            "ticker": ticker,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "shares": pos.shares,
            "pnl": pnl,
            "rod3_bps": pos.rod3_bps,
            "entry_iso": pos.entry_iso,
            "exit_iso": exit_iso,
            "exit_reason": exit_reason,
        }
        st.closed_legs.append(leg)
        logger.info(
            "[V910-EOD-EXIT] %s %s %s shares=%d exit=%.4f pnl=%+.2f "
            "reason=%s",
            portfolio_id, pos.ticker, pos.side, pos.shares, exit_price,
            pnl, exit_reason,
        )
        return leg

    def mark_attempted(self, portfolio_id: str) -> None:
        st = self._states.get(portfolio_id)
        if st is not None:
            st.entry_attempted = True

    def has_attempted(self, portfolio_id: str) -> bool:
        st = self._states.get(portfolio_id)
        return bool(st and st.entry_attempted)

    def increment_rejected(self, portfolio_id: str) -> None:
        st = self._states.get(portfolio_id)
        if st is not None:
            st.rejected_count += 1

    def snapshot(self) -> dict:
        """JSON-shaped state for the dashboard /api/state.v10.eod block."""
        per_pid: dict = {}
        for pid, st in self._states.items():
            per_pid[pid] = {
                "open_count": len(st.open_positions),
                "open_positions": [
                    {
                        "ticker": p.ticker, "side": p.side,
                        "shares": p.shares,
                        "entry_price": round(p.entry_price, 4),
                        "rod3_bps": round(p.rod3_bps, 1),
                        "notional": round(p.notional_at_entry, 2),
                    }
                    for p in st.open_positions.values()
                ],
                "realized_pnl_today": round(st.realized_pnl_today, 2),
                "entry_attempted": st.entry_attempted,
                "rejected_count": st.rejected_count,
                "closed_legs": [
                    {**leg, "pnl": round(leg["pnl"], 2),
                     "entry_price": round(leg["entry_price"], 4),
                     "exit_price": round(leg["exit_price"], 4)}
                    for leg in st.closed_legs
                ],
            }
        return {
            "enabled": self.cfg.enabled,
            "config": {
                "universe": list(self.cfg.universe),
                "long_tickers": list(self.cfg.long_tickers),
                "short_tickers": list(self.cfg.short_tickers),
                "top_n": self.cfg.top_n,
                "notional_pct": self.cfg.notional_pct,
                "entry_et": _fmt_et(self.cfg.entry_et_minutes),
                "exit_et": _fmt_et(self.cfg.exit_et_minutes),
                "fire_broker": self.cfg.fire_broker,
            },
            "session_date": self._session_date,
            "per_portfolio": per_pid,
        }


def _fmt_et(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
