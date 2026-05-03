"""Canonical sweep runner template \u2014 v6.9.3+.

Copy-paste this skeleton for every new Wave N sweep runner.
DO NOT modify the frozen v651 runner; use this as the reference pattern.

Pattern enforced here:
  1. build_sweep_env()   \u2014 hermetic env per isolate dir
  2. preflight_smoke()   \u2014 abort before fanning out if day 0 fails
  3. ProcessPoolExecutor \u2014 parallel day fan-out
  4. Empty-streak kill   \u2014 abort if >MAX_EMPTY_STREAK consecutive empty days

This template prevents the three silent failure modes that caused v6.9.0,
v6.9.1, and v6.9.2 Wave 2 sweeps to produce unusable results:

  v6.9.0 \u2014 cache slower than baseline (no smoke check)
  v6.9.1 \u2014 /data permission errors   (workers reported FAIL pnl=? silently)
  v6.9.2 \u2014 FMP_API_KEY hard-fail at import (empty raw JSON files)

Usage
-----
Copy this file, rename the sweep-specific constants, fill in the
``variant_configs`` list, and adjust ``BARS_DIR`` / ``OUTPUT_ROOT``.
Run with::

    python backtest/my_wave3_sweep.py \\
        --bars-dir /data/bars \\
        --output-root /data/sweep_out/wave3 \\
        --sample-date 2026-04-28

"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from backtest.sweep_env import build_sweep_env, preflight_smoke

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sweep configuration \u2014 edit these for each new wave
# ---------------------------------------------------------------------------

SWEEP_NAME = "wave_template"
MAX_WORKERS = 4
MAX_EMPTY_STREAK = 3  # abort if this many consecutive days return empty summary

# Each entry is a dict of extra env overrides applied per variant.
# Example entries for a stop-pct x cooldown grid:
VARIANT_CONFIGS: list[dict[str, str]] = [
    {"STOP_PCT": "0.010", "POST_LOSS_COOLDOWN_BARS": "3"},
    {"STOP_PCT": "0.015", "POST_LOSS_COOLDOWN_BARS": "3"},
    {"STOP_PCT": "0.020", "POST_LOSS_COOLDOWN_BARS": "5"},
]


# ---------------------------------------------------------------------------
# Per-day replay worker (runs in subprocess pool)
# ---------------------------------------------------------------------------

def _run_one_day(
    date_str: str,
    isolate_dir: Path,
    bars_dir: Path,
    tg_data_root: Path,
    extra: dict[str, str],
) -> dict[str, Any]:
    """Run one day replay; return parsed summary dict (or error stub)."""
    from backtest.sweep_env import build_sweep_env  # re-import in child
    import subprocess, json

    env = build_sweep_env(
        isolate_dir=isolate_dir,
        tg_data_root=tg_data_root,
        extra=extra,
    )
    output_path = isolate_dir / f"{date_str}.json"
    cmd = [
        "python", "-m", "backtest.replay_v511_full",
        "--date", date_str,
        "--bars-dir", str(bars_dir),
        "--output", str(output_path),
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not output_path.exists():
        return {"date": date_str, "error": True, "rc": result.returncode}
    try:
        data = json.loads(output_path.read_text())
        data["date"] = date_str
        return data
    except Exception as exc:
        return {"date": date_str, "error": True, "exc": str(exc)}


# ---------------------------------------------------------------------------
# Main sweep orchestrator
# ---------------------------------------------------------------------------

def run_sweep(
    *,
    bars_dir: Path,
    output_root: Path,
    sample_date: str,
    date_list: list[str],
) -> None:
    """Orchestrate multi-variant sweep with pre-flight smoke and empty-streak kill."""
    output_root.mkdir(parents=True, exist_ok=True)
    tg_data_root = bars_dir.parent  # convention: bars_dir is <root>/bars

    for variant_idx, extra in enumerate(VARIANT_CONFIGS):
        variant_name = f"v{variant_idx:03d}_" + "_".join(
            f"{k}={v}" for k, v in sorted(extra.items())
        )
        logger.info("=== Variant %s ===", variant_name)

        isolate_root = output_root / variant_name
        isolate_root.mkdir(parents=True, exist_ok=True)

        # Step 1: build hermetic env
        env = build_sweep_env(
            isolate_dir=isolate_root,
            tg_data_root=tg_data_root,
            extra=extra,
        )

        # Step 2: pre-flight smoke \u2014 ONE day before fanning out
        logger.info("Running pre-flight smoke check on %s ...", sample_date)
        preflight_smoke(
            workdir=isolate_root,
            bars_dir=bars_dir,
            sample_date=sample_date,
            env=env,
        )
        logger.info("Pre-flight smoke PASSED. Fanning out %d days.", len(date_list))

        # Step 3: fan out day grid with empty-streak kill switch
        results: list[dict[str, Any]] = []
        empty_streak = 0

        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _run_one_day,
                    date_str,
                    isolate_root / date_str,
                    bars_dir,
                    tg_data_root,
                    extra,
                ): date_str
                for date_str in date_list
            }

            for future in as_completed(futures):
                date_str = futures[future]
                try:
                    day_result = future.result()
                except Exception as exc:
                    day_result = {"date": date_str, "error": True, "exc": str(exc)}

                results.append(day_result)

                # Empty-streak tracking
                summary = day_result.get("summary") or {}
                entries = summary.get("entries", 0) or 0
                exits = summary.get("exits", 0) or 0
                is_empty = day_result.get("error") or (entries == 0 and exits == 0)

                if is_empty:
                    empty_streak += 1
                    logger.warning(
                        "Empty/failed result for %s (streak=%d/%d)",
                        date_str, empty_streak, MAX_EMPTY_STREAK,
                    )
                    if empty_streak > MAX_EMPTY_STREAK:
                        logger.error(
                            "Empty streak exceeded %d \u2014 aborting variant %s",
                            MAX_EMPTY_STREAK, variant_name,
                        )
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                else:
                    empty_streak = 0

        # Step 4: persist aggregated results
        out_file = output_root / f"{variant_name}_results.json"
        out_file.write_text(json.dumps(results, indent=2))
        logger.info("Variant %s done \u2014 wrote %s", variant_name, out_file)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"{SWEEP_NAME} sweep runner")
    p.add_argument("--bars-dir", required=True, type=Path)
    p.add_argument("--output-root", required=True, type=Path)
    p.add_argument("--sample-date", required=True,
                   help="YYYY-MM-DD for pre-flight smoke check")
    p.add_argument("--dates-file", type=Path, default=None,
                   help="newline-separated list of YYYY-MM-DD to replay")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.dates_file:
        date_list = [d.strip() for d in args.dates_file.read_text().splitlines() if d.strip()]
    else:
        date_list = [args.sample_date]

    run_sweep(
        bars_dir=args.bars_dir,
        output_root=args.output_root,
        sample_date=args.sample_date,
        date_list=date_list,
    )
