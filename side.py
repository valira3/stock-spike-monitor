"""Side enum + SideConfig lookup for the long/short collapse refactor.

v4.9.0 \u2014 Stage B2 final form. The unified `check_breakout` /
`execute_breakout` / `close_breakout` bodies in trade_genius.py read all
side-specific values from `CONFIGS[side]`. The legacy per-side bodies
were deleted in v4.9.0 along with the SSM_USE_COLLAPSED feature flag.

Pure module \u2014 no imports from trade_genius. Safe to import from anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def is_long(self) -> bool:
        return self is Side.LONG

    @property
    def is_short(self) -> bool:
        return self is Side.SHORT


@dataclass(frozen=True)
class SideConfig:
    """Static, side-specific values consumed by the unified breakout
    functions. String literals must match the legacy long/short Telegram
    payloads byte-for-byte \u2014 the synthetic-harness goldens are the
    enforcement mechanism.
    """
    side: Side
    # OR attribute name on the ticker row ("or_high" vs "or_low").
    or_attr: str
    # State-dict names \u2014 looked up via globals() in trade_genius
    positions_attr: str
    daily_count_attr: str
    daily_date_attr: str
    trade_history_attr: str
    # Stop-cap helper name (resolved via getattr in trade_genius)
    capped_stop_fn_name: str
    # v4.9.0 \u2014 fields added for the real Stage B2 collapse.
    # Lower-case "long"/"short" written into trade_history rows + log lines.
    history_side_label: str
    # Upper-case "LONG"/"SHORT" written into the persistent trade_log.
    log_side_label: str
    # "BUY" / "SHORT" verb used in paper_log on entry.
    paper_log_entry_verb: str
    # "SELL" / "COVER" verb on close (paper_log + trade_history.action).
    paper_log_close_verb: str
    # SKIP-line label \u2014 "long" for longs, "short" for shorts.
    skip_label: str
    # OR-side label inside SKIP / log strings: "OR High" vs "OR Low" (and
    # the mixed-case "or_hi"/"or_lo" abbreviations used in EXTENDED logs).
    or_side_label: str
    or_side_short_label: str
    # DI sign string used in the DI-rejected log line.
    di_sign_label: str
    # Stop-baseline label for the entry-Telegram message (variable arm).
    stop_baseline_label: str
    # Stop-cap label for the entry-Telegram message (when capped).
    stop_capped_label: str
    # _emit_signal kinds + entry reason.
    entry_signal_kind: str
    exit_signal_kind: str
    entry_signal_reason: str
    # Trail peak field name on the position dict ("trail_high" vs "trail_low").
    trail_peak_attr: str
    # Limit-price offset (+0.02 for longs, -0.02 for shorts).
    limit_offset: float

    def realized_pnl(self, entry: float, exit_: float, shares: int) -> float:
        if self.side.is_long:
            return (exit_ - entry) * shares
        return (entry - exit_) * shares

    def entry_cash_delta(self, shares: int, price: float) -> float:
        # Long entry debits cash; short entry credits cash.
        if self.side.is_long:
            return -shares * price
        return +shares * price

    def close_cash_delta(self, shares: int, price: float) -> float:
        # Long close credits sale proceeds; short close debits cover cost.
        if self.side.is_long:
            return +shares * price
        return -shares * price


LONG = SideConfig(
    side=Side.LONG,
    or_attr="or_high",
    positions_attr="positions",
    daily_count_attr="daily_entry_count",
    daily_date_attr="daily_entry_date",
    trade_history_attr="trade_history",
    capped_stop_fn_name="_capped_long_stop",
    history_side_label="long",
    log_side_label="LONG",
    paper_log_entry_verb="BUY",
    paper_log_close_verb="SELL",
    skip_label="long",
    or_side_label="OR High",
    or_side_short_label="or_hi",
    di_sign_label="DI+",
    stop_baseline_label="OR_High-$0.90",
    stop_capped_label="entry \u22120.75%",
    entry_signal_kind="ENTRY_LONG",
    exit_signal_kind="EXIT_LONG",
    entry_signal_reason="BREAKOUT",
    trail_peak_attr="trail_high",
    limit_offset=+0.02,
)

SHORT = SideConfig(
    side=Side.SHORT,
    or_attr="or_low",
    positions_attr="short_positions",
    daily_count_attr="daily_short_entry_count",
    daily_date_attr="daily_short_entry_date",
    trade_history_attr="short_trade_history",
    capped_stop_fn_name="_capped_short_stop",
    history_side_label="short",
    log_side_label="SHORT",
    paper_log_entry_verb="SHORT",
    paper_log_close_verb="COVER",
    skip_label="short",
    or_side_label="OR Low",
    or_side_short_label="or_lo",
    di_sign_label="DI-",
    stop_baseline_label="PDC+$0.90",
    stop_capped_label="entry +0.75%",
    entry_signal_kind="ENTRY_SHORT",
    exit_signal_kind="EXIT_SHORT",
    entry_signal_reason="WOUNDED_BUFFALO",
    trail_peak_attr="trail_low",
    limit_offset=-0.02,
)


CONFIGS = {Side.LONG: LONG, Side.SHORT: SHORT}
