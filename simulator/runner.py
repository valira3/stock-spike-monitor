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
from simulator.reporter import (
    ScenarioReporter,
    install_log_capture,
    uninstall_log_capture,
)
from simulator.scenarios import SCENARIOS, get_scenario, list_scenarios

logger = logging.getLogger(__name__)


# ---- Keystone production baseline ------------------------------------
#
# Mirrors the env block in CLAUDE.md "Keystone -- canonical production
# baseline" section. Applied via setdefault so scenario / operator
# overrides still win.

_KEYSTONE_DEFAULTS: Dict[str, str] = {
    # v10 ORB morning anchor
    "ORB_LIVE_MODE": "1",
    "ORB_OR_MINUTES": "30",
    "ORB_RR": "2.5",
    "ORB_RISK_PER_TRADE_PCT": "1.0",
    "ORB_RANGE_MIN_PCT": "0.008",
    "ORB_RANGE_MAX_PCT": "0.025",
    "ORB_MAX_TRADES_PER_DAY": "5",
    "ORB_MAX_CONCURRENT_RISK_DOLLARS": "2000",
    "ORB_DAILY_LOSS_KILL_PCT": "2.0",
    "ORB_ATR_STOP_MULT": "1.75",
    "ORB_ATR_LOOKBACK_5M": "14",
    "ORB_PARTIAL_PROFIT_AT_1R": "1",
    "ORB_MOVE_TO_BE_AFTER_1R": "1",
    "ORB_STOP_BUFFER_BPS": "5.0",
    "ORB_ENTRY_SLIPPAGE_BPS": "1.5",
    "ORB_EXIT_SLIPPAGE_BPS": "1.5",
    "ORB_STOP_KICK_BPS": "5.0",
    "ORB_SHORT_PENALTY_BPS": "1.0",
    "ORB_MAX_TRADE_NOTIONAL_PCT": "75",
    "ORB_SKIP_GAP_ABOVE_PCT": "1.5",
    "ORB_SKIP_VIX_ABOVE": "25.0",
    "ORB_SKIP_PRIOR_SPY_RET_LT_BPS": "-40.0",
    "ORB_SKIP_EARNINGS_WINDOW": "1",
    "ORB_TIME_CUTOFF_ET": "11:00",
    "ORB_EOD_CUTOFF_ET": "15:55",
    "ORB_ACCOUNT": "100000",
    "ORB_COMPOUND_DAILY": "1",
    "ORB_TICKER_SIDE_BLOCKLIST": "{}",
    "ORB_MAX_VWAP_DEV_BPS": "15.0",
    "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO",
    "ORB_POST_TRADE_COOLDOWN_MIN": "10",
    # v9.1 EOD reversal addon
    "ORB_EOD_REVERSAL_ENABLED": "1",
    "ORB_EOD_UNIVERSE": "ORCL,AAPL,MSFT,AVGO,NFLX,TSLA",
    "ORB_EOD_LONG_TICKERS": "ORCL,AAPL,MSFT,AVGO,TSLA",
    "ORB_EOD_SHORT_TICKERS": "ORCL,NFLX,AAPL,MSFT,TSLA",
    "ORB_EOD_TOP_N": "1",
    "ORB_EOD_NOTIONAL_PCT": "35",
    "ORB_EOD_ENTRY_ET": "15:00",
    "ORB_EOD_EXIT_ET": "15:56",
    "ORB_EOD_ENTRY_CUTOFF_ET": "15:51",
    "ORB_EOD_FIRE_BROKER": "0",  # simulator never reaches a real broker
}


# ----- session pacing ---------------------------------------------------

OR_START = 9 * 60 + 30
OR_END_30 = 10 * 60
EOD_BUCKET = 15 * 60 + 55
SESSION_END = 16 * 60


@dataclass
class SimulatorRunner:
    scenario: dict
    verbose: bool = False
    quiet: bool = False
    state: Dict[str, Any] = field(default_factory=dict)
    reporter: Optional[ScenarioReporter] = None
    _orig: Dict[str, Any] = field(default_factory=dict)
    _clock: Optional[SimulatedClock] = None
    _feeder: Optional[BarFeeder] = None
    _log_handler: Any = None

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
        """Apply config, build feeder, install clock + mocks + reporter."""
        # 1a. Keystone defaults -- match the production v10.0 + r17 baseline
        # documented in CLAUDE.md "Keystone -- canonical production baseline".
        # Each setdefault honors scenario/operator overrides while ensuring
        # the simulator always has a sane configuration that mirrors live.
        for k, v in _KEYSTONE_DEFAULTS.items():
            os.environ.setdefault(k, v)
        # 1b. Scenario overrides (win over Keystone defaults).
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
        # Scenario-injected failure registry (see simulator.mocks.errors).
        # Copy to mutate-safe dict so each scenario starts from its own
        # baseline (the counters in inject_failures decrement during the run).
        self.state["inject_failures"] = dict(self.scenario.get("inject_failures") or {})
        self._clock.install()

        # 4. Mocks.
        self._orig = install_all(self._feeder, self.state)

        # 5. Reporter + log capture. Capture the forensic audit trail
        # only when NOT in quiet mode (e.g. batch runs); in quiet mode
        # the audit lines balloon the output across hundreds of days.
        if self.reporter is None:
            self.reporter = ScenarioReporter(
                name=self.scenario["name"],
                description=self.scenario.get("description", ""),
                universe=list(universe),
                date=date,
                quiet=self.quiet,
                verbose=self.verbose,
            )
        self.reporter.header(self.scenario.get("config_overrides", {}))
        self._log_handler = install_log_capture(
            self.reporter, capture_audit=not self.quiet,
        )

    def teardown(self) -> None:
        uninstall_log_capture(self._log_handler)
        self._log_handler = None
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
        rep = self.reporter

        # ----- Premarket phase -----
        rep.phase("Premarket (boot / session start)")

        # Reset + boot.
        if hasattr(live_runtime, "_reset_for_testing"):
            live_runtime._reset_for_testing()
        if hasattr(live_runtime, "bootstrap"):
            live_runtime.bootstrap()

        date = self.scenario["date"]
        universe = self.scenario["universe"]

        # Seed open / PDC for ensure_session_started.
        # opens[t]  = today's 09:30 open (first bar of `date`)
        # pdcs[t]   = prior trading day's last bar close (read from the
        #             previous date directory under corpus_root)
        # Using bars[0].open for BOTH (a previous bug) zeroes the
        # per-ticker gap and silently disables ORB_SKIP_GAP_ABOVE_PCT.
        opens = {}
        pdcs = {}
        prior_date = _previous_corpus_date(date, self._feeder)
        for ticker in universe:
            bars = self._feeder._bars_by_ticker.get(ticker.upper(), [])
            opens[ticker] = float(bars[0].get("open", 100.0)) if bars else 100.0
            pdcs[ticker] = _prior_close_for(prior_date, ticker, opens[ticker])

        ok = live_runtime.ensure_session_started(
            date_iso=date,
            tickers=list(universe),
            vix_close_d1=18.0,
            ticker_open_today=opens,
            ticker_prev_close=pdcs,
            equity_per_portfolio={"main": 100_000.0, "val": 30_000.0, "gene": 100_000.0},
        )
        rep.line(f"ensure_session_started: date={date}  tickers={list(universe)}  ok={ok}")
        rep.line(f"opens     = " + ", ".join(f"{t}={opens[t]:.2f}" for t in universe))
        rep.line(f"prev_close= " + ", ".join(f"{t}={pdcs[t]:.2f}" for t in universe))

        # Walk minute-by-minute from 09:30 to 16:00 ET.
        # Use a 30-min OR by default; v10 keystone uses 30m.
        cfg_or_minutes = int(os.environ.get("ORB_OR_MINUTES", "30"))
        or_end_bucket = OR_START + cfg_or_minutes
        cutoff = _bucket_str_to_min(os.environ.get("ORB_TIME_CUTOFF_ET", "11:00"))
        # Keystone -- EOD reversal addon timing.
        eod_entry_bucket = _bucket_str_to_min(os.environ.get("ORB_EOD_ENTRY_ET", "15:00"))
        eod_cutoff_bucket = _bucket_str_to_min(os.environ.get("ORB_EOD_ENTRY_CUTOFF_ET", "15:51"))
        eod_exit_bucket = _bucket_str_to_min(os.environ.get("ORB_EOD_EXIT_ET", "15:56"))

        # Per-ticker session VWAP accumulators (cumulative typical price *
        # volume / cumulative volume from session open through current bar).
        vwap_pv: Dict[str, float] = {t: 0.0 for t in universe}
        vwap_v: Dict[str, float] = {t: 0.0 for t in universe}

        # Last-known close per ticker (drives EOD reversal current_prices).
        last_close: Dict[str, float] = {t: 0.0 for t in universe}

        rep.phase(f"OR Window (09:30 -> {_bucket_to_str(or_end_bucket)} ET)")
        _last_phase = "or"

        for bucket in range(OR_START, SESSION_END):
            self._clock.set_et(hour=bucket // 60, minute=bucket % 60)
            self._feed_minute(live_runtime, universe, bucket)

            # Per-ticker session-VWAP accumulator. Updated on every 1m bar
            # so check_entry gets a current value at 5m boundaries.
            for ticker in universe:
                bar = self._feeder.bar_at(ticker, bucket)
                if not bar:
                    continue
                close_px = float(bar.get("close", 0) or 0)
                vol = float(bar.get("total_volume") or bar.get("iex_volume") or 0)
                last_close[ticker] = close_px
                if vol > 0 and close_px > 0:
                    vwap_pv[ticker] += close_px * vol
                    vwap_v[ticker] += vol

            # OR-window progress: track per-ticker high/low.
            if bucket < or_end_bucket:
                for ticker in universe:
                    bar = self._feeder.bar_at(ticker, bucket)
                    if bar:
                        rep.on_or_bar(ticker, bucket,
                                      float(bar.get("high", 0) or 0),
                                      float(bar.get("low", 0) or 0))
            elif bucket == or_end_bucket:
                rep.on_or_complete()

            # Phase transitions for the report.
            if bucket == or_end_bucket and _last_phase != "entry":
                rep.phase(f"Entry Window ({_bucket_to_str(or_end_bucket)} -> {_bucket_to_str(cutoff)} ET)")
                _last_phase = "entry"
            elif bucket == cutoff + 1 and _last_phase != "manage":
                rep.phase(f"Management ({_bucket_to_str(cutoff + 1)} -> 15:55 ET)")
                _last_phase = "manage"
            elif bucket == EOD_BUCKET and _last_phase != "eod":
                rep.phase("EOD Flush (15:55 -> 16:00 ET)")
                _last_phase = "eod"

            # Entry window: from OR end to the operator-configured cutoff.
            # Keystone v10 ORB fires on 5-min CLOSE bars (not every 1m).
            # 5m close buckets after OR end = or_end_bucket, or_end_bucket+5,
            # +10, ... (the bucket index aligns with 5m boundaries because
            # OR_START=570 and or_end_bucket=600 are both multiples of 5).
            on_5m_boundary = (bucket - or_end_bucket) % 5 == 0
            if or_end_bucket <= bucket <= cutoff and on_5m_boundary:
                for ticker in universe:
                    bar = self._feeder.bar_at(ticker, bucket)
                    if not bar:
                        continue
                    or_state = rep._or_states.get(ticker)
                    if not or_state:
                        continue
                    or_high = or_state["high"]
                    or_low = or_state["low"]
                    close_px = float(bar["close"])
                    # Detect breakout direction; only call check_entry on
                    # genuine signals (don't pound the gate every minute).
                    side = None
                    if close_px > or_high:
                        side = "LONG"
                    elif close_px < or_low:
                        side = "SHORT"
                    if side is None:
                        continue
                    try:
                        # Pass session_vwap so the v9 chase-prevention
                        # filter activates (15bps cap on the 6 mega-caps).
                        sv = (vwap_pv[ticker] / vwap_v[ticker]
                              if vwap_v[ticker] > 0 else None)
                        result = live_runtime.check_entry(
                            portfolio_id="main",
                            ticker=ticker,
                            side=side,
                            five_min_close=close_px,
                            next_open=close_px,
                            equity=100_000.0,
                            session_vwap=sv,
                        )
                    except TypeError as exc:
                        # Signature mismatch is a programmer error -- surface
                        # it as a WARNING so the next session sees it instead
                        # of silently swallowing every call.
                        rep.on_warning(f"check_entry signature mismatch: {exc}")
                        continue
                    except Exception as exc:
                        rep.on_warning(f"check_entry({ticker}@{bucket}) raised: {exc}")
                        continue
                    if result and getattr(result, "ok", False):
                        # Stamp per-ticker context so the expectation
                        # evaluator can run a *real* per-ticker check
                        # against the bot's actual gates (not a SPY proxy).
                        ticker_open = opens.get(ticker, 0.0)
                        ticker_pdc = pdcs.get(ticker, 0.0)
                        gap_pct = (
                            ((ticker_open - ticker_pdc) / ticker_pdc) * 100.0
                            if ticker_pdc else 0.0
                        )
                        or_state = rep._or_states.get(ticker, {})
                        oh, ol = or_state.get("high", 0.0), or_state.get("low", 0.0)
                        or_mid = (oh + ol) / 2.0
                        or_pct = ((oh - ol) / or_mid) * 100.0 if or_mid else 0.0
                        fill_px = float(
                            getattr(result, "fill_price",
                                    getattr(result, "next_open", close_px)) or close_px
                        )
                        n_shares = int(getattr(result, "shares", 0) or 0)
                        entry = {
                            "ticker": ticker, "side": side,
                            "bucket": bucket,
                            "price": fill_px,
                            "stop": float(getattr(result, "stop", 0) or 0),
                            "target": float(getattr(result, "target", 0) or 0),
                            "shares": n_shares,
                            "ticker_gap_pct": round(gap_pct, 3),
                            "ticker_or_range_pct": round(or_pct, 3),
                        }
                        self.state["entries"].append(entry)
                        rep.on_entry(entry)
                        # v10.1: dispatch a mock-Alpaca order so the mock
                        # broker tracks the position + realized P&L. The
                        # mock fills immediately at the limit price; the
                        # admission's `fill_price` becomes the cost basis.
                        self._dispatch_mock_order(
                            ticker=ticker,
                            side="buy" if side == "LONG" else "sell",
                            qty=n_shares,
                            limit_price=fill_px,
                        )
                    else:
                        # Optional: surface the rejection reason for the
                        # first few attempts per ticker so the report tells
                        # us WHY entries didn't fire on a day they "should".
                        reason = getattr(result, "reason_no", "") if result else "no_result"
                        key = f"_seen_reject_{ticker}"
                        if not self.state.get(key) and reason:
                            self.state[key] = True
                            rep.line(f"[{_bucket_to_str(bucket)} ET] check_entry({ticker} {side}) rejected: {reason}")

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
                # Partial-at-1R fire: half-close, position stays open.
                if exit_res and getattr(exit_res, "partial", False):
                    p_shares = int(getattr(exit_res, "partial_shares", 0) or 0)
                    p_price = float(getattr(exit_res, "partial_price", 0) or 0)
                    if p_shares > 0 and p_price > 0:
                        # Determine the closing side from the existing
                        # mock-broker position (opposite to entry side).
                        pos = self.state.get("alpaca_positions", {}).get(ticker.upper())
                        side_close = "sell" if (pos and pos.side == "long") else "buy"
                        self._dispatch_mock_order(
                            ticker=ticker,
                            side=side_close,
                            qty=p_shares,
                            limit_price=p_price,
                        )
                        rep.on_exit({
                            "ticker": ticker, "reason": "partial_1R",
                            "bucket": bucket, "price": p_price,
                            "partial": True, "shares": p_shares,
                        })

                if exit_res and getattr(exit_res, "exit", False):
                    exit_evt = {
                        "ticker": ticker, "reason": exit_res.reason,
                        "bucket": bucket,
                        "price": float(getattr(exit_res, "price", 0) or 0),
                    }
                    self.state["exits"].append(exit_evt)
                    rep.on_exit(exit_evt)
                    # Close the full remaining position on the mock book.
                    self._dispatch_mock_close(ticker, exit_evt["price"])

            # ----- Keystone r17 EOD reversal addon ----------------------
            # Entry window 15:00 -> 15:50 ET (open one position per side
            # per portfolio). Exit window from 15:56 ET.
            self._run_eod_reversal_tick(
                live_runtime=live_runtime, bucket=bucket,
                last_close=last_close,
                eod_entry_bucket=eod_entry_bucket,
                eod_cutoff_bucket=eod_cutoff_bucket,
                eod_exit_bucket=eod_exit_bucket,
                rep=rep,
            )

    # ---- helpers ------------------------------------------------------

    def _run_eod_reversal_tick(self, *, live_runtime, bucket: int,
                                last_close: Dict[str, float],
                                eod_entry_bucket: int,
                                eod_cutoff_bucket: int,
                                eod_exit_bucket: int,
                                rep: ScenarioReporter) -> None:
        """Drive the r17 EOD reversal addon for one minute tick.

        Production flow (`engine/scan.py:_eod_reversal_pass`):
          - On the first tick in [entry, cutoff) per portfolio, call
            select_signals() to pick LONG/SHORT winners by ROD3, then
            admit() each pick.
          - On every tick after `exit_bucket`, close all open positions.
        """
        eod = live_runtime.get_eod_engine() if hasattr(live_runtime, "get_eod_engine") else None
        if eod is None or not eod.cfg.enabled:
            return

        # Use the bot's date_iso convention.
        date_iso = self.scenario["date"]
        try:
            eod.reset_for_session(date_iso)
        except Exception as exc:
            rep.on_warning(f"eod.reset_for_session raised: {exc}")
            return

        # Entry: fire once per portfolio inside [entry, cutoff).
        if eod_entry_bucket <= bucket < eod_cutoff_bucket and not eod.has_attempted("main"):
            # Build current_prices + prior_closes from the feeder.
            current_prices = {t: last_close.get(t, 0.0) for t in eod.cfg.universe
                              if last_close.get(t, 0.0) > 0}
            prior_closes: Dict[str, float] = {}
            # Read prior-day last bar close for each universe ticker.
            prior_date = _previous_corpus_date(date_iso, self._feeder)
            for t in eod.cfg.universe:
                prior_closes[t] = _prior_close_for(prior_date, t,
                                                   current_prices.get(t, 0.0))
            try:
                long_picks, short_picks = eod.select_signals(
                    current_prices=current_prices,
                    prior_closes=prior_closes,
                )
            except Exception as exc:
                rep.on_warning(f"eod.select_signals raised: {exc}")
                return

            iso = self._clock.now_utc.isoformat().replace("+00:00", "Z")
            # 35% notional per leg on a $100k book.
            equity = 100_000.0
            admitted = []
            for tk, rod_bps in long_picks:
                px = current_prices.get(tk, 0.0)
                if px <= 0:
                    continue
                pos = eod.admit(portfolio_id="main", ticker=tk, side="long",
                                entry_price=px, equity=equity,
                                rod3_bps=rod_bps, entry_iso=iso)
                if pos is None:
                    continue
                shares = int(pos.shares)
                self._dispatch_mock_order(ticker=tk, side="buy", qty=shares,
                                          limit_price=px)
                self.state["entries"].append({
                    "ticker": tk, "side": "LONG", "bucket": bucket,
                    "price": px, "stop": 0.0, "target": 0.0,
                    "shares": shares,
                    "ticker_gap_pct": 0.0,
                    "ticker_or_range_pct": 1.0,
                    "strategy": "eod_reversal",
                })
                admitted.append(("LONG", tk, rod_bps, px, shares))

            for tk, rod_bps in short_picks:
                px = current_prices.get(tk, 0.0)
                if px <= 0:
                    continue
                pos = eod.admit(portfolio_id="main", ticker=tk, side="short",
                                entry_price=px, equity=equity,
                                rod3_bps=rod_bps, entry_iso=iso)
                if pos is None:
                    continue
                shares = int(pos.shares)
                self._dispatch_mock_order(ticker=tk, side="sell", qty=shares,
                                          limit_price=px)
                self.state["entries"].append({
                    "ticker": tk, "side": "SHORT", "bucket": bucket,
                    "price": px, "stop": 0.0, "target": 0.0,
                    "shares": shares,
                    "ticker_gap_pct": 0.0,
                    "ticker_or_range_pct": 1.0,
                    "strategy": "eod_reversal",
                })
                admitted.append(("SHORT", tk, rod_bps, px, shares))

            eod.mark_attempted("main")
            for side, tk, rod, px, sh in admitted:
                rep.line(
                    f"[{_bucket_to_str(bucket)} ET]  EOD-ENTRY  "
                    f"{tk:6s} {side:5s} rod3={rod:+.0f}bps  @ {px:.2f}  shares={sh}"
                )

        # Exit: flatten EOD positions at/after the exit bucket.
        if bucket >= eod_exit_bucket:
            st = eod._states.get("main") if hasattr(eod, "_states") else None
            if st is None:
                return
            iso = self._clock.now_utc.isoformat().replace("+00:00", "Z")
            for tk in list(st.open_positions.keys()):
                px = last_close.get(tk, 0.0)
                leg = eod.close(portfolio_id="main", ticker=tk,
                                exit_price=px, exit_iso=iso, exit_reason="eod")
                if leg is None:
                    continue
                # Close the mock-broker position.
                self._dispatch_mock_close(tk, px)
                self.state["exits"].append({
                    "ticker": tk, "reason": "eod_reversal_close",
                    "bucket": bucket, "price": px,
                    "strategy": "eod_reversal",
                    "pnl_engine_side": float(leg.get("pnl", 0.0)),
                })
                rep.line(
                    f"[{_bucket_to_str(bucket)} ET]  EOD-EXIT   "
                    f"{tk:6s} {leg['side'].upper():5s} pnl=${leg['pnl']:+.2f} @ {px:.2f}"
                )

    def _dispatch_mock_order(self, *, ticker: str, side: str, qty: int,
                              limit_price: float) -> None:
        """Submit a mock-Alpaca order to track the fill in the in-process
        broker book. Used right after a v10 admission so realized P&L
        accrues on the mock side."""
        if qty <= 0 or limit_price <= 0:
            return
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import LimitOrderRequest
            client = TradingClient("sim", "sim", paper=True)
            req = LimitOrderRequest(
                symbol=ticker, qty=qty, side=side,
                type="limit", limit_price=limit_price,
            )
            client.submit_order(req)
        except Exception as exc:
            if self.reporter is not None:
                self.reporter.on_warning(f"mock-Alpaca order failed: {exc}")

    def _dispatch_mock_close(self, ticker: str, fallback_price: float) -> None:
        """Close any remaining mock-Alpaca position for `ticker` at the
        current bar's price (set via the simulator clock)."""
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient("sim", "sim", paper=True)
            client.close_position(ticker)
        except Exception as exc:
            if self.reporter is not None:
                self.reporter.on_warning(f"mock-Alpaca close failed: {exc}")

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


def _previous_corpus_date(date: str, feeder) -> Optional[str]:
    """Walk backward in the on-disk corpus to find the previous trading
    day. Falls back to None when nothing earlier is on disk."""
    root = os.environ.get("SIMULATOR_CORPUS_ROOT", "data")
    if not os.path.isdir(root):
        return None
    days = sorted(d for d in os.listdir(root)
                  if len(d) == 10 and d[4] == "-" and d[7] == "-"
                  and os.path.isdir(os.path.join(root, d)))
    if date not in days:
        return None
    i = days.index(date)
    return days[i - 1] if i > 0 else None


def _prior_close_for(prior_date: Optional[str], ticker: str,
                     fallback: float) -> float:
    """Read the prior day's last bar close for `ticker`. Falls back to
    `fallback` (typically today's open, giving 0% gap) when prior data
    is unavailable."""
    if not prior_date:
        return fallback
    from simulator.bar_feeder import BarFeeder
    root = os.environ.get("SIMULATOR_CORPUS_ROOT", "data")
    feeder = BarFeeder.from_corpus(prior_date, [ticker], corpus_root=root)
    bars = feeder._bars_by_ticker.get(ticker.upper(), [])
    if not bars:
        return fallback
    try:
        return float(bars[-1].get("close", fallback))
    except Exception:
        return fallback


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
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Show per-entry/exit detail in the progress stream")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Suppress phase progress; just print the summary")
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
    runner.quiet = args.quiet

    # Suppress the bot's INFO chatter; reporter captures WARNING+ on its own.
    logging.basicConfig(level=logging.ERROR)

    state = runner.run()
    expected = runner.scenario.get("expected") or {}
    passed = runner.reporter.summary(state, expected)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(_main())
