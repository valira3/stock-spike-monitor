"""simulator.synth_corpus -- perturb real days into synthetic scenarios.

Real market history covers most regimes naturally, but some scenarios
are rare (multi-stock halts, flash crashes, extreme volatility shocks)
and you cannot wait for them to test what the bot does. This module
takes a real corpus day and applies a documented perturbation to
produce a synthetic copy under simulator/corpus/synthetic/<name>/.

Perturbations (all preserve bar count + timestamp grid):

  gap_up_<pct>           open scaled by +pct%, rest of day rebased
                         around the new open
  gap_down_<pct>         open scaled by -pct%
  flash_crash_<pct>      inject a -pct% trough between 11:00 and 14:00
                         that recovers within 30 minutes
  halt_<minutes>         zero out volume for `minutes` consecutive bars
                         starting at 10:30 ET (looks like a halt)
  vix_spike              annotate a synthetic VIX value above the
                         ORB_SKIP_VIX_ABOVE threshold (write to
                         <output>/vix-override.json)
  multi_halt_<tickers>   halt N tickers simultaneously between
                         10:30-10:45 ET (proxy for systemic event)
  vol_3x                 triple the (high-low) range of every bar
                         (extreme intraday volatility)

CLI:
    python -m simulator.synth_corpus \\
        --from-day 2026-05-15 \\
        --perturbation gap_up_3 \\
        --tickers AAPL,MSFT,NVDA

  Output: simulator/corpus/synthetic/gap_up_3_2026-05-15/
            AAPL.jsonl  MSFT.jsonl  NVDA.jsonl  manifest.json

Batch mode (parallel across (day, perturbation) combinations):

    python -m simulator.synth_corpus \\
        --batch 2026-05-12,2026-05-13,2026-05-14 \\
        --perturbations gap_up_3,halt_15,flash_crash_2 \\
        --workers 4
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as _mp
import os
import sys
from typing import Dict, List, Optional, Tuple


# ----------------------------------------------------------------------
# Perturbation primitives
# ----------------------------------------------------------------------


def _parse_pct(s: str, suffix: str) -> float:
    """gap_up_3 -> 3.0"""
    tail = s[len(suffix):]
    try:
        return float(tail)
    except Exception:
        return 0.0


def _parse_int(s: str, suffix: str) -> int:
    tail = s[len(suffix):]
    try:
        return int(tail)
    except Exception:
        return 0


def perturb_gap(bars: List[dict], pct: float, direction: str) -> List[dict]:
    """Shift every price by pct% in the chosen direction. `bars` is
    in-memory; returns a NEW list."""
    if not bars or pct == 0:
        return list(bars)
    multiplier = 1.0 + (pct / 100.0) * (1 if direction == "up" else -1)
    out = []
    for b in bars:
        nb = dict(b)
        for k in ("open", "high", "low", "close"):
            if k in nb and nb[k] is not None:
                nb[k] = round(float(nb[k]) * multiplier, 4)
        out.append(nb)
    return out


def perturb_halt(bars: List[dict], minutes: int, start_hh: int = 10,
                 start_mm: int = 30) -> List[dict]:
    """Zero out the volume on `minutes` consecutive bars starting at
    ET start_hh:start_mm. Volume==0 is what the bot uses to detect a
    halt."""
    if minutes <= 0:
        return list(bars)
    from simulator.corpus_index import _bar_bucket as _bucket
    start_b = start_hh * 60 + start_mm
    end_b = start_b + minutes
    out = []
    for b in bars:
        bk = _bucket(b)
        if start_b <= bk < end_b:
            nb = dict(b)
            nb["iex_volume"] = 0
            nb["total_volume"] = 0
            out.append(nb)
        else:
            out.append(dict(b))
    return out


def perturb_flash_crash(bars: List[dict], pct: float) -> List[dict]:
    """Inject a -pct% drawdown between 11:00 and 11:30 ET, recovering
    by 11:45. Floor is preserved per-bar so OHLC remains consistent."""
    if not bars or pct <= 0:
        return list(bars)
    from simulator.corpus_index import _bar_bucket as _bucket
    crash_start = 11 * 60
    crash_low = 11 * 60 + 15
    crash_end = 11 * 60 + 30
    out = []
    for b in bars:
        bk = _bucket(b)
        nb = dict(b)
        if crash_start <= bk <= crash_end:
            # Linear scaling: deepest at crash_low, none at the edges.
            if bk <= crash_low:
                progress = (bk - crash_start) / (crash_low - crash_start) if crash_low > crash_start else 1.0
            else:
                progress = 1.0 - (bk - crash_low) / (crash_end - crash_low) if crash_end > crash_low else 1.0
            factor = 1.0 - (pct / 100.0) * progress
            for k in ("open", "high", "low", "close"):
                if k in nb and nb[k] is not None:
                    nb[k] = round(float(nb[k]) * factor, 4)
        out.append(nb)
    return out


def perturb_vol_3x(bars: List[dict]) -> List[dict]:
    """Triple the high-low range of every bar (open/close unchanged)."""
    out = []
    for b in bars:
        nb = dict(b)
        if all(k in nb for k in ("open", "high", "low", "close")):
            o = float(nb["open"]); c = float(nb["close"])
            h = float(nb["high"]); lo = float(nb["low"])
            mid = (h + lo) / 2.0
            new_h = mid + (h - mid) * 3.0
            new_lo = mid - (mid - lo) * 3.0
            # Re-clip so open + close stay inside.
            new_h = max(new_h, o, c)
            new_lo = min(new_lo, o, c)
            nb["high"] = round(new_h, 4)
            nb["low"] = round(new_lo, 4)
        out.append(nb)
    return out


# ----------------------------------------------------------------------
# Per-day perturbation orchestration (parallelizable)
# ----------------------------------------------------------------------


def _load_day(date: str, ticker: str, corpus_root: str) -> List[dict]:
    path = os.path.join(corpus_root, date, f"{ticker}.jsonl")
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _write_day(out_dir: str, ticker: str, bars: List[dict]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{ticker}.jsonl"), "w") as fh:
        for b in bars:
            fh.write(json.dumps(b) + "\n")


def _apply_perturbation(bars: List[dict], perturbation: str) -> List[dict]:
    """Dispatch to the right perturb_* function based on the name."""
    if perturbation.startswith("gap_up_"):
        return perturb_gap(bars, _parse_pct(perturbation, "gap_up_"), "up")
    if perturbation.startswith("gap_down_"):
        return perturb_gap(bars, _parse_pct(perturbation, "gap_down_"), "down")
    if perturbation.startswith("halt_"):
        return perturb_halt(bars, _parse_int(perturbation, "halt_"))
    if perturbation.startswith("flash_crash_"):
        return perturb_flash_crash(bars, _parse_pct(perturbation, "flash_crash_"))
    if perturbation == "vol_3x":
        return perturb_vol_3x(bars)
    if perturbation == "vix_spike":
        # Bars unchanged -- VIX override is emitted separately.
        return list(bars)
    raise ValueError(f"unknown perturbation: {perturbation}")


def _generate_one(args: Tuple[str, str, List[str], str, str]) -> dict:
    """Worker. args = (source_date, perturbation, tickers, corpus_root,
    output_root)."""
    source_date, perturbation, tickers, corpus_root, output_root = args
    out_dir = os.path.join(output_root, f"{perturbation}_{source_date}")
    written = 0
    for ticker in tickers:
        bars = _load_day(source_date, ticker, corpus_root)
        if not bars:
            continue
        out_bars = _apply_perturbation(bars, perturbation)
        _write_day(out_dir, ticker, out_bars)
        written += 1

    # Always write a manifest describing what this synthetic day represents.
    manifest = {
        "source_date": source_date,
        "perturbation": perturbation,
        "tickers": tickers,
        "out_dir": out_dir,
    }
    if perturbation == "vix_spike":
        manifest["vix_override"] = 28.0
        with open(os.path.join(out_dir, "vix-override.json"), "w") as fh:
            json.dump({"vix_close": 28.0}, fh)

    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    manifest["written"] = written
    return manifest


def generate_batch(
    source_dates: List[str],
    perturbations: List[str],
    tickers: List[str],
    corpus_root: str = "data",
    output_root: str = "simulator/corpus/synthetic",
    workers: int = 0,
) -> List[dict]:
    """Generate every (date, perturbation) combination in parallel.

    Returns the list of manifest dicts (one per synthetic day produced).
    """
    workers = workers or max(1, _mp.cpu_count() // 2)
    tasks: List[Tuple] = [
        (d, p, list(tickers), corpus_root, output_root)
        for d in source_dates for p in perturbations
    ]
    if not tasks:
        return []
    if workers == 1 or len(tasks) == 1:
        return [_generate_one(t) for t in tasks]
    ctx = _mp.get_context("spawn")
    with ctx.Pool(processes=workers) as pool:
        return list(pool.imap(_generate_one, tasks))


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


KNOWN_PERTURBATIONS = (
    "gap_up_<pct>", "gap_down_<pct>", "halt_<minutes>",
    "flash_crash_<pct>", "vol_3x", "vix_spike",
)


def _main(argv=None):
    p = argparse.ArgumentParser(description="Synthesize perturbed corpus days")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--from-day", help="Single source day YYYY-MM-DD")
    g.add_argument("--batch", help="Comma-separated source days")
    p.add_argument("--perturbation",
                   help=f"One perturbation. Known: {KNOWN_PERTURBATIONS}")
    p.add_argument("--perturbations",
                   help="Comma-separated perturbation set (batch mode)")
    p.add_argument("--tickers",
                   default="AAPL,MSFT,NVDA,AMZN,META,GOOG,AVGO,NFLX,ORCL,TSLA,QQQ,SPY")
    p.add_argument("--corpus-root", default="data")
    p.add_argument("--output-root", default="simulator/corpus/synthetic")
    p.add_argument("--workers", type=int, default=0)
    args = p.parse_args(argv)

    dates = [args.from_day] if args.from_day else [
        d.strip() for d in args.batch.split(",") if d.strip()
    ]
    perts = ([args.perturbation] if args.perturbation else
             [p.strip() for p in (args.perturbations or "").split(",") if p.strip()])
    if not perts:
        raise SystemExit("Provide --perturbation X or --perturbations X,Y,Z")

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    manifests = generate_batch(
        source_dates=dates,
        perturbations=perts,
        tickers=tickers,
        corpus_root=args.corpus_root,
        output_root=args.output_root,
        workers=args.workers,
    )

    print(f"[synth_corpus] wrote {len(manifests)} synthetic days "
          f"({len(dates)} dates x {len(perts)} perturbations)")
    for m in manifests[:10]:
        print(f"  -> {m['out_dir']}  ({m['written']} tickers)")
    if len(manifests) > 10:
        print(f"  ... ({len(manifests) - 10} more)")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
