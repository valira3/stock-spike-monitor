"""Build the (date, ticker) -> premarket-features pickle cache.

Run once after a fresh pull. Subsequent scans / sweeps load this in
~1s instead of doing 173k file reads per cell.

Usage:
    python tools/build_scanner_cache.py \\
        --pm-corpus data_pm_universe \\
        --universe data/universe/sp500.json \\
        --workers 12
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orb.scanner_cache import build_cache, save_cache


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pm-corpus", default="data_pm_universe")
    p.add_argument("--universe", required=True)
    p.add_argument("--workers", type=int, default=12)
    args = p.parse_args(argv[1:])

    pm = Path(args.pm_corpus)
    if not pm.is_dir():
        print(f"ERROR: {pm} not found", file=sys.stderr)
        return 1

    dates = sorted(d.name for d in pm.iterdir()
                   if d.is_dir() and d.name[0].isdigit())
    uni = json.loads(Path(args.universe).read_text())
    tickers = uni["tickers"]

    t0 = time.time()
    cache = build_cache(pm, dates, tickers, workers=args.workers)
    path = save_cache(cache, pm)
    print(f"\nWrote {path}  ({len(cache):,} rows, {time.time()-t0:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
