#!/usr/bin/env bash
# scripts/post_deploy_smoke.sh -- v5.8.1 Infra-B post-deploy verification.
#
# Runs the 7 checks from scripts/lib/checks.sh against the live Railway
# deploy and prints a PASS/FAIL summary. Exits 0 if all pass, 1 if any fail.
#
# Usage:
#   bash scripts/post_deploy_smoke.sh [expected_version]
#
# Required env vars (see scripts/lib/checks.sh for the full list):
#   RAILWAY_API_TOKEN, RAILWAY_PROJECT, RAILWAY_SERVICE, RAILWAY_ENVIRONMENT,
#   DASHBOARD_URL, DASHBOARD_PASSWORD.
#
# Failures are informational -- this script does NOT block automated merges.
# Run it after every release and post the output as a PR comment.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/checks.sh
source "${HERE}/lib/checks.sh"

EXPECTED_VERSION="${1:-}"

# Tag schema as of v5.8.0:
#   STARTUP SUMMARY -- bot startup banner
#   [UNIVERSE_GUARD] -- universe-drift guard (renamed from [UNIVERSE] in v5.8.0)
#   [V560-GATE]      -- entry-gate decisions
#   [V570-STRIKE]    -- Titan-strike entries
#   [V571-EXIT_PHASE] -- exit-reason classification (v5.7.1+)
EXPECTED_TAGS=(
    "STARTUP SUMMARY"
    "[UNIVERSE_GUARD]"
    "[V560-GATE]"
    "[V570-STRIKE]"
    "[V571-EXIT_PHASE]"
)

PASS=0
FAIL=0

run_check() {
    local label="$1"
    shift
    if "$@"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        echo "  ^ FAIL: ${label}"
    fi
}

echo "=== post-deploy smoke (expected v${EXPECTED_VERSION:-unspecified}) ==="
run_check "deploy_status"      check_deploy_status
run_check "universe_loaded"    check_universe_loaded
run_check "log_tags"           check_log_tags "${EXPECTED_TAGS[@]}"
run_check "no_errors"          check_no_errors
run_check "bar_archive_today"  check_bar_archive_today
run_check "shadow_db_count"    check_shadow_db_count
run_check "dashboard_state"    check_dashboard_state

TOTAL=$((PASS + FAIL))
echo "----"
if [ "${FAIL}" -eq 0 ]; then
    echo "POST-DEPLOY SMOKE PASS (${PASS}/${TOTAL})"
    exit 0
fi
echo "POST-DEPLOY SMOKE FAIL (${FAIL}/${TOTAL} failed)"
exit 1
