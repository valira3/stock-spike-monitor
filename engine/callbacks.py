"""v5.11.0 \u2014 engine.callbacks: structural seam for the per-minute scan loop.

`EngineCallbacks` is a `Protocol` that captures the side-effecting calls
`engine.scan.scan_loop` makes back into the bot lifecycle layer
(broker / Telegram / persistence / clock). Production wires a wrapper
around the existing `trade_genius` module-level functions; replay (PR 6)
will pass a record-only mock that appends trades to a list instead of
hitting the broker.

The exact method set mirrors what `scan_loop` actually calls today.
Pure-data accessors are included alongside side-effecting calls so a
record-only mock can stub the whole surface without reaching back into
trade_genius. Inner archival / cache / observability plumbing (e.g.
1m bar archive writes, the per-cycle bar cache, `eot_glue.*` taps)
remains accessed via the `_tg()` indirection inside `engine.scan` \u2014
those are state plumbing, not the gate seam.

Zero behavior change. The Protocol is structural; nothing checks it at
runtime, so the production `_ProdCallbacks` shim does not inherit from
it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class EngineCallbacks(Protocol):
    """Side-effect surface for the per-minute scan loop.

    `Any` is used in a few signatures to keep the Protocol honest about
    what trade_genius currently passes (e.g. `bars` is the loose dict
    returned by `fetch_1min_bars`); future PRs may tighten these.
    """

    # --- Clock ----------------------------------------------------------
    def now_et(self) -> datetime: ...
    def now_cdt(self) -> datetime: ...

    # --- Market data ----------------------------------------------------
    def fetch_1min_bars(self, ticker: str) -> Any: ...

    # --- Position store -------------------------------------------------
    def get_position(self, ticker: str, side: str) -> dict | None: ...
    def has_long(self, ticker: str) -> bool: ...
    def has_short(self, ticker: str) -> bool: ...

    # --- Position management (stops / trails) ---------------------------
    def manage_positions(self) -> None: ...
    def manage_short_positions(self) -> None: ...

    # --- Entry signals (gate compute) -----------------------------------
    def check_entry(self, ticker: str) -> tuple[bool, Any]: ...
    def check_short_entry(self, ticker: str) -> tuple[bool, Any]: ...

    # --- Order execution ------------------------------------------------
    def execute_entry(self, ticker: str, price: float) -> None: ...
    def execute_short_entry(self, ticker: str, price: float) -> None: ...
    def execute_exit(self, ticker: str, side: str, price: float, reason: str) -> None: ...

    # --- Operator surface -----------------------------------------------
    def alert(self, msg: str) -> None: ...
    def report_error(
        self, *, executor: str, code: str, severity: str, summary: str, detail: str
    ) -> None: ...

    # --- Broker order placement (record-only in replay) -----------------
    # v5.26.0 stage7: added so the replay harness can record orders that
    # the production trade_genius/broker code attempts to place, without
    # hitting Alpaca. Production is unaffected: the Protocol is structural
    # and `_ProdCallbacks` does not implement these (trade_genius reaches
    # broker.* directly).
    def place_limit_order(
        self, *, ticker: str, side: str, qty: int, limit_price: float, reason: str
    ) -> str: ...
    def place_stop_market_order(
        self, *, ticker: str, side: str, qty: int, stop_price: float, reason: str
    ) -> str: ...
    def place_market_order(self, *, ticker: str, side: str, qty: int, reason: str) -> str: ...
    def cancel_order(self, *, order_id: str) -> bool: ...

    # --- Telegram (record-only in replay) -------------------------------
    def send_telegram(self, *, chat: str, message: str) -> None: ...

    # --- Position store mutators (replay simulates state) ---------------
    def set_position(self, ticker: str, side: str, position: dict) -> None: ...
    def remove_position(self, ticker: str, side: str) -> None: ...
