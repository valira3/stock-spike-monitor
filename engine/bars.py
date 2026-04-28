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


def compute_5m_ohlc_and_ema9(bars: dict | None) -> dict | None:
    """Return {opens, highs, lows, closes, ema9, seeded, last_bucket}
    for closed 5m bars derived from a `fetch_1min_bars` payload.

    `seeded` is True once 9 closed 5m bars are available (Gene's spec
    requires \u2265 9 closes since 9:30 ET to seed the 5m 9-EMA). `ema9`
    is the most recent EMA9 value, or None.
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
    if len(closes) >= 9:
        # Standard EMA(9): SMA seed over first 9, then alpha = 2/(9+1).
        seed = sum(closes[:9]) / 9.0
        ema = seed
        alpha = 2.0 / 10.0
        for c in closes[9:]:
            ema = alpha * c + (1.0 - alpha) * ema
        ema9 = ema
        seeded = True
    return {
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "ema9": ema9,
        "seeded": seeded,
        "last_bucket": ordered[-1],
    }


__all__ = ["compute_5m_ohlc_and_ema9"]
