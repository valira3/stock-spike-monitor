"""Side enum + SideConfig lookup for the long/short collapse refactor (v4.8.0).

Stage B1 of the long/short harmonization. Defines the symbolic API used by
the collapsed `check_breakout` / `execute_breakout` / `close_breakout`
functions in trade_genius.py. The dataclass carries every value that
historically diverged between the long and short paths (Telegram labels,
state-dict attribute names, P&L sign, cash-flow direction) so the
collapsed functions can stay structurally side-agnostic.

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
    """Static, side-specific values used by the harmonized functions.

    String literals here MUST match the legacy long/short Telegram
    messages byte-for-byte. The differential test family in smoke_test.py
    asserts identical Telegram payloads against the legacy path.
    """
    side: Side
    # Telegram labels
    entry_label: str
    entry_emoji: str
    exit_emoji: str
    cash_word: str
    # OR polarity (descriptive only; logic uses the methods below)
    or_attr: str
    polarity_op: str
    # DI direction
    di_attr: str
    # State-dict names \u2014 looked up via globals() in trade_genius
    positions_attr: str
    daily_count_attr: str
    daily_date_attr: str
    trade_history_attr: str
    # Stop-cap helper name (resolved via getattr in trade_genius)
    capped_stop_fn_name: str

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

    def or_breakout(self, current_price: float, or_h: float, or_l: float) -> bool:
        if self.side.is_long:
            return current_price > or_h
        return current_price < or_l

    def di_aligned(self, plus_di: float, minus_di: float) -> bool:
        if self.side.is_long:
            return plus_di > minus_di
        return minus_di > plus_di


LONG = SideConfig(
    side=Side.LONG,
    entry_label="LONG ENTRY",
    entry_emoji="\U0001F4C8",   # chart up
    exit_emoji="\U0001F4B0",    # money bag
    cash_word="Cost",
    or_attr="or_high",
    polarity_op=">",
    di_attr="plus_di",
    positions_attr="positions",
    daily_count_attr="daily_entry_count",
    daily_date_attr="daily_entry_date",
    trade_history_attr="trade_history",
    capped_stop_fn_name="_capped_long_stop",
)

SHORT = SideConfig(
    side=Side.SHORT,
    entry_label="SHORT ENTRY",
    entry_emoji="\U0001FA78",   # drop of blood
    exit_emoji="\U0001F4B8",    # flying money
    cash_word="Proceeds",
    or_attr="or_low",
    polarity_op="<",
    di_attr="minus_di",
    positions_attr="short_positions",
    daily_count_attr="daily_short_entry_count",
    daily_date_attr="daily_short_entry_date",
    trade_history_attr="short_trade_history",
    capped_stop_fn_name="_capped_short_stop",
)


CONFIGS = {Side.LONG: LONG, Side.SHORT: SHORT}
