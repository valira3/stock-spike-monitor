"""simulator.reporter -- format scenario progress + summary for the operator.

Two output modes:

  Default (-q / --quiet):
      Just the final summary block.

  Verbose (default when run on a TTY, force with -v):
      Phase headers + per-phase highlights + warnings + a final
      comparison table.

The reporter is callback-driven. The runner calls .on_phase(...),
.on_entry(...), .on_exit(...), .on_warning(...) etc. as the session
progresses; the reporter decides what to print.

All output is plain ASCII (no color, no emoji) so the report copies
cleanly into Slack / chat / logs.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TextIO


WIDTH = 70  # column width for section headers


# ---- Keystone config baseline (for config diff) ------------------------
#
# These mirror the values documented in CLAUDE.md under "Keystone --
# canonical production baseline". Any env override that differs from
# this dict gets flagged in the config-diff block.

_KEYSTONE_DEFAULTS: Dict[str, str] = {
    "ORB_OR_MINUTES": "30",
    "ORB_RR": "2.5",
    "ORB_RISK_PER_TRADE_PCT": "1.0",
    "ORB_RANGE_MIN_PCT": "0.008",
    "ORB_RANGE_MAX_PCT": "0.025",
    "ORB_ATR_STOP_MULT": "1.75",
    "ORB_ATR_LOOKBACK_5M": "14",
    "ORB_MAX_TRADES_PER_DAY": "5",
    "ORB_MAX_CONCURRENT_RISK_DOLLARS": "2000",
    "ORB_DAILY_LOSS_KILL_PCT": "2.0",
    "ORB_PARTIAL_PROFIT_AT_1R": "1",
    "ORB_MOVE_TO_BE_AFTER_1R": "1",
    "ORB_SKIP_GAP_ABOVE_PCT": "1.5",
    "ORB_SKIP_VIX_ABOVE": "25.0",
    "ORB_MAX_VWAP_DEV_BPS": "15.0",
    "ORB_MAX_VWAP_DEV_TICKERS": "META,MSFT,AAPL,AMZN,GOOG,AVGO",
    "ORB_POST_TRADE_COOLDOWN_MIN": "10",
    "ORB_TIME_CUTOFF_ET": "11:00",
    "ORB_EOD_CUTOFF_ET": "15:55",
    "ORB_LIVE_MODE": "1",
    "ORB_ACCOUNT": "100000",
}


# ---- Reporter ----------------------------------------------------------


@dataclass
class ScenarioReporter:
    name: str
    description: str = ""
    universe: List[str] = field(default_factory=list)
    date: str = ""
    quiet: bool = False
    verbose: bool = True
    out: TextIO = sys.stdout

    # Internal state.
    _phase: str = ""
    _phase_lines: List[str] = field(default_factory=list)
    _warnings: List[str] = field(default_factory=list)
    _audit_trail: List[str] = field(default_factory=list)
    _audit_in_phase: List[str] = field(default_factory=list)
    _entries: List[dict] = field(default_factory=list)
    _exits: List[dict] = field(default_factory=list)
    _or_states: Dict[str, dict] = field(default_factory=dict)  # per-ticker {high, low, bars_seen}
    _started: bool = False

    # ----- header ------------------------------------------------------

    def header(self, config_overrides: Dict[str, str]):
        if self.quiet:
            return
        self._w(_box(f"TradeGenius Simulator -- {self.name}"))
        if self.description:
            self._w(_wrap(self.description, WIDTH))
            self._w("")
        self._w(f"  Trading day:   {self.date}")
        self._w(f"  Universe:      {', '.join(self.universe)}")
        self._w(f"  Session:       09:30 ET -> 16:00 ET  (390 virtual minutes)")
        self._w("")
        diff = _config_diff(config_overrides)
        if diff:
            self._w("  Config overrides vs Keystone defaults:")
            for k, v_now, v_base in diff:
                self._w(f"    {k:36s} {v_base!r:>10s}  ->  {v_now!r}")
            self._w("")
        self._started = True

    # ----- phases ------------------------------------------------------

    def phase(self, name: str):
        """Open a new phase. Always flush the previous one (even if
        empty -- "(nothing notable)" is signal, not noise)."""
        if self.quiet:
            return
        if self._phase:
            self._flush_phase()
        self._phase = name
        self._phase_lines = []

    def line(self, text: str):
        """Add a line to the current phase."""
        if self.quiet:
            return
        self._phase_lines.append(text)

    def _flush_phase(self):
        if not self._phase:
            return
        self._w(_section_header(self._phase))
        if not self._phase_lines and not self._audit_in_phase:
            self._w("  (nothing notable)")
        else:
            for ln in self._phase_lines:
                self._w("  " + ln)
            if self._audit_in_phase:
                if self._phase_lines:
                    self._w("")
                self._w("  --- engine forensic trace ---")
                for ln in self._audit_in_phase:
                    self._w("  " + ln[:140])
        self._w("")
        self._phase_lines = []
        self._audit_in_phase = []

    # ----- structured events -------------------------------------------

    def on_warning(self, text: str):
        self._warnings.append(text.strip())

    def on_audit(self, text: str):
        """Per-bar forensic audit line (e.g. `[V79-ORB-ENTRY] AAPL LONG
        admit ...`). Accumulates per-phase so each section header shows
        what the bot decided during that window."""
        if not text:
            return
        self._audit_trail.append(text.strip())
        # Keep only the latest ~12 lines per phase to avoid drowning
        # the report with bar-by-bar feed_bar chatter.
        self._audit_in_phase.append(text.strip())
        if len(self._audit_in_phase) > 24:
            self._audit_in_phase = self._audit_in_phase[-24:]

    def on_or_bar(self, ticker: str, bucket: int, high: float, low: float):
        """Record OR-window bar progress. Used for the per-ticker OR
        formation summary at the end of the OR phase."""
        st = self._or_states.setdefault(ticker, {"high": high, "low": low,
                                                 "bars_seen": 0,
                                                 "first_bucket": bucket})
        st["high"] = max(st["high"], high)
        st["low"] = min(st["low"], low)
        st["bars_seen"] += 1
        st["last_bucket"] = bucket

    def on_or_complete(self):
        """Called when the OR window closes -- summarize per-ticker."""
        if self.quiet:
            return
        for ticker, st in self._or_states.items():
            rng = st["high"] - st["low"]
            mid = (st["high"] + st["low"]) / 2.0 or 1.0
            rng_pct = (rng / mid) * 100.0 if mid else 0.0
            self.line(
                f"{ticker:6s}  OR: {st['low']:.2f} - {st['high']:.2f}  "
                f"range {rng_pct:.2f}%   bars={st['bars_seen']}/30"
            )

    def on_entry(self, entry: dict):
        self._entries.append(entry)
        if self.quiet:
            return
        bk = _bucket_to_str(entry.get("bucket", 0))
        self.line(
            f"[{bk} ET]  ENTRY  {entry['ticker']:6s} {entry.get('side','?'):5s} "
            f"@ {entry.get('price', 0):.2f}  "
            f"stop={entry.get('stop', 0):.2f}  target={entry.get('target', 0):.2f}  "
            f"shares={entry.get('shares', 0)}"
        )

    def on_exit(self, exit_evt: dict):
        self._exits.append(exit_evt)
        if self.quiet:
            return
        bk = _bucket_to_str(exit_evt.get("bucket", 0))
        self.line(
            f"[{bk} ET]  EXIT   {exit_evt['ticker']:6s}  "
            f"reason={exit_evt.get('reason','?'):20s}  "
            f"@ {exit_evt.get('price', 0):.2f}"
        )

    # ----- summary ------------------------------------------------------

    def summary(self, state: Dict[str, Any], expected: Dict[str, Any]) -> bool:
        """Print the final summary block. Returns True if expectations
        passed, False otherwise."""
        # Flush the last phase first.
        if self._phase and not self.quiet:
            self._flush_phase()

        # Warnings (if any) get their own block.
        if self._warnings and not self.quiet:
            self._w(_section_header("Bot warnings during run"))
            seen = set()
            for w in self._warnings:
                if w in seen:
                    continue
                seen.add(w)
                self._w("  - " + _wrap(w, WIDTH - 4).replace("\n", "\n    "))
            self._w("")

        # The summary itself prints in both quiet and verbose mode.
        self._w(_box("Summary"))

        entries = state.get("entries", [])
        exits = state.get("exits", [])
        positions = state.get("alpaca_positions", {})
        realized = state.get("alpaca_realized_pl", {})
        telegram = state.get("telegram_sends", [])
        orders = state.get("alpaca_orders", [])
        fmp = state.get("fmp_calls", [])
        yahoo = state.get("yahoo_calls", [])

        self._w("Strategy outcomes")
        self._w(f"  Entries fired:           {len(entries):>4d}")
        self._w(f"  Exits taken:             {len(exits):>4d}")
        eod_ok = len(positions) == 0
        eod_mark = "" if eod_ok else "  WARN (should flush at EOD)"
        self._w(f"  Open at EOD:             {len(positions):>4d}{eod_mark}")
        total_pl = sum(realized.values())
        self._w(f"  Realized P&L:            ${total_pl:>+10.2f}")
        if realized:
            for sym, pl in sorted(realized.items()):
                self._w(f"    {sym:6s}                 ${pl:>+10.2f}")
        self._w("")

        self._w("Service interactions")
        self._w(f"  Alpaca orders:           {len(orders):>4d}")
        self._w(f"  FMP calls:               {len(fmp):>4d}")
        self._w(f"  Yahoo calls:             {len(yahoo):>4d}")
        self._w(f"  Telegram sends:          {len(telegram):>4d}")
        if telegram and self.verbose:
            self._w("  Telegram messages:")
            for s in telegram[:8]:
                txt = (s.get("text") or "")[:50].replace("\n", " ")
                self._w(f"    chat={s.get('chat_id')!s:>14s}  text={txt!r}")
            if len(telegram) > 8:
                self._w(f"    ... ({len(telegram) - 8} more)")
        self._w("")

        # Expectations comparison.
        failures = self._check_expectations(state, expected)
        if expected:
            self._w("Expectations")
            self._w(_format_expectation_table(state, expected))
            self._w("")

        if failures:
            self._w("Verdict:  FAIL")
            self._w("")
            for f in failures:
                self._w(f"  FAIL: {f}")
        else:
            self._w("Verdict:  PASS")
        self._w("")
        return not failures

    # ----- helpers -----------------------------------------------------

    def _w(self, text: str):
        print(text, file=self.out)

    def _check_expectations(self, state, expected) -> List[str]:
        out: List[str] = []
        n_entries = len(state.get("entries", []))
        n_exits = len(state.get("exits", []))
        n_tg = len(state.get("telegram_sends", []))
        if "min_entries" in expected and n_entries < expected["min_entries"]:
            out.append(f"min_entries={expected['min_entries']} got {n_entries}")
        if "max_entries" in expected and n_entries > expected["max_entries"]:
            out.append(f"max_entries={expected['max_entries']} got {n_entries}")
        if "min_exits" in expected and n_exits < expected["min_exits"]:
            out.append(f"min_exits={expected['min_exits']} got {n_exits}")
        if "max_exits" in expected and n_exits > expected["max_exits"]:
            out.append(f"max_exits={expected['max_exits']} got {n_exits}")
        if "telegram_sends_max" in expected and n_tg > expected["telegram_sends_max"]:
            out.append(f"telegram_sends_max={expected['telegram_sends_max']} got {n_tg}")
        return out


# ---- helpers -----------------------------------------------------------


def _config_diff(overrides: Dict[str, str]) -> List[tuple]:
    """Return [(key, override_val, keystone_default), ...] for entries
    that differ from the Keystone baseline. Keys not in Keystone are
    skipped (those are scenario-specific knobs)."""
    out = []
    for k, v in (overrides or {}).items():
        base = _KEYSTONE_DEFAULTS.get(k)
        if base is None:
            continue
        if str(v) != str(base):
            out.append((k, str(v), str(base)))
    return out


def _format_expectation_table(state, expected) -> str:
    """Render the per-rule comparison as an aligned table."""
    rows = []
    n_entries = len(state.get("entries", []))
    n_exits = len(state.get("exits", []))
    n_tg = len(state.get("telegram_sends", []))
    if "min_entries" in expected:
        passed = n_entries >= expected["min_entries"]
        rows.append(("min_entries", str(expected["min_entries"]), str(n_entries), passed))
    if "max_entries" in expected:
        passed = n_entries <= expected["max_entries"]
        rows.append(("max_entries", str(expected["max_entries"]), str(n_entries), passed))
    if "min_exits" in expected:
        passed = n_exits >= expected["min_exits"]
        rows.append(("min_exits", str(expected["min_exits"]), str(n_exits), passed))
    if "max_exits" in expected:
        passed = n_exits <= expected["max_exits"]
        rows.append(("max_exits", str(expected["max_exits"]), str(n_exits), passed))
    if "telegram_sends_max" in expected:
        passed = n_tg <= expected["telegram_sends_max"]
        rows.append(("telegram_sends_max", str(expected["telegram_sends_max"]), str(n_tg), passed))
    if not rows:
        return "  (no expectations declared)"
    lines = ["  rule                       expected      actual    status"]
    lines.append("  " + "-" * (WIDTH - 4))
    for k, exp, act, ok in rows:
        status = "PASS" if ok else "FAIL"
        lines.append(f"  {k:24s}   {exp:>10s}   {act:>7s}    {status}")
    return "\n".join(lines)


def _section_header(name: str) -> str:
    bar = "-" * 4
    inner = f" {name} "
    total_after = max(0, WIDTH - len(bar) - len(inner))
    return f"\n{bar}{inner}{'-' * total_after}"


def _box(title: str) -> str:
    line = "=" * WIDTH
    centered = title.center(WIDTH)
    return f"\n{line}\n{centered}\n{line}"


def _bucket_to_str(bucket: int) -> str:
    return f"{bucket // 60:02d}:{bucket % 60:02d}"


def _wrap(text: str, width: int) -> str:
    """Simple word-wrap, no fancy hyphenation."""
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines)


# ---- log capture -------------------------------------------------------


# Forensic-log tags the bot emits at INFO. Routed to the reporter's
# audit trail so the operator can correlate each decision step
# against the strategy:
#
#   V79-ORB-*    morning ORB engine boot, OR lock, gates, admit/reject
#   V900-*       per-bar admission filters (mbr, vwap-chase, spy-regime)
#   V10-FIRE     dispatch to executor
#   V10-FIRE-OK  fill confirmed
#   V81-*        partial-at-1R fire + BE-stop move
#   V910-EOD-*   EOD reversal addon
#   V611-*       regime-B short amplification
FORENSIC_PREFIXES = (
    "[V79-ORB-", "[V900-", "[V10-FIRE", "[V81-", "[V910-EOD-",
    "[V611-", "[V79-", "[V10-", "[V73", "[V74", "[V90",
)


def install_log_capture(reporter: ScenarioReporter,
                        capture_audit: bool = True):
    """Hook the bot's log records into the reporter:

      - WARNING+ go to the reporter's `warnings` list (shown in summary)
      - INFO records matching one of FORENSIC_PREFIXES go to the
        per-bucket audit trail (shown inline in each phase) when
        `capture_audit=True` (the default)

    Returns the handler so the caller can uninstall after the scenario.
    """
    import logging

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            try:
                msg = self.format(record)
            except Exception:
                return
            if record.levelno >= logging.WARNING:
                reporter.on_warning(msg)
                return
            if not capture_audit:
                return
            for pfx in FORENSIC_PREFIXES:
                if pfx in msg:
                    reporter.on_audit(msg)
                    return

    h = _CaptureHandler(level=logging.INFO if capture_audit else logging.WARNING)
    h.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    if capture_audit and root.level > logging.INFO:
        h._prev_root_level = root.level  # type: ignore[attr-defined]
        root.setLevel(logging.INFO)
    return h


def uninstall_log_capture(handler):
    if handler is None:
        return
    import logging
    try:
        logging.getLogger().removeHandler(handler)
    except Exception:
        pass
