"""v5.11.0 PR1 \u2014 synthetic golden harness for engine extraction.

Streams archived 1m bars for the universe through
`compute_5m_ohlc_and_ema9` (the function being moved into
`engine/bars.py`) and writes a deterministic JSONL trace of
(ticker, ts, args_hash, return) tuples.

The harness is deliberately narrow: PR1 only moves a single pure
compute function. Recording every `evaluate_*` call across the
full per-tick scan loop is overkill for this PR (those code paths
are extracted in PR3/PR4) and would couple this harness to code
not yet moved. The byte-equal invariant we need today is exactly
the output of the function being relocated, called over real
archived bars.

Usage:
    python -m tests.golden.record_session [--date YYYY-MM-DD] [--out PATH]

Re-run after the extraction with the same args; the JSONL must be
byte-equal. `tests/golden/verify.py` automates that comparison.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = REPO_ROOT.parent / "today_bars"


def _load_bars(path: pathlib.Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


def _ts_to_epoch(ts: str) -> int:
    # Bars use "...Z" (UTC) ISO-8601.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return int(_dt.datetime.fromisoformat(ts).timestamp())


def _resolve_compute_fn():
    """Import the compute function from whichever module owns it.

    Pre-extraction: only `_v5105_compute_5m_ohlc_and_ema9` exists in
    trade_genius.py. Post-extraction: `engine.bars.compute_5m_ohlc_and_ema9`
    is the canonical home and the trade_genius private alias stays as
    a deprecation shim. Either way, we exercise the single canonical
    implementation \u2014 the goal is byte-equal output across the move.
    """
    try:
        from engine.bars import compute_5m_ohlc_and_ema9  # type: ignore
        return compute_5m_ohlc_and_ema9, "engine.bars.compute_5m_ohlc_and_ema9"
    except Exception:
        pass
    # Pre-extraction fallback: import from the monolith. We avoid
    # importing trade_genius outright (it boots a bunch of side effects)
    # and instead re-load just the function via exec on the source.
    src = (REPO_ROOT / "trade_genius.py").read_text()
    marker = "def _v5105_compute_5m_ohlc_and_ema9("
    idx = src.find(marker)
    if idx < 0:
        raise RuntimeError("could not find _v5105_compute_5m_ohlc_and_ema9 in trade_genius.py")
    end = src.find("\n\n\ndef ", idx)
    if end < 0:
        end = src.find("\ndef ", idx + len(marker))
    snippet = src[idx:end]
    ns: dict = {}
    exec(compile(snippet, "<trade_genius_excerpt>", "exec"), ns)
    return ns["_v5105_compute_5m_ohlc_and_ema9"], "trade_genius._v5105_compute_5m_ohlc_and_ema9"


def _round_floats(obj):
    """Stable JSON: convert floats to repr that preserves bit-identical round-trip."""
    if isinstance(obj, float):
        return obj
    if isinstance(obj, list):
        return [_round_floats(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    return obj


def record(date_str: str, out_path: pathlib.Path) -> dict:
    archive_dir = ARCHIVE_ROOT / date_str
    if not archive_dir.is_dir():
        raise SystemExit(f"archive missing: {archive_dir}")
    tickers = sorted(p.stem for p in archive_dir.glob("*.jsonl"))
    if not tickers:
        raise SystemExit(f"no *.jsonl files in {archive_dir}")

    fn, fn_name = _resolve_compute_fn()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_records = 0
    n_nonempty = 0
    with out_path.open("w") as out:
        # Note: `fn_name` is deliberately omitted from _meta. The byte-equal
        # invariant covers the function's *output*, not its source location \u2014
        # the whole point of this PR is that the compute moves from
        # trade_genius into engine.bars.
        out.write(json.dumps({
            "_meta": {
                "harness_version": 1,
                "date": date_str,
                "tickers": tickers,
            }
        }, sort_keys=True, separators=(",", ":")) + "\n")

        for ticker in tickers:
            rows = _load_bars(archive_dir / f"{ticker}.jsonl")
            ts_list: list[int] = []
            opens: list[float] = []
            highs: list[float] = []
            lows: list[float] = []
            closes: list[float] = []
            for r in rows:
                try:
                    ts = _ts_to_epoch(r["ts"])
                except Exception:
                    continue
                o = r.get("open"); h = r.get("high"); lo = r.get("low"); c = r.get("close")
                if None in (o, h, lo, c):
                    continue
                ts_list.append(ts)
                opens.append(float(o))
                highs.append(float(h))
                lows.append(float(lo))
                closes.append(float(c))

                bars = {
                    "timestamps": list(ts_list),
                    "opens": list(opens),
                    "highs": list(highs),
                    "lows": list(lows),
                    "closes": list(closes),
                }
                result = fn(bars)
                rec = {
                    "ticker": ticker,
                    "ts": ts,
                    "n_bars_in": len(ts_list),
                    "result": _round_floats(result),
                }
                out.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
                n_records += 1
                if result is not None:
                    n_nonempty += 1

    return {
        "out": str(out_path),
        "tickers": len(tickers),
        "records": n_records,
        "nonempty_results": n_nonempty,
        "fn": fn_name,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default="2026-04-28",
                   help="YYYY-MM-DD subdir under today_bars/. "
                        "Note: 2026-04-27 was requested in the spec but is "
                        "not present in the sandbox; 2026-04-28 has a 6-min "
                        "gap which is documented in the PR description.")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)

    out = args.out or str(REPO_ROOT / "tests" / "golden" / f"v5_10_7_session_{args.date}.jsonl")
    summary = record(args.date, pathlib.Path(out))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
