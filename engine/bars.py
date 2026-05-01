"""v5.11.0 \u2014 engine.bars: 5m OHLC + EMA9 computation.

Extracted verbatim from `trade_genius._v5105_compute_5m_ohlc_and_ema9`
(was at trade_genius.py:8270 in v5.10.7). Public name drops the
`_v5105_` version prefix per the v5.11.0 refactor convention; a
private alias (`_v5105_compute_5m_ohlc_and_ema9 = compute_5m_ohlc_and_ema9`)
is kept in trade_genius.py for one release as a deprecation shim.

Zero behavior change. Validated byte-equal pre/post the move via
`tests/golden/verify.py`.
"""

from __future__ import annotations


def compute_5m_ohlc_and_ema9(
    bars: dict | None, pdc: float | None = None
) -> dict | None:
    """Return {opens, highs, lows, closes, ema9, seeded, last_bucket}
    for closed 5m bars derived from a `fetch_1min_bars` payload.

    `seeded` is True once a usable EMA9 value can be produced. With
    \u2265 9 closed 5m bars this matches the original Gene spec. v6.0.0
    adds a synthetic-prefix path: when fewer than 9 closed bars exist
    AND ``pdc`` is provided, a 9-bar synthetic history flat at PDC is
    prepended (SMA seed = PDC) and the standard EMA recursion runs on
    the real bars. ``ema9`` is the most recent EMA9 value, or None.
    """
    if not bars:
        return None
    ts_list = bars.get("timestamps") or []
    opens_all = bars.get("opens") or []
    highs_all = bars.get("highs") or []
    lows_all = bars.get("lows") or []
    closes_all = bars.get("closes") or []
    if not ts_list or not closes_all:
        return None

    buckets_open: dict[int, float] = {}
    buckets_high: dict[int, float] = {}
    buckets_low: dict[int, float] = {}
    buckets_close: dict[int, float] = {}
    n = len(ts_list)
    for i in range(n):
        ts = ts_list[i]
        if ts is None:
            continue
        o = opens_all[i] if i < len(opens_all) else None
        h = highs_all[i] if i < len(highs_all) else None
        lo = lows_all[i] if i < len(lows_all) else None
        c = closes_all[i] if i < len(closes_all) else None
        if o is None or h is None or lo is None or c is None:
            continue
        bucket = int(ts) // 300
        if bucket not in buckets_open:
            buckets_open[bucket] = o
            buckets_high[bucket] = h
            buckets_low[bucket] = lo
            buckets_close[bucket] = c
        else:
            buckets_high[bucket] = max(buckets_high[bucket], h)
            buckets_low[bucket] = min(buckets_low[bucket], lo)
            buckets_close[bucket] = c
    ordered = sorted(buckets_open.keys())
    if len(ordered) <= 1:
        return None
    ordered = ordered[:-1]  # drop newest (possibly forming)
    if not ordered:
        return None
    opens = [buckets_open[b] for b in ordered]
    highs = [buckets_high[b] for b in ordered]
    lows = [buckets_low[b] for b in ordered]
    closes = [buckets_close[b] for b in ordered]
    ema9 = None
    seeded = False
    # v5.27.0 \u2014 expose per-bar ema9 series so callers (e.g. Alarm B
    # 2-bar confirm) can read the EMA9 reading at bucket -2 as well as
    # the most recent bucket. The series is aligned with ``closes`` and
    # ``opens``; entries before the SMA seed slot are None.
    ema9_series: list[float | None] = [None] * len(closes)
    alpha = 2.0 / 10.0
    if len(closes) >= 9:
        # Standard EMA(9): SMA seed over first 9, then alpha = 2/(9+1).
        seed = sum(closes[:9]) / 9.0
        ema = seed
        ema9_series[8] = seed
        for idx, c in enumerate(closes[9:], start=9):
            ema = alpha * c + (1.0 - alpha) * ema
            ema9_series[idx] = ema
        ema9 = ema
        seeded = True
    elif pdc is not None and pdc > 0 and len(closes) >= 1:
        # v6.0.0 \u2014 synthetic 9-bar PDC-anchored prefix. Seed = PDC
        # (SMA of 9 synthetic flat-at-PDC closes). Real bars then
        # advance the EMA at the standard alpha so the regime gate has
        # a defensible reading from bar #1 instead of waiting 45 min.
        ema = float(pdc)
        for idx, c in enumerate(closes):
            ema = alpha * c + (1.0 - alpha) * ema
            ema9_series[idx] = ema
        ema9 = ema
        seeded = True
    return {
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "ema9": ema9,
        "ema9_series": ema9_series,
        "seeded": seeded,
        "last_bucket": ordered[-1],
    }


__all__ = ["compute_5m_ohlc_and_ema9"]
