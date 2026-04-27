"""v5.5.0 \u2014 nightly backtest entry point.

Runs the offline replay (with --validate + --export-equity) for the most
recent completed trading day and writes a top-level
/data/backtest_reports/latest.json index that the dashboard's
/api/backtest_latest endpoint reads.

Wired in two places:
  1. As an in-process scheduler entry in trade_genius.py (preferred path)
     firing daily at 22:00 ET (post EOD reconciliation).
  2. As a standalone script: `python -m scripts.nightly_backtest`.

Always exits 0 \u2014 validation drift is surfaced via the dashboard, not
by Railway/scheduler restarts.
"""
from __future__ import annotations

import io
import json
import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


REPORTS_DIR = os.getenv("BACKTEST_REPORTS_DIR", "/data/backtest_reports")
BARS_DIR = os.getenv("BACKTEST_BARS_DIR", "/data/bars")
STATE_DB = os.getenv("BACKTEST_STATE_DB", "/data/state.db")


def _is_trading_day(d: date) -> bool:
    """Use volume_profile's NYSE calendar when importable; fall back to
    weekday-only. The fallback is only hit in stubbed test environments
    where heavy deps are unavailable.
    """
    try:
        import volume_profile as _vp
        return bool(_vp.is_trading_day(d))
    except Exception:
        return d.weekday() < 5


def most_recent_completed_trading_day(today: Optional[date] = None) -> str:
    """Return the most recently *completed* NYSE trading day as YYYY-MM-DD.

    Today itself is excluded \u2014 the nightly runs after market close so
    "completed" means the prior session.
    """
    cur = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    while not _is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur.isoformat()


def _build_per_config_summary(name: str, rows: list[dict],
                              vr: Optional[dict]) -> dict:
    """Produce one config's row for latest.json."""
    from backtest import ledger as _ledger
    s = _ledger.summarize(rows)
    out = {
        "match_rate": None,
        "replay_only_count": None,
        "prod_only_count": None,
        "total_pnl": s["total_pnl"],
        "trade_count": s["trades"],
        "win_rate": s["win_rate_pct"],
        "exit_code": 0,
    }
    if vr is not None:
        out["match_rate"] = vr["match_rate"]
        out["replay_only_count"] = len(vr["replay_only"])
        out["prod_only_count"] = len(vr["prod_only"])
        out["exit_code"] = 1 if vr["match_rate"] < 0.95 else 0
    return out


def run_nightly(
    day: Optional[str] = None,
    *,
    bars_dir: Optional[str] = None,
    state_db: Optional[str] = None,
    reports_dir: Optional[str] = None,
    validate: bool = True,
) -> dict:
    """Run the nightly backtest. Returns the latest.json payload that was
    written. Always succeeds (errors are captured into the log file).
    """
    bars_dir = bars_dir or BARS_DIR
    state_db = state_db or STATE_DB
    reports_dir = reports_dir or REPORTS_DIR
    rdir = Path(reports_dir)
    rdir.mkdir(parents=True, exist_ok=True)

    target_day = day or most_recent_completed_trading_day()
    log_path = rdir / f"{target_day}_log.txt"
    charts_path = rdir / f"{target_day}_charts.json"
    latest_path = rdir / "latest.json"

    log_buf = io.StringIO()
    configs_payload: dict[str, dict] = {}

    try:
        with redirect_stdout(log_buf), redirect_stderr(log_buf):
            print(
                f"[nightly_backtest] day={target_day} bars_dir={bars_dir} "
                f"state_db={state_db} out={reports_dir}"
            )
            from backtest import replay as _replay, loader as _loader

            cfgs = _replay._load_shadow_configs()
            rows_by_cfg: dict[str, list[dict]] = {}
            for cfg in cfgs:
                rows: list[dict] = []
                try:
                    rows = _replay.replay_one_day(bars_dir, target_day, cfg)
                except Exception as e:
                    print(f"[{cfg['name']}] replay error: {type(e).__name__}: {e}")
                rows_by_cfg[cfg["name"]] = rows
                from backtest import ledger as _ledger
                ledger_path = rdir / (
                    f"{cfg['name']}_{target_day}_{target_day}.csv"
                )
                _ledger.write_ledger(str(ledger_path), rows)
                s = _ledger.summarize(rows)
                print(
                    f"[{cfg['name']}] trades={s['trades']} "
                    f"pnl=${s['total_pnl']:+.2f} "
                    f"win_rate={s['win_rate_pct']:.2f}% -> {ledger_path}"
                )

                vr = None
                if validate:
                    try:
                        prod = _loader.load_prod_entries(
                            state_db, cfg["name"], target_day, target_day
                        )
                        vr = _replay.validate(rows, prod)
                        rep_path = rdir / (
                            f"{cfg['name']}_{target_day}_{target_day}_validation.md"
                        )
                        _replay.write_validation_report(
                            str(rep_path), cfg["name"],
                            target_day, target_day, vr,
                        )
                        print(
                            f"[{cfg['name']}] validate: "
                            f"match_rate={vr['match_rate']:.2%} "
                            f"matches={len(vr['matches'])} "
                            f"replay_only={len(vr['replay_only'])} "
                            f"prod_only={len(vr['prod_only'])}"
                        )
                    except Exception as e:
                        print(
                            f"[{cfg['name']}] validate error: "
                            f"{type(e).__name__}: {e}"
                        )

                configs_payload[cfg["name"]] = _build_per_config_summary(
                    cfg["name"], rows, vr,
                )

            # Dashboard-shape JSON for offline chart rendering.
            try:
                _replay.write_equity_export(str(charts_path), rows_by_cfg)
                print(f"[export-equity] -> {charts_path}")
            except Exception as e:
                print(f"[export-equity] error: {type(e).__name__}: {e}")
    except Exception:
        log_buf.write("\n[nightly_backtest] FATAL:\n")
        log_buf.write(traceback.format_exc())

    # Write the per-day log file regardless of outcome.
    try:
        log_path.write_text(log_buf.getvalue(), encoding="utf-8")
    except Exception:
        pass

    payload = {
        "as_of": target_day,
        "generated_at": datetime.now(timezone.utc)
                                .isoformat().replace("+00:00", "Z"),
        "configs": configs_payload,
    }
    try:
        latest_path.write_text(
            json.dumps(payload, indent=2), encoding="utf-8",
        )
    except Exception:
        pass
    return payload


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m scripts.nightly_backtest",
        description="v5.5.0 nightly backtest \u2014 always exits 0.",
    )
    p.add_argument("--day", default=None,
                   help="YYYY-MM-DD to replay (default: most recent completed)")
    p.add_argument("--bars-dir", default=None)
    p.add_argument("--state-db", default=None)
    p.add_argument("--reports-dir", default=None)
    p.add_argument("--no-validate", action="store_true")
    args = p.parse_args(argv)
    run_nightly(
        day=args.day,
        bars_dir=args.bars_dir,
        state_db=args.state_db,
        reports_dir=args.reports_dir,
        validate=not args.no_validate,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
