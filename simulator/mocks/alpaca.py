"""simulator.mocks.alpaca -- in-process replacements for alpaca-py clients.

The bot uses two alpaca-py clients:

  - alpaca.trading.client.TradingClient
        Order placement (LimitOrderRequest / MarketOrderRequest),
        position queries (get_all_positions, get_open_position,
        close_position), account info (get_account).

  - alpaca.data.historical.StockHistoricalDataClient
        Historical 1-minute bars (get_stock_bars).

Both are patched at module level. Anything that does
``from alpaca.trading.client import TradingClient`` *after* install()
sees the mock.

Order semantics:
  - submit_order() accepts limit orders, fills them immediately at the
    limit price, and records the fill in scenario_state["alpaca_orders"].
  - get_all_positions() returns the running paper position book.
  - close_position(symbol, qty=...) reduces the position; full close
    when qty omitted.
  - get_account() returns synthetic equity / buying_power derived from
    the starting account size (default $100k) + realized P&L from
    closed positions.

Bar semantics:
  - get_stock_bars(request) returns the bars in the corpus up to the
    current simulator clock time.
  - Each Bar exposes the fields the bot reads: timestamp, open, high,
    low, close, volume, trade_count, vwap.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from simulator.bar_feeder import BarFeeder


# ----- Mock Bar / Position / Order shapes ---------------------------


@dataclass
class _MockBar:
    """Mirrors the relevant subset of alpaca.data.models.Bar."""
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    trade_count: int = 0
    vwap: Optional[float] = None


@dataclass
class _MockBarSet:
    """Mirrors alpaca.data.models.BarSet (has a .data dict)."""
    data: Dict[str, List[_MockBar]] = field(default_factory=dict)

    def __iter__(self):
        # Some bot code iterates the barset for one symbol; mirror that.
        for symbol, bars in self.data.items():
            for b in bars:
                yield b


@dataclass
class _MockPosition:
    symbol: str
    qty: float
    side: str  # "long" / "short"
    avg_entry_price: float
    market_value: float = 0.0
    cost_basis: float = 0.0
    unrealized_pl: float = 0.0
    current_price: float = 0.0
    qty_available: float = 0.0


@dataclass
class _MockOrder:
    id: str
    symbol: str
    qty: float
    side: str  # "buy" / "sell"
    status: str  # "filled" / "accepted"
    limit_price: Optional[float] = None
    filled_avg_price: Optional[float] = None
    filled_qty: float = 0.0
    submitted_at: Optional[datetime] = None


@dataclass
class _MockAccount:
    account_number: str = "PA0SIMULATOR"
    equity: str = "100000.0"
    cash: str = "100000.0"
    buying_power: str = "400000.0"  # 4x for paper margin
    portfolio_value: str = "100000.0"
    pattern_day_trader: bool = False
    daytrade_count: int = 0
    status: str = "ACTIVE"


# ----- The mock clients themselves -----------------------------------


class MockTradingClient:
    """Stand-in for alpaca.trading.client.TradingClient."""

    def __init__(self, *args, **kwargs):
        # The bot constructs with (key, secret, paper=True). We accept anything.
        self._state = _SHARED_STATE

    def get_account(self) -> _MockAccount:
        equity = self._state.starting_equity + sum(self._state.realized_pl.values())
        return _MockAccount(
            equity=f"{equity:.2f}",
            cash=f"{equity:.2f}",
            buying_power=f"{equity * 4.0:.2f}",
            portfolio_value=f"{equity:.2f}",
        )

    def get_all_positions(self) -> List[_MockPosition]:
        return list(self._state.positions.values())

    def get_open_position(self, symbol: str) -> _MockPosition:
        from simulator.mocks.errors import MockAlpacaAPIError
        symbol = symbol.upper()
        pos = self._state.positions.get(symbol)
        if pos is None:
            raise MockAlpacaAPIError(404, f"position not found for {symbol}")
        return pos

    def submit_order(self, order_data: Any) -> _MockOrder:
        # Scenario-injected failure check (rate limit, service down).
        from simulator.mocks.errors import (
            alpaca_validate_order,
            alpaca_scenario_failure,
        )
        scenario_err = alpaca_scenario_failure(self._state.scenario_state)
        if scenario_err is not None:
            raise scenario_err
        # Input-shape validation (matches Alpaca's 422 errors).
        validation_err = alpaca_validate_order(order_data)
        if validation_err is not None:
            raise validation_err

        # order_data may be a LimitOrderRequest, MarketOrderRequest, or a
        # plain dict. Read the fields defensively.
        symbol = _attr(order_data, "symbol", "").upper()
        qty = float(_attr(order_data, "qty", 0) or 0)
        side = str(_attr(order_data, "side", "") or "")
        # alpaca-py's OrderSide enum: ``OrderSide.BUY`` stringifies as
        # "OrderSide.BUY"; normalize to lower buy/sell.
        side_l = side.split(".")[-1].lower() if "." in side else side.lower()
        if side_l not in ("buy", "sell"):
            side_l = "buy"
        limit_price = float(_attr(order_data, "limit_price", 0) or 0)
        order_type = (str(_attr(order_data, "type", "limit"))).split(".")[-1].lower()

        # Determine fill price: limit at the limit; market at the latest
        # known close for the symbol (from bar feeder).
        if order_type == "market" or limit_price <= 0:
            fill = self._state.last_price(symbol) or limit_price or 0.0
        else:
            fill = limit_price

        order_id = f"sim-{self._state.next_order_id()}"
        order = _MockOrder(
            id=order_id, symbol=symbol, qty=qty, side=side_l,
            status="filled",
            limit_price=limit_price if limit_price > 0 else None,
            filled_avg_price=fill, filled_qty=qty,
            submitted_at=datetime.now(timezone.utc),
        )

        # Apply the fill to the running paper book.
        self._state.apply_fill(symbol=symbol, side=side_l, qty=qty, fill_price=fill)

        # Record the order in scenario state for assertions.
        self._state.scenario_state["alpaca_orders"].append({
            "id": order_id, "symbol": symbol, "side": side_l, "qty": qty,
            "limit_price": limit_price, "filled_avg_price": fill,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        })
        return order

    def close_position(self, symbol: str, qty: Optional[float] = None) -> _MockOrder:
        from simulator.mocks.errors import MockAlpacaAPIError
        symbol = symbol.upper()
        pos = self._state.positions.get(symbol)
        if pos is None:
            raise MockAlpacaAPIError(404, f"position not found for {symbol}")
        close_qty = abs(qty if qty is not None else pos.qty)
        # Opposite side to flatten.
        side = "sell" if pos.side == "long" else "buy"
        # Synthesize a market-order fill at last known price.
        last = self._state.last_price(symbol) or pos.avg_entry_price
        return self.submit_order(_SimpleOrderReq(symbol, close_qty, side, "market", last))

    def cancel_orders(self) -> List[Any]:
        return []


class MockStockHistoricalDataClient:
    """Stand-in for alpaca.data.historical.StockHistoricalDataClient."""

    def __init__(self, *args, **kwargs):
        self._state = _SHARED_STATE

    def get_stock_bars(self, request: Any) -> _MockBarSet:
        """Return bars from the simulator feeder, up to the current
        simulator clock bucket."""
        # request.symbol_or_symbols may be a string or list.
        symbols = _attr(request, "symbol_or_symbols", []) or []
        if isinstance(symbols, str):
            symbols = [symbols]
        bucket = self._state.now_bucket_min()
        out: Dict[str, List[_MockBar]] = {}
        for sym in symbols:
            sym_u = sym.upper()
            raw = self._state.bar_feeder.bars_up_to(sym_u, bucket) if self._state.bar_feeder else []
            out[sym_u] = [
                _MockBar(
                    symbol=sym_u,
                    timestamp=_iso_to_dt(b.get("timestamp_utc") or b.get("timestamp") or ""),
                    open=float(b.get("open", 0) or 0),
                    high=float(b.get("high", 0) or 0),
                    low=float(b.get("low", 0) or 0),
                    close=float(b.get("close", 0) or 0),
                    volume=int(b.get("total_volume") or b.get("iex_volume") or 0),
                    trade_count=int(b.get("trade_count") or 0),
                    vwap=b.get("bar_vwap"),
                )
                for b in raw
            ]
        return _MockBarSet(data=out)


# ----- Shared state singleton ---------------------------------------


class _SimState:
    def __init__(self):
        self.bar_feeder: Optional[BarFeeder] = None
        self.scenario_state: dict = {}
        self.starting_equity: float = 100_000.0
        self.positions: Dict[str, _MockPosition] = {}
        self.realized_pl: Dict[str, float] = {}
        self._order_ctr = 0

    def reset(self):
        self.positions.clear()
        self.realized_pl.clear()
        self._order_ctr = 0

    def next_order_id(self) -> int:
        self._order_ctr += 1
        return self._order_ctr

    def now_bucket_min(self) -> int:
        clock = self.scenario_state.get("clock") if self.scenario_state else None
        if clock is None:
            return 9 * 60 + 30
        return clock.bucket_min()

    def last_price(self, symbol: str) -> Optional[float]:
        if not self.bar_feeder:
            return None
        bucket = self.now_bucket_min()
        # Walk backward from `bucket` to find the latest bar.
        for b in range(bucket, 0, -1):
            bar = self.bar_feeder.bar_at(symbol, b)
            if bar:
                return float(bar.get("close", 0) or 0)
        return None

    def apply_fill(self, symbol: str, side: str, qty: float, fill_price: float):
        existing = self.positions.get(symbol)
        if existing is None:
            # Open new position.
            pos_side = "long" if side == "buy" else "short"
            self.positions[symbol] = _MockPosition(
                symbol=symbol, qty=qty if pos_side == "long" else -qty,
                side=pos_side, avg_entry_price=fill_price,
                current_price=fill_price, cost_basis=fill_price * qty,
                market_value=fill_price * qty, qty_available=qty,
            )
            return

        # Close or reduce.
        if (existing.side == "long" and side == "sell") or \
           (existing.side == "short" and side == "buy"):
            close_qty = min(qty, abs(existing.qty))
            pnl = (fill_price - existing.avg_entry_price) * close_qty
            if existing.side == "short":
                pnl = -pnl
            self.realized_pl[symbol] = self.realized_pl.get(symbol, 0.0) + pnl
            remaining = abs(existing.qty) - close_qty
            if remaining <= 0:
                self.positions.pop(symbol, None)
            else:
                existing.qty = remaining if existing.side == "long" else -remaining
        else:
            # Adding to an existing position -- VWAP-weight the entry.
            new_total = abs(existing.qty) + qty
            new_avg = (existing.avg_entry_price * abs(existing.qty) + fill_price * qty) / new_total
            existing.qty = new_total if existing.side == "long" else -new_total
            existing.avg_entry_price = new_avg


_SHARED_STATE = _SimState()


# ----- Module installation -----------------------------------------


def install(bar_feeder: BarFeeder, scenario_state: dict) -> dict:
    """Patch alpaca.trading.client.TradingClient and
    alpaca.data.historical.StockHistoricalDataClient at module level."""
    _SHARED_STATE.reset()
    _SHARED_STATE.bar_feeder = bar_feeder
    _SHARED_STATE.scenario_state = scenario_state
    _SHARED_STATE.starting_equity = float(scenario_state.get("starting_equity", 100_000.0))
    # Expose the running position dict for assertions.
    scenario_state["alpaca_positions"] = _SHARED_STATE.positions
    scenario_state["alpaca_realized_pl"] = _SHARED_STATE.realized_pl

    orig: dict = {}

    # Inject mock modules. alpaca-py may or may not be installed; either
    # way we override.
    _ensure_modules()
    import alpaca.trading.client as _tc_mod
    import alpaca.data.historical as _dh_mod

    orig["TradingClient"] = getattr(_tc_mod, "TradingClient", None)
    orig["StockHistoricalDataClient"] = getattr(_dh_mod, "StockHistoricalDataClient", None)
    _tc_mod.TradingClient = MockTradingClient
    _dh_mod.StockHistoricalDataClient = MockStockHistoricalDataClient

    return orig


def uninstall(orig: dict) -> None:
    if not orig:
        return
    try:
        import alpaca.trading.client as _tc_mod
        import alpaca.data.historical as _dh_mod
        if orig.get("TradingClient"):
            _tc_mod.TradingClient = orig["TradingClient"]
        if orig.get("StockHistoricalDataClient"):
            _dh_mod.StockHistoricalDataClient = orig["StockHistoricalDataClient"]
    except Exception:
        pass


def _ensure_modules():
    """If alpaca-py isn't installed, inject empty modules so the bot's
    `from alpaca.trading.client import TradingClient` works against
    our mocks. If it IS installed, leave the real modules in place
    and we'll override the class attribute below."""
    if "alpaca" not in sys.modules:
        sys.modules["alpaca"] = types.ModuleType("alpaca")
    if "alpaca.trading" not in sys.modules:
        sys.modules["alpaca.trading"] = types.ModuleType("alpaca.trading")
    if "alpaca.trading.client" not in sys.modules:
        m = types.ModuleType("alpaca.trading.client")
        m.TradingClient = MockTradingClient  # type: ignore[attr-defined]
        sys.modules["alpaca.trading.client"] = m
    if "alpaca.trading.requests" not in sys.modules:
        m = types.ModuleType("alpaca.trading.requests")
        m.LimitOrderRequest = _SimpleOrderReq  # type: ignore[attr-defined]
        m.MarketOrderRequest = _SimpleOrderReq  # type: ignore[attr-defined]
        sys.modules["alpaca.trading.requests"] = m
    if "alpaca.trading.enums" not in sys.modules:
        m = types.ModuleType("alpaca.trading.enums")
        m.OrderSide = _OrderSide  # type: ignore[attr-defined]
        m.TimeInForce = _TimeInForce  # type: ignore[attr-defined]
        sys.modules["alpaca.trading.enums"] = m
    if "alpaca.data" not in sys.modules:
        sys.modules["alpaca.data"] = types.ModuleType("alpaca.data")
    if "alpaca.data.historical" not in sys.modules:
        m = types.ModuleType("alpaca.data.historical")
        m.StockHistoricalDataClient = MockStockHistoricalDataClient  # type: ignore[attr-defined]
        sys.modules["alpaca.data.historical"] = m
    if "alpaca.data.requests" not in sys.modules:
        m = types.ModuleType("alpaca.data.requests")
        m.StockBarsRequest = _SimpleOrderReq  # type: ignore[attr-defined]
        sys.modules["alpaca.data.requests"] = m
    if "alpaca.data.timeframe" not in sys.modules:
        m = types.ModuleType("alpaca.data.timeframe")
        m.TimeFrame = _TimeFrame  # type: ignore[attr-defined]
        sys.modules["alpaca.data.timeframe"] = m


# ----- Simple stand-in request / enum types --------------------------


class _SimpleOrderReq:
    """Catch-all stand-in for LimitOrderRequest / MarketOrderRequest /
    StockBarsRequest. Stores keyword args as attributes."""

    def __init__(self, symbol="", qty=0, side="", type="limit", limit_price=0.0,
                 time_in_force=None, symbol_or_symbols=None, timeframe=None,
                 start=None, end=None, limit=None, **kwargs):
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.type = type
        self.limit_price = limit_price
        self.time_in_force = time_in_force
        self.symbol_or_symbols = symbol_or_symbols
        self.timeframe = timeframe
        self.start = start
        self.end = end
        self.limit = limit
        for k, v in kwargs.items():
            setattr(self, k, v)


class _OrderSide:
    BUY = "buy"
    SELL = "sell"


class _TimeInForce:
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"


class _TimeFrame:
    Minute = "1Min"
    Day = "1Day"


# ----- helpers --------------------------------------------------------


def _attr(obj: Any, name: str, default=None):
    """Defensive attr/dict access -- callers may pass a dict, a request
    dataclass, or our _SimpleOrderReq."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _iso_to_dt(s: str) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
