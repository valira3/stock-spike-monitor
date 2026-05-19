"""Premarket breakout scanner: rank S&P 500 by setup score at 09:29 ET.

Reads premarket bars (04:00-09:29 ET) from `data_pm_universe/<DATE>/<TICKER>.jsonl`
and emits the top-K tickers most likely to break out at the RTH open.

Signal options:
  - "gap"             : |premarket close (09:29) − prior RTH close| / prior RTH close
  - "volume"          : premarket dollar volume (sum of close × volume for 04:00-09:29)
  - "range"           : (premarket high − premarket low) / premarket open
  - "composite"       : z-score sum of (gap, volume, range), divided by 3
  - "compression"     : NR-N compression -- inverse of bar-range over the last N
                        premarket bars within the last M minutes (the r18
                        signal). Higher score = TIGHTER range. ABSOLUTE — biased
                        toward names that are always sleepy.
  - "compression_rel" : Relative NR-N -- today's last-N-bar range divided by the
                        14-day mean of the same statistic for this ticker.
                        Sorts highest for "tight TODAY relative to typical."
                        Requires the feature cache built by orb.scanner_cache
                        (it stores the per-ticker rolling baseline).

The scanner is upstream of the ORB engine. It does not change entry
logic, exit logic, or risk caps; it only changes which tickers the
engine evaluates that day.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PREMARKET_OPEN_BUCKET = "0400"
PREMARKET_CLOSE_BUCKET = "0929"
RTH_OPEN_BUCKET = "0930"


@dataclass(frozen=True)
class ScanResult:
    ticker: str
    score: float
    gap_pct: float          # signed gap; positive = premarket above prior close
    pm_dollar_volume: float
    pm_range_pct: float     # (high − low) / open of the premarket window
    n_pm_bars: int


def _load_bars(path: Path) -> list[dict]:
    """Load all bars for one (date, ticker) JSONL file."""
    out = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except FileNotFoundError:
        return []
    return out


def _premarket_bars(bars: list[dict]) -> list[dict]:
    """Filter to et_bucket strictly < 0930."""
    return [b for b in bars if b.get("et_bucket", "9999") < RTH_OPEN_BUCKET]


def _prior_close(corpus_root: Path, ticker: str, current_date: str, lookback_days: int = 7) -> float | None:
    """Walk back up to lookback_days calendar days to find a prior RTH close
    (last bar before 16:00 ET). Returns None if not found."""
    from datetime import date, timedelta
    d = date.fromisoformat(current_date)
    for back in range(1, lookback_days + 1):
        prev = d - timedelta(days=back)
        path = corpus_root / prev.isoformat() / f"{ticker}.jsonl"
        bars = _load_bars(path)
        # Last bar inside RTH (bucket between 0930 and 1559)
        rth = [b for b in bars if RTH_OPEN_BUCKET <= b.get("et_bucket", "9999") < "1600"]
        if rth:
            return float(rth[-1]["close"])
    return None


def _signal_features(
    corpus_root: Path,
    ticker: str,
    date_str: str,
    min_pm_bars: int = 10,
) -> ScanResult | None:
    """Compute per-ticker scan features for one day. Returns None if the
    ticker has insufficient premarket data."""
    path = corpus_root / date_str / f"{ticker}.jsonl"
    bars = _load_bars(path)
    if not bars:
        return None
    pm = _premarket_bars(bars)
    if len(pm) < min_pm_bars:
        return None

    pm_open = float(pm[0]["open"])
    pm_close = float(pm[-1]["close"])
    pm_high = max(float(b["high"]) for b in pm)
    pm_low = min(float(b["low"]) for b in pm)
    pm_dollar_vol = sum(float(b["close"]) * float(b["total_volume"]) for b in pm)

    # Gap vs prior RTH close
    prior_close = _prior_close(corpus_root, ticker, date_str)
    if prior_close is None or prior_close <= 0:
        return None
    gap_pct = (pm_close - prior_close) / prior_close

    pm_range_pct = (pm_high - pm_low) / pm_open if pm_open > 0 else 0.0

    return ScanResult(
        ticker=ticker,
        score=0.0,  # filled in by scan_day()
        gap_pct=gap_pct,
        pm_dollar_volume=pm_dollar_vol,
        pm_range_pct=pm_range_pct,
        n_pm_bars=len(pm),
    )


def _z_scores(values: list[float]) -> list[float]:
    """Z-score normalization. Returns zeros if stddev is 0."""
    if len(values) < 2:
        return [0.0] * len(values)
    mu = statistics.fmean(values)
    sigma = statistics.pstdev(values)
    if sigma <= 0:
        return [0.0] * len(values)
    return [(v - mu) / sigma for v in values]


def _compression_score(bars: list[dict], pm_lookback_n: int,
                       pm_min_lookback_min: int) -> float | None:
    """NR-N compression score. Returns higher value for tighter recent range.

    Window: last `pm_min_lookback_min` minutes of premarket (i.e. bucket >=
    09:30 - pm_min_lookback_min and bucket < 09:30). Within that window,
    take the LAST `pm_lookback_n` bars. Range = (max_high - min_low) /
    mean_close. Score = -range (so sorting desc picks tightest first).

    Returns None if there are fewer than pm_lookback_n bars in the window.
    """
    rth_open_min = 9 * 60 + 30   # 09:30 ET
    window_start_min = rth_open_min - pm_min_lookback_min
    in_window = []
    for b in bars:
        bkt = b.get("et_bucket", "9999")
        if bkt == "9999":
            continue
        # bucket "HHMM" → minutes
        try:
            mins = int(bkt[:2]) * 60 + int(bkt[2:])
        except ValueError:
            continue
        if window_start_min <= mins < rth_open_min:
            in_window.append((mins, b))
    if len(in_window) < pm_lookback_n:
        return None
    # Sort by minutes asc, take last N
    in_window.sort(key=lambda x: x[0])
    last_n = [b for _, b in in_window[-pm_lookback_n:]]
    highs = [float(b["high"]) for b in last_n]
    lows = [float(b["low"]) for b in last_n]
    closes = [float(b["close"]) for b in last_n]
    rng = max(highs) - min(lows)
    mean_close = sum(closes) / len(closes) if closes else 0.0
    if mean_close <= 0:
        return None
    rng_pct = rng / mean_close
    return -rng_pct   # negate so higher = tighter


def scan_day(
    corpus_root: Path | str,
    date_str: str,
    universe: Iterable[str],
    signal: str = "composite",
    top_k: int = 10,
    min_pm_bars: int = 10,
    min_dollar_volume: float = 100_000.0,
    feature_cache: dict | None = None,
    pm_lookback_n: int = 5,
    pm_min_lookback_min: int = 30,
) -> list[ScanResult]:
    """Rank `universe` by `signal` on `date_str`; return top_k ScanResults.

    `signal` ∈ {"gap", "volume", "range", "composite"}. Composite is the
    sum of z-scores across (|gap|, log dollar volume, premarket range%) /
    3.

    Tickers with < min_pm_bars premarket bars or < min_dollar_volume in
    premarket dollar volume are dropped (illiquid noise filter).

    `feature_cache` (optional): pre-built dict[(date_str, ticker)] ->
    (gap_pct, pm_dollar_vol, pm_range_pct, n_pm_bars, prior_close).
    When supplied, scanner skips the JSONL read path entirely. Build
    via `orb.scanner_cache.build_cache` + `save_cache`.

    The "compression" signal always re-reads the raw bars (it needs
    per-bar high/low for the last-N window). Feature cache is unused
    for that signal path.
    """
    corpus_root = Path(corpus_root)
    raw: list[ScanResult] = []
    compression_scores: list[float] = []
    compression_rel_scores: list[float] = []
    for tk in universe:
        if signal == "compression_rel":
            # Pure cache-lookup; needs the extended cache built by
            # orb.scanner_cache.build_cache (7-field tuple).
            if feature_cache is None:
                continue
            feats = feature_cache.get((date_str, tk))
            if feats is None:
                continue
            try:
                gap_pct, pm_dol, pm_rng, n_pm, _prior, last_n_rng, baseline = feats
            except (TypeError, ValueError):
                continue
            if n_pm < min_pm_bars or pm_dol < min_dollar_volume:
                continue
            if last_n_rng <= 0 or baseline <= 0:
                continue
            rel = last_n_rng / baseline  # < 1.0 = tighter than typical
            raw.append(ScanResult(ticker=tk, score=0.0, gap_pct=gap_pct,
                                  pm_dollar_volume=pm_dol, pm_range_pct=pm_rng,
                                  n_pm_bars=n_pm))
            compression_rel_scores.append(-rel)  # negate so higher = tighter
        elif signal == "compression":
            # Need the raw bars; cache is per-aggregate
            bars = _load_bars(corpus_root / date_str / f"{tk}.jsonl")
            if not bars:
                continue
            pm = _premarket_bars(bars)
            if len(pm) < min_pm_bars:
                continue
            pm_dol = sum(float(b["close"]) * float(b["total_volume"]) for b in pm)
            if pm_dol < min_dollar_volume:
                continue
            score = _compression_score(bars, pm_lookback_n, pm_min_lookback_min)
            if score is None:
                continue
            pm_open = float(pm[0]["open"])
            pm_high = max(float(b["high"]) for b in pm)
            pm_low = min(float(b["low"]) for b in pm)
            pm_range_pct = (pm_high - pm_low) / pm_open if pm_open > 0 else 0.0
            # gap_pct via prior close
            prior_close = _prior_close(corpus_root, tk, date_str)
            gap_pct = ((float(pm[-1]["close"]) - prior_close) / prior_close
                       if prior_close and prior_close > 0 else 0.0)
            raw.append(ScanResult(ticker=tk, score=0.0, gap_pct=gap_pct,
                                  pm_dollar_volume=pm_dol,
                                  pm_range_pct=pm_range_pct,
                                  n_pm_bars=len(pm)))
            compression_scores.append(score)
        elif feature_cache is not None:
            feats = feature_cache.get((date_str, tk))
            if feats is None:
                continue
            # Backwards-compat: accept either 5- or 7-field tuples
            gap_pct, pm_dol, pm_rng, n_pm = feats[0], feats[1], feats[2], feats[3]
            if n_pm < min_pm_bars or pm_dol < min_dollar_volume:
                continue
            r = ScanResult(ticker=tk, score=0.0, gap_pct=gap_pct,
                           pm_dollar_volume=pm_dol, pm_range_pct=pm_rng,
                           n_pm_bars=n_pm)
            raw.append(r)
        else:
            r = _signal_features(corpus_root, tk, date_str, min_pm_bars=min_pm_bars)
            if r is None:
                continue
            if r.pm_dollar_volume < min_dollar_volume:
                continue
            raw.append(r)

    if not raw:
        return []

    # Compute the requested score for each row.
    abs_gap = [abs(r.gap_pct) for r in raw]
    log_vol = [math.log(max(r.pm_dollar_volume, 1.0)) for r in raw]
    rng = [r.pm_range_pct for r in raw]

    if signal == "compression_rel":
        scores = compression_rel_scores
    elif signal == "compression":
        scores = compression_scores
    elif signal == "gap":
        scores = abs_gap
    elif signal == "volume":
        scores = log_vol
    elif signal == "range":
        scores = rng
    elif signal == "composite":
        z_gap = _z_scores(abs_gap)
        z_vol = _z_scores(log_vol)
        z_rng = _z_scores(rng)
        scores = [(g + v + r) / 3.0 for g, v, r in zip(z_gap, z_vol, z_rng)]
    else:
        raise ValueError(f"unknown signal: {signal!r}")

    # Re-emit with scores filled in, sorted desc, capped to top_k.
    ranked = sorted(
        (
            ScanResult(
                ticker=r.ticker,
                score=s,
                gap_pct=r.gap_pct,
                pm_dollar_volume=r.pm_dollar_volume,
                pm_range_pct=r.pm_range_pct,
                n_pm_bars=r.n_pm_bars,
            )
            for r, s in zip(raw, scores)
        ),
        key=lambda x: x.score,
        reverse=True,
    )
    return ranked[:top_k]


def scan_universe_to_dict(
    corpus_root: Path | str,
    date_str: str,
    universe: Iterable[str],
    signal: str = "composite",
    top_k: int = 10,
    **kwargs,
) -> dict:
    """JSON-serializable wrapper around scan_day for CLI / harness use."""
    results = scan_day(corpus_root, date_str, universe, signal=signal, top_k=top_k, **kwargs)
    return {
        "date": date_str,
        "signal": signal,
        "top_k": top_k,
        "n_picks": len(results),
        "picks": [
            {
                "ticker": r.ticker,
                "score": round(r.score, 6),
                "gap_pct": round(r.gap_pct * 100, 4),
                "pm_dollar_volume": round(r.pm_dollar_volume, 0),
                "pm_range_pct": round(r.pm_range_pct * 100, 4),
                "n_pm_bars": r.n_pm_bars,
            }
            for r in results
        ],
    }
