#!/usr/bin/env bash
# Poll the sweep-results branch and emit one status line per state
# change. Designed to run inside Claude Code's Monitor tool so each
# stdout line becomes a chat notification.
#
# Usage:
#   poll_sweep_progress.sh <expected_count> [run_id_or_trigger_name]
#
#   expected_count : how many variants the workflow run should produce
#                    (matches the length of the variants JSON array).
#   run_id_or_trigger_name :
#                    Optional. Either a numeric GitHub Actions run id,
#                    or a trigger-file basename (e.g. "batch_a_r2") for
#                    auto-triggered runs. If omitted, the script
#                    auto-detects the latest run id present in the
#                    sweep-results branch.
#
# Exits with the final variant table once <expected_count> summaries
# have been pushed, or after a timeout.
#
# Layouts handled:
#   sweeps/run-<id>/<vid>/summary.json       (workflow_dispatch)
#   sweeps/<trigger>/run-<id>/<vid>/summary.json  (auto-trigger)
set -uo pipefail

EXPECTED=${1:-1}
EXPLICIT=${2:-}
TIMEOUT_MIN=${POLL_TIMEOUT_MIN:-90}
POLL_SEC=${POLL_INTERVAL_SEC:-45}

START_TS=$(date -u +%s)
PREV_DONE=-1
PREV_PREFIX=""

cd "$(dirname "$0")/.."

# Build a path prefix from the explicit arg if provided. Without it, we
# pick the most recently-touched leaf path in sweep-results that
# matches either layout.
build_prefix() {
  if [ -n "$EXPLICIT" ]; then
    if [[ "$EXPLICIT" =~ ^[0-9]+$ ]]; then
      echo "sweeps/run-${EXPLICIT}"
    else
      LATEST_RUN=$(git ls-tree -r origin/sweep-results --name-only 2>/dev/null \
        | grep -oE "sweeps/${EXPLICIT}/run-[0-9]+" | sort -u | tail -1 || true)
      if [ -n "$LATEST_RUN" ]; then
        echo "$LATEST_RUN"
      else
        echo "sweeps/${EXPLICIT}"  # might not exist yet; will retry
      fi
    fi
  else
    # Latest run-id across both layouts.
    git ls-tree -r origin/sweep-results --name-only 2>/dev/null \
      | grep -oE "sweeps/(run-[0-9]+|[a-zA-Z0-9_]+/run-[0-9]+)" \
      | sort -u | tail -1 || true
  fi
}

while true; do
  ELAPSED_SEC=$(( $(date -u +%s) - START_TS ))
  ELAPSED_MIN=$(( ELAPSED_SEC / 60 ))
  if [ "$ELAPSED_MIN" -ge "$TIMEOUT_MIN" ]; then
    echo "TIMEOUT after ${TIMEOUT_MIN}min"
    exit 1
  fi

  git fetch origin sweep-results 2>/dev/null

  if ! git rev-parse --verify origin/sweep-results >/dev/null 2>&1; then
    if [ "$ELAPSED_MIN" -lt 1 ] || [ $((ELAPSED_SEC % 120)) -lt "$POLL_SEC" ]; then
      echo "$(date -u +%H:%M:%SZ) sweep-results branch not yet pushed (${ELAPSED_MIN}min)"
    fi
    sleep "$POLL_SEC"
    continue
  fi

  PREFIX=$(build_prefix)
  if [ -z "$PREFIX" ]; then
    sleep "$POLL_SEC"; continue
  fi

  if [ "$PREFIX" != "$PREV_PREFIX" ]; then
    echo "$(date -u +%H:%M:%SZ) tracking ${PREFIX} (expecting ${EXPECTED} variants)"
    PREV_PREFIX="$PREFIX"
    PREV_DONE=-1
  fi

  DONE=$(git ls-tree -r origin/sweep-results --name-only 2>/dev/null \
    | grep "^${PREFIX}/" | grep "summary.json$" | wc -l | tr -d ' ')

  if [ "$DONE" != "$PREV_DONE" ]; then
    NAMES=$(git ls-tree -r origin/sweep-results --name-only 2>/dev/null \
      | grep "^${PREFIX}/" | grep "summary.json$" \
      | sed -E "s|^${PREFIX}/(.+)/summary.json|\1|" | sort | paste -sd ',')
    echo "$(date -u +%H:%M:%SZ) ${PREFIX}: ${DONE}/${EXPECTED} done [${NAMES}] (${ELAPSED_MIN}min)"
    PREV_DONE="$DONE"
  fi

  if [ "$DONE" -ge "$EXPECTED" ]; then
    echo ""
    echo "=== ALL ${EXPECTED} VARIANTS COMPLETE FOR ${PREFIX} ==="
    printf "%-32s | %12s | %8s | %8s | %6s\n" "variant" "net_pnl" "entries" "wr_pct" "wall"
    printf "%s\n" "----------------------------------------------------------------------------------"
    git ls-tree -r origin/sweep-results --name-only 2>/dev/null \
      | grep "^${PREFIX}/" | grep "summary.json$" | sort \
      | while read path; do
          git show "origin/sweep-results:${path}" 2>/dev/null \
            | jq -r '[.variant, (.net_pnl|tostring), (.entries|tostring),
                     ((.win_rate_pct // 0)|tostring), ((.wall_min // 0)|tostring)]
                    | @tsv' \
            | awk -F'\t' '{printf "%-32s | %12s | %8s | %8s | %6s\n", $1, $2, $3, $4, $5}'
        done
    exit 0
  fi

  sleep "$POLL_SEC"
done
