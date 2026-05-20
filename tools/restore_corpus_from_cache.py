"""Recovery: reconstruct data_pm_universe/<date>/<TICKER>.jsonl from
the per-(date, ticker) pickle cache at .bt_cache/<date>/<TICKER>.pkl.

Use when the source JSONL files were accidentally deleted (e.g. by
`find -delete` traversing the simulator's bars/ symlink). The pickle
cache is built once via tools/build_sim_bar_cache.py and is a
verbatim copy of the JSONL row dicts -- so reverse-restoring is
loss-free.

Idempotent: dates that already have JSONLs are skipped.

Usage:
    python tools/restore_corpus_from_cache.py
    python tools/restore_corpus_from_cache.py --corpus data_pm_universe
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from multiprocessing import Pool, cpu_count


def _restore_one(args) -> tuple[str, int]:
    date, corpus_root = args
    cache_dir = os.path.join(corpus_root, ".bt_cache", date)
    dst_dir = os.path.join(corpus_root, date)
    if not os.path.isdir(cache_dir):
        return (date, 0)
    if os.path.isdir(dst_dir) and any(
        f.endswith(".jsonl") for f in os.listdir(dst_dir)
    ):
        return (date, -1)  # skipped, already present
    os.makedirs(dst_dir, exist_ok=True)
    count = 0
    for fname in os.listdir(cache_dir):
        if not fname.endswith(".pkl"):
            continue
        ticker = fname[:-4]
        try:
            with open(os.path.join(cache_dir, fname), "rb") as fh:
                bars = pickle.load(fh)
        except Exception:
            continue
        if not isinstance(bars, list):
            continue
        out_path = os.path.join(dst_dir, f"{ticker}.jsonl")
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "w") as fh:
            for b in bars:
                fh.write(json.dumps(b) + "\n")
        os.replace(tmp_path, out_path)
        count += 1
    return (date, count)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", default="data_pm_universe")
    p.add_argument("--workers", type=int, default=0)
    args = p.parse_args(argv)

    cache_root = os.path.join(args.corpus, ".bt_cache")
    if not os.path.isdir(cache_root):
        print(f"No cache at {cache_root}", file=sys.stderr)
        return 1
    dates = sorted(os.listdir(cache_root))
    print(f"Recovering {len(dates)} dates from pickle cache...")
    workers = args.workers or max(1, cpu_count() // 2)
    with Pool(processes=workers) as pool:
        results = pool.map(_restore_one,
                           [(d, args.corpus) for d in dates])
    rebuilt = sum(1 for _, c in results if c > 0)
    skipped = sum(1 for _, c in results if c == -1)
    empty = sum(1 for _, c in results if c == 0)
    files = sum(c for _, c in results if c > 0)
    print(f"Done: {rebuilt} dates rebuilt ({files} ticker files), "
          f"{skipped} skipped, {empty} empty")
    return 0


if __name__ == "__main__":
    sys.exit(main())
