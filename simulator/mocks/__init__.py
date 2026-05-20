"""simulator.mocks -- install / uninstall the full mock service set.

Single entry point:

    from simulator.mocks import install_all, uninstall_all
    state = install_all(bar_feeder=feeder, scenario_state=state)
    # ... run scenario ...
    uninstall_all(state)

What gets patched:

    * Alpaca: alpaca.trading.client.TradingClient, alpaca.data.historical
      .StockHistoricalDataClient
    * FMP: urllib.request.urlopen for financialmodelingprep.com URLs
    * Yahoo: urllib.request.urlopen for query{1,2}.finance.yahoo.com URLs
    * Telegram: urllib.request.urlopen for api.telegram.org URLs
                (telegram_io.send_telegram POSTs there)
    * Railway: urllib.request.urlopen for backboard.railway.app URLs
                (no-op stub)

Each mock writes activity into the shared `scenario_state` dict so the
runner can assert what happened after the run.
"""
from __future__ import annotations

import urllib.request
from typing import Optional

from simulator.bar_feeder import BarFeeder
from simulator.mocks import alpaca as _alpaca_mock
from simulator.mocks import fmp as _fmp_mock
from simulator.mocks import telegram as _telegram_mock
from simulator.mocks import yahoo as _yahoo_mock


def install_all(bar_feeder: BarFeeder, scenario_state: dict) -> dict:
    """Install all mocks. Returns a dict of originals for uninstall_all().

    scenario_state is the shared mutable dict the runner uses to collect
    assertion-time observations (entries, exits, sends, etc.). All mocks
    write into well-known keys:
        scenario_state["telegram_sends"]: list of dicts
        scenario_state["alpaca_orders"]: list of dicts
        scenario_state["alpaca_positions"]: dict[symbol, dict]
        scenario_state["fmp_calls"]: list of URLs
        scenario_state["yahoo_calls"]: list of URLs
    """
    scenario_state.setdefault("telegram_sends", [])
    scenario_state.setdefault("alpaca_orders", [])
    scenario_state.setdefault("alpaca_positions", {})
    scenario_state.setdefault("fmp_calls", [])
    scenario_state.setdefault("yahoo_calls", [])
    scenario_state.setdefault("clock", None)  # set by runner before install

    orig: dict = {}

    # 1. Alpaca client classes -- patched at module level. The bot lazily
    #    imports them inside functions (orb.bar_fetch._alpaca_data_client,
    #    executors.base, etc.), so module-level swap takes effect.
    orig["alpaca"] = _alpaca_mock.install(bar_feeder, scenario_state)

    # 2. urllib.request.urlopen -- single-point-of-truth for FMP, Yahoo,
    #    Telegram, and Railway HTTP. We chain the URL dispatcher.
    orig["urlopen"] = urllib.request.urlopen

    def _routed_urlopen(req, *args, **kwargs):
        url = _get_req_url(req)
        if "financialmodelingprep.com" in url:
            return _fmp_mock.handle(req, scenario_state)
        if "query1.finance.yahoo.com" in url or "query2.finance.yahoo.com" in url:
            return _yahoo_mock.handle(req, scenario_state, bar_feeder)
        if "api.telegram.org" in url:
            return _telegram_mock.handle(req, scenario_state)
        if "backboard.railway" in url:
            return _railway_noop(req)
        # Unknown -- let real urlopen handle it (test, github, etc.)
        return orig["urlopen"](req, *args, **kwargs)

    urllib.request.urlopen = _routed_urlopen

    return orig


def uninstall_all(orig: dict) -> None:
    if not orig:
        return
    _alpaca_mock.uninstall(orig.get("alpaca") or {})
    if "urlopen" in orig:
        urllib.request.urlopen = orig["urlopen"]


def _get_req_url(req) -> str:
    """urllib.request.Request.full_url, or the string itself."""
    if hasattr(req, "full_url"):
        return req.full_url
    if hasattr(req, "get_full_url"):
        return req.get_full_url()
    return str(req)


def _railway_noop(req):
    """Railway GraphQL is no-op in simulator mode. The bot polls it for
    deployment status; we always return an empty success response."""
    import io
    payload = b'{"data": {}}'
    resp = io.BytesIO(payload)
    resp.headers = {}  # type: ignore[attr-defined]
    resp.status = 200  # type: ignore[attr-defined]
    return resp
