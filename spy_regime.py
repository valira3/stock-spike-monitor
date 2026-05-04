"""v6.11.0 \u2014 SPY Regime Classifier (C25).

Computes the SPY first-30-minute return at 10:00 ET each session and
classifies it into one of five bands (A/B/C/D/E). Exposes
``is_regime_b()`` for the short-amplification gate in broker/orders.py.

Band definitions (strict on both boundaries of B; symmetric structure):

    A : ret <= LOWER_PCT              (deep down)
    B : LOWER_PCT < ret < UPPER_PCT   (moderately down)
    C : UPPER_PCT <= ret <= -UPPER_PCT (flat)
    D : -UPPER_PCT < ret <= -LOWER_PCT (moderately up)
    E : ret > -LOWER_PCT              (deep up)

Where LOWER_PCT = -0.50 and UPPER_PCT = -0.15 (defaults; env-overridable).

Symmetry: LOWER/UPPER define the down side; the up side mirrors them.
Band C spans [-0.15, +0.15] inclusive on both ends.
Band B spans (-0.50, -0.15) exclusive on both ends per spec test-11.

Usage::

    from spy_regime import SpyRegime
    sr = SpyRegime()

    # In the 09:30 scan tick:
    sr.tick(now_et, spy_price)   # captures 09:30 anchor

    # In the 10:00 scan tick:
    sr.tick(now_et, spy_price)   # captures 10:00 anchor, classifies

    sr.is_regime_b()             # True on regime-B days
    sr.current_regime()          # "A"|"B"|"C"|"D"|"E"|None

    # At session rollover:
    sr.daily_reset()
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (defaults; also used directly by tests).
# ---------------------------------------------------------------------------
# Band boundaries -- loaded from env at import time so they are baked in for
# tests but overridable in production via environment variables.
_LOWER_PCT: float = float(os.getenv("V611_REGIME_B_LOWER_PCT", "-0.50"))
_UPPER_PCT: float = float(os.getenv("V611_REGIME_B_UPPER_PCT", "-0.15"))


class SpyRegime:
    """Per-session SPY 30-minute return regime classifier.

    State is scoped to one trading day. Call ``daily_reset()`` at each
    session start to clear anchors for the new day.
    """

    def __init__(self) -> None:
        self.spy_open_930: Optional[float] = None
        self.spy_close_1000: Optional[float] = None
        self.spy_30m_return_pct: Optional[float] = None
        self.regime: Optional[str] = None
        self._classified_at: Optional[str] = None
        # Read boundaries from module-level constants so they pick up any
        # env-based overrides set before this instance is created.
        self._lower_pct: float = _LOWER_PCT
        self._upper_pct: float = _UPPER_PCT

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def tick(self, now_et, spy_price: float) -> None:
        """Advance regime state with a new SPY price observation.

        Captures the 09:30 anchor on the first call within
        [09:30:00, 09:31:00) ET and the 10:00 anchor on the first call
        within [10:00:00, 10:01:00) ET. Once both anchors are set the
        return is computed and the regime band is classified. Subsequent
        calls are no-ops.

        If the 09:30 anchor is missing when the 10:00 window opens the
        regime stays None (fails closed -- no amplification).
        """
        if spy_price is None:
            return

        hh = now_et.hour
        mm = now_et.minute

        # Capture 09:30 anchor -- first bar in [09:30, 09:31) ET.
        if hh == 9 and mm == 30 and self.spy_open_930 is None:
            self.spy_open_930 = float(spy_price)
            return

        # Capture 10:00 anchor -- first bar in [10:00, 10:01) ET.
        if hh == 10 and mm == 0 and self.spy_close_1000 is None:
            self.spy_close_1000 = float(spy_price)
            self._classify(now_et)

    def is_regime_b(self) -> bool:
        """Return True iff today's regime is B."""
        return self.regime == "B"

    def is_regime_a(self) -> bool:
        """Return True iff today's regime is A."""
        return self.regime == "A"

    def current_regime(self) -> Optional[str]:
        """Return today's regime letter or None if not yet classified."""
        return self.regime

    def daily_reset(self) -> None:
        """Clear all session-day state. Call at session start (09:30 ET)."""
        self.spy_open_930 = None
        self.spy_close_1000 = None
        self.spy_30m_return_pct = None
        self.regime = None
        self._classified_at = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify(self, now_et) -> None:
        """Compute return and assign regime band. Emits [V611-REGIME-B]."""
        if self.spy_open_930 is None or self.spy_open_930 == 0:
            # Feed gap -- fail closed.
            self.regime = None
            logger.warning(
                "[V611-REGIME-B] spy_open_930 missing at 10:00 \u2014 regime=None b=false"
            )
            return

        ret = (self.spy_close_1000 - self.spy_open_930) / self.spy_open_930 * 100.0
        self.spy_30m_return_pct = round(ret, 4)

        lo = self._lower_pct  # e.g. -0.50
        hi = self._upper_pct  # e.g. -0.15

        # Band classification (strict on both B boundaries per spec test-11).
        if ret <= lo:
            band = "A"
        elif lo < ret < hi:
            band = "B"
        elif hi <= ret <= (-hi):
            band = "C"
        elif (-hi) < ret <= (-lo):
            band = "D"
        else:
            band = "E"

        self.regime = band
        try:
            import datetime as _dt
            self._classified_at = now_et.astimezone(_dt.timezone.utc).isoformat()
        except Exception:
            self._classified_at = None

        logger.info(
            "[V611-REGIME-B] spy_open=%.4f spy_close_1000=%.4f ret_pct=%.4f regime=%s b=%s",
            self.spy_open_930,
            self.spy_close_1000,
            self.spy_30m_return_pct,
            band,
            "true" if band == "B" else "false",
        )
