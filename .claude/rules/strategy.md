# Strategy-rule development — mistakes recorded as rules

Hard-won lessons from the v8.3.x → v9.0.0 development arc. Read this before adding a new admission filter, exit rule, day-gate, or any other strategy lever to `orb/`. Each item is a rule because skipping it caused a real bug or false-win during recent work.

## R1. Reproduce baseline

**Mistake**: Initially used `r6_drawdown_rules.py:BASE` as the v12 baseline, getting -$28,519 FY. Spent significant time debugging "why doesn't v12 reproduce" before realizing `r5_per_ticker_cap.py:BASE` was the actual v12 baseline (different slippage + had the T5 blocklist + cut=11:00).

**Rule**: Before testing any new theory, run the prior round's winning config end-to-end. Print the BASE env dict and reconcile against the predecessor round's BASE before declaring success. R5_recheck_risk1pt0 = $+24,864 (target $+24,875 within rounding) was the trip-wire that exposed the BASE-drift issue.

**Concrete check**: `diff <(python -c "from docs.research import r5_per_ticker_cap as r; print(r.BASE)") <(python -c "from docs.research import r6_drawdown_rules as r; print(r.BASE)")` — flag every difference.

## R2. Defaults-ON keep tests passing

**Mistake**: Shipped `min_break_bps=5` as default in `OrbConfig` in v9.0.0. Existing tests (`test_orb_accuracy.py::test_admission_math`, `test_orb_session_sim.py::test_shares_clamped_to_75pct_equity`) used `push_pct=0.0001` (1bp past OR), which the new mbr filter rejected. 15+ legacy tests broke until I added v9 disables to `SessionSimulator.__enter__`.

**Rule**: When a new lever defaults ON, audit ALL test fixtures BEFORE merging. The fix pattern: in any helper that constructs `OrbConfig` for tests that don't care about the new lever, explicitly set the new field to 0 / off:

```python
# In SimulatorConfig or test helper
env_overrides: dict[str, str] = field(default_factory=lambda: {
    "ORB_<NEW_LEVER>": "0",   # disable for legacy tests
})
```

Or, in the simulator's `__enter__`:
```python
for k, v in {"ORB_<NEW_LEVER>": "0"}.items():
    self.cfg.env_overrides.setdefault(k, v)   # caller can override
```

**Concrete check**: `python3 -m pytest tests/strategy/ -q` must show identical pass count before and after the new field is added to `OrbConfig` (no test changes other than the simulator helper). If existing tests break, the helper change is the canonical fix — not modifying the tests.

## R3. Quarterly CV mandatory

**Mistake**: Several v8.3.x rounds shipped on FY-only numbers and silently introduced quarterly negs. R6's "combo_150_500" was the example: $+9.9k FY headline that hid a Q2-25 problem when combined with ATR stops.

**Rule**: Never ship a strategy change without quarterly CV across the full corpus (`q2_2025`, `q3_2025`, `q4_2025`, `q1q2_2026` slices in `/tmp/cv_q*`). Required metric: `neg_q <= baseline_neg_q`. A FY win that breaks a previously-positive quarter is **not a ship** — record it in the falsified list and try a different angle.

**Concrete check**: every `r<N>_*.py` sweep script must run `evaluate()` against all 5 corpus slices (fy + 4 quarters). The output JSON must include `neg_q` count.

## R4. Plateau-test the winner

**Mistake**: R10's initial best was `vwap_dev<=30` at $+15,632. Without micro-sweeping the threshold, we would have shipped at 30. R10b showed 25 was actually $+1,634 better, and the plateau ran 15-27bps. Generalizable: a single-point optimum can be an overfit artifact.

**Rule**: After picking a winning threshold, sweep ±5 / ±10 / ±20 units. The optimum must sit inside a ≥3-point plateau where neighbors produce within 5% FY. Otherwise treat the result as overfit and search wider.

**Concrete check**: a plateau table in the round's report markdown showing the contiguous range and FY net at each point.

## R5. Fence, don't globalize

**Mistake**: R9 initially tested `ORB_MAX_VWAP_DEV_BPS=30` applied **globally**. Result: $+3,872 FY on no-T5 control (much better than -$14K control), but $-12K worse than the T5-block winner. The filter was catching legit chase-style winners on NVDA/ORCL/SPY/QQQ that didn't have the mega-cap bleed pattern.

**Rule**: If forensic per-(ticker, side) analysis shows bleed is concentrated in a specific subset, FENCE the filter to that subset using a ticker-list env var (`ORB_<FILTER>_TICKERS=META,MSFT,...`). The fence pattern is: empty tuple = global (no fence), non-empty = filter only applies to listed tickers. Implement as `if filter_enabled and (not fence_tickers or ticker in fence_tickers): ...`.

**Concrete check**: when adding a new universal filter, also add a ticker-fence env var even if you ship with empty default. Future rounds will need it.

## R6. `break` vs `continue` in multi-window loops

**Mistake**: R15/R16 afternoon strategies (fade mode + mid-day OR) initially used `break` to exit the loop at the time_cutoff. This prevented the loop from EVER reaching the PM-OR window or the fade window. The fade variants silently returned the same result as morning-only for all sweeps.

**Rule**: Inside a scan loop with multiple possible "active" windows (morning, fade, PM-OR, power-hour), `break` is almost always wrong. Use `continue` when you want to skip a specific 5m bar but keep scanning. Reserve `break` for "we're past EVERY active window — end the day".

**Concrete check**: write a unit test that has a signal bar in window 2 (e.g., PM-OR at bucket 750) and ensure it fires. If the test passes only when window 1's logic is also met, you have a `break` bug.

## R7. Bootstrap includes zero days

**Mistake**: Initial v9 bootstrap sampled from 201 active days only, producing CAGR 31.99% — clearly too optimistic. The correct calculation includes the 50 regime-skip days as 0% return, dropping CAGR to 24.78% (the actual FY return).

**Rule**: When projecting an N-year compounded outcome via Monte Carlo daily-return resampling, the sample pool must reflect what the strategy DOES over a full year, including idle days. For regime-skipped strategies:
```python
returns_daily = [pnl / equity for pnl in active_day_pnls]
returns_daily.extend([0.0] * n_skipped_days)   # critical
random.shuffle(returns_daily)
```

**Concrete check**: realized FY CAGR (start equity → end equity) should match the bootstrap median P50 within ±0.5%. If P50 is materially higher than realized, the sample pool is missing zero days.

## R8. Conservative ≠ full skip

**Mistake**: R13/R13b assumed that on regime-low days (prior SPY -0.4%+ drop), trading at HALF risk would extract +$1-3K vs full skip. Tested 12+ conservative-mode variants (half risk, tighter ATR, max=1, tighter mbr, fenced ticker skip, all-stacked). **All underperform full skip.** Best variant (half-risk + skip TSLA/NFLX) came within $1,150 of full-skip but added 25 more trades for zero net gain.

**Rule**: When forensic identifies a regime where the strategy bleeds, the default policy is FULL SKIP. Test conservative variants for completeness, but do not assume they'll win. The structural EV is low on those days regardless of position sizing. Only retain conservative-mode plumbing as an operator escape hatch, not as the default config.

**Concrete check**: implement `regime_low_*` overrides if helpful for operator psychology, but ship them off-by-default. Document the test results as "no conservative variant beat full-skip on this corpus" in the v<N+1> P&L report.

## R9. Audit every override

**Mistake**: In R13b initially, conservative-mode overrides (`ORB_REGIME_LOW_RISK_PER_TRADE_PCT=0.5` etc.) silently did nothing because the activation logic was `if regime_low_today and not regime_low_skip_tickers`. Setting half-risk but no skip-tickers meant the day was still fully skipped. All conservative variants returned identical results to the full-skip baseline until I fixed the activation logic to check ANY override field.

**Rule**: When a feature gates on "ANY of these conditional overrides is set", the check must enumerate every override, not just one:
```python
has_partial_mode = bool(
    cfg.regime_low_skip_tickers
    or cfg.regime_low_risk_per_trade_pct > 0
    or cfg.regime_low_atr_stop_mult > 0
    or cfg.regime_low_max_trades_per_day > 0
    or cfg.regime_low_max_vwap_dev_bps > 0
    or cfg.regime_low_min_break_bps > 0
)
```

**Concrete check**: write a unit test for each override field that confirms setting ONLY that field (with all others zero) changes the result vs baseline. If even one override fails this test, the activation logic is broken.

## R10. Document falsified theories

**Mistake**: Several v8.3.x rounds re-tested falsified theories because earlier rounds didn't log them clearly. The most expensive case: re-attempting universal `vwap_dev` filter in R9b after R9 had already shown it kills non-T5 winners.

**Rule**: Every research round must write its falsified theories into:
- The top-of-file docstring of `docs/research/r<N>_<theme>.py` (one line per dead theory)
- The "Falsified" section of `docs/pl_optimization_final_report_v<N>.md`
- A consolidated list in the latest report's TL;DR (so the next-round reviewer has a single reference)

The v13 report's falsified list has 16+ entries from R8 through R16. Future rounds use it to skip dead alleys.

**Concrete check**: when starting a new research round, grep all `docs/pl_optimization_final_report_v*.md` for "falsified" and review what's already known-dead. Update the new report with the inherited list plus this round's additions.

## R11. R6 defenses conflict with v9 chase

**Mistake**: R6 (`combo_150_500` — `loss_lock_threshold_usd=150` + `peak_dd_halt_usd=500`) was shipped in v8.3.34 as defaults-OFF. After v9 shipped chase-prevention, layering R6 on top is tempting because both target "intraday giveback". But R10 testing showed: stacked, R6 + ATR + chase costs $1-1.5K vs chase alone. The mechanisms target the same bleeders; double-counting hurts.

**Rule**: Treat v8.3.34 `ORB_LOSS_LOCK_THRESHOLD_USD` and `ORB_PEAK_DD_HALT_USD` as **mutually exclusive with v9 chase-prevention** by default. Operator should leave them at 0 in Railway env. Document this in `OrbConfig` field comments and in CLAUDE.md. Only enable them in environments where v9 chase-prevention is disabled (e.g., backwards-compatibility tests).

**Concrete check**: a backtest that sets all of `ORB_LOSS_LOCK_THRESHOLD_USD=150`, `ORB_PEAK_DD_HALT_USD=500`, `ORB_MAX_VWAP_DEV_BPS=25` should produce LOWER FY net than just the chase filter alone. If it doesn't, the test corpus or BASE has drifted.

## R12. Afternoon doesn't work

**Mistake**: R15 (afternoon fade) and R16 (mid-day OR / power-hour) tested across 9 variants. None come within $7K of morning-only. The afternoon has no directional edge AND no reversion edge — it's structural chop.

**Rule**: For the v10 ORB strategy on the 12-mega-cap universe, the trading day effectively ends at 11:00 ET. Do not propose afternoon enhancements that try to extract additional P&L. If the operator wants more capital deployment, the answer is a different universe or a different strategy framework — not a tweak to ORB.

**Concrete check**: `ORB_TIME_CUTOFF_ET=11:00` is sacred for this universe. Tests that change this without a different strategy in the afternoon window should fail.

---

## Format note

This document lives at `.claude/rules/strategy.md` and is meant as a constraint set for future strategy work. New rules are appended here as they're discovered. Cross-reference from CLAUDE.md "Mandatory PR rules" section when a rule is universal enough to apply at PR-level.
