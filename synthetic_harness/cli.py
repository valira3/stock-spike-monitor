"""CLI: python -m synthetic_harness <list|record|replay|diff>."""
from __future__ import annotations

import argparse
import sys

from synthetic_harness.scenarios import SCENARIOS, list_scenarios
from synthetic_harness.runner import (
    record_scenario,
    replay_scenario,
)


def _select(names_arg: str | None) -> list[str]:
    if not names_arg:
        return list_scenarios()
    requested = [n.strip() for n in names_arg.split(",") if n.strip()]
    out = []
    for n in requested:
        if n not in SCENARIOS:
            print(f"unknown scenario: {n}", file=sys.stderr)
            sys.exit(2)
        out.append(n)
    return out


def cmd_list(_args) -> int:
    for name in list_scenarios():
        sc = SCENARIOS[name]
        print(f"  {name:<35} {sc.description}")
    print(f"\n  {len(SCENARIOS)} scenarios")
    return 0


def cmd_record(args) -> int:
    names = _select(args.scenarios)
    for name in names:
        path = record_scenario(name)
        print(f"  RECORDED  {name}  ->  {path.name}")
    print(f"\n  {len(names)} scenarios recorded")
    return 0


def cmd_replay(args) -> int:
    names = _select(args.scenarios)
    failures = []
    for name in names:
        ok, diff = replay_scenario(name)
        marker = "+" if ok else "X"
        print(f"  {marker}  {name}")
        if not ok:
            failures.append((name, diff))
    if failures:
        print()
        for name, diff in failures:
            print(f"--- {name} ---")
            print(diff)
            print()
    passed = len(names) - len(failures)
    print(f"\n  {passed} passed, {len(failures)} failed of {len(names)}")
    return 0 if not failures else 1


def cmd_diff(args) -> int:
    name = args.scenario
    if name not in SCENARIOS:
        print(f"unknown scenario: {name}", file=sys.stderr)
        return 2
    ok, diff = replay_scenario(name)
    if ok:
        print(f"  {name}: OK (no diff)")
        return 0
    print(diff)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m synthetic_harness",
        description="TradeGenius synthetic harness CLI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list all scenarios")
    p_list.set_defaults(fn=cmd_list)

    p_rec = sub.add_parser("record", help="record golden outputs")
    p_rec.add_argument("--scenarios", default=None,
                       help="comma-separated names (default: all)")
    p_rec.set_defaults(fn=cmd_record)

    p_rep = sub.add_parser("replay", help="replay and compare to goldens")
    p_rep.add_argument("--scenarios", default=None,
                       help="comma-separated names (default: all)")
    p_rep.set_defaults(fn=cmd_replay)

    p_diff = sub.add_parser("diff", help="show diff for one scenario")
    p_diff.add_argument("scenario")
    p_diff.set_defaults(fn=cmd_diff)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
