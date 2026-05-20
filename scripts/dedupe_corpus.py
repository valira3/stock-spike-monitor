"""Dedupe bar JSONL corpus files by (et_bucket) -> single best-source row.

Why: data_pm_universe (sim source) and data (backtest source) both accumulated
duplicate bars over time. data_pm_universe has up to 4-17x stacks per minute,
with src=None chunks containing shifted-bucket replays of the sip chunk. data
has 2x stacks (sip + iex). Both inflate 5m aggregation OHLC and produce
non-deterministic engine behavior.

Priority order (best -> worst): sip > iex > <None/missing>.
Within same source, keep the first occurrence (file order).

Output is written to a separate directory so the original corpus is preserved.
"""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

SOURCE_PRIORITY = {"sip": 3, "iex": 2}


def dedupe_file(src: Path, dst: Path) -> dict:
    """Dedupe a single JSONL file. Returns stats dict."""
    best = {}  # et_bucket_str -> (priority, row_index, row)
    n_in = 0
    with src.open("r") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_in += 1
            eb = row.get("et_bucket")
            if eb is None:
                # No bucket -> can't dedupe this row; skip
                continue
            key = str(eb)
            pri = SOURCE_PRIORITY.get(row.get("feed_source"), 1)
            existing = best.get(key)
            if existing is None or pri > existing[0]:
                best[key] = (pri, i, row)

    rows = [v[2] for v in best.values()]
    # Sort by et_bucket numerically (HHMM -> minutes)
    def to_mins(r):
        eb = r.get("et_bucket", "0000")
        s = str(eb).zfill(4)
        try:
            return int(s[:2]) * 60 + int(s[2:])
        except ValueError:
            return -1

    rows.sort(key=to_mins)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    return {
        "in": n_in,
        "out": len(rows),
        "dropped": n_in - len(rows),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--src", required=True, help="Source corpus root, e.g. data/")
    p.add_argument("--dst", required=True, help="Destination root, e.g. data_dedup/")
    p.add_argument(
        "--tickers",
        default="",
        help="Comma-separated ticker filter. Empty = all tickers in source.",
    )
    p.add_argument("--limit-dates", type=int, default=0, help="If >0, only process first N dates")
    args = p.parse_args()

    src_root = Path(args.src)
    dst_root = Path(args.dst)
    if not src_root.is_dir():
        sys.exit(f"src dir not found: {src_root}")
    if dst_root.exists() and not dst_root.is_dir():
        sys.exit(f"dst path is not a dir: {dst_root}")

    ticker_filter = set(t.strip().upper() for t in args.tickers.split(",") if t.strip())

    date_dirs = sorted(d for d in src_root.iterdir() if d.is_dir() and d.name.startswith("20"))
    if args.limit_dates > 0:
        date_dirs = date_dirs[: args.limit_dates]

    total_in = 0
    total_out = 0
    files_done = 0
    by_drop = Counter()
    for dd in date_dirs:
        for src_file in dd.glob("*.jsonl"):
            tk = src_file.stem.upper()
            if ticker_filter and tk not in ticker_filter:
                continue
            dst_file = dst_root / dd.name / src_file.name
            try:
                stats = dedupe_file(src_file, dst_file)
            except Exception as e:
                print(f"  ERROR {src_file}: {e}", file=sys.stderr)
                continue
            total_in += stats["in"]
            total_out += stats["out"]
            by_drop[stats["dropped"] > 0] += 1
            files_done += 1
        if files_done and files_done % 200 == 0:
            print(f"  ... {files_done} files done")

    print(
        f"\nDone. {files_done} files. {total_in} rows in, {total_out} rows out, "
        f"{total_in - total_out} dupes dropped ({(total_in - total_out) / max(total_in, 1) * 100:.1f}%)."
    )
    print(f"  Files with at least one dupe dropped: {by_drop.get(True, 0)}")
    print(f"  Files clean (no dupes): {by_drop.get(False, 0)}")


if __name__ == "__main__":
    main()
