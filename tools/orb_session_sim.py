"""tools.orb_session_sim -- end-to-end scenario simulator for v10 ORB.

Drives the LIVE production runtime (orb.live_runtime) through a synthetic
trading day with no broker, no telegram, no scan loop. Used by:

  1. Unit tests under tests/strategy/test_orb_session_sim.py to assert
     each rule (RR target, stop, BE, EOD, VIX kill, earnings skip, gap
     skip, blocklist, range band, risk caps, multi-portfolio).
  2. CLI mode for ad-hoc rule probing during development:
       python -m tools.orb_session_sim --scenario golden_long

What this is NOT:
  - Not a backtester (use tools/orb_backtest.py for historical P&L).
  - Not a broker simulator (no order books / spread / slippage modeling).
  - Not a load test.

It IS a deterministic, fast, in-process driver for the same code path
that engine/scan.py exercises in production. Tests built on this run in
milliseconds and prove that the live runtime honors the v10 keystone
rules end-to-end.

Design (per rule #7b -- no look-ahead):
  - The simulator only feeds data through the same APIs the live engine
    uses (feed_bar -> check_entry -> check_exit). It cannot bypass any
    gate.
  - The clock is supplied as the bar bucket_min parameter. Time advances
    only when the caller advances it, so every test is fully
    deterministic.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

from orb import live_runtime
from orb.live_adapter import EntryResult, ExitResult

logger = logging.getLogger(__name__)


# ----- Constants ---------------------------------------------------

OR_START_BUCKET = 9 * 60 + 30  # 09:30 ET = bucket 570
OR_END_BUCKET_DEFAULT = 9 * 60 + 60  # 10:00 ET = bucket 600 (30-min OR)
EOD_BUCKET = 15 * 60 + 55  # 15:55 ET = bucket 955
SESSION_END_BUCKET = 16 * 60  # 16:00 ET = bucket 960


# ----- Bar helpers -------------------------------------------------


@dataclass
class Bar:
    """One synthetic 1-min bar."""
    bucket_min: int
    high: float
    low: float
    open: float
    close: float
    volume: float = 10_000.0


def make_or_bars(*, ticker: str, or_low: float, or_high: float,
                 or_minutes: int = 30,
                 start_bucket: int = OR_START_BUCKET,
                 ) -> list[Bar]:
    """Build `or_minutes` synthetic 1-min bars whose collective range is
    exactly [or_low, or_high]. The first bar carries the high+low; the
    rest oscillate inside.
    """
    bars: list[Bar] = []
    mid = (or_high + or_low) / 2.0
    for i in range(or_minutes):
        b = Bar(
            bucket_min=start_bucket + i,
            high=or_high if i == 0 else mid + 0.05,
            low=or_low if i == 0 else mid - 0.05,
            open=mid - 0.02,
            close=mid + 0.02,
            volume=10_000.0,
        )
        bars.append(b)
    return bars


def make_breakout_bar(*, bucket: int, side: str, or_high: float,
                      or_low: float, push_pct: float = 0.005) -> Bar:
    """Build a 1-min bar that closes outside the OR window in `side`.

    side="long":  close = or_high * (1 + push_pct)
    side="short": close = or_low  * (1 - push_pct)
    """
    if side == "long":
        close_px = or_high * (1.0 + push_pct)
        return Bar(bucket_min=bucket, open=or_high, high=close_px,
                   low=or_high - 0.01, close=close_px, volume=20_000.0)
    elif side == "short":
        close_px = or_low * (1.0 - push_pct)
        return Bar(bucket_min=bucket, open=or_low, high=or_low + 0.01,
                   low=close_px, close=close_px, volume=20_000.0)
    raise ValueError(f"side must be long|short, got: {side}")


def make_exit_bar(*, bucket: int, high: float, low: float,
                  close: Optional[float] = None) -> Bar:
    """Build an arbitrary 1-min bar for exit evaluation."""
    c = close if close is not None else (high + low) / 2.0
    return Bar(bucket_min=bucket, high=high, low=low,
               open=(high + low) / 2.0, close=c, volume=15_000.0)


# ----- Simulator ---------------------------------------------------


@dataclass
class ScenarioStep:
    """One audit-trail entry recorded by the simulator."""
    kind: str        # "feed_bar" | "entry" | "exit" | "session_start"
    detail: dict     # arbitrary diagnostic payload


@dataclass
class SimulatorConfig:
    """All knobs the test suite cares about. Defaults match v10 keystone."""
    date_iso: str = "2026-01-15"
    tickers: list[str] = field(default_factory=lambda: ["AAPL"])
    vix_close_d1: Optional[float] = 18.0
    ticker_open_today: dict[str, float] = field(default_factory=dict)
    ticker_prev_close: dict[str, float] = field(default_factory=dict)
    equity_per_portfolio: dict[str, float] = field(
        default_factory=lambda: {"main": 100_000.0})
    # OrbConfig overrides via env (set on enter, cleared on exit)
    env_overrides: dict[str, str] = field(default_factory=dict)


class SessionSimulator:
    """Drive a v10 ORB session synthetically.

    Usage:
        sim = SessionSimulator(SimulatorConfig(
            date_iso="2026-01-15",
            tickers=["AAPL"],
            vix_close_d1=18.0,
            ticker_open_today={"AAPL": 100.0},
            ticker_prev_close={"AAPL": 100.0},
            equity_per_portfolio={"main": 100_000.0},
        ))
        with sim:
            sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
            sim.feed_bar(Bar(bucket_min=600, high=101.5, low=100.5,
                              open=100.5, close=101.0))
            ent = sim.try_long(ticker="AAPL", price=101.0)
            assert ent.ok
            ex = sim.walk_to_target(ticker="AAPL", target=ent.target)
            assert ex.exit and ex.reason == "target"

    Context-manager use is encouraged so env overrides + runtime state
    are reset deterministically between scenarios.
    """

    def __init__(self, cfg: Optional[SimulatorConfig] = None) -> None:
        self.cfg = cfg or SimulatorConfig()
        self.steps: list[ScenarioStep] = []
        self._prev_env: dict[str, Optional[str]] = {}
        self._started = False

    # --- context manager ---

    def __enter__(self):
        import os
        # Apply env overrides
        for k, v in self.cfg.env_overrides.items():
            self._prev_env[k] = os.environ.get(k)
            os.environ[k] = v
        live_runtime._reset_for_testing()
        live_runtime.bootstrap()
        return self

    def __exit__(self, *exc):
        import os
        live_runtime._reset_for_testing()
        # Restore env
        for k, prev in self._prev_env.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        self._prev_env.clear()
        return False

    # --- session lifecycle ---

    def start(self) -> bool:
        """Idempotent session start. Returns True if started."""
        # Default open/pdc from tickers list when not supplied
        opens = dict(self.cfg.ticker_open_today)
        pdcs = dict(self.cfg.ticker_prev_close)
        for tk in self.cfg.tickers:
            opens.setdefault(tk, 100.0)
            pdcs.setdefault(tk, 100.0)
        ok = live_runtime.ensure_session_started(
            date_iso=self.cfg.date_iso,
            tickers=list(self.cfg.tickers),
            vix_close_d1=self.cfg.vix_close_d1,
            ticker_open_today=opens,
            ticker_prev_close=pdcs,
            equity_per_portfolio=dict(self.cfg.equity_per_portfolio),
        )
        if ok:
            self._started = True
            self.steps.append(ScenarioStep(kind="session_start",
                                           detail={"date": self.cfg.date_iso}))
        return ok

    # --- feeding ---

    def feed_bar(self, bar: Bar, *, ticker: str) -> None:
        """Forward a single 1-min bar to the engine."""
        if not self._started:
            self.start()
        live_runtime.feed_bar(
            ticker=ticker,
            bar_high=bar.high, bar_low=bar.low, bar_open=bar.open,
            bar_close=bar.close, bar_volume=bar.volume,
            bar_bucket_min=bar.bucket_min,
        )
        self.steps.append(ScenarioStep(
            kind="feed_bar",
            detail={"ticker": ticker, "bucket": bar.bucket_min,
                    "high": bar.high, "low": bar.low, "close": bar.close},
        ))

    def feed_or(self, *, ticker: str, or_low: float, or_high: float,
                or_minutes: int = 30) -> None:
        """Feed a complete 30-bar OR window with the given high/low."""
        if not self._started:
            self.start()
        for b in make_or_bars(ticker=ticker, or_low=or_low, or_high=or_high,
                              or_minutes=or_minutes):
            self.feed_bar(b, ticker=ticker)

    # --- entry path ---

    def try_long(self, *, ticker: str, price: float,
                 portfolio_id: str = "main",
                 equity: Optional[float] = None,
                 ) -> EntryResult:
        """Attempt a LONG breakout entry. Caller is responsible for
        having pushed the OR window + a breakout bar BEFORE this call.

        `price` is both the 5-min close (signal) and the next-open
        (fill). Fine for tests since v10's geometry uses next_open as
        the fill; backtests can pass distinct values.
        """
        eq = equity if equity is not None else self.cfg.equity_per_portfolio.get(
            portfolio_id, 100_000.0)
        result = live_runtime.check_entry(
            portfolio_id=portfolio_id,
            ticker=ticker, side="long",
            five_min_close=price, next_open=price,
            equity=eq,
        )
        self.steps.append(ScenarioStep(
            kind="entry",
            detail={"side": "long", "ticker": ticker, "pid": portfolio_id,
                    "ok": result.ok, "reason_no": result.reason_no,
                    "shares": result.shares, "stop": result.stop,
                    "target": result.target},
        ))
        return result

    def try_short(self, *, ticker: str, price: float,
                  portfolio_id: str = "main",
                  equity: Optional[float] = None,
                  ) -> EntryResult:
        """Attempt a SHORT breakout entry. Same contract as try_long."""
        eq = equity if equity is not None else self.cfg.equity_per_portfolio.get(
            portfolio_id, 100_000.0)
        result = live_runtime.check_entry(
            portfolio_id=portfolio_id,
            ticker=ticker, side="short",
            five_min_close=price, next_open=price,
            equity=eq,
        )
        self.steps.append(ScenarioStep(
            kind="entry",
            detail={"side": "short", "ticker": ticker, "pid": portfolio_id,
                    "ok": result.ok, "reason_no": result.reason_no,
                    "shares": result.shares, "stop": result.stop,
                    "target": result.target},
        ))
        return result

    # --- exit path ---

    def check_exit(self, *, ticker: str, ticket_id: str,
                   bar: Bar, portfolio_id: str = "main",
                   ) -> ExitResult:
        result = live_runtime.check_exit(
            portfolio_id=portfolio_id, ticker=ticker, ticket_id=ticket_id,
            bar_high=bar.high, bar_low=bar.low, bar_close=bar.close,
            bar_bucket_min=bar.bucket_min,
        )
        self.steps.append(ScenarioStep(
            kind="exit",
            detail={"ticker": ticker, "ticket": ticket_id,
                    "exit": result.exit, "reason": result.reason,
                    "price": result.price, "bucket": bar.bucket_min},
        ))
        return result

    def walk_to_target(self, *, ticker: str, ticket_id: str, target: float,
                       start_bucket: int = 605,
                       portfolio_id: str = "main",
                       ) -> ExitResult:
        """Push a single bar whose high >= target. Returns the exit."""
        bar = make_exit_bar(bucket=start_bucket,
                            high=target * 1.001, low=target * 0.999,
                            close=target * 1.0005)
        return self.check_exit(ticker=ticker, ticket_id=ticket_id, bar=bar,
                               portfolio_id=portfolio_id)

    def walk_to_stop(self, *, ticker: str, ticket_id: str, stop: float,
                     start_bucket: int = 605,
                     portfolio_id: str = "main",
                     ) -> ExitResult:
        """Push a single bar whose low <= stop. Returns the exit."""
        bar = make_exit_bar(bucket=start_bucket,
                            high=stop * 1.001, low=stop * 0.999,
                            close=stop * 0.9995)
        return self.check_exit(ticker=ticker, ticket_id=ticket_id, bar=bar,
                               portfolio_id=portfolio_id)

    def force_eod(self, *, ticker: str, ticket_id: str,
                  price: float,
                  portfolio_id: str = "main",
                  ) -> ExitResult:
        """Push a 15:55 bar and check exit. Should trigger EOD flatten."""
        bar = make_exit_bar(bucket=EOD_BUCKET,
                            high=price * 1.0001, low=price * 0.9999,
                            close=price)
        return self.check_exit(ticker=ticker, ticket_id=ticket_id, bar=bar,
                               portfolio_id=portfolio_id)

    # --- introspection ---

    def snapshot(self) -> dict:
        return live_runtime.snapshot()

    def history(self) -> list[ScenarioStep]:
        return list(self.steps)


# ----- CLI ---------------------------------------------------------


def _scenario_golden_long(verbose: bool = False) -> bool:
    """Long breakout at OR_high * 1.005, RR=2.5 target hit."""
    cfg = SimulatorConfig(
        date_iso="2026-01-15", tickers=["AAPL"],
        vix_close_d1=18.0,
        ticker_open_today={"AAPL": 100.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    with SessionSimulator(cfg) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_long(ticker="AAPL", price=101.0)
        if not ent.ok:
            if verbose:
                print(f"FAIL entry: {ent.reason_no}")
            return False
        ex = sim.walk_to_target(ticker="AAPL", ticket_id=ent.ticket_id,
                                target=ent.target)
        if verbose:
            print(f"entry ok: shares={ent.shares} stop={ent.stop:.2f} "
                  f"target={ent.target:.2f}")
            print(f"exit: {ex.exit} reason={ex.reason} price={ex.price:.2f}")
        return ex.exit and ex.reason == "target"


def _basic_cfg(equity: float = 100_000.0, vix: float = 18.0,
                tickers: list[str] | None = None) -> SimulatorConfig:
    tks = tickers or ["AAPL"]
    return SimulatorConfig(
        date_iso="2026-01-15", tickers=tks, vix_close_d1=vix,
        ticker_open_today={tk: 100.0 for tk in tks},
        ticker_prev_close={tk: 100.0 for tk in tks},
        equity_per_portfolio={"main": equity},
    )


def _scenario_golden_short(verbose: bool = False) -> bool:
    """Short breakout at OR_low * 0.995, RR=2.5 target hit."""
    with SessionSimulator(_basic_cfg()) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="short",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_short(ticker="AAPL", price=99.0)
        if not ent.ok:
            if verbose: print(f"FAIL entry: {ent.reason_no}")
            return False
        ex = sim.walk_to_target(ticker="AAPL", ticket_id=ent.ticket_id,
                                target=ent.target)
        if verbose:
            print(f"entry ok: shares={ent.shares} stop={ent.stop:.2f} "
                  f"target={ent.target:.2f}")
            print(f"exit: {ex.exit} reason={ex.reason} price={ex.price:.2f}")
        return ex.exit and ex.reason == "target"


def _scenario_long_stop(verbose: bool = False) -> bool:
    """Long breakout, price reverses, stop hit."""
    with SessionSimulator(_basic_cfg()) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_long(ticker="AAPL", price=101.0)
        if not ent.ok:
            if verbose: print(f"FAIL entry: {ent.reason_no}")
            return False
        ex = sim.walk_to_stop(ticker="AAPL", ticket_id=ent.ticket_id,
                              stop=ent.stop)
        if verbose:
            print(f"entry ok shares={ent.shares} stop={ent.stop:.2f}")
            print(f"exit: {ex.exit} reason={ex.reason} price={ex.price:.2f}")
        return ex.exit and ex.reason == "stop"


def _scenario_short_stop(verbose: bool = False) -> bool:
    with SessionSimulator(_basic_cfg()) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="short",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_short(ticker="AAPL", price=99.0)
        if not ent.ok:
            if verbose: print(f"FAIL entry: {ent.reason_no}")
            return False
        ex = sim.walk_to_stop(ticker="AAPL", ticket_id=ent.ticket_id,
                              stop=ent.stop)
        if verbose:
            print(f"entry ok shares={ent.shares} stop={ent.stop:.2f}")
            print(f"exit: {ex.exit} reason={ex.reason} price={ex.price:.2f}")
        return ex.exit and ex.reason == "stop"


def _scenario_vix_kill(verbose: bool = False) -> bool:
    """VIX > 22 must block every entry today."""
    with SessionSimulator(_basic_cfg(vix=25.0)) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_long(ticker="AAPL", price=101.0)
        if verbose:
            print(f"entry: ok={ent.ok} reason={ent.reason_no}")
            snap = sim.snapshot()
            print(f"day_status: {snap.get('day_status', {})}")
        return not ent.ok


def _scenario_gap_skip(verbose: bool = False) -> bool:
    """Gap > 1.5% must block the ticker."""
    cfg = SimulatorConfig(
        date_iso="2026-01-15", tickers=["AAPL"], vix_close_d1=18.0,
        ticker_open_today={"AAPL": 102.0},
        ticker_prev_close={"AAPL": 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    with SessionSimulator(cfg) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=101.5, or_high=102.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=102.5, or_low=101.5),
                     ticker="AAPL")
        ent = sim.try_long(ticker="AAPL", price=103.0)
        if verbose:
            print(f"entry: ok={ent.ok} reason={ent.reason_no}")
        return not ent.ok


def _scenario_eod_flatten(verbose: bool = False) -> bool:
    """Open long position at 15:55 ET must be force-exited (eod)."""
    with SessionSimulator(_basic_cfg()) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        sim.feed_bar(make_breakout_bar(bucket=600, side="long",
                                       or_high=100.5, or_low=99.5),
                     ticker="AAPL")
        ent = sim.try_long(ticker="AAPL", price=101.0)
        if not ent.ok:
            if verbose: print(f"FAIL entry: {ent.reason_no}")
            return False
        ex = sim.force_eod(ticker="AAPL", ticket_id=ent.ticket_id,
                           price=101.5)
        if verbose:
            print(f"entry ok stop={ent.stop:.2f} target={ent.target:.2f}")
            print(f"exit: {ex.exit} reason={ex.reason} price={ex.price:.2f}")
        return ex.exit and ex.reason == "eod"


def _scenario_daily_kill(verbose: bool = False) -> bool:
    """Two stop-outs on $10k equity should trigger the daily-loss kill,
    blocking the third entry attempt."""
    with SessionSimulator(_basic_cfg(equity=10_000.0)) as sim:
        sim.start()
        sim.feed_or(ticker="AAPL", or_low=99.5, or_high=100.5)
        for i in range(2):
            sim.feed_bar(make_breakout_bar(bucket=600 + i * 5, side="long",
                                           or_high=100.5, or_low=99.5,
                                           push_pct=0.005 + i * 0.005),
                         ticker="AAPL")
            e = sim.try_long(ticker="AAPL", price=101.0 + i * 0.5,
                             equity=10_000.0)
            if not e.ok:
                if verbose: print(f"unexpected reject at iter {i}: {e}")
                return False
            sim.walk_to_stop(ticker="AAPL", ticket_id=e.ticket_id,
                             stop=e.stop, start_bucket=605 + i * 5)
        sim.feed_bar(make_breakout_bar(bucket=615, side="long",
                                       or_high=100.5, or_low=99.5,
                                       push_pct=0.015),
                     ticker="AAPL")
        ent3 = sim.try_long(ticker="AAPL", price=102.0, equity=10_000.0)
        if verbose:
            print(f"3rd entry: ok={ent3.ok} reason={ent3.reason_no}")
        return not ent3.ok


_SCENARIOS = {
    "golden_long": _scenario_golden_long,
    "golden_short": _scenario_golden_short,
    "long_stop": _scenario_long_stop,
    "short_stop": _scenario_short_stop,
    "vix_kill": _scenario_vix_kill,
    "gap_skip": _scenario_gap_skip,
    "eod_flatten": _scenario_eod_flatten,
    "daily_kill": _scenario_daily_kill,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orb_session_sim",
        description="v10 ORB scenario simulator (drives the live runtime "
                    "synthetically).",
    )
    parser.add_argument("--scenario", default="golden_long",
                        choices=sorted(_SCENARIOS.keys()),
                        help="Which built-in scenario to run.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print each step.")
    args = parser.parse_args(argv)
    fn = _SCENARIOS[args.scenario]
    ok = fn(verbose=args.verbose)
    print(f"scenario={args.scenario} ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
