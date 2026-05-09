# Backtest Results Archive — 2026-05

This branch is an **archival** snapshot of TradeGenius backtest artifacts produced during the v7.x lever-development cycle (v7.3.0 through v7.6.0-experimental) plus a v6.15.x-class `v15_84day_sweep` baseline.

Contents under `backtests/`:

| Dir | What it is |
|---|---|
| `v15_84day_sweep` | v6.15.x baseline + full-flag sweep over canonical 84-day SIP corpus |
| `v707_vs_v728_replay` | v7.0.7 vs v7.2.8 head-to-head replay |
| `v720_pmr_pmc_replay` | v7.2.0 PMR/PMC replay artifacts |
| `v730_first15_block_backtest` | v7.3.0 first-15-min entry block lever |
| `v730_no_entry2_backtest` | v7.3.0 no-Entry2 (gate funnel forensics + spot checks) |
| `v730_regime_c_skip_backtest` | v7.3.0 regime-C entry skip lever |
| `v730_regime_mitigations` | v7.3.0 BE-arm-2R / streak3-30 / streak3-45 / streak4-30 mitigations |
| `v730_stop_hysteresis_84d` | v7.3.0 stop hysteresis vs baseline_ref |
| `v740_mfe_ratchet_84d` | v7.4.0 MFE-ratchet trail (Lever #3) sweep |
| `v750_84day_sweep` | v7.5.0 84-day sweep, on/off variants |
| `v750_smoke` | v7.5.0 smoke runs |
| `v760_expuni_sweep_partial` | v7.6.0-experimental expanded-universe + earnings-guard sweep — completed variants only (sweep was still running at archive time): `static_12__{none,blackout,blackout_dampen}`, `static_28__none`. Includes runner script, earnings fixture, ticker rosters, and live log. |

Each subdir typically contains: `FINAL.json`, `PROGRESS.json`, `REPORT.md`, `aggregate.py`, `run_*.py`, `sweep.log`, plus per-variant `per_day/` JSON outputs and (where available) `raw/` and `cache/` forensic data.

This branch is intentionally orphaned — it has no shared history with `main` so cloning `main` won't pull these large artifacts.

Generated: 2026-05-08 22:35 CDT.
