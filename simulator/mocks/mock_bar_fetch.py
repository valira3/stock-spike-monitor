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

    def _mk_fetch(source_name: str):
        def _fetch(ticker: str) -> Optional[dict]:
            sym = (ticker or "").strip().upper()
            if not sym:
                return None
            clock = scenario_state.get("clock") if scenario_state else None
            if clock is None:
                bucket = 9 * 60 + 30
            else:
                bucket = clock.bucket_min()
            raw = bar_feeder.bars_up_to(sym, bucket) if bar_feeder else []
            if not raw:
                return None
            timestamps: list[int] = []
            opens: list[float] = []
            highs: list[float] = []
            lows: list[float] = []
            closes: list[float] = []
            volumes: list[int] = []
            for b in raw:
                ts_unix = _row_ts_unix(b)
                if ts_unix is None:
                    continue
                timestamps.append(ts_unix)
                opens.append(float(b.get("open", 0) or 0))
                highs.append(float(b.get("high", 0) or 0))
                lows.append(float(b.get("low", 0) or 0))
                closes.append(float(b.get("close", 0) or 0))
                volumes.append(int(b.get("total_volume")
                                   or b.get("iex_volume") or 0))
            if not closes:
                return None
            current_price = closes[-1]
            pdc = 0.0
            try:
                pdc_val = prior_close_lookup(sym)
                if pdc_val is not None:
                    pdc = float(pdc_val)
            except Exception:
                pdc = 0.0
            return {
                "timestamps": timestamps,
                "opens": opens,
                "highs": highs,
                "lows": lows,
                "closes": closes,
                "volumes": volumes,
                "current_price": current_price,
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
