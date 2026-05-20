"""simulator.mocks.fmp -- intercept FMP REST calls.

The bot's FMP endpoints (via urllib.request.urlopen):

  * https://financialmodelingprep.com/api/v3/quote/<TICKER>
        live quote -- returns [{symbol, price, change, ...}]
  * https://financialmodelingprep.com/api/v3/earning_calendar?...
        earnings calendar window -- returns [{symbol, date, eps, ...}]
  * https://financialmodelingprep.com/api/v3/profile/<TICKER>
        company profile (sector, marketCap) -- rarely used live

Quote prices are sourced from the simulator's bar feeder at the current
clock bucket. Earnings calendar is sourced from the scenario_state
override (default: empty).
"""
from __future__ import annotations

import io
import json
import urllib.parse
from typing import Any


def handle(req: Any, scenario_state: dict):
    """Return a urllib-style response object for FMP requests."""
    url = _get_url(req)
    scenario_state["fmp_calls"].append(url)

    parsed = urllib.parse.urlparse(url)
    path = parsed.path or ""

    from simulator.mocks.errors import (
        fmp_quote_failure, fmp_earnings_failure, http_error_resp,
    )

    if "/quote/" in path:
        # /api/v3/quote/AAPL,MSFT or /api/v3/quote/AAPL
        symbols_part = path.rsplit("/quote/", 1)[-1]
        symbols = [s.strip().upper() for s in symbols_part.split(",") if s.strip()]
        for sym in symbols:
            fail = fmp_quote_failure(sym, scenario_state)
            if fail is not None:
                http_error_resp(*fail)  # raises HTTPError
        payload = _quotes(symbols, scenario_state)
        return _resp(json.dumps(payload).encode())

    if "/earning_calendar" in path:
        fail = fmp_earnings_failure(scenario_state)
        if fail is not None:
            http_error_resp(*fail)
        from_date = _qs_get(parsed.query, "from")
        to_date = _qs_get(parsed.query, "to")
        payload = scenario_state.get("fmp_earnings_calendar", [])
        # Optional date filter
        if from_date or to_date:
            payload = [
                row for row in payload
                if (not from_date or row.get("date", "") >= from_date)
                and (not to_date or row.get("date", "") <= to_date)
            ]
        return _resp(json.dumps(payload).encode())

    if "/profile/" in path:
        symbols_part = path.rsplit("/profile/", 1)[-1]
        sym = symbols_part.strip().upper()
        payload = [{"symbol": sym, "sector": "Technology", "industry": "Software",
                    "mktCap": 2_000_000_000_000, "exchange": "NASDAQ"}]
        return _resp(json.dumps(payload).encode())

    # Unknown endpoint -- return empty list.
    return _resp(b"[]")


# ----- helpers --------------------------------------------------------


def _quotes(symbols, scenario_state):
    """Construct a per-symbol live-quote payload from the bar feeder."""
    out = []
    feeder = scenario_state.get("bar_feeder")
    clock = scenario_state.get("clock")
    bucket = clock.bucket_min() if clock else (9 * 60 + 30)
    for sym in symbols:
        price = None
        prev_close = None
        if feeder:
            for b in range(bucket, 0, -1):
                bar = feeder.bar_at(sym, b)
                if bar:
                    price = float(bar.get("close", 0) or 0)
                    break
            # PDC = previous-day close. We don't have a previous day in
            # most synthetic scenarios; fall back to "open of today".
            first = None
            for b in feeder._bars_by_ticker.get(sym, [])[:1]:
                first = b
            if first is not None:
                prev_close = float(first.get("open", 0) or 0)
        if price is None:
            price = 100.0
        if prev_close is None:
            prev_close = price
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0.0
        out.append({
            "symbol": sym,
            "price": price,
            "changesPercentage": change_pct,
            "change": change,
            "previousClose": prev_close,
            "marketCap": 2_000_000_000_000,
            "exchange": "NASDAQ",
            "volume": 1_000_000,
            "avgVolume": 5_000_000,
            "open": prev_close,
            "dayLow": min(prev_close, price),
            "dayHigh": max(prev_close, price),
            "yearLow": prev_close * 0.7,
            "yearHigh": prev_close * 1.3,
            "pe": 25.0,
            "earningsAnnouncement": None,
            "sharesOutstanding": 1_000_000_000,
            "timestamp": int(__import__("time").time()),
        })
    return out


def _qs_get(qs: str, key: str) -> str:
    parsed = urllib.parse.parse_qs(qs)
    vals = parsed.get(key, [])
    return vals[0] if vals else ""


def _get_url(req: Any) -> str:
    if hasattr(req, "full_url"):
        return req.full_url
    if hasattr(req, "get_full_url"):
        return req.get_full_url()
    return str(req)


def _resp(body: bytes):
    """Build a urlopen-compatible response."""
    stream = io.BytesIO(body)
    stream.headers = {"Content-Type": "application/json"}  # type: ignore[attr-defined]
    stream.status = 200  # type: ignore[attr-defined]

    def _read(*args):
        return body if not args else body[: args[0]]
    stream.read = _read  # type: ignore[attr-defined]

    # Context-manager protocol so `with urlopen(...) as resp:` works.
    stream.__enter__ = lambda self=stream: stream  # type: ignore[attr-defined]
    stream.__exit__ = lambda self, *a: False  # type: ignore[attr-defined]
    return stream
