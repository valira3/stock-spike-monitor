"""CLI wrapper around orb.premarket_scanner.scan_day for one-off inspection.

Usage:
    python tools/run_premarket_scanner.py \\
        --corpus data_pm_universe \\
        --date 2026-05-15 \\
        --universe data/universe/sp500.json \\
        --signal composite --top-k 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from orb.premarket_scanner import scan_universe_to_dict


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", default="data_pm_universe")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--universe", required=True, help="JSON file with tickers[] field")
    p.add_argument("--signal", default="composite",
                   choices=["gap", "volume", "range", "composite"])
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--min-pm-bars", type=int, default=10)
    p.add_argument("--min-dollar-volume", type=float, default=100_000.0)
    p.add_argument("--out", default="", help="Optional: write JSON to file")
    args = p.parse_args(argv[1:])

    uni = json.loads(Path(args.universe).read_text())
    tickers = uni["tickers"]
    out = scan_universe_to_dict(
        args.corpus,
        args.date,
        tickers,
        signal=args.signal,
        top_k=args.top_k,
        min_pm_bars=args.min_pm_bars,
        min_dollar_volume=args.min_dollar_volume,
    )
    text = json.dumps(out, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
