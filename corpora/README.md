# Corpus Archive

Compressed Alpaca SIP minute-bar corpora used as inputs for the backtests under `../backtests/`.

| File | Source workspace dir | Description | Compressed | Uncompressed |
|---|---|---|---|---|
| `sip_84day_2026.tar.gz` | `canonical_backtest_data_v707/` | **Active 84-day 2026 SIP corpus.** Used by every v7.x sweep including the in-flight v7.6.0-experimental expanded-universe + earnings-guard run. Contains `replay_layout/<date>/<TICKER>.jsonl` (RTH minute bars), `premarket/<date>/<TICKER>.jsonl`, `days_84.txt`, and the `pull_missing_15.py` resume helper. | 86 MB | 506 MB |
| `sip_legacy_archive.tar.gz` | `canonical_backtest_data/` | Older corpora kept for forensic comparison: `63day_2026_archive_iexrth_sippm/` and `84day_2026_sip/`. Only used by older backtests; not the active corpus. | 82 MB | 572 MB |

## Integrity
```
a2cf54b25b0e6a0f4321612a8fa962dab3696cb47022afaf9de271e46db59a49  sip_84day_2026.tar.gz
0759486040580590a4911d860550bf6316768e021b372189662ad6389e084617  sip_legacy_archive.tar.gz
```

## Restore
```bash
mkdir -p /home/user/workspace
cd /home/user/workspace
tar -xzf /path/to/sip_84day_2026.tar.gz   # restores canonical_backtest_data_v707/
tar -xzf /path/to/sip_legacy_archive.tar.gz  # restores canonical_backtest_data/
```

## Provenance
Both corpora were originally pulled from Alpaca SIP (paid feed) by the helpers in `canonical_backtest_data_v707/pull_missing_15*.py`. See the source repo for the rebuild path; this archive exists so an exact byte-for-byte replay of every sweep on the `archive/backtest-results-2026-05` branch is possible without re-pulling the SIP data.
