# Golden ledger snapshots

Each `*.golden.json` file in this directory is the canonical ledger
produced by a named scenario when driven through `orb.live_runtime`.
The corresponding test in `tests/strategy/test_orb_golden_ledger.py`
re-runs each scenario, regenerates the ledger, and **diffs against
the checked-in baseline**.

If a v10 code change alters the ledger (admit/exit
timing/price/size/reason), the test fails and prints the diff. The
author must then either:

1. **Acknowledge the change is intentional** -- regenerate the
   golden via `python -m pytest tests/strategy/test_orb_golden_ledger.py
   --regen-goldens` (or by deleting the file and re-running). Commit
   the new golden alongside the code change. This makes the
   intent-of-change visible in the PR diff.

2. **Fix the regression** -- the test caught an unintentional drift.

Schema: each golden is a JSON list of event dicts shaped like:

```
[
  {"kind": "session_start", "date": "2026-01-15"},
  {"kind": "admit", "ticker": "AAPL", "side": "long", "shares": 742,
   "price": 101.0, "stop": 99.45, "target": 104.87},
  {"kind": "exit",  "ticker": "AAPL", "reason": "target", "price": 104.87}
]
```

Fields are rounded to 4 decimals for stability across float platforms.
