"""simulator.mocks.mock_bar_fetch -- intercept the production 1m bar fetch.

The bot's bar fetch path lives in ``orb.bar_fetch``:

  _fetch_1min_bars_alpaca(ticker)  -- primary (Alpaca SIP)
  _fetch_1min_bars_yahoo(ticker)   -- legacy fallback

trade_genius re-imports both at module load and the public orchestrator
``trade_genius.fetch_1min_bars`` resolves the alpaca primary via its
*local* module namespace. So we patch THREE locations:

  - orb.bar_fetch._fetch_1min_bars_alpaca
  - orb.bar_fetch._fetch_1min_bars_yahoo
  - trade_genius._fetch_1min_bars_alpaca (the re-export, used by the
    orchestrator at trade_genius.py:3531)

The mock builds the dict shape the orchestrator expects directly from
the simulator BarFeeder + SimulatedClock -- no alpaca-py / urllib /
network involvement. This makes scan_loop reach OR-windowed bars
deterministically.

Dict shape (mirrors orb.bar_fetch.fetch return value):

  {
    "timestamps": [int unix-seconds, oldest first],
    "opens":   [float, ...],
    "highs":   [float, ...],
    "lows":    [float, ...],
    "closes":  [float, ...],
    "volumes": [int, ...],
    "current_price": float,    # last-known close at sim clock
    "pdc":           float,    # prior trading day close
  }
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from simulator.bar_feeder import BarFeeder, _bar_bucket


logger = logging.getLogger(__name__)


def install(bar_feeder: BarFeeder, scenario_state: dict,
            prior_close_lookup) -> dict:
    """Patch the production bar fetch helpers to read from `bar_feeder`.

    Args:
      bar_feeder: simulator.bar_feeder.BarFeeder loaded with the day's bars.
      scenario_state: shared dict; we read ``clock`` for the current sim time.
      prior_close_lookup: callable(ticker_upper: str) -> Optional[float].
        Returns the prior trading day's last close (or None). The runner
        owns the corpus-walking logic; we just call it for the ``pdc``
        field on each result dict.

    Returns dict of originals for uninstall().
    """
    from orb import bar_fetch as _orb_bf
    import trade_genius as _tg

    # Per-ticker pre-extracted columns cache + sorted bucket list. Profile
    # showed each scan_loop tick re-iterates the entire bars list and
    # re-extracts the same columns; for a 12-ticker day this was ~4M dict
    # accesses per simulated minute. Cache columns + bucket index once
    # per ticker; on each fetch, bisect bucket -> slice arrays.
    import bisect as _bisect
    _col_cache: dict[str, dict] = {}

    def _columns_for(sym: str) -> Optional[dict]:
        cached = _col_cache.get(sym)
        if cached is not None:
            return cached
        bars = bar_feeder._bars_by_ticker.get(sym) if bar_feeder else None
        if not bars:
            _col_cache[sym] = {}
            return None
        buckets: list[int] = []
        timestamps: list[int] = []
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        volumes: list[int] = []
        from simulator.bar_feeder import _bar_bucket
        for b in bars:
            bk = _bar_bucket(b)
            if bk is None:
                continue
            ts_unix = _row_ts_unix(b)
            if ts_unix is None:
                continue
            buckets.append(bk)
            timestamps.append(ts_unix)
            opens.append(float(b.get("open", 0) or 0))
            highs.append(float(b.get("high", 0) or 0))
            lows.append(float(b.get("low", 0) or 0))
            closes.append(float(b.get("close", 0) or 0))
            volumes.append(int(b.get("total_volume")
                               or b.get("iex_volume") or 0))
        cols = {
            "buckets": buckets,
            "timestamps": timestamps,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volumes": volumes,
        }
        _col_cache[sym] = cols
        return cols

    def _mk_fetch(source_name: str):
        def _fetch(ticker: str) -> Optional[dict]:
            sym = (ticker or "").strip().upper()
            if not sym:
                return None
            cols = _columns_for(sym)
            if not cols or not cols.get("closes"):
                return None
            clock = scenario_state.get("clock") if scenario_state else None
            bucket = 9 * 60 + 30 if clock is None else clock.bucket_min()
            # bisect_right(buckets, bucket - 1) excludes the bar AT the
            # current sim minute -- production cannot see a 1m bar's
            # close until the minute is over (the bar at HH:MM closes
            # at HH:MM:59). Including it caused a 1-minute look-ahead
            # vs the live engine: at sim clock=10:05 we'd return
            # current_price = close at bucket 1005, while production
            # at 10:05:00 wall-clock only knows close at bucket 1004.
            n = _bisect.bisect_right(cols["buckets"], bucket - 1)
            if n == 0:
                return None
            closes = cols["closes"][:n]
            pdc = 0.0
            try:
                pdc_val = prior_close_lookup(sym)
                if pdc_val is not None:
                    pdc = float(pdc_val)
            except Exception:
                pdc = 0.0
            return {
                "timestamps": cols["timestamps"][:n],
                "opens": cols["opens"][:n],
                "highs": cols["highs"][:n],
                "lows": cols["lows"][:n],
                "closes": closes,
                "volumes": cols["volumes"][:n],
                "current_price": closes[-1],
                "pdc": pdc,
                "_simulator_source": source_name,
            }
        return _fetch

    orig: dict = {
        "orb_bar_fetch._fetch_1min_bars_alpaca":
            _orb_bf._fetch_1min_bars_alpaca,
        "orb_bar_fetch._fetch_1min_bars_yahoo":
            _orb_bf._fetch_1min_bars_yahoo,
        "trade_genius._fetch_1min_bars_alpaca":
            _tg._fetch_1min_bars_alpaca,
    }
    fake_alpaca = _mk_fetch("simulator-alpaca")
    fake_yahoo = _mk_fetch("simulator-yahoo")
    _orb_bf._fetch_1min_bars_alpaca = fake_alpaca
    _orb_bf._fetch_1min_bars_yahoo = fake_yahoo
    _tg._fetch_1min_bars_alpaca = fake_alpaca

    # Also stub _alpaca_pdc so the real Alpaca path (if anything bypasses
    # _fetch_1min_bars_alpaca and reaches the daily-close fetcher) finds
    # our prior_close_lookup instead of hitting alpaca-py.
    orig["orb_bar_fetch._alpaca_pdc"] = _orb_bf._alpaca_pdc
    def _fake_alpaca_pdc(sym, _client=None):
        try:
            v = prior_close_lookup(sym.upper())
            return float(v) if v is not None else None
        except Exception:
            return None
    _orb_bf._alpaca_pdc = _fake_alpaca_pdc

    return orig


def uninstall(orig: dict) -> None:
    if not orig:
        return
    try:
        from orb import bar_fetch as _orb_bf
        import trade_genius as _tg
        if "orb_bar_fetch._fetch_1min_bars_alpaca" in orig:
            _orb_bf._fetch_1min_bars_alpaca = orig[
                "orb_bar_fetch._fetch_1min_bars_alpaca"]
        if "orb_bar_fetch._fetch_1min_bars_yahoo" in orig:
            _orb_bf._fetch_1min_bars_yahoo = orig[
                "orb_bar_fetch._fetch_1min_bars_yahoo"]
        if "trade_genius._fetch_1min_bars_alpaca" in orig:
            _tg._fetch_1min_bars_alpaca = orig[
                "trade_genius._fetch_1min_bars_alpaca"]
        if "orb_bar_fetch._alpaca_pdc" in orig:
            _orb_bf._alpaca_pdc = orig["orb_bar_fetch._alpaca_pdc"]
    except Exception:
        pass


def _row_ts_unix(bar: dict) -> Optional[int]:
    """Extract unix-seconds from a corpus row.

    Corpus rows carry the timestamp as `ts` (ISO8601 with Z) or
    `timestamp_utc` (same). Some synthetic rows just carry `et_bucket`.
    For et_bucket-only rows we fall back to a synthetic UTC time at
    that bucket on the feeder's date.
    """
    raw = bar.get("ts") or bar.get("timestamp_utc") or bar.get("timestamp") \
        or bar.get("t")
    if raw:
        try:
            if isinstance(raw, str) and raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            pass
    # Fallback: derive from et_bucket if a date hint exists.
    bk = _bar_bucket(bar)
    if bk is None:
        return None
    return None
