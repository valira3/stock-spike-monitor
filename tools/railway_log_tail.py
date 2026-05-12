"""tools.railway_log_tail -- v7.79.0 -- fetch recent Railway deployment
logs via the Railway GraphQL API for monitor-side log analysis.

The dashboard monitor (tools/dashboard_monitor.py) already polls
/api/state for structural invariants, but many production issues
only manifest in the bot's stdout/stderr logs -- e.g.:

  [ALPACA-ERR] insufficient_buying_power
  [SENTINEL][CRITICAL] both Alpaca and Yahoo failed
  [V79-ORB-REJECT] long X portfolio=main reason=...
  [paper] skip X -- insufficient cash
  [V15-SIZING] X side=LONG WAIT (defensive abort)
  risk_reject:notional_cap

This module fetches the last N log lines from the currently-running
Railway deployment and exposes a structured scanner for known
failure signatures. Used by inv_railway_logs_clean in
tools/dashboard_monitor_invariants.py.

## Environment

  RAILWAY_API_TOKEN   Personal or team API token from Railway dashboard.
                      Project tokens also work for read-only log access.
  RAILWAY_SERVICE_ID  The service UUID. Find it in the Railway dashboard
                      URL when viewing the service:
                      https://railway.app/project/<PROJECT_ID>/service/<SERVICE_ID>

  Optional:
  RAILWAY_API_URL     Override the API endpoint (default:
                      https://backboard.railway.com/graphql/v2).

If either required env is missing, fetch_recent_logs returns [] and
log-based invariants will skip rather than fail. This is intentional
-- the rest of the monitor must keep working without Railway access.

## Failure-tolerance

Every external call is wrapped in a broad try/except. Network errors,
authentication failures, schema drift, or empty responses all return
[] silently. The caller can distinguish "no logs available" from
"logs available but clean" by checking the returned list length.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_API = "https://backboard.railway.com/graphql/v2"

# Failure-signature regexes keyed by signal name. Order in the dict
# doesn't matter; the scanner returns counts per signal.
FAILURE_SIGNATURES: dict[str, str] = {
    # Broker submission errors (Alpaca rejects, network timeouts).
    "alpaca_error": r"\[ALPACA-ERR\]",
    # Sentinel-loop critical errors (data feed failures, etc.).
    "sentinel_critical": r"\[SENTINEL\]\[CRITICAL\]",
    # Engine-side risk rejects.
    "risk_reject_notional_cap": r"risk_reject:notional_cap",
    "risk_reject_other": r"\[V79-ORB-REJECT\]",
    # Cash gates.
    "insufficient_cash": r"\[paper\] skip .* insufficient cash",
    # V15-SIZING gate (should disappear post-v7.78.0 for v10 entries).
    "v15_wait_abort": r"\[V15-SIZING\].*WAIT \(defensive abort\)",
    # Ingest health.
    "ingest_disconnect": r"\[INGEST\].*DISCONNECTED",
    # Catastrophic exceptions.
    "uncaught_traceback": r"Traceback \(most recent call last\):",
}


# v7.100.0 -- last GraphQL errors captured by _gql, exposed for
# diagnostic surfaces. A response with HTTP 200 + non-empty "errors"
# array is the silent-failure mode that's been hiding Railway's
# schema-validation complaints from us across v7.99.0's date-range
# attempt -- the dict came back valid-looking, we mapped "data is
# None" to [] in the caller, and the error message was thrown away.
# This module-global captures the most recent error so the probe
# can include it in the issue footer without changing the _gql
# return type (which 50+ callers depend on).
_last_gql_errors: list[str] = []


def _gql(token: str, query: str, variables: dict, *,
         api_url: str = _DEFAULT_API,
         timeout: float = 10.0) -> dict | None:
    """POST a GraphQL query. Returns parsed JSON or None on any error.

    v7.92.0 -- supports BOTH Railway token types: personal/team API
    tokens (Authorization: Bearer header, from
    https://railway.app/account/tokens) AND project-scoped tokens
    (Project-Access-Token header, generated from a Project's Tokens
    tab). Tries Bearer first; on 401/403 retries with the
    Project-Access-Token header. Pre-v7.92.0 the helper only sent
    Bearer, so project tokens silently failed with auth_failed
    even when the operator had set RAILWAY_API_TOKEN correctly.
    """
    payload = json.dumps({"query": query, "variables": variables}).encode()

    def _try(headers: dict) -> dict | None:
        req = urllib.request.Request(
            api_url, data=payload, method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")[:300]
            except Exception:
                body = "<unreadable>"
            logger.debug("railway gql HTTP %d: %s", e.code, body)
            # Signal auth-failure variants to the caller so the
            # outer function can decide whether to retry with the
            # alternate header. Returning {"_auth_failed": True}
            # keeps us inside the existing "None means hard fail"
            # contract without confusing other paths.
            if e.code in (401, 403):
                return {"_auth_failed": True}
            return None
        except Exception as e:
            logger.debug("railway gql error: %s: %s", type(e).__name__, str(e)[:200])
            return None

    common = {
        "Content-Type": "application/json",
        "User-Agent": "tg-railway-log-tail/7.100.0",
    }
    # Attempt 1 -- personal/team API token shape.
    resp = _try({**common, "Authorization": f"Bearer {token}"})
    if resp is None:
        # Non-auth failure (network, 5xx, schema drift). No point
        # retrying with a different header -- the server isn't
        # rejecting the credential, it's failing for another reason.
        return None
    if not resp.get("_auth_failed"):
        return _record_errors(resp)
    # Bearer was rejected with 401/403 -- retry with project-token
    # header. If THIS one also returns _auth_failed, both auth
    # shapes are wrong (bad token, missing scope) and we return None.
    resp2 = _try({**common, "Project-Access-Token": token})
    if resp2 is None or resp2.get("_auth_failed"):
        return None
    return _record_errors(resp2)


def _record_errors(resp: dict | None) -> dict | None:
    """v7.100.0 -- capture GraphQL errors in _last_gql_errors.

    Railway returns HTTP 200 with `{"errors": [...], "data": null}`
    when the query/schema is wrong (deprecated field, missing
    required arg, wrong arg name, etc.). Without this hook, callers
    that check for `data` or for a specific field fall through to
    [] and the actual error message is thrown away. The capture
    lets `probe_railway_access` include the last error in the
    issue footer so an operator can read Railway's complaint
    directly without inspecting the workflow log.
    """
    global _last_gql_errors
    _last_gql_errors = []
    if not isinstance(resp, dict):
        return resp
    errs = resp.get("errors")
    if isinstance(errs, list) and errs:
        out: list[str] = []
        for e in errs[:5]:
            if isinstance(e, dict):
                msg = str(e.get("message") or "")
            else:
                msg = str(e)
            if msg:
                out.append(msg[:200])
        if out:
            _last_gql_errors = out
            logger.warning("[GRAPHQL-ERROR] %s", "; ".join(out))
    return resp


_LATEST_DEPLOYMENT_QUERY = """
query latestDeployment($serviceId: String!) {
  deployments(input: {serviceId: $serviceId}, first: 1) {
    edges { node { id status createdAt } }
  }
}
"""

_DEPLOYMENT_LOGS_QUERY = """
query deploymentLogs(
  $deploymentId: String!,
  $limit: Int!,
  $startDate: DateTime!,
  $endDate: DateTime!
) {
  deploymentLogs(
    deploymentId: $deploymentId,
    limit: $limit,
    startDate: $startDate,
    endDate: $endDate
  ) {
    timestamp
    message
    severity
  }
}
"""


def _resolve_latest_deployment_id(token: str, service_id: str,
                                  api_url: str) -> str | None:
    """Query Railway for the most recent deployment of the service.
    Returns the deployment id or None on any failure.
    """
    resp = _gql(token, _LATEST_DEPLOYMENT_QUERY, {"serviceId": service_id},
                api_url=api_url)
    if not resp or "data" not in resp:
        return None
    try:
        edges = (resp["data"]["deployments"] or {}).get("edges") or []
        if not edges:
            return None
        return str(edges[0]["node"]["id"])
    except (KeyError, TypeError):
        return None


def fetch_recent_logs(limit: int = 500) -> list[dict]:
    """Fetch the last `limit` log lines from the current Railway
    deployment of the service identified by RAILWAY_SERVICE_ID.

    Returns a list of dicts with keys: ``timestamp``, ``message``,
    ``severity``. Returns [] when:
      - RAILWAY_API_TOKEN or RAILWAY_SERVICE_ID is missing
      - the GraphQL API is unreachable
      - the query schema has drifted
      - the response is empty or unparseable

    Callers MUST treat [] as "no logs available -- skip log-based
    invariants" rather than "logs available and clean". The list
    being non-empty is the only signal that the fetch succeeded.
    """
    token = (os.environ.get("RAILWAY_API_TOKEN", "") or "").strip()
    service_id = (os.environ.get("RAILWAY_SERVICE_ID", "") or "").strip()
    if not token or not service_id:
        return []
    api_url = (os.environ.get("RAILWAY_API_URL", "") or "").strip() or _DEFAULT_API
    deployment_id = _resolve_latest_deployment_id(token, service_id, api_url)
    if not deployment_id:
        return []
    # v7.99.0 -- Railway's deploymentLogs query now requires startDate
    # and endDate (DateTime!). Pre-v7.99.0 we omitted both arguments
    # and Railway silently returned 0 rows even with valid auth and
    # a fresh deployment_id. Use a 24h window ending now as the
    # default -- spans all of today's RTH activity plus the prior
    # session for late-evening replays.
    # v7.101.0 -- Railway also enforces a max value on `limit`.
    # Issue #593's GraphQL error `Error in limit - Invalid input`
    # revealed that our limit=10000 (bumped 3000->10000 in v7.95.0)
    # exceeds Railway's accepted range. Clamp to 500 -- the
    # historical v7.79.0 value, known to work -- and let callers
    # request more if Railway's cap turns out to be higher in a
    # future version.
    _safe_limit = max(1, min(int(limit), 500))
    from datetime import datetime, timedelta, timezone as _tz
    _now = datetime.now(_tz.utc)
    _start = (_now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _end = _now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    resp = _gql(token, _DEPLOYMENT_LOGS_QUERY,
                {"deploymentId": deployment_id, "limit": _safe_limit,
                 "startDate": _start, "endDate": _end},
                api_url=api_url)
    if not resp or "data" not in resp:
        return []
    try:
        rows = resp["data"]["deploymentLogs"] or []
    except (KeyError, TypeError):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "timestamp": str(row.get("timestamp") or ""),
            "message": str(row.get("message") or ""),
            "severity": str(row.get("severity") or ""),
        })
    return out


# v7.96.0 -- tiny helper: actual lines returned from a real
# fetch_recent_logs call. Lets the dashboard monitor footer
# distinguish "Railway capped our limit silently" from "the bot's
# recent log window genuinely contains zero matches for the grep
# patterns we care about." A common Railway GraphQL behaviour is
# to cap deploymentLogs(limit:) below the requested value with no
# error -- the v7.95.0 bump 3000->10000 may have been silently
# truncated to whatever Railway's per-query ceiling is.
def count_recent_logs(limit: int = 10000) -> int:
    """Return how many log rows fetch_recent_logs(limit) actually
    returned. Mirrors fetch_recent_logs failure handling -- returns
    0 on any error rather than raising. Callers compare the returned
    count against the requested limit: count << limit means Railway
    capped us; count >= limit means we asked for as much as Railway
    is willing to give.
    """
    rows = fetch_recent_logs(limit=limit)
    return len(rows)


def get_last_gql_errors() -> list[str]:
    """v7.100.0 -- read-only accessor for the last GraphQL `errors`
    captured by `_record_errors`. Empty list if the last call was
    successful or didn't include errors. Used by `probe_railway_access`
    to include the most recent schema complaint in the monitor footer.
    """
    return list(_last_gql_errors)


# v7.91.0 -- single-leg diagnostic probe. fetch_recent_logs swallows
# every failure mode into [] (auth fail, schema drift, network
# error, empty window). That uniformity is correct for callers but
# made today's "Why is the log slice empty?" question unanswerable
# without inspecting the dashboard monitor's workflow log. This
# probe returns a structured status so the monitor can surface
# WHICH leg is broken in the issue body and in stdout.
#
# Return values, in order of detection:
#   "missing_token"     RAILWAY_API_TOKEN env var is empty
#   "missing_service"   RAILWAY_SERVICE_ID env var is empty
#   "auth_failed"       GraphQL call returned None (HTTP error / 401 /
#                       network failure / schema drift). Most likely
#                       cause when both env vars look set: token
#                       missing the project log-read scope, or
#                       service_id pointing at a project_id instead
#                       of a service_id.
#   "no_deployment"     auth ok but the service has zero deployments
#                       (very unusual; either a freshly-created service
#                       or wrong service_id pointing at an empty one).
#   "ok"                fetched the latest deployment id without error.
#                       At least confirms auth + service resolution
#                       work; logs may still be empty due to retention
#                       but not because of credentials.
def probe_railway_access() -> dict:
    """One-shot diagnostic of Railway credential health.

    Returns a dict with keys:
      status              one of the strings above
      token_set           bool -- RAILWAY_API_TOKEN env var is non-empty
      service_set         bool -- RAILWAY_SERVICE_ID env var is non-empty
      deployment_id       resolved deployment id when status=="ok", else ""
      deployment_status   Railway-reported status string for the resolved
                          deployment (SUCCESS / FAILED / REMOVED /
                          BUILDING / DEPLOYING / CRASHED / ...). v7.97.0
                          added this because v7.96.0's
                          lines_fetched_on_10k_request=0 on issue #583
                          suggested the resolver was picking a wrong
                          (non-running) deployment.
      deployment_created  ISO timestamp of when the resolved deployment
                          was created. v7.98.0 added this -- with a
                          full-account Railway token, status=SUCCESS
                          plus lines_fetched=0 means either the
                          deployment is stale (createdAt is days old)
                          or the deploymentLogs GraphQL schema has
                          drifted. The createdAt timestamp differentiates.
    """
    token = (os.environ.get("RAILWAY_API_TOKEN", "") or "").strip()
    service_id = (os.environ.get("RAILWAY_SERVICE_ID", "") or "").strip()
    out = {
        "status": "ok",
        "token_set": bool(token),
        "service_set": bool(service_id),
        "deployment_id": "",
        "deployment_status": "",
        "deployment_created": "",
    }
    if not token:
        out["status"] = "missing_token"
        return out
    if not service_id:
        out["status"] = "missing_service"
        return out
    api_url = (os.environ.get("RAILWAY_API_URL", "") or "").strip() or _DEFAULT_API
    resp = _gql(token, _LATEST_DEPLOYMENT_QUERY, {"serviceId": service_id},
                api_url=api_url)
    if not resp or "data" not in resp:
        out["status"] = "auth_failed"
        return out
    try:
        edges = (resp["data"]["deployments"] or {}).get("edges") or []
    except (KeyError, TypeError):
        out["status"] = "auth_failed"
        return out
    if not edges:
        out["status"] = "no_deployment"
        return out
    try:
        node = edges[0]["node"] or {}
        out["deployment_id"] = str(node.get("id") or "")
        out["deployment_status"] = str(node.get("status") or "")
        out["deployment_created"] = str(node.get("createdAt") or "")
    except (KeyError, TypeError):
        out["status"] = "auth_failed"
    return out


def scan_for_failures(logs: list[dict],
                      signatures: dict[str, str] | None = None,
                      ) -> dict[str, dict]:
    """Scan logs for FAILURE_SIGNATURES.

    Args:
        logs: list of {timestamp, message, severity} dicts (from
              fetch_recent_logs).
        signatures: optional custom regex map (defaults to
              FAILURE_SIGNATURES module constant).

    Returns:
        {signal_name: {"count": int, "first_message": str,
                       "last_timestamp": str}}
        for every signal that matched at least once. Signals with
        zero matches are NOT included.
    """
    sigs = signatures if signatures is not None else FAILURE_SIGNATURES
    compiled = {name: re.compile(pat) for name, pat in sigs.items()}
    findings: dict[str, dict] = {}
    for row in logs:
        msg = row.get("message") or ""
        ts = row.get("timestamp") or ""
        for name, rx in compiled.items():
            if rx.search(msg):
                bucket = findings.setdefault(name, {
                    "count": 0,
                    "first_message": msg[:300],
                    "last_timestamp": ts,
                })
                bucket["count"] += 1
                # Keep the most recent timestamp for the bucket.
                if ts and ts > bucket["last_timestamp"]:
                    bucket["last_timestamp"] = ts
    return findings


def grep_logs(pattern: str, *, limit: int = 500,
              max_matches: int = 20) -> list[dict]:
    """v7.84.0 -- fetch Railway logs and return rows matching `pattern`.

    Used by invariants to enrich their failure detail with the actual
    Railway log lines that explain the violation. E.g.:

        slice = grep_logs(r"\\[V79-MIRROR-\\w+\\] Val")
        if slice:
            detail += "\\n\\nLog slice:\\n" + format_log_slice(slice)

    Args:
        pattern: regex pattern (Python `re.search` semantics).
        limit: how many recent log lines to fetch (default 500).
        max_matches: cap matches returned to avoid bloated issue bodies
                     (default 20; older lines win the slot if exceeded).

    Returns:
        List of matching {timestamp, message, severity} dicts. Empty
        list when Railway log fetch is unavailable (missing secrets,
        API error, etc.) -- callers should treat empty as "no log
        context available" rather than "no matches".
    """
    try:
        compiled = re.compile(pattern)
    except re.error:
        return []
    logs = fetch_recent_logs(limit=limit)
    if not logs:
        return []
    matched: list[dict] = []
    for row in logs:
        msg = row.get("message") or ""
        if compiled.search(msg):
            matched.append(row)
            if len(matched) >= max_matches:
                break
    return matched


def format_log_slice(rows: list[dict], *, max_lines: int = 20) -> str:
    """v7.84.0 -- compact one-line-per-row formatter for issue bodies.

    Returns a string like:
      2026-05-11T17:47:48Z [V79-MIRROR-RECV] Val kind=ENTRY_LONG ...
      2026-05-11T17:47:48Z [V79-MIRROR-SKIP] Val ENTRY_LONG TSLA qty=0 ...
    """
    out: list[str] = []
    for r in rows[:max_lines]:
        ts = r.get("timestamp") or "?"
        msg = (r.get("message") or "").rstrip()
        # Trim msg if very long (preserve the first 280 chars).
        if len(msg) > 280:
            msg = msg[:280] + "…"
        out.append(f"{ts} {msg}")
    if len(rows) > max_lines:
        out.append(f"... +{len(rows) - max_lines} more matches")
    return "\n".join(out)
