"""v5.11.0 PR6 \u2014 canonical replay harness, consumes engine.scan directly.

This harness replaces the workspace-only `replay_v510_full_v4.py`
(36 KB, 791 lines) which carried a parallel re-implementation of
the per-minute scan/seed/phase logic that drifted from prod every
release. As of v5.11.0 the engine package (`engine.bars`,
`engine.seeders`, `engine.phase_machine`, `engine.callbacks`,
`engine.scan`) exposes a structural seam \u2014 the `EngineCallbacks`
Protocol \u2014 so backtests can drive the same scan body the bot runs
in prod, with broker / Telegram / clock calls routed through a
record-only mock instead of a parallel re-implementation.

Architecture:

  * `RecordOnlyCallbacks` satisfies the `engine.callbacks.EngineCallbacks`
    Protocol. Every method either appends to a recording list or
    returns a deterministic stub (e.g. `now_et()` returns the
    simulated wall-clock the driver has advanced to). No method
    talks to a broker, Telegram, or persistence.

  * `_install_fake_tg(...)` plants a `SimpleNamespace` in
    `sys.modules["trade_genius"]` with every global / helper that
    `engine.scan` reaches for via its `_tg()` indirection (see
    engine/scan.py top-of-file docstring for the canonical list).
    Helpers are stubbed to no-ops; globals are seeded with sane
    defaults (TRADE_TICKERS, _scan_paused=False, pdc={}, etc.).

  * `run_replay(date_str, tickers, bars_dir)` loads pre-market +
    RTH 1m JSONL bars from `<bars_dir>/<date>/`, walks per-minute
    from 09:35 ET to 15:55 ET, and on each tick calls
    `engine.scan.scan_loop(callbacks)`. The fetch-bars callback
    returns the last N bars as of the simulated clock so the
    engine sees "live" market data the same way prod does.

  * Real production data lives at /home/user/workspace/today_bars/
    (workspace, not in repo \u2014 too large). For CI / regression we
    ship a minimal fixture under tests/fixtures/replay_v511_minimal/
    with one ticker (AAPL) and a handful of pre-market + RTH bars.

Usage:

    # Real workspace data (full Apr 28 session):
    python -m backtest.replay_v511_full \\
        --date 2026-04-28 \\
        --bars-dir /home/user/workspace/today_bars

    # CI fixture:
    python -m backtest.replay_v511_full \\
        --date 2026-04-28 \\
        --bars-dir tests/fixtures/replay_v511_minimal

This harness is intentionally tiny relative to v4: the goal is to
prove the seam works, not to mirror v4's gate-by-gate ledger. P&L
and trade-pairing reports were v4's job; that layer can be rebuilt
on top of `RecordOnlyCallbacks.entries` / `.exits` once we trust
the seam end-to-end.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys as _sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CDT = ZoneInfo("America/Chicago")

logger = logging.getLogger("backtest.replay_v511")


# ---------------------------------------------------------------------------
# Bar loading
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_day_bars(bars_dir: Path, date_str: str, ticker: str) -> list[dict]:
    """Load pre-market + RTH 1m bars for `ticker` on `date_str`.

    Pre-market bars (if any) live under `<bars_dir>/<date>/premarket/<TICKER>.jsonl`;
    RTH bars live at `<bars_dir>/<date>/<TICKER>.jsonl`. Returned list is
    sorted ascending by UTC ts and tagged with a parsed `_dt` field.
    """
    rth = _load_jsonl(bars_dir / date_str / f"{ticker}.jsonl")
    pre = _load_jsonl(bars_dir / date_str / "premarket" / f"{ticker}.jsonl")
    bars = list(pre) + list(rth)
    out: list[dict] = []
    for b in bars:
        dt = _parse_ts(b.get("ts"))
        if dt is None:
            continue
        b = dict(b)
        b["_dt"] = dt
        out.append(b)
    out.sort(key=lambda b: b["_dt"])
    return out


# ---------------------------------------------------------------------------
# Record-only callbacks
# ---------------------------------------------------------------------------


@dataclass
class RecordOnlyCallbacks:
    """Satisfies `engine.callbacks.EngineCallbacks` without side effects.

    Attributes:
        clock_et: simulated wall-clock in ET. Driver advances this
            minute-by-minute before each `scan_loop` invocation.
        bars_by_ticker: full-day bars keyed by ticker. The fetch
            callback slices these up to and including `clock_et`.
        positions / short_positions: empty dicts here \u2014 the harness
            does not (yet) simulate holdings. `has_long` / `has_short`
            return False so every scan eligible ticker is treated as
            a fresh entry candidate.
        entries / exits / alerts / errors / ticks: append-only logs
            of everything the engine asks the callbacks to do. The
            regression test asserts these are populated.
    """

    clock_et: datetime = field(default_factory=lambda: datetime(2026, 4, 28, 9, 35, tzinfo=ET))
    bars_by_ticker: dict[str, list[dict]] = field(default_factory=dict)

    entries: list[dict] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)
    short_entries: list[dict] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    fetch_calls: list[str] = field(default_factory=list)
    ticks: list[datetime] = field(default_factory=list)

    # ---- Clock --------------------------------------------------------
    def now_et(self) -> datetime:
        return self.clock_et

    def now_cdt(self) -> datetime:
        return self.clock_et.astimezone(CDT)

    # ---- Market data --------------------------------------------------
    def fetch_1min_bars(self, ticker: str) -> Any:
        """Return the rolling parallel-array shape `engine.scan` expects.

        Includes only bars whose timestamp is <= the simulated clock
        so the engine sees the same forward-only view it would in prod.
        """
        self.fetch_calls.append(ticker)
        all_bars = self.bars_by_ticker.get(ticker.upper()) or []
        cutoff = self.clock_et.astimezone(timezone.utc)
        visible = [b for b in all_bars if b["_dt"] <= cutoff]
        if not visible:
            return None
        opens = [b.get("open") for b in visible]
        highs = [b.get("high") for b in visible]
        lows = [b.get("low") for b in visible]
        closes = [b.get("close") for b in visible]
        vols = [
            b.get("iex_volume") if b.get("iex_volume") is not None else b.get("volume")
            for b in visible
        ]
        timestamps = [int(b["_dt"].timestamp()) for b in visible]
        last_close = next((c for c in reversed(closes) if c is not None), None)
        return {
            "current_price": last_close,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volumes": vols,
            "timestamps": timestamps,
        }

    # ---- Position store -----------------------------------------------
    def get_position(self, ticker: str, side: str) -> dict | None:
        return None

    def has_long(self, ticker: str) -> bool:
        return False

    def has_short(self, ticker: str) -> bool:
        return False

    # ---- Position management ------------------------------------------
    def manage_positions(self) -> None:
        return None

    def manage_short_positions(self) -> None:
        return None

    # ---- Entry signals ------------------------------------------------
    def check_entry(self, ticker: str) -> tuple[bool, Any]:
        return (False, None)

    def check_short_entry(self, ticker: str) -> tuple[bool, Any]:
        return (False, None)

    # ---- Order execution ----------------------------------------------
    def execute_entry(self, ticker: str, price: float) -> None:
        self.entries.append(
            {
                "ts": self.clock_et.isoformat(),
                "ticker": ticker,
                "price": price,
                "side": "long",
            }
        )

    def execute_short_entry(self, ticker: str, price: float) -> None:
        self.short_entries.append(
            {
                "ts": self.clock_et.isoformat(),
                "ticker": ticker,
                "price": price,
                "side": "short",
            }
        )

    def execute_exit(self, ticker: str, side: str, price: float, reason: str) -> None:
        self.exits.append(
            {
                "ts": self.clock_et.isoformat(),
                "ticker": ticker,
                "side": side,
                "price": price,
                "reason": reason,
            }
        )

    # ---- Operator surface ---------------------------------------------
    def alert(self, msg: str) -> None:
        self.alerts.append(msg)

    def report_error(
        self, *, executor: str, code: str, severity: str, summary: str, detail: str
    ) -> None:
        self.errors.append(
            {
                "executor": executor,
                "code": code,
                "severity": severity,
                "summary": summary,
                "detail": detail,
            }
        )


# ---------------------------------------------------------------------------
# Fake `trade_genius` module \u2014 satisfies engine.scan's `_tg()` indirection
# ---------------------------------------------------------------------------


def _install_fake_tg(tickers: list[str]) -> SimpleNamespace:
    """Install a record-only stub in `sys.modules["trade_genius"]`.

    `engine.scan._tg()` resolves the live trade_genius module via
    `sys.modules.get("trade_genius") or sys.modules.get("__main__")`.
    For replay we need a stand-in with every global / helper the scan
    body touches; everything is a no-op that records the access.
    """
    fake = SimpleNamespace()

    # Globals
    fake.TRADE_TICKERS = list(tickers)
    fake.V561_INDEX_TICKER = "QQQ"
    fake._scan_idle_hours = False
    fake._scan_paused = False
    fake._current_mode = "REPLAY"
    fake._last_scan_time = None
    # v5.13.9 \u2014 _regime_bullish removed; scan.py no longer reads it.
    fake.positions = {}
    fake.short_positions = {}
    fake.pdc = {}
    fake._ws_consumer = None
    fake._QQQ_REGIME = SimpleNamespace(last_close=None, ema9=None)

    # No-op helpers \u2014 all wrapped in try/except inside engine.scan
    # so a missing attribute would crash the loop, but a no-op is fine.
    fake._refresh_market_mode = lambda: None
    fake._clear_cycle_bar_cache = lambda: None
    fake._v561_archive_qqq_bar = lambda *a, **kw: None
    fake._v512_archive_minute_bar = lambda *a, **kw: None
    fake._v590_qqq_regime_tick = lambda: None
    fake._v561_maybe_persist_or_snapshots = lambda *a, **kw: None
    fake._update_gate_snapshot = lambda *a, **kw: None
    fake._v520_mtm_ticker = lambda *a, **kw: None
    fake._opening_avwap = lambda *a, **kw: None

    # Stash a recorder for tests / debugging.
    fake._replay_marker = "v5.11.0-pr6-replay-fake-tg"

    _sys.modules["trade_genius"] = fake
    return fake


# ---------------------------------------------------------------------------
# Replay driver
# ---------------------------------------------------------------------------

DEFAULT_TICKERS = ["AAPL", "AMZN", "AVGO", "GOOG", "META", "MSFT", "NFLX", "NVDA", "ORCL", "TSLA"]


@dataclass
class ReplayResult:
    date: str
    tickers: list[str]
    minutes_processed: int
    callbacks: RecordOnlyCallbacks


def run_replay(
    date_str: str,
    tickers: list[str] | None = None,
    bars_dir: Path | str = Path("/home/user/workspace/today_bars"),
    *,
    start_hhmm: tuple[int, int] = (9, 35),
    end_hhmm: tuple[int, int] = (15, 55),
) -> ReplayResult:
    """Drive `engine.scan.scan_loop` per-minute over an archived day.

    Args:
        date_str: 'YYYY-MM-DD' \u2014 the session date.
        tickers: trade-universe override; defaults to the v5.11 Titan 10.
        bars_dir: parent dir containing `<date>/{TICKER}.jsonl` and
            optional `<date>/premarket/{TICKER}.jsonl` files.
        start_hhmm / end_hhmm: ET wall-clock window to step through.
    """
    bars_dir = Path(bars_dir)
    tickers = list(tickers or DEFAULT_TICKERS)
    universe = tickers + ["QQQ", "SPY"]

    # Load all bars up front, keyed by ticker.
    bars_by_ticker: dict[str, list[dict]] = {}
    for tk in universe:
        bars_by_ticker[tk] = load_day_bars(bars_dir, date_str, tk)

    # Fake trade_genius module so engine.scan._tg() finds something.
    _install_fake_tg(tickers)

    # Import after fake install so engine.scan resolves the stub when
    # it caches `_tg()` at first call.
    import engine.scan as _engine_scan

    # Build callbacks. Initial clock = start of window.
    yyyy, mm, dd = (int(p) for p in date_str.split("-"))
    start_dt = datetime(yyyy, mm, dd, start_hhmm[0], start_hhmm[1], tzinfo=ET)
    end_dt = datetime(yyyy, mm, dd, end_hhmm[0], end_hhmm[1], tzinfo=ET)

    cb = RecordOnlyCallbacks(
        clock_et=start_dt,
        bars_by_ticker=bars_by_ticker,
    )

    # Step minute-by-minute.
    cur = start_dt
    minutes = 0
    while cur <= end_dt:
        cb.clock_et = cur
        cb.ticks.append(cur)
        try:
            _engine_scan.scan_loop(cb)
        except Exception as e:
            logger.error("scan_loop crashed at %s: %s", cur.isoformat(), e)
            cb.errors.append(
                {
                    "executor": "replay_driver",
                    "code": "SCAN_LOOP_EXCEPTION",
                    "severity": "error",
                    "summary": f"scan_loop crashed at {cur.isoformat()}",
                    "detail": f"{type(e).__name__}: {str(e)[:200]}",
                }
            )
        minutes += 1
        cur = cur + timedelta(minutes=1)

    return ReplayResult(
        date=date_str,
        tickers=tickers,
        minutes_processed=minutes,
        callbacks=cb,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def format_report(result: ReplayResult) -> str:
    cb = result.callbacks
    pnl = 0.0
    paired = _pair_entries_exits(cb.entries, cb.exits)
    for p in paired:
        pnl += p["pnl"]
    lines = [
        f"# v5.11.0 replay (engine seam) \u2014 {result.date}",
        "",
        f"- universe: {', '.join(result.tickers)}",
        f"- minutes processed: {result.minutes_processed}",
        f"- fetch_1min_bars calls: {len(cb.fetch_calls)}",
        f"- alerts emitted: {len(cb.alerts)}",
        f"- errors logged: {len(cb.errors)}",
        f"- entries: {len(cb.entries)}  (long={len(cb.entries)} short={len(cb.short_entries)})",
        f"- exits:   {len(cb.exits)}",
        f"- paired round-trips: {len(paired)}  pnl=${pnl:+.2f}",
        "",
        "## Entries",
    ]
    for e in cb.entries:
        lines.append(f"- {e['ts']} {e['ticker']} long @ {e['price']}")
    lines.append("")
    lines.append("## Exits")
    for x in cb.exits:
        lines.append(f"- {x['ts']} {x['ticker']} {x['side']} @ {x['price']} ({x['reason']})")
    lines.append("")
    return "\n".join(lines)


def _pair_entries_exits(entries: list[dict], exits: list[dict]) -> list[dict]:
    """Greedy FIFO pairing on (ticker, side) for crude P&L."""
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in entries:
        by_key[(e["ticker"], e["side"])].append(dict(e))
    paired: list[dict] = []
    for x in exits:
        key = (x["ticker"], x["side"])
        if not by_key[key]:
            continue
        e = by_key[key].pop(0)
        sign = 1.0 if x["side"] == "long" else -1.0
        pnl = sign * (float(x["price"]) - float(e["price"]))
        paired.append(
            {
                "ticker": x["ticker"],
                "side": x["side"],
                "entry_ts": e["ts"],
                "exit_ts": x["ts"],
                "entry_price": e["price"],
                "exit_price": x["price"],
                "pnl": pnl,
            }
        )
    return paired


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backtest.replay_v511_full",
        description=(
            "v5.11.0 PR6 replay harness. Drives engine.scan.scan_loop "
            "per-minute over an archived day with a record-only callbacks "
            "mock. Replaces the workspace-only replay_v510_full_v4.py "
            "parallel re-implementation."
        ),
    )
    p.add_argument("--date", required=True, help="YYYY-MM-DD session date (e.g. 2026-04-28)")
    p.add_argument(
        "--bars-dir",
        default="/home/user/workspace/today_bars",
        help=(
            "Parent dir of <date>/{TICKER}.jsonl files. Default points "
            "at the workspace bar archive (NOT shipped in repo). For CI, "
            "use tests/fixtures/replay_v511_minimal."
        ),
    )
    p.add_argument(
        "--tickers",
        default=",".join(DEFAULT_TICKERS),
        help="Comma-separated trade universe override",
    )
    p.add_argument("--start", default="09:35", help="ET start time HH:MM (default 09:35)")
    p.add_argument("--end", default="15:55", help="ET end time HH:MM (default 15:55)")
    p.add_argument("--out", default=None, help="Optional output path for the markdown report")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    args = _build_parser().parse_args(argv)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    sh, sm = (int(x) for x in args.start.split(":"))
    eh, em = (int(x) for x in args.end.split(":"))
    result = run_replay(
        args.date,
        tickers=tickers,
        bars_dir=args.bars_dir,
        start_hhmm=(sh, sm),
        end_hhmm=(eh, em),
    )
    report = format_report(result)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    _sys.exit(main())
