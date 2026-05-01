"""v5.26.0 stage7 \u2014 real-trade_genius replay harness.

Drives the real `trade_genius` module (v5.26.0 spec-strict) per-minute
over an archived day with a record-only callbacks layer. Replaces the
v5.11 SimpleNamespace `_install_fake_tg` stub harness; the goal is
correlation between replay and prod.

Architecture:

  * `setup_real_tg_environment()` sets `SSM_SMOKE_TEST=1` plus dummy
    Telegram credentials so `import trade_genius` succeeds without
    hitting the Telegram API or starting the scheduler / dashboard
    threads.
  * `RecordOnlyBrokerLayer` monkey-patches the broker order-placement
    surface inside trade_genius / broker.* so any LIMIT / STOP_MARKET /
    MARKET / cancel that the real code attempts is captured to a list.
  * `RecordOnlyTelegram` monkey-patches `send_telegram` /
    `send_telegram_alert` to capture messages.
  * `BacktestClock` is the single source of truth for the simulated
    wall-clock; `_now_et` / `_now_cdt` are monkey-patched to read it.
  * `RecordOnlyCallbacks` satisfies `engine.callbacks.EngineCallbacks`
    by delegating most methods to the REAL trade_genius / broker code.
    `manage_positions`, `check_entry`, `execute_entry`, etc. all hit
    the production code paths \u2014 only side effects (orders, Telegram,
    persistence) are intercepted.

CLI:

    python -m backtest.replay_v511_full \\
        --date 2026-04-30 \\
        --bars-dir /home/user/workspace/today_bars \\
        --output /home/user/workspace/v526_today_backtest/raw_run.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys as _sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CDT = ZoneInfo("America/Chicago")

logger = logging.getLogger("backtest.replay_v511")


# ---------------------------------------------------------------------------
# Bar loading (unchanged from v5.11 harness)
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_day_bars(bars_dir: Path, date_str: str, ticker: str) -> list[dict]:
    """Load pre-market + RTH 1m bars for `ticker` on `date_str`."""
    rth = _load_jsonl(bars_dir / date_str / f"{ticker}.jsonl")
    pre = _load_jsonl(bars_dir / date_str / "premarket" / f"{ticker}.jsonl")
    bars = list(pre) + list(rth)
    out: list[dict] = []
    for b in bars:
        dt = _parse_ts(b.get("ts"))
        if dt is None:
            continue
        b = dict(b)
        b["_dt"] = dt
        out.append(b)
    out.sort(key=lambda b: b["_dt"])
    return out


# ---------------------------------------------------------------------------
# Step 1 \u2014 environment setup so real trade_genius imports cleanly
# ---------------------------------------------------------------------------


def setup_real_tg_environment() -> None:
    """Set env vars so `import trade_genius` succeeds without network I/O.

    `SSM_SMOKE_TEST=1` is the canonical escape hatch in trade_genius.py
    that skips the Telegram bot loop, the scheduler thread, and the
    startup catch-up. The dummy `TELEGRAM_TOKEN` / `CHAT_ID` keep the
    module-level `Application.builder()` happy. The Alpaca executor
    bootstrap fails closed when `PAPER_KEY` is missing, which is fine
    \u2014 the harness installs a record-only broker layer afterwards.
    """
    os.environ.setdefault("SSM_SMOKE_TEST", "1")
    os.environ.setdefault("TELEGRAM_TOKEN", "0:backtest_dummy_token")
    os.environ.setdefault("CHAT_ID", "0")
    os.environ.setdefault("DASHBOARD_PASSWORD", "")
    # Make sure trade_genius does not try to seed from external data.
    os.environ.setdefault("TG_BACKTEST_MODE", "1")


# ---------------------------------------------------------------------------
# Step 2 \u2014 simulated clock
# ---------------------------------------------------------------------------


@dataclass
class BacktestClock:
    """Single source of truth for `_now_et` / `_now_cdt` during replay.

    The driver advances `now` minute-by-minute before each
    `scan_loop` call. Production-code paths that read the clock via
    `trade_genius._now_et()` (monkey-patched at install time) see this
    deterministic value instead of the wall clock.
    """

    now: datetime = field(default_factory=lambda: datetime(2026, 4, 30, 9, 35, tzinfo=ET))

    def now_et(self) -> datetime:
        return self.now

    def now_cdt(self) -> datetime:
        return self.now.astimezone(CDT)


# ---------------------------------------------------------------------------
# Step 3 \u2014 record-only broker layer
# ---------------------------------------------------------------------------


@dataclass
class RecordOnlyBrokerLayer:
    """Captures every order-placement attempt from the real broker code.

    Strategy: monkey-patch the public `client` / `alpaca` / executor
    surface inside trade_genius so the production order-routing code
    paths run, but the actual REST calls are no-ops that append a
    record dict. Each record has: ts, ticker, side, order_type, qty,
    price (limit/stop), reason, order_id.
    """

    orders: list[dict] = field(default_factory=list)
    cancellations: list[dict] = field(default_factory=list)
    # Position closes captured via the tg.close_position /
    # tg.close_short_position wrappers installed in
    # `install_record_only_layers`. Each record has the entry side
    # ("long" / "short"), exit price + reason, and the entry snapshot
    # we read off the position store immediately before tg removed it.
    closes: list[dict] = field(default_factory=list)
    _next_id: int = 0

    def _alloc_id(self) -> str:
        self._next_id += 1
        return f"backtest-{self._next_id:06d}"

    def place_limit_order(
        self,
        *,
        ticker: str,
        side: str,
        qty: int,
        limit_price: float,
        reason: str,
        ts: datetime | None = None,
    ) -> str:
        oid = self._alloc_id()
        self.orders.append(
            {
                "ts": (ts or datetime.now(tz=ET)).isoformat(),
                "ticker": ticker,
                "side": side,
                "order_type": "LIMIT",
                "qty": int(qty),
                "limit_price": float(limit_price),
                "stop_price": None,
                "reason": reason,
                "order_id": oid,
            }
        )
        return oid

    def place_stop_market_order(
        self,
        *,
        ticker: str,
        side: str,
        qty: int,
        stop_price: float,
        reason: str,
        ts: datetime | None = None,
    ) -> str:
        oid = self._alloc_id()
        self.orders.append(
            {
                "ts": (ts or datetime.now(tz=ET)).isoformat(),
                "ticker": ticker,
                "side": side,
                "order_type": "STOP_MARKET",
                "qty": int(qty),
                "limit_price": None,
                "stop_price": float(stop_price),
                "reason": reason,
                "order_id": oid,
            }
        )
        return oid

    def place_market_order(
        self, *, ticker: str, side: str, qty: int, reason: str, ts: datetime | None = None
    ) -> str:
        oid = self._alloc_id()
        self.orders.append(
            {
                "ts": (ts or datetime.now(tz=ET)).isoformat(),
                "ticker": ticker,
                "side": side,
                "order_type": "MARKET",
                "qty": int(qty),
                "limit_price": None,
                "stop_price": None,
                "reason": reason,
                "order_id": oid,
            }
        )
        return oid

    def cancel_order(self, *, order_id: str, ts: datetime | None = None) -> bool:
        self.cancellations.append(
            {
                "ts": (ts or datetime.now(tz=ET)).isoformat(),
                "order_id": order_id,
            }
        )
        return True


# ---------------------------------------------------------------------------
# Step 4 \u2014 record-only Telegram
# ---------------------------------------------------------------------------


@dataclass
class RecordOnlyTelegram:
    messages: list[dict] = field(default_factory=list)

    def send(self, message: str, chat: str = "") -> None:
        self.messages.append({"chat": chat, "message": str(message)})


# ---------------------------------------------------------------------------
# Step 5 \u2014 record-only position store
# ---------------------------------------------------------------------------


@dataclass
class RecordOnlyPositionStore:
    """Wraps trade_genius.positions / .short_positions so the harness
    can read mutations the real code performs.

    Production code mutates the dicts in place (e.g.
    `positions[ticker] = {...}`); we simply snapshot them through the
    callback get/has/set/remove surface.
    """

    positions: dict[str, dict] = field(default_factory=dict)
    short_positions: dict[str, dict] = field(default_factory=dict)

    def get(self, ticker: str, side: str) -> dict | None:
        if str(side).lower() == "long":
            return self.positions.get(ticker)
        return self.short_positions.get(ticker)

    def has_long(self, ticker: str) -> bool:
        return ticker in self.positions

    def has_short(self, ticker: str) -> bool:
        return ticker in self.short_positions

    def set(self, ticker: str, side: str, position: dict) -> None:
        if str(side).lower() == "long":
            self.positions[ticker] = position
        else:
            self.short_positions[ticker] = position

    def remove(self, ticker: str, side: str) -> None:
        if str(side).lower() == "long":
            self.positions.pop(ticker, None)
        else:
            self.short_positions.pop(ticker, None)


# ---------------------------------------------------------------------------
# Step 6 \u2014 install the record-only layers into the real trade_genius
# ---------------------------------------------------------------------------


def install_record_only_layers(
    tg,
    clock: BacktestClock,
    broker_layer: RecordOnlyBrokerLayer,
    telegram_layer: RecordOnlyTelegram,
    position_store: RecordOnlyPositionStore,
) -> None:
    """Monkey-patch trade_genius so order placement / Telegram / clock
    read from the harness's recording surfaces.

    Patches applied:
      * `trade_genius._now_et` / `_now_cdt` \u2192 BacktestClock
      * `trade_genius.fetch_1min_bars` \u2192 reads from harness bar store
        (avoids Yahoo Finance network calls per ticker per minute)
      * `trade_genius.get_fmp_quote` \u2192 synthesizes from harness bars
        (avoids financialmodelingprep.com network calls)
      * `trade_genius._cycle_bar_cache` \u2192 cleared each tick advance
      * `trade_genius.send_telegram` \u2192 RecordOnlyTelegram.send
      * `trade_genius.positions` / `.short_positions` already exist as
        module dicts; we replace the references with the harness's
        store dicts so the production code mutates ours instead.
      * `trade_genius.client.*` order-placement surface (if present)
        \u2192 RecordOnlyBrokerLayer methods. Different bot versions wire
        Alpaca through different shim names; this routine attempts a
        few common ones and silently skips any that aren't present.
    """
    # Clock.
    tg._now_et = clock.now_et
    tg._now_cdt = clock.now_cdt

    # Bar fetch \u2014 the engine's `fetch_1min_bars` is called from many
    # sites in trade_genius (gate snapshot, position management, exit
    # checks, etc.) and hits Yahoo Finance directly. Patch it to read
    # from the harness bar store, returning the prod-shape dict that
    # downstream code expects.
    # We close over `_bars_owner`; the driver sets `_bars_owner["bars"]`
    # after this function returns via `tg._harness_bars_owner`.
    _bars_owner = {"bars": None, "clock": clock}

    def _harness_fetch_1min_bars(ticker: str):
        bars = (_bars_owner["bars"] or {}).get(ticker.upper()) or []
        if not bars:
            return None
        cutoff = clock.now.astimezone(timezone.utc)
        visible = [b for b in bars if b["_dt"] <= cutoff]
        if not visible:
            return None
        opens = [b.get("open") for b in visible]
        highs = [b.get("high") for b in visible]
        lows = [b.get("low") for b in visible]
        closes = [b.get("close") for b in visible]
        vols = [
            b.get("iex_volume") if b.get("iex_volume") is not None else b.get("volume")
            for b in visible
        ]
        timestamps = [int(b["_dt"].timestamp()) for b in visible]
        last_close = next((c for c in reversed(closes) if c is not None), 0.0)
        # `pdc` is previous-day close; harness bars don't carry that, so
        # fall back to the first bar's open as a stand-in. Production paths
        # that strictly need PDC will short-circuit, which is fine for replay.
        first_open = next((o for o in opens if o is not None), last_close)
        return {
            "timestamps": timestamps,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volumes": vols,
            "current_price": last_close or 0.0,
            "pdc": first_open or 0.0,
        }

    def _harness_get_fmp_quote(ticker: str):
        bars = _harness_fetch_1min_bars(ticker)
        if not bars or not bars.get("current_price"):
            return None
        cp = bars["current_price"]
        # Synthesize a tight bid/ask quote around the last close. Spec
        # uses bid * 0.999 (long limit) and ask * 1.001 (short limit) so
        # spread of $0.02 is fine for replay; real spreads vary.
        return {
            "symbol": ticker,
            "price": cp,
            "bid": round(cp - 0.01, 2),
            "ask": round(cp + 0.01, 2),
            "timestamp": int(clock.now.timestamp() * 1000),
        }

    tg.fetch_1min_bars = _harness_fetch_1min_bars
    tg.get_fmp_quote = _harness_get_fmp_quote
    # Expose attach hook so the driver can plug in bars_by_ticker after
    # the layers are installed.
    tg._harness_bars_owner = _bars_owner

    # Telegram.
    if hasattr(tg, "send_telegram"):
        tg.send_telegram = lambda msg, *a, **kw: telegram_layer.send(msg)
    if hasattr(tg, "send_telegram_alert"):
        tg.send_telegram_alert = lambda msg, *a, **kw: telegram_layer.send(msg)
    if hasattr(tg, "send_startup_message"):
        tg.send_startup_message = lambda *a, **kw: None

    # Position dicts \u2014 swap module-level references so production code
    # mutates the harness-owned dicts.
    tg.positions = position_store.positions
    tg.short_positions = position_store.short_positions

    # Close-fn wrappers \u2014 production exit paths (manage_positions /
    # manage_short_positions / eod_close / sentinel rails) all funnel
    # through `tg.close_position` / `tg.close_short_position`. Wrap
    # both module attributes so every close (regardless of caller)
    # is captured into broker_layer.closes with full entry context.
    _orig_close_long = getattr(tg, "close_position", None)
    _orig_close_short = getattr(tg, "close_short_position", None)

    def _wrapped_close_position(ticker, price, reason="STOP", suppress_signal=False):
        # Snapshot the position BEFORE tg deletes it from the dict.
        pos_snap = position_store.positions.get(ticker)
        try:
            res = (
                _orig_close_long(ticker, price, reason, suppress_signal=suppress_signal)
                if _orig_close_long is not None
                else None
            )
        finally:
            broker_layer.closes.append(
                {
                    "ts": clock.now.isoformat(),
                    "ticker": ticker,
                    "side": "long",
                    "exit_price": float(price) if price is not None else None,
                    "reason": reason,
                    "entry_price": (pos_snap or {}).get("entry_price"),
                    "shares": (pos_snap or {}).get("shares"),
                    "entry_ts_utc": (pos_snap or {}).get("entry_ts_utc")
                    or (pos_snap or {}).get("entry_time"),
                }
            )
        return res

    def _wrapped_close_short_position(ticker, price, reason="STOP", suppress_signal=False):
        pos_snap = position_store.short_positions.get(ticker)
        try:
            res = (
                _orig_close_short(ticker, price, reason, suppress_signal=suppress_signal)
                if _orig_close_short is not None
                else None
            )
        finally:
            broker_layer.closes.append(
                {
                    "ts": clock.now.isoformat(),
                    "ticker": ticker,
                    "side": "short",
                    "exit_price": float(price) if price is not None else None,
                    "reason": reason,
                    "entry_price": (pos_snap or {}).get("entry_price"),
                    "shares": (pos_snap or {}).get("shares"),
                    "entry_ts_utc": (pos_snap or {}).get("entry_ts_utc")
                    or (pos_snap or {}).get("entry_time"),
                }
            )
        return res

    if _orig_close_long is not None:
        tg.close_position = _wrapped_close_position
    if _orig_close_short is not None:
        tg.close_short_position = _wrapped_close_short_position

    # Broker order surface \u2014 trade_genius.client is the Alpaca shim
    # (paper book). When `SSM_SMOKE_TEST=1` it's likely a stub; we
    # replace its order methods with recording wrappers anyway so
    # any code path that tries to place an order ends up here.
    _client = getattr(tg, "client", None)
    if _client is not None:

        def _submit_limit(symbol, qty, side, limit_price, **kw):
            return broker_layer.place_limit_order(
                ticker=symbol,
                side=str(side),
                qty=int(qty),
                limit_price=float(limit_price),
                reason=kw.get("reason", "unknown"),
                ts=clock.now,
            )

        def _submit_stop(symbol, qty, side, stop_price, **kw):
            return broker_layer.place_stop_market_order(
                ticker=symbol,
                side=str(side),
                qty=int(qty),
                stop_price=float(stop_price),
                reason=kw.get("reason", "unknown"),
                ts=clock.now,
            )

        def _submit_market(symbol, qty, side, **kw):
            return broker_layer.place_market_order(
                ticker=symbol,
                side=str(side),
                qty=int(qty),
                reason=kw.get("reason", "unknown"),
                ts=clock.now,
            )

        def _cancel(order_id, **kw):
            return broker_layer.cancel_order(order_id=str(order_id), ts=clock.now)

        for name in ("submit_limit_order", "place_limit_order"):
            if hasattr(_client, name):
                setattr(_client, name, _submit_limit)
        for name in ("submit_stop_order", "place_stop_market_order"):
            if hasattr(_client, name):
                setattr(_client, name, _submit_stop)
        for name in ("submit_market_order", "place_market_order"):
            if hasattr(_client, name):
                setattr(_client, name, _submit_market)
        for name in ("cancel_order",):
            if hasattr(_client, name):
                setattr(_client, name, _cancel)


# ---------------------------------------------------------------------------
# Step 7 \u2014 RecordOnlyCallbacks (real-tg-aware)
# ---------------------------------------------------------------------------


@dataclass
class RecordOnlyCallbacks:
    """`EngineCallbacks` impl that delegates most methods to real tg.

    The harness owns the clock, bars, position store, broker recorder,
    and telegram recorder. The PRODUCTION trade_genius / broker code
    runs unchanged for entry-signal evaluation, position management,
    and exit handling \u2014 only the side effects route to the record-
    only layers via the monkey-patches installed by
    `install_record_only_layers`.
    """

    tg: Any  # the real trade_genius module
    clock: BacktestClock
    bars_by_ticker: dict[str, list[dict]]
    broker_layer: RecordOnlyBrokerLayer
    telegram_layer: RecordOnlyTelegram
    position_store: RecordOnlyPositionStore

    entries: list[dict] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)
    short_entries: list[dict] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    fetch_calls: list[str] = field(default_factory=list)
    ticks: list[datetime] = field(default_factory=list)

    # ---- Clock -------------------------------------------------------
    def now_et(self) -> datetime:
        return self.clock.now_et()

    def now_cdt(self) -> datetime:
        return self.clock.now_cdt()

    # ---- Market data -------------------------------------------------
    def fetch_1min_bars(self, ticker: str) -> Any:
        self.fetch_calls.append(ticker)
        all_bars = self.bars_by_ticker.get(ticker.upper()) or []
        cutoff = self.clock.now.astimezone(timezone.utc)
        visible = [b for b in all_bars if b["_dt"] <= cutoff]
        if not visible:
            return None
        opens = [b.get("open") for b in visible]
        highs = [b.get("high") for b in visible]
        lows = [b.get("low") for b in visible]
        closes = [b.get("close") for b in visible]
        vols = [
            b.get("iex_volume") if b.get("iex_volume") is not None else b.get("volume")
            for b in visible
        ]
        timestamps = [int(b["_dt"].timestamp()) for b in visible]
        last_close = next((c for c in reversed(closes) if c is not None), None)
        return {
            "current_price": last_close,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volumes": vols,
            "timestamps": timestamps,
        }

    # ---- Position store ----------------------------------------------
    def get_position(self, ticker: str, side: str) -> dict | None:
        return self.position_store.get(ticker, side)

    def has_long(self, ticker: str) -> bool:
        return self.position_store.has_long(ticker)

    def has_short(self, ticker: str) -> bool:
        return self.position_store.has_short(ticker)

    def set_position(self, ticker: str, side: str, position: dict) -> None:
        self.position_store.set(ticker, side, position)

    def remove_position(self, ticker: str, side: str) -> None:
        self.position_store.remove(ticker, side)

    # ---- Position management \u2014 delegate to real tg ------------------
    def manage_positions(self) -> None:
        try:
            self.tg.manage_positions()
        except Exception as e:
            logger.debug("manage_positions raised: %s", e)
            self.errors.append(
                {
                    "executor": "replay_driver",
                    "code": "MANAGE_POSITIONS_EXCEPTION",
                    "severity": "warning",
                    "summary": "manage_positions raised",
                    "detail": f"{type(e).__name__}: {str(e)[:200]}",
                }
            )

    def manage_short_positions(self) -> None:
        try:
            self.tg.manage_short_positions()
        except Exception as e:
            logger.debug("manage_short_positions raised: %s", e)
            self.errors.append(
                {
                    "executor": "replay_driver",
                    "code": "MANAGE_SHORT_POSITIONS_EXCEPTION",
                    "severity": "warning",
                    "summary": "manage_short_positions raised",
                    "detail": f"{type(e).__name__}: {str(e)[:200]}",
                }
            )

    # ---- Entry signals \u2014 delegate to real tg -------------------------
    def check_entry(self, ticker: str) -> tuple[bool, Any]:
        try:
            return self.tg.check_entry(ticker)
        except Exception as e:
            logger.debug("check_entry(%s) raised: %s", ticker, e)
            return (False, None)

    def check_short_entry(self, ticker: str) -> tuple[bool, Any]:
        try:
            return self.tg.check_short_entry(ticker)
        except Exception as e:
            logger.debug("check_short_entry(%s) raised: %s", ticker, e)
            return (False, None)

    # ---- Order execution \u2014 delegate to real tg + record -------------
    def execute_entry(self, ticker: str, price: float) -> None:
        # Record the harness-level entry first so the report has it
        # even if the real execute_entry raises mid-flight.
        self.entries.append(
            {
                "ts": self.clock.now.isoformat(),
                "ticker": ticker,
                "side": "long",
                "price": float(price),
            }
        )
        try:
            self.tg.execute_entry(ticker, price)
        except Exception as e:
            logger.debug("execute_entry(%s) raised: %s", ticker, e)

    def execute_short_entry(self, ticker: str, price: float) -> None:
        self.short_entries.append(
            {
                "ts": self.clock.now.isoformat(),
                "ticker": ticker,
                "side": "short",
                "price": float(price),
            }
        )
        try:
            self.tg.execute_short_entry(ticker, price)
        except Exception as e:
            logger.debug("execute_short_entry(%s) raised: %s", ticker, e)

    def execute_exit(self, ticker: str, side: str, price: float, reason: str) -> None:
        self.exits.append(
            {
                "ts": self.clock.now.isoformat(),
                "ticker": ticker,
                "side": str(side).lower(),
                "price": float(price),
                "reason": reason,
            }
        )
        try:
            if str(side).lower() == "long":
                self.tg.close_position(ticker, price, reason)
            else:
                self.tg.close_short_position(ticker, price, reason)
        except Exception as e:
            logger.debug("execute_exit(%s/%s) raised: %s", ticker, side, e)

    # ---- Operator surface --------------------------------------------
    def alert(self, msg: str) -> None:
        self.alerts.append(msg)
        self.telegram_layer.send(msg)

    def report_error(
        self, *, executor: str, code: str, severity: str, summary: str, detail: str
    ) -> None:
        self.errors.append(
            {
                "executor": executor,
                "code": code,
                "severity": severity,
                "summary": summary,
                "detail": detail,
            }
        )

    # ---- Broker passthroughs (Protocol methods) ----------------------
    def place_limit_order(
        self, *, ticker: str, side: str, qty: int, limit_price: float, reason: str
    ) -> str:
        return self.broker_layer.place_limit_order(
            ticker=ticker,
            side=side,
            qty=qty,
            limit_price=limit_price,
            reason=reason,
            ts=self.clock.now,
        )

    def place_stop_market_order(
        self, *, ticker: str, side: str, qty: int, stop_price: float, reason: str
    ) -> str:
        return self.broker_layer.place_stop_market_order(
            ticker=ticker,
            side=side,
            qty=qty,
            stop_price=stop_price,
            reason=reason,
            ts=self.clock.now,
        )

    def place_market_order(self, *, ticker: str, side: str, qty: int, reason: str) -> str:
        return self.broker_layer.place_market_order(
            ticker=ticker,
            side=side,
            qty=qty,
            reason=reason,
            ts=self.clock.now,
        )

    def cancel_order(self, *, order_id: str) -> bool:
        return self.broker_layer.cancel_order(order_id=order_id, ts=self.clock.now)

    def send_telegram(self, *, chat: str, message: str) -> None:
        self.telegram_layer.send(message, chat=chat)


# ---------------------------------------------------------------------------
# Step 8 \u2014 P&L pairing
# ---------------------------------------------------------------------------


def pair_entries_to_exits(entries: list[dict], exits: list[dict]) -> list[dict]:
    """Greedy FIFO pairing on (ticker, side) for crude realized P&L.

    Returns a list of dicts: ticker, side, entry_ts, exit_ts,
    entry_price, exit_price, pnl_dollars. Long pnl = exit - entry;
    short pnl = entry - exit. No share-count math (entries/exits do
    not always carry it cleanly through the harness yet).
    """
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in entries:
        by_key[(e["ticker"], e["side"])].append(dict(e))
    paired: list[dict] = []
    for x in exits:
        key = (x["ticker"], x["side"])
        if not by_key[key]:
            continue
        e = by_key[key].pop(0)
        sign = 1.0 if x["side"] == "long" else -1.0
        pnl = sign * (float(x["price"]) - float(e["price"]))
        paired.append(
            {
                "ticker": x["ticker"],
                "side": x["side"],
                "entry_ts": e["ts"],
                "exit_ts": x["ts"],
                "entry_price": e["price"],
                "exit_price": x["price"],
                "pnl_dollars": round(pnl, 4),
            }
        )
    return paired


def summarize(entries: list[dict], exits: list[dict], pairs: list[dict]) -> dict:
    wins = sum(1 for p in pairs if p["pnl_dollars"] > 0)
    losses = sum(1 for p in pairs if p["pnl_dollars"] < 0)
    total = round(sum(p["pnl_dollars"] for p in pairs), 4)
    return {
        "entries": len(entries),
        "exits": len(exits),
        "wins": wins,
        "losses": losses,
        "total_pnl": total,
    }


# ---------------------------------------------------------------------------
# Step 9 \u2014 driver
# ---------------------------------------------------------------------------

DEFAULT_TICKERS = ["AAPL", "AMZN", "AVGO", "GOOG", "META", "MSFT", "NFLX", "NVDA", "ORCL", "TSLA"]


@dataclass
class ReplayResult:
    date: str
    tickers: list[str]
    minutes_processed: int
    callbacks: RecordOnlyCallbacks
    bot_version: str


def run_replay(
    date_str: str,
    tickers: list[str] | None = None,
    bars_dir: Path | str = Path("/home/user/workspace/today_bars"),
    *,
    start_hhmm: tuple[int, int] = (9, 35),
    end_hhmm: tuple[int, int] = (15, 55),
) -> ReplayResult:
    """Drive `engine.scan.scan_loop` per-minute over an archived day."""
    bars_dir = Path(bars_dir)
    tickers = list(tickers or DEFAULT_TICKERS)
    universe = tickers + ["QQQ", "SPY"]

    # Load all bars up front, keyed by ticker.
    bars_by_ticker: dict[str, list[dict]] = {}
    for tk in universe:
        bars_by_ticker[tk] = load_day_bars(bars_dir, date_str, tk)

    # 1) Set the env up so the real tg imports without booting Telegram.
    setup_real_tg_environment()

    # 2) Import the real trade_genius. SSM_SMOKE_TEST=1 short-circuits
    #    the boot sequence so we land with the module fully constructed
    #    but no Telegram bot, no scheduler, no dashboard threads.
    import trade_genius as _tg  # noqa: E402

    # 3) Build the recording layers + clock.
    yyyy, mm, dd = (int(p) for p in date_str.split("-"))
    start_dt = datetime(yyyy, mm, dd, start_hhmm[0], start_hhmm[1], tzinfo=ET)
    end_dt = datetime(yyyy, mm, dd, end_hhmm[0], end_hhmm[1], tzinfo=ET)

    clock = BacktestClock(now=start_dt)
    broker_layer = RecordOnlyBrokerLayer()
    telegram_layer = RecordOnlyTelegram()
    position_store = RecordOnlyPositionStore()

    # 4) Install monkey-patches into the real trade_genius.
    install_record_only_layers(_tg, clock, broker_layer, telegram_layer, position_store)

    # 4b) Wire the harness bar store into tg's record-only fetch_1min_bars.
    _tg._harness_bars_owner["bars"] = bars_by_ticker

    # 5) Build the callbacks.
    cb = RecordOnlyCallbacks(
        tg=_tg,
        clock=clock,
        bars_by_ticker=bars_by_ticker,
        broker_layer=broker_layer,
        telegram_layer=telegram_layer,
        position_store=position_store,
    )

    # 6) Step minute-by-minute through the session.
    import engine.scan as _engine_scan

    cur = start_dt
    minutes = 0
    while cur <= end_dt:
        clock.now = cur
        cb.ticks.append(cur)
        # Clear the per-cycle bar cache so each tick re-reads from the
        # harness bar store at the new clock time.
        _cache = getattr(_tg, "_cycle_bar_cache", None)
        if _cache is not None and hasattr(_cache, "clear"):
            _cache.clear()
        try:
            _engine_scan.scan_loop(cb)
        except Exception as e:
            logger.warning("scan_loop crashed at %s: %s", cur.isoformat(), e)
            cb.errors.append(
                {
                    "executor": "replay_driver",
                    "code": "SCAN_LOOP_EXCEPTION",
                    "severity": "error",
                    "summary": f"scan_loop crashed at {cur.isoformat()}",
                    "detail": f"{type(e).__name__}: {str(e)[:200]}",
                }
            )
        minutes += 1
        cur = cur + timedelta(minutes=1)

    return ReplayResult(
        date=date_str,
        tickers=tickers,
        minutes_processed=minutes,
        callbacks=cb,
        bot_version=getattr(_tg, "BOT_VERSION", "unknown"),
    )


# ---------------------------------------------------------------------------
# Step 10 \u2014 JSON report
# ---------------------------------------------------------------------------


def _broker_closes_to_exits(closes: list[dict]) -> list[dict]:
    """Translate `broker_layer.closes` records (captured via the
    tg.close_position / tg.close_short_position wrappers) into the
    harness `exits` shape so they pair cleanly against entries.
    """
    out = []
    for c in closes:
        out.append(
            {
                "ts": c.get("ts"),
                "ticker": c.get("ticker"),
                "side": c.get("side"),
                "price": c.get("exit_price"),
                "reason": c.get("reason"),
                "entry_price": c.get("entry_price"),
                "shares": c.get("shares"),
            }
        )
    return out


def build_json_report(result: ReplayResult) -> dict:
    cb = result.callbacks
    # Production exits flow through tg.close_position /
    # tg.close_short_position; the harness wrappers capture them in
    # broker_layer.closes. Merge with any harness-direct exits and use
    # the unified list for pairing.
    merged_exits = list(cb.exits) + _broker_closes_to_exits(cb.broker_layer.closes)
    pairs = pair_entries_to_exits(cb.entries + cb.short_entries, merged_exits)
    summary = summarize(cb.entries + cb.short_entries, merged_exits, pairs)
    return {
        "date": result.date,
        "version": result.bot_version,
        "minutes_processed": result.minutes_processed,
        "tickers": result.tickers,
        "entries": cb.entries + cb.short_entries,
        "exits": merged_exits,
        "orders": cb.broker_layer.orders,
        "cancellations": cb.broker_layer.cancellations,
        "closes_raw": cb.broker_layer.closes,
        "telegram_messages": cb.telegram_layer.messages,
        "alerts": cb.alerts,
        "errors": cb.errors,
        "pnl_pairs": pairs,
        "summary": summary,
    }


def format_text_report(result: ReplayResult) -> str:
    cb = result.callbacks
    pairs = pair_entries_to_exits(cb.entries + cb.short_entries, cb.exits)
    summary = summarize(cb.entries + cb.short_entries, cb.exits, pairs)
    lines = [
        f"# v{result.bot_version} replay (real trade_genius) \u2014 {result.date}",
        "",
        f"- universe: {', '.join(result.tickers)}",
        f"- minutes processed: {result.minutes_processed}",
        f"- fetch_1min_bars calls: {len(cb.fetch_calls)}",
        f"- alerts: {len(cb.alerts)}  errors: {len(cb.errors)}",
        f"- entries: {len(cb.entries)} long, {len(cb.short_entries)} short",
        f"- exits: {len(cb.exits)}",
        f"- orders recorded: {len(cb.broker_layer.orders)}  cancels: {len(cb.broker_layer.cancellations)}",
        f"- paired round-trips: {len(pairs)}  total P&L: ${summary['total_pnl']:+.2f}",
        f"- wins: {summary['wins']}  losses: {summary['losses']}",
        "",
    ]
    if cb.entries or cb.short_entries:
        lines.append("## Entries")
        for e in cb.entries + cb.short_entries:
            lines.append(f"- {e['ts']} {e['ticker']} {e['side']} @ {e['price']}")
        lines.append("")
    if cb.exits:
        lines.append("## Exits")
        for x in cb.exits:
            lines.append(f"- {x['ts']} {x['ticker']} {x['side']} @ {x['price']} ({x['reason']})")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backtest.replay_v511_full",
        description=(
            "v5.26.0 stage7 replay harness. Drives the REAL trade_genius "
            "module (v5.26.0 spec-strict) per-minute over an archived day "
            "with a record-only callbacks layer."
        ),
    )
    p.add_argument("--date", required=True, help="YYYY-MM-DD session date")
    p.add_argument(
        "--bars-dir",
        default="/home/user/workspace/today_bars",
        help="Parent dir of <date>/{TICKER}.jsonl files",
    )
    p.add_argument(
        "--tickers",
        default=",".join(DEFAULT_TICKERS),
        help="Comma-separated trade universe override",
    )
    p.add_argument("--start", default="09:35", help="ET start time HH:MM")
    p.add_argument("--end", default="15:55", help="ET end time HH:MM")
    p.add_argument(
        "--output", default=None, help="Write JSON report to this path (else stdout text)"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    args = _build_parser().parse_args(argv)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    sh, sm = (int(x) for x in args.start.split(":"))
    eh, em = (int(x) for x in args.end.split(":"))
    result = run_replay(
        args.date,
        tickers=tickers,
        bars_dir=args.bars_dir,
        start_hhmm=(sh, sm),
        end_hhmm=(eh, em),
    )
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(build_json_report(result), indent=2), encoding="utf-8")
        print(f"wrote {out_path}")
    else:
        print(format_text_report(result))
    return 0


if __name__ == "__main__":
    _sys.exit(main())
