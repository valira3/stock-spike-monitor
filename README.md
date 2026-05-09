# Backtest Results Archive — 2026-05

Archival snapshot of TradeGenius backtest artifacts produced during the v7.x lever-development cycle (v7.3.0 through v7.6.0-experimental) plus a v6.15.x-class `v15_84day_sweep` baseline. Includes the underlying SIP minute-bar corpora needed to reproduce every result.

## Layout

- [`backtests/`](./backtests) — sweep outputs (`FINAL.json`, `REPORT.md`, per-day JSONs, raw replays, runner scripts)
- [`corpora/`](./corpora) — gzipped Alpaca SIP corpora used as backtest inputs

## Backtests

| Dir | What it is |
|---|---|
| `v15_84day_sweep` | v6.15.x baseline + full-flag sweep over the canonical 84-day SIP corpus |
| `v707_vs_v728_replay` | v7.0.7 vs v7.2.8 head-to-head replay |
| `v720_pmr_pmc_replay` | v7.2.0 PMR/PMC replay artifacts |
| `v730_first15_block_backtest` | v7.3.0 first-15-min entry-block lever |
| `v730_no_entry2_backtest` | v7.3.0 no-Entry2 (gate funnel forensics + spot checks) |
| `v730_regime_c_skip_backtest` | v7.3.0 regime-C entry-skip lever |
| `v730_regime_mitigations` | v7.3.0 BE-arm-2R / streak3-30 / streak3-45 / streak4-30 mitigations |
| `v730_stop_hysteresis_84d` | v7.3.0 stop-hysteresis vs baseline_ref |
| `v740_mfe_ratchet_84d` | v7.4.0 MFE-ratchet trail (Lever #3) sweep |
| `v750_84day_sweep` | v7.5.0 84-day sweep, on/off variants |
| `v750_smoke` | v7.5.0 smoke runs |
| `v760_expuni_sweep_partial` | v7.6.0-experimental expanded-universe + earnings-guard sweep \u2014 completed variants only at archive time: `static_12__{none,blackout,blackout_dampen}` and `static_28__none`. Includes runner script, earnings fixture, ticker rosters, and live log. |

## Corpora

| File | Description | Compressed | Uncompressed |
|---|---|---|---|
| `corpora/sip_84day_2026.tar.gz` | Active 84-day 2026 SIP corpus (`canonical_backtest_data_v707/`). Used by every v7.x sweep. | 86 MB | 506 MB |
| `corpora/sip_legacy_archive.tar.gz` | Older corpora kept for forensic comparison (`canonical_backtest_data/`). | 82 MB | 572 MB |

See `corpora/README.md` for sha256 checksums and restore steps.

## Branch hygiene

This branch is intentionally **orphaned** \u2014 it has no shared history with `main`, so a normal `git clone --single-branch main` won't pull these large artifacts.

Generated: 2026-05-08 22:42 CDT.
