# Auto Agentic Rule Framework

Operating rules for autonomous, quality work in this session and future sessions. Established 2026-05-10 across the v7.6.0 → ORB optimization workstream. Each rule cites the directive that established it.

---

## OVERARCHING META RULE

**0. Manager Agent oversight.** Whenever a multi-step workstream is running, a Manager Agent operates in parallel with the primary executor and oversees ongoing rule compliance. The Manager Agent:
- Knows every rule in this framework
- Periodically reviews assistant state vs the rules: timed updates (#5/#27), keep-Val-aware (#28), hang-checks (#29), auto-merge cadence (#3), commit hygiene, multi-agent for surprising results (#22), look-ahead audits on new levers (#7b), compounding default (#11b), iPhone-narrow format (#17/#27), CT timezone (#20), step counter (#27), and the rest.
- Works alongside specialized subagents (research, audit, code review). When deviations are found, it either (a) auto-corrects trivial issues (e.g. nudge a stale commit, trigger a missed PR merge), (b) surfaces the deviation in chat as an iPhone-narrow alert, or (c) for significant deviations spawns a corrective subagent.
- Runs for the lifetime of the workstream. Heartbeat cadence: every 5–10 minutes during active work, plus on-demand when surprising results arrive.
- Reports compliance status alongside the primary work — never silently.

Operationally implemented as: (1) a lightweight Monitor that polls git/PR/time-since-last-message state every ~2 minutes and emits `DEVIATION:` lines on rule violations, (2) a periodic spawned subagent that does deeper compliance review every 10–15 minutes.
*Established by: "Whenever we are running, we should have a manager agent that oversees the implementation of all the rules on ongoing basis and works with others to autocorrect any deviations"*

---

## I. Autonomy

**1. Run autonomously.** Don't wait for human action between sub-tasks. Keep advancing through the work in the loop.
*Established by: "Do everything automatically", "Make the process completely hands off"*

**2. Loop until complete.** Chain phase → phase → phase without pausing. Stop only on (a) deliverable shipped, (b) hard external blocker, or (c) ambiguous decision worth one well-formed question.
*Established by: "Don't get stuck waiting for my action", "keep looping until complete"*

**3. Always auto-commit and auto-merge** PRs once CI is green. No "should I merge?" questions. Pattern: open PR → wait CI → merge on green.
*Established by: "Auto merge in the future", "Always auto commit"*

**4. Don't wait passively.** Monitor signals (CI status, PR webhooks, sweep results) actively so we know progress in real-time. Use persistent monitors and event pollers.
*Established by: "Make sure to monitor all signals so that we don't wait for nothing"*

**5. Monitor / status reports** every 3 minutes minimum during long-running work. Show top-5 results so far + total variants run + mark in-progress with `*`.
*Established by: "Reduce manager check in time to 3 mins", "running tally of the best 5 results so far with key KPIs"*

---

## II. Quality / scientific rigor

**6. Sanity-check before declaring.** When numbers look extraordinary, verify math/logic before celebrating. Spawn an audit subagent if needed. Past examples: caught +$318k/yr as phantom-leverage; caught compounding's volatility drag.
*Established by: "Double check the math/logic - those P&L seem too good to be true", "Make sure to run some sanity checks in between to ensure that we catch unrealistic scenarios"*

**7. Audit-driven realism.** Phantom leverage, look-ahead bias, over-leverage, fee/slippage gaps — all need to be hunted before relying on backtest numbers. Realistic execution constraints baked into the harness.
*Established by: "Update the strategy to be realistic"*

**7b. No look-ahead, ever.** A backtest may NEVER use information that would not have been available in real time at the moment of the decision. Concretely:
- A signal/filter computed at time T may only consume data with timestamps ≤ T.
- Day-skip filters must use data available BEFORE the day's first entry would fire — prior-day close, overnight futures, pre-market quotes, prior session ATR/RVOL/etc. Same-day OR-derived signals may gate entries that fire AFTER the OR window closes, but never before.
- Indicators that are computed once per day (e.g. session VWAP, daily range) must be reset at session open and built incrementally, never end-of-day-back-dated.
- Forward-fill from "next available bar" is forbidden. If data is missing at T, the strategy sees nothing — not the next print.
- Cross-validation/STRIDE samples must not leak across train/test boundaries (a feature engineered on day D may not implicitly use day D+1 data).

Every new lever ships with a one-line claim of which timestamp's data it consumes. Audits explicitly test: "if I removed all data after T, does this signal compute the same value?" If the answer is no, it's look-ahead.
*Established by: "remember that when we are running backtests, we can not know the future. So the run should never assume any indicators that would not be known at the time"*

**8. Industry research-grounded.** New levers should come from peer-reviewed or community-replicated literature (ATR stops, ADX, VWAP, Kelly sizing, etc.) not handwaved hypotheses. Spawn a research subagent for this.
*Established by: "Identify any other potential levers we can use (again refer to industry research)"*

**9. Local-first, GHA-confirm.** Cheap fast local screens (~5s/variant) eliminate weak candidates BEFORE expensive cloud sweeps (~10min/variant). Only top-N go to GHA for the official record.
*Established by: "Before doing full sweeps, run quick local tests to eliminate highly unlikely candidates"*

**10. Patch all bugs found in audit** before continuing. Don't carry known issues forward.
*Established by: "Make sure that everything is patched"*

**11. Account for compounding** in P&L projections. Position sizes scale with running balance day-to-day. Report both arithmetic ("constant base") and geometric ("compounding") returns.
*Established by: "Are we accounting for compounding? Ie, all losses and wins compound to the next day's base portfolio"*

**11b. Compounding is the DEFAULT.** Every variant comparison, ranking, and screen runs with `ORB_COMPOUND_DAILY=1` set as the baseline. Constant-base numbers are reported as a secondary frame only. Position sizing scales with the running account balance so a losing streak naturally de-leverages and a winning streak naturally compounds — this is the actual deployable behavior, not the arithmetic projection. Any leaderboard, top-N table, or "deploy this" recommendation MUST be ranked by compounded P&L unless explicitly noted otherwise.
*Established by: "Apply compounding rules to all variants (and add this to the rules so we always follow it)"*

---

## III. Risk management

**12. Honor the risk envelope** the user specifies (e.g. $500/day, $1500/day, $2000/day). Treat as a hard constraint not a soft target. Worst-day must stay within cap (modulo small slippage overshoot).
*Established by: "We don't want to be in position to lose more than $500/day"*

**13. Validate stability, not just headline P&L.** "Most stable in delivery of optimal value." Top-N variants ranked by stability metrics (Sharpe, max DD, per-ticker concentration, % profit days) NOT raw P&L alone.
*Established by: "validate which ones would be the most stable in delivery of optimal value"*

---

## IV. Reporting

**14. Show absolute revenue** (e.g. $X over N days, projected $Y/yr, ROI%) in addition to deltas vs baseline. Both frames in every report.
*Established by: "show absolute revenue, not just delta", "Always show totals in absolute $ revenue as well as increments"*

**15. Show deltas vs production baseline** (e.g. Δ vs −$20,771/yr prod) so deployment decision-makers see incremental impact.
*Established by: "make sure that the report shows absolute revenue, not just delta" (implying both)*

**16. Top-5 deliverable**: when iteration completes, deliver the top 5 variants ranked by combined P&L × stability score with full KPIs.
*Established by: "Once done, present the best 5 variants"*

**17. iPhone-friendly format** for periodic status reports: narrow (~36 chars wide), stacked rows, scannable hierarchy. No wide tables in monitor outputs.
*Established by: "Make sure that it's in a format easily consumable on an iPhone"*

**18. Mark in-progress runs with `*`** so the user sees what's still cooking.
*Established by: "any current ones indicated by asterisk"*

**19. Show the full report inline** in chat when a Final Report is published, not just a "see the file" reference.
*Established by: "Show the report here as well when done"*

**20. Times in Central Time.** All future report timestamps + ETAs in CT.
*Established by: "I am in central time zone for future reports"*

**27. Periodic timed progress updates.** During long-running work (sweeps, GHA matrices, multi-step compute jobs), surface a progress update every ~5 minutes with step counter, elapsed/expected/ETA in CT, and overall-loop ETA. **Wrap to ≤34 chars per line so it fits on an iPhone** — narrow stacked rows, no wide tables. Format:

```
Progress: <step name>
Step:     3/20
Elapsed:  5m of ~55m
Step ETA: 10:30am CT
Loop:     3/12 done
Full ETA: 12:45pm CT
```

Required fields:
- **Step counter** (`Step: N/M`) when the work is part of a known multi-step loop (research → audit → screen → cross-val → report → ...). If M is unknown, say so explicitly (`N/?`).
- **This step's elapsed/expected** and **Step ETA** in CT.
- **Loop counter** (`Loop: K/L done`) and **Full ETA** in CT for the entire phase. If the loop is open-ended, state it and give a soft cap.
- **iPhone-friendly width**: every line ≤ ~34 chars. Don't put multiple fields on one line. Stack vertically.

Add brief context if helpful (variants completed, top-3 so far, current sub-step) — but keep each line narrow. Don't spam if nothing meaningful has changed — but the user should never wonder "is it still running?" or "how much further?" without a recent timestamped signal.
*Established by: "Periodically (every 5 mins) provide progress update in time. Ie, processing 5 mins out of 55mins expected. ETA 10:30am CT" + "Show step vs total steps expected (ie, step 1/20). Also add an estimate for a full completion of the overall loop" + "Make sure to wrap text in progress reporting so it fits on an iPhone"*

**28. Keep Val aware.** The user's name is Val. Outbound progress communication is required at minimum every 5 minutes during active long-running work — even when rule #27's elapsed/ETA format doesn't apply (e.g. ETA unknown, multiple parallel tasks). At minimum: a one-line status confirming the current step, last result, and what's running. Silence longer than 5 minutes during active work is a process bug, not a feature. If genuinely idle (no work in flight, awaiting user direction), say so explicitly — silence is never an acceptable status.
*Established by: "Keep Val aware of the progress. At least every 5 mins"*

**29. Hang-check every 2 minutes.** Long-running processes (background bashes, GHA sweeps, local screens, monitors, subagents) get a liveness check every ~2 minutes: PID alive? output file growing? log timestamps advancing? CI status changing? Webhook events arriving? Detect hung processes early — better to catch a stalled monitor at the 2-minute mark than to find out at the 30-minute timeout. If a process is suspected hung: investigate (check the output file, ps, signal trace), don't passively wait.
*Established by: "Check all long running processes every 2 mins to make sure nothing is stuck"*

---

## V. Cost / infrastructure

**21. Prefer GitHub Actions over Railway** for the iteration cadence. GHA: 2,000 free minutes/month covers ~30 sweeps. Railway costs more for the same workload.
*Established by: "This is too expensive. Let's go back to use GitHub actions"*

---

## VI. Execution architecture (multi-agent)

**22. Multi-agent structure** for high-leverage tasks:
- **Cross-checking**: spawn an independent reviewer/auditor agent when results look suspicious or a phase is critical. Don't trust a single chain of reasoning.
- **Parallel research**: independent agents in parallel for non-overlapping work (research, audit, code review). Run via simultaneous tool calls.
- **Refinement loops**: first-pass implementation → second-pass code review → third-pass polish, with different agents for fresh eyes.
- **Specialized expertise**: code quality (architecture/patterns), strategy research (industry literature), data sanity (audit math/logic) — each as a focused subagent rather than one omnibus reasoner.
*Established by: "Add a component for optimal and efficient execution. Best architecture and code quality, multi-agent structure for performance, cross checking and refinement"*

**23. Code quality as first-class.** Modular files, single-responsibility functions, type hints where they help, no untested hot paths, no quiet duplication. Enforced by code-reviewer agent invocations on substantive changes.
*Established by: "Best architecture and code quality"*

**24. Optimal execution path.**
- Parallelize where independent (multiple GHA jobs, multiple subagents, multiple local-screen variants).
- Cheap-fast local screens before expensive cloud sweeps.
- Idempotent + resumable workflows where in-flight failures cost real time/money.
*Established by: "optimal and efficient execution"*

**25. Loop until complete.** (Re-stated from rule #2 with emphasis under multi-agent context.) Chain across subagents and phases without stopping at intermediate milestones.
*Established by: "keep looping until complete"*

---

## VII. Notification discipline

**26. Notify only on branch decisions.** Surface a question/notification to the user ONLY when an actual branch decision is required (incompatible options that need human judgement, irreversible action with material risk, ambiguous requirement that can't be resolved from context). Otherwise keep executing — no "want me to continue?", no progress preambles awaiting acknowledgement, no validation pings. Status updates land in the chat as natural side-effects of work; they do not require a response.
*Established by: "Only notify me (app notification?) if I need to make a branch decisions. Otherwise, keep going"*

What counts as a branch decision:
- Trade-off between two viable strategies where the user's preference is unknown (e.g. "Should I optimize for compounding or constant-base?")
- Destructive/irreversible action without prior authorization (force push, dropping a table)
- Material spend (e.g. "Run 30-variant GHA sweep at $X cost?") if not pre-approved
- Conflicting directives between current task and a prior rule

What does NOT warrant a notification:
- "I'm about to run the screen / merge / push" — just do it
- "Phase 9 is done; shall I continue to Phase 10?" — yes, by default, continue
- "CI passed" — don't ping; PR webhook will say so if subscribed
- "I found a bug; fixing" — fix it and report after, not before

---

## How to apply this framework

When starting a new task or new phase:

1. **Plan**: identify if multi-agent decomposition would help. Independent subtasks → parallel agents.
2. **Execute**: follow rules I (autonomy), II (rigor), III (risk).
3. **Validate**: spawn audit agent on extraordinary results. Cross-check.
4. **Report**: per rule IV. Inline + persistent + iPhone-friendly cadence.
5. **Commit + merge**: auto, no asking. Per rule 3.
6. **Loop**: don't stop until deliverable shipped. Per rule 2/25.

Failure modes the framework explicitly defends against:
- ❌ Stopping at "I'm waiting for X" when X isn't actually a blocker
- ❌ Trusting a backtest result without auditing realism
- ❌ Spending GHA cycles on variants that local-screen would reject
- ❌ Carrying known bugs forward into the next phase
- ❌ Losing the $/yr ROI frame when comparing variants
- ❌ Single-agent reasoning chains for complex/critical work

---

## Living document

This framework evolves with the work. New rules added when the user provides new directives. Each rule cites the directive. Rules can be deprecated by a contradicting directive (with note).

Last updated: 2026-05-10
Maintained at: `docs/auto_agentic_framework.md`
