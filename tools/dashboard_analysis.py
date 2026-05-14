"""tools/dashboard_analysis.py \u2014 live dashboard quality analysis.

Fetches /api/state + /api/executor/val + /api/indices and runs a structured
audit of the bot's live state against the Keystone strategy expectations.
Designed to run every 5 min from scripts/run_monitor.py (alongside
unified_monitor) and as a standalone diagnostic:

    python tools/dashboard_analysis.py [--url URL] [--password PW]
    python tools/dashboard_analysis.py --trading   # extra RTH trade correlation
    python tools/dashboard_analysis.py --json       # machine-readable output

During RTH the analysis correlates live positions and trade history against
v10 ORB + r17 EOD strategy expectations (entry windows, risk sizing, stop
placement, cooldown wiring) and flags deviations. Outside RTH it runs a
lighter config sanity check.

Exit codes: 0 = all green, 1 = warnings present, 2 = critical issues.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("dashboard_analysis")

# Strategy constants (Keystone baseline \u2014 matches CLAUDE.md)
KEYSTONE = {
    "or_minutes": 30,
    "rr": 2.5,
    "risk_per_trade_pct": 1.0,
    "max_trades_per_day": 5,
    "max_concurrent_risk_dollars": 2000.0,
    "daily_loss_kill_pct": 2.0,
    "skip_vix_above": 22.0,
    "skip_gap_above_pct": 1.5,
    "atr_stop_mult": 1.75,
    "partial_profit_at_1r": True,
    "max_vwap_dev_bps": 25.0,
    # RTH entry window: 09:30-11:00 ET (minutes past midnight)
    "entry_open_min": 570,  # 09:30
    "entry_close_min": 660,  # 11:00
    # EOD window: 15:00-15:59 ET
    "eod_entry_min": 900,  # 15:00
    "eod_exit_min": 959,  # 15:59
    # Starting capital for risk sizing
    "account": 100_000.0,
}

GREEN = "OK"
WARN = "WARN"
CRIT = "CRIT"
INFO = "INFO"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _fetch_all(base_url: str, password: str | None) -> dict:
    """Fetch /api/state, /api/executor/val, /api/indices concurrently."""
    results: dict = {}

    # Build a shared cookie jar so we only login once.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    if password:
        try:
            login_data = urllib.parse.urlencode({"password": password}).encode()
            opener.open(base_url + "/login", login_data, timeout=10)
        except Exception as e:
            logger.warning("Login failed: %s", e)
            return results

    def _get(path: str, key: str) -> None:
        try:
            with opener.open(base_url + path, timeout=10) as r:
                results[key] = json.loads(r.read())
        except Exception as e:
            logger.debug("%s failed: %s", path, e)

    threads = [
        threading.Thread(target=_get, args=("/api/state", "state")),
        threading.Thread(target=_get, args=("/api/executor/val", "val")),
        threading.Thread(target=_get, args=("/api/indices", "indices")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)

    return results


# ---------------------------------------------------------------------------
# Analysis checks
# ---------------------------------------------------------------------------


class Check:
    __slots__ = ("name", "status", "detail", "value")

    def __init__(self, name: str, status: str, detail: str, value=None):
        self.name = name
        self.status = status
        self.detail = detail
        self.value = value

    def __repr__(self) -> str:
        return f"Check({self.name!r}, {self.status}, {self.detail!r})"


def _check_version(state: dict, expected: str | None) -> Check:
    ver = state.get("version", "?")
    if expected and ver != expected:
        return Check("version", WARN, f"live={ver} expected={expected}")
    return Check("version", GREEN, f"v{ver}")


def _check_v10_config(state: dict) -> list[Check]:
    checks = []
    v10 = state.get("v10") or {}
    cfg = v10.get("config") or {}
    if not cfg:
        return [Check("v10_config", WARN, "v10 block missing from /api/state")]

    for field, expected in KEYSTONE.items():
        if field.endswith("_min") or field == "account":
            continue  # timing constants not in API config
        if field not in cfg:
            continue
        live = cfg[field]
        if isinstance(expected, bool):
            ok = bool(live) == expected
        elif isinstance(expected, float):
            ok = abs(float(live) - expected) < 1e-6
        else:
            ok = live == expected

        if not ok:
            checks.append(Check(f"config.{field}", WARN, f"live={live!r} expected={expected!r}"))

    if not checks:
        checks.append(Check("v10_config", GREEN, f"{len(cfg)} fields match Keystone"))
    return checks


def _check_vix(state: dict, indices: dict | None) -> Check:
    v10 = state.get("v10") or {}
    ds = v10.get("day_status") or {}
    thr = float(ds.get("vix_threshold") or (v10.get("config") or {}).get("skip_vix_above") or 22.0)

    # Prefer prior-day close (gate value), fall back to current (display)
    vix = ds.get("vix_d1_close")
    vix_src = "prior_day"
    if vix is None:
        vix = ds.get("vix_current")
        vix_src = "current"
    if vix is None and indices:
        # Try Yahoo ^VIX from indices payload
        for row in indices.get("indices") or []:
            if row.get("symbol") == "^VIX" and row.get("available") and row.get("last") is not None:
                vix = row["last"]
                vix_src = "yahoo_live"
                break

    if vix is None:
        return Check("vix", WARN, "VIX unavailable from all sources (Alpaca + Yahoo)")

    vix = float(vix)
    if vix > thr:
        return Check(
            "vix",
            CRIT,
            f"VIX {vix:.2f} > threshold {thr:.0f} ({vix_src}) \u2014 block_day expected",
        )
    return Check("vix", GREEN, f"VIX {vix:.2f}/{thr:.0f} ({vix_src}) \u2014 gate clear")


def _check_eod_config(state: dict) -> list[Check]:
    checks = []
    eod = (state.get("v10") or {}).get("eod") or {}
    if not eod:
        return [Check("eod", WARN, "EOD block absent from v10 snapshot")]

    cfg = eod.get("config") or {}
    enabled = eod.get("enabled", False)
    fire_broker = cfg.get("fire_broker", False)

    checks.append(
        Check(
            "eod_enabled",
            GREEN if enabled else WARN,
            "enabled" if enabled else "disabled (ORB_EOD_REVERSAL_ENABLED=0)",
        )
    )
    checks.append(
        Check(
            "eod_fire_mode",
            INFO if fire_broker else WARN,
            f"{'LIVE ORDERS' if fire_broker else 'paper-observe'} (ORB_EOD_FIRE_BROKER={'1' if fire_broker else '0'})",
        )
    )

    # Verify entry/exit window matches production expectation
    entry_et = cfg.get("entry_et", "?")
    exit_et = cfg.get("exit_et", "?")
    if entry_et != "15:00":
        checks.append(
            Check(
                "eod_entry_window",
                WARN,
                f"entry_et={entry_et!r} expected '15:00' (production default since v9.1.2)",
            )
        )
    else:
        checks.append(Check("eod_window", GREEN, f"window {entry_et}–{exit_et} ET"))

    # Fence check
    long_tkrs = set(cfg.get("long_tickers") or [])
    short_tkrs = set(cfg.get("short_tickers") or [])
    exp_long = {"ORCL", "AAPL", "MSFT", "AVGO"}
    exp_short = {"ORCL", "NFLX", "AAPL", "MSFT"}
    if long_tkrs != exp_long:
        checks.append(
            Check("eod_long_fence", WARN, f"long={sorted(long_tkrs)} expected={sorted(exp_long)}")
        )
    if short_tkrs != exp_short:
        checks.append(
            Check(
                "eod_short_fence", WARN, f"short={sorted(short_tkrs)} expected={sorted(exp_short)}"
            )
        )
    if long_tkrs == exp_long and short_tkrs == exp_short:
        checks.append(
            Check(
                "eod_fence", GREEN, "r17 fence: ORCL/AAPL/MSFT/AVGO long; ORCL/NFLX/AAPL/MSFT short"
            )
        )

    return checks


def _check_risk_books(state: dict) -> list[Check]:
    checks = []
    v10 = state.get("v10") or {}
    rbs = v10.get("risk_books") or {}
    if not rbs:
        return [Check("risk_books", WARN, "no risk_books in v10 snapshot")]

    for pid, rb in rbs.items():
        eq = rb.get("equity", 0)
        open_risk = rb.get("open_risk", 0)
        max_risk = rb.get("max_risk_dollars", 2000)
        util = rb.get("utilization_pct", 0)
        killed = rb.get("daily_kill_triggered", False)
        pnl = rb.get("realized_pnl_today", 0)

        if killed:
            checks.append(
                Check(
                    f"risk.{pid}",
                    CRIT,
                    f"daily kill triggered \u2014 pnl=${pnl:.0f}, open_risk=${open_risk:.0f}",
                )
            )
        elif util > 90:
            checks.append(
                Check(
                    f"risk.{pid}",
                    WARN,
                    f"risk util {util:.0f}% \u2014 ${open_risk:.0f}/{max_risk:.0f} used",
                )
            )
        else:
            checks.append(
                Check(
                    f"risk.{pid}",
                    GREEN,
                    f"eq=${eq:,.0f} pnl=${pnl:+.0f} util={util:.0f}% open_risk=${open_risk:.0f}",
                )
            )

    return checks


def _check_positions(state: dict, val_state: dict | None) -> list[Check]:
    checks = []
    positions = state.get("positions") or []
    now_et = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute

    for p in positions:
        ticker = p.get("ticker", "?")
        side = p.get("side", "?")
        entry = float(p.get("entry") or 0)
        stop = float(p.get("stop") or 0)
        shares = float(p.get("shares") or 0)

        # Risk-per-trade sanity: |entry - stop| * shares should be ~1% of $100k = ~$1000
        risk_dollars = abs(entry - stop) * shares if entry and stop and shares else None
        if risk_dollars is not None:
            expected_risk = KEYSTONE["account"] * KEYSTONE["risk_per_trade_pct"] / 100
            if risk_dollars > expected_risk * 2.5:
                checks.append(
                    Check(
                        f"pos.{ticker}.risk",
                        CRIT,
                        f"{side} ${risk_dollars:.0f} risk >> expected ${expected_risk:.0f} (2.5× breach)",
                    )
                )
            elif risk_dollars > expected_risk * 1.5:
                checks.append(
                    Check(
                        f"pos.{ticker}.risk",
                        WARN,
                        f"{side} ${risk_dollars:.0f} risk > expected ${expected_risk:.0f}",
                    )
                )

        # Entry-time check: should be 09:30-11:00 or 15:00-15:59 ET
        entry_time_raw = p.get("entry_time") or p.get("time") or ""
        if entry_time_raw:
            try:
                h, m = (int(x) for x in str(entry_time_raw).split(":")[:2])
                entry_min = h * 60 + m
                in_orb = KEYSTONE["entry_open_min"] <= entry_min <= KEYSTONE["entry_close_min"]
                in_eod = KEYSTONE["eod_entry_min"] <= entry_min <= KEYSTONE["eod_exit_min"]
                if not in_orb and not in_eod:
                    checks.append(
                        Check(
                            f"pos.{ticker}.window",
                            WARN,
                            f"{side} entered {entry_time_raw} ET \u2014 outside ORB (09:30-11:00) and EOD (15:00-15:59) windows",
                        )
                    )
            except (ValueError, TypeError):
                pass

    if not checks:
        n = len(positions)
        checks.append(
            Check(
                "positions",
                GREEN,
                f"{n} open position{'s' if n != 1 else ''}"
                + (" \u2014 all within risk/window bounds" if n else ""),
            )
        )

    # Val vs Main divergence
    if val_state:
        val_positions = val_state.get("positions") or []
        main_tickers = {p.get("ticker") for p in positions}
        val_tickers = {p.get("ticker") for p in val_positions}
        val_only = val_tickers - main_tickers
        main_only = main_tickers - val_tickers
        if val_only:
            checks.append(
                Check(
                    "pos.val_diverge",
                    WARN,
                    f"Val has positions Main doesn't: {sorted(val_only)} \u2014 Val-only positions won't get exit signal from bus",
                )
            )
        if main_only:
            checks.append(
                Check(
                    "pos.main_diverge",
                    INFO,
                    f"Main has positions Val doesn't: {sorted(main_only)} \u2014 expected if Val rejected on risk cap",
                )
            )

    return checks


def _check_trade_correlation(state: dict) -> list[Check]:
    """During RTH: check that today's trades match strategy expectations."""
    checks = []
    trades = state.get("trades_today") or []
    if not trades:
        return [Check("trades", INFO, "no trades today yet")]

    entries = [t for t in trades if t.get("action") in ("BUY", "SHORT", "SELL_SHORT")]
    closes = [t for t in trades if t.get("action") in ("SELL", "COVER", "BUY_TO_COVER")]
    pnl_sum = sum(float(t.get("pnl") or 0) for t in closes)
    wins = sum(1 for t in closes if float(t.get("pnl") or 0) > 0)
    losses = len(closes) - wins

    # Time-window check for entries
    outside_window = []
    orb_entries, eod_entries = 0, 0
    for t in entries:
        raw = t.get("time") or t.get("entry_time") or ""
        try:
            parts = str(raw).replace(" ET", "").split(":")
            h, m = int(parts[0]), int(parts[1])
            em = h * 60 + m
            in_orb = KEYSTONE["entry_open_min"] <= em <= KEYSTONE["entry_close_min"]
            in_eod = KEYSTONE["eod_entry_min"] <= em <= KEYSTONE["eod_exit_min"]
            if in_orb:
                orb_entries += 1
            elif in_eod:
                eod_entries += 1
            else:
                outside_window.append(f"{t.get('ticker')}@{raw}")
        except (ValueError, TypeError, IndexError):
            pass

    checks.append(
        Check(
            "trades.summary",
            GREEN,
            f"{len(entries)} entries ({orb_entries} ORB + {eod_entries} EOD), "
            f"{len(closes)} closes (W{wins}/L{losses}) "
            f"P&L=${pnl_sum:+.2f}",
        )
    )

    if outside_window:
        checks.append(
            Check(
                "trades.window",
                WARN,
                f"entries outside ORB/EOD window: {', '.join(outside_window)}",
            )
        )

    if len(entries) > KEYSTONE["max_trades_per_day"]:
        checks.append(
            Check(
                "trades.count",
                WARN,
                f"{len(entries)} entries today > max {KEYSTONE['max_trades_per_day']}/day cap "
                f"(multi-portfolio = expected if ORB_PORTFOLIO_FIRE=1)",
            )
        )

    # Win rate sanity
    if len(closes) >= 3:
        wr = wins / len(closes) * 100
        # Keystone backtest ~52% win rate; extreme deviation is a signal
        if wr < 20:
            checks.append(
                Check(
                    "trades.win_rate",
                    WARN,
                    f"win rate {wr:.0f}% on {len(closes)} closes \u2014 below typical 40-60% range",
                )
            )
        elif wr > 90:
            checks.append(
                Check(
                    "trades.win_rate",
                    WARN,
                    f"win rate {wr:.0f}% on {len(closes)} closes \u2014 suspiciously high, verify data",
                )
            )

    return checks


def _check_cooldowns(state: dict) -> list[Check]:
    """Verify cooldown tracking is working."""
    by_pid = state.get("active_cooldowns_by_portfolio") or {}
    if not by_pid:
        # Missing key altogether \u2014 backend might be old version
        return [Check("cooldowns", WARN, "active_cooldowns_by_portfolio key missing from API")]

    total = sum(len(v) for v in by_pid.values())
    details = []
    for pid, cds in by_pid.items():
        if cds:
            for c in cds:
                rem = int((c.get("remaining_sec") or 0) / 60)
                details.append(f"{pid}:{c.get('ticker')}({c.get('side')}) {rem}min left")

    if total == 0:
        return [Check("cooldowns", GREEN, "0 active cooldowns across all portfolios")]

    return [Check("cooldowns", INFO, f"{total} active: " + ", ".join(details))]


def _check_executors(state: dict, val_state: dict | None) -> list[Check]:
    checks = []
    exec_status = state.get("executors_status") or {}
    val_en = (exec_status.get("val") or {}).get("enabled", False)
    gene_en = (exec_status.get("gene") or {}).get("enabled", False)

    checks.append(
        Check("executor.val", GREEN if val_en else WARN, "enabled paper" if val_en else "disabled")
    )
    checks.append(
        Check(
            "executor.gene",
            INFO if not gene_en else GREEN,
            "disabled (ALPACA_SKIP_PORTFOLIOS=gene)" if not gene_en else "enabled",
        )
    )

    # Val account health
    if val_state and val_en:
        account = val_state.get("account") or {}
        eq = account.get("equity", 0)
        day_pnl = account.get("day_pnl", 0)
        healthy = val_state.get("healthy", False)
        err = val_state.get("error")
        if err:
            checks.append(Check("executor.val.health", CRIT, f"error: {err}"))
        elif not healthy:
            checks.append(Check("executor.val.health", WARN, "healthy=False"))
        else:
            checks.append(
                Check("executor.val.equity", GREEN, f"${eq:,.2f} (day P&L ${day_pnl:+.2f})")
            )

    return checks


def _check_ingest(state: dict) -> Check:
    ingest = state.get("ingest_status") or {}
    ws = ingest.get("ws_state", "?")
    status = ingest.get("status", "?")
    gaps = ingest.get("open_gaps_today", 0)
    bars = ingest.get("bars_today", 0)
    is_rth_ingest = ingest.get("ingest_health", {}).get("is_rth", False)

    # Outside RTH, CONNECTING/unconfigured is expected
    if not is_rth_ingest:
        return Check("ingest", GREEN, f"off-hours (ws={ws} bars_today={bars})")

    if gaps > 0:
        return Check("ingest", WARN, f"ws={ws} open_gaps={gaps} bars={bars}")
    if ws not in ("CONNECTED", "STREAMING", "OK"):
        return Check("ingest", WARN, f"ws={ws} status={status}")
    return Check("ingest", GREEN, f"ws={ws} bars={bars} gaps={gaps}")


def _check_session_state(state: dict) -> Check:
    v10 = state.get("v10") or {}
    session_date = v10.get("session_date", "")
    bootstrapped = v10.get("bootstrapped", False)
    live_mode = v10.get("live_mode", False)
    now_et = datetime.now(ET)
    is_rth = 9 <= now_et.hour < 16 and now_et.weekday() < 5

    if not bootstrapped:
        return Check("session", WARN, "ORB engine not bootstrapped")
    if not live_mode:
        return Check(
            "session", CRIT, "ORB_LIVE_MODE=0 \u2014 LEGACY fallback active, v10 not trading"
        )
    if is_rth and not session_date:
        return Check(
            "session",
            WARN,
            "RTH but session_date empty \u2014 session reset not yet fired (expect at 09:25 ET)",
        )

    return Check(
        "session",
        GREEN,
        f"bootstrapped, live_mode=ON, session_date={session_date or '(off-hours)'}",
    )


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def run_analysis(
    base_url: str,
    password: str | None,
    expected_version: str | None = None,
    deep: bool = False,
) -> dict:
    """Run the full analysis. Returns a structured report dict."""
    t0 = time.monotonic()
    now_et = datetime.now(ET)
    is_rth = 9 <= now_et.hour < 16 and now_et.weekday() < 5

    raw = _fetch_all(base_url, password)
    state = raw.get("state") or {}
    val_state = raw.get("val") or {}
    indices = raw.get("indices") or {}

    fetch_elapsed = time.monotonic() - t0

    if not state or not state.get("ok"):
        return {
            "ok": False,
            "error": "failed to fetch /api/state",
            "fetch_s": round(fetch_elapsed, 2),
            "ts_et": now_et.isoformat(),
        }

    # Run all checks
    checks: list[Check] = []
    if expected_version:
        checks.append(_check_version(state, expected_version))

    checks.append(_check_session_state(state))
    checks.extend(_check_v10_config(state))
    checks.append(_check_vix(state, indices))
    checks.extend(_check_eod_config(state))
    checks.extend(_check_risk_books(state))
    checks.extend(_check_positions(state, val_state if val_state else None))
    checks.extend(_check_cooldowns(state))
    checks.extend(_check_executors(state, val_state if val_state else None))
    checks.append(_check_ingest(state))

    if deep or is_rth:
        checks.extend(_check_trade_correlation(state))

    # Summary
    crits = [c for c in checks if c.status == CRIT]
    warns = [c for c in checks if c.status == WARN]

    overall = GREEN if not crits and not warns else (WARN if not crits else CRIT)

    elapsed = time.monotonic() - t0
    return {
        "ok": True,
        "overall": overall,
        "ts_et": now_et.strftime("%Y-%m-%dT%H:%M:%S ET"),
        "version": state.get("version", "?"),
        "is_rth": is_rth,
        "fetch_s": round(fetch_elapsed, 2),
        "analysis_s": round(elapsed, 2),
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        "summary": {
            "total": len(checks),
            "green": sum(1 for c in checks if c.status == GREEN),
            "warn": len(warns),
            "crit": len(crits),
            "info": sum(1 for c in checks if c.status == INFO),
        },
        "crits": [c.name for c in crits],
        "warns": [c.name for c in warns],
    }


def print_report(report: dict) -> None:
    """Print a human-readable report to stdout."""
    overall = report.get("overall", "?")
    icon = {"OK": "[OK]", "WARN": "[WARN]", "CRIT": "[CRIT]"}.get(overall, "[?]")
    print(
        f"\n=== Dashboard Analysis {icon} ==="
        f"  {report.get('ts_et', '')}  v{report.get('version', '?')}"
    )

    status_icon = {GREEN: "+", WARN: "!", CRIT: "X", INFO: "."}

    for c in report.get("checks", []):
        s = c["status"]
        print(f"  {status_icon.get(s, '?')}  [{s:<4}]  {c['name']:<35}  {c['detail']}")

    sm = report.get("summary", {})
    print(
        f"\n  {sm.get('green', 0)} green · {sm.get('warn', 0)} warn ·"
        f" {sm.get('crit', 0)} crit · {sm.get('info', 0)} info"
        f"  ({report.get('analysis_s', 0):.1f}s)"
    )


def send_alert_on_crit(report: dict, token: str, chat_id: str) -> None:
    """Send Telegram alert when CRIT checks are present."""
    crits = report.get("crits", [])
    if not crits:
        return
    warns = report.get("warns", [])
    ver = report.get("version", "?")
    msg = (
        f"DASHBOARD ANALYSIS CRIT\n"
        f"Version: v{ver}\n"
        f"Time: {report.get('ts_et', '')}\n"
        f"\nCRITICAL ({len(crits)}):\n"
        + "\n".join(
            "  " + c["name"] + ": " + c["detail"]
            for c in report.get("checks", [])
            if c["status"] == CRIT
        )
        + (
            f"\n\nWarnings ({len(warns)}):\n"
            + "\n".join(
                "  " + c["name"] + ": " + c["detail"]
                for c in report.get("checks", [])
                if c["status"] == WARN
            )
            if warns
            else ""
        )
    )
    payload = json.dumps({"chat_id": chat_id, "text": msg}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            logger.info("Telegram alert sent (HTTP %d)", r.status)
    except Exception as e:
        logger.warning("Telegram alert error: %s", e)


# ---------------------------------------------------------------------------
# Save to disk (for monitor integration)
# ---------------------------------------------------------------------------


def save_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    latest = out_dir / "dashboard_analysis_latest.json"
    archive = out_dir / f"dashboard_analysis_{ts}.json"
    text = json.dumps(report, indent=2)
    latest.write_text(text, encoding="utf-8")
    archive.write_text(text, encoding="utf-8")
    return latest


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get(
            "DASHBOARD_BASE_URL",
            os.environ.get("DASHBOARD_URL", "https://tradegenius.up.railway.app"),
        ),
    )
    parser.add_argument("--password", default=os.environ.get("DASHBOARD_PASSWORD"))
    parser.add_argument("--version", default=None, help="Expected BOT_VERSION")
    parser.add_argument(
        "--trading",
        action="store_true",
        help="Force deep trade correlation (default: auto during RTH)",
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument(
        "--alert",
        action="store_true",
        help="Send Telegram alert on CRIT (uses TELEGRAM_TP_TOKEN/CHAT_ID env)",
    )
    parser.add_argument("--save", metavar="DIR", default=None, help="Save report JSON to DIR")
    args = parser.parse_args()

    report = run_analysis(
        args.url.rstrip("/"),
        args.password,
        expected_version=args.version,
        deep=args.trading,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        if not report.get("ok"):
            print(f"ERROR: {report.get('error')}")
            return 2
        print_report(report)

    if args.save:
        out = save_report(report, Path(args.save))
        if not args.json:
            print(f"\n  Saved: {out}")

    if args.alert:
        tok = os.environ.get("TELEGRAM_TP_TOKEN", "")
        cid = os.environ.get("TELEGRAM_TP_CHAT_ID", "")
        if tok and cid:
            send_alert_on_crit(report, tok, cid)

    overall = report.get("overall", CRIT)
    if overall == CRIT:
        return 2
    if overall == WARN:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
