"""Afternoon-strategy backtests (R17).

Implements two academically-documented afternoon strategies, independent of
the morning ORB framework so each can be validated standalone on the same
corpus that produced the v9.0.0 results (`/tmp/rth-data/data/` + the
quarterly slices under `/tmp/cv_q*`).

Both strategies enter at 15:30 ET and exit at 15:55 ET by default.

IMPORTANT: production eod_reversal.py (orb/eod_reversal.py) uses 15:00 ET
entry since v9.1.2 (entry_et_minutes default = 15*60). To match production
set AFT_ENTRY_BUCKET=900 AFT_EXIT_BUCKET=959 explicitly. The 15:00 entry
gives +$10,036/yr vs +$4,649/yr at the default 15:30 on the Keystone corpus.

Strategy 1 -- INTRADAY_MOMENTUM (Gao, Han, Li, Zhou 2015 + Zarattini, Aziz,
Barbon 2024):
    Sign of 09:30-10:00 ET return predicts last-30-min direction.
    Universe: SPY + QQQ only (the literature's tested instruments).
    For each ticker independently:
      m_return = (close at 9:59 - open at 9:30) / open at 9:30
      side = "long" if m_return > 0 else "short" if m_return < 0 else SKIP
      entry  = next 1m bar after 15:30 ET close, slipped 5bps adverse
      exit   = bar at 15:55 ET close, slipped 5bps adverse
      risk_pct of equity per leg (default 0.5%)

Strategy 2 -- EOD_REVERSAL (Baltussen, Da, Soebhag 2024):
    Cross-sectional reversal of intraday return in the last 30 min.
    Universe: 12 mega-caps (full v9 universe).
    For each date:
      ROD3[t] = (close at 15:29 - prior_day_close) / prior_day_close
    Rank, take top-N losers (long) and top-N winners (short), N=2 default.
    Equal-weight; half-risk_pct per leg.
    Hold 15:30 -> 15:55 ET.

Compounding optional (default ON, matches v9 config). Output schema mirrors
orb_backtest.py: per_day/<DATE>.json + summary.json. Independent enough
that the v9 ORB winner config can be combined externally by summing per-
day pnl across the two engines.

Usage:
    python3 tools/afternoon_backtest.py \
        --strategy intraday_momentum \
        --corpus /tmp/rth-data/data --out /tmp/afternoon_im
    python3 tools/afternoon_backtest.py \
        --strategy eod_reversal \
        --corpus /tmp/rth-data/data --out /tmp/afternoon_eod
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Reuse the production-tested bar loader from the ORB backtest. Same
# DST-aware bucket extraction + Bar1m dataclass + load_day_bars().
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.orb_backtest import Bar1m, load_day_bars

logger = logging.getLogger("afternoon_backtest")


# ---------- config ----------


@dataclass
class AfternoonConfig:
    """All knobs in one place. Defaults mirror the v9 baseline so combined
    runs use the same risk envelope.
    """

    strategy: str = "intraday_momentum"  # or "eod_reversal"

    # Universe
    intraday_momentum_tickers: tuple = ("SPY", "QQQ")
    eod_universe: tuple = (
        "AAPL",
        "MSFT",
        "NVDA",
        "TSLA",
        "META",
        "GOOG",
        "AMZN",
        "AVGO",
        "NFLX",
        "ORCL",
        "SPY",
        "QQQ",
    )

    # Sizing
    # The literature strategies (Gao et al., Baltussen et al.) hold to
    # 15:55 / 16:00 without intraday stops; sizing is a fixed fraction
    # of equity per leg, not stop-based. We default to that model and
    # apply a wide safety-net stop (200bps) that rarely binds in normal
    # 25-min holds. "stop_based" mode is available for stress-testing
    # but produces over-sized positions when the realized 25-min move
    # is smaller than the stop distance.
    sizing_mode: str = "fixed_notional"  # "fixed_notional" | "stop_based"
    notional_pct_per_leg: float = 25.0  # 25% of equity per leg
    risk_per_trade_pct: float = 0.5  # used only when sizing_mode=stop_based
    account: float = 100_000.0
    compound_daily: bool = True

    # EOD-reversal-specific
    eod_top_n: int = 2  # top-N winners + top-N losers
    # Per-(ticker, side) fence. If non-empty, only the listed pairs are
    # eligible. e.g. "ORCL:long,ORCL:short,AAPL:long,MSFT:long,NFLX:short".
    # Empty = use eod_universe for both sides.
    eod_long_tickers: tuple = ()
    eod_short_tickers: tuple = ()

    # Time anchors (ET minutes-from-midnight). The literature
    # (Gao 2015) uses signal=09:30-10:00 -> trade=15:30-16:00. We
    # default to that window. entry_bucket is the bar whose OPEN is
    # the entry price (so entry_bucket=930 = 15:30 ET open).
    morning_window_start: int = 9 * 60 + 30  # 09:30
    morning_window_end: int = 10 * 60  # 10:00 (exclusive close)
    entry_bucket: int = 15 * 60 + 30  # 15:30 ET entry bar's open
    # v9.1.125: default exit moved 15:59 -> 15:56 to match the live
    # engine's eod_reversal.exit_et_minutes (orb/eod_reversal.py).
    # Backtest now mirrors the production timing; override via
    # AFT_EXIT_BUCKET if you need a different window for research.
    exit_bucket: int = 15 * 60 + 56  # 15:56 ET exit bar's close

    # Slippage. ETF mega-cap (SPY/QQQ) have ~1bp spreads in
    # liquid hours; individual mega-caps run 1-3bps. Defaults are
    # conservative-realistic, NOT the v9 ORB's 5bps which was for
    # an OR-bar-derived breakout fill.
    entry_slippage_bps: float = 1.5
    exit_slippage_bps: float = 1.5
    short_pen_bps: float = 0.5  # short-side adverse penalty

    # Safety-net stop. Set wide enough that it rarely binds in normal
    # 25-min holds (realized SPY/QQQ moves are 5-50bps typically).
    stop_pct: float = 0.02  # 200 bps safety stop

    @classmethod
    def from_env(cls) -> "AfternoonConfig":
        def _f(k: str, d: float) -> float:
            try:
                return float(os.environ.get(k, d))
            except (TypeError, ValueError):
                return d

        def _i(k: str, d: int) -> int:
            try:
                return int(os.environ.get(k, d))
            except (TypeError, ValueError):
                return d

        def _b(k: str, d: bool) -> bool:
            v = os.environ.get(k)
            if v is None:
                return d
            return v.strip() in ("1", "true", "True", "yes", "YES")

        def _t(k: str, d: tuple) -> tuple:
            v = os.environ.get(k, "")
            if not v.strip():
                return d
            return tuple(t.strip().upper() for t in v.split(",") if t.strip())

        return cls(
            strategy=os.environ.get("AFT_STRATEGY", "intraday_momentum"),
            intraday_momentum_tickers=_t("AFT_IM_TICKERS", cls.intraday_momentum_tickers),
            eod_universe=_t("AFT_EOD_UNIVERSE", cls.eod_universe),
            sizing_mode=os.environ.get("AFT_SIZING_MODE", "fixed_notional"),
            notional_pct_per_leg=_f("AFT_NOTIONAL_PCT", 25.0),
            risk_per_trade_pct=_f("AFT_RISK_PCT", 0.5),
            account=_f("AFT_ACCOUNT", 100_000.0),
            compound_daily=_b("AFT_COMPOUND_DAILY", True),
            eod_top_n=_i("AFT_EOD_TOP_N", 2),
            eod_long_tickers=_t("AFT_EOD_LONG_TICKERS", ()),
            eod_short_tickers=_t("AFT_EOD_SHORT_TICKERS", ()),
            entry_bucket=_i("AFT_ENTRY_BUCKET", 15 * 60 + 30),
            exit_bucket=_i("AFT_EXIT_BUCKET", 15 * 60 + 56),
            entry_slippage_bps=_f("AFT_ENTRY_SLIP_BPS", 1.5),
            exit_slippage_bps=_f("AFT_EXIT_SLIP_BPS", 1.5),
            short_pen_bps=_f("AFT_SHORT_PEN_BPS", 0.5),
            stop_pct=_f("AFT_STOP_PCT", 0.02),
        )


# ---------- helpers ----------


def _bar_at_bucket(bars: list[Bar1m], bucket: int) -> Optional[Bar1m]:
    """Return the first bar with the exact bucket, or the closest later bar
    within 4 minutes (handles missing prints).
    """
    if not bars:
        return None
    # exact
    for b in bars:
        if b.bucket == bucket:
            return b
    # closest later within 4min
    for b in bars:
        if 0 < b.bucket - bucket <= 4:
            return b
    return None


def _last_bar_before(bars: list[Bar1m], bucket: int) -> Optional[Bar1m]:
    """Last bar with bucket strictly less than `bucket`. None if none."""
    last = None
    for b in bars:
        if b.bucket < bucket:
            last = b
        else:
            break
    return last


def _last_rth_close(bars: list[Bar1m]) -> Optional[float]:
    """Last RTH (<=15:59) bar's close. None if no RTH bars."""
    last = None
    for b in bars:
        if 9 * 60 + 30 <= b.bucket <= 15 * 60 + 59:
            last = b
    return last.close if last else None


def _simulate_one_leg(
    *,
    side: str,
    entry_price: float,
    exit_price: float,
    shares: int,
    stop_price: Optional[float],
    bars_during_hold: list[Bar1m],
    cfg: AfternoonConfig,
) -> tuple[float, str]:
    """Return (pnl_dollars, exit_reason).

    A hard stop is honored intra-hold (priority over the 15:55 exit). When
    the stop fires, exit at stop_price; otherwise exit at the 15:55 exit
    bar's close.
    """
    if stop_price is not None:
        for b in bars_during_hold:
            if side == "long" and b.low <= stop_price:
                # Stop hit. Slip the exit price (already conservative).
                ex_slip = stop_price * cfg.exit_slippage_bps / 10000.0
                ex_price = stop_price - ex_slip
                pnl = (ex_price - entry_price) * shares
                return pnl, "stop"
            if side == "short" and b.high >= stop_price:
                ex_slip = stop_price * cfg.exit_slippage_bps / 10000.0
                ex_price = stop_price + ex_slip
                pnl = (entry_price - ex_price) * shares
                return pnl, "stop"
    # Normal EOD exit
    if side == "long":
        ex_slip = exit_price * cfg.exit_slippage_bps / 10000.0
        ex_price = exit_price - ex_slip
        pnl = (ex_price - entry_price) * shares
    else:
        ex_slip = exit_price * cfg.exit_slippage_bps / 10000.0
        ex_price = exit_price + ex_slip
        pnl = (entry_price - ex_price) * shares
    return pnl, "eod"


def _enter_leg(
    *, side: str, raw_entry: float, stop_pct: float, cfg: AfternoonConfig, equity: float
) -> tuple[float, float, int, float]:
    """Compute (entry_price, stop_price, shares, risk_dollars).

    Sizing: when cfg.sizing_mode == "fixed_notional", shares are computed
    from notional_pct_per_leg (a fixed slice of equity). When
    "stop_based", shares scale to risk_per_trade_pct / stop_distance.
    Fixed-notional is the literature's default for intraday-momentum
    and EOD-reversal -- both hold to a clock-based exit, not a stop.
    """
    slip_bps = cfg.entry_slippage_bps + (cfg.short_pen_bps if side == "short" else 0.0)
    slip = raw_entry * slip_bps / 10000.0
    entry_price = raw_entry + slip if side == "long" else raw_entry - slip
    if side == "long":
        stop_price = entry_price * (1.0 - stop_pct)
    else:
        stop_price = entry_price * (1.0 + stop_pct)

    if cfg.sizing_mode == "fixed_notional":
        notional_target = equity * cfg.notional_pct_per_leg / 100.0
        if entry_price <= 0:
            return entry_price, stop_price, 0, 0.0
        shares = max(1, int(notional_target / entry_price))
        risk_dollars = abs(entry_price - stop_price) * shares
    else:
        # stop_based
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0.001:
            return entry_price, stop_price, 0, 0.0
        risk_dollars_target = equity * cfg.risk_per_trade_pct / 100.0
        shares = max(1, int(risk_dollars_target / risk_per_share))
        risk_dollars = risk_per_share * shares
    return entry_price, stop_price, shares, risk_dollars


# ---------- strategy 1: intraday momentum on SPY+QQQ ----------


def simulate_intraday_momentum_day(
    *,
    date: str,
    corpus_dir: Path,
    cfg: AfternoonConfig,
    equity: float,
) -> list[dict]:
    """Per-day P&L pairs for the intraday-momentum strategy."""
    pairs: list[dict] = []
    for ticker in cfg.intraday_momentum_tickers:
        bars = load_day_bars(corpus_dir, date, ticker)
        if not bars:
            continue
        rth = [b for b in bars if 9 * 60 + 30 <= b.bucket <= 15 * 60 + 59]
        if not rth:
            continue
        # Morning window: open of 9:30 bar -> close of 9:59 bar
        morning_bars = [
            b for b in rth if cfg.morning_window_start <= b.bucket < cfg.morning_window_end
        ]
        if len(morning_bars) < 5:
            continue
        m_open = morning_bars[0].open
        m_close = morning_bars[-1].close
        if m_open <= 0:
            continue
        m_return = (m_close - m_open) / m_open
        if m_return == 0:
            continue
        side = "long" if m_return > 0 else "short"

        # Entry: next 1m bar after 15:30 ET signal (so signal is read on
        # the 15:30 close, entry on the 15:31 open). Exit: 15:55 close.
        entry_bar = _bar_at_bucket(rth, cfg.entry_bucket)
        exit_bar = _bar_at_bucket(rth, cfg.exit_bucket)
        if entry_bar is None or exit_bar is None:
            continue
        # Hold bars: bars BETWEEN entry and exit (exclusive of entry, inclusive of exit-1)
        hold_bars = [b for b in rth if entry_bar.bucket < b.bucket < exit_bar.bucket]

        entry_price, stop_price, shares, risk_dollars = _enter_leg(
            side=side,
            raw_entry=entry_bar.open,
            stop_pct=cfg.stop_pct,
            cfg=cfg,
            equity=equity,
        )
        if shares == 0:
            continue
        pnl, exit_reason = _simulate_one_leg(
            side=side,
            entry_price=entry_price,
            exit_price=exit_bar.close,
            shares=shares,
            stop_price=stop_price,
            bars_during_hold=hold_bars,
            cfg=cfg,
        )
        pairs.append(
            {
                "ticker": ticker,
                "side": side,
                "entry_bucket": entry_bar.bucket,
                "exit_bucket": exit_bar.bucket,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_bar.close, 4),
                "stop_price": round(stop_price, 4),
                "shares": shares,
                "pnl_dollars": round(pnl, 2),
                "exit_reason": exit_reason,
                "risk_dollars": round(risk_dollars, 2),
                "m_return_bps": round(m_return * 10000, 1),
            }
        )
    return pairs


# ---------- strategy 2: end-of-day cross-sectional reversal ----------


def simulate_eod_reversal_day(
    *,
    date: str,
    corpus_dir: Path,
    cfg: AfternoonConfig,
    prev_close_lookup: dict[str, float],
    equity: float,
) -> list[dict]:
    """Per-day P&L pairs for the EOD-reversal cross-sectional strategy."""
    # 1. Compute ROD3 (prior_close -> 15:29 close) for each ticker.
    rod_signals: list[tuple[str, float, list[Bar1m]]] = []
    for ticker in cfg.eod_universe:
        bars = load_day_bars(corpus_dir, date, ticker)
        if not bars:
            continue
        rth = [b for b in bars if 9 * 60 + 30 <= b.bucket <= 15 * 60 + 59]
        if not rth:
            continue
        signal_bar = _last_bar_before(rth, cfg.entry_bucket)
        if signal_bar is None or signal_bar.close <= 0:
            continue
        pc = prev_close_lookup.get(ticker)
        if pc is None or pc <= 0:
            continue
        rod = (signal_bar.close - pc) / pc
        rod_signals.append((ticker, rod, rth))

    if len(rod_signals) < 2 * cfg.eod_top_n:
        return []

    # 2. Rank: lowest ROD3 = top loser (-> LONG); highest = top winner (-> SHORT)
    rod_signals.sort(key=lambda x: x[1])
    # Per-side fence: optionally restrict eligible tickers for each side.
    if cfg.eod_long_tickers:
        eligible_long = [r for r in rod_signals if r[0] in cfg.eod_long_tickers]
    else:
        eligible_long = list(rod_signals)
    if cfg.eod_short_tickers:
        eligible_short = [r for r in rod_signals if r[0] in cfg.eod_short_tickers]
    else:
        eligible_short = list(rod_signals)
    longs = eligible_long[: cfg.eod_top_n]
    shorts = eligible_short[-cfg.eod_top_n :]

    # 3. Enter each leg at 15:31 open, exit at 15:55 close.
    pairs: list[dict] = []
    for side, group in [("long", longs), ("short", shorts)]:
        for ticker, rod, rth in group:
            entry_bar = _bar_at_bucket(rth, cfg.entry_bucket + 1)
            exit_bar = _bar_at_bucket(rth, cfg.exit_bucket)
            if entry_bar is None or exit_bar is None:
                continue
            hold_bars = [b for b in rth if entry_bar.bucket < b.bucket < exit_bar.bucket]
            entry_price, stop_price, shares, risk_dollars = _enter_leg(
                side=side,
                raw_entry=entry_bar.open,
                stop_pct=cfg.stop_pct,
                cfg=cfg,
                equity=equity,
            )
            if shares == 0:
                continue
            pnl, exit_reason = _simulate_one_leg(
                side=side,
                entry_price=entry_price,
                exit_price=exit_bar.close,
                shares=shares,
                stop_price=stop_price,
                bars_during_hold=hold_bars,
                cfg=cfg,
            )
            pairs.append(
                {
                    "ticker": ticker,
                    "side": side,
                    "entry_bucket": entry_bar.bucket,
                    "exit_bucket": exit_bar.bucket,
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(exit_bar.close, 4),
                    "stop_price": round(stop_price, 4),
                    "shares": shares,
                    "pnl_dollars": round(pnl, 2),
                    "exit_reason": exit_reason,
                    "risk_dollars": round(risk_dollars, 2),
                    "rod3_bps": round(rod * 10000, 1),
                }
            )
    return pairs


# ---------- driver ----------


def discover_dates(corpus_dir: Path, year_prefix: str) -> list[str]:
    if not corpus_dir.is_dir():
        return []
    out = []
    for child in sorted(os.listdir(corpus_dir)):
        if child.startswith(year_prefix) and (corpus_dir / child).is_dir():
            out.append(child)
    return out


def build_prev_close_index(
    corpus_dir: Path, dates: list[str], tickers: tuple
) -> dict[str, dict[str, float]]:
    """Return {date: {ticker: prior_session_close}}. Walks each date's
    archive once and remembers the last-RTH close; the next date's lookup
    points back to it. Trading-day adjacent (no weekend/holiday gaps in
    the corpus directory listing).
    """
    closes_by_date: dict[str, dict[str, float]] = {}
    for d in dates:
        closes_by_date[d] = {}
        for tk in tickers:
            bars = load_day_bars(corpus_dir, d, tk)
            c = _last_rth_close(bars)
            if c is not None:
                closes_by_date[d][tk] = c
    # Build prev-close lookup: prev_close[date] = closes_by_date[prior_date]
    prev_close: dict[str, dict[str, float]] = {}
    for i, d in enumerate(dates):
        if i == 0:
            prev_close[d] = {}
        else:
            prev_close[d] = closes_by_date[dates[i - 1]]
    return prev_close


def run(cfg: AfternoonConfig, corpus_dir: Path, out_dir: Path, year_prefix: str = "202") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "per_day").mkdir(parents=True, exist_ok=True)

    dates = discover_dates(corpus_dir, year_prefix)
    if not dates:
        logger.error("No dates found in %s with prefix %s", corpus_dir, year_prefix)
        return {}

    # Build prev-close lookup only for EOD strategy.
    prev_close_idx: dict[str, dict[str, float]] = {}
    if cfg.strategy == "eod_reversal":
        prev_close_idx = build_prev_close_index(corpus_dir, dates, cfg.eod_universe)

    running_account = cfg.account
    starting_account = cfg.account
    n_entries = 0
    n_wins = 0
    n_losses = 0
    daily_pnl_summary: list[tuple[str, float]] = []

    for date in dates:
        current_account = running_account if cfg.compound_daily else starting_account
        equity = current_account

        if cfg.strategy == "intraday_momentum":
            pairs = simulate_intraday_momentum_day(
                date=date,
                corpus_dir=corpus_dir,
                cfg=cfg,
                equity=equity,
            )
        elif cfg.strategy == "eod_reversal":
            pairs = simulate_eod_reversal_day(
                date=date,
                corpus_dir=corpus_dir,
                cfg=cfg,
                prev_close_lookup=prev_close_idx.get(date, {}),
                equity=equity,
            )
        else:
            raise ValueError(f"unknown strategy: {cfg.strategy}")

        day_pnl = sum(p["pnl_dollars"] for p in pairs)
        n_entries += len(pairs)
        n_wins += sum(1 for p in pairs if p["pnl_dollars"] > 0)
        n_losses += sum(1 for p in pairs if p["pnl_dollars"] < 0)
        running_account += day_pnl
        daily_pnl_summary.append((date, day_pnl))

        per_day_obj = {
            "date": date,
            "strategy": cfg.strategy,
            "starting_equity": round(equity, 2),
            "day_pnl": round(day_pnl, 2),
            "running_equity": round(running_account, 2),
            "pnl_pairs": pairs,
        }
        with (out_dir / "per_day" / f"{date}.json").open("w") as fh:
            json.dump(per_day_obj, fh, default=str)

    total_decided = max(1, n_wins + n_losses)
    summary = {
        "strategy": cfg.strategy,
        "tickers": list(
            cfg.intraday_momentum_tickers
            if cfg.strategy == "intraday_momentum"
            else cfg.eod_universe
        ),
        "starting_account": starting_account,
        "ending_account": round(running_account, 2),
        "net_pnl": round(running_account - starting_account, 2),
        "fy_return_pct": round((running_account - starting_account) / starting_account * 100, 3),
        "entries": n_entries,
        "wins": n_wins,
        "losses": n_losses,
        "win_rate_pct": round(n_wins / total_decided * 100, 2),
        "n_dates": len(dates),
        "compound_daily": cfg.compound_daily,
        "risk_per_trade_pct": cfg.risk_per_trade_pct,
        "eod_top_n": cfg.eod_top_n if cfg.strategy == "eod_reversal" else None,
    }
    with (out_dir / "summary.json").open("w") as fh:
        json.dump(summary, fh, indent=2, default=str)
    return summary


# ---------- CLI ----------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="afternoon_backtest",
        description="Afternoon-trading backtests (R17): intraday-momentum + EOD-reversal.",
    )
    parser.add_argument("--strategy", required=True, choices=["intraday_momentum", "eod_reversal"])
    parser.add_argument("--corpus", required=True, help="Corpus root (e.g. /tmp/rth-data/data)")
    parser.add_argument("--out", required=True, help="Output dir")
    parser.add_argument("--year-prefix", default="202", help="Date prefix (default 202 = 2020s)")
    parser.add_argument("--account", type=float, default=None, help="Override starting equity")
    parser.add_argument(
        "--risk-pct",
        type=float,
        default=None,
        help="Override risk_per_trade_pct (stop_based sizing only)",
    )
    parser.add_argument(
        "--notional-pct",
        type=float,
        default=None,
        help="Override notional_pct_per_leg (fixed_notional sizing)",
    )
    parser.add_argument("--sizing-mode", default=None, choices=["fixed_notional", "stop_based"])
    parser.add_argument(
        "--slip-bps", type=float, default=None, help="Override slippage (sets both entry + exit)"
    )
    parser.add_argument(
        "--no-compound", action="store_true", help="Disable daily compounding (use static account)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = AfternoonConfig.from_env()
    cfg.strategy = args.strategy
    if args.account is not None:
        cfg.account = args.account
    if args.risk_pct is not None:
        cfg.risk_per_trade_pct = args.risk_pct
    if args.notional_pct is not None:
        cfg.notional_pct_per_leg = args.notional_pct
    if args.sizing_mode is not None:
        cfg.sizing_mode = args.sizing_mode
    if args.slip_bps is not None:
        cfg.entry_slippage_bps = args.slip_bps
        cfg.exit_slippage_bps = args.slip_bps
    if args.no_compound:
        cfg.compound_daily = False

    summary = run(cfg, Path(args.corpus), Path(args.out), year_prefix=args.year_prefix)
    if not summary:
        return 1
    logger.info(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
