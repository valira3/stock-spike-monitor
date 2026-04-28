#!/usr/bin/env python3
"""v5.8.4 \u2014 Saturday weekly report parser.

Replaces the broken Saturday cron 873854a1 which still parses
[V510-SHADOW] lines from v5.5.x. Live prod (v5.8.x) emits
[V560-GATE]/[V570-STRIKE]/[V571-EXIT_PHASE]/[ENTRY]/[TRADE_CLOSED]/[SKIP].

Usage:
    python scripts/saturday_weekly_report.py \\
        --week-start 2026-04-27 \\
        [--out-dir /home/user/workspace/backtest_v57x] \\
        [--logs-dir <path>]

If --logs-dir is provided, reads day_YYYY-MM-DD.jsonl files from there
(offline mode). Otherwise pulls from Railway's deploymentLogs GraphQL.
Output: <out-dir>/week_<MONDAY>/{report.md, report.json, day_*.jsonl}.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path


GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"
USER_AGENT = "tradegenius-ops/1.0"

SHADOW_CONFIGS = ("TICKER+QQQ", "TICKER_ONLY", "QQQ_ONLY", "GEMINI_A")

EXIT_REASONS = (
    "hard_stop_2c",
    "ema_trail",
    "be_stop",
    "velocity_fuse",
    "eod",
    "kill_switch",
)


# --- Railway GraphQL helper -------------------------------------------------


def _railway_gql(query: str, variables: dict, token: str) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": f"HTTP {e.code}: {e.reason}"}]}
    except Exception as e:  # noqa: BLE001
        return {"errors": [{"message": f"{type(e).__name__}: {e}"}]}
    return json.loads(resp.read())


def _list_deployments(
    token: str, project: str, service: str, environment: str, max_pages: int = 6
) -> list[dict]:
    q = (
        "query($p:String!,$s:String,$e:String,$a:String){"
        "deployments(input:{projectId:$p,serviceId:$s,environmentId:$e},"
        "first:50,after:$a){edges{cursor node{id status createdAt}}"
        "pageInfo{hasNextPage endCursor}}}"
    )
    after = None
    out = []
    for _ in range(max_pages):
        data = _railway_gql(
            q,
            {
                "p": project,
                "s": service,
                "e": environment,
                "a": after,
            },
            token,
        )
        if data.get("errors"):
            break
        d = data.get("data", {}).get("deployments") or {}
        for edge in d.get("edges") or []:
            out.append(edge["node"])
        info = d.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return out


def _fetch_deployment_logs(deployment_id: str, token: str, limit: int = 5000) -> list[dict]:
    q = "query($d:String!,$l:Int!){deploymentLogs(deploymentId:$d,limit:$l){timestamp message}}"
    data = _railway_gql(q, {"d": deployment_id, "l": limit}, token)
    if data.get("errors"):
        return []
    return data.get("data", {}).get("deploymentLogs") or []


def fetch_week_logs(week_start: dt.date, out_dir: Path) -> dict[str, int]:
    """Fetch the Mon-Fri trading-week logs from Railway and persist as
    one JSONL per UTC day. Returns {day: lines_written}.

    Each output line: {"t": "<iso>", "m": "<message>"}.
    """
    token = os.environ.get("RAILWAY_API_TOKEN", "").strip()
    project = os.environ.get("RAILWAY_PROJECT", "").strip()
    service = os.environ.get("RAILWAY_SERVICE", "").strip()
    environment = os.environ.get("RAILWAY_ENVIRONMENT", "").strip()
    if not (token and project and service and environment):
        print(
            "[saturday_weekly_report] RAILWAY_* env vars missing; no logs fetched.",
            file=sys.stderr,
        )
        return {}
    deployments = _list_deployments(token, project, service, environment)
    week_days = [week_start + dt.timedelta(days=i) for i in range(5)]
    week_set = {d.isoformat() for d in week_days}

    # Buckets keyed by UTC day; values are list[(ts, msg)] tuples.
    by_day: dict[str, list[tuple[str, str]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()

    for node in deployments:
        created = node.get("createdAt") or ""
        # Created stamp is when the deploy started; logs may span next
        # day. We still want to look at any deployment whose creation
        # falls within or just before the week (-1 day for safety).
        if len(created) < 10:
            continue
        c_day = created[:10]
        if c_day < (week_start - dt.timedelta(days=1)).isoformat():
            continue
        if c_day > (week_start + dt.timedelta(days=5)).isoformat():
            continue
        logs = _fetch_deployment_logs(node["id"], token, limit=5000)
        for entry in logs:
            ts = entry.get("timestamp") or ""
            msg = entry.get("message") or ""
            if not ts or len(ts) < 10:
                continue
            day = ts[:10]
            if day not in week_set:
                continue
            key = (ts, msg)
            if key in seen:
                continue
            seen.add(key)
            by_day[day].append(key)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    for day, items in by_day.items():
        items.sort()
        path = out_dir / f"day_{day}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for ts, msg in items:
                f.write(json.dumps({"t": ts, "m": msg}) + "\n")
        written[day] = len(items)
    return written


# --- Parsers ---------------------------------------------------------------

# kv pairs: word=value where value is non-space and unquoted, OR a JSON
# object payload (for gate_state). We parse generically for non-JSON
# fields and special-case gate_state which contains '{...}'.
_KV_RE = re.compile(r"(\w+)=([^\s]+)")


def _kv_from(line: str) -> dict:
    """Parse `key=value` tokens from a log line. Skips JSON object
    values like gate_state={...} \u2014 these are not needed for the
    aggregations we compute. Booleans are returned as the strings
    'True'/'False' (we cast at the call site)."""
    # Strip a trailing gate_state={...} if present so the regex doesn't
    # over-consume; we don't need its content for the report.
    cleaned = re.sub(r"gate_state=\{[^}]*\}", "gate_state=__JSON__", line)
    out: dict = {}
    for m in _KV_RE.finditer(cleaned):
        out[m.group(1)] = m.group(2)
    return out


def _to_float(s: str | None) -> float | None:
    if s is None or s == "" or s == "null" or s == "None":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _to_int(s: str | None) -> int | None:
    f = _to_float(s)
    return None if f is None else int(f)


def _to_bool(s: str | None) -> bool | None:
    if s is None:
        return None
    sl = s.strip().lower()
    if sl in ("true", "1", "yes"):
        return True
    if sl in ("false", "0", "no"):
        return False
    return None


def parse_jsonl_file(path: Path) -> list[dict]:
    """Read one day's jsonl. Returns list of {'t', 'm'} dicts. Lines
    that fail to decode are skipped."""
    out: list[dict] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and "m" in rec:
                out.append(rec)
    return out


def parse_log_message(msg: str) -> dict | None:
    """Classify a single log message line. Returns {'type': ..., **kv}
    or None if not a recognized event."""
    if "[V570-STRIKE]" in msg:
        kv = _kv_from(msg)
        return {
            "type": "V570_STRIKE",
            "ticker": kv.get("ticker"),
            "side": kv.get("side"),
            "ts": kv.get("ts"),
            "strike_num": _to_int(kv.get("strike_num")),
            "is_first": _to_bool(kv.get("is_first")),
            "expansion_gate_pass": _to_bool(kv.get("expansion_gate_pass")),
            "hod_break": _to_bool(kv.get("hod_break")),
            "lod_break": _to_bool(kv.get("lod_break")),
        }
    if "[V571-EXIT_PHASE]" in msg:
        kv = _kv_from(msg)
        return {
            "type": "V571_EXIT_PHASE",
            "ticker": kv.get("ticker"),
            "side": kv.get("side"),
            "entry_id": kv.get("entry_id"),
            "from_phase": kv.get("from_phase"),
            "to_phase": kv.get("to_phase"),
            "trigger": kv.get("trigger"),
            "current_stop": _to_float(kv.get("current_stop")),
            "ts": kv.get("ts"),
        }
    if "[TRADE_CLOSED]" in msg:
        kv = _kv_from(msg)
        return {
            "type": "TRADE_CLOSED",
            "ticker": kv.get("ticker"),
            "side": kv.get("side"),
            "entry_id": kv.get("entry_id"),
            "entry_ts": kv.get("entry_ts"),
            "entry_price": _to_float(kv.get("entry_price")),
            "exit_ts": kv.get("exit_ts"),
            "exit_price": _to_float(kv.get("exit_price")),
            "exit_reason": kv.get("exit_reason"),
            "qty": _to_int(kv.get("qty")),
            "pnl_dollars": _to_float(kv.get("pnl_dollars")),
            "pnl_pct": _to_float(kv.get("pnl_pct")),
            "hold_seconds": _to_int(kv.get("hold_seconds")),
            "strike_num": _to_int(kv.get("strike_num")),
        }
    if "[ENTRY]" in msg and "ticker=" in msg and "entry_id=" in msg:
        kv = _kv_from(msg)
        return {
            "type": "ENTRY",
            "ticker": kv.get("ticker"),
            "side": kv.get("side"),
            "entry_id": kv.get("entry_id"),
            "entry_ts": kv.get("entry_ts"),
            "entry_price": _to_float(kv.get("entry_price")),
            "qty": _to_int(kv.get("qty")),
            "strike_num": _to_int(kv.get("strike_num")),
        }
    if "[V560-GATE]" in msg and "pass=" in msg:
        kv = _kv_from(msg)
        return {
            "type": "V560_GATE",
            "ticker": kv.get("ticker"),
            "side": kv.get("side"),
            "ts": kv.get("ts"),
            "g1": _to_bool(kv.get("g1")),
            "g3": _to_bool(kv.get("g3")),
            "g4": _to_bool(kv.get("g4")),
            "pass": _to_bool(kv.get("pass")),
            "reason": kv.get("reason"),
        }
    if "[SKIP]" in msg and "reason=" in msg and "ticker=" in msg:
        kv = _kv_from(msg)
        return {
            "type": "SKIP",
            "ticker": kv.get("ticker"),
            "reason": kv.get("reason"),
            "ts": kv.get("ts"),
        }
    if "[V510-SHADOW][CFG=" in msg:
        # cfg name and verdict are needed to attribute per-config
        # decisions to live entries.
        m = re.search(r"\[V510-SHADOW\]\[CFG=([^\]]+)\]", msg)
        cfg_name = m.group(1) if m else None
        kv = _kv_from(msg)
        return {
            "type": "SHADOW_CFG",
            "config": cfg_name,
            "ticker": kv.get("ticker"),
            "bucket": kv.get("bucket"),
            "stage": _to_int(kv.get("stage")),
            "verdict": kv.get("verdict"),
            "reason": kv.get("reason"),
            "entry_decision": kv.get("entry_decision"),
        }
    if "[V571-FSM]" in msg:
        return {"type": "V571_FSM"}
    return None


def parse_records(records: list[dict]) -> list[dict]:
    """Map raw {'t','m'} records to typed events, dropping non-events."""
    out: list[dict] = []
    for r in records:
        ev = parse_log_message(r.get("m", ""))
        if ev is None:
            continue
        ev["t"] = r.get("t")
        out.append(ev)
    return out


# --- Aggregations ----------------------------------------------------------


def _last_monday(today: dt.date) -> dt.date:
    """Most recent Monday before `today` (strict). For Saturday cron
    runs that is the Monday of the trading week that just ended."""
    # weekday(): Mon=0..Sun=6
    delta = today.weekday() if today.weekday() != 0 else 7
    return today - dt.timedelta(days=delta)


def _pair_entries_to_closes(events: list[dict]) -> list[dict]:
    """Pair [ENTRY] and [TRADE_CLOSED] by entry_id. Returns list of
    paired records: {'entry': <ENTRY ev>, 'close': <TRADE_CLOSED ev>}.
    A close without a matching entry still produces a record with
    entry=None (we still get pnl/exit_reason from the close)."""
    entries: dict[str, dict] = {}
    pairs: list[dict] = []
    for ev in events:
        if ev["type"] == "ENTRY" and ev.get("entry_id"):
            entries[ev["entry_id"]] = ev
        elif ev["type"] == "TRADE_CLOSED":
            eid = ev.get("entry_id")
            pairs.append({"entry": entries.get(eid), "close": ev})
    return pairs


def _shadow_decisions_at_entry(events: list[dict]) -> dict[str, dict]:
    """For each (config, ticker) at the time the live bot fires an
    [ENTRY], the most recent [V510-SHADOW][CFG=<config>] verdict for
    that ticker tells us whether that config would have allowed the
    same trade. We index by entry_id of the closest preceding ENTRY,
    since all events within the run are time-ordered.

    Returns: {entry_id: {config_name: verdict}}.
    """
    result: dict[str, dict] = defaultdict(dict)
    last_shadow: dict[tuple[str, str], str] = {}
    for ev in events:
        t = ev.get("type")
        if t == "SHADOW_CFG":
            cfg = ev.get("config")
            tkr = ev.get("ticker")
            verdict = ev.get("verdict")
            if cfg and tkr and verdict:
                last_shadow[(cfg, tkr)] = verdict
        elif t == "ENTRY":
            eid = ev.get("entry_id")
            tkr = ev.get("ticker")
            if not eid:
                continue
            for cfg in SHADOW_CONFIGS:
                v = last_shadow.get((cfg, tkr))
                if v is not None:
                    result[eid][cfg] = v
    return result


def aggregate_week(events: list[dict]) -> dict:
    """Build the report dict. Pure: takes already-parsed events."""
    pairs = _pair_entries_to_closes(events)

    # Headline ----
    closed_pnl = [
        p["close"].get("pnl_dollars") for p in pairs if p["close"].get("pnl_dollars") is not None
    ]
    actual_pnl = sum(closed_pnl)
    n_entries = sum(1 for ev in events if ev["type"] == "ENTRY")
    n_closed = len(pairs)
    n_wins = sum(1 for v in closed_pnl if v > 0)
    win_rate = (n_wins / n_closed) if n_closed else 0.0

    # Per-config table ----
    shadow = _shadow_decisions_at_entry(events)
    cfg_stats: dict[str, dict] = {
        c: {"allowed": 0, "blocked": 0, "allowed_pnl": 0.0, "blocked_pnl": 0.0, "allowed_wins": 0}
        for c in SHADOW_CONFIGS
    }
    for p in pairs:
        eid = (p["entry"] or {}).get("entry_id")
        pnl = p["close"].get("pnl_dollars") or 0.0
        decisions = shadow.get(eid or "", {})
        for cfg in SHADOW_CONFIGS:
            verdict = decisions.get(cfg)
            if verdict == "PASS":
                cfg_stats[cfg]["allowed"] += 1
                cfg_stats[cfg]["allowed_pnl"] += pnl
                if pnl > 0:
                    cfg_stats[cfg]["allowed_wins"] += 1
            elif verdict == "FAIL":
                cfg_stats[cfg]["blocked"] += 1
                cfg_stats[cfg]["blocked_pnl"] += pnl
            else:
                # No shadow verdict observed \u2014 treat as allowed by
                # default (mirrors the live bot which entered).
                cfg_stats[cfg]["allowed"] += 1
                cfg_stats[cfg]["allowed_pnl"] += pnl
                if pnl > 0:
                    cfg_stats[cfg]["allowed_wins"] += 1
    for cfg, s in cfg_stats.items():
        a = s["allowed"]
        s["allowed_win_rate"] = (s["allowed_wins"] / a) if a else 0.0
        s["net_swing_vs_actual"] = s["allowed_pnl"] - actual_pnl

    # Per-exit-reason ----
    by_reason: dict[str, dict] = {
        r: {"count": 0, "pnl_total": 0.0, "wins": 0} for r in EXIT_REASONS
    }
    other = {"count": 0, "pnl_total": 0.0, "wins": 0}
    for p in pairs:
        c = p["close"]
        reason = c.get("exit_reason") or ""
        pnl = c.get("pnl_dollars") or 0.0
        bucket = by_reason.get(reason, other if reason else other)
        bucket["count"] += 1
        bucket["pnl_total"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
    for r, b in by_reason.items():
        b["pnl_avg"] = (b["pnl_total"] / b["count"]) if b["count"] else 0.0
        b["win_rate"] = (b["wins"] / b["count"]) if b["count"] else 0.0
    other["pnl_avg"] = (other["pnl_total"] / other["count"]) if other["count"] else 0.0
    other["win_rate"] = (other["wins"] / other["count"]) if other["count"] else 0.0

    # Skipped-candidate stats ----
    skip_by_reason: Counter = Counter()
    skip_tickers_by_reason: dict[str, Counter] = defaultdict(Counter)
    for ev in events:
        if ev["type"] != "SKIP":
            continue
        reason = ev.get("reason") or "unknown"
        tkr = ev.get("ticker") or "UNKNOWN"
        skip_by_reason[reason] += 1
        skip_tickers_by_reason[reason][tkr] += 1
    top3_skip = skip_by_reason.most_common(3)
    skip_top_details: list[dict] = []
    for reason, count in top3_skip:
        top_tickers = skip_tickers_by_reason[reason].most_common(5)
        skip_top_details.append(
            {
                "reason": reason,
                "count": count,
                "top_tickers": [{"ticker": t, "count": c} for t, c in top_tickers],
            }
        )

    # Anomalies ----
    anomalies: list[str] = []
    closes_without_entry = sum(1 for p in pairs if p["entry"] is None)
    if closes_without_entry:
        anomalies.append(f"{closes_without_entry} TRADE_CLOSED line(s) with no matching ENTRY")
    n_strikes = sum(1 for ev in events if ev["type"] == "V570_STRIKE")
    if n_strikes == 0 and n_entries > 0:
        anomalies.append(
            "No [V570-STRIKE] events observed despite ENTRY lines \u2014 possible logging gap"
        )
    if not events:
        anomalies.append("No recognized events parsed for the week")
    no_shadow = sum(1 for p in pairs if not shadow.get((p["entry"] or {}).get("entry_id") or ""))
    if no_shadow and pairs:
        anomalies.append(
            f"{no_shadow}/{len(pairs)} closed trades had no shadow "
            "verdict observed \u2014 per-config attribution may understate"
        )

    return {
        "headline": {
            "actual_pnl": actual_pnl,
            "total_entries": n_entries,
            "total_closed": n_closed,
            "win_rate": win_rate,
        },
        "per_config": cfg_stats,
        "per_exit_reason": {
            **{r: by_reason[r] for r in EXIT_REASONS},
            "other": other,
        },
        "skip_stats": {
            "by_reason": dict(skip_by_reason),
            "top3": skip_top_details,
        },
        "anomalies": anomalies,
    }


# --- Markdown rendering ---------------------------------------------------


def render_report_md(week_start: dt.date, agg: dict, cumulative: dict | None) -> str:
    h = agg["headline"]
    lines: list[str] = []
    lines.append(f"# Saturday weekly report \u2014 week of {week_start.isoformat()}")
    lines.append("")
    lines.append("## 1. Headline")
    lines.append(f"- Actual P&L: ${h['actual_pnl']:.2f}")
    lines.append(f"- Total entries: {h['total_entries']} (closed: {h['total_closed']})")
    lines.append(f"- Win rate (closed): {h['win_rate'] * 100:.1f}%")
    lines.append("")

    lines.append("## 2. 4-config comparison")
    lines.append("")
    lines.append(
        "| Config | Allowed | Blocked | Allowed P&L | Blocked P&L "
        "| Allowed win rate | Net swing vs actual |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for cfg in SHADOW_CONFIGS:
        s = agg["per_config"][cfg]
        lines.append(
            f"| {cfg} | {s['allowed']} | {s['blocked']} | "
            f"${s['allowed_pnl']:.2f} | ${s['blocked_pnl']:.2f} | "
            f"{s['allowed_win_rate'] * 100:.1f}% | "
            f"${s['net_swing_vs_actual']:.2f} |"
        )
    lines.append("")

    lines.append("## 3. Per-exit-reason P&L breakdown")
    lines.append("")
    lines.append("| Exit reason | Count | Total P&L | Avg P&L | Win rate |")
    lines.append("|---|---:|---:|---:|---:|")
    for r in EXIT_REASONS:
        b = agg["per_exit_reason"][r]
        lines.append(
            f"| {r} | {b['count']} | ${b['pnl_total']:.2f} | "
            f"${b['pnl_avg']:.2f} | {b['win_rate'] * 100:.1f}% |"
        )
    o = agg["per_exit_reason"]["other"]
    if o["count"]:
        lines.append(
            f"| (other/unknown) | {o['count']} | ${o['pnl_total']:.2f} | "
            f"${o['pnl_avg']:.2f} | {o['win_rate'] * 100:.1f}% |"
        )
    lines.append("")

    lines.append("## 4. Skipped-candidate stats")
    lines.append("")
    by = agg["skip_stats"]["by_reason"]
    if not by:
        lines.append("(no [SKIP] events observed)")
    else:
        lines.append("| Reason | Count |")
        lines.append("|---|---:|")
        for reason, count in sorted(by.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {reason} | {count} |")
        lines.append("")
        lines.append("Top-3 skipped gates \u2014 most-affected tickers:")
        for item in agg["skip_stats"]["top3"]:
            tt = ", ".join(f"{t['ticker']}({t['count']})" for t in item["top_tickers"])
            lines.append(f"- **{item['reason']}** ({item['count']}): {tt}")
    lines.append("")

    lines.append("## 5. Cumulative two-week + comparison vs prior week")
    if cumulative is None:
        lines.append("")
        lines.append("(no prior week report.json found in parent dir)")
    else:
        prior_h = cumulative.get("prior_headline") or {}
        cum_h = cumulative.get("cumulative_headline") or {}
        lines.append("")
        lines.append("| Metric | Prior week | This week | Cumulative |")
        lines.append("|---|---:|---:|---:|")
        lines.append(
            f"| P&L | ${prior_h.get('actual_pnl', 0.0):.2f} | "
            f"${h['actual_pnl']:.2f} | "
            f"${cum_h.get('actual_pnl', 0.0):.2f} |"
        )
        lines.append(
            f"| Entries | {prior_h.get('total_entries', 0)} | "
            f"{h['total_entries']} | {cum_h.get('total_entries', 0)} |"
        )
        lines.append(
            f"| Win rate | {prior_h.get('win_rate', 0.0) * 100:.1f}% | "
            f"{h['win_rate'] * 100:.1f}% | "
            f"{cum_h.get('win_rate', 0.0) * 100:.1f}% |"
        )
    lines.append("")

    lines.append("## 6. Anomalies / data gaps / things to investigate")
    if not agg["anomalies"]:
        lines.append("(none)")
    else:
        for a in agg["anomalies"]:
            lines.append(f"- {a}")
    lines.append("")
    return "\n".join(lines)


def find_prior_week_json(parent_dir: Path, week_start: dt.date) -> Path | None:
    """Find the most recent week_<MONDAY>/report.json with monday <
    week_start. Returns the path or None."""
    if not parent_dir.exists():
        return None
    candidates: list[tuple[dt.date, Path]] = []
    for child in parent_dir.iterdir():
        if not child.is_dir() or not child.name.startswith("week_"):
            continue
        try:
            d = dt.date.fromisoformat(child.name[len("week_") :])
        except ValueError:
            continue
        if d >= week_start:
            continue
        rj = child / "report.json"
        if rj.exists():
            candidates.append((d, rj))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def build_cumulative(prior_path: Path | None, this_agg: dict) -> dict | None:
    if prior_path is None:
        return None
    try:
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    p_h = prior.get("headline") or {}
    t_h = this_agg["headline"]
    total_closed = (p_h.get("total_closed", 0) or 0) + t_h["total_closed"]
    total_entries = (p_h.get("total_entries", 0) or 0) + t_h["total_entries"]
    cum_pnl = (p_h.get("actual_pnl", 0.0) or 0.0) + t_h["actual_pnl"]
    p_wins = (p_h.get("win_rate", 0.0) or 0.0) * (p_h.get("total_closed", 0) or 0)
    t_wins = t_h["win_rate"] * t_h["total_closed"]
    cum_wins = p_wins + t_wins
    cum_wr = (cum_wins / total_closed) if total_closed else 0.0
    return {
        "prior_headline": p_h,
        "cumulative_headline": {
            "actual_pnl": cum_pnl,
            "total_entries": total_entries,
            "total_closed": total_closed,
            "win_rate": cum_wr,
        },
    }


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="v5.8.4 Saturday weekly report parser",
    )
    today = dt.date.today()
    default_monday = _last_monday(today)
    p.add_argument(
        "--week-start",
        default=default_monday.isoformat(),
        help=f"Monday (YYYY-MM-DD) of the trading week. Default: {default_monday.isoformat()}",
    )
    p.add_argument(
        "--out-dir",
        default="/home/user/workspace/backtest_v57x",
        help="Parent dir; results written to <out-dir>/week_<MONDAY>/",
    )
    p.add_argument(
        "--logs-dir",
        default=None,
        help="If set, read day_*.jsonl from here instead of pulling from Railway (offline mode).",
    )
    args = p.parse_args(argv)

    try:
        week_start = dt.date.fromisoformat(args.week_start)
    except ValueError:
        print(f"ERROR: --week-start must be YYYY-MM-DD, got {args.week_start!r}", file=sys.stderr)
        return 2

    parent = Path(args.out_dir)
    out_dir = parent / f"week_{week_start.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Source jsonl files
    if args.logs_dir:
        src = Path(args.logs_dir)
        print(f"[saturday_weekly_report] offline mode: reading {src}")
    else:
        print("[saturday_weekly_report] online mode: pulling Railway logs")
        written = fetch_week_logs(week_start, out_dir)
        print(f"  fetched: {written}")
        src = out_dir

    # Parse all day files for the trading week
    week_days = [(week_start + dt.timedelta(days=i)).isoformat() for i in range(5)]
    raw: list[dict] = []
    for day in week_days:
        path = src / f"day_{day}.jsonl"
        raw.extend(parse_jsonl_file(path))
    print(f"[saturday_weekly_report] raw lines parsed: {len(raw)}")

    events = parse_records(raw)
    print(f"[saturday_weekly_report] typed events: {len(events)}")

    agg = aggregate_week(events)

    prior = find_prior_week_json(parent, week_start)
    cumulative = build_cumulative(prior, agg)

    # Write json (machine-readable, used by next-week comparison)
    out_json = {
        "week_start": week_start.isoformat(),
        "headline": agg["headline"],
        "per_config": agg["per_config"],
        "per_exit_reason": agg["per_exit_reason"],
        "skip_stats": agg["skip_stats"],
        "anomalies": agg["anomalies"],
    }
    (out_dir / "report.json").write_text(
        json.dumps(out_json, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    md = render_report_md(week_start, agg, cumulative)
    (out_dir / "report.md").write_text(md, encoding="utf-8")

    print(f"[saturday_weekly_report] wrote {out_dir / 'report.md'}")
    print(f"[saturday_weekly_report] wrote {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
