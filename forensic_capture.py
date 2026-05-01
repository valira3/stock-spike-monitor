"""v5.26.2 \u2014 forensic data capture for backtest reproducibility.

This module is the single source of truth for writing JSONL forensic
records during live trading. The goal is that any future replay can
reconstruct every entry/skip decision deterministically without the
production indicator / sentinel code present at the same revision.

Three record streams are written under ``/data/forensics/<YYYY-MM-DD>/``:

1. ``decisions/<TICKER>.jsonl`` \u2014 one record per ``check_breakout`` /
   ``check_short_breakout`` call that reaches the gate stack. Captures
   the final decision (entered/skipped + reason) plus every input the
   gate stack consumed.

2. ``boundary/<TICKER>.jsonl`` \u2014 one record per
   ``evaluate_boundary_hold_gate`` invocation. Captures side, boundary
   value, last 2 closes, consecutive_outside count, hold result, and
   the strike number context.

3. ``indicators/<TICKER>.jsonl`` \u2014 per-minute snapshot of every
   indicator + state variable feeding the gate stack: DI+/-, ADX 1m/5m,
   RSI(15), session HOD/LOD, ORH/ORL, strike count, sentinel state.
   Written from the scan loop on each minute close.

All writes are append-only JSONL, atomic per line on Linux ext4 (lines
< PIPE_BUF). Failure-tolerant \u2014 a forensic write can NEVER raise into
the trading loop. Failures log at warning level and are dropped.

The bid/ask + last_trade_price fields are populated on the bar archive
side via ``bar_archive.write_bar``; this module does not duplicate
that path.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("trade_genius.forensic_capture")

DEFAULT_BASE_DIR = "/data/forensics"


def _today_str(today: date | None = None) -> str:
    if today is None:
        today = datetime.utcnow().date()
    return today.strftime("%Y-%m-%d")


def _safe_float(x):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    # JSON does not support inf / nan \u2014 normalise to None.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _safe_bool(x):
    if x is None:
        return None
    return bool(x)


def _write_line(
    *,
    base_dir: str | os.PathLike,
    today: date | None,
    stream: str,
    ticker: str,
    record: dict,
) -> str | None:
    """Append a single JSONL record to
    ``{base_dir}/{YYYY-MM-DD}/{stream}/{TICKER}.jsonl``.

    Returns the absolute path written, or None on failure (logged at
    warning, never raised).
    """
    if not ticker:
        return None
    try:
        sym = str(ticker).strip().upper()
        if not sym:
            return None
        day = _today_str(today)
        dir_path = Path(base_dir) / day / stream
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{sym}.jsonl"
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return str(file_path)
    except Exception as e:
        logger.warning(
            "[V526-FORENSIC] %s %s write failed: %s",
            stream,
            ticker,
            e,
        )
        return None


def _write_day_line(
    *,
    base_dir: str | os.PathLike,
    today: date | None,
    stream: str,
    record: dict,
) -> str | None:
    """v5.31.0 \u2014 append JSONL to ``{base_dir}/{YYYY-MM-DD}/{stream}.jsonl``.

    Sister of ``_write_line`` for day-scoped streams (no per-ticker segment),
    e.g. macro snapshots. Failure-tolerant; never raises into the caller.
    """
    try:
        day = _today_str(today)
        dir_path = Path(base_dir) / day
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{stream}.jsonl"
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return str(file_path)
    except Exception as e:
        logger.warning(
            "[V531-FORENSIC] %s day-write failed: %s",
            stream,
            e,
        )
        return None


def _write_flat_line(
    *,
    base_dir: str | os.PathLike,
    ticker: str,
    record: dict,
) -> str | None:
    """v5.31.0 \u2014 append JSONL to ``{base_dir}/{TICKER}.jsonl`` (no date segment).

    Used for accumulating-across-days streams such as the daily OHLC archive
    at ``/data/bars/daily/{TICKER}.jsonl``. Failure-tolerant; never raises.
    """
    if not ticker:
        return None
    try:
        sym = str(ticker).strip().upper()
        if not sym:
            return None
        dir_path = Path(base_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{sym}.jsonl"
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return str(file_path)
    except Exception as e:
        logger.warning(
            "[V531-FORENSIC] flat %s write failed: %s",
            ticker,
            e,
        )
        return None


# ---------------------------------------------------------------------
# Stream 1 \u2014 decision records
# ---------------------------------------------------------------------


def write_decision_record(
    *,
    ticker: str,
    side: str,
    ts_utc: str,
    strike_num: int | None,
    decision: str,  # "ENTER" or "SKIP:<reason>"
    current_price: float | None,
    last_close: float | None,
    or_high: float | None,
    or_low: float | None,
    pdc: float | None,
    qqq_last: float | None,
    qqq_avwap: float | None,
    qqq_5m_close: float | None,
    qqq_ema9: float | None,
    sess_hod: float | None,
    sess_lod: float | None,
    prev_sess_hod: float | None,
    prev_sess_lod: float | None,
    di_1m: float | None,
    di_5m: float | None,
    adx_1m: float | None,
    adx_5m: float | None,
    rsi_15: float | None,
    boundary_hold_or: bool | None,
    boundary_hold_nhod_nlod: bool | None,
    is_extreme_print: bool | None,
    permit_open: bool | None,
    alarm_e_blocked: bool | None,
    sentinel_state: dict | None,
    entry_bid: float | None = None,
    entry_ask: float | None = None,
    spread_bps: float | None = None,
    decision_latency_ms: float | None = None,
    base_dir: str | os.PathLike = DEFAULT_BASE_DIR,
    today: date | None = None,
) -> str | None:
    record = {
        "ts_utc": ts_utc,
        "ticker": (ticker or "").strip().upper(),
        "side": (side or "").strip().upper(),
        "strike_num": int(strike_num) if strike_num is not None else None,
        "decision": str(decision),
        "current_price": _safe_float(current_price),
        "last_close": _safe_float(last_close),
        "or_high": _safe_float(or_high),
        "or_low": _safe_float(or_low),
        "pdc": _safe_float(pdc),
        "qqq_last": _safe_float(qqq_last),
        "qqq_avwap": _safe_float(qqq_avwap),
        "qqq_5m_close": _safe_float(qqq_5m_close),
        "qqq_ema9": _safe_float(qqq_ema9),
        "sess_hod": _safe_float(sess_hod),
        "sess_lod": _safe_float(sess_lod),
        "prev_sess_hod": _safe_float(prev_sess_hod),
        "prev_sess_lod": _safe_float(prev_sess_lod),
        "di_1m": _safe_float(di_1m),
        "di_5m": _safe_float(di_5m),
        "adx_1m": _safe_float(adx_1m),
        "adx_5m": _safe_float(adx_5m),
        "rsi_15": _safe_float(rsi_15),
        "boundary_hold_or": _safe_bool(boundary_hold_or),
        "boundary_hold_nhod_nlod": _safe_bool(boundary_hold_nhod_nlod),
        "is_extreme_print": _safe_bool(is_extreme_print),
        "permit_open": _safe_bool(permit_open),
        "alarm_e_blocked": _safe_bool(alarm_e_blocked),
        "sentinel_state": sentinel_state if isinstance(sentinel_state, dict) else None,
        # v5.31.0 \u2014 quote snapshot at decision time + decision-stack latency
        "entry_bid": _safe_float(entry_bid),
        "entry_ask": _safe_float(entry_ask),
        "spread_bps": _safe_float(spread_bps),
        "decision_latency_ms": _safe_float(decision_latency_ms),
    }
    return _write_line(
        base_dir=base_dir,
        today=today,
        stream="decisions",
        ticker=ticker,
        record=record,
    )


# ---------------------------------------------------------------------
# Stream 2 \u2014 boundary gate state
# ---------------------------------------------------------------------


def write_boundary_record(
    *,
    ticker: str,
    side: str,
    ts_utc: str,
    boundary_label: str,  # "ORH_ORL" or "NHOD_NLOD"
    boundary_high: float | None,
    boundary_low: float | None,
    last_close: float | None,
    prior_close: float | None,
    consecutive_outside: int | None,
    hold: bool | None,
    reason: str | None,
    strike_num: int | None,
    base_dir: str | os.PathLike = DEFAULT_BASE_DIR,
    today: date | None = None,
) -> str | None:
    record = {
        "ts_utc": ts_utc,
        "ticker": (ticker or "").strip().upper(),
        "side": (side or "").strip().upper(),
        "boundary_label": str(boundary_label),
        "boundary_high": _safe_float(boundary_high),
        "boundary_low": _safe_float(boundary_low),
        "last_close": _safe_float(last_close),
        "prior_close": _safe_float(prior_close),
        "consecutive_outside": (
            int(consecutive_outside) if consecutive_outside is not None else None
        ),
        "hold": _safe_bool(hold),
        "reason": (None if reason is None else str(reason)),
        "strike_num": int(strike_num) if strike_num is not None else None,
    }
    return _write_line(
        base_dir=base_dir,
        today=today,
        stream="boundary",
        ticker=ticker,
        record=record,
    )


# ---------------------------------------------------------------------
# Stream 3 \u2014 per-minute indicator snapshot
# ---------------------------------------------------------------------


def write_indicator_snapshot(
    *,
    ticker: str,
    ts_utc: str,
    bar_close: float | None,
    bar_open: float | None,
    bar_high: float | None,
    bar_low: float | None,
    bar_volume: float | None,
    bid: float | None,
    ask: float | None,
    last_trade_price: float | None,
    or_high: float | None,
    or_low: float | None,
    pdc: float | None,
    sess_hod: float | None,
    sess_lod: float | None,
    di_plus_1m: float | None,
    di_minus_1m: float | None,
    di_plus_5m: float | None,
    di_minus_5m: float | None,
    adx_1m: float | None,
    adx_5m: float | None,
    rsi_15: float | None,
    strike_count: int | None,
    sentinel_state: dict | None,
    permit_state: dict | None = None,
    base_dir: str | os.PathLike = DEFAULT_BASE_DIR,
    today: date | None = None,
) -> str | None:
    record = {
        "ts_utc": ts_utc,
        "ticker": (ticker or "").strip().upper(),
        "bar_close": _safe_float(bar_close),
        "bar_open": _safe_float(bar_open),
        "bar_high": _safe_float(bar_high),
        "bar_low": _safe_float(bar_low),
        "bar_volume": _safe_float(bar_volume),
        "bid": _safe_float(bid),
        "ask": _safe_float(ask),
        "last_trade_price": _safe_float(last_trade_price),
        "or_high": _safe_float(or_high),
        "or_low": _safe_float(or_low),
        "pdc": _safe_float(pdc),
        "sess_hod": _safe_float(sess_hod),
        "sess_lod": _safe_float(sess_lod),
        "di_plus_1m": _safe_float(di_plus_1m),
        "di_minus_1m": _safe_float(di_minus_1m),
        "di_plus_5m": _safe_float(di_plus_5m),
        "di_minus_5m": _safe_float(di_minus_5m),
        "adx_1m": _safe_float(adx_1m),
        "adx_5m": _safe_float(adx_5m),
        "rsi_15": _safe_float(rsi_15),
        "strike_count": (int(strike_count) if strike_count is not None else None),
        "sentinel_state": sentinel_state if isinstance(sentinel_state, dict) else None,
        # v5.31.0 \u2014 boundary-hold + vol-gate snapshot for permit reproducibility
        "permit_state": permit_state if isinstance(permit_state, dict) else None,
    }
    return _write_line(
        base_dir=base_dir,
        today=today,
        stream="indicators",
        ticker=ticker,
        record=record,
    )


# ---------------------------------------------------------------------
# Stream 4 \u2014 exit records (v5.31.0)
# ---------------------------------------------------------------------


def write_exit_record(
    *,
    ticker: str,
    side: str,
    ts_utc: str,
    exit_price: float | None,
    entry_price: float | None = None,
    entry_ts_utc: str | None = None,
    shares: int | None = None,
    fill_slippage_bps: float | None = None,
    alarm_triggered: str | None = None,
    exit_reason_code: str | None = None,
    peak_close_at_exit: float | None = None,
    trail_stage_at_exit: int | None = None,
    bars_in_trade: int | None = None,
    mae_bps: float | None = None,
    mfe_bps: float | None = None,
    pnl_dollars: float | None = None,
    pnl_pct: float | None = None,
    base_dir: str | os.PathLike = DEFAULT_BASE_DIR,
    today: date | None = None,
) -> str | None:
    """v5.31.0 \u2014 write an exit record to ``exits/{TICKER}.jsonl``.

    Single funnel for ALL exits (sentinel A/A2/B/F, EOD, manual). Captures
    the alarm code, peak excursion, slippage, and MAE/MFE so a backtest can
    reconstruct the full trade lifecycle. Failure-tolerant; the caller MUST
    wrap this in try/except to ensure no forensic write ever blocks an exit.
    """
    record = {
        "ts_utc": ts_utc,
        "ticker": (ticker or "").strip().upper(),
        "side": (side or "").strip().upper(),
        "exit_price": _safe_float(exit_price),
        "entry_price": _safe_float(entry_price),
        "entry_ts_utc": (None if entry_ts_utc is None else str(entry_ts_utc)),
        "shares": (int(shares) if shares is not None else None),
        "fill_slippage_bps": _safe_float(fill_slippage_bps),
        "alarm_triggered": (None if alarm_triggered is None else str(alarm_triggered)),
        "exit_reason_code": (None if exit_reason_code is None else str(exit_reason_code)),
        "peak_close_at_exit": _safe_float(peak_close_at_exit),
        "trail_stage_at_exit": (
            int(trail_stage_at_exit) if trail_stage_at_exit is not None else None
        ),
        "bars_in_trade": (int(bars_in_trade) if bars_in_trade is not None else None),
        "mae_bps": _safe_float(mae_bps),
        "mfe_bps": _safe_float(mfe_bps),
        "pnl_dollars": _safe_float(pnl_dollars),
        "pnl_pct": _safe_float(pnl_pct),
    }
    return _write_line(
        base_dir=base_dir,
        today=today,
        stream="exits",
        ticker=ticker,
        record=record,
    )


# ---------------------------------------------------------------------
# Stream 5 \u2014 macro snapshot (v5.31.0)
# ---------------------------------------------------------------------


def write_macro_snapshot(
    *,
    ts_utc: str,
    qqq_last: float | None = None,
    spy_last: float | None = None,
    vix_or_uvxy: float | None = None,
    qqq_5m_close: float | None = None,
    qqq_avwap: float | None = None,
    qqq_ema9: float | None = None,
    regime_mode: str | None = None,
    breadth: str | None = None,
    rsi_regime: str | None = None,
    base_dir: str | os.PathLike = DEFAULT_BASE_DIR,
    today: date | None = None,
) -> str | None:
    """v5.31.0 \u2014 per-minute macro snapshot to ``{date}/macro.jsonl``.

    Day-scoped JSONL (no per-ticker segment). Captures index quotes plus
    the regime/breadth/RSI labels so a backtest can replay decisions with
    the exact macro context the live engine saw.
    """
    record = {
        "ts_utc": ts_utc,
        "qqq_last": _safe_float(qqq_last),
        "spy_last": _safe_float(spy_last),
        "vix_or_uvxy": _safe_float(vix_or_uvxy),
        "qqq_5m_close": _safe_float(qqq_5m_close),
        "qqq_avwap": _safe_float(qqq_avwap),
        "qqq_ema9": _safe_float(qqq_ema9),
        "regime_mode": (None if regime_mode is None else str(regime_mode)),
        "breadth": (None if breadth is None else str(breadth)),
        "rsi_regime": (None if rsi_regime is None else str(rsi_regime)),
    }
    return _write_day_line(
        base_dir=base_dir,
        today=today,
        stream="macro",
        record=record,
    )


# ---------------------------------------------------------------------
# Stream 6 \u2014 daily OHLC archive (v5.31.0)
# ---------------------------------------------------------------------


DEFAULT_DAILY_BAR_DIR = "/data/bars/daily"


def write_daily_bar(
    *,
    ticker: str,
    date_str: str,
    open_: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    volume: float | None = None,
    or_high: float | None = None,
    or_low: float | None = None,
    pdc: float | None = None,
    sess_hod: float | None = None,
    sess_lod: float | None = None,
    base_dir: str | os.PathLike = DEFAULT_DAILY_BAR_DIR,
) -> str | None:
    """v5.31.0 \u2014 daily OHLC + session context to ``{base_dir}/{TICKER}.jsonl``.

    Called once per ticker at end-of-day from ``broker/lifecycle.py:eod_close``.
    Cross-day flat archive (no per-date directory) so a backtest can stream
    a ticker's full daily history with a single open-and-tail.
    """
    record = {
        "date": str(date_str),
        "ticker": (ticker or "").strip().upper(),
        "open": _safe_float(open_),
        "high": _safe_float(high),
        "low": _safe_float(low),
        "close": _safe_float(close),
        "volume": _safe_float(volume),
        "or_high": _safe_float(or_high),
        "or_low": _safe_float(or_low),
        "pdc": _safe_float(pdc),
        "sess_hod": _safe_float(sess_hod),
        "sess_lod": _safe_float(sess_lod),
    }
    return _write_flat_line(
        base_dir=base_dir,
        ticker=ticker,
        record=record,
    )
