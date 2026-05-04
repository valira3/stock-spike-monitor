"""Pre-market readiness check for TradeGenius (v6.11.1).

Usage (inside Railway container via ssh):
    python3 /app/scripts/premarket_check.py --in-container [--json]

Usage (local dev box -- stub only in Phase 1):
    python3 scripts/premarket_check.py --remote [--json]

Callable API (for Telegram /test integration):
    from scripts.premarket_check import run_all_checks, format_for_telegram
    result = run_all_checks(in_container=True, write_artifact=False)

Exit codes:
    0 -- PASS
    1 -- FAIL
    2 -- WARN
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# v6.11.4 \u2014 ensure /app is on sys.path so we can import bot_version,
# spy_regime, broker, etc. when invoked as `python3 /app/scripts/premarket_check.py`.
# Without this, sys.path[0] is /app/scripts/ and every cross-package import
# fails with ModuleNotFoundError. Idempotent: only inserts if /app exists
# and is not already on the path. Local-dev invocations (`python3
# scripts/premarket_check.py` from repo root) already have the repo root
# on sys.path so this is a no-op there.
_TG_APP_ROOT = "/app"
if os.path.isdir(_TG_APP_ROOT) and _TG_APP_ROOT not in sys.path:
    sys.path.insert(0, _TG_APP_ROOT)

# v6.11.7 \u2014 SSM_SMOKE_TEST guard MUST only fire on the CLI / cron path,
# never on the import path. v6.11.6 set the env var unconditionally at
# module import time, which polluted the live process: trade_genius.py
# imports telegram_commands early, telegram_commands imports
# scripts.premarket_check at top level (for /test integration), so the
# setdefault ran during prod boot and made trade_genius take the
# smoke-test branch \u2014 dashboard up, Telegram polling never started.
#
# When invoked as __main__ (railway ssh / cron), set the guard BEFORE
# any trade_genius import so we don't spawn a second bot in the same
# container. When imported (by telegram_commands), do not touch env.
# Idempotent: setdefault only sets if unset.
if __name__ == "__main__":
    os.environ.setdefault("SSM_SMOKE_TEST", "1")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_VERSION = "1"
BOT_VERSION_EXPECTED = "6.11.10"

# Minimum .jsonl files expected in yesterday's bar directory.
BAR_FILE_MIN = 5

# State DB path (mirrors persistence.py default).
_TG_DATA_ROOT = os.environ.get("TG_DATA_ROOT", "/data")
STATE_DB_PATH = os.environ.get("STATE_DB_PATH", _TG_DATA_ROOT + "/state.db")

# Expected tables in state.db (shadow_positions was dropped in v5.5.10 --
# use executor_positions as the live table to verify).
EXPECTED_TABLES = {"executor_positions", "fired_set", "session_state"}

# Alpaca endpoints.
_ALPACA_PAPER_URL = "https://paper-api.alpaca.markets/v2/account"
_ALPACA_LIVE_URL = "https://api.alpaca.markets/v2/account"
_ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/AAPL/trades/latest?feed=sip"
_ALPACA_CLOCK_URL = "https://api.alpaca.markets/v2/clock"

# Minimum free disk space thresholds.
# v6.11.6: Railway volumes for TradeGenius are 433MB total, so the original
# absolute 1GB warn threshold can never be satisfied. Use percentage-based
# thresholds instead, with absolute floors as a safety net for huge volumes.
_DISK_WARN_PCT_FREE = 15.0    # warn below 15% free
_DISK_FAIL_PCT_FREE = 5.0     # fail below 5% free
_DISK_FAIL_BYTES_FLOOR = 50_000_000   # 50 MB hard floor (any volume size)

# Maximum drift (seconds) before time-sync check warns / fails.
_TIME_DRIFT_WARN_S = 5
_TIME_DRIFT_FAIL_S = 30

# ---------------------------------------------------------------------------
# Result struct helpers
# ---------------------------------------------------------------------------

def _result(
    name: str,
    status: str,
    detail: str,
    elapsed_ms: int,
    data: dict | None = None,
) -> dict:
    """Build a canonical check-result dict."""
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "elapsed_ms": elapsed_ms,
        "data": data or {},
    }


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_process_alive() -> dict:
    """Check 1 -- bot_version import + state.db last-write recency."""
    t0 = time.monotonic()
    name = "process_alive"
    try:
        import bot_version as bv
        ver = str(bv.BOT_VERSION)
        db_path = Path(STATE_DB_PATH)
        if not db_path.exists():
            return _result(name, "FAIL", "state.db missing at %s" % db_path, _ms_since(t0),
                           {"bot_version": ver})
        age_s = time.time() - db_path.stat().st_mtime
        age_h = age_s / 3600.0
        if age_s > 86400:
            return _result(name, "FAIL",
                           "state.db last write %.1fh ago (>24h)" % age_h,
                           _ms_since(t0), {"bot_version": ver, "db_age_s": int(age_s)})
        status = "WARN" if age_s > 21600 else "PASS"
        detail = "state.db last write %.1fh ago -- bot_version=%s" % (age_h, ver)
        return _result(name, status, detail, _ms_since(t0),
                       {"bot_version": ver, "db_age_s": int(age_s)})
    except Exception as exc:
        return _result(name, "FAIL", "exception: %s: %s" % (type(exc).__name__, exc),
                       _ms_since(t0))


def check_version_parity() -> dict:
    """Check 2 -- bot_version.BOT_VERSION == trade_genius.BOT_VERSION."""
    t0 = time.monotonic()
    name = "version_parity"
    try:
        import bot_version as bv
        import trade_genius as tg
        bvv = str(bv.BOT_VERSION)
        tgv = str(tg.BOT_VERSION)
        if bvv != tgv:
            return _result(name, "FAIL",
                           "MISMATCH: bot_version=%s trade_genius=%s" % (bvv, tgv),
                           _ms_since(t0), {"bot_version_py": bvv, "trade_genius_py": tgv})
        return _result(name, "PASS", "both=%s" % bvv, _ms_since(t0),
                       {"version": bvv})
    except Exception as exc:
        return _result(name, "FAIL", "exception: %s: %s" % (type(exc).__name__, exc),
                       _ms_since(t0))


def check_module_imports() -> dict:
    """Check 3 -- import all critical modules in sequence."""
    t0 = time.monotonic()
    name = "module_imports"
    modules = [
        "bot_version",
        "trade_genius",
        "eye_of_tiger",
        "qqq_regime",
        "spy_regime",
        "broker.orders",
        "engine",
        "indicators",
        "volume_bucket",
        "volume_profile",
        "persistence",
        "paper_state",
        "bar_archive",
        "ingest",
        "ingest_config",
        "forensic_capture",
        "lifecycle_logger",
    ]
    failed = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception as exc:
            failed.append("%s (%s: %s)" % (mod, type(exc).__name__, str(exc)[:80]))
    if failed:
        return _result(name, "FAIL",
                       "import failures: %s" % "; ".join(failed),
                       _ms_since(t0), {"failed": failed, "total": len(modules)})
    return _result(name, "PASS",
                   "all %d modules imported OK" % len(modules),
                   _ms_since(t0), {"total": len(modules)})


def check_persistence_reachable() -> dict:
    """Check 4 -- open state.db and verify expected tables exist."""
    t0 = time.monotonic()
    name = "persistence_reachable"
    try:
        conn = sqlite3.connect(STATE_DB_PATH, timeout=5)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' LIMIT 20"
            ).fetchall()
            found = {r[0] for r in rows}
            missing = EXPECTED_TABLES - found
            count = conn.execute(
                "SELECT COUNT(*) FROM executor_positions"
            ).fetchone()[0]
        finally:
            conn.close()
        if missing:
            return _result(name, "FAIL",
                           "missing tables: %s" % sorted(missing),
                           _ms_since(t0),
                           {"found_tables": sorted(found), "missing": sorted(missing)})
        return _result(name, "PASS",
                       "tables OK; executor_positions rows=%d" % count,
                       _ms_since(t0),
                       {"found_tables": sorted(found), "executor_positions_count": count})
    except Exception as exc:
        return _result(name, "FAIL",
                       "sqlite error: %s: %s" % (type(exc).__name__, exc),
                       _ms_since(t0))


def check_bar_archive_yesterday() -> dict:
    """Check 5 -- verify last-trading-day bar directory exists and is non-empty."""
    t0 = time.monotonic()
    name = "bar_archive_yesterday"
    bars_base = os.environ.get("BAR_ARCHIVE_BASE",
                               _TG_DATA_ROOT + "/bars")
    base = Path(bars_base)
    # Scan last 4 UTC days to skip weekends / holidays.
    today_utc = datetime.datetime.utcnow().date()
    candidate_dir = None
    for offset in range(1, 5):
        d = today_utc - datetime.timedelta(days=offset)
        p = base / d.strftime("%Y-%m-%d")
        if p.is_dir():
            candidate_dir = p
            break
    if candidate_dir is None:
        return _result(name, "FAIL",
                       "no bar directory found in last 4 days under %s" % base,
                       _ms_since(t0), {"bars_base": str(base)})
    files = list(candidate_dir.glob("*.jsonl"))
    count = len(files)
    if count == 0:
        # Also try *.parquet just in case extension changes.
        files = list(candidate_dir.glob("*.parquet"))
        count = len(files)
    if count == 0:
        return _result(name, "FAIL",
                       "bar directory %s exists but is empty" % candidate_dir.name,
                       _ms_since(t0), {"dir": str(candidate_dir), "count": 0})
    status = "WARN" if count < BAR_FILE_MIN else "PASS"
    detail = "dir=%s files=%d%s" % (
        candidate_dir.name, count,
        (" (< expected %d)" % BAR_FILE_MIN) if status == "WARN" else "",
    )
    return _result(name, status, detail, _ms_since(t0),
                   {"dir": str(candidate_dir), "count": count,
                    "expected_min": BAR_FILE_MIN})


def _alpaca_account_request(url: str, key: str, secret: str, timeout: int = 8) -> tuple:
    """Make an Alpaca account GET request. Returns (status_code, parsed_json_or_None, err_str)."""
    req = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            try:
                data = json.loads(body)
            except Exception:
                data = None
            return resp.status, data, ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:120]
        except Exception:
            body = str(e)
        return e.code, None, body
    except Exception as exc:
        return 0, None, str(exc)[:120]


def check_alpaca_auth_paper() -> dict:
    """Check 6 -- verify paper Alpaca credentials work."""
    t0 = time.monotonic()
    name = "alpaca_auth_paper"
    key = os.environ.get("VAL_ALPACA_PAPER_KEY", "")
    secret = os.environ.get("VAL_ALPACA_PAPER_SECRET", "")
    if not key or not secret:
        return _result(name, "FAIL",
                       "VAL_ALPACA_PAPER_KEY or VAL_ALPACA_PAPER_SECRET not set",
                       _ms_since(t0))
    code, data, err = _alpaca_account_request(_ALPACA_PAPER_URL, key, secret)
    if code == 200 and isinstance(data, dict) and "account_number" in data:
        return _result(name, "PASS",
                       "account_number=%s" % data.get("account_number", "?"),
                       _ms_since(t0),
                       {"account_number": data.get("account_number"),
                        "status": data.get("status")})
    return _result(name, "FAIL",
                   "HTTP %d -- %s" % (code, err or "no account_number in response"),
                   _ms_since(t0), {"http_status": code})


def check_alpaca_auth_live() -> dict:
    """Check 7 -- verify live Alpaca credentials (SKIP if not configured)."""
    t0 = time.monotonic()
    name = "alpaca_auth_live"
    key = os.environ.get("VAL_ALPACA_LIVE_KEY", "")
    secret = os.environ.get("VAL_ALPACA_LIVE_SECRET", "")
    if not key or not secret:
        return _result(name, "SKIP",
                       "VAL_ALPACA_LIVE_KEY / _SECRET not set -- skipped",
                       _ms_since(t0))
    code, data, err = _alpaca_account_request(_ALPACA_LIVE_URL, key, secret)
    if code == 200 and isinstance(data, dict) and "account_number" in data:
        return _result(name, "PASS",
                       "account_number=%s" % data.get("account_number", "?"),
                       _ms_since(t0),
                       {"account_number": data.get("account_number"),
                        "status": data.get("status")})
    return _result(name, "FAIL",
                   "HTTP %d -- %s" % (code, err or "no account_number in response"),
                   _ms_since(t0), {"http_status": code})


def check_alpaca_data_feed_recent_trade() -> dict:
    """Check 8 -- verify AAPL latest trade timestamp is recent (soft fail)."""
    t0 = time.monotonic()
    name = "alpaca_data_feed_recent_trade"
    # Try paper key first, then live.
    key = os.environ.get("VAL_ALPACA_PAPER_KEY") or os.environ.get("VAL_ALPACA_LIVE_KEY", "")
    secret = (os.environ.get("VAL_ALPACA_PAPER_SECRET")
              or os.environ.get("VAL_ALPACA_LIVE_SECRET", ""))
    if not key or not secret:
        return _result(name, "SKIP",
                       "no Alpaca credentials available -- skipped",
                       _ms_since(t0))
    req = urllib.request.Request(
        _ALPACA_DATA_URL,
        headers={
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.status
            if code != 200:
                return _result(name, "FAIL",
                               "HTTP %d" % code, _ms_since(t0))
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _result(name, "FAIL",
                       "HTTP %d" % e.code, _ms_since(t0))
    except Exception as exc:
        return _result(name, "FAIL",
                       "connection error: %s" % str(exc)[:80],
                       _ms_since(t0))
    trade = data.get("trade") or {}
    ts_str = trade.get("t", "")
    if not ts_str:
        return _result(name, "WARN",
                       "no trade timestamp in response",
                       _ms_since(t0), {"response_keys": list(data.keys())})
    try:
        # Alpaca returns RFC3339 like "2026-05-03T20:00:00.123456789Z"
        ts_str_trimmed = ts_str[:26].rstrip("Z")
        if "." in ts_str_trimmed:
            ts_str_trimmed = ts_str_trimmed[:26]
        trade_dt = datetime.datetime.fromisoformat(ts_str_trimmed).replace(
            tzinfo=datetime.timezone.utc
        )
        now_utc = datetime.datetime.now(tz=datetime.timezone.utc)
        age_s = (now_utc - trade_dt).total_seconds()
        if age_s > 3600:
            return _result(name, "WARN",
                           "latest trade %.0fs ago (>1h)" % age_s,
                           _ms_since(t0), {"trade_ts": ts_str, "age_s": int(age_s)})
        return _result(name, "PASS",
                       "latest trade %.0fs ago" % age_s,
                       _ms_since(t0), {"trade_ts": ts_str, "age_s": int(age_s)})
    except Exception as exc:
        return _result(name, "WARN",
                       "could not parse trade timestamp %r: %s" % (ts_str[:40], exc),
                       _ms_since(t0))


def check_classifier_smoke() -> dict:
    """Check 9 -- SpyRegime + QQQRegime instantiation and synthetic tick."""
    t0 = time.monotonic()
    name = "classifier_smoke"
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        from spy_regime import SpyRegime
        from qqq_regime import QQQRegime

        ET = ZoneInfo("America/New_York")

        # --- SpyRegime smoke ---
        sr = SpyRegime()
        sr.daily_reset()
        assert sr.current_regime() is None, "expected None before ticks"
        assert not sr.is_regime_b(), "expected False before ticks"

        # Tick at 9:30 (open anchor).
        t930 = _dt.datetime(2026, 5, 4, 9, 30, 0, tzinfo=ET)
        sr.tick(t930, 500.0)
        assert sr.spy_open_930 == 500.0

        # Tick at 10:00 with a moderately-down price to land in regime B.
        # ret = (498.4 - 500.0) / 500.0 * 100 = -0.32 -- inside (-0.50, -0.15).
        t1000 = _dt.datetime(2026, 5, 4, 10, 0, 0, tzinfo=ET)
        sr.tick(t1000, 498.4)
        regime = sr.current_regime()
        if regime != "B":
            return _result(name, "FAIL",
                           "expected regime=B for ret=-0.32pct, got %r" % regime,
                           _ms_since(t0),
                           {"regime": regime, "spy_open": 500.0, "spy_close": 498.4})
        assert sr.is_regime_b(), "is_regime_b() should be True"

        # --- QQQRegime smoke ---
        qr = QQQRegime()
        # Feed 9 bars to seed EMA9 (period=9 requires 9 closes).
        for price in [400.0, 401.0, 402.0, 403.0, 404.0, 405.0, 406.0, 407.0, 408.0]:
            qr.update(price)
        compass = qr.current_compass()
        if compass is None:
            return _result(name, "FAIL",
                           "QQQRegime compass still None after 9 bars",
                           _ms_since(t0))

        return _result(name, "PASS",
                       "SpyRegime regime=%s is_regime_b=%s; QQQRegime compass=%s" % (
                           regime, sr.is_regime_b(), compass),
                       _ms_since(t0),
                       {"spy_regime": regime, "qqq_compass": compass})
    except Exception as exc:
        return _result(name, "FAIL",
                       "exception: %s: %s" % (type(exc).__name__, exc),
                       _ms_since(t0))


def check_sizing_helper_smoke() -> dict:
    """Check 10 -- _maybe_apply_regime_b_short_amp branch coverage smoke."""
    t0 = time.monotonic()
    name = "sizing_helper_smoke"
    try:
        import datetime as _dt
        import types
        from zoneinfo import ZoneInfo
        from broker.orders import _maybe_apply_regime_b_short_amp

        ET = ZoneInfo("America/New_York")

        def _cfg(is_long: bool):
            cfg = types.SimpleNamespace()
            cfg.side = types.SimpleNamespace(is_long=is_long)
            return cfg

        def _regime(b: bool):
            r = types.SimpleNamespace()
            r.is_regime_b = lambda: b
            return r

        arm = "10:00"
        disarm = "11:00"
        in_window = _dt.datetime(2026, 5, 4, 10, 30, 0, tzinfo=ET)
        pre_arm = _dt.datetime(2026, 5, 4, 9, 55, 0, tzinfo=ET)
        post_disarm = _dt.datetime(2026, 5, 4, 11, 0, 0, tzinfo=ET)

        errors = []

        # Branch 1: long side -- passthrough.
        r = _maybe_apply_regime_b_short_amp(
            cfg=_cfg(True), shares=10, ticker="AAPL", now_et=in_window,
            regime=_regime(True), scale=1.5, arm_hhmm_et=arm, disarm_hhmm_et=disarm,
        )
        if r != 10:
            errors.append("branch1(long) expected 10 got %d" % r)

        # Branch 2: short, non-regime-B -- passthrough.
        r = _maybe_apply_regime_b_short_amp(
            cfg=_cfg(False), shares=10, ticker="AAPL", now_et=in_window,
            regime=_regime(False), scale=1.5, arm_hhmm_et=arm, disarm_hhmm_et=disarm,
        )
        if r != 10:
            errors.append("branch2(non-B) expected 10 got %d" % r)

        # Branch 3: short, regime-B, before arm -- passthrough.
        r = _maybe_apply_regime_b_short_amp(
            cfg=_cfg(False), shares=10, ticker="AAPL", now_et=pre_arm,
            regime=_regime(True), scale=1.5, arm_hhmm_et=arm, disarm_hhmm_et=disarm,
        )
        if r != 10:
            errors.append("branch3(pre-arm) expected 10 got %d" % r)

        # Branch 4: short, regime-B, after disarm (10:00 == disarm exclusive) -- passthrough.
        r = _maybe_apply_regime_b_short_amp(
            cfg=_cfg(False), shares=10, ticker="AAPL", now_et=post_disarm,
            regime=_regime(True), scale=1.5, arm_hhmm_et=arm, disarm_hhmm_et=disarm,
        )
        if r != 10:
            errors.append("branch4(post-disarm) expected 10 got %d" % r)

        # Branch 5: short, regime-B, in window -- amplify.
        r = _maybe_apply_regime_b_short_amp(
            cfg=_cfg(False), shares=10, ticker="AAPL", now_et=in_window,
            regime=_regime(True), scale=1.5, arm_hhmm_et=arm, disarm_hhmm_et=disarm,
        )
        if r != 15:
            errors.append("branch5(amplify) expected 15 got %d" % r)

        # Branch 6: disabled via V611_REGIME_B_ENABLED -- passthrough.
        orig = os.environ.get("V611_REGIME_B_ENABLED")
        os.environ["V611_REGIME_B_ENABLED"] = "0"
        try:
            import importlib
            import eye_of_tiger as _eot
            importlib.reload(_eot)
            r = _maybe_apply_regime_b_short_amp(
                cfg=_cfg(False), shares=10, ticker="AAPL", now_et=in_window,
                regime=_regime(True), scale=1.5, arm_hhmm_et=arm,
                disarm_hhmm_et=disarm,
            )
            # The function re-imports eye_of_tiger at call time; reloading
            # sets V611_REGIME_B_ENABLED=False so it returns shares unchanged.
            if r != 10:
                errors.append("branch6(disabled) expected 10 got %d" % r)
        finally:
            if orig is None:
                os.environ.pop("V611_REGIME_B_ENABLED", None)
            else:
                os.environ["V611_REGIME_B_ENABLED"] = orig
            importlib.reload(_eot)

        if errors:
            return _result(name, "FAIL",
                           "branch errors: %s" % "; ".join(errors),
                           _ms_since(t0), {"errors": errors})
        return _result(name, "PASS",
                       "all 6 branches correct",
                       _ms_since(t0), {"branches_tested": 6})
    except Exception as exc:
        return _result(name, "FAIL",
                       "exception: %s: %s" % (type(exc).__name__, exc),
                       _ms_since(t0))


class _PreflightNoRedirect(urllib.request.HTTPRedirectHandler):
    """Suppress 302 redirect-following on dashboard /login (v6.11.2)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _extract_session_cookie(set_cookie_headers) -> str:
    """Pull `spike_session=<value>` out of Set-Cookie header values.

    Returns just `name=value` for use as a Cookie request header.
    Empty string if not found.
    """
    for raw in set_cookie_headers or []:
        first_pair = (raw.split(";", 1)[0] or "").strip()
        if first_pair.startswith("spike_session="):
            return first_pair
    return ""


def check_dashboard_state() -> dict:
    """Check 11 -- HTTP login + /api/state validation (soft fail).

    v6.11.2: forwards spike_session via an explicit Cookie header to
    bypass http.cookiejar Secure-flag stripping on plain-http loopback.
    """
    t0 = time.monotonic()
    name = "dashboard_state"
    pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not pw:
        return _result(name, "SKIP",
                       "DASHBOARD_PASSWORD not set -- skipped",
                       _ms_since(t0))
    port = int(os.environ.get("DASHBOARD_PORT", "8080") or "8080")
    base_url = "http://127.0.0.1:%d" % port
    try:
        import http.cookiejar as _cj
        import urllib.parse as _uparse
        cookie_jar = _cj.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar),
            _PreflightNoRedirect(),
        )
        login_data = _uparse.urlencode({"password": pw}).encode("utf-8")
        login_req = urllib.request.Request(
            base_url + "/login",
            data=login_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "TradeGenius-PreMarket/6.11.2",
                "Origin": base_url,
                "Referer": base_url + "/",
            },
        )
        login_code = 0
        set_cookie_hdrs = []  # type: list
        try:
            with opener.open(login_req, timeout=5) as r:
                login_code = r.status
                set_cookie_hdrs = r.headers.get_all("Set-Cookie") or []
        except urllib.request.HTTPError as http_exc:
            login_code = http_exc.code
            try:
                set_cookie_hdrs = http_exc.headers.get_all("Set-Cookie") or []
            except Exception:
                set_cookie_hdrs = []
        except Exception as exc:
            return _result(name, "WARN",
                           "login failed: %s" % str(exc)[:80],
                           _ms_since(t0))
        # 200 (legacy) and 302 (HTTPFound) both = success.
        if login_code >= 400 or login_code == 0:
            return _result(name, "WARN",
                           "login returned HTTP %d" % login_code,
                           _ms_since(t0))
        cookie_pair = _extract_session_cookie(set_cookie_hdrs)
        if not cookie_pair:
            return _result(name, "WARN",
                           "login ok but no spike_session cookie set",
                           _ms_since(t0))
        state_req = urllib.request.Request(
            base_url + "/api/state",
            headers={
                "User-Agent": "TradeGenius-PreMarket/6.11.2",
                "Cookie": cookie_pair,
            },
        )
        with opener.open(state_req, timeout=5) as r:
            if r.status != 200:
                return _result(name, "WARN",
                               "/api/state HTTP %d" % r.status,
                               _ms_since(t0))
            data = json.loads(r.read())
        if not isinstance(data, dict):
            return _result(name, "WARN",
                           "/api/state returned non-dict",
                           _ms_since(t0))
        # v6.11.4 \u2014 dashboard /api/state emits the field as `version`,
        # not `bot_version`. Reading the wrong key surfaced as
        # "bot_version='' (expected '6.11.3')" WARN in v6.11.3.
        bv = data.get("version", "")
        has_spy = "spy_regime_today" in data
        has_v611 = "v611_window" in data
        issues = []
        if bv != BOT_VERSION_EXPECTED:
            issues.append("version=%r (expected %r)" % (bv, BOT_VERSION_EXPECTED))
        if not has_spy:
            issues.append("spy_regime_today missing")
        if not has_v611:
            issues.append("v611_window missing")
        if issues:
            return _result(name, "WARN",
                           "; ".join(issues),
                           _ms_since(t0),
                           {"bot_version": bv, "has_spy_regime_today": has_spy,
                            "has_v611_window": has_v611})
        return _result(name, "PASS",
                       "version=%s spy_regime_today=yes v611_window=yes" % bv,
                       _ms_since(t0),
                       {"bot_version": bv})
    except Exception as exc:
        return _result(name, "WARN",
                       "exception: %s: %s" % (type(exc).__name__, exc),
                       _ms_since(t0))


def check_disk_space() -> dict:
    """Check 12 -- available disk space on /data."""
    t0 = time.monotonic()
    name = "disk_space"
    try:
        out = subprocess.check_output(
            ["df", "-B1", _TG_DATA_ROOT],
            stderr=subprocess.STDOUT,
            timeout=5,
        ).decode("utf-8", errors="replace")
        lines = out.strip().splitlines()
        if len(lines) < 2:
            return _result(name, "WARN",
                           "df output unexpected: %r" % out[:80],
                           _ms_since(t0))
        parts = lines[-1].split()
        # df -B1 output: Filesystem 1B-blocks Used Available Use% Mounted
        total_bytes = int(parts[1])
        free_bytes = int(parts[3])
        pct_free = (free_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0
        free_mb = free_bytes // 1_000_000
        total_mb = total_bytes // 1_000_000
        meta = {
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "pct_free": round(pct_free, 1),
        }
        if free_bytes < _DISK_FAIL_BYTES_FLOOR or pct_free < _DISK_FAIL_PCT_FREE:
            return _result(name, "FAIL",
                           "%dMB free of %dMB (%.1f%% < %.0f%% critical)" % (
                               free_mb, total_mb, pct_free, _DISK_FAIL_PCT_FREE),
                           _ms_since(t0), meta)
        if pct_free < _DISK_WARN_PCT_FREE:
            return _result(name, "WARN",
                           "%dMB free of %dMB (%.1f%% < %.0f%% warning)" % (
                               free_mb, total_mb, pct_free, _DISK_WARN_PCT_FREE),
                           _ms_since(t0), meta)
        return _result(name, "PASS",
                       "%dMB free of %dMB (%.1f%%)" % (free_mb, total_mb, pct_free),
                       _ms_since(t0), meta)
    except Exception as exc:
        return _result(name, "WARN",
                       "df error: %s: %s" % (type(exc).__name__, exc),
                       _ms_since(t0))


def check_time_sync() -> dict:
    """Check 13 -- compare local clock to Alpaca API Date header."""
    t0 = time.monotonic()
    name = "time_sync"
    # Read /proc/uptime for sanity.
    try:
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
    except Exception:
        uptime_s = -1.0
    key = os.environ.get("VAL_ALPACA_PAPER_KEY") or os.environ.get("VAL_ALPACA_LIVE_KEY", "")
    secret = (os.environ.get("VAL_ALPACA_PAPER_SECRET")
              or os.environ.get("VAL_ALPACA_LIVE_SECRET", ""))
    if not key or not secret:
        return _result(name, "SKIP",
                       "no Alpaca credentials -- skipped (uptime=%.0fs)" % uptime_s,
                       _ms_since(t0), {"uptime_s": int(uptime_s)})
    # v6.11.4 \u2014 Alpaca /v2/clock rejects HEAD with 405 Method Not Allowed.
    # Use GET; the response body is small (~80 bytes) and we only need the
    # Date header for the drift comparison.
    req = urllib.request.Request(
        _ALPACA_CLOCK_URL,
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        method="GET",
    )
    try:
        local_before = time.time()
        with urllib.request.urlopen(req, timeout=8) as resp:
            date_hdr = resp.headers.get("Date", "")
            local_after = time.time()
    except Exception as exc:
        return _result(name, "WARN",
                       "could not reach Alpaca clock: %s" % str(exc)[:80],
                       _ms_since(t0), {"uptime_s": int(uptime_s)})
    local_mid = (local_before + local_after) / 2.0
    try:
        import email.utils as _eu
        remote_epoch = _eu.parsedate_to_datetime(date_hdr).timestamp()
        drift_s = abs(local_mid - remote_epoch)
    except Exception as exc:
        return _result(name, "WARN",
                       "could not parse Date header %r: %s" % (date_hdr[:40], exc),
                       _ms_since(t0), {"uptime_s": int(uptime_s)})
    if drift_s > _TIME_DRIFT_FAIL_S:
        return _result(name, "FAIL",
                       "clock drift %.1fs (>30s)" % drift_s,
                       _ms_since(t0),
                       {"drift_s": round(drift_s, 2), "uptime_s": int(uptime_s)})
    if drift_s > _TIME_DRIFT_WARN_S:
        return _result(name, "WARN",
                       "clock drift %.1fs (>5s)" % drift_s,
                       _ms_since(t0),
                       {"drift_s": round(drift_s, 2), "uptime_s": int(uptime_s)})
    return _result(name, "PASS",
                   "drift=%.1fs uptime=%.0fs" % (drift_s, uptime_s),
                   _ms_since(t0),
                   {"drift_s": round(drift_s, 2), "uptime_s": int(uptime_s)})


def check_cron_introspection() -> dict:
    """Check 14 -- SKIP in-container (no Railway API token available there)."""
    return _result(
        "cron_introspection",
        "SKIP",
        "no Railway API token in-container -- deferred to remote mode",
        0,
    )


def check_replay_smoke() -> dict:
    """Check 15 -- deferred to Phase 2."""
    return _result(
        "replay_smoke",
        "SKIP",
        "phase 2 candidate -- not implemented in v6.11.1",
        0,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _aggregate_status(checks: list) -> str:
    """Return overall PASS/WARN/FAIL from a list of check dicts."""
    statuses = {c["status"] for c in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


# ---------------------------------------------------------------------------
# Public callable API
# ---------------------------------------------------------------------------

def run_all_checks(
    in_container: bool = True,
    write_artifact: bool = True,
) -> dict:
    """Run all pre-market checks and return the result dict.

    Parameters
    ----------
    in_container:
        True  -- assume we are inside the Railway container (cron / Telegram path).
        False -- remote stub (errors out with a clear message).
    write_artifact:
        If True and in_container, write result JSON to /data/preflight/<date>.json.
        Set False for on-demand Telegram invocations.
    """
    if not in_container:
        raise RuntimeError(
            "Remote mode not yet implemented in Phase 1. "
            "Use --in-container via railway ssh."
        )

    wall_t0 = time.monotonic()
    now_utc = datetime.datetime.now(tz=datetime.timezone.utc)
    ts_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    check_fns = [
        check_process_alive,
        check_version_parity,
        check_module_imports,
        check_persistence_reachable,
        check_bar_archive_yesterday,
        check_alpaca_auth_paper,
        check_alpaca_auth_live,
        check_alpaca_data_feed_recent_trade,
        check_classifier_smoke,
        check_sizing_helper_smoke,
        check_dashboard_state,
        check_disk_space,
        check_time_sync,
        check_cron_introspection,
        check_replay_smoke,
    ]

    # Hard checks (1-7): fail-fast -- if any FAIL, remaining hard checks
    # are skipped but soft checks still run.
    hard_fns = check_fns[:7]
    soft_fns = check_fns[7:]

    checks = []
    hard_failed = False
    fail_name = ""
    for fn in hard_fns:
        if hard_failed:
            checks.append(_result(
                fn.__name__.replace("check_", ""),
                "SKIP",
                "skipped -- prior hard failure in %s" % fail_name,
                0,
            ))
            continue
        try:
            res = fn()
        except Exception as exc:
            res = _result(
                fn.__name__.replace("check_", ""),
                "FAIL",
                "unexpected exception: %s: %s" % (type(exc).__name__, str(exc)[:120]),
                0,
            )
        checks.append(res)
        if res["status"] == "FAIL":
            hard_failed = True
            fail_name = res["name"]

    # Soft checks (8-15): always run, accumulate WARN.
    for fn in soft_fns:
        try:
            res = fn()
        except Exception as exc:
            res = _result(
                fn.__name__.replace("check_", ""),
                "FAIL",
                "unexpected exception: %s: %s" % (type(exc).__name__, str(exc)[:120]),
                0,
            )
        checks.append(res)

    elapsed_total_ms = int((time.monotonic() - wall_t0) * 1000)
    overall = _aggregate_status(checks)

    n_pass = sum(1 for c in checks if c["status"] == "PASS")
    n_warn = sum(1 for c in checks if c["status"] == "WARN")
    n_fail = sum(1 for c in checks if c["status"] == "FAIL")
    n_skip = sum(1 for c in checks if c["status"] == "SKIP")

    # Resolve bot_version from the first available source.
    bot_ver = "unknown"
    for c in checks:
        if c["name"] == "version_parity" and "version" in c.get("data", {}):
            bot_ver = c["data"]["version"]
            break
        if c["name"] == "process_alive" and "bot_version" in c.get("data", {}):
            bot_ver = c["data"]["bot_version"]

    result = {
        "version": SCRIPT_VERSION,
        "timestamp_utc": ts_str,
        "bot_version": bot_ver,
        "overall_status": overall,
        "n_pass": n_pass,
        "n_warn": n_warn,
        "n_fail": n_fail,
        "n_skip": n_skip,
        "elapsed_total_ms": elapsed_total_ms,
        "checks": checks,
    }

    if write_artifact and in_container:
        _write_artifact(result, now_utc)

    return result


def _write_artifact(result: dict, now_utc: datetime.datetime) -> None:
    """Write result JSON to /data/preflight/<UTC_date>.json."""
    out_dir = Path(_TG_DATA_ROOT) / "preflight"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / now_utc.strftime("%Y-%m-%d.json")
        out_path.write_text(json.dumps(result, indent=2))
    except Exception as exc:
        sys.stderr.write(
            "[premarket_check] WARNING: could not write artifact: %s\n" % exc
        )


# ---------------------------------------------------------------------------
# Telegram formatting helper
# ---------------------------------------------------------------------------

_MAX_TELEGRAM_CHARS = 3500


def format_for_telegram(result: dict) -> str:
    """Format a run_all_checks() result as a compact Telegram-ready string.

    Truncates non-failing checks to one summary line if total would exceed
    ~3500 chars. Always shows full detail for WARN and FAIL entries.
    """
    overall = result.get("overall_status", "?")
    ts = result.get("timestamp_utc", "")[:16].replace("T", " ")
    bv = result.get("bot_version", "?")
    n_pass = result.get("n_pass", 0)
    n_warn = result.get("n_warn", 0)
    n_fail = result.get("n_fail", 0)
    n_skip = result.get("n_skip", 0)
    elapsed_ms = result.get("elapsed_total_ms", 0)
    elapsed_s = elapsed_ms / 1000.0

    header = "Pre-market check (%s)\nv%s @ %s UTC\n\n" % (overall, bv, ts)
    footer = "\n%d PASS, %d WARN, %d FAIL, %d SKIP in %.1fs" % (
        n_pass, n_warn, n_fail, n_skip, elapsed_s
    )

    checks = result.get("checks", [])

    # First pass: build full lines.
    full_lines = []
    for c in checks:
        status = c.get("status", "?")
        name = c.get("name", "?")
        ms = c.get("elapsed_ms", 0)
        detail = c.get("detail", "")
        if detail:
            full_lines.append("[%s] %s (%dms): %s" % (status, name, ms, detail))
        else:
            full_lines.append("[%s] %s (%dms)" % (status, name, ms))

    body_full = "\n".join(full_lines)
    candidate = header + body_full + footer
    if len(candidate) <= _MAX_TELEGRAM_CHARS:
        return candidate

    # Second pass: compact PASS/SKIP to one char each; keep WARN/FAIL full.
    compact_lines = []
    for c in checks:
        status = c.get("status", "?")
        name = c.get("name", "?")
        ms = c.get("elapsed_ms", 0)
        detail = c.get("detail", "")
        if status in ("PASS", "SKIP"):
            compact_lines.append("[%s] %s (%dms)" % (status, name, ms))
        else:
            if detail:
                compact_lines.append("[%s] %s (%dms): %s" % (status, name, ms, detail))
            else:
                compact_lines.append("[%s] %s (%dms)" % (status, name, ms))

    body_compact = "\n".join(compact_lines)
    candidate = header + body_compact + footer
    if len(candidate) <= _MAX_TELEGRAM_CHARS:
        return candidate

    # Hard truncate with note.
    budget = _MAX_TELEGRAM_CHARS - len(header) - len(footer) - 40
    truncated = body_compact[:budget] + "\n...(truncated)"
    return header + truncated + footer


# ---------------------------------------------------------------------------
# CLI output helpers
# ---------------------------------------------------------------------------

def _human_output(result: dict) -> str:
    """Format result as human-readable text (no TTY / no ANSI)."""
    lines = []
    for c in result["checks"]:
        detail = (" -- " + c["detail"]) if c["detail"] else ""
        lines.append("[%s] %s (%dms)%s" % (
            c["status"], c["name"], c["elapsed_ms"], detail))
    overall = result["overall_status"]
    elapsed_s = result["elapsed_total_ms"] / 1000.0
    lines.append(
        "OVERALL: %s (%d pass, %d warn, %d fail, %d skip in %.1fs)" % (
            overall,
            result["n_pass"], result["n_warn"],
            result["n_fail"], result["n_skip"],
            elapsed_s,
        )
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="TradeGenius pre-market readiness check (v6.11.1)"
    )
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument(
        "--in-container",
        action="store_true",
        default=True,
        help="Run inside Railway container (default)",
    )
    grp.add_argument(
        "--remote",
        action="store_true",
        default=False,
        help="Remote mode (Phase 1 stub -- not implemented)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit JSON to stdout",
    )
    parser.add_argument(
        "--no-artifact",
        action="store_true",
        default=False,
        help="Skip writing /data/preflight/<date>.json",
    )
    args = parser.parse_args()

    in_container = not args.remote
    write_artifact = in_container and not args.no_artifact

    result = run_all_checks(
        in_container=in_container,
        write_artifact=write_artifact,
    )

    if args.json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        sys.stdout.write(_human_output(result) + "\n")

    overall = result["overall_status"]
    if overall == "FAIL":
        return 1
    if overall == "WARN":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
