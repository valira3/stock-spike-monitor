"""tools.trade_replay -- v7.93.0 -- pull `/api/trade_log` + render a
per-trade table with summary stats for a given trading day.

Built so the operator can answer "how did today / yesterday actually
go?" without SSH access to Railway's persistent volume. Hits the
existing `/api/trade_log` endpoint (auth via DASHBOARD_PASSWORD,
same as `tools/dashboard_monitor.py`) and writes:

  - A markdown summary to stdout
  - The same markdown to $GITHUB_STEP_SUMMARY (so the result is
    visible in the GHA workflow run page without downloading the
    artifact)
  - A `trade_replay.md` artifact file with the full content

Optional: when RAILWAY_API_TOKEN + RAILWAY_SERVICE_ID are set AND
INCLUDE_LOGS=1, also fetches a Railway log slice grepping for
[ENTRY] / [TRADE_CLOSED] / [V79-ORB-EXIT] / [SIGNAL-BUS-EMIT] /
[V79-MIRROR-RECV] lines and embeds it in the markdown for forensic
context.

## Environment

  DASHBOARD_BASE_URL   e.g. https://tradegenius.up.railway.app
  DASHBOARD_PASSWORD   same password as the running bot's login
  SINCE                YYYY-MM-DD; default = today (UTC)
  LIMIT                int, max 5000; default 500
  INCLUDE_LOGS         "1" to include Railway log slice
  GITHUB_STEP_SUMMARY  set by GHA; we append to it if present
  ARTIFACT_PATH        path to write the markdown file; default
                       trade_replay.md in the cwd

Exit code: 0 on success, 1 on auth / fetch failure.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tools.dashboard_monitor import DashboardClient

logger = logging.getLogger("trade_replay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Rendering helpers (pure functions -- tested independently)
# ---------------------------------------------------------------------------


def _fmt_dollar(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if n >= 0:
        return f"${n:,.2f}"
    return f"−${abs(n):,.2f}"


def _fmt_hold(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _r_multiple(row: dict) -> float | None:
    """R = (exit-entry)/(entry-stop) for long; flipped for short.

    Uses the ORIGINAL hard stop (the stop at entry, before any
    trail movement) as the risk denominator -- this is the
    classic R-multiple definition. v7.96.0-and-earlier used
    `effective_stop_at_exit` instead, which captures the trail's
    final state and inverts the denominator sign once the trail
    moves past breakeven. That produced negative R values on
    profitable trades whenever the trail tightened beyond entry
    (the NFLX SHORT on 2026-05-11 was -1.30R despite +$244 P&L --
    classic mis-signed denominator). v7.102.0 switches to the
    original-stop convention so Avg R is interpretable.

    Returns None when any required field is missing or stop==entry.
    """
    try:
        entry = float(row["entry_price"])
        exit_ = float(row["exit_price"])
    except (KeyError, TypeError, ValueError):
        return None
    # v7.102.0 -- prefer the hard_stop_at_exit (original protective
    # stop) as the risk denominator. The trail captures upside but
    # is NOT the trade's initial risk; classic R is defined against
    # initial risk. effective_stop_at_exit is kept as a last-resort
    # fallback for legacy rows that pre-date the trail snapshot.
    stop = row.get("hard_stop_at_exit") or row.get("effective_stop_at_exit")
    try:
        stop_v = float(stop)
    except (TypeError, ValueError):
        return None
    if abs(entry - stop_v) < 1e-6:
        return None
    side = str(row.get("side", "")).upper()
    if side == "LONG":
        return (exit_ - entry) / (entry - stop_v)
    if side == "SHORT":
        return (entry - exit_) / (stop_v - entry)
    return None


def _summary_stats(rows: list[dict]) -> dict:
    closes = [r for r in rows if "exit_price" in r and "pnl" in r]
    pnls = []
    for r in closes:
        try:
            pnls.append(float(r["pnl"]))
        except (TypeError, ValueError):
            continue
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    win_count = len(wins)
    loss_count = len(losses)
    rs = [r for r in (_r_multiple(c) for c in closes) if r is not None]
    avg_r = sum(rs) / len(rs) if rs else None
    return {
        "trades": len(closes),
        "total_pnl": total,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": (win_count / (win_count + loss_count)) if (win_count + loss_count) else None,
        "avg_r": avg_r,
        "biggest_win": max(pnls) if pnls else None,
        "biggest_loss": min(pnls) if pnls else None,
    }


def render_markdown(
    rows: list[dict],
    since: str,
    log_slice: str | None = None,
    generated_at: str | None = None,
) -> str:
    """Build the full markdown report. Pure: no I/O.

    v7.96.0 -- generated_at is embedded in the header so re-runs
    produce a unique markdown body even when the underlying trade
    data + log slice are identical. Without it the workflow's
    no-diff-skip logic looks (to the operator) like the run didn't
    happen at all.
    """
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).astimezone(ET).strftime(
            "%Y-%m-%d %H:%M:%S ET"
        )
    stats = _summary_stats(rows)
    lines: list[str] = []
    lines.append(f"# Trade replay — {since}")
    lines.append("")
    lines.append(f"_Generated at {generated_at}. Pulled {len(rows)} row(s) "
                 f"from `/api/trade_log` (closed-trade rows only counted "
                 f"in summary)._")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Trades (closed) | {stats['trades']} |")
    lines.append(f"| Total P&L | {_fmt_dollar(stats['total_pnl'])} |")
    if stats["win_rate"] is not None:
        lines.append(f"| Win rate | {stats['win_rate'] * 100:.1f}% "
                     f"({stats['wins']}W / {stats['losses']}L) |")
    if stats["avg_r"] is not None:
        lines.append(f"| Avg R-multiple | {stats['avg_r']:+.2f}R |")
    if stats["biggest_win"] is not None:
        lines.append(f"| Biggest winner | {_fmt_dollar(stats['biggest_win'])} |")
    if stats["biggest_loss"] is not None:
        lines.append(f"| Biggest loser | {_fmt_dollar(stats['biggest_loss'])} |")
    lines.append("")
    lines.append("## Per-trade detail")
    lines.append("")
    lines.append("| Ticker | Side | Sh | Entry | Exit | P&L | % | Hold | R | Reason |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- |")
    # Newest first for the table (the API returns newest-last).
    for r in reversed(rows):
        if "exit_price" not in r:
            continue
        r_mult = _r_multiple(r)
        r_str = f"{r_mult:+.2f}R" if r_mult is not None else "—"
        pnl_pct = r.get("pnl_pct")
        try:
            pct_str = f"{float(pnl_pct):+.2f}%"
        except (TypeError, ValueError):
            pct_str = "—"
        lines.append(
            "| {tkr} | {side} | {sh} | {ent} | {exi} | {pnl} | {pct} | {hold} | {r} | {rsn} |".format(
                tkr=r.get("ticker", "?"),
                side=r.get("side", "?"),
                sh=r.get("shares", "?"),
                ent=_fmt_dollar(r.get("entry_price")),
                exi=_fmt_dollar(r.get("exit_price")),
                pnl=_fmt_dollar(r.get("pnl")),
                pct=pct_str,
                hold=_fmt_hold(r.get("hold_seconds")),
                r=r_str,
                rsn=(r.get("reason") or "?")[:24],
            )
        )
    if log_slice:
        lines.append("")
        lines.append("## Railway log slice (forensic)")
        lines.append("")
        lines.append("```")
        lines.append(log_slice)
        lines.append("```")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _today_iso_et() -> str:
    return datetime.now(timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"FATAL: env var {name} is required", file=sys.stderr)
        sys.exit(1)
    return v


def _maybe_log_slice() -> str | None:
    if (os.environ.get("INCLUDE_LOGS", "").strip()) != "1":
        return None
    try:
        from tools.railway_log_tail import grep_logs, format_log_slice
    except Exception as exc:
        logger.warning("railway_log_tail import failed: %s", exc)
        return None
    try:
        # v7.95.0 -- limit raised 3000 -> 10000 so the slice reaches
        # back through a full trading day even when the report runs
        # after-hours. 3000 lines only covered ~2-3h of post-RTH
        # idle activity; a replay run at 19:00 ET wouldn't capture
        # any of the day's [TRADE_CLOSED] / [SIGNAL-BUS-EMIT] lines.
        rows = grep_logs(
            r"\[(ENTRY|TRADE_CLOSED|V79-ORB-EXIT|V10-FIRE|SIGNAL-BUS-EMIT|V79-MIRROR-RECV)\]",
            limit=10000, max_matches=200,
        )
    except Exception as exc:
        logger.warning("grep_logs raised: %s", exc)
        return None
    if not rows:
        return "(no matching log lines in recent Railway window)"
    try:
        return format_log_slice(rows, max_lines=80)
    except Exception as exc:
        logger.warning("format_log_slice raised: %s", exc)
        return None


def main() -> int:
    base_url = _require_env("DASHBOARD_BASE_URL")
    password = _require_env("DASHBOARD_PASSWORD")
    since = (os.environ.get("SINCE", "").strip()) or _today_iso_et()
    try:
        limit = max(1, min(5000, int(os.environ.get("LIMIT", "500"))))
    except (TypeError, ValueError):
        limit = 500
    artifact_path = (os.environ.get("ARTIFACT_PATH", "").strip()) or "trade_replay.md"

    client = DashboardClient(base_url, password)
    try:
        client.login()
        logger.info("login ok against %s", base_url)
    except Exception as exc:
        logger.error("login failed: %s", exc)
        return 1
    try:
        payload = client.get_json(
            f"/api/trade_log?limit={limit}&since={since}&portfolio=paper"
        )
    except Exception as exc:
        logger.error("trade_log fetch failed: %s", exc)
        return 1
    rows = list(payload.get("rows") or [])
    logger.info("fetched %d trade_log rows for %s", len(rows), since)

    log_slice = _maybe_log_slice()
    md = render_markdown(rows, since=since, log_slice=log_slice)

    # stdout
    sys.stdout.write(md)
    sys.stdout.flush()

    # artifact file
    try:
        with open(artifact_path, "w", encoding="utf-8") as fh:
            fh.write(md)
        logger.info("wrote artifact %s", artifact_path)
    except OSError as exc:
        logger.warning("artifact write failed: %s", exc)

    # GHA step summary
    step_sum = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if step_sum:
        try:
            with open(step_sum, "a", encoding="utf-8") as fh:
                fh.write(md)
            logger.info("appended to GITHUB_STEP_SUMMARY")
        except OSError as exc:
            logger.warning("step summary write failed: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
