#!/usr/bin/env bash
# tests/test_checks_lib.sh -- v5.8.1 unit tests for scripts/lib/checks.sh.
#
# Plain bash + manual asserts, no bats dependency. Run with:
#   bash tests/test_checks_lib.sh
# Exits 0 on all-pass, 1 on any failure. Each fixture covers at least one
# happy + sad path per check.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
FIX="${HERE}/fixtures/checks"

# shellcheck source=../scripts/lib/checks.sh
source "${REPO}/scripts/lib/checks.sh"

# Stub credentials so any real-curl branch would never silently dial out --
# all tests must hit fixture paths.
export RAILWAY_API_TOKEN="test-token"
export RAILWAY_PROJECT="test-project"
export RAILWAY_SERVICE="test-service"
export RAILWAY_ENVIRONMENT="test-env"
export DASHBOARD_URL="http://localhost:0"
export DASHBOARD_PASSWORD="test"

PASS=0
FAIL=0

assert_contains() {
    local needle="$1"
    local haystack="$2"
    local label="$3"
    if printf '%s' "${haystack}" | grep -qF -- "${needle}"; then
        PASS=$((PASS + 1))
        echo "  ok: ${label}"
    else
        FAIL=$((FAIL + 1))
        echo "  FAIL: ${label}"
        echo "       expected substring: ${needle}"
        echo "       got: ${haystack}"
    fi
}

assert_rc() {
    local want="$1"
    local got="$2"
    local label="$3"
    if [ "${want}" = "${got}" ]; then
        PASS=$((PASS + 1))
        echo "  ok: ${label} (rc=${got})"
    else
        FAIL=$((FAIL + 1))
        echo "  FAIL: ${label} (want rc=${want}, got rc=${got})"
    fi
}

# Reset cached deploy state between cases so each test gets a clean lookup.
reset_state() {
    _LATEST_DEPLOY_ID=""
    _LATEST_DEPLOY_STATUS=""
    _LATEST_DEPLOY_VERSION=""
    unset RAILWAY_DEPLOY_FIXTURE RAILWAY_LOGS_FIXTURE RAILWAY_SSH_FIXTURE DASHBOARD_STATE_FIXTURE
}

echo "[1] check_deploy_status (happy)"
reset_state
export RAILWAY_DEPLOY_FIXTURE="${FIX}/deploy_success.json"
out=$(check_deploy_status); rc=$?
assert_rc 0 "${rc}" "deploy SUCCESS returns 0"
assert_contains "DEPLOY SUCCESS 2a684474" "${out}" "echoes DEPLOY + 8-char id"
assert_contains "v5.8.1" "${out}" "echoes version"

echo "[2] check_deploy_status (sad)"
reset_state
export RAILWAY_DEPLOY_FIXTURE="${FIX}/deploy_failed.json"
out=$(check_deploy_status); rc=$?
assert_rc 1 "${rc}" "deploy FAILED returns 1"
assert_contains "DEPLOY FAILED" "${out}" "echoes FAILED status"

echo "[3] check_universe_loaded (happy)"
reset_state
export RAILWAY_DEPLOY_FIXTURE="${FIX}/deploy_success.json"
export RAILWAY_LOGS_FIXTURE="${FIX}/logs_healthy.json"
out=$(check_universe_loaded); rc=$?
assert_rc 0 "${rc}" "12 tickers >= 11 returns 0"
assert_contains "UNIVERSE 12 tickers:" "${out}" "echoes UNIVERSE 12"
assert_contains "AAPL" "${out}" "ticker list present"

echo "[4] check_universe_loaded (drift fallback)"
reset_state
export RAILWAY_DEPLOY_FIXTURE="${FIX}/deploy_success.json"
export RAILWAY_LOGS_FIXTURE="${FIX}/logs_with_errors.json"
out=$(check_universe_loaded); rc=$?
# logs_with_errors has 11 tickers in the DRIFT list -- exactly threshold.
assert_rc 0 "${rc}" "drift line with 11 tickers passes >=11 check"
assert_contains "UNIVERSE 11" "${out}" "drift parser counts tickers"

echo "[5] check_log_tags (happy)"
reset_state
export RAILWAY_DEPLOY_FIXTURE="${FIX}/deploy_success.json"
export RAILWAY_LOGS_FIXTURE="${FIX}/logs_healthy.json"
out=$(check_log_tags "STARTUP SUMMARY" "[UNIVERSE_GUARD]" "[V560-GATE]" "[V570-STRIKE]" "[V571-EXIT_PHASE]"); rc=$?
assert_rc 0 "${rc}" "all 5 tags present returns 0"
assert_contains "TAG [V560-GATE] 1" "${out}" "V560-GATE counted"
assert_contains "TAG [V571-EXIT_PHASE] 1" "${out}" "V571-EXIT_PHASE counted"
assert_contains "TAG STARTUP SUMMARY 1" "${out}" "STARTUP SUMMARY counted"

echo "[6] check_log_tags (missing tag)"
reset_state
export RAILWAY_DEPLOY_FIXTURE="${FIX}/deploy_success.json"
export RAILWAY_LOGS_FIXTURE="${FIX}/logs_healthy.json"
out=$(check_log_tags "[V999-MISSING]"); rc=$?
assert_rc 1 "${rc}" "missing tag returns 1"
assert_contains "TAG [V999-MISSING] 0" "${out}" "missing tag count=0"

echo "[7] check_no_errors (clean)"
reset_state
export RAILWAY_DEPLOY_FIXTURE="${FIX}/deploy_success.json"
export RAILWAY_LOGS_FIXTURE="${FIX}/logs_healthy.json"
out=$(check_no_errors); rc=$?
assert_rc 0 "${rc}" "clean logs return 0"
assert_contains "ERRORS coroutine=0 ws=0 traceback=0 error=0" "${out}" "all zero counts"

echo "[8] check_no_errors (errors present)"
reset_state
export RAILWAY_DEPLOY_FIXTURE="${FIX}/deploy_success.json"
export RAILWAY_LOGS_FIXTURE="${FIX}/logs_with_errors.json"
out=$(check_no_errors); rc=$?
assert_rc 1 "${rc}" "errors present returns 1"
assert_contains "coroutine=1" "${out}" "coroutine counted"
assert_contains "traceback=1" "${out}" "traceback counted"
assert_contains "ws=1" "${out}" "ws counted"
assert_contains "error=1" "${out}" "[ERROR] counted"

echo "[9] check_bar_archive_today (exists)"
reset_state
export RAILWAY_SSH_FIXTURE="${FIX}/ssh_bars_today.txt"
out=$(check_bar_archive_today 2>/dev/null); rc=$?
assert_rc 0 "${rc}" "dir exists returns 0"
assert_contains "BARS_TODAY exists=true" "${out}" "exists=true"
assert_contains "ticker_count=12" "${out}" "count parsed"
assert_contains "bytes=4823104" "${out}" "bytes parsed"

echo "[10] check_bar_archive_today (missing)"
reset_state
export RAILWAY_SSH_FIXTURE="${FIX}/ssh_bars_missing.txt"
out=$(check_bar_archive_today 2>/dev/null); rc=$?
assert_rc 1 "${rc}" "missing dir returns 1"
assert_contains "exists=false" "${out}" "exists=false"

echo "[11] check_shadow_db_count (informational)"
reset_state
export RAILWAY_SSH_FIXTURE="${FIX}/ssh_shadow_db.txt"
out=$(check_shadow_db_count); rc=$?
assert_rc 0 "${rc}" "always returns 0"
assert_contains "SHADOW_DB total=4128" "${out}" "total parsed"
assert_contains "GEMINI_A=22" "${out}" "breakdown contains GEMINI_A"

echo "[12] check_dashboard_state (live)"
reset_state
export DASHBOARD_STATE_FIXTURE="${FIX}/dashboard_live.json"
out=$(check_dashboard_state); rc=$?
assert_rc 0 "${rc}" "live status returns 0"
assert_contains "DASHBOARD shadow_data_status=live" "${out}" "echoes live"
assert_contains "version=5.8.0" "${out}" "version parsed"

echo "[13] check_dashboard_state (stale)"
reset_state
export DASHBOARD_STATE_FIXTURE="${FIX}/dashboard_stale.json"
out=$(check_dashboard_state); rc=$?
assert_rc 1 "${rc}" "stale status returns 1"
assert_contains "shadow_data_status=stale" "${out}" "echoes stale"

echo
echo "----"
TOTAL=$((PASS + FAIL))
if [ "${FAIL}" -eq 0 ]; then
    echo "checks_lib tests: ${PASS}/${TOTAL} PASS"
    exit 0
fi
echo "checks_lib tests: ${FAIL}/${TOTAL} FAILED"
exit 1
