---
name: qcheck
description: Skeptical staff-engineer review of every changed file in the current session against CLAUDE.md + tasks/lessons.md. Invoke before opening a PR or committing.
---

# qcheck — skeptical staff-engineer code review

Adapted from vscarpenter/claude-code-build-system's `/qcheck`. The default posture is **"this is not ready to ship until proven otherwise"**.

## How to work

1. Run `git status` to see staged, unstaged, and untracked files.
2. Run `git diff` and `git diff --staged` to see the actual changes.
3. Read `CLAUDE.md` for project conventions.
4. Read `tasks/lessons.md` for accumulated gotchas.
5. For every changed file, evaluate against the checks below.

## What to check

### 1. CLAUDE.md mandatory PR rules

- `BOT_VERSION` in `bot_version.py` == `trade_genius.py` == top CHANGELOG heading (only required for non-`[skip-version]` PRs).
- New `## vX.Y.Z` heading at TOP of `CHANGELOG.md`.
- `ARCHITECTURE.md` updated if behavior changes.
- No literal em-dashes (U+2014) in `.py` source. The `.claude/hooks/format-edits.sh` hook catches this but verify staged changes too.
- No `scrape|crawl|scraping|crawling` anywhere.
- `#h-tick` never hidden; health-pill count never dropped.
- Telegram mobile code-block: ≤34 chars per line.

### 2. UI cross-tab parity (added v8.3.18)

If the diff touches `dashboard_static/`, audit BOTH renderer paths:
- Main: `index.html` + `renderV10DayStatus()` / `renderPositions()` / `renderTrades()` / `renderV10ActivityFeed()` in IIFE-1 (app.js ~lines 320-520, 600-740, 6190-6260).
- Val/Gene: `execSkeleton()` HTML + `renderV10PerPortfolio()` / `renderExecTrades()` in IIFE-2 (app.js ~lines 3870-4750).

If the section order changed on one tab, verify the other tabs match per the canonical order in CLAUDE.md `body.v10-live` rule.

### 3. Strategy-code correctness

If the diff touches `orb/`, `engine/`, `executors/`, `broker/`:
- New filters positioned AFTER the FSM `can_enter` check (so they don't burn trade-counter capacity on filtered entries).
- Session-scoped counters reset in `start_new_session()` and exposed via `snapshot()`.
- VWAP calculations use the signal-bar's last 1m bucket (`session_vwap_at(sig.bucket + 4)` parity).
- All thresholds default such that `0 = filter off` (not "auto-default to some value").
- New env levers added to `orb/live_runtime.py:_build_config_from_env` with v13-report-validated defaults.

### 4. Tier-A guards (CI-equivalent)

- `python scripts/run_ci.py` passes (fast lane: 1,173+ strategy tests, ~6 s).
- No new TODO comments without a tracking reference.
- No `print()` debug calls, no `breakpoint()`.
- No `--no-verify`, no `--admin` on `gh pr merge` in scripts.

### 5. tasks/lessons.md gotchas

Cross-check the diff against every entry under the current month in `tasks/lessons.md`. The top three traps as of 2026-05:
- Bar/corpus data must not be tracked.
- Dockerfile `COPY` lines must match deleted modules.
- Two em-dash forms coexist; verify both are searched.

### 6. Reversibility check

For any new file, ask:
- Will deleting this require fixing imports elsewhere? List them.
- Does this re-introduce a retired pattern (Tiger Sentinel, Permit Matrix, Volume Bucket, eye_of_tiger)? If yes, justify.

## Output format

```
## qcheck review

### Files reviewed
- path/to/file1.py
- path/to/file2.js

### Findings

#### Critical (must fix before commit)
- file:line, issue, recommended fix

#### Important (should fix before commit)
- file:line, issue, recommended fix

#### Nits (optional polish)
- file:line, issue, recommended fix

### Definition of Done
- [x] run_ci.py passes
- [x] No literal em-dashes in changed .py
- [x] CLAUDE.md mandatory PR rules satisfied
- [x] tasks/lessons.md gotchas not retriggered
- [ ] UI cross-tab parity verified  (n/a -- no UI changes)

### Verdict
Ready to commit / Needs changes / Blocked on X.
```

## Tone

Skeptical staff engineer, not a cheerleader. Find what is wrong; don't congratulate what is right. "This is fine" is never an acceptable finding — be specific or move on with a one-liner.
