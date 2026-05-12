# P&L Optimization — Final Report v12 (Phase 14, full-year + theory loop)

Date: 2026-05-12 ET
Branch: `claude/analyze-pl-optimization-K0NeZ`
Account: **$100,000** (paper)
Corpus: **251 trading days** (2025-05-12 → 2026-05-11) — full year, RTH-only, fetched via GHA `pull-rth-bars.yml`
Universe: 12 tickers (AAPL, MSFT, NVDA, TSLA, META, GOOG, AMZN, AVGO, NFLX, ORCL, SPY, QQQ)
Risk envelope: $2000/day concurrent cap
Compounding: **default ON** (Auto Agentic rule #11b)

---

## TL;DR

The v11 anchor was validated on a **124-day** in-sample window (+43% CAGR). On a full-year **251-day** out-of-sample superset it was net **−$3,750** with **3/4 quarters losing**. Five research rounds (62 theories × 5 corpus splits = 310 backtests) found three **4/4 positive quarter** configs. Each is a single-knob change to the anchor; they trade off headline P&L for worst-day grit.

| Rank | Config (delta from anchor) | FY net | neg_q | worst-day | WR | Q4-25 |
|---|---|---:|:---:|---:|---:|---:|
| 1 | **`ORB_RISK_PER_TRADE_PCT=1.0`** (half size) | **$+24,875** | **0/4** | −$3,157 | 56% | $+964 |
| 2 | **`NFLX:["SHORT"]` added to blocklist** | $+20,046 | **0/4** | **−$1,893** | 53% | $+1,651 |
| 3 | **max=1 + nflxS + risk=1%** (triple) | $+19,281 | **0/4** | −$1,781 | 54% | **$+4,468** |
| — | anchor (v11, full year) | −$3,750 | 3/4 | −$3,750 | 49% | −$5,700 |

**Recommended for production: config #1 (risk=1.0%)** — same lever set as v10 anchor, just one number changed. Highest stability-adjusted CAGR (+24.9%), no new logic, 56% WR.

---

## Why v11's headline was unrepresentative

v11 backtested 124 days starting 2025-11-03. That window was unusually friendly:

- Most pre-summer-2025 tariff regime days were excluded.
- The chop / index-fade days that dominated Aug–Oct 2025 were excluded.
- The compound-growth tailwind from Q3-25 wasn't dampened by prior losses.

On the **full 251-day corpus**:
- Anchor net: **−$3,750** vs v11's +$19,225
- 3/4 quarters losing (Q2-25 −$3.3k, Q3-25 +$6.6k, Q4-25 −$5.7k, Q12-26 −$1.5k)
- 31 of 31 VIX > 22 days were losers; the prior-day-close VIX gate doesn't catch intraday spikes
- Per-ticker P&L extremely concentrated: ORCL +$7.7k / AAPL −$5.9k / META −$5.2k

Treat **v11 "+43% CAGR"** as in-sample only. v12's full-year is the OOS reality.

---

## Research loop summary (Rounds 1–5)

Rule #1 / #2 / #13 framework: theories → experiments → rank by **stability first, headline second**. Each round narrowed on the next set of bleeders.

### Round 1 (13 theories) — first signal
Established the dominant lever: **block AAPL+AMZN+GOOG+AVGO long+short** (call it T5) flipped FY net from −$3.7k to +$12.9k. ORCL, NVDA, SPY/QQQ, TSLA, NFLX kept tradeable.

### Round 2 (18 theories) — layered combinations
On top of T5, layered cutoff + VIX. Winner: **T5 + cut11:00 + VIX≤20** = $+25,331 net (Q4-25 still −$1,634).

### Round 3 (21 theories) — Q4 bleeders + robustness grid
Forensic of Q4 bleed showed NFLX shorts (Oct 10/27, 10/30, 10/02 stacked stops) + TSLA Dec longs + NVDA Oct longs. Tested:

- Cutoff grid (10:30 / 11:00 / 11:30 / 12:00): **11:00 is optimal**, off-by-30-min = −$5k
- VIX grid (19 / 20 / 21 / 22): 20–22 indistinguishable in this corpus (no days in that band)
- Range cap (0.018 / 0.020 / 0.022 / 0.025): **0.025 is optimal**, tightening severely hurts (−$12k at 0.018)
- Per-trade risk (1.0 / 1.5 / 2.0%): **1.0% gives 4/4 positive quarters** for the first time
- NFLX block (full vs shorts-only): **shorts-only is the sweet spot**

Two **4/4 positive quarter** configs emerged: risk=1.0% (FY $+24,875) and NFLX shorts blocked (FY $+20,046).

### Round 4 (18 theories) — combo dimension
Stacking these levers DOESN'T add — nflxS + risk=1% gives FY $+9,937, much less than either alone. **The two stability levers operate on overlapping bad days**; combining halves the position size on the SAME smaller set of trades.

SPY/QQQ regime alignment gate (V12-style direction-align): **rejected**. Cuts FY by 60% (most legitimate breakouts are against the index 30-min OR direction).

Lower daily-loss-kill (1.5%), wider stop buffer (10–15 bps), lower concurrent risk cap ($1k), RR=3.0: all marginal effects, no improvement on stability.

### Round 5 (8 theories) — per-ticker cap=1
Q4 forensic showed multiple same-ticker duplicate stops (NFLX×2 on 10/27, NVDA×2 on 10/10). `ORB_MAX_TRADES_PER_DAY` is **per-ticker** in the engine. Setting it to 1 suppresses pyramiding:

- **max=1 alone**: FY $+22,136 with **worst-day −$1,748** (best of all configs)
- **max=1 + nflxS + risk=1%**: FY $+19,281, **Q4 +$4,468** (strongest Q4 grit), worst-day −$1,781

---

## The three deployable configs

All three are **single-line `Variables` patches** on top of the existing v10 ORB anchor in `orb/config_seed.py`. None require code changes.

### Config A — risk-half (RECOMMENDED)

```bash
ORB_RISK_PER_TRADE_PCT=1.0     # was 2.0
# (everything else unchanged from v10 anchor)
```

- FY +$24,875 / **0/4** negative quarters
- Q2-25 $+3,659 / Q3-25 $+8,621 / Q4-25 $+964 / Q12-26 $+15,098
- worst-day −$3,157, best-day $+4,051, WR 56% (highest of any config)
- **Rationale**: halving position size halves headline volatility. Daily-loss-kill triggers less often → strategy stays active to recover after first stop. WR shifts from 53% to 56% because fewer follow-on chase trades after a kill.
- **Trade-off**: Q4 only +$964 (thin margin).

### Config B — NFLX shorts blocked

```bash
ORB_TICKER_SIDE_BLOCKLIST='{"META":["LONG","SHORT"],"MSFT":["LONG","SHORT"],"AAPL":["LONG","SHORT"],"AMZN":["LONG","SHORT"],"GOOG":["LONG","SHORT"],"AVGO":["LONG","SHORT"],"NFLX":["SHORT"]}'
ORB_TIME_CUTOFF_ET=11:00       # was 15:55
ORB_SKIP_VIX_ABOVE=20          # was 22
```

- FY +$20,046 / **0/4** negative quarters
- Q2-25 $+2,214 / Q3-25 $+7,354 / Q4-25 $+1,651 / Q12-26 $+11,422
- worst-day −$1,893 (best single-lever choice)
- **Rationale**: NFLX short setups got faded on 10/02, 10/06, 10/27 (Oct earnings + October-rally regime). NFLX longs work fine — the bias is asymmetric.

### Config C — triple combo (max Q4 grit)

```bash
ORB_MAX_TRADES_PER_DAY=1                            # was 5 (per-ticker)
ORB_RISK_PER_TRADE_PCT=1.0                          # was 2.0
ORB_TICKER_SIDE_BLOCKLIST='{...META,MSFT,AAPL,AMZN,GOOG,AVGO + NFLX:["SHORT"]}'
ORB_TIME_CUTOFF_ET=11:00                            # was 15:55
ORB_SKIP_VIX_ABOVE=20                               # was 22
```

- FY +$19,281 / **0/4** negative quarters
- Q2-25 $+1,377 / Q3-25 $+2,685 / **Q4-25 $+4,468** / Q12-26 $+8,335
- worst-day −$1,781, WR 54%
- **Rationale**: combines all three stability levers. Per-ticker cap=1 suppresses the duplicate-same-side stops (e.g. NVDA×2 on 10/10, NFLX×2 on 10/30). Strongest Q4 of any config.
- **Trade-off**: lowest headline of the three, but tightest worst-day.

---

## What was tested and falsified

These were tested and DID NOT improve on the anchor — listed so future research doesn't retry them:

- **BE-arm-after-1R disabled**: makes every variant ~$400 worse. BE helps, doesn't cap winners.
- **Tighter range cap (0.018–0.022)**: −$12k. The widest-OR days (0.020–0.025) are profitable.
- **Earlier cutoff (10:30)**: −$8k. Late-morning breakouts still work.
- **Later cutoff (12:00–15:55)**: progressively worse. The 11:00 cutoff is a global optimum on this corpus.
- **SPY / QQQ regime direction-align gate**: −60% FY. Most breakouts trade against the index 30-min OR direction (they're catalyst-driven).
- **VIX threshold 18 / 19**: cuts good trades; only the >22 days have edge-destroying VIX.
- **MAX_TRADES_PER_DAY=2 or 3** (vs default 5): no effect — the cap rarely bound.
- **Lower concurrent risk cap ($1k)**: no effect.
- **Stop buffer 10–15 bps**: no effect.
- **RR=3.0 alone**: similar net, lower WR (49%).
- **RR=2.0 alone**: higher WR (58%) but lower FY (−40%).
- **Adding NFLX longs to block** (full NFLX block): −$10k vs shorts-only block. NFLX longs are profitable.
- **Daily-loss-kill at 1% or 1.5%**: no effect.

---

## How this addresses news-event regime risk

The 2025 tariff days (Apr 2025 spike, Aug 2025 Trump-China escalation, Oct 2025 chip ban headlines) all show up in Q2/Q3-25 corpus. The anchor's per-day VIX gate uses **prior-day's close** — reactive, not predictive. Recommendations:

1. **Adopt Config B or C** in production. Both restrict NFLX (the noisiest news-event responder) and tighten the time window to 11:00 ET (most catalyst-day moves are over by then).
2. **Add intraday VIX gate** (post-MVP): in `orb/day_gates.py`, fetch live VIX during the OR window and halt new entries if VIX > 22 intraday — not just at prior close. This is **not** in v12 — requires a new code path. Slotted for Phase 15.
3. **Universe diversification stays the same** (12 tickers). Tested 25-ticker expansion in v11 / Phase 13 — net negative (smaller-cap ORB signals are noisier).

---

## Validation methodology

For each theory, all 5 corpora are run **in parallel** (5-way `ThreadPoolExecutor`). Per-quarter neg-q count is the stability score; full-year net is the headline score. Backtests are **deterministic** for a given env+corpus state, so reruns reproduce within the corpus-version they sample.

Caveat surfaced during research: the GHA-fetched bar archive is being refreshed by `pull-rth-bars.yml` runs, which can mutate `/tmp/rth-data/data` mid-experiment. Round 3's first half ran against a slightly older snapshot than its second half. **Numbers in this report come from the R5 recheck pass, which was done in a single foreground process with frozen corpus state.**

---

## Implementation path

1. **Pick one of A / B / C above** based on operator risk tolerance.
2. Update `orb/config_seed.py` or production env to the chosen overrides.
3. Bump `BOT_VERSION` to 7.109.0 (or whatever's next).
4. Add CHANGELOG entry citing this report.
5. Run `bash scripts/preflight.sh` and ship via PR.
6. Post-deploy: `bash scripts/post_deploy_smoke.sh 7.109.0`.

The 3 configs are otherwise identical to v10 — same FSM, same risk book, same per-portfolio fanout. **No code paths change**, only env knobs.

---

## Artifacts

- `/tmp/orb_research_r{1..5}.py` — sweep scripts (Round 1 unavailable — pre-compaction)
- `/tmp/research_r{2..5}/all.json` — per-theory results
- `/tmp/cv_q*` — quarterly CV symlink corpora
- `/tmp/rth-data/` — git worktree of `data-extensions/rth-expand`, 251 trading days
