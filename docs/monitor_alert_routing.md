# Monitor Alert Categorization

Each check is classified into one of three response tiers:

| Tier | Label | When | Who acts | Where |
|---|---|---|---|---|
| 1 | AUTO-FIX | Bot can self-repair without code change | Monitor loop | Prod (env var / Railway API) |
| 2 | HOTFIX | Code change, safe to skip staging | Human / prod session | main -> prod direct |
| 3 | STAGING | Code change, needs validation | Human / staging session | staging -> main -> prod |

## CRITICAL — Production Down / Live Loss Risk

| Check | Tier | Action |
|---|---|---|
| `api.state unreachable` | **HOTFIX** | Bot crashed or Railway down. Check logs, redeploy or rollback. |
| `session: ORB_LIVE_MODE=0` | **HOTFIX** | Legacy fallback active — v10 not trading. Fix env var or code, redeploy. |
| `executor.val.health: error` | **HOTFIX** | Val executor threw an exception. Fix root cause, redeploy. |
| `risk.{pid}: kill triggered` | **AUTO-FIX** | Daily loss limit hit — expected behavior. Monitor only, no fix needed. |
| `railway_logs: ERROR/CRITICAL` | **HOTFIX** | Unhandled exception in Railway logs. Read logs, fix crash, redeploy. |
| `vix: day blocked + VIX spike` | **AUTO-FIX** | VIX gate fired — bot correctly blocking. No fix needed. |
| `vwap_chase: entry blocked` | **AUTO-FIX** | VWAP gate fired — correct behavior. No fix needed. |
| `trade_log: large loss` | **HOTFIX** | Single trade loss exceeds threshold. Review position, check stop logic. |

## CRITICAL — Strategy / Config Integrity

| Check | Tier | Action |
|---|---|---|
| `ui.dom_ids: server error in HTML` | **HOTFIX** | Dashboard rendering crash. Fix server-side error, redeploy. |
| `inv_v10_in_pos_has_internal_position` | **HOTFIX** | Phantom IN_POS: FSM says in-pos but no internal position. Rollback or restart. |
| `inv_railway_logs_clean: traceback` | **HOTFIX** | Unhandled exception in scan loop. Fix crash, hotfix to main. |

## WARN — Config Drift (safe to auto-fix or 1-liner hotfix)

| Check | Tier | Action |
|---|---|---|
| `eod.fence: wrong tickers` | **HOTFIX** | Expected tickers changed (e.g. after TSLA add). Update system_check_bot.py KEYSTONE dict. |
| `eod.window: wrong entry/exit ET` | **HOTFIX** | Entry/exit times drifted from expected. Update KEYSTONE dict. |
| `api.version: version mismatch` | **AUTO-FIX** | Deploy in progress. Wait 3 min and recheck before acting. |
| `config.{lever}: drift` | **HOTFIX** | Strategy lever drifted from KEYSTONE. Update env var in Railway. |
| `eod.fire_mode: paper` | **AUTO-FIX** | EOD fire mode shows paper — check ORB_EOD_FIRE_BROKER env var. |
| `vix: unavailable` | **AUTO-FIX** | VIX feed down (weekend, holiday). Monitor only. |

## WARN — Executor / Trade State

| Check | Tier | Action |
|---|---|---|
| `inv_val_gene_trades_match_main` | **AUTO-FIX** | In FIRE=1 mode Val can have more trades than Main — expected. Check if FIRE=1. |
| `inv_equity_matches_baseline` | **AUTO-FIX** | Equity drifted from floor (normal after trading). No fix needed during RTH. |
| `inv_position_count_three_way` | **HOTFIX** | Position count mismatch across portfolios. Check for phantom positions. |
| `inv_open_risk_within_cap` | **AUTO-FIX** | Risk approaching cap — normal intraday. Monitor closely. |
| `inv_no_phantom_positions` | **HOTFIX** | Engine thinks position open but no broker position. Rollback_admit may have failed. |
| `inv_v10_live_mode_on_during_rth` | **AUTO-FIX** | v10 not bootstrapped — normal pre-9:30. CRIT if during RTH. |
| `inv_daily_kill_consistency` | **AUTO-FIX** | Kill switch state vs trade log mismatch — recheck after RTH ends. |

## WARN — Data / UI Quality

| Check | Tier | Action |
|---|---|---|
| `ui.title: changed` | **STAGING** | Dashboard title changed unexpectedly. UI regression — validate on staging. |
| `ui.dom_ids: missing` | **STAGING** | Required DOM ID missing from dashboard. UI bug — fix on staging. |
| `ui.eod_section: missing` | **STAGING** | EOD section ID missing. UI regression — fix on staging. |
| `data_quality: entry_stop=0` | **STAGING** | Stop price missing on stop-exit. Bug in exit recording — fix on staging. |
| `or_break: OR break issue` | **STAGING** | Opening range break anomaly. May indicate bar data issue or logic bug. |
| `fill_match: Alpaca fills vs log` | **AUTO-FIX** | Fill count mismatch — common post-redeploy. Clear after next trade cycle. |
| `trade_log.version_consistency` | **AUTO-FIX** | Trades from prior version — cosmetic. Clears at next session reset. |
| `fetch_warn: fetch failed` | **AUTO-FIX** | Transient network error. Retry next cycle. |

## WARN — Invariants needing code change

| Check | Tier | Action |
|---|---|---|
| `inv_signal_bus_has_listeners` | **STAGING** | Signal bus lost a listener. Executor/adapter bug — needs staging validation. |
| `inv_or_window_well_formed` | **STAGING** | OR window start/end times malformed. Logic bug — fix on staging. |
| `inv_or_locked_after_or_end` | **STAGING** | OR not locked after OR end — FSM state bug. Fix on staging. |
| `inv_or_window_data_quality` | **STAGING** | OR data quality issues (gaps, bad bars). Investigate bar archive. |
| `inv_risk_book_notional_cap` | **HOTFIX** | Risk book notional cap is zero — blocking all entries. Fix env or config. |
| `inv_equity_self_consistent` | **STAGING** | Equity calculation inconsistency. Math bug — fix on staging. |

---

## Tier decision rules

**AUTO-FIX** — no code change needed. Either expected behavior, transient state,
or correctable via Railway env var change without a deploy.

**HOTFIX** — one-liner code or config change, low blast radius, well-understood root cause.
Safe to push direct to main. Backport to staging via sync-staging.yml auto-runs.

**STAGING** — behavioral/logic change, UI change, or anything that needs
a full test cycle before reaching production. Fix in staging session,
validate for at least one RTH session, then promote.