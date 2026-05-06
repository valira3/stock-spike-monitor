"""tools/backfill_canonical_et_bucket.py \u2014 in-place et_bucket backfill
for the canonical 83-day SIP replay archive.

v7.0.7 \u2014 motivated by the discovery that every bar in
``/home/user/workspace/canonical_backtest_data/84day_2026_sip/replay_layout/``
was written with ``et_bucket: null``. The v6.15.3 SPY regime backfill
scans ``et_bucket=='0930'`` / ``'1000'``, so it never finds anchors and
``_SPY_REGIME.regime`` stays ``None`` for the entire replay \u2014 silently
disarming the V611 short-amplification gate (broker/orders.py:1000) in
every backtest variant since v6.11.

This script walks every ``replay_layout/<date>/<TICKER>.jsonl``, recomputes
``et_bucket`` from ``ts`` using the production helper
``ingest.algo_plus._compute_et_bucket``, and rewrites each file in place.
It is idempotent: running it again on already-backfilled data is a no-op
(file content matches, mtime preserved when --skip-unchanged is set).

USAGE:
    # Dry run \u2014 print what would change, write nothing:
    python3 tools/backfill_canonical_et_bucket.py --dry-run

    # Backfill the default canonical archive in place:
    python3 tools/backfill_canonical_et_bucket.py

    # Limit to a single date or ticker (smoke):
    python3 tools/backfill_canonical_et_bucket.py \\
        --since 2026-01-02 --until 2026-01-02 --tickers SPY

INPUTS (per-line JSON, canonical schema):
    ts, et_bucket (often null), open, high, low, close, volume, n, vw,
    bid, ask, last_trade_price, _feed

OUTPUT:
    Same lines, with ``et_bucket`` recomputed from ``ts``. RTH bars get
    a ``"HHMM"`` string; pre-/post-market bars get ``null`` (this matches
    production behavior \u2014 ``_compute_et_bucket`` returns None outside
    [09:30, 16:00] ET).

CONSTRAINTS:
    - Forbidden-word policy honored.
    - All em-dashes escaped as \\u2014 in source.
    - No external HTTP calls; pure local file rewrite.
    - Atomic per-file write via tmp + os.replace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from ingest.algo_plus import _compute_et_bucket  # type: ignore
except Exception:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")

    def _compute_et_bucket(ts):  # type: ignore[no-redef]
        try:
            if isinstance(ts, datetime):
                ts_utc = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
            else:
                s = str(ts).strip()
                if not s:
                    return None
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                ts_utc = datetime.fromisoformat(s)
                if ts_utc.tzinfo is None:
                    ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
        ts_et = ts_utc.astimezone(_ET_TZ)
        h, m = ts_et.hour, ts_et.minute
        in_rth = (
            (h == 9 and m >= 30)
            or (10 <= h <= 15)
            or (h == 16 and m == 0)
        )
        if not in_rth:
            return None
        return f"{h:02d}{m:02d}"


DEFAULT_ROOT = "/home/user/workspace/canonical_backtest_data/84day_2026_sip/replay_layout"


def _process_file(path: Path, dry_run: bool) -> tuple[int, int, int, int]:
    """Return (n_lines, n_changed, n_rth, n_extended)."""
    n_lines = 0
    n_changed = 0
    n_rth = 0
    n_extended = 0
    out_lines: list[str] = []

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            n_lines += 1
            stripped = line.rstrip("\n")
            if not stripped.strip():
                out_lines.append(stripped)
                continue
            try:
                bar = json.loads(stripped)
            except (ValueError, TypeError):
                out_lines.append(stripped)
                continue
            ts = bar.get("ts")
            new_bucket = _compute_et_bucket(ts) if ts is not None else None
            old_bucket = bar.get("et_bucket")
            if new_bucket is not None:
                n_rth += 1
            else:
                n_extended += 1
            if old_bucket != new_bucket:
                n_changed += 1
                bar["et_bucket"] = new_bucket
                out_lines.append(json.dumps(bar, separators=(", ", ": ")))
            else:
                out_lines.append(stripped)

    if not dry_run and n_changed > 0:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for ln in out_lines:
                f.write(ln + "\n")
        os.replace(tmp, path)

    return n_lines, n_changed, n_rth, n_extended


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="Canonical replay_layout root")
    ap.add_argument("--since", default=None,
                    help="Inclusive start date YYYY-MM-DD")
    ap.add_argument("--until", default=None,
                    help="Inclusive end date YYYY-MM-DD")
    ap.add_argument("--tickers", default=None,
                    help="Comma-separated ticker subset (default: all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't rewrite files; print stats only")
    ap.add_argument("--verbose", action="store_true",
                    help="Per-file logging")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: root not found: {root}", file=sys.stderr)
        return 2

    tickers_filter: set[str] | None = None
    if args.tickers:
        tickers_filter = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}

    date_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if args.since:
        date_dirs = [p for p in date_dirs if p.name >= args.since]
    if args.until:
        date_dirs = [p for p in date_dirs if p.name <= args.until]

    total_files = 0
    total_lines = 0
    total_changed = 0
    total_rth = 0
    total_ext = 0

    for ddir in date_dirs:
        files = sorted(ddir.glob("*.jsonl"))
        if tickers_filter is not None:
            files = [f for f in files if f.stem.upper() in tickers_filter]
        for fp in files:
            n_lines, n_changed, n_rth, n_ext = _process_file(fp, args.dry_run)
            total_files += 1
            total_lines += n_lines
            total_changed += n_changed
            total_rth += n_rth
            total_ext += n_ext
            if args.verbose:
                print(f"  {ddir.name}/{fp.name}: lines={n_lines} "
                      f"changed={n_changed} rth={n_rth} ext={n_ext}")

    verb = "would change" if args.dry_run else "changed"
    print(f"\nDone. files={total_files} lines={total_lines} "
          f"{verb}={total_changed} rth_bars={total_rth} extended_bars={total_ext}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
