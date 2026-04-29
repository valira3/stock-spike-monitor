"""v5.15.0 PR-3a \u2014 Tiger Sovereign vAA-1 momentum-state foundation.

Three small in-memory state holders required by the vAA-1 Phase-3
authorisation, Sentinel-D HVP lock, and divergence-aware exits. This
module is intentionally a leaf: nothing imports it yet (apart from
``tests/test_momentum_state.py`` and the two PR-3a-tagged tests in
``tests/test_tiger_sovereign_vAA_spec.py``). PR-3, PR-4, PR-5 will wire
the classes into the live entry/exit paths.

API surface
-----------
``TradeHVP``
    Per-Strike peak 5m ADX. ``on_strike_open(initial_adx_5m)`` seeds the
    peak; ``update(current_adx_5m)`` is max-monotone; the ``peak``
    property returns the current high-water mark and raises
    ``RuntimeError`` if no Strike is open.

``DivergenceMemory``
    Per-(ticker, side) Stored_Peak_Price / Stored_Peak_RSI memory.
    ``update(ticker, side, price, rsi)`` only stores when the new
    (price, rsi) pair improves on the existing peak in the side-correct
    direction. ``peak(ticker, side)`` returns the stored
    ``(price, rsi)`` tuple or ``None``. ``is_diverging(...)`` answers
    the bear-/bull-divergence question used by SENT-D.
    ``session_reset()`` clears all entries (called at EOD).

``ADXTrendWindow``
    Three-element ring of 1m ADX values with a strict-decreasing check
    used by SENT-C / vAA-1 momentum exits.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional


# Spec marker for grep-tests / audits. Constants live alongside the
# classes that consume them so the spec rule and the code stay
# co-located.
_SPEC_MARKERS = (
    "Trade_HVP",
    "Stored_Peak_Price",
    "Stored_Peak_RSI",
    "ADX_Trend_Window",
)


# ---------------------------------------------------------------------------
# TradeHVP \u2014 per-Strike peak 5m ADX
# ---------------------------------------------------------------------------


class TradeHVP:
    """Track the high-water-mark 5m ADX for the current Strike.

    Behaviour
    ---------
    * ``on_strike_open(initial_adx_5m)`` seeds (or reseeds) the peak
      with the new Strike's fill-time ADX. Every subsequent call resets
      the peak to the new initial value \u2014 the previous Strike's HVP
      does NOT carry over.
    * ``update(current_adx_5m)`` is max-monotone: the peak only ever
      moves UP within a single Strike. Lower readings are ignored.
    * ``peak`` returns the current peak. Accessing it before any
      ``on_strike_open`` raises ``RuntimeError``.
    """

    __slots__ = ("_peak", "_strike_open")

    def __init__(self) -> None:
        self._peak: float = 0.0
        self._strike_open: bool = False

    def on_strike_open(self, initial_adx_5m: float) -> None:
        """Open (or re-open) a Strike and seed the peak."""
        self._peak = float(initial_adx_5m)
        self._strike_open = True

    def update(self, current_adx_5m: float) -> None:
        """Feed the live 5m ADX. No-op if no Strike is open."""
        if not self._strike_open:
            return
        v = float(current_adx_5m)
        if v > self._peak:
            self._peak = v

    @property
    def peak(self) -> float:
        if not self._strike_open:
            raise RuntimeError("TradeHVP.peak accessed before on_strike_open")
        return self._peak


# ---------------------------------------------------------------------------
# DivergenceMemory \u2014 Stored_Peak_Price / Stored_Peak_RSI per (ticker, side)
# ---------------------------------------------------------------------------


class DivergenceMemory:
    """Per-(ticker, side) divergence memory.

    For LONG positions the stored peak captures the highest price that
    was simultaneously confirmed by RSI (i.e. the new price exceeds the
    stored price AND the new RSI is at or above the stored RSI). For
    SHORT positions the mirror condition applies (lower price, RSI at
    or below).

    ``is_diverging`` answers the bear-/bull-divergence question:
    a LONG is diverging when the current price prints a fresh high
    while the 15m RSI prints a LOWER reading than the stored peak's
    RSI \u2014 classic bearish RSI divergence into a price high. The
    SHORT mirror is bullish divergence into a price low.
    """

    __slots__ = ("_peaks",)

    def __init__(self) -> None:
        self._peaks: dict[tuple[str, str], tuple[float, float]] = {}

    @staticmethod
    def _key(ticker: str, side: str) -> tuple[str, str]:
        return (str(ticker).upper(), str(side).upper())

    def update(self, ticker: str, side: str, price: float, rsi: float) -> None:
        """Conditionally raise the stored peak.

        For LONG: stored only if ``price > stored_price`` AND
        ``rsi >= stored_rsi`` (or no prior entry exists). For SHORT
        the inequality on price flips to ``<``.
        """
        k = self._key(ticker, side)
        p = float(price)
        r = float(rsi)
        cur = self._peaks.get(k)
        side_u = k[1]
        if cur is None:
            self._peaks[k] = (p, r)
            return
        sp, sr = cur
        if side_u == "LONG":
            if p > sp and r >= sr:
                self._peaks[k] = (p, r)
        elif side_u == "SHORT":
            if p < sp and r <= sr:
                self._peaks[k] = (p, r)
        # Unknown side: ignore (defensive; callers are expected to pass
        # LONG or SHORT only).

    def peak(self, ticker: str, side: str) -> Optional[tuple[float, float]]:
        """Return the stored ``(price, rsi)`` peak or ``None``."""
        return self._peaks.get(self._key(ticker, side))

    def is_diverging(
        self,
        ticker: str,
        side: str,
        current_price: float,
        current_rsi_15: float,
    ) -> bool:
        """True iff the current tick prints a divergence vs the stored peak.

        LONG: ``current_price > stored_price`` AND
        ``current_rsi_15 < stored_rsi`` \u2014 bearish RSI divergence into
        a price high.

        SHORT: ``current_price < stored_price`` AND
        ``current_rsi_15 > stored_rsi`` \u2014 bullish RSI divergence into
        a price low.
        """
        cur = self.peak(ticker, side)
        if cur is None:
            return False
        sp, sr = cur
        cp = float(current_price)
        cr = float(current_rsi_15)
        side_u = self._key(ticker, side)[1]
        if side_u == "LONG":
            return cp > sp and cr < sr
        if side_u == "SHORT":
            return cp < sp and cr > sr
        return False

    def session_reset(self) -> None:
        """Clear all stored peaks (called at session boundary / EOD)."""
        self._peaks.clear()


# ---------------------------------------------------------------------------
# ADXTrendWindow \u2014 3-element ring of 1m ADX values
# ---------------------------------------------------------------------------


class ADXTrendWindow:
    """Bounded 3-element window of 1m ADX values.

    ``is_strictly_decreasing`` returns True iff the window holds three
    samples and they are strictly monotone-decreasing left-to-right
    (oldest > middle > newest). Equality fails the strict check.
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf: Deque[float] = deque(maxlen=3)

    def push(self, adx: float) -> None:
        self._buf.append(float(adx))

    def is_strictly_decreasing(self) -> bool:
        if len(self._buf) < 3:
            return False
        a, b, c = self._buf[0], self._buf[1], self._buf[2]
        return a > b > c


__all__ = [
    "TradeHVP",
    "DivergenceMemory",
    "ADXTrendWindow",
]
