"""v5.10.6 \u2014 dashboard snapshot helpers for the Eye-of-the-Tiger panel.

This module exposes a single public function `build_v510_snapshot(m)` that
trade_genius / dashboard_server can call from inside `snapshot()` to surface
the live v5.10 state to the dashboard without growing dashboard_server.py
or coupling it to v5_10_1_integration internals.

Returns a dict with three keys:

  - section_i_permit: top-level Section I (QQQ Market Shield + Sovereign
    Anchor) state. {long_open, short_open, qqq_5m_close, qqq_5m_ema9,
    qqq_avwap_0930, sovereign_anchor_open}
  - per_ticker_v510: list of dicts \u2014 one per trade ticker \u2014 with the
    Volume Bucket and Boundary Hold gate state.
  - per_position_v510: dict keyed by (ticker, side) string "TICKER:SIDE"
    \u2014 surfaces phase + sovereign brake distance + entry_2 fired flag.

`m` is the trade_genius module handle (caller supplies it so we don't
import lazily from inside dashboard_server's executor thread).

The helper is defensive: every getattr / state read is wrapped so a
malformed cache value drops *that* field, not the whole snapshot. The
existing /api/state contract stays intact.
"""

from __future__ import annotations

from typing import Any


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _qqq_avwap_open(m) -> float | None:
    """Read the live QQQ opening AVWAP without re-fetching bars.

    The scan loop calls _opening_avwap("QQQ") each cycle; the result is
    only logged, not cached, so we recompute defensively here. Returns
    None on any error (helper must never raise).
    """
    try:
        fn = getattr(m, "_opening_avwap", None)
        if fn is None:
            return None
        return _safe_float(fn("QQQ"))
    except Exception:
        return None


def _qqq_regime_state(m) -> tuple[float | None, float | None]:
    """Pull the QQQ Regime Shield's latest closed-5m close and 9-EMA.

    Mirrors the read pattern used by maybe_log_permit_state in
    trade_genius.py L9977-L9978.
    """
    try:
        regime = getattr(m, "_QQQ_REGIME", None)
        if regime is None:
            return (None, None)
        return (
            _safe_float(getattr(regime, "last_close", None)),
            _safe_float(getattr(regime, "ema9", None)),
        )
    except Exception:
        return (None, None)


def _qqq_current_price(m) -> float | None:
    try:
        fetch = getattr(m, "fetch_1min_bars", None)
        if fetch is None:
            return None
        bars = fetch("QQQ")
        if not bars:
            return None
        return _safe_float(bars.get("current_price"))
    except Exception:
        return None


def _section_i_permit(m) -> dict:
    """Build the section_i_permit block. Calls evaluate_section_i twice
    (LONG, SHORT) so both rails are visible to the operator; the
    Sovereign Anchor leg is shared between sides so we report it once
    on the LONG-side reading.
    """
    qqq_close, qqq_ema9 = _qqq_regime_state(m)
    qqq_cur = _qqq_current_price(m)
    qqq_avwap = _qqq_avwap_open(m)
    out = {
        "qqq_5m_close": qqq_close,
        "qqq_5m_ema9": qqq_ema9,
        "qqq_current_price": qqq_cur,
        "qqq_avwap_0930": qqq_avwap,
        "long_open": False,
        "short_open": False,
        "sovereign_anchor_open": False,
    }
    try:
        glue = getattr(m, "_v510_integration", None) or _import_glue()
        if glue is None:
            return out
        long_p = glue.evaluate_section_i("LONG", qqq_close, qqq_ema9, qqq_cur, qqq_avwap)
        short_p = glue.evaluate_section_i("SHORT", qqq_close, qqq_ema9, qqq_cur, qqq_avwap)
        out["long_open"] = bool(long_p.get("open"))
        out["short_open"] = bool(short_p.get("open"))
        if qqq_cur is not None and qqq_avwap is not None:
            out["sovereign_anchor_open"] = bool(qqq_cur > qqq_avwap)
    except Exception:
        pass
    return out


def _import_glue():
    try:
        import v5_10_1_integration as glue

        return glue
    except Exception:
        return None


def _build_archive_volume_lookup():
    """Return a callable mapping ticker -> latest archived bar volume.

    v6.14.5 -- the v5.14.0 shadow retirement removed the volume_profile
    WS consumer that previously fed `current_1m_vol`. Nothing replaced
    it on the dashboard read path, so the field went silently to 0 for
    every ticker on every tick. The live ingest pipeline
    (ingest.algo_plus.BarAssembler -> bar_archive.write_bar) keeps
    writing one minute bar per ticker per tick to
    `/data/bars/<YYYY-MM-DD>/<TICKER>.jsonl` with `total_volume`
    populated, so we use that file as the source of truth for
    "latest minute volume".

    Returns a closure that reads the last non-empty line of today's
    file for each ticker, parses it lazily, and yields the bar's
    `total_volume`. Cached for the lifetime of one snapshot call so
    repeated calls within a single `/api/state` build don't re-read
    files. Returns 0 on any error so a missing file or parse failure
    cannot break the snapshot.
    """
    import json as _json
    import os as _os
    from datetime import datetime as _dt
    from pathlib import Path as _P

    base = _os.environ.get("BAR_ARCHIVE_BASE")
    if not base:
        tg_root = _os.environ.get("TG_DATA_ROOT", "/data")
        base = tg_root + "/bars"
    today = _dt.utcnow().strftime("%Y-%m-%d")
    day_dir = _P(base) / today
    cache: dict[str, int] = {}

    def _last_volume(ticker: str) -> int:
        sym = ticker.upper()
        if sym in cache:
            return cache[sym]
        v = 0
        fp = day_dir / (sym + ".jsonl")
        try:
            if fp.exists():
                # Read the last non-empty line. For typical RTH+EXT
                # volume on these tickers a file is well under 1 MB,
                # so reading it once per snapshot is cheap; the per-
                # snapshot cache makes this O(1) amortised across the
                # 10-ticker loop.
                with open(fp, "rb") as fh:
                    fh.seek(0, 2)
                    end = fh.tell()
                    chunk_size = min(end, 4096)
                    fh.seek(end - chunk_size)
                    tail = fh.read().decode("utf-8", errors="ignore")
                lines = [ln for ln in tail.splitlines() if ln.strip()]
                if lines:
                    bar = _json.loads(lines[-1])
                    raw = bar.get("total_volume")
                    if raw is None:
                        raw = bar.get("iex_volume")
                    if raw is not None:
                        try:
                            v = int(float(raw))
                        except (TypeError, ValueError):
                            v = 0
        except Exception:
            v = 0
        cache[sym] = v
        return v

    return _last_volume


def _vol_bucket_per_ticker(
    m, tickers: list[str], minute_hhmm: str, prev_minute_hhmm: str | None
) -> dict:
    """For each ticker, return the latest Volume Bucket state without
    triggering a re-evaluation. We read the live baseline singleton
    plus the WS consumer's just-closed bucket count. If either is
    unavailable, fall back to COLDSTART defaults.

    v5.20.5: looks up the WS consumer at ``prev_minute_hhmm`` (the
    just-closed bucket) instead of ``minute_hhmm`` (the still-forming
    one). Adds ``days_available``, ``lookback_days``, and
    ``ratio_to_55bar_avg`` so dashboard cards can explain why a gate
    is in COLDSTART vs PASS vs FAIL without round-tripping the logs.
    """
    out: dict = {}
    glue = _import_glue()
    consumer = getattr(m, "_ws_consumer", None)
    # v6.14.5 \u2014 the legacy volume_profile WS consumer was retired
    # in v5.14.0 ("shadow strategy retirement"). Nothing has populated
    # `m._ws_consumer` since, so this getattr always returns None and
    # `current_1m_vol` was hard-pinned to 0 across the entire dashboard.
    # Fall back to reading the most recent archived bar from the live
    # SIP ingest path (ingest.algo_plus -> bar_archive.write_bar). The
    # archive is the canonical write target for every minute bar and
    # already carries `total_volume`, so it works for both RTH and the
    # extended-hours session that Val watches in the late afternoon.
    archive_volume = _build_archive_volume_lookup() if consumer is None else None
    try:
        if glue is not None:
            bb = glue.get_volume_baseline()
        else:
            bb = None
    except Exception:
        bb = None
    # v5.20.5 \u2014 the lookback constant lives on the baseline; surface
    # it so the card can render "days_available / lookback_days".
    lookback_days = None
    try:
        if bb is not None:
            lookback_days = int(getattr(bb, "lookback_days", 0) or 0) or None
    except Exception:
        lookback_days = None
    lookup_bucket = prev_minute_hhmm or minute_hhmm
    for t in tickers:
        cur_v = 0
        try:
            if consumer is not None and lookup_bucket:
                cur_v = int(consumer.current_volume(t, lookup_bucket) or 0)
            elif archive_volume is not None:
                cur_v = int(archive_volume(t) or 0)
        except Exception:
            cur_v = 0
        gate = "COLDSTART"
        ratio = None
        baseline_med = None
        days_available = None
        try:
            if bb is not None:
                # Use the just-closed bucket so the gate result
                # matches what entry-1 would actually see.
                res = bb.check(t, lookup_bucket or minute_hhmm, cur_v)
                gate = str(res.get("gate") or "COLDSTART")
                ratio = _safe_float(res.get("ratio"))
                baseline_med = _safe_float(res.get("baseline"))
                days_available = res.get("days_available")
                if days_available is not None:
                    try:
                        days_available = int(days_available)
                    except (TypeError, ValueError):
                        days_available = None
        except Exception:
            pass
        state = {
            "PASS": "PASS",
            "FAIL": "FAIL",
            "COLDSTART": "COLDSTART",
        }.get(gate, "COLDSTART")
        out[t] = {
            "state": state,
            "current_1m_vol": int(cur_v),
            "baseline_at_minute": baseline_med,
            "ratio": ratio,
            # v5.20.5 \u2014 explanatory metrics surfaced for the card UI.
            "ratio_to_55bar_avg": ratio,
            "days_available": days_available,
            "lookback_days": lookback_days,
            "lookup_bucket": lookup_bucket,
        }
    return out


def _boundary_hold_per_ticker(m, tickers: list[str]) -> dict:
    """Read each ticker's Boundary Hold cache + the OR window levels.
    No re-evaluation \u2014 we only surface what's already in the glue's
    rolling 1m close window.
    """
    out: dict = {}
    glue = _import_glue()
    or_high = getattr(m, "or_high", {}) or {}
    or_low = getattr(m, "or_low", {}) or {}
    closes_cache: dict = {}
    if glue is not None:
        try:
            closes_cache = getattr(glue, "_last_1m_closes", {}) or {}
        except Exception:
            closes_cache = {}
    for t in tickers:
        last_two = list(closes_cache.get(t, []) or [])[-2:]
        oh = _safe_float(or_high.get(t))
        ol = _safe_float(or_low.get(t))
        # Side resolution: we report whichever side is currently
        # SATISFIED if any; otherwise the side closest to qualifying
        # (most outside closes). Default to LONG when ambiguous.
        side = None
        state = "ARMED"
        # v5.20.5 \u2014 surface raw consecutive_outside counts so the card
        # can show "LONG: 1/2 closes outside, SHORT: 0/2 closes outside".
        long_consec = 0
        short_consec = 0
        if glue is not None:
            try:
                long_res = glue.evaluate_boundary_hold_gate(t, "LONG", oh, ol)
                short_res = glue.evaluate_boundary_hold_gate(t, "SHORT", oh, ol)
                long_consec = int(long_res.get("consecutive_outside") or 0)
                short_consec = int(short_res.get("consecutive_outside") or 0)
                if bool(long_res.get("hold")):
                    state = "SATISFIED"
                    side = "LONG"
                elif bool(short_res.get("hold")):
                    state = "SATISFIED"
                    side = "SHORT"
                else:
                    long_n = long_consec
                    short_n = short_consec
                    if long_n == 0 and short_n == 0:
                        state = "ARMED"
                        side = None
                    elif long_n >= short_n:
                        state = "ARMED" if long_n < 2 else "SATISFIED"
                        side = "LONG"
                    else:
                        state = "ARMED" if short_n < 2 else "SATISFIED"
                        side = "SHORT"
                    if (
                        state == "ARMED"
                        and last_two
                        and (
                            (
                                side == "LONG"
                                and oh is not None
                                and last_two[-1] is not None
                                and last_two[-1] <= oh
                                and len(last_two) >= 2
                                and last_two[-2] > oh
                            )
                            or (
                                side == "SHORT"
                                and ol is not None
                                and last_two[-1] is not None
                                and last_two[-1] >= ol
                                and len(last_two) >= 2
                                and last_two[-2] < ol
                            )
                        )
                    ):
                        state = "BROKEN"
            except Exception:
                pass
        out[t] = {
            "state": state,
            "side": side,
            "last_two_closes": [_safe_float(c) for c in last_two],
            "or_high": oh,
            "or_low": ol,
            # v5.20.5 \u2014 expose raw consec counts for the dashboard card.
            # Renders as "LONG: long_consec/2, SHORT: short_consec/2".
            "long_consecutive_outside": long_consec,
            "short_consecutive_outside": short_consec,
        }
    return out


def _di_per_ticker(m, tickers: list[str]) -> dict:
    """v5.20.5 \u2014 surface DI+/DI- on both 1m and 5m for each ticker.

    Reads ``v5_di_1m_5m`` (no recompute incurred since fetch_1min_bars
    is cached per scan cycle) and the ``TIGER_V2_DI_THRESHOLD`` value
    so the card can render "DI+ 28.4 / DI- 11.0 (need >=25)" instead
    of just "PASS" or "FAIL".

    Each per-ticker entry: {di_plus_1m, di_minus_1m, di_plus_5m,
    di_minus_5m, threshold, seed_bars, sufficient}. Any individual
    field is None when warmup is incomplete.
    """
    out: dict = {}
    threshold = _safe_float(getattr(m, "TIGER_V2_DI_THRESHOLD", None))
    seed_cache = getattr(m, "_DI_SEED_CACHE", {}) or {}
    fn = getattr(m, "v5_di_1m_5m", None)
    for t in tickers:
        seed = seed_cache.get(t) or []
        seed_n = len(seed)
        di = {
            "di_plus_1m": None,
            "di_minus_1m": None,
            "di_plus_5m": None,
            "di_minus_5m": None,
        }
        if fn is not None:
            try:
                raw = fn(t) or {}
                for k in di:
                    di[k] = _safe_float(raw.get(k))
            except Exception:
                pass
        out[t] = {
            **di,
            "threshold": threshold,
            "seed_bars": int(seed_n),
            "sufficient": bool(seed_n >= 15),
        }
    return out


def _phase_for_position(pos: dict) -> str:
    """Defensive read of pos["phase"] \u2014 v5.10.5 wires this on every
    manage tick (trade_genius.py:8890). Falls back to "A" if unset.
    """
    phase = str(pos.get("phase") or "A").upper()
    return phase if phase in ("A", "B", "C") else "A"


def _sovereign_brake_distance(unrealized: float | None) -> float | None:
    """Distance until Sovereign Brake fires. The brake fires at
    unrealized P&L \u2264 -$500 (eye_of_tiger.evaluate_sovereign_brake), so
    distance = unrealized + 500. Positive = breathing room; near-zero
    or negative = imminent / already tripped.
    """
    if unrealized is None:
        return None
    try:
        return float(unrealized) + 500.0
    except (TypeError, ValueError):
        return None


# v5.20.5 \u2014 thresholds surfaced on dashboard cards (see Change 3 in spec).
_SOVEREIGN_BRAKE_DOLLARS = -500.0
_VELOCITY_FUSE_PCT = 0.01


def _time_in_position_min(pos: dict) -> float | None:
    """Minutes since entry. Reads ``entry_time`` (ISO) from the position
    dict; returns None if unparseable. v5.20.5 dashboard card metric.
    """
    raw = pos.get("entry_time") or pos.get("entry_ts_utc")
    if not raw:
        return None
    try:
        from datetime import datetime as _dt, timezone as _tz

        s = str(raw).replace("Z", "+00:00")
        ts = _dt.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        now = _dt.now(tz=_tz.utc)
        delta = (now - ts).total_seconds() / 60.0
        return round(delta, 1) if delta >= 0 else None
    except Exception:
        return None


def _last_5m_move_pct(tkr: str, pos: dict, prices: dict) -> float | None:
    """Pct change of the current 1m bar (open \u2192 last). Surfaced on the
    Velocity Fuse card so traders can see how close the fuse is to tripping.
    Reads ``current_1m_open`` from the position dict if the manage tick
    stamped it; otherwise returns None (null-safe per spec).
    """
    op = _safe_float(pos.get("current_1m_open"))
    px = _safe_float(prices.get(tkr))
    if op is None or px is None or op <= 0.0:
        return None
    try:
        return round((px - op) / op * 100.0, 4)
    except Exception:
        return None


def _strikes_block(tkr: str) -> dict:
    """Strike counter + (placeholder) recent-event history for the POS
    Strikes card. v5.20.5 surfaces the count from
    ``trade_genius._v570_strike_counts``; ``strike_history`` is a stub
    list (empty) until a per-ticker event log is wired separately.
    """
    count: int | None = None
    try:
        import trade_genius as _tg

        counts = getattr(_tg, "_v570_strike_counts", {}) or {}
        count = int(counts.get(str(tkr).upper(), 0))
    except Exception:
        count = None
    return {
        "strikes_count": count,
        "strike_history": [],  # placeholder; populated by future event log
    }


def _per_position_v510(longs: dict, shorts: dict, prices: dict) -> dict:
    """Build {key: {phase, sovereign_brake_distance_dollars, entry_2_fired,
    sovereign_brake, velocity_fuse, strikes}} for every open position.
    Key is "TICKER:SIDE" so the dashboard can match it against the
    existing positions array. v5.20.5 adds card-metric blocks per spec.
    """
    out: dict = {}
    for tkr, pos in (longs or {}).items():
        try:
            entry = _safe_float(pos.get("entry_price")) or 0.0
            shares = int(pos.get("shares", 0) or 0)
            mark = _safe_float(prices.get(tkr)) or entry
            unreal = (mark - entry) * shares
            position_value = entry * shares if (entry and shares) else 0.0
            unreal_pct: float | None
            brake_pct: float | None
            if position_value > 0.0:
                unreal_pct = round(unreal / position_value * 100.0, 4)
                brake_pct = round(_SOVEREIGN_BRAKE_DOLLARS / position_value * 100.0, 4)
            else:
                unreal_pct = None
                brake_pct = None
            out[f"{tkr}:LONG"] = {
                "phase": _phase_for_position(pos),
                "sovereign_brake_distance_dollars": _sovereign_brake_distance(unreal),
                "entry_2_fired": bool(pos.get("v5104_entry2_fired")),
                "sovereign_brake": {
                    "unrealized_pct": unreal_pct,
                    "brake_threshold_pct": brake_pct,
                    "brake_threshold_dollars": _SOVEREIGN_BRAKE_DOLLARS,
                    "time_in_position_min": _time_in_position_min(pos),
                },
                "velocity_fuse": {
                    "last_5m_move_pct": _last_5m_move_pct(tkr, pos, prices),
                    "fuse_threshold_pct": _VELOCITY_FUSE_PCT * 100.0,
                },
                "strikes": _strikes_block(tkr),
            }
        except Exception:
            continue
    for tkr, pos in (shorts or {}).items():
        try:
            entry = _safe_float(pos.get("entry_price")) or 0.0
            shares = int(pos.get("shares", 0) or 0)
            mark = _safe_float(prices.get(tkr)) or entry
            unreal = (entry - mark) * shares
            position_value = entry * shares if (entry and shares) else 0.0
            unreal_pct: float | None
            brake_pct: float | None
            if position_value > 0.0:
                unreal_pct = round(unreal / position_value * 100.0, 4)
                brake_pct = round(_SOVEREIGN_BRAKE_DOLLARS / position_value * 100.0, 4)
            else:
                unreal_pct = None
                brake_pct = None
            out[f"{tkr}:SHORT"] = {
                "phase": _phase_for_position(pos),
                "sovereign_brake_distance_dollars": _sovereign_brake_distance(unreal),
                "entry_2_fired": bool(pos.get("v5104_entry2_fired")),
                "sovereign_brake": {
                    "unrealized_pct": unreal_pct,
                    "brake_threshold_pct": brake_pct,
                    "brake_threshold_dollars": _SOVEREIGN_BRAKE_DOLLARS,
                    "time_in_position_min": _time_in_position_min(pos),
                },
                "velocity_fuse": {
                    "last_5m_move_pct": _last_5m_move_pct(tkr, pos, prices),
                    "fuse_threshold_pct": _VELOCITY_FUSE_PCT * 100.0,
                },
                "strikes": _strikes_block(tkr),
            }
        except Exception:
            continue
    return out


def _global_qqq_direction(m) -> str | None:
    """v5.31.5 \u2014 classify the live QQQ direction (UP / DOWN / FLAT).

    Uses the SAME `(EMA9 OR AVWAP) AND DI` rule that the per-stock
    classifier uses, so the divergence flag is symmetric. The QQQ DI
    streams are read via the same v5_di_1m_5m helper used by the
    Section III gate. Returns None on any error so the dashboard
    treats the comparison as data-missing.
    """
    try:
        from engine.local_weather import classify_local_weather
        regime = getattr(m, "_QQQ_REGIME", None)
        if regime is None:
            return None
        close_5m = _safe_float(getattr(regime, "last_close", None))
        ema9_5m = _safe_float(getattr(regime, "ema9", None))
        last = _qqq_current_price(m)
        avwap = _qqq_avwap_open(m)
        try:
            di_streams = m.v5_di_1m_5m("QQQ") or {}
        except Exception:
            di_streams = {}
        di_plus = _safe_float(di_streams.get("di_plus_1m"))
        di_minus = _safe_float(di_streams.get("di_minus_1m"))
        return classify_local_weather(
            close_5m, ema9_5m, last, avwap, di_plus, di_minus,
        )
    except Exception:
        return None


def _mini_chart_per_ticker(m, tickers: list[str]) -> dict:
    """v6.0.0 \u2014 lightweight sparkline payload for the collapsed
    permit-matrix row.

    Returns a dict keyed by ticker: ``{points: list[float], hi, lo,
    open, last, count}``. ``points`` is a downsampled (max 60) series
    of 1m closes for today's RTH session so the dashboard can paint a
    20-pixel-wide trend strip per row without an extra fetch. Failure
    on any single ticker is silently coerced to an empty payload so a
    feed hiccup never collapses the matrix.
    """
    out: dict = {}
    try:
        fetch = getattr(m, "fetch_1min_bars", None)
    except Exception:
        fetch = None
    if fetch is None:
        return out
    for t in tickers:
        try:
            bars = fetch(t)
            if not bars:
                out[t] = {"points": [], "hi": None, "lo": None,
                          "open": None, "last": None, "count": 0}
                continue
            # Use today's 1m closes if exposed; fall back to current_price
            # alone when the structured stream is missing.
            closes_raw = bars.get("closes_1m") or bars.get("closes") or []
            closes: list[float] = []
            for c in closes_raw:
                try:
                    cf = float(c)
                except (TypeError, ValueError):
                    continue
                if cf > 0:
                    closes.append(cf)
            # Downsample to at most 60 points for a tight sparkline.
            n = len(closes)
            if n > 60:
                step = max(1, n // 60)
                pts = closes[::step][-60:]
            else:
                pts = list(closes)
            cur = _safe_float(bars.get("current_price"))
            if cur is not None and (not pts or pts[-1] != cur):
                pts.append(cur)
            if not pts:
                out[t] = {"points": [], "hi": None, "lo": None,
                          "open": None, "last": cur, "count": 0}
                continue
            hi = max(pts)
            lo = min(pts)
            out[t] = {
                "points": [round(p, 4) for p in pts],
                "hi": round(hi, 4),
                "lo": round(lo, 4),
                "open": round(pts[0], 4),
                "last": round(pts[-1], 4),
                "count": len(pts),
            }
        except Exception:
            out[t] = {"points": [], "hi": None, "lo": None,
                      "open": None, "last": None, "count": 0}
    return out


def _momentum_distances_per_ticker(
    m, tickers: list[str], di_blk: dict, wx_blk: dict
) -> dict:
    """v6.0.0 \u2014 distance-to-next-trigger metrics for the Momentum card.

    Surfaces the gap between the current state of each Phase 3 gate and
    the value at which it would flip to PASS. The dashboard renders this
    so an operator can answer "how close is this ticker to firing?"
    without reading raw DI/ADX feeds.

    Per-ticker entry:
      - adx_5m, adx_1m: current ADX values (None on warmup)
      - adx_threshold: 20.0 (Phase 3 spec gate)
      - adx_5m_gap: 20 \u2212 adx_5m  (positive = below trigger; <=0 = passing)
      - di_long_gap: threshold \u2212 di_plus_1m
      - di_short_gap: threshold \u2212 di_minus_1m
      - di_cross_gap: di_plus_1m \u2212 di_minus_1m  (positive = long-leaning)
      - vwap_gap_pct: (last \u2212 avwap) / avwap  (positive = above AVWAP)
      - ema9_gap_pct: (last \u2212 ema9_5m) / ema9_5m

    All fields null-safe; any missing input drops only that field.
    """
    out: dict = {}
    threshold = _safe_float(getattr(m, "TIGER_V2_DI_THRESHOLD", None))
    adx_fn = getattr(m, "v5_adx_1m_5m", None)
    for t in tickers:
        di = di_blk.get(t) or {}
        wx = wx_blk.get(t) or {}
        adx_1m = None
        adx_5m = None
        if adx_fn is not None:
            try:
                adx_raw = adx_fn(t) or {}
                adx_1m = _safe_float(adx_raw.get("adx_1m"))
                adx_5m = _safe_float(adx_raw.get("adx_5m"))
            except Exception:
                pass
        di_p_1m = _safe_float(di.get("di_plus_1m"))
        di_m_1m = _safe_float(di.get("di_minus_1m"))
        last = _safe_float(wx.get("last"))
        avwap = _safe_float(wx.get("avwap"))
        ema9 = _safe_float(wx.get("ema9_5m"))

        adx_5m_gap = None
        if adx_5m is not None:
            adx_5m_gap = round(20.0 - adx_5m, 3)
        di_long_gap = None
        di_short_gap = None
        if threshold is not None:
            if di_p_1m is not None:
                di_long_gap = round(threshold - di_p_1m, 3)
            if di_m_1m is not None:
                di_short_gap = round(threshold - di_m_1m, 3)
        di_cross_gap = None
        if di_p_1m is not None and di_m_1m is not None:
            di_cross_gap = round(di_p_1m - di_m_1m, 3)
        vwap_gap_pct = None
        if last is not None and avwap and avwap > 0.0:
            vwap_gap_pct = round((last - avwap) / avwap * 100.0, 4)
        ema9_gap_pct = None
        if last is not None and ema9 and ema9 > 0.0:
            ema9_gap_pct = round((last - ema9) / ema9 * 100.0, 4)

        out[t] = {
            "adx_1m": adx_1m,
            "adx_5m": adx_5m,
            "adx_threshold": 20.0,
            "adx_5m_gap": adx_5m_gap,
            "di_long_gap": di_long_gap,
            "di_short_gap": di_short_gap,
            "di_cross_gap": di_cross_gap,
            "vwap_gap_pct": vwap_gap_pct,
            "ema9_gap_pct": ema9_gap_pct,
        }
    return out


def _local_weather_per_ticker(m, tickers: list[str], di_blk: dict) -> dict:
    """v5.31.5 \u2014 build the per-ticker local-weather payload.

    Each entry surfaces the raw inputs (5m close, EMA9, last, AVWAP)
    plus the classified direction and the divergence flag (True iff
    local direction differs from QQQ AND neither is 'flat').
    """
    try:
        from engine.local_weather import classify_local_weather
    except Exception:
        return {}
    out: dict = {}
    cache = getattr(m, "_TICKER_REGIME", None) or {}
    global_dir = _global_qqq_direction(m)
    for tkr in tickers:
        try:
            sym = tkr.upper() if isinstance(tkr, str) else tkr
            entry = cache.get(sym) or {}
            close_5m = _safe_float(entry.get("last_close_5m"))
            ema9_5m = _safe_float(entry.get("ema9_5m"))
            last = _safe_float(entry.get("last"))
            avwap = _safe_float(entry.get("avwap"))
            di_entry = (di_blk or {}).get(tkr) or {}
            di_plus = _safe_float(di_entry.get("di_plus_1m"))
            di_minus = _safe_float(di_entry.get("di_minus_1m"))
            direction = classify_local_weather(
                close_5m, ema9_5m, last, avwap, di_plus, di_minus,
            )
            divergence = bool(
                global_dir is not None
                and direction != "flat"
                and global_dir != "flat"
                and direction != global_dir
            )
            out[tkr] = {
                "direction": direction,
                "divergence": divergence,
                "global_direction": global_dir,
                "last_close_5m": close_5m,
                "ema9_5m": ema9_5m,
                "last": last,
                "avwap": avwap,
            }
        except Exception:
            continue
    return out


def build_v510_snapshot(m, tickers: list[str], longs: dict, shorts: dict, prices: dict) -> dict:
    """Top-level v5.10 snapshot. Never raises; on internal error returns
    the partial dict accumulated so far so the parent /api/state still
    serializes successfully.
    """
    try:
        now_et = m._now_et()
        minute_hhmm = now_et.strftime("%H%M")
    except Exception:
        now_et = None
        minute_hhmm = ""
    # v5.20.5 \u2014 the just-closed bucket (current_minute - 1) is what
    # the WS consumer's _volumes dict is keyed by; the still-forming
    # current minute returns 0 until it closes. Compute once here so
    # the volume helper can do a single lookup against real volume.
    prev_minute_hhmm: str | None = None
    if now_et is not None:
        try:
            from volume_profile import previous_session_bucket as _prev_b

            prev_minute_hhmm = _prev_b(now_et)
        except Exception:
            prev_minute_hhmm = None
    out: dict = {
        "section_i_permit": _section_i_permit(m),
        "per_ticker_v510": {},
        "per_position_v510": {},
    }
    try:
        if minute_hhmm:
            vol = _vol_bucket_per_ticker(m, list(tickers), minute_hhmm, prev_minute_hhmm)
        else:
            vol = {
                t: {
                    "state": "COLDSTART",
                    "current_1m_vol": 0,
                    "baseline_at_minute": None,
                    "ratio": None,
                    "ratio_to_55bar_avg": None,
                    "days_available": None,
                    "lookback_days": None,
                    "lookup_bucket": None,
                }
                for t in tickers
            }
    except Exception:
        vol = {}
    try:
        bnd = _boundary_hold_per_ticker(m, list(tickers))
    except Exception:
        bnd = {}
    try:
        di_blk = _di_per_ticker(m, list(tickers))
    except Exception:
        di_blk = {}
    # v5.31.5 \u2014 per-ticker local weather block. Reads the
    # _TICKER_REGIME cache populated by _ticker_weather_tick_all() each
    # scan cycle, classifies the ticker's local direction with the same
    # rule the override gate uses, and flags whether it diverges from
    # the global QQQ direction. The dashboard renders this as a per-stock
    # Weather card and as the Weather column glyph in the permit matrix.
    try:
        wx_blk = _local_weather_per_ticker(m, list(tickers), di_blk)
    except Exception:
        wx_blk = {}
    # v6.0.0 \u2014 sparkline payload for each row's mini-chart.
    try:
        mini_blk = _mini_chart_per_ticker(m, list(tickers))
    except Exception:
        mini_blk = {}
    # v6.0.0 \u2014 distance-to-next-trigger metrics for the Momentum card.
    try:
        mom_blk = _momentum_distances_per_ticker(
            m, list(tickers), di_blk, wx_blk
        )
    except Exception:
        mom_blk = {}
    per_t: dict = {}
    for t in tickers:
        per_t[t] = {
            "vol_bucket": vol.get(t)
            or {
                "state": "COLDSTART",
                "current_1m_vol": 0,
                "baseline_at_minute": None,
                "ratio": None,
                "ratio_to_55bar_avg": None,
                "days_available": None,
                "lookback_days": None,
                "lookup_bucket": None,
            },
            "boundary_hold": bnd.get(t)
            or {
                "state": "ARMED",
                "side": None,
                "last_two_closes": [],
                "or_high": None,
                "or_low": None,
                "long_consecutive_outside": 0,
                "short_consecutive_outside": 0,
            },
            "di": di_blk.get(t)
            or {
                "di_plus_1m": None,
                "di_minus_1m": None,
                "di_plus_5m": None,
                "di_minus_5m": None,
                "threshold": None,
                "seed_bars": 0,
                "sufficient": False,
            },
            "weather": wx_blk.get(t)
            or {
                "direction": "flat",
                "divergence": False,
                "global_direction": None,
                "last_close_5m": None,
                "ema9_5m": None,
                "last": None,
                "avwap": None,
            },
            "mini_chart": mini_blk.get(t)
            or {
                "points": [],
                "hi": None,
                "lo": None,
                "open": None,
                "last": None,
                "count": 0,
            },
            "momentum_distances": mom_blk.get(t)
            or {
                "adx_1m": None,
                "adx_5m": None,
                "adx_threshold": 20.0,
                "adx_5m_gap": None,
                "di_long_gap": None,
                "di_short_gap": None,
                "di_cross_gap": None,
                "vwap_gap_pct": None,
                "ema9_gap_pct": None,
            },
        }
    out["per_ticker_v510"] = per_t
    try:
        out["per_position_v510"] = _per_position_v510(longs, shorts, prices)
    except Exception:
        pass
    return out
