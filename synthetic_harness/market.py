"""SyntheticMarket — deterministic market-data source.

Replaces fetch_1min_bars / get_fmp_quote / tiger_di / _tiger_two_bar_*
in trade_genius for the duration of a scenario.

The scenario timeline supplies a per-ticker frame the harness exposes;
the harness can step the timeline forward via advance_to(ts).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class TickerFrame:
    """Snapshot of one ticker at a moment in time.

    bars_1min mirrors the dict returned by trade_genius.fetch_1min_bars.
    """
    bars_1min: dict
    quote: dict | None = None
    di: tuple = (None, None)
    two_bar_long: bool | None = None
    two_bar_short: bool | None = None


@dataclass
class SyntheticMarket:
    """Per-ticker market data accessible to mocked trade_genius funcs."""
    frames: dict[str, TickerFrame] = field(default_factory=dict)

    def set_frame(self, ticker: str, frame: TickerFrame) -> None:
        self.frames[ticker] = frame

    def update_price(self, ticker: str, current_price: float) -> None:
        f = self.frames.get(ticker)
        if not f:
            return
        f.bars_1min["current_price"] = current_price
        if f.quote is not None:
            f.quote["price"] = current_price

    # ---- mocked accessors (signatures match trade_genius) ----

    def fetch_1min_bars(self, ticker: str):
        f = self.frames.get(ticker)
        return f.bars_1min if f else None

    def get_fmp_quote(self, ticker: str):
        f = self.frames.get(ticker)
        return f.quote if f else None

    def tiger_di(self, ticker: str):
        f = self.frames.get(ticker)
        return f.di if f else (None, None)

    def two_bar_long(self, closes, or_h):
        # If the scenario wants to override, prefer that. Otherwise
        # delegate to the real predicate (closes vs or_h).
        if not closes or len(closes) < 2:
            return False
        return closes[-1] > or_h and closes[-2] > or_h

    def two_bar_short(self, closes, or_l):
        if not closes or len(closes) < 2:
            return False
        return closes[-1] < or_l and closes[-2] < or_l


def make_long_breakout_frame(
    ticker: str,
    current_price: float,
    or_high: float,
    pdc_val: float,
    *,
    di_plus: float = 30.0,
    di_minus: float = 10.0,
    avg_vol: float = 100_000.0,
    breakout_vol_ratio: float = 2.5,
) -> TickerFrame:
    """Build a frame that satisfies all long entry gates.

    Closes: 5 entries — the last two strictly above or_high so the
    2-bar Tiger gate clears. Volumes mirror that pattern.
    """
    c1 = or_high - 0.10
    c2 = or_high - 0.05
    c3 = or_high - 0.02
    c4 = or_high + 0.10
    c5 = current_price
    closes = [c1, c2, c3, c4, c5]
    opens = [c - 0.05 for c in closes]
    highs = [c + 0.10 for c in closes]
    lows = [c - 0.10 for c in closes]
    volumes = [
        int(avg_vol),
        int(avg_vol),
        int(avg_vol),
        int(avg_vol),
        int(avg_vol * breakout_vol_ratio),
    ]
    timestamps = [1700000000 + 60 * i for i in range(len(closes))]
    bars = {
        "timestamps": timestamps,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
        "current_price": current_price,
        "pdc": pdc_val,
    }
    quote = {"price": current_price, "previousClose": pdc_val}
    return TickerFrame(
        bars_1min=bars,
        quote=quote,
        di=(di_plus, di_minus),
    )


def make_short_breakdown_frame(
    ticker: str,
    current_price: float,
    or_low: float,
    pdc_val: float,
    *,
    di_plus: float = 10.0,
    di_minus: float = 30.0,
    avg_vol: float = 100_000.0,
    breakdown_vol_ratio: float = 2.5,
) -> TickerFrame:
    """Frame that satisfies all short entry gates."""
    c1 = or_low + 0.10
    c2 = or_low + 0.05
    c3 = or_low + 0.02
    c4 = or_low - 0.10
    c5 = current_price
    closes = [c1, c2, c3, c4, c5]
    opens = [c + 0.05 for c in closes]
    highs = [c + 0.10 for c in closes]
    lows = [c - 0.10 for c in closes]
    volumes = [
        int(avg_vol),
        int(avg_vol),
        int(avg_vol),
        int(avg_vol),
        int(avg_vol * breakdown_vol_ratio),
    ]
    timestamps = [1700000000 + 60 * i for i in range(len(closes))]
    bars = {
        "timestamps": timestamps,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
        "current_price": current_price,
        "pdc": pdc_val,
    }
    quote = {"price": current_price, "previousClose": pdc_val}
    return TickerFrame(
        bars_1min=bars,
        quote=quote,
        di=(di_plus, di_minus),
    )


def make_index_bull_frame(ticker: str, current_price: float, pdc_val: float):
    """SPY/QQQ frame with current_price > pdc (long-favorable)."""
    return make_long_breakout_frame(
        ticker, current_price, pdc_val - 0.50, pdc_val,
        di_plus=25.0, di_minus=15.0,
    )


def make_index_bear_frame(ticker: str, current_price: float, pdc_val: float):
    """SPY/QQQ frame with current_price < pdc (short-favorable)."""
    return make_short_breakdown_frame(
        ticker, current_price, pdc_val + 0.50, pdc_val,
        di_plus=15.0, di_minus=25.0,
    )
