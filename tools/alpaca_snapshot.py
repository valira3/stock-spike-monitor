"""v9.1.17 -- Alpaca account snapshot uploader.

Pulls the live Alpaca paper account state for every configured
portfolio (`main`, `val`, `gene`) and writes the result to disk so a
companion GHA workflow (`alpaca-snapshot.yml`) can commit it to the
`alpaca-live` branch.

The pattern mirrors `tools.state_snapshot`: a read-only script whose
output a workflow forwards to a dedicated branch. The downstream
purpose is operator-side AI agents being able to retrieve the live
broker view (equity, cash, BP, open positions, today's orders) via
the GitHub MCP `get_file_contents` without needing live Alpaca
credentials in their sandbox.

Per-portfolio resolution mirrors `engine.portfolio_equity`:
  <PID>_ALPACA_PAPER_KEY    e.g. VAL_ALPACA_PAPER_KEY
  <PID>_ALPACA_PAPER_SECRET e.g. VAL_ALPACA_PAPER_SECRET

For `main`, fall back to a `MAIN_ALPACA_PAPER_KEY/_SECRET` if set,
otherwise reuse `VAL_*` / `GENE_*` data-only credentials (the live
bot's main book is paper-only, so a read-only data fetch with any
valid paper key is safe).

Each portfolio's snapshot includes:
  account     {equity, cash, last_equity, buying_power, status,
               account_blocked, pattern_day_trader}
  positions   list of {symbol, qty, avg_entry_price, current_price,
               market_value, unrealized_pl, unrealized_plpc,
               cost_basis, change_today}
  orders      today's orders (state in {filled, canceled,
               partially_filled, accepted}) -- gives the operator a
               broker-side view of what fired, what slipped, what
               was rejected

Two artifacts per run:
  data/alpaca/latest.json      single source of truth for "what
                               does Alpaca see right now?"
  data/alpaca/YYYY-MM-DD.jsonl append-only history line per tick

Required env (at least one portfolio's credentials):
  VAL_ALPACA_PAPER_KEY / VAL_ALPACA_PAPER_SECRET
  GENE_ALPACA_PAPER_KEY / GENE_ALPACA_PAPER_SECRET
  [optional] MAIN_ALPACA_PAPER_KEY / MAIN_ALPACA_PAPER_SECRET

Optional env:
  ALPACA_SNAPSHOT_DIR   output directory (default ./data/alpaca)
  ALPACA_SNAPSHOT_QUIET 1 = suppress per-portfolio progress logs

Exit codes:
  0  at least one portfolio's snapshot succeeded
  1  no credentials available for any portfolio
  2  alpaca-py SDK missing
  3  serialization error
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


PORTFOLIOS: tuple[str, ...] = ("main", "val", "gene")


def _log(msg: str) -> None:
    if os.environ.get("ALPACA_SNAPSHOT_QUIET") == "1":
        return
    print(msg, flush=True)


def _creds_for(pid: str) -> Optional[tuple[str, str]]:
    """Return (key, secret) for `pid` or None if creds missing.

    main falls back to VAL_ / GENE_ if MAIN_ is unset (read-only data
    fetch with any valid paper key is harmless).
    """
    pid_up = pid.upper()
    key = (os.getenv(f"{pid_up}_ALPACA_PAPER_KEY") or "").strip()
    secret = (os.getenv(f"{pid_up}_ALPACA_PAPER_SECRET") or "").strip()
    if key and secret:
        return (key, secret)
    if pid_up == "MAIN":
        for fallback in ("VAL", "GENE"):
            key = (os.getenv(f"{fallback}_ALPACA_PAPER_KEY") or "").strip()
            secret = (os.getenv(f"{fallback}_ALPACA_PAPER_SECRET") or "").strip()
            if key and secret:
                return (key, secret)
    return None


def _account_dict(acct) -> dict[str, Any]:
    return {
        "account_number": str(getattr(acct, "account_number", "") or ""),
        "status": str(getattr(acct, "status", "") or ""),
        "account_blocked": bool(getattr(acct, "account_blocked", False)),
        "pattern_day_trader": bool(getattr(acct, "pattern_day_trader", False)),
        "equity": float(getattr(acct, "equity", 0) or 0),
        "last_equity": float(getattr(acct, "last_equity", 0) or 0),
        "cash": float(getattr(acct, "cash", 0) or 0),
        "buying_power": float(getattr(acct, "buying_power", 0) or 0),
        "long_market_value": float(getattr(acct, "long_market_value", 0) or 0),
        "short_market_value": float(getattr(acct, "short_market_value", 0) or 0),
        "daytrade_count": int(getattr(acct, "daytrade_count", 0) or 0),
    }


def _position_dict(pos) -> dict[str, Any]:
    return {
        "symbol": str(getattr(pos, "symbol", "") or ""),
        "side": str(getattr(pos, "side", "") or ""),
        "qty": float(getattr(pos, "qty", 0) or 0),
        "avg_entry_price": float(getattr(pos, "avg_entry_price", 0) or 0),
        "current_price": float(getattr(pos, "current_price", 0) or 0),
        "market_value": float(getattr(pos, "market_value", 0) or 0),
        "cost_basis": float(getattr(pos, "cost_basis", 0) or 0),
        "unrealized_pl": float(getattr(pos, "unrealized_pl", 0) or 0),
        "unrealized_plpc": float(getattr(pos, "unrealized_plpc", 0) or 0),
        "change_today": float(getattr(pos, "change_today", 0) or 0),
    }


def _order_dict(o) -> dict[str, Any]:
    return {
        "id": str(getattr(o, "id", "") or ""),
        "client_order_id": str(getattr(o, "client_order_id", "") or ""),
        "symbol": str(getattr(o, "symbol", "") or ""),
        "side": str(getattr(o, "side", "") or ""),
        "qty": str(getattr(o, "qty", "") or ""),
        "filled_qty": str(getattr(o, "filled_qty", "") or ""),
        "filled_avg_price": str(getattr(o, "filled_avg_price", "") or ""),
        "type": str(getattr(o, "type", "") or ""),
        "time_in_force": str(getattr(o, "time_in_force", "") or ""),
        "status": str(getattr(o, "status", "") or ""),
        "submitted_at": str(getattr(o, "submitted_at", "") or ""),
        "filled_at": str(getattr(o, "filled_at", "") or ""),
        "canceled_at": str(getattr(o, "canceled_at", "") or ""),
    }


def _pull_portfolio(pid: str) -> dict[str, Any]:
    """Return a portfolio snapshot or an error dict."""
    creds = _creds_for(pid)
    if creds is None:
        return {"__error__": f"no credentials for {pid}"}
    key, secret = creds
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
    except ImportError as e:
        return {"__error__": f"alpaca-py not installed: {e}"}
    try:
        tc = TradingClient(key, secret, paper=True)
        acct = tc.get_account()
        positions = tc.get_all_positions()
        # Today's orders -- both open and closed.
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        req = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            after=today_utc,
            limit=500,
        )
        orders = tc.get_orders(filter=req)
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {str(e)[:200]}"}
    return {
        "account": _account_dict(acct),
        "positions": [_position_dict(p) for p in (positions or [])],
        "orders_today": [_order_dict(o) for o in (orders or [])],
    }


def _write_outputs(snapshot: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest.json"
    latest.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8",
    )
    ts = snapshot["captured_at_utc"]
    day = ts.split("T", 1)[0]
    daily = out_dir / f"{day}.jsonl"
    line = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    with daily.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return latest, daily


def main() -> int:
    out_dir = Path(os.environ.get(
        "ALPACA_SNAPSHOT_DIR", "data/alpaca"
    )).resolve()

    _log(f"[alpaca-snapshot] out={out_dir}")

    bundle: dict[str, Any] = {}
    any_ok = False
    for pid in PORTFOLIOS:
        snap = _pull_portfolio(pid)
        bundle[pid] = snap
        if "__error__" in snap:
            _log(f"  {pid} FAILED: {snap['__error__']}")
        else:
            n_pos = len(snap.get("positions") or [])
            n_ord = len(snap.get("orders_today") or [])
            eq = (snap.get("account") or {}).get("equity")
            _log(f"  {pid} OK  equity=${eq}  pos={n_pos}  orders={n_ord}")
            any_ok = True

    if not any_ok:
        print("::error::no portfolio credentials available", flush=True)
        return 1

    snapshot = {
        "schema_version": 1,
        "captured_at_utc": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "portfolios": bundle,
    }

    try:
        latest, daily = _write_outputs(snapshot, out_dir)
        _log(f"  wrote {latest}")
        _log(f"  appended {daily}")
    except Exception as e:
        print(f"::error::write failed: {e}", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
