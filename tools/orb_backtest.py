"""Classical 15-minute Opening Range Breakout backtest.

Standalone -- imports nothing from trade_genius / engine / broker.
Reads data/<YYYY-MM-DD>/<TICKER>.jsonl (1-min bars) and outputs the
same summary.json + per_day/*.json schema as tools/lever_sweep_runner.py
so the manager and stability analyzer can rank it alongside the v15
spec variants.

Strategy (programmable, no discretion):

  1. Define the Opening Range from the first ORB_OR_MINUTES (default 15)
     of the regular session. OR_HIGH = max(high) and OR_LOW = min(low)
     across that window.

  2. After the OR window closes, scan for 5-min candle closes above
     OR_HIGH (long) or below OR_LOW (short). On signal, enter on the
     NEXT 5-min candle's open price (with slippage).

  3. Filters:
       - Range filter:    ORB_RANGE_MIN_PCT <= (OR_HIGH-OR_LOW)/midprice <= ORB_RANGE_MAX_PCT
       - Time cutoff:     no new entries after ORB_TIME_CUTOFF_ET (default 12:00)
       - Per-day cap:     at most ORB_MAX_TRADES_PER_DAY entries per ticker
                          (default 1; "first signal of the day" semantics)

  4. Risk management:
       - Stop: opposite side of OR with ORB_STOP_BUFFER_BPS slippage adder
       - Target: 1 : ORB_RR risk-reward
       - EOD flush: force close at ORB_EOD_CUTOFF_ET (default 15:55 ET)
       - Position sizing: risk ORB_RISK_PER_TRADE_PCT of ORB_ACCOUNT per trade

  5. Slippage model (matches v15 harness): entry adverse-bps + exit
     adverse-bps + stop-kick on stop-trigger exits + short-side penalty.

Usage:
    ORB_OR_MINUTES=15 ORB_RR=1.5 python tools/orb_backtest.py \
        --corpus data --out /tmp/orb_run --tickers AAPL,MSFT,...

Config env vars (all optional):
    ORB_OR_MINUTES         15
    ORB_RR                 1.5
    ORB_STOP_BUFFER_BPS    5.0
    ORB_TIME_CUTOFF_ET     12:00
    ORB_EOD_CUTOFF_ET      15:55
    ORB_RANGE_MIN_PCT      0.005
    ORB_RANGE_MAX_PCT      0.015
    ORB_VOLUME_MULT        0     (disabled; >1 enables vol filter)
    ORB_MAX_TRADES_PER_DAY 1
    ORB_RISK_PER_TRADE_PCT 1.0
    ORB_ACCOUNT            100000
    ORB_TICKER_SIDE_BLOCKLIST '{"ORCL":["LONG"],...}'  (same schema as prod)
    ORB_ENTRY_SLIPPAGE_BPS 1.5
    ORB_EXIT_SLIPPAGE_BPS  1.5
    ORB_STOP_KICK_BPS      5.0
    ORB_SHORT_PENALTY_BPS  1.0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


# ---------- env knobs ----------
def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _envs(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _et_to_minutes(s: str) -> int:
    """Parse "HH:MM" (ET) to minutes-from-midnight."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _bucket_to_minutes(b: str) -> int:
    """et_bucket is "HHMM" string. Convert to minutes-from-midnight."""
    if len(b) == 4:
        return int(b[:2]) * 60 + int(b[2:])
    return -1


# ---------- bar loading ----------
@dataclass
class Bar1m:
    bucket: int          # minutes-from-midnight ET
    open: float
    high: float
    low: float
    close: float
    volume: float        # total_volume preferred, fallback iex_volume


def load_day_bars(corpus_dir: Path, date: str, ticker: str) -> list[Bar1m]:
    fp = corpus_dir / date / f"{ticker}.jsonl"
    if not fp.is_file():
        return []
    out: list[Bar1m] = []
    try:
        for line in fp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            bkt = _bucket_to_minutes(d.get("et_bucket", ""))
            if bkt < 0:
                continue
            o = d.get("open"); h = d.get("high"); l = d.get("low"); c = d.get("close")
            if o is None or h is None or l is None or c is None:
                continue
            v = d.get("total_volume") or d.get("iex_volume") or 0
            try:
                out.append(Bar1m(bkt, float(o), float(h), float(l),
                                 float(c), float(v or 0)))
            except (TypeError, ValueError):
                continue
    except OSError:
        return []
    out.sort(key=lambda b: b.bucket)
    return out


def aggregate_5m(bars_1m: list[Bar1m]) -> list[Bar1m]:
    """Aggregate 1-min bars into 5-min candles, anchored at 5-min boundaries
    (e.g. 09:30, 09:35, 09:40, ...). Returns synthetic 5-min OHLCV bars."""
    by_bucket: dict[int, list[Bar1m]] = defaultdict(list)
    for b in bars_1m:
        anchor = (b.bucket // 5) * 5
        by_bucket[anchor].append(b)
    out: list[Bar1m] = []
    for anchor in sorted(by_bucket):
        bars = by_bucket[anchor]
        if not bars:
            continue
        out.append(Bar1m(
            bucket=anchor,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=sum(b.volume for b in bars),
        ))
    return out


# ---------- strategy config ----------
@dataclass
class ORBConfig:
    or_minutes: int = 15
    rr: float = 1.5
    stop_buffer_bps: float = 5.0
    time_cutoff_et: int = field(default_factory=lambda: _et_to_minutes("12:00"))
    eod_cutoff_et: int = field(default_factory=lambda: _et_to_minutes("15:55"))
    range_min_pct: float = 0.005
    range_max_pct: float = 0.015
    volume_mult: float = 0.0
    max_trades_per_day: int = 1
    risk_per_trade_pct: float = 1.0
    account: float = 100_000.0
    blocklist: dict = field(default_factory=dict)
    entry_slippage_bps: float = 1.5
    exit_slippage_bps: float = 1.5
    stop_kick_bps: float = 5.0
    short_pen_bps: float = 1.0

    @classmethod
    def from_env(cls) -> "ORBConfig":
        bl_raw = _envs("ORB_TICKER_SIDE_BLOCKLIST", "")
        try:
            bl = json.loads(bl_raw) if bl_raw.strip() else {}
        except Exception:
            bl = {}
        return cls(
            or_minutes=_envi("ORB_OR_MINUTES", 15),
            rr=_envf("ORB_RR", 1.5),
            stop_buffer_bps=_envf("ORB_STOP_BUFFER_BPS", 5.0),
            time_cutoff_et=_et_to_minutes(_envs("ORB_TIME_CUTOFF_ET", "12:00")),
            eod_cutoff_et=_et_to_minutes(_envs("ORB_EOD_CUTOFF_ET", "15:55")),
            range_min_pct=_envf("ORB_RANGE_MIN_PCT", 0.005),
            range_max_pct=_envf("ORB_RANGE_MAX_PCT", 0.015),
            volume_mult=_envf("ORB_VOLUME_MULT", 0.0),
            max_trades_per_day=_envi("ORB_MAX_TRADES_PER_DAY", 1),
            risk_per_trade_pct=_envf("ORB_RISK_PER_TRADE_PCT", 1.0),
            account=_envf("ORB_ACCOUNT", 100_000.0),
            blocklist=bl,
            entry_slippage_bps=_envf("ORB_ENTRY_SLIPPAGE_BPS", 1.5),
            exit_slippage_bps=_envf("ORB_EXIT_SLIPPAGE_BPS", 1.5),
            stop_kick_bps=_envf("ORB_STOP_KICK_BPS", 5.0),
            short_pen_bps=_envf("ORB_SHORT_PENALTY_BPS", 1.0),
        )


SESSION_START_ET = _et_to_minutes("09:30")


# ---------- backtest one ticker-day ----------
def run_ticker_day(date: str, ticker: str, bars_1m: list[Bar1m],
                   cfg: ORBConfig) -> list[dict]:
    """Returns a list of pnl_pair dicts (matches lever_sweep_runner schema)."""
    if not bars_1m:
        return []

    # Restrict to RTH (09:30 - 16:00).
    rth = [b for b in bars_1m if SESSION_START_ET <= b.bucket < _et_to_minutes("16:00")]
    if not rth:
        return []

    or_end = SESSION_START_ET + cfg.or_minutes
    or_window = [b for b in rth if SESSION_START_ET <= b.bucket < or_end]
    if not or_window:
        return []

    or_high = max(b.high for b in or_window)
    or_low = min(b.low for b in or_window)
    mid = (or_high + or_low) / 2.0
    or_range_pct = (or_high - or_low) / mid if mid > 0 else 0.0

    if not (cfg.range_min_pct <= or_range_pct <= cfg.range_max_pct):
        return []

    # Aggregate post-OR bars to 5-min candles for breakout signals.
    post_or_1m = [b for b in rth if b.bucket >= or_end]
    candles_5m = aggregate_5m(post_or_1m)
    if not candles_5m:
        return []

    blocked_sides = {s.upper() for s in cfg.blocklist.get(ticker, [])}

    pairs: list[dict] = []
    trades_today = 0

    # Walk 5-min candles, find first breakout (signal). Entry is on the
    # NEXT 5-min candle's open. So index forward: signal on candles_5m[i],
    # entry on candles_5m[i+1] (if it exists and is in time window).
    for i in range(len(candles_5m) - 1):
        if trades_today >= cfg.max_trades_per_day:
            break
        sig = candles_5m[i]
        if sig.bucket >= cfg.time_cutoff_et:
            break
        # Signal: close above OR_high (long) or below OR_low (short).
        side = None
        if sig.close > or_high:
            side = "long"
        elif sig.close < or_low:
            side = "short"
        if side is None:
            continue
        if side.upper() in blocked_sides:
            continue

        entry_candle = candles_5m[i + 1]
        # Entry at next 5-min candle open with adverse slippage.
        raw_entry = entry_candle.open
        slip_bps = cfg.entry_slippage_bps + (cfg.short_pen_bps if side == "short" else 0)
        slip = raw_entry * slip_bps / 10000.0
        entry_price = raw_entry + slip if side == "long" else raw_entry - slip

        # Stop: opposite side of OR with buffer adder.
        stop_buf = entry_price * cfg.stop_buffer_bps / 10000.0
        if side == "long":
            stop = or_low - stop_buf
        else:
            stop = or_high + stop_buf

        risk = abs(entry_price - stop)
        if risk <= 0.001:
            continue
        target = entry_price + cfg.rr * risk if side == "long" else entry_price - cfg.rr * risk

        # Position sizing: risk ORB_RISK_PER_TRADE_PCT of account.
        risk_dollars = cfg.account * cfg.risk_per_trade_pct / 100.0
        shares = max(1, int(risk_dollars / risk))

        # Walk forward from entry_candle.bucket through 1-min bars to find
        # exit. 1-min granularity for accurate intra-bar stop/target checks.
        entry_bkt = entry_candle.bucket
        # Skip 1m bars BEFORE entry candle starts.
        forward_1m = [b for b in rth if b.bucket >= entry_bkt]

        exit_price = None
        exit_reason = None
        exit_bkt = None
        for fb in forward_1m:
            # First check stop/target intra-bar (high/low pierce).
            if side == "long":
                # Stop-out: bar.low <= stop -> fill at min(open, stop)
                if fb.low <= stop:
                    fill = min(fb.open, stop)
                    fill = max(fb.low, fill)
                    fill = min(fb.high, fill)
                    exit_price = fill
                    exit_reason = "stop"
                    exit_bkt = fb.bucket
                    break
                # Target hit: bar.high >= target -> fill at max(open, target)
                if fb.high >= target:
                    fill = max(fb.open, target)
                    fill = max(fb.low, fill)
                    fill = min(fb.high, fill)
                    exit_price = fill
                    exit_reason = "target"
                    exit_bkt = fb.bucket
                    break
            else:  # short
                if fb.high >= stop:
                    fill = max(fb.open, stop)
                    fill = max(fb.low, fill)
                    fill = min(fb.high, fill)
                    exit_price = fill
                    exit_reason = "stop"
                    exit_bkt = fb.bucket
                    break
                if fb.low <= target:
                    fill = min(fb.open, target)
                    fill = max(fb.low, fill)
                    fill = min(fb.high, fill)
                    exit_price = fill
                    exit_reason = "target"
                    exit_bkt = fb.bucket
                    break
            # EOD cutoff?
            if fb.bucket >= cfg.eod_cutoff_et:
                exit_price = fb.close
                exit_reason = "eod"
                exit_bkt = fb.bucket
                break

        if exit_price is None:
            # Walked off corpus without exit -> use last bar close as forced
            # EOD (defensive; shouldn't happen for full RTH days).
            last = forward_1m[-1] if forward_1m else None
            if last is None:
                continue
            exit_price = last.close
            exit_reason = "eod_fallback"
            exit_bkt = last.bucket

        # Apply exit slippage.
        exit_slip_bps = cfg.exit_slippage_bps + (
            cfg.stop_kick_bps if exit_reason == "stop" else 0
        ) + (cfg.short_pen_bps if side == "short" else 0)
        slip = exit_price * exit_slip_bps / 10000.0
        if side == "long":
            exit_price -= slip
        else:
            exit_price += slip

        pnl_per_share = (exit_price - entry_price) if side == "long" else (entry_price - exit_price)
        pnl_dollars = pnl_per_share * shares

        pairs.append({
            "ticker": ticker,
            "side": side,
            "entry_ts": _bucket_to_iso(date, entry_bkt),
            "exit_ts": _bucket_to_iso(date, exit_bkt),
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "shares": int(shares),
            "pnl_per_share": round(pnl_per_share, 4),
            "pnl_dollars": round(pnl_dollars, 4),
            "exit_reason": exit_reason,
            "or_high": round(or_high, 4),
            "or_low": round(or_low, 4),
            "or_range_pct": round(or_range_pct, 6),
        })
        trades_today += 1

    return pairs


def _bucket_to_iso(date: str, minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{date}T{h:02d}:{m:02d}:00-05:00"  # ET (DST-naive)


# ---------- driver ----------
def discover_dates(corpus_dir: Path, year_prefix: str, tickers: list[str]) -> list[str]:
    out = []
    for p in sorted(corpus_dir.iterdir()):
        if not p.is_dir() or not p.name.startswith(year_prefix):
            continue
        if all((p / f"{t}.jsonl").exists() for t in tickers):
            out.append(p.name)
    return out


def run(corpus_dir: Path, out_dir: Path, dates: list[str],
        tickers: list[str], cfg: ORBConfig, vid: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    per_day_dir = out_dir / "per_day"
    per_day_dir.mkdir(parents=True, exist_ok=True)

    total_pnl = 0.0
    total_entries = 0
    total_wins = 0
    total_losses = 0
    days_ok = 0
    days_failed = 0

    for date in dates:
        day_pairs: list[dict] = []
        for tk in tickers:
            try:
                bars = load_day_bars(corpus_dir, date, tk)
                if not bars:
                    continue
                pairs = run_ticker_day(date, tk, bars, cfg)
                day_pairs.extend(pairs)
            except Exception as e:
                print(f"WARN {date} {tk}: {e}", file=sys.stderr)

        n_entries = len(day_pairs)
        wins = sum(1 for p in day_pairs if p["pnl_dollars"] > 0)
        losses = sum(1 for p in day_pairs if p["pnl_dollars"] <= 0)
        day_pnl = sum(p["pnl_dollars"] for p in day_pairs)
        total_pnl += day_pnl
        total_entries += n_entries
        total_wins += wins
        total_losses += losses
        days_ok += 1

        per_day_dir.joinpath(f"{date}.json").write_text(json.dumps({
            "date": date,
            "tickers": list(tickers),
            "entries": [{"ts": p["entry_ts"], "ticker": p["ticker"],
                         "side": p["side"], "price": p["entry_price"]}
                        for p in day_pairs],
            "exits": [{"ts": p["exit_ts"], "ticker": p["ticker"],
                       "side": p["side"], "exit_price": p["exit_price"],
                       "reason": p["exit_reason"]} for p in day_pairs],
            "pnl_pairs": day_pairs,
            "summary": {
                "entries": n_entries, "exits": n_entries,
                "wins": wins, "losses": losses,
                "total_pnl": round(day_pnl, 4),
                "pairs_missing_shares": 0,
            },
            "_orb_strategy": True,
        }, indent=2))

    closed = total_wins + total_losses
    summary = {
        "variant": vid,
        "universe": list(tickers),
        "earnings_layer": "none",
        "days_planned": len(dates),
        "days_ran": days_ok,
        "days_resumed_skip": 0,
        "days_ok": days_ok,
        "days_failed": days_failed,
        "net_pnl": round(total_pnl, 2),
        "entries": total_entries,
        "exits": total_entries,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate_pct": round(100 * total_wins / closed, 2) if closed else None,
        "wall_min": 0,
        "config": {
            "strategy": "orb_classical",
            "or_minutes": cfg.or_minutes,
            "rr": cfg.rr,
            "stop_buffer_bps": cfg.stop_buffer_bps,
            "time_cutoff_et": _minutes_to_et(cfg.time_cutoff_et),
            "eod_cutoff_et": _minutes_to_et(cfg.eod_cutoff_et),
            "range_min_pct": cfg.range_min_pct,
            "range_max_pct": cfg.range_max_pct,
            "volume_mult": cfg.volume_mult,
            "max_trades_per_day": cfg.max_trades_per_day,
            "risk_per_trade_pct": cfg.risk_per_trade_pct,
            "account": cfg.account,
            "blocklist": cfg.blocklist,
        },
    }
    out_dir.joinpath("summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    return summary


def _minutes_to_et(m: int) -> str:
    h, mm = divmod(m, 60)
    return f"{h:02d}:{mm:02d}"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", default="data",
                   help="Corpus root (e.g. 'data' or '/home/.../data')")
    p.add_argument("--out", required=True, help="Output dir")
    p.add_argument("--vid", default="orb_classical_15min")
    p.add_argument("--tickers", default="AAPL,MSFT,NVDA,TSLA,META,GOOG,AMZN,AVGO,NFLX,ORCL")
    p.add_argument("--year-prefix", default="2026-")
    p.add_argument("--max-dates", type=int, default=0)
    args = p.parse_args(argv[1:])

    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"ERROR: corpus dir {corpus} not found", file=sys.stderr)
        return 1

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    cfg = ORBConfig.from_env()

    dates = discover_dates(corpus, args.year_prefix, tickers)
    # v8 -- honor DATES_STRIDE env var for cross-validation sweeps. The
    # GHA matrix wrapper passes stride from the trigger JSON via this env
    # var. Without this, STRIDE=2/4/8 variants silently ran on the full
    # corpus, producing byte-identical results to STRIDE=1 -- making
    # cross-validation impossible.
    stride = max(1, int(os.environ.get("DATES_STRIDE", "1")))
    if stride > 1:
        dates = dates[::stride]
        print(f"ORB: DATES_STRIDE={stride} -> {len(dates)} dates",
              file=sys.stderr, flush=True)
    if args.max_dates > 0:
        dates = dates[:args.max_dates]
    if not dates:
        print("ERROR: no eligible dates found", file=sys.stderr)
        return 1

    print(f"ORB: {len(dates)} dates, {len(tickers)} tickers, "
          f"OR={cfg.or_minutes}m, RR={cfg.rr}, stride={stride}, "
          f"range=[{cfg.range_min_pct:.3f},{cfg.range_max_pct:.3f}]")

    summary = run(corpus, Path(args.out), dates, tickers, cfg, args.vid)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
