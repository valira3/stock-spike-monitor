#!/usr/bin/env bash
# scripts/lib/checks.sh -- v5.8.1 Infra-B post-deploy smoke library.
#
# Pure bash functions, sourceable. Each prints a structured stdout line and
# returns 0 on PASS / 1 on FAIL. Both `scripts/post_deploy_smoke.sh` and the
# weekday/Saturday crons source this file as the single source of truth for
# "what does a healthy TradeGenius deploy look like?"
#
# Required env vars (callers set once at top):
#   RAILWAY_API_TOKEN     Railway personal API token (NOT RAILWAY_TOKEN).
#   RAILWAY_PROJECT       Railway project UUID.
#   RAILWAY_SERVICE       Railway service UUID.
#   RAILWAY_ENVIRONMENT   Railway environment UUID.
#   DASHBOARD_URL         e.g. https://tradegenius.up.railway.app
#   DASHBOARD_PASSWORD    Login password for /login form.
#
# Optional fixture overrides (used by tests/test_checks_lib.sh):
#   RAILWAY_LOGS_FIXTURE       Path to a JSON fixture; bypasses real curl in
#                              _railway_logs_json.
#   RAILWAY_DEPLOY_FIXTURE     Path to a deployments-query JSON fixture.
#   RAILWAY_SSH_FIXTURE        Path to a stdout fixture; bypasses real
#                              `railway ssh` in _railway_ssh.
#   DASHBOARD_STATE_FIXTURE    Path to /api/state JSON fixture.
#
# Bash quoting note:
#   Python helpers receive their input via the LOGS_JSON / DATA_JSON env var
#   rather than stdin, because bash cannot combine a `<<HEREDOC` (script body)
#   and a `<<<` (data on stdin) on the same `python3 -` invocation -- the
#   second redirect silently overrides the first.

set -o pipefail

# ----- internal helpers ------------------------------------------------------

_RAILWAY_GQL_URL="https://backboard.railway.com/graphql/v2"

_warn() {
    echo "WARN: $*" >&2
}

# Build a JSON {"query":..., "variables":...} payload safely with python3.
_build_gql_payload() {
    local query="$1"
    local variables="${2:-{}}"
    GQL_QUERY="${query}" GQL_VARS="${variables}" python3 -c '
import json, os
print(json.dumps({
    "query": os.environ["GQL_QUERY"],
    "variables": json.loads(os.environ["GQL_VARS"]),
}))
'
}

# POST a GraphQL query/variables JSON to Railway. Echoes raw response body.
# Honors RAILWAY_DEPLOY_FIXTURE when set.
_railway_gql() {
    local query="$1"
    local variables="${2:-{}}"
    if [ -n "${RAILWAY_DEPLOY_FIXTURE:-}" ] && [ -f "${RAILWAY_DEPLOY_FIXTURE}" ]; then
        cat "${RAILWAY_DEPLOY_FIXTURE}"
        return 0
    fi
    local payload
    payload=$(_build_gql_payload "${query}" "${variables}")
    curl -sS -X POST "${_RAILWAY_GQL_URL}" \
        -H "Authorization: Bearer ${RAILWAY_API_TOKEN}" \
        -H "Content-Type: application/json" \
        --data "${payload}"
}

# Fetch up to <limit> log lines for a deployment. Echoes a JSON array of
# {message, timestamp, ...} objects. Honors RAILWAY_LOGS_FIXTURE.
_railway_logs_json() {
    local deployment_id="$1"
    local limit="${2:-200}"
    if [ -n "${RAILWAY_LOGS_FIXTURE:-}" ] && [ -f "${RAILWAY_LOGS_FIXTURE}" ]; then
        cat "${RAILWAY_LOGS_FIXTURE}"
        return 0
    fi
    local query='query($deploymentId: String!, $limit: Int!) { deploymentLogs(deploymentId: $deploymentId, limit: $limit) { message timestamp severity } }'
    local vars
    vars=$(DEP_ID="${deployment_id}" LIMIT="${limit}" python3 -c '
import json, os
print(json.dumps({"deploymentId": os.environ["DEP_ID"], "limit": int(os.environ["LIMIT"])}))
')
    _railway_gql "${query}" "${vars}"
}

# Run a shell command via `railway ssh`. Echoes stdout. Honors RAILWAY_SSH_FIXTURE.
_railway_ssh() {
    local cmd="$1"
    if [ -n "${RAILWAY_SSH_FIXTURE:-}" ] && [ -f "${RAILWAY_SSH_FIXTURE}" ]; then
        cat "${RAILWAY_SSH_FIXTURE}"
        return 0
    fi
    RAILWAY_API_TOKEN="${RAILWAY_API_TOKEN}" railway ssh \
        --project "${RAILWAY_PROJECT}" \
        --environment "${RAILWAY_ENVIRONMENT}" \
        --service "${RAILWAY_SERVICE}" \
        "${cmd}"
}

# Resolve the latest deployment id + status + version, caching results in globals.
_LATEST_DEPLOY_ID=""
_LATEST_DEPLOY_STATUS=""
_LATEST_DEPLOY_VERSION=""
_resolve_latest_deploy() {
    if [ -n "${_LATEST_DEPLOY_ID}" ]; then
        return 0
    fi
    local query='query($projectId: String!, $envId: String!, $serviceId: String!) { deployments(first: 1, input: {projectId: $projectId, environmentId: $envId, serviceId: $serviceId}) { edges { node { id status meta } } } }'
    local vars
    vars=$(P="${RAILWAY_PROJECT}" E="${RAILWAY_ENVIRONMENT}" S="${RAILWAY_SERVICE}" python3 -c '
import json, os
print(json.dumps({"projectId": os.environ["P"], "envId": os.environ["E"], "serviceId": os.environ["S"]}))
')
    local resp
    resp=$(_railway_gql "${query}" "${vars}")
    local parsed
    parsed=$(DATA_JSON="${resp}" python3 -c '
import json, os
raw = os.environ.get("DATA_JSON", "")
try:
    data = json.loads(raw)
except Exception:
    print("|UNKNOWN|")
    raise SystemExit(0)
edges = (((data or {}).get("data") or {}).get("deployments") or {}).get("edges") or []
if not edges:
    print("|NONE|")
    raise SystemExit(0)
node = edges[0].get("node") or {}
did = node.get("id") or ""
status = node.get("status") or "UNKNOWN"
meta = node.get("meta") or {}
version = ""
for k in ("commitMessage", "version", "branch"):
    v = meta.get(k)
    if isinstance(v, str) and v:
        version = v
        break
# 3-field pipe-delimited record. Fields cannot contain | in practice (status
# is an enum, id is a UUID, version is a short string).
print(f"{did}|{status}|{version}")
')
    _LATEST_DEPLOY_ID="${parsed%%|*}"
    local rest="${parsed#*|}"
    _LATEST_DEPLOY_STATUS="${rest%%|*}"
    _LATEST_DEPLOY_VERSION="${rest#*|}"
}

# ----- public check functions ------------------------------------------------

# 1. check_deploy_status -- echoes "DEPLOY <status> <8-char-id> v<version>".
check_deploy_status() {
    _resolve_latest_deploy
    local short="${_LATEST_DEPLOY_ID:0:8}"
    echo "DEPLOY ${_LATEST_DEPLOY_STATUS} ${short} v${_LATEST_DEPLOY_VERSION}"
    if [ "${_LATEST_DEPLOY_STATUS}" = "SUCCESS" ]; then
        return 0
    fi
    return 1
}

# 2. check_universe_loaded -- echoes "UNIVERSE <count> tickers: <list>".
# As of v5.8.0 the tag is [UNIVERSE_GUARD] (was [UNIVERSE] pre-v5.8.0).
check_universe_loaded() {
    _resolve_latest_deploy
    local logs
    logs=$(_railway_logs_json "${_LATEST_DEPLOY_ID}" 200)
    local parsed
    parsed=$(DATA_JSON="${logs}" python3 -c '
import json, os, re
raw = os.environ.get("DATA_JSON", "")
try:
    data = json.loads(raw)
except Exception:
    print("0|")
    raise SystemExit(0)
if isinstance(data, dict):
    logs = (((data.get("data") or {}).get("deploymentLogs")) or [])
elif isinstance(data, list):
    logs = data
else:
    logs = []
count = 0
tickers = ""
for entry in reversed(logs):
    msg = entry.get("message", "") if isinstance(entry, dict) else str(entry)
    if "[UNIVERSE_GUARD]" not in msg:
        continue
    m = re.search(r"universe consistent \((\d+) tickers\)\s*[:\-]?\s*([A-Z, ]*)", msg)
    if m:
        count = int(m.group(1))
        tickers = m.group(2).strip().rstrip(",")
        break
    m = re.search(r"DRIFT detected:\s*(.*?)(?:rewriting|$)", msg)
    if m:
        body = m.group(1).strip()
        ticks = re.findall(r"\b[A-Z]{1,5}\b", body)
        count = len(ticks)
        tickers = ",".join(ticks)
        break
print(f"{count}|{tickers}")
')
    local count="${parsed%%|*}"
    local tickers="${parsed#*|}"
    echo "UNIVERSE ${count} tickers: ${tickers}"
    if [ "${count}" -ge 11 ] 2>/dev/null; then
        return 0
    fi
    return 1
}

# 3. check_log_tags <tag1> <tag2> ... -- echoes one "TAG <name> <count>" line
# per requested tag. Returns 0 if every tag occurs at least once.
check_log_tags() {
    if [ "$#" -eq 0 ]; then
        echo "TAG_ERR no tags supplied"
        return 1
    fi
    _resolve_latest_deploy
    local logs
    logs=$(_railway_logs_json "${_LATEST_DEPLOY_ID}" 500)
    # Pass tags via newline-delimited env var so Python can re-split safely.
    local tags_blob
    tags_blob=$(printf '%s\n' "$@")
    local out
    out=$(DATA_JSON="${logs}" TAGS_BLOB="${tags_blob}" python3 -c '
import json, os, sys
raw = os.environ.get("DATA_JSON", "")
try:
    data = json.loads(raw)
except Exception:
    data = {}
if isinstance(data, dict):
    logs = (((data.get("data") or {}).get("deploymentLogs")) or [])
elif isinstance(data, list):
    logs = data
else:
    logs = []
blob = "\n".join(
    (e.get("message", "") if isinstance(e, dict) else str(e))
    for e in logs
)
tags = [t for t in os.environ.get("TAGS_BLOB", "").split("\n") if t]
fail = 0
for tag in tags:
    n = blob.count(tag)
    print(f"TAG {tag} {n}")
    if n < 1:
        fail = 1
sys.exit(fail)
')
    local rc=$?
    printf '%s\n' "${out}"
    return ${rc}
}

# 4. check_no_errors -- echoes "ERRORS coroutine=N ws=N traceback=N error=N".
check_no_errors() {
    _resolve_latest_deploy
    local logs
    logs=$(_railway_logs_json "${_LATEST_DEPLOY_ID}" 500)
    local out
    out=$(DATA_JSON="${logs}" python3 -c '
import json, os, sys
raw = os.environ.get("DATA_JSON", "")
try:
    data = json.loads(raw)
except Exception:
    data = {}
if isinstance(data, dict):
    logs = (((data.get("data") or {}).get("deploymentLogs")) or [])
elif isinstance(data, list):
    logs = data
else:
    logs = []
blob = "\n".join(
    (e.get("message", "") if isinstance(e, dict) else str(e))
    for e in logs
)
patterns = [
    ("coroutine", "must be a coroutine"),
    ("ws", "websocket error"),
    ("traceback", "Traceback"),
    ("error", "[ERROR]"),
]
counts = {name: blob.count(needle) for name, needle in patterns}
print(
    "ERRORS coroutine={coroutine} ws={ws} traceback={traceback} error={error}".format(
        **counts
    )
)
sys.exit(1 if any(v > 0 for v in counts.values()) else 0)
')
    local rc=$?
    printf '%s\n' "${out}"
    return ${rc}
}

# 5. check_bar_archive_today -- "BARS_TODAY exists=<bool> ticker_count=<N> bytes=<N>".
# Per spec note: dir-existence is the real signal; market-closed days legitimately
# have 0 tickers, so we return 0 whenever the dir exists and only soft-warn
# when empty. Hard-fail only if the dir is missing.
check_bar_archive_today() {
    local today
    today=$(date -u +%Y-%m-%d)
    local cmd
    cmd=$(cat <<EOF
set -e
DIR=/data/bars/${today}
if [ -d "\$DIR" ]; then
  COUNT=\$(ls -1 "\$DIR" 2>/dev/null | wc -l)
  BYTES=\$(du -sb "\$DIR" 2>/dev/null | awk '{print \$1}')
  echo "EXISTS=true COUNT=\$COUNT BYTES=\$BYTES"
else
  echo "EXISTS=false COUNT=0 BYTES=0"
fi
EOF
)
    local raw
    raw=$(_railway_ssh "${cmd}" 2>/dev/null || true)
    local exists count bytes
    exists=$(printf '%s\n' "${raw}" | sed -nE 's/.*EXISTS=([a-z]+).*/\1/p' | head -1)
    count=$(printf '%s\n' "${raw}" | sed -nE 's/.*COUNT=([0-9]+).*/\1/p' | head -1)
    bytes=$(printf '%s\n' "${raw}" | sed -nE 's/.*BYTES=([0-9]+).*/\1/p' | head -1)
    exists="${exists:-false}"
    count="${count:-0}"
    bytes="${bytes:-0}"
    echo "BARS_TODAY exists=${exists} ticker_count=${count} bytes=${bytes}"
    if [ "${exists}" != "true" ]; then
        return 1
    fi
    if [ "${count}" -lt 1 ]; then
        _warn "bar archive dir exists but is empty (market may be closed)"
    fi
    return 0
}

# 6. check_shadow_db_count -- "SHADOW_DB total=<N> last_24h=<c1=N,c2=N,...>".
# Always returns 0 (informational).
check_shadow_db_count() {
    local cmd
    cmd=$(cat <<'EOF'
python3 - <<'PY'
import sqlite3, os, sys
db = "/data/shadow.db"
if not os.path.exists(db):
    print("TOTAL=0 BREAKDOWN=missing_db")
    sys.exit(0)
con = sqlite3.connect(db)
cur = con.cursor()
try:
    cur.execute("SELECT COUNT(*) FROM shadow_trades")
    total = cur.fetchone()[0]
except Exception as e:
    print(f"TOTAL=0 BREAKDOWN=err:{type(e).__name__}")
    sys.exit(0)
try:
    cur.execute(
        "SELECT config_name, COUNT(*) FROM shadow_trades "
        "WHERE entry_ts >= datetime('now', '-1 day') "
        "GROUP BY config_name ORDER BY config_name"
    )
    rows = cur.fetchall()
    parts = ",".join(f"{name}={n}" for name, n in rows) or "none"
except Exception as e:
    parts = f"err:{type(e).__name__}"
print(f"TOTAL={total} BREAKDOWN={parts}")
PY
EOF
)
    local raw
    raw=$(_railway_ssh "${cmd}" 2>/dev/null || true)
    local total breakdown
    total=$(printf '%s\n' "${raw}" | sed -nE 's/.*TOTAL=([0-9]+).*/\1/p' | head -1)
    breakdown=$(printf '%s\n' "${raw}" | sed -nE 's/.*BREAKDOWN=([^[:space:]]+).*/\1/p' | head -1)
    total="${total:-0}"
    breakdown="${breakdown:-unknown}"
    echo "SHADOW_DB total=${total} last_24h=${breakdown}"
    return 0
}

# 7. check_dashboard_state -- POST /login, GET /api/state, parse status+version.
# Echoes "DASHBOARD shadow_data_status=<v> version=<v>". Returns 0 if status==live.
check_dashboard_state() {
    local body
    if [ -n "${DASHBOARD_STATE_FIXTURE:-}" ] && [ -f "${DASHBOARD_STATE_FIXTURE}" ]; then
        body=$(cat "${DASHBOARD_STATE_FIXTURE}")
    else
        local jar
        jar=$(mktemp)
        curl -sS -c "${jar}" -b "${jar}" \
            -X POST "${DASHBOARD_URL}/login" \
            -H "Content-Type: application/x-www-form-urlencoded" \
            --data-urlencode "password=${DASHBOARD_PASSWORD}" \
            -o /dev/null
        body=$(curl -sS -b "${jar}" "${DASHBOARD_URL}/api/state")
        rm -f "${jar}"
    fi
    local parsed
    parsed=$(DATA_JSON="${body}" python3 -c '
import json, os
raw = os.environ.get("DATA_JSON", "")
try:
    data = json.loads(raw)
except Exception:
    print("unknown|unknown")
    raise SystemExit(0)
status = data.get("shadow_data_status") or "unknown"
version = data.get("version") or "unknown"
print(f"{status}|{version}")
')
    local status="${parsed%%|*}"
    local version="${parsed#*|}"
    echo "DASHBOARD shadow_data_status=${status} version=${version}"
    if [ "${status}" = "live" ]; then
        return 0
    fi
    return 1
}
