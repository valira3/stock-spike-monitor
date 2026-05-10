# Final Report v7 — Out-of-Sample Amendment

**Status: CRITICAL CORRECTION TO v7**

Date: 2026-05-10
Issue: v7's headline +$47,191/yr was based on the 83-day 2026-only corpus. Extending to the full 124-day corpus available (Nov 2025 — May 2026) reveals **substantial regime sensitivity** that v7's STRIDE=2 cross-validation did not catch.

---

## What changed

After v7 was published, we re-ran the deployed config on the **full 124-day corpus** (Nov 2025 – May 2026) instead of just 2026-only:

| corpus slice | net P&L | annual | WR |
|---|---:|---:|---:|
| 2026-only (v7 headline, 83d) | +$15,543 | +$47,191/yr | 52.3% |
| **2025-11 to 2026-05 (124d, full)** | **+$8,725** | **+$17,732/yr** | **47.4%** |
| 2025-11 to 2026-02 (41d, "train") | −$6,818 | −$41,904/yr | 37.9% |

The 2025 portion of the corpus has the strategy LOSING ~$42k/yr.

## Per-month decomposition (where the regime breaks)

| month | net | days | WR | regime |
|---|---:|---:|---:|---|
| **2025-11** | **−$7,510** | 19 | **34.6%** | ❌ chop / fade-of-breakouts |
| 2025-12 | +$692 | 22 | 40.0% | weak |
| 2026-01 | +$4,538 | 20 | 55.2% | strong |
| 2026-02 | +$3,251 | 19 | 58.3% | strong |
| 2026-03 | +$6,753 | 22 | 51.3% | strong |
| 2026-04 | +$2,466 | 21 | 48.6% | OK |

**2025-11 is the killer.** A single 19-day stretch with 34.6% WR drops the whole-corpus number by ~$30k/yr. The other 5 months are all positive.

## Implications for deployment

1. **The +$47k/yr v7 number is conditional on a 2026-Q1-style market regime** (trending, breakout-friendly mega-cap action). It does NOT represent steady-state edge.
2. **Without a regime detector**, the strategy will likely have months like 2025-11 in production. Worst case: −$7.5k month, possibly larger.
3. **Mitigations available** (research subagent's Phase 9 levers):
   - **SPY/QQQ regime gate** — only trade when SPY+QQQ are aligned in direction with the breakout signal. Cheap to implement.
   - **NR7 / Inside-day filter** — Crabel's volatility-contraction-precedes-expansion finding.
   - **Don't trade the first 1–2 weeks of a new month** when regime data is thin.

## Honest revised recommendation

Without a regime gate, the deployable expectation should be **the 124-day corpus number: ~$17,732/yr** (≈+18% ROI on $100k account). NOT the +$47k/yr headline.

With a regime gate (Phase 9 work in flight after this amendment): expected to recover much of the −$7.5k 2025-11 drag. Final number TBD.

## What we did well

- Multi-agent execution caught this. The research subagent flagged "demand STRIDE=2/3 cross-val confirmation" — and STRIDE=2 wasn't enough; we needed cross-period validation.
- Audit-driven realism caps prevented the 25× phantom leverage from corrupting the result.
- The Auto Agentic framework's rule on "sanity-check extraordinary results" remains correctly applied — we caught this BEFORE shipping live capital.

## What we missed

- STRIDE=2 cross-val on the same period is NOT a substitute for cross-period validation. STRIDE=2 just samples every other day from the same regime.
- Should have used the full available corpus from the start, with explicit train/test split (e.g. Nov 2025 to Jan 2026 train, Feb-May 2026 test).
- Single-corpus headline numbers are inherently fragile. Multi-year corpus + bootstrap resampling would be next-level rigor.

---

## Action items (Phase 9 + Phase 10 in flight)

1. **Phase 10 (quality)**: 4 HIGH-severity code review fixes — IN FLIGHT (subagent working)
2. **Phase 9 (performance)**: SPY/QQQ regime gate — implement next, retest on 124d
3. **Phase 9 (performance)**: NR7 prior-day filter
4. **Multi-corpus rigor**: future work — extend corpus to multi-year, formalize train/test/holdout split

Final Report v8 will publish the corrected numbers once Phase 9 work lands.

---

This amendment supersedes v7's deployment recommendation pending Phase 9 outcomes.
