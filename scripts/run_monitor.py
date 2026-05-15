"""scripts/run_monitor.py -- local monitor loop.

Replaces .github/workflows/monitor.yml. Tick cadence varies by market phase:
  - RTH (Mon-Fri 07:00-19:00 ET): every 5 min
  - Pre-market (Mon-Fri 06:00-07:00 ET): every 15 min
  - Off-hours / overnight: every 60 min

Reads credentials from .env.monitor in the repo root.

Usage:
    python scripts/run_monitor.py           # default (phase-aware cadence)
    python scripts/run_monitor.py --always  # run continuously (for testing)
    python scripts/run_monitor.py --once    # single run then exit

system_check_bot combines unified_monitor + dashboard_analysis + web UI checks
into a single pass (one login, all data fetched concurrently). Results land in
data/monitor/system_check_latest.json. Telegram alert on any CRIT check.
"""

from __future__ import annotations

import argparse
import json as _json
import logging
import os
import sys
import time
import urllib.request as _urllib
from datetime import datetime, timedelta, timezone
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
PRE_MARKET_START = 6  # 06:00 ET - pre-market monitoring begins
RTH_START = 7  # 07:00 ET
RTH_END = 19  # 19:00 ET (covers pre-market open through after-hours close)
RTH_INTERVAL = 300  # 5 min during RTH
PRE_MARKET_INTERVAL = 900  # 15 min during pre-market
OFF_HOURS_INTERVAL = 3600  # 60 min overnight / off-hours

# v9.1.75 -- Telegram dedup: track (section, name) → last-alerted timestamp.
# Same CRIT/WARN won't fire again within ALERT_DEDUP_SECS (30 min).
# Prevents repeated Telegram spam from the same invariant when old monitor
# instances with stale code accumulate at the 5-min tick boundaries.
_ALERT_DEDUP_SECS = 1800  # 30 minutes
_last_alerted: dict[tuple[str, str], float] = {}

_REPO_ROOT = Path(__file__).parent.parent
ENV_FILE = _REPO_ROOT / ".env.monitor"
MONITOR_DIR = _REPO_ROOT / "data" / "monitor"

# Railway log monitoring state
_railway_dep_id: str | None = None
_railway_log_cursor: float = 0.0  # Unix timestamp of last log line seen

_RAILWAY_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://railway.app",
    "Referer": "https://railway.app/",
}

# Patterns that indicate a real error in Railway logs (not just INFO traffic)
_RAILWAY_ERROR_PATTERNS = (
    "Traceback (most recent call last)",
    "CRITICAL",
    "FATAL",
)


def _railway_gql(query: str) -> dict:
    token = os.environ.get("RAILWAY_API_TOKEN", "").strip()
    if not token:
        return {}
    headers = {**_RAILWAY_HEADERS, "Authorization": f"Bearer {token}"}
    req = _urllib.Request(
        "https://backboard.railway.app/graphql/v2",
        data=_json.dumps({"query": query}).encode(),
        headers=headers,
    )
    resp = _urllib.urlopen(req, timeout=6)
    return _json.loads(resp.read())


def _get_railway_dep_id() -> str | None:
    global _railway_dep_id
    if _railway_dep_id:
        return _railway_dep_id
    svc_id = os.environ.get("RAILWAY_SERVICE_ID", "").strip()
    if not svc_id:
        return None
    try:
        d = _railway_gql('{deployments(input:{serviceId:"%s"}){edges{node{id status}}}}' % svc_id)
        edges = ((d.get("data") or {}).get("deployments") or {}).get("edges", [])
        if edges:
            _railway_dep_id = edges[0]["node"]["id"]
    except Exception:
        pass
    return _railway_dep_id


def _check_railway_logs() -> list[dict]:
    """Fetch recent Railway deployment logs and return CRIT checks for any errors found."""
    global _railway_log_cursor, _railway_dep_id

    if not os.environ.get("RAILWAY_API_TOKEN", "").strip():
        return []
    try:
        dep_id = _get_railway_dep_id()
        if not dep_id:
            return []

        d = _railway_gql(
            '{deploymentLogs(deploymentId:"%s",limit:40){message timestamp severity}}' % dep_id
        )
        logs = ((d.get("data") or {}).get("deploymentLogs")) or []
        if not logs:
            return []

        errors = []
        new_cursor = _railway_log_cursor

        for entry in logs:
            ts_str = entry.get("timestamp") or ""
            msg = (entry.get("message") or "").strip()
            sev = (entry.get("severity") or "").upper()

            ts_epoch = 0.0
            if ts_str:
                try:
                    ts_epoch = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass

            new_cursor = max(new_cursor, ts_epoch)

            # Skip logs we've already seen
            if ts_epoch <= _railway_log_cursor:
                continue

            is_error = sev in ("ERROR", "CRITICAL", "EMERGENCY", "ALERT") or any(
                p in msg for p in _RAILWAY_ERROR_PATTERNS
            )
            if is_error:
                errors.append(
                    {
                        "section": "RAILWAY",
                        "name": "log_error",
                        "status": "CRIT",
                        "detail": msg[:200],
                    }
                )

        _railway_log_cursor = new_cursor

        if errors:
            logger.warning("Railway log errors: %d new entries", len(errors))
        return errors

    except Exception as e:
        logger.debug("Railway log check skipped: %s", e)
        return []


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


def _market_phase() -> str:
    """Return 'rth', 'premarket', or 'offhours' for the current ET time."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return "offhours"
    if RTH_START <= now.hour < RTH_END:
        return "rth"
    if PRE_MARKET_START <= now.hour < RTH_START:
        return "premarket"
    return "offhours"


def _seconds_until_next_window() -> float:
    """Seconds until the next pre-market or RTH window (whichever comes first)."""
    now = datetime.now(ET)
    today_premarket = now.replace(hour=PRE_MARKET_START, minute=0, second=0, microsecond=0)
    if now < today_premarket and now.weekday() < 5:
        return (today_premarket - now).total_seconds()
    candidate = now + timedelta(days=1)
    candidate = candidate.replace(hour=PRE_MARKET_START, minute=0, second=0, microsecond=0)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return max(60.0, (candidate - now).total_seconds())


def _sleep_to_next_tick(interval: int) -> None:
    now = time.time()
    boundary = (int(now) // interval + 1) * interval
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

    # Inject Railway log errors as additional CRIT checks
    railway_errors = _check_railway_logs()
    if railway_errors:
        report = dict(report)
        report["checks"] = list(report.get("checks") or []) + railway_errors
        if report.get("overall") == "OK":
            report["overall"] = "CRIT"

    try:
        print_report(report)
    except Exception:
        pass

    try:
        save_report(report, MONITOR_DIR)
    except Exception as e:
        logger.warning("system_check_bot save failed: %s", e)

    try:
        # v9.1.75 -- dedup: suppress re-alerting on the same CRIT/WARN
        # within _ALERT_DEDUP_SECS (30 min) so stale old-code monitor
        # instances can't spam Telegram at every 5-min tick boundary.
        now_ts = time.time()
        new_checks = []
        for c in report.get("checks") or []:
            if c.get("status") not in ("CRIT", "WARN"):
                continue
            key = (c.get("section", ""), c.get("name", ""))
            last = _last_alerted.get(key, 0.0)
            if now_ts - last >= _ALERT_DEDUP_SECS:
                new_checks.append(c)
                _last_alerted[key] = now_ts
        if new_checks:
            # Build a filtered report with only the un-deduplicated checks.
            deduped = dict(report)
            deduped["checks"] = new_checks
            send_telegram_alert(deduped)
        else:
            suppressed = [
                f"{c.get('section')}.{c.get('name')}"
                for c in (report.get("checks") or [])
                if c.get("status") in ("CRIT", "WARN")
            ]
            if suppressed:
                logger.info("Telegram suppressed (dedup, already alerted): %s", suppressed)
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

    # Seed Railway log cursor to now so we only alert on errors that occur
    # after the monitor starts, not historical log lines.
    global _railway_log_cursor
    _railway_log_cursor = time.time()

    if args.once:
        sys.exit(_run_once())

    logger.info(
        "Monitor loop started \u2014 RTH=5min pre-market=15min off-hours=60min window=%s",
        "24/7" if args.always else "Mon-Fri %02d:00-%02d:00 ET" % (PRE_MARKET_START, RTH_END),
    )

    try:
        while True:
            phase = _market_phase() if not args.always else "rth"

            if phase == "offhours":
                secs = _seconds_until_next_window()
                eta = datetime.fromtimestamp(time.time() + secs, ET)
                logger.info(
                    "Off-hours \u2014 sleeping until %s ET (%.0f min)",
                    eta.strftime("%a %H:%M"),
                    secs / 60,
                )
                time.sleep(min(secs, OFF_HOURS_INTERVAL))
                continue

            interval = RTH_INTERVAL if phase == "rth" else PRE_MARKET_INTERVAL
            t0 = time.time()
            logger.info("=== tick %s ET [%s] ===", datetime.now(ET).strftime("%H:%M:%S"), phase)
            rc = _run_once()
            elapsed = time.time() - t0
            label = {0: "OK", 1: "WARN", 2: "CRIT"}.get(rc, f"ERR({rc})")
            logger.info("Tick done \u2014 rc=%d %s in %.1fs", rc, label, elapsed)

            _sleep_to_next_tick(interval)

    except KeyboardInterrupt:
        logger.info("Stopped (Ctrl-C)")


if __name__ == "__main__":
    main()
