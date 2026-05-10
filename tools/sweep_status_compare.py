"""Render sweep-status payload as a comparison table.

Usage
-----
    python tools/sweep_status_compare.py [trigger_name]

If trigger_name is omitted, all status files under origin/sweep-status:status/
are listed. Otherwise reads status/<trigger_name>.json and prints:

  - Phase + timing
  - Per-variant net_pnl, entries, wins, losses, win_rate_pct
  - Annualized projection (252 / days_ok * net_pnl)
  - Delta vs the first variant (treated as baseline)

Reads from `git show origin/sweep-status:status/<name>.json` so no R2
or filesystem dependency. Run after `git fetch origin sweep-status`.

Designed to be invoked once sweep-status `phase=done` event lands.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: rc={p.returncode} stderr={p.stderr}"
        )
    return p.stdout


def list_triggers() -> list[str]:
    out = _git("ls-tree", "-r", "--name-only",
               "origin/sweep-status", "status/")
    names = []
    for line in out.splitlines():
        if line.startswith("status/") and line.endswith(".json"):
            names.append(line[len("status/"):-len(".json")])
    return sorted(set(names))


def load_status(trigger_name: str) -> dict[str, Any]:
    body = _git("show", f"origin/sweep-status:status/{trigger_name}.json")
    return json.loads(body)


def annualize(net_pnl: float | None, days_ok: int | None) -> str:
    if net_pnl is None or not days_ok:
        return "n/a"
    annual = net_pnl * 252 / max(days_ok, 1)
    return f"${annual:,.0f}/yr"


def render(trigger_name: str, status: dict[str, Any]) -> None:
    print(f"\n=== {trigger_name} ===")
    print(f"phase:           {status.get('phase')}")
    print(f"started_at:      {status.get('started_at')}")
    print(f"updated_at:      {status.get('updated_at')}")
    print(f"variants_total:  {status.get('variants_total')}")
    print(f"completed:       {status.get('variants_completed')}")
    print(f"succeeded:       {status.get('variants_succeeded')}")
    print(f"failed:          {status.get('variants_failed')}")
    if status.get("variants_resumed"):
        print(f"resumed (R2):    {status['variants_resumed']}")
    if status.get("error"):
        print(f"error:           {status['error']}")

    results = status.get("results") or []
    if not results:
        return

    rows = []
    baseline_pnl = None
    for r in results:
        s = r.get("summary") or {}
        net_pnl = s.get("net_pnl")
        days_ok = s.get("days_ok")
        wr = s.get("win_rate_pct")
        rows.append({
            "vid":      r.get("vid", "?"),
            "rc":       r.get("rc", "?"),
            "resumed":  "*" if r.get("resumed") else "",
            "net_pnl":  net_pnl,
            "entries":  s.get("entries"),
            "wins":     s.get("wins"),
            "losses":   s.get("losses"),
            "wr":       wr,
            "days":     days_ok,
            "annual":   annualize(net_pnl, days_ok),
        })
        if baseline_pnl is None and net_pnl is not None:
            baseline_pnl = net_pnl

    print()
    fmt_h = "{vid:<46} {rc:>3} {res:<3} {pnl:>10} {ent:>5} {wr:>6} {days:>4}  {annual:>14}  {delta}"
    fmt_r = "{vid:<46} {rc:>3} {res:<3} {pnl:>10} {ent:>5} {wr:>6} {days:>4}  {annual:>14}  {delta}"
    print(fmt_h.format(
        vid="vid", rc="rc", res="res",
        pnl="net_pnl", ent="ents", wr="wr%", days="days",
        annual="annual",
        delta="delta vs baseline",
    ))
    print("-" * 120)
    for row in rows:
        delta_str = ""
        if row["net_pnl"] is not None and baseline_pnl is not None:
            delta = row["net_pnl"] - baseline_pnl
            delta_annual = annualize(delta, row["days"])
            delta_str = f"{delta:+8.2f} ({delta_annual})"
        print(fmt_r.format(
            vid=str(row["vid"])[:46],
            rc=str(row["rc"]),
            res=row["resumed"],
            pnl=f"${row['net_pnl']:.2f}" if row["net_pnl"] is not None else "n/a",
            ent=str(row["entries"]) if row["entries"] is not None else "n/a",
            wr=f"{row['wr']:.1f}" if row["wr"] is not None else "n/a",
            days=str(row["days"]) if row["days"] is not None else "n/a",
            annual=row["annual"],
            delta=delta_str,
        ))


def main(argv: list[str]) -> int:
    try:
        _git("fetch", "-q", "origin", "sweep-status")
    except RuntimeError as e:
        print(f"warning: fetch failed: {e}", file=sys.stderr)

    if len(argv) >= 2:
        name = argv[1]
        status = load_status(name)
        render(name, status)
        return 0

    triggers = list_triggers()
    if not triggers:
        print("no sweep-status entries on origin/sweep-status:status/")
        return 0
    print(f"found {len(triggers)} trigger(s):")
    for t in triggers:
        print(f"  {t}")
    print("\nrun: python tools/sweep_status_compare.py <trigger_name>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
