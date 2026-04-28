"""v5.11.0 PR1 \u2014 verify the golden harness output is byte-equal.

Re-runs `record_session` to a tmp file and asserts byte-equality
against the committed golden JSONL. Used as the validation gate
both before and after the engine extraction.
"""
from __future__ import annotations

import argparse
import filecmp
import pathlib
import sys
import tempfile

from tests.golden import record_session

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default="2026-04-28")
    p.add_argument("--golden", default=None)
    args = p.parse_args(argv)

    golden = pathlib.Path(args.golden or
        REPO_ROOT / "tests" / "golden" / f"v5_10_7_session_{args.date}.jsonl")
    if not golden.exists():
        print(f"FAIL: golden not found: {golden}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as td:
        actual = pathlib.Path(td) / "actual.jsonl"
        record_session.record(args.date, actual)
        if filecmp.cmp(str(golden), str(actual), shallow=False):
            print(f"OK: byte-equal vs {golden} ({golden.stat().st_size} bytes)")
            return 0
        # Show first diverging line for diagnostics.
        with golden.open() as g, actual.open() as a:
            for i, (lg, la) in enumerate(zip(g, a), start=1):
                if lg != la:
                    print(f"FAIL: first diff at line {i}", file=sys.stderr)
                    print(f"  golden: {lg.rstrip()[:200]}", file=sys.stderr)
                    print(f"  actual: {la.rstrip()[:200]}", file=sys.stderr)
                    return 1
        print("FAIL: length differs", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
