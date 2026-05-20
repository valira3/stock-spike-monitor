"""simulator.diff -- shared logic for diffing simulator output vs live.

Pulled out of simulator.replay so annual.py + replay.py + anomaly.py can
all consume the same correlation surface. Two responsibilities:

  1. Load the bot's live trade log for a given date.
  2. Pair each simulator entry with the nearest live trade on
     (ticker, side) and label MATCH / DRIFT / SIM-ONLY / LIVE-ONLY.

A "DRIFT" verdict means we found a corresponding live trade for the
same (ticker, side) on the same day, but the entry price differs by
more than DRIFT_PRICE_THRESHOLD. That signals the bot fired but with
different geometry -- usually a timing or slippage divergence worth
investigating.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional


DRIFT_PRICE_THRESHOLD = 0.10  # dollars


def load_trade_log(date: str,
                   path: str = "data/trade_log.jsonl",
                   extra_paths: Optional[List[str]] = None) -> List[dict]:
    """Return all closed-trade rows whose entry or exit timestamp
    starts with `date` (YYYY-MM-DD).

    Multiple paths can be probed (production volume on /data, local
    /tmp during dev, snapshot branch, etc.). Empty list when nothing
    is found.
    """
    candidates = [path]
    if extra_paths:
        candidates.extend(extra_paths)

    rows: List[dict] = []
    for p in candidates:
        if not os.path.isfile(p):
            continue
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if _on_date(obj.get("entry_ts_utc"), date) or \
                   _on_date(obj.get("exit_ts_utc"), date):
                    rows.append(obj)
    return rows


def diff_one_day(sim_result: dict, live_trades: List[dict]) -> Dict:
    """Pair simulator entries with live trades on (ticker, side).

    Returns a dict with:
      rows:         per-pair label rows
      matched:      keys ("AAPL LONG", ...) that paired with MATCH/DRIFT
      sim_only:     keys present in sim but absent in live
      live_only:    keys present in live but absent in sim
      verdict:      "PASS" if no divergence, "DIVERGE" otherwise
      drift_count:  pairs that matched on key but differed on price
    """
    sim_entries = sim_result.get("entries", [])
    sim_by_key = {(e["ticker"], _side_norm(e["side"])): e for e in sim_entries}
    live_by_key: Dict[tuple, dict] = {}
    for t in live_trades:
        key = (str(t.get("ticker", "")).upper(),
               _side_norm(str(t.get("side", ""))))
        live_by_key[key] = t

    rows: List[dict] = []
    matched: List[str] = []
    sim_only: List[str] = []
    live_only: List[str] = []
    drift_count = 0

    for key, sim in sim_by_key.items():
        live = live_by_key.get(key)
        if live is None:
            sim_only.append(f"{key[0]} {key[1]}")
            rows.append({"key": key, "sim": sim, "live": None, "verdict": "SIM-ONLY"})
            continue
        sim_price = float(sim.get("price", 0) or 0)
        live_entry_px = float(live.get("entry_price") or live.get("price") or 0)
        delta = abs(sim_price - live_entry_px)
        verdict = "MATCH" if delta < DRIFT_PRICE_THRESHOLD else "DRIFT"
        if verdict == "DRIFT":
            drift_count += 1
        matched.append(f"{key[0]} {key[1]}")
        rows.append({"key": key, "sim": sim, "live": live, "verdict": verdict,
                     "price_delta": delta})

    for key in live_by_key:
        if key not in sim_by_key:
            live_only.append(f"{key[0]} {key[1]}")
            rows.append({"key": key, "sim": None, "live": live_by_key[key],
                         "verdict": "LIVE-ONLY"})

    return {
        "rows": rows,
        "matched": matched,
        "sim_only": sim_only,
        "live_only": live_only,
        "drift_count": drift_count,
        "verdict": "PASS" if (not sim_only and not live_only and drift_count == 0) else "DIVERGE",
    }


def _side_norm(s: str) -> str:
    s = (s or "").upper()
    if "LONG" in s or s == "BUY":
        return "LONG"
    if "SHORT" in s or s == "SELL":
        return "SHORT"
    return s


def _on_date(iso_ts: Optional[str], date: str) -> bool:
    if not iso_ts:
        return False
    return iso_ts.startswith(date)
