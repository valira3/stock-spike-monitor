"""tools/backfill_bar_archive.py \u2014 one-shot backfill of /data/bars/ from
the canonical 83-day SIP archive.

v6.14.0 \u2014 motivated by issue #354 (volume gate broken end-to-end). The
production /data/bars archive only carried 6 days of history at the time
of the fix; the volume baseline lookback is 55 days. This tool re-maps
the canonical archive into the production schema and appends bars that
are not already present.

USAGE:
    # Local dry run (prints what would happen, writes nothing):
    python3 tools/backfill_bar_archive.py --dry-run

    # On Railway (run as the SSH session user, writes into /data/bars):
    python3 tools/backfill_bar_archive.py --target /data/bars

    # Limit to a date range or a ticker subset (smoke test):
    python3 tools/backfill_bar_archive.py --target /tmp/test_bars \\
        --since 2026-04-01 --tickers AAPL,MSFT

CANONICAL ARCHIVE SCHEMA (input, per-line JSON):
    ts, open, high, low, close, volume, n, vw, bid, ask,
    last_trade_price, _feed

PRODUCTION SCHEMA (output, per bar_archive.BAR_SCHEMA_FIELDS):
    ts, et_bucket, open, high, low, close, total_volume, iex_volume,
    iex_sip_ratio_used, bid, ask, last_trade_price, trade_count,
    bar_vwap, feed_source

FIELD MAP:
    canonical.volume -> production.total_volume
    canonical.n      -> production.trade_count
    canonical.vw     -> production.bar_vwap
    canonical._feed  -> production.feed_source
    et_bucket        -> recomputed from ts via _compute_et_bucket
    iex_volume       -> None (legacy field)
    iex_sip_ratio_used -> None (legacy field)

IDEMPOTENCY:
    The tool reads existing JSONL on the target side and skips any line
    whose ``ts`` already exists. Re-running is safe.

CONSTRAINTS:
    - Forbidden words (scrape/crawl) absent.
    - All em-dashes escaped as \\u2014 in source.
    - No external HTTP calls; everything reads local files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Re-use the production helper so the et_bucket logic matches exactly.
# Falls back to a local copy if run from a path where ingest/ is not on
# sys.path (e.g. invoking this script with --dry-run on a fresh checkout
# from outside the repo root).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from ingest.algo_plus import _compute_et_bucket  # type: ignore
except Exception:
    # Local fallback (kept in sync with ingest/algo_plus.py).
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


DEFAULT_SOURCE = "/home/user/workspace/canonical_backtest_data/84day_2026_sip/replay_layout"
DEFAULT_TARGET = "/data/bars"

# Production universe (12 tickers). Only these are needed by the volume
# baseline; canonical archive carries others (NFLX, ORCL, SPY, QQQ are in
# both; AMD/ASML/BRK.B etc are skipped to keep the archive small).
DEFAULT_TICKERS = (
    "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG", "AMZN",
    "AVGO", "NFLX", "ORCL", "SPY", "QQQ",
)


def _remap_canonical_bar(raw: dict) -> dict | None:
    """Map a canonical-archive bar dict to the production schema.

    Returns None if the bar lacks the bare-minimum fields needed to be
    useful for the volume baseline (a parseable ts and a non-null volume).
    """
    ts = raw.get("ts")
    if not ts:
        return None
    vol_raw = raw.get("volume")
    try:
        total_vol = float(vol_raw) if vol_raw is not None else None
    except (TypeError, ValueError):
        total_vol = None
    if total_vol is None:
        return None  # cannot help the baseline
    return {
        "ts": ts,
        "et_bucket": _compute_et_bucket(ts),
        "open": raw.get("open"),
        "high": raw.get("high"),
        "low": raw.get("low"),
        "close": raw.get("close"),
        "total_volume": total_vol,
        "iex_volume": None,
        "iex_sip_ratio_used": None,
        "bid": raw.get("bid"),
        "ask": raw.get("ask"),
        "last_trade_price": raw.get("last_trade_price"),
        "trade_count": raw.get("n"),
        "bar_vwap": raw.get("vw"),
        "feed_source": str(raw.get("_feed") or "sip"),
    }


def _read_existing_ts(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    out.add(str(json.loads(line).get("ts") or ""))
                except (ValueError, TypeError):
                    continue
    except OSError:
        return set()
    return out


def _backfill_one(
    source_day_dir: Path,
    target_day_dir: Path,
    tickers: tuple[str, ...],
    *,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Backfill a single day. Returns (bars_written, bars_skipped, files_touched)."""
    written = 0
    skipped = 0
    files_touched = 0
    if not source_day_dir.exists() or not source_day_dir.is_dir():
        return (0, 0, 0)
    target_day_dir.mkdir(parents=True, exist_ok=True)
    for ticker in tickers:
        src = source_day_dir / f"{ticker}.jsonl"
        if not src.exists():
            continue
        dst = target_day_dir / f"{ticker}.jsonl"
        existing = _read_existing_ts(dst)
        new_lines: list[str] = []
        try:
            with open(src, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    remapped = _remap_canonical_bar(raw)
                    if remapped is None:
                        skipped += 1
                        continue
                    if remapped["ts"] in existing:
                        skipped += 1
                        continue
                    new_lines.append(
                        json.dumps(remapped, separators=(",", ":")) + "\n"
                    )
        except OSError as e:
            print(f"[WARN] read failed: {src}: {e}", file=sys.stderr)
            continue
        if not new_lines:
            continue
        files_touched += 1
        if dry_run:
            written += len(new_lines)
            continue
        try:
            with open(dst, "a", encoding="utf-8") as fh:
                fh.writelines(new_lines)
            written += len(new_lines)
        except OSError as e:
            print(f"[WARN] write failed: {dst}: {e}", file=sys.stderr)
    return (written, skipped, files_touched)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill /data/bars from canonical SIP archive.")
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help=f"Canonical archive root (default: {DEFAULT_SOURCE})")
    ap.add_argument("--target", default=DEFAULT_TARGET,
                    help=f"Production /data/bars target (default: {DEFAULT_TARGET})")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS),
                    help="Comma-separated ticker list (default: 12 prod tickers)")
    ap.add_argument("--since", default=None,
                    help="Only backfill days >= YYYY-MM-DD (default: all)")
    ap.add_argument("--until", default=None,
                    help="Only backfill days <= YYYY-MM-DD (default: all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute what would be written without touching disk")
    args = ap.parse_args()

    src_root = Path(args.source)
    tgt_root = Path(args.target)
    tickers = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())

    if not src_root.exists():
        print(f"[FAIL] source not found: {src_root}", file=sys.stderr)
        return 2

    def _parse_iso(s: str | None) -> date | None:
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except ValueError:
            print(f"[FAIL] bad date: {s}", file=sys.stderr)
            sys.exit(2)

    since = _parse_iso(args.since)
    until = _parse_iso(args.until)

    day_dirs: list[Path] = sorted(
        d for d in src_root.iterdir()
        if d.is_dir() and len(d.name) == 10 and d.name[4] == "-" and d.name[7] == "-"
    )

    total_written = 0
    total_skipped = 0
    total_files = 0
    days_touched = 0

    print(f"[BACKFILL] source={src_root}")
    print(f"[BACKFILL] target={tgt_root}")
    print(f"[BACKFILL] tickers={','.join(tickers)}")
    print(f"[BACKFILL] mode={'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"[BACKFILL] day_dirs={len(day_dirs)}")
    if since:
        print(f"[BACKFILL] since={since.isoformat()}")
    if until:
        print(f"[BACKFILL] until={until.isoformat()}")

    for src_day in day_dirs:
        try:
            day = date.fromisoformat(src_day.name)
        except ValueError:
            continue
        if since is not None and day < since:
            continue
        if until is not None and day > until:
            continue
        tgt_day = tgt_root / src_day.name
        w, s, f = _backfill_one(src_day, tgt_day, tickers, dry_run=args.dry_run)
        total_written += w
        total_skipped += s
        total_files += f
        if w or s:
            days_touched += 1
            print(
                f"[BACKFILL] {src_day.name}: written={w} skipped={s} files={f}"
            )

    print(
        f"[BACKFILL] DONE: days_touched={days_touched} "
        f"total_written={total_written} total_skipped={total_skipped} "
        f"files_touched={total_files}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
