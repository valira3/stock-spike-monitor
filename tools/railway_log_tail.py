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


def _gql(token: str, query: str, variables: dict, *,
         api_url: str = _DEFAULT_API,
         timeout: float = 10.0) -> dict | None:
    """POST a GraphQL query. Returns parsed JSON or None on any error."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        api_url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "tg-railway-log-tail/7.79.0",
        },
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
        return None
    except Exception as e:
        logger.debug("railway gql error: %s: %s", type(e).__name__, str(e)[:200])
        return None


_LATEST_DEPLOYMENT_QUERY = """
query latestDeployment($serviceId: String!) {
  deployments(input: {serviceId: $serviceId}, first: 1) {
    edges { node { id status } }
  }
}
"""

_DEPLOYMENT_LOGS_QUERY = """
query deploymentLogs($deploymentId: String!, $limit: Int!) {
  deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
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
    resp = _gql(token, _DEPLOYMENT_LOGS_QUERY,
                {"deploymentId": deployment_id, "limit": int(limit)},
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
