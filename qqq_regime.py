"""v5.9.0 \u2014 QQQ Regime Shield (Permission Gate G1).

Maintains a 5-minute 3-EMA and 9-EMA of QQQ closes. The cross
relationship (`compass`) becomes the binary L-P1-G1 / S-P1-G1
permission gate that replaces the v5.6.0 AVWAP penny-switch.

API
---
    QQQRegime()                       \u2014 fresh state.
    qr.update(close_5m_finalized)     \u2014 advance EMAs on a closed 5m bar.
    qr.current_compass()              \u2014 returns "UP" | "DOWN" | "FLAT" | None.
    qr.seed(closes, source)           \u2014 replay a chronological list of
                                        pre-market closes.

Standard EMA recurrence with smoothing factor alpha = 2/(N+1).
The first N closes seed an SMA; subsequent closes apply the recurrence.
FLAT means EMA3 == EMA9 (equality FAILs in G1). None means warmup is
not yet complete \u2014 G1 must fail-closed when the compass is None.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple


EMA3_PERIOD = 3
EMA9_PERIOD = 9

COMPASS_UP = "UP"
COMPASS_DOWN = "DOWN"
COMPASS_FLAT = "FLAT"

_VALID_SEED_SOURCES = ("archive", "alpaca", "prior_session")


def _alpha(period: int) -> float:
    return 2.0 / (period + 1.0)


def _step_ema(
    prev_ema: Optional[float], close: float, period: int, seed_buf: List[float]
) -> Tuple[Optional[float], List[float]]:
    """One closed-bar advance for a single EMA stream.

    `seed_buf` accumulates closes until length == period; first EMA is
    the SMA of those `period` closes. After seeding, prev_ema is the
    persisted state.
    """
    if prev_ema is None:
        seed_buf.append(float(close))
        if len(seed_buf) >= period:
            ema = sum(seed_buf) / float(period)
            return float(ema), []
        return None, seed_buf
    k = _alpha(period)
    ema = (float(close) - float(prev_ema)) * k + float(prev_ema)
    return float(ema), []


class QQQRegime:
    """5m EMA(3) / EMA(9) state machine with pre-market seeding."""

    def __init__(self) -> None:
        self.ema3: Optional[float] = None
        self.ema9: Optional[float] = None
        self._seed_buf3: List[float] = []
        self._seed_buf9: List[float] = []
        self.bars_seen: int = 0
        # last finalized close fed in via update(); useful for log lines.
        self.last_close: Optional[float] = None
        # populated by seed(); read once for [V572-REGIME-SEED] log line.
        self.seed_source: Optional[str] = None
        self.seed_bar_count: int = 0

    def update(self, close_5m_finalized: float) -> None:
        """Advance EMA(3) and EMA(9) on one closed 5m QQQ bar."""
        if close_5m_finalized is None:
            return
        c = float(close_5m_finalized)
        self.last_close = c
        self.bars_seen += 1
        self.ema3, self._seed_buf3 = _step_ema(
            self.ema3,
            c,
            EMA3_PERIOD,
            self._seed_buf3,
        )
        self.ema9, self._seed_buf9 = _step_ema(
            self.ema9,
            c,
            EMA9_PERIOD,
            self._seed_buf9,
        )

    def current_compass(self) -> Optional[str]:
        """Return compass direction or None if warmup incomplete.

        Compass is None until both EMAs have seeded (>=9 closed 5m bars
        observed). Once both present, returns UP / DOWN / FLAT.
        """
        if self.ema3 is None or self.ema9 is None:
            return None
        if self.ema3 > self.ema9:
            return COMPASS_UP
        if self.ema3 < self.ema9:
            return COMPASS_DOWN
        return COMPASS_FLAT

    def seed(self, closes: Iterable[float], source: str) -> int:
        """Replay a chronological list of pre-market 5m closes.

        Returns the count of bars actually applied. Records `source` and
        bar count for the [V572-REGIME-SEED] log emitted on first
        compass evaluation.
        """
        if source not in _VALID_SEED_SOURCES:
            raise ValueError(
                "seed source must be one of %s; got %r" % (_VALID_SEED_SOURCES, source)
            )
        n = 0
        for c in closes:
            if c is None:
                continue
            self.update(c)
            n += 1
        self.seed_source = source
        self.seed_bar_count = n
        return n
