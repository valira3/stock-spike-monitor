"""simulator.expectations -- declarative "what should happen" rules.

A rule is a triple (matcher, expectation, severity). The anomaly
detector evaluates every rule against every day's index row + the
simulator's result; mismatches become anomalies.

Example rule set:

    RULES = [
      Rule(matcher={"category": "gap_up_1_5pct"},
           expect={"max_entries": 0},
           severity="WARN",
           why="ORB_SKIP_GAP_ABOVE_PCT=1.5 should block any entry"),
      Rule(matcher={"category": "vix_high"},
           expect={"max_entries": 0},
           severity="ERROR",
           why="ORB_SKIP_VIX_ABOVE=25 should kill the day"),
      Rule(matcher={"category": "range_compression"},
           expect={"max_entries": 0},
           severity="WARN",
           why="ORB_RANGE_MIN_PCT=0.8 should reject narrow OR windows"),
    ]

The matcher dict is AND-joined over the day index row + computed
metadata; the expect dict is AND-joined over the simulator state.

Both sides understand operator suffixes: `__lt`, `__gt`, `__le`,
`__ge`, `__eq` (default), `__in`, `__contains`. So you can write
``matcher={"spy_gap_pct__gt": 1.5}`` even if the day index does not
have a precomputed "gap_up" category.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ----- Rule definitions -------------------------------------------------


@dataclass
class Rule:
    matcher: Dict[str, Any] = field(default_factory=dict)
    expect: Dict[str, Any] = field(default_factory=dict)
    severity: str = "WARN"   # "WARN" | "ERROR" | "INFO"
    why: str = ""
    name: str = ""

    def matches(self, day_row: Dict[str, Any]) -> bool:
        return _all_match(self.matcher, day_row)

    def evaluate(self, day_result: Dict[str, Any]) -> Optional["RuleFailure"]:
        """Returns a RuleFailure if the day_result violates `expect`,
        else None."""
        # The day_result keys we use to evaluate.
        ctx = {
            "n_entries": len(day_result.get("entries", [])),
            "n_exits": len(day_result.get("exits", [])),
            "max_entries": len(day_result.get("entries", [])),
            "min_entries": len(day_result.get("entries", [])),
            "telegram_count": day_result.get("telegram_count", 0),
            "fmp_count": day_result.get("fmp_count", 0),
            "yahoo_count": day_result.get("yahoo_count", 0),
            "alpaca_orders": len(day_result.get("alpaca_orders", [])),
            "realized_pl_total": day_result.get("realized_pl_total", 0.0),
            "open_at_eod": len(day_result.get("open_at_eod", [])),
        }
        # Special-case the bounded asserts (max_X / min_X) so the rule
        # author can write expect={"max_entries": 0} without the __le
        # suffix.
        ok, why_fail = _evaluate_expect(self.expect, ctx)
        if ok:
            return None
        return RuleFailure(
            rule_name=self.name or repr(self.matcher),
            why=self.why,
            why_fail=why_fail,
            severity=self.severity,
        )


@dataclass
class RuleFailure:
    rule_name: str
    why: str
    why_fail: str
    severity: str


# ----- The default ruleset ---------------------------------------------
#
# These mirror the gates documented in CLAUDE.md Keystone section. When
# a day matches a category, the rule says what the algorithm "promises"
# should happen. Failures point to either:
#   - the data being misclassified (false positive)
#   - the algorithm letting something slip (false negative)
#   - a regression introduced since the rule was written

# Important: simulator categories are SPY-day-level proxies. The bot's
# gates run per-ticker (each ticker's gap vs its own PDC; each ticker's
# OR range). So a day where SPY gapped 1.5% does NOT mean AAPL did. The
# gap/range rules below are therefore INFO-severity (operator-eye, not
# auto-fail) -- they highlight days worth investigating when the bot
# fired entries on the same date SPY hit a category. Promote any rule
# to WARN/ERROR after adding per-ticker classification in corpus_index.
#
# Rules at ERROR severity are SEV-1 invariants that should hold
# regardless of corpus classification accuracy.

DEFAULT_RULES: List[Rule] = [
    Rule(
        name="gap_skip",
        matcher={"categories__contains": "gap_up_1_5pct"},
        expect={"max_entries": 0},
        severity="INFO",
        why="SPY gapped >=1.5% up; the per-ticker ORB_SKIP_GAP_ABOVE_PCT "
            "may or may not have blocked the entry depending on the firing "
            "ticker's own gap. Investigate any fires.",
    ),
    Rule(
        name="gap_down_skip",
        matcher={"categories__contains": "gap_down_1_5pct"},
        expect={"max_entries": 0},
        severity="INFO",
        why="SPY gapped <=-1.5% down; same per-ticker caveat as gap_skip.",
    ),
    Rule(
        name="vix_kill",
        matcher={"categories__contains": "vix_high"},
        expect={"max_entries": 0},
        severity="ERROR",
        why="ORB_SKIP_VIX_ABOVE=25 is a day-level gate (uses prior-day VIX "
            "close). When this trips, the bot SHOULD reject every entry.",
    ),
    Rule(
        name="range_floor",
        matcher={"categories__contains": "range_compression"},
        expect={"max_entries": 0},
        severity="INFO",
        why="SPY OR-range below 0.4%. Each ticker is gated on its own OR "
            "range (ORB_RANGE_MIN_PCT=0.8); a fire on AAPL with a 1%+ range "
            "is legal even on a tight-SPY day.",
    ),
    Rule(
        name="range_ceiling",
        matcher={"categories__contains": "range_expansion"},
        expect={"max_entries": 0},
        severity="INFO",
        why="SPY OR-range above 1.6%. Per-ticker caveat as range_floor.",
    ),
    Rule(
        name="halt_safety",
        matcher={"categories__contains": "halt_present"},
        expect={"max_entries": 1},
        severity="INFO",
        why="A trading halt on any RTH bar; expect at most 1 entry "
            "(volume=0 should defeat the v10 admission filters).",
    ),
    Rule(
        name="no_carry_over",
        matcher={},  # every day
        expect={"open_at_eod": 0},
        severity="ERROR",
        why="The EOD flush at 15:57 ET must close all positions; carryover "
            "is a SEV-1 invariant violation regardless of the day's regime.",
    ),
    # alpaca_order_count_matches_entries was removed: the simulator's
    # runner.py calls live_runtime.check_entry() directly to test the
    # admission decision; it does NOT dispatch through engine.scan ->
    # callbacks.execute_entry -> broker.orders.execute_breakout. So
    # zero broker orders for every entry is a simulator scope limitation
    # rather than a bot bug. Re-enable this rule only after the runner
    # is extended to drive the full executor path.
]


# ----- Evaluator helpers -----------------------------------------------


def _all_match(matcher: Dict[str, Any], row: Dict[str, Any]) -> bool:
    for key, expected in matcher.items():
        if not _check_one(key, expected, row):
            return False
    return True


def _check_one(key: str, expected: Any, row: Dict[str, Any]) -> bool:
    op = "eq"
    actual_key = key
    for suffix in ("__lt", "__gt", "__le", "__ge", "__eq", "__in", "__contains"):
        if key.endswith(suffix):
            op = suffix[2:]
            actual_key = key[: -len(suffix)]
            break
    actual = row.get(actual_key)
    return _compare(actual, op, expected)


def _compare(actual, op: str, expected) -> bool:
    try:
        if op == "eq":
            return actual == expected
        if op == "lt":
            return actual is not None and actual < expected
        if op == "gt":
            return actual is not None and actual > expected
        if op == "le":
            return actual is not None and actual <= expected
        if op == "ge":
            return actual is not None and actual >= expected
        if op == "in":
            return actual in expected
        if op == "contains":
            if isinstance(actual, (list, set, tuple)):
                return expected in actual
            if isinstance(actual, str):
                return expected in actual
            return False
    except Exception:
        return False
    return False


def _evaluate_expect(expect: Dict[str, Any], ctx: Dict[str, Any]):
    """Returns (ok: bool, why_fail: str)."""
    for key, expected in expect.items():
        if key == "max_entries":
            if ctx["n_entries"] > expected:
                return False, f"expected at most {expected} entries, got {ctx['n_entries']}"
        elif key == "min_entries":
            if ctx["n_entries"] < expected:
                return False, f"expected at least {expected} entries, got {ctx['n_entries']}"
        elif key == "max_exits":
            if ctx["n_exits"] > expected:
                return False, f"expected at most {expected} exits, got {ctx['n_exits']}"
        elif key == "open_at_eod":
            if ctx["open_at_eod"] != expected:
                return False, f"expected {expected} positions open at EOD, got {ctx['open_at_eod']}"
        elif key == "alpaca_orders_within_one_of_entries":
            # entries + exits should roughly equal order count.
            entries = ctx["n_entries"]
            orders = ctx["alpaca_orders"]
            if entries > 0 and orders == 0:
                return False, f"saw {entries} entries but 0 broker orders"
        else:
            # Generic compare against ctx (allows future expansion).
            if ctx.get(key) != expected:
                return False, f"{key}: expected {expected}, got {ctx.get(key)}"
    return True, ""


# ----- Public API ------------------------------------------------------


def evaluate(day_row: Dict[str, Any], day_result: Dict[str, Any],
             rules: Optional[List[Rule]] = None) -> List[RuleFailure]:
    """Return all rule failures for this day."""
    rules = rules if rules is not None else DEFAULT_RULES
    failures: List[RuleFailure] = []
    for rule in rules:
        if not rule.matches(day_row):
            continue
        fail = rule.evaluate(day_result)
        if fail is not None:
            failures.append(fail)
    return failures
