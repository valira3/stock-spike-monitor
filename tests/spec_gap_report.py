"""Walk tests/test_tiger_sovereign_spec.py and print spec_gap markers.

Output is a table grouped by the closing PR. This is the precise to-do
list driving v5.13.0 PRs 2–6.

Usage::

    python tests/spec_gap_report.py
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = REPO_ROOT / "tests" / "test_tiger_sovereign_spec.py"


def _extract_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def collect_gaps(path: Path) -> list[tuple[str, str, str]]:
    """Return list of (pr, rule_id, test_name) tuples."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            # @pytest.mark.spec_gap("PR-N", "rule-id")
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "spec_gap"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "mark"
            ):
                if len(deco.args) >= 2:
                    pr = _extract_string(deco.args[0])
                    rule = _extract_string(deco.args[1])
                    if pr and rule:
                        out.append((pr, rule, node.name))
    return out


def render_table(gaps: list[tuple[str, str, str]]) -> str:
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pr, rule, test in gaps:
        grouped[pr].append((rule, test))
    lines: list[str] = []
    lines.append("Tiger Sovereign spec gaps (PR 1 inventory)")
    lines.append("=" * 60)
    if not gaps:
        lines.append("(no spec_gap markers found)")
        return "\n".join(lines)
    total = 0
    for pr in sorted(grouped):
        rules = sorted(grouped[pr])
        lines.append("")
        lines.append(f"{pr} — {len(rules)} rule(s)")
        lines.append("-" * 60)
        for rule, test in rules:
            lines.append(f"  {rule:<22}  {test}")
            total += 1
    lines.append("")
    lines.append(f"TOTAL: {total} gap(s) across {len(grouped)} PR(s)")
    return "\n".join(lines)


def main() -> int:
    if not TEST_FILE.exists():
        print(f"error: {TEST_FILE} not found", file=sys.stderr)
        return 2
    gaps = collect_gaps(TEST_FILE)
    print(render_table(gaps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
