"""simulator.mocks.mock_errors -- documented failure modes the bot must handle.

Real services return errors. The mocks should too, so the bot's defensive
code paths get exercised. Two injection mechanisms:

  1. INPUT VALIDATION (always on): bad calls produce the same error
     shape that the real service would. Examples:
       - Alpaca submit_order(qty=0): 422 Unprocessable Entity
       - Alpaca submit_order(symbol unknown): 404
       - Alpaca close_position(no_open): 404 position does not exist
       - FMP /quote/<bad_symbol>: empty array
       - Yahoo /chart/<bad_symbol>: chart.error present, result empty
       - Telegram bot_token invalid: 401 Unauthorized

  2. SCENARIO-INJECTED FAILURES (opt-in): set
     ``scenario_state["inject_failures"] = {...}`` to force specific
     error responses, mirroring real outages we have seen. Keys:
       - "alpaca_rate_limited" (int): N submit_order calls return 429
       - "alpaca_server_down" (bool): every submit_order raises 503
       - "fmp_quote_timeout" (set[str]): /quote for these tickers raises
       - "fmp_earnings_500" (bool): earning_calendar returns 500
       - "yahoo_chart_429" (int): N /chart calls return 429
       - "yahoo_chart_5xx_at_bucket" (int): all calls after this bucket 5xx
       - "telegram_unauthorized" (bool): every send returns 401
       - "telegram_chat_not_found" (bool): every send returns
         400 "chat not found"
       - "railway_no_token" (bool): GraphQL returns 401

References:
  - Alpaca: https://docs.alpaca.markets/reference/postorder
  - FMP:    https://site.financialmodelingprep.com/developer/docs
  - Yahoo:  query1.finance.yahoo.com observed responses
  - Telegram bot API: https://core.telegram.org/bots/api#making-requests

The bot's existing defensive code paths (try/except + retry + fail-open
to fallback source) are what we want to validate. Mock errors should
match the actual wire-shape so the bot can't tell the difference.
"""
from __future__ import annotations

import io
import json
from typing import Any, Dict, Optional


class MockAlpacaAPIError(Exception):
    """Mirrors alpaca.common.exceptions.APIError shape:
    has .status_code and .response (dict-like with 'message')."""

    def __init__(self, status_code: int, message: str, code: Optional[int] = None):
        super().__init__(f"{status_code} {message}")
        self.status_code = status_code
        self.code = code
        self.response = {"message": message, "code": code or status_code}


def alpaca_validate_order(order_data: Any) -> Optional[MockAlpacaAPIError]:
    """Run the same shape-of-input validation Alpaca enforces. Returns
    a MockAlpacaAPIError if the input is invalid, else None."""
    qty = _attr(order_data, "qty", None)
    symbol = _attr(order_data, "symbol", None)
    side = _attr(order_data, "side", None)
    limit_price = _attr(order_data, "limit_price", None)
    order_type = _attr(order_data, "type", "limit")

    if not symbol or not isinstance(symbol, str) or len(symbol) > 10:
        return MockAlpacaAPIError(422, "symbol is required and must be <= 10 chars")
    try:
        qty_f = float(qty) if qty is not None else 0.0
    except Exception:
        return MockAlpacaAPIError(422, "qty must be numeric")
    if qty_f <= 0:
        return MockAlpacaAPIError(422, "qty must be positive")
    side_l = str(side).split(".")[-1].lower() if side else ""
    if side_l not in ("buy", "sell"):
        return MockAlpacaAPIError(422, f"side must be buy|sell, got {side!r}")
    if str(order_type).split(".")[-1].lower() == "limit":
        try:
            lp = float(limit_price or 0)
        except Exception:
            return MockAlpacaAPIError(422, "limit_price must be numeric for limit orders")
        if lp <= 0:
            return MockAlpacaAPIError(422, "limit_price > 0 required for limit orders")
    return None


def alpaca_scenario_failure(scenario_state: dict) -> Optional[MockAlpacaAPIError]:
    """Check the scenario-injected failure registry. Decrements the
    counter if one fires."""
    inj = scenario_state.get("inject_failures") or {}
    if inj.get("alpaca_server_down"):
        return MockAlpacaAPIError(503, "service unavailable")
    rl = inj.get("alpaca_rate_limited", 0)
    if isinstance(rl, int) and rl > 0:
        inj["alpaca_rate_limited"] = rl - 1
        return MockAlpacaAPIError(429, "rate limit exceeded")
    return None


def fmp_quote_failure(symbol: str, scenario_state: dict):
    """Returns (status_code, body_bytes) tuple if injected; else None."""
    inj = scenario_state.get("inject_failures") or {}
    timeouts = inj.get("fmp_quote_timeout") or set()
    if isinstance(timeouts, (list, set, tuple)) and symbol.upper() in {s.upper() for s in timeouts}:
        return (504, b'{"error":"gateway timeout"}')
    return None


def fmp_earnings_failure(scenario_state: dict):
    inj = scenario_state.get("inject_failures") or {}
    if inj.get("fmp_earnings_500"):
        return (500, b'{"error":"internal server error"}')
    return None


def yahoo_chart_failure(scenario_state: dict, current_bucket: int = 0):
    inj = scenario_state.get("inject_failures") or {}
    cnt = inj.get("yahoo_chart_429", 0)
    if isinstance(cnt, int) and cnt > 0:
        inj["yahoo_chart_429"] = cnt - 1
        return (429, b'{"chart":{"result":null,"error":{"code":"Too Many Requests"}}}')
    after = inj.get("yahoo_chart_5xx_at_bucket")
    if isinstance(after, int) and current_bucket >= after:
        return (502, b'{"chart":{"result":null,"error":{"code":"Bad Gateway"}}}')
    return None


def telegram_failure(scenario_state: dict):
    inj = scenario_state.get("inject_failures") or {}
    if inj.get("telegram_unauthorized"):
        return (401, b'{"ok":false,"error_code":401,"description":"Unauthorized"}')
    if inj.get("telegram_chat_not_found"):
        return (400, b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}')
    return None


def railway_failure(scenario_state: dict):
    inj = scenario_state.get("inject_failures") or {}
    if inj.get("railway_no_token"):
        return (401, b'{"errors":[{"message":"Unauthorized"}]}')
    return None


# ----- response builders ----------------------------------------------


def http_error_resp(status: int, body: bytes):
    """urlopen treats 4xx/5xx as HTTPError. We mimic that semantics by
    raising a synthetic HTTPError-shaped exception."""
    import urllib.error
    raise urllib.error.HTTPError(
        url="http://mock",
        code=status,
        msg=f"HTTP {status}",
        hdrs={"Content-Type": "application/json"},
        fp=io.BytesIO(body),
    )


# ----- small helpers ---------------------------------------------------


def _attr(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
