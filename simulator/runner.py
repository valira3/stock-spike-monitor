"""simulator.runner -- orchestrate a full scenario.

Run order (per scenario):

  1. Apply config_overrides via os.environ.
  2. Build the BarFeeder from corpus or synthetic builder.
  3. Install SimulatedClock at the scenario start time.
  4. Install all mocks (alpaca, fmp, yahoo, telegram, railway).
  5. Import / reset orb.live_runtime fresh.
  6. Boot a session: configure runtime, start_new_session.
  7. Tick by minute: feed each bar through live_runtime.feed_bar(),
     attempt entries, then attempt exits. Advance the clock.
  8. EOD flush.
  9. Compare scenario_state to scenario["expected"]. Print summary.
 10. Uninstall mocks + clock.

CLI:
  python -m simulator.runner --list
  python -m simulator.runner --scenario golden_orb_long
  python -m simulator.runner --scenario golden_orb_long --verbose
  python -m simulator.runner --replay 2026-05-15 --tickers AAPL,MSFT
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from simulator.bar_feeder import BarFeeder
from simulator.clock import SimulatedClock
from simulator.mocks import install_all, uninstall_all
from simulator.scenarios import SCENARIOS, get_scenario, list_scenarios

logger = logging.getLogger(__name__)


# ----- session pacing ---------------------------------------------------

OR_START = 9 * 60 + 30
OR_END_30 = 10 * 60
EOD_BUCKET = 15 * 60 + 55
SESSION_END = 16 * 60


@dataclass
class SimulatorRunner:
    scenario: dict
    verbose: bool = False
    state: Dict[str, Any] = field(default_factory=dict)
    _orig: Dict[str, Any] = field(default_factory=dict)
    _clock: Optional[SimulatedClock] = None
    _feeder: Optional[BarFeeder] = None

    # ---- factories ----------------------------------------------------

    @classmethod
    def from_scenario(cls, name: str, verbose: bool = False) -> "SimulatorRunner":
        return cls(scenario=get_scenario(name), verbose=verbose)

    @classmethod
    def from_replay(cls, date: str, tickers: List[str], verbose: bool = False) -> "SimulatorRunner":
        scenario = {
            "name": f"replay-{date}",
            "description": f"Historical replay of {date} for {','.join(tickers)}",
            "date": date,
            "universe": tickers,
            "bars": None,  # signal to read from corpus
            "config_overrides": {
                "ORB_LIVE_MODE": "1",
                "ORB_ACCOUNT": "100000",
                "ORB_TICKER_SIDE_BLOCKLIST": "{}",
            },
            "expected": {},
        }
        return cls(scenario=scenario, verbose=verbose)

    # ---- lifecycle ----------------------------------------------------

    def setup(self) -> None:
        """Apply config, build feeder, install clock + mocks."""
        # 1. Config overrides.
        for k, v in self.scenario.get("config_overrides", {}).items():
            os.environ[k] = str(v)
        os.environ.setdefault("SIMULATOR_MODE", "1")
        # Force in-process state -- no /data volume writes.
        os.environ.setdefault("TG_DATA_ROOT", "/tmp/simulator_data")
        os.makedirs(os.environ["TG_DATA_ROOT"], exist_ok=True)

        # 2. Bar feeder.
        date = self.scenario["date"]
        universe = self.scenario["universe"]
        bars_builder = self.scenario.get("bars")
        if callable(bars_builder):
            bars_map = bars_builder(date)
            self._feeder = BarFeeder.from_synthetic(date, bars_map)
        else:
            self._feeder = BarFeeder.from_corpus(
                date, universe, corpus_root=os.environ.get("SIMULATOR_CORPUS_ROOT", "data"),
            )

        # 3. Clock + state.
        self._clock = SimulatedClock.at_et(date=date, hour=9, minute=25)
        self.state["clock"] = self._clock
        self.state["bar_feeder"] = self._feeder
        self.state["scenario_name"] = self.scenario["name"]
        self.state["entries"] = []
        self.state["exits"] = []
        self.state["log"] = []
        self._clock.install()

        # 4. Mocks.
        self._orig = install_all(self._feeder, self.state)

    def teardown(self) -> None:
        uninstall_all(self._orig)
        if self._clock is not None:
            self._clock.uninstall()

    # ---- the actual scenario run --------------------------------------

    def run(self) -> Dict[str, Any]:
        self.setup()
        try:
            self._run_session()
        finally:
            self.teardown()
        return self.state

    def _run_session(self) -> None:
        """Drive orb.live_runtime through one trading day.

        Uses the same API surface as tools/orb_session_sim.SessionSimulator:
        bootstrap -> ensure_session_started -> feed_bar (one per minute)
        -> check_entry per ticker between OR end and time cutoff ->
        check_exit_by_ticker every minute.
        """
        import orb.live_runtime as live_runtime  # noqa: WPS433

        # Reset + boot.
        if hasattr(live_runtime, "_reset_for_testing"):
            live_runtime._reset_for_testing()
        if hasattr(live_runtime, "bootstrap"):
            live_runtime.bootstrap()

        date = self.scenario["date"]
        universe = self.scenario["universe"]

        # Seed open / PDC for ensure_session_started. Use the first bar's
        # open for both -- close enough for simulator purposes.
        opens = {}
        pdcs = {}
        for ticker in universe:
            bars = self._feeder._bars_by_ticker.get(ticker.upper(), [])
            if bars:
                opens[ticker] = float(bars[0].get("open", 100.0))
                pdcs[ticker] = float(bars[0].get("open", 100.0))
            else:
                opens[ticker] = 100.0
                pdcs[ticker] = 100.0

        ok = live_runtime.ensure_session_started(
            date_iso=date,
            tickers=list(universe),
            vix_close_d1=18.0,
            ticker_open_today=opens,
            ticker_prev_close=pdcs,
            equity_per_portfolio={"main": 100_000.0, "val": 30_000.0, "gene": 100_000.0},
        )
        if self.verbose:
            self._log(f"session start: {date} tickers={universe} ok={ok}")

        # Walk minute-by-minute from 09:30 to 16:00 ET.
        # Use a 30-min OR by default; v10 keystone uses 30m.
        cfg_or_minutes = int(os.environ.get("ORB_OR_MINUTES", "30"))
        or_end_bucket = OR_START + cfg_or_minutes
        cutoff = _bucket_str_to_min(os.environ.get("ORB_TIME_CUTOFF_ET", "11:00"))

        for bucket in range(OR_START, SESSION_END):
            self._clock.set_et(hour=bucket // 60, minute=bucket % 60)
            self._feed_minute(live_runtime, universe, bucket)

            # Entry window: from OR end to the operator-configured cutoff.
            if or_end_bucket <= bucket <= cutoff:
                for ticker in universe:
                    bar = self._feeder.bar_at(ticker, bucket)
                    if not bar:
                        continue
                    try:
                        result = live_runtime.check_entry(
                            portfolio_id="main",
                            ticker=ticker,
                            bar_high=float(bar["high"]),
                            bar_low=float(bar["low"]),
                            bar_open=float(bar["open"]),
                            bar_close=float(bar["close"]),
                            bar_bucket_min=bucket,
                            equity=100_000.0,
                        )
                    except Exception as exc:
                        self._log(f"check_entry({ticker}@{bucket}) raised: {exc}")
                        continue
                    if result and getattr(result, "ok", False):
                        side = getattr(result, "side", "LONG")
                        self.state["entries"].append({
                            "ticker": ticker, "side": str(side),
                            "bucket": bucket,
                            "price": float(getattr(result, "fill_price", 0) or 0),
                            "stop": float(getattr(result, "stop", 0) or 0),
                            "target": float(getattr(result, "target", 0) or 0),
                            "shares": int(getattr(result, "shares", 0) or 0),
                        })
                        if self.verbose:
                            self._log(
                                f"[{_bucket_to_str(bucket)} ET] ENTRY {ticker} {side} "
                                f"fill={result.fill_price:.2f} stop={result.stop:.2f} "
                                f"target={result.target:.2f}"
                            )

            # Exit check on any open position.
            for ticker in universe:
                bar = self._feeder.bar_at(ticker, bucket)
                if not bar:
                    continue
                try:
                    exit_res = live_runtime.check_exit_by_ticker(
                        portfolio_id="main",
                        ticker=ticker,
                        bar_high=float(bar["high"]),
                        bar_low=float(bar["low"]),
                        bar_close=float(bar["close"]),
                        bar_bucket_min=bucket,
                    )
                except Exception:
                    continue
                if exit_res and getattr(exit_res, "exit", False):
                    self.state["exits"].append({
                        "ticker": ticker, "reason": exit_res.reason,
                        "bucket": bucket,
                        "price": float(getattr(exit_res, "price", 0) or 0),
                    })
                    if self.verbose:
                        self._log(
                            f"[{_bucket_to_str(bucket)} ET] EXIT {ticker} "
                            f"reason={exit_res.reason} @ {exit_res.price:.2f}"
                        )

    # ---- helpers ------------------------------------------------------

    def _feed_minute(self, live_runtime, universe, bucket):
        for ticker in universe:
            bar = self._feeder.bar_at(ticker, bucket)
            if not bar:
                continue
            try:
                live_runtime.feed_bar(
                    ticker=ticker,
                    bar_high=float(bar["high"]),
                    bar_low=float(bar["low"]),
                    bar_open=float(bar["open"]),
                    bar_close=float(bar["close"]),
                    bar_volume=float(bar.get("total_volume") or bar.get("iex_volume") or 0),
                    bar_bucket_min=bucket,
                )
            except Exception as exc:
                self._log(f"feed_bar({ticker}@{bucket}) raised: {exc}")

    def _log(self, msg: str):
        self.state["log"].append(msg)
        if self.verbose:
            print(msg)


# ----- module helpers ----------------------------------------------------


def _fresh_runtime():
    """Import (or re-import) orb.live_runtime so module-level caches are
    clean per scenario."""
    name = "orb.live_runtime"
    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)
    return sys.modules[name]


def _build_config(live_runtime):
    """Call live_runtime._build_config_from_env (the same boot path
    production uses)."""
    if hasattr(live_runtime, "_build_config_from_env"):
        return live_runtime._build_config_from_env()
    # Older shape -- just call OrbConfig().
    return live_runtime.OrbConfig()


def _bucket_str_to_min(s: str) -> int:
    try:
        hh, mm = s.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return 11 * 60  # default 11:00 ET


def _bucket_to_str(bucket: int) -> str:
    return f"{bucket // 60:02d}:{bucket % 60:02d}"


# ----- CLI --------------------------------------------------------------


def _main(argv=None):
    p = argparse.ArgumentParser(description="TradeGenius simulator runner")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--scenario", help="Run a built-in scenario by name")
    g.add_argument("--replay", help="Historical replay for YYYY-MM-DD")
    g.add_argument("--list", action="store_true", help="List built-in scenarios")
    p.add_argument("--tickers", help="Comma-separated tickers (for --replay)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    if args.list:
        for name in list_scenarios():
            s = SCENARIOS[name]
            print(f"  {name:24s}  {s['description']}")
        return 0

    if args.scenario:
        runner = SimulatorRunner.from_scenario(args.scenario, verbose=args.verbose)
    else:
        tickers = [t.strip().upper() for t in (args.tickers or "AAPL").split(",") if t.strip()]
        runner = SimulatorRunner.from_replay(args.replay, tickers, verbose=args.verbose)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    state = runner.run()

    print("\n===== Scenario Summary =====")
    print(f"name:           {state.get('scenario_name')}")
    print(f"entries:        {len(state.get('entries', []))}")
    print(f"exits:          {len(state.get('exits', []))}")
    print(f"telegram sends: {len(state.get('telegram_sends', []))}")
    print(f"alpaca orders:  {len(state.get('alpaca_orders', []))}")
    print(f"fmp calls:      {len(state.get('fmp_calls', []))}")
    print(f"yahoo calls:    {len(state.get('yahoo_calls', []))}")
    positions = state.get("alpaca_positions", {})
    print(f"open positions: {len(positions)}")
    realized = state.get("alpaca_realized_pl", {})
    if realized:
        total = sum(realized.values())
        print(f"realized P&L:   ${total:+.2f}  ({realized})")

    if args.verbose and state.get("entries"):
        print("\n--- Entries ---")
        for e in state["entries"]:
            print(f"  {_bucket_to_str(e['bucket'])} ET  {e['ticker']:6s} {e['side']:5s} @ {e['price']:.2f}")
    if args.verbose and state.get("exits"):
        print("\n--- Exits ---")
        for x in state["exits"]:
            print(f"  {_bucket_to_str(x['bucket'])} ET  {x['ticker']:6s} {x['reason']:20s} @ {x['price']:.2f}")

    expected = runner.scenario.get("expected") or {}
    failures = _validate(state, expected)
    if failures:
        print("\n--- Expectation failures ---")
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("\n--- Expectations: PASS ---")
    return 0


def _validate(state, expected):
    out = []
    n_entries = len(state.get("entries", []))
    if "min_entries" in expected and n_entries < expected["min_entries"]:
        out.append(f"min_entries={expected['min_entries']} got {n_entries}")
    if "max_entries" in expected and n_entries > expected["max_entries"]:
        out.append(f"max_entries={expected['max_entries']} got {n_entries}")
    n_sends = len(state.get("telegram_sends", []))
    if "telegram_sends_max" in expected and n_sends > expected["telegram_sends_max"]:
        out.append(f"telegram_sends_max={expected['telegram_sends_max']} got {n_sends}")
    return out


if __name__ == "__main__":
    sys.exit(_main())
