"""simulator.mocks.yahoo -- intercept Yahoo Finance HTTP calls.

The bot's Yahoo endpoint (via urllib.request.urlopen in orb.bar_fetch):

  * https://query1.finance.yahoo.com/v8/finance/chart/<TICKER>?...
        returns 1m chart data structured as chart.result[0] with
        indicators.quote[0].{open,high,low,close,volume} arrays and
        a parallel timestamp array.

Yahoo is the fallback path when Alpaca data fails. In simulator mode
we serve from the same bar feeder so the bot sees a coherent view.
"""
from __future__ import annotations

import io
import json
import urllib.parse
from typing import Any


def handle(req: Any, scenario_state: dict, bar_feeder=None):
    url = _get_url(req)
    scenario_state["yahoo_calls"].append(url)

    parsed = urllib.parse.urlparse(url)
    path = parsed.path or ""

    if "/v8/finance/chart/" in path:
        sym = path.rsplit("/v8/finance/chart/", 1)[-1].strip().upper()
        clock = scenario_state.get("clock")
        bucket = clock.bucket_min() if clock else (9 * 60 + 30)
        bars = bar_feeder.bars_up_to(sym, bucket) if bar_feeder else []
        payload = _yahoo_chart(sym, bars)
        return _resp(json.dumps(payload).encode())

    return _resp(b'{"chart": {"result": [], "error": null}}')


def _yahoo_chart(sym: str, bars):
    if not bars:
        return {"chart": {"result": [], "error": None}}
    timestamps = []
    opens, highs, lows, closes, vols = [], [], [], [], []
    for b in bars:
        ts = b.get("timestamp_utc") or b.get("timestamp") or ""
        try:
            from datetime import datetime
            iso = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
            ts_int = int(datetime.fromisoformat(iso).timestamp())
        except Exception:
            ts_int = 0
        timestamps.append(ts_int)
        opens.append(float(b.get("open", 0) or 0))
        highs.append(float(b.get("high", 0) or 0))
        lows.append(float(b.get("low", 0) or 0))
        closes.append(float(b.get("close", 0) or 0))
        vols.append(int(b.get("total_volume") or b.get("iex_volume") or 0))

    return {
        "chart": {
            "result": [{
                "meta": {
                    "currency": "USD", "symbol": sym, "exchangeName": "NMS",
                    "regularMarketPrice": closes[-1] if closes else 0.0,
                    "chartPreviousClose": opens[0] if opens else 0.0,
                },
                "timestamp": timestamps,
                "indicators": {
                    "quote": [{
                        "open": opens, "high": highs, "low": lows,
                        "close": closes, "volume": vols,
                    }]
                },
            }],
            "error": None,
        }
    }


def _get_url(req: Any) -> str:
    if hasattr(req, "full_url"):
        return req.full_url
    if hasattr(req, "get_full_url"):
        return req.get_full_url()
    return str(req)


def _resp(body: bytes):
    stream = io.BytesIO(body)
    stream.headers = {"Content-Type": "application/json"}  # type: ignore[attr-defined]
    stream.status = 200  # type: ignore[attr-defined]
    def _read(*args):
        return body if not args else body[: args[0]]
    stream.read = _read  # type: ignore[attr-defined]
    stream.__enter__ = lambda self=stream: stream  # type: ignore[attr-defined]
    stream.__exit__ = lambda self, *a: False  # type: ignore[attr-defined]
    return stream
