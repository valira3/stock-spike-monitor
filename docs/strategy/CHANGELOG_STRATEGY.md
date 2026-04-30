# STRATEGY.md changelog

## vAA-1 — adopted in v5.15.0 (pending)
Supersedes v2026-04-28h. Headline changes:
- Strike Model with `STRIKE-CAP-3` (max 3 Strikes per (ticker, side) per day) and `STRIKE-FLAT-GATE`.
- Phase 2 volume gate is time-conditional (`L-P2-S3` / `S-P2-S3`): auto-passes before 10:00 ET; ≥ 100% of 55-bar same-minute baseline after.
- Phase 3 sizing rewritten as momentum-sensitive: 1m DI± > 30 → 100% (`L-P3-FULL`); 25 ≤ 1m DI± ≤ 30 → 50% Scaled-A; add Scaled-B only on DI±>30 + fresh NHOD/NLOD + Alarm E False.
- Order pricing: `LIMIT @ ask × 1.001` (long), `LIMIT @ bid × 0.999` (short).
- Sentinels heavily morphed: A split into A_LOSS (-$500) and A_FLASH (>1%/min); A1/A2 legacy codes deleted; B unchanged; C *replaced* with Velocity Ratchet (3 strictly-decreasing 1m ADX → STOP MARKET ± 0.25%); Titan Grip Harvest *deleted entirely*; D *new* — HVP Lock (5m ADX < 75% Trade_HVP → MARKET EXIT); E *new* — Divergence Trap (RSI(15) on 1m bars; pre-entry filter for Strikes 2/3, post-entry stop ratchet).
- All profit-taking now via stop-ratchet trips, Alarm D, or EOD. No fixed harvests.

## v2026-04-28h — adopted in v5.13.0
Tiger Sovereign baseline: Bison/Buffalo phase machine, fixed 50/50 entry sequence, Titan Grip Harvest exit ladder, Alarms A/B/C with combined A1/A2 codes.
