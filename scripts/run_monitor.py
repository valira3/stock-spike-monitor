"""scripts/run_monitor.py -- local RTH monitor loop.

Replaces .github/workflows/monitor.yml. Runs tools.system_check_bot every
5 min during US market hours (Mon-Fri, 07:00-19:00 ET). Reads credentials
from .env.monitor in the repo root (copy from .env.monitor.example).

Usage:
    python scripts/run_monitor.py           # RTH-gated (default)
    python scripts/run_monitor.py --always  # run continuously (for testing)
    python scripts/run_monitor.py --once    # single run then exit

system_check_bot combines unified_monitor + dashboard_analysis + web UI checks
into a single pass (one login, all data fetched concurrently). Results land in
data/monitor/system_check_latest.json. Telegram alert on any CRIT check.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure repo root is on sys.path so `tools.unified_monitor` is importable
# regardless of where the script is invoked from.
_REPO_ROOT_EARLY = Path(__file__).parent.parent
if str(_REPO_ROOT_EARLY) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_EARLY))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("run_monitor")

ET = ZoneInfo("America/New_York")
INTERVAL = 300  # 5 min
RTH_START = 7  # 07:00 ET
RTH_END = 19  # 19:00 ET (covers pre-market open through after-hours close)

_REPO_ROOT = Path(__file__).parent.parent
ENV_FILE = _REPO_ROOT / ".env.monitor"
MONITOR_DIR = _REPO_ROOT / "data" / "monitor"


def _load_env(path: Path) -> None:
    if not path.exists():
        logger.warning(
            "%s not found \u2014 create it from .env.monitor.example or set env vars manually",
            path.name,
        )
        return
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
            loaded += 1
    logger.info("Loaded %d var(s) from %s", loaded, path.name)


def _is_rth() -> bool:
    now = datetime.now(ET)
    return now.weekday() < 5 and RTH_START <= now.hour < RTH_END


def _seconds_until_rth() -> float:
    now = datetime.now(ET)
    today_open = now.replace(hour=RTH_START, minute=0, second=0, microsecond=0)
    if now < today_open and now.weekday() < 5:
        return (today_open - now).total_seconds()
    candidate = now + timedelta(days=1)
    candidate = candidate.replace(hour=RTH_START, minute=0, second=0, microsecond=0)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return max(60.0, (candidate - now).total_seconds())


def _sleep_to_next_tick() -> None:
    now = time.time()
    boundary = (int(now) // INTERVAL + 1) * INTERVAL
    sleep = max(5.0, boundary - now)
    logger.info("Next tick in %.0fs", sleep)
    time.sleep(sleep)


def _run_once() -> int:
    """Run system_check_bot: single login, SYSTEM + INVARIANTS + STRATEGY checks,
    web UI validation, Alpaca + Railway side-channel pulls. Saves to
    data/monitor/system_check_latest.json. Returns 0=OK, 1=WARN, 2=CRIT."""
    from tools.system_check_bot import run, print_report, save_report, send_telegram_alert

    url = os.environ.get("DASHBOARD_BASE_URL", "https://tradegenius.up.railway.app").rstrip("/")
    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()

    if not password:
        logger.error("DASHBOARD_PASSWORD not set in env or .env.monitor")
        return 99

    try:
        report = run(url, password)
    except Exception as e:
        logger.exception("system_check_bot raised: %s", e)
        return 99

    try:
        print_report(report)
    except Exception:
        pass

    try:
        save_report(report, MONITOR_DIR)
    except Exception as e:
        logger.warning("system_check_bot save failed: %s", e)

    try:
        send_telegram_alert(report)
    except Exception as e:
        logger.warning("system_check_bot alert failed: %s", e)

    overall = report.get("overall", "CRIT")
    return 0 if overall == "OK" else (1 if overall == "WARN" else 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--always", action="store_true", help="Run 24/7, bypass RTH gate")
    parser.add_argument(
        "--once", action="store_true", help="Run once and exit (useful for ad-hoc checks)"
    )
    args = parser.parse_args()

    _load_env(ENV_FILE)
    MONITOR_DIR.mkdir(parents=True, exist_ok=True)

    if args.once:
        sys.exit(_run_once())

    logger.info(
        "Monitor loop started \u2014 interval=5min window=%s",
        "24/7" if args.always else "Mon-Fri %02d:00-%02d:00 ET" % (RTH_START, RTH_END),
    )

    try:
        while True:
            if not args.always and not _is_rth():
                secs = _seconds_until_rth()
                eta = datetime.fromtimestamp(time.time() + secs, ET)
                logger.info(
                    "Outside RTH \u2014 sleeping until %s ET (%.0f min)",
                    eta.strftime("%a %H:%M"),
                    secs / 60,
                )
                time.sleep(min(secs, 3600))
                continue

            t0 = time.time()
            logger.info("=== tick %s ET ===", datetime.now(ET).strftime("%H:%M:%S"))
            rc = _run_once()
            elapsed = time.time() - t0
            label = {0: "OK", 1: "WARN", 2: "CRIT"}.get(rc, f"ERR({rc})")
            logger.info("Tick done \u2014 rc=%d %s in %.1fs", rc, label, elapsed)

            _sleep_to_next_tick()

    except KeyboardInterrupt:
        logger.info("Stopped (Ctrl-C)")


if __name__ == "__main__":
    main()
