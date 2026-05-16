# TradeGenius Backtest Grid — 2026-05-15
Corpus: SIP bars Jan 2025-May 2026 (341 days). Compounded $100k. Ann=x(252/341).
KS4 baseline: $46,504/yr. KS5 (current prod): $52,518/yr.
* = current production config

## 1. Keystone Evolution
| Version | Change | Ann/yr | vs prev |
|---|---|---:|---:|
| KS v2.1 | loss-only 30m, 5 EOD tickers, 15:59 exit | $41,485 | -- |
| KS v3 | sym-10m cooldown replaces loss-only | $42,573 | +1,088 |
| KS v4 | +TSLA in EOD fence | $46,504 | +3,931 |
| KS v5 * | VWAP gate 25->15bps, VIX ceiling 22->25 | $52,518 | +6,014 |

## 2. Cooldown Sweep
| Variant | Ann/yr | vs KS4 |
|---|---:|---:|
| No cooldown | $40,619 | -5,885 |
| Loss-only 30m | $41,614 | -4,890 |
| Sym 10m * (← prod after sym-10 promo) | $42,573 | -3,931 |
| Sym 15m | $41,914 | -4,590 |
| Sym 20m | $41,674 | -4,830 |
| Sym 30m | $41,484 | -5,020 |
| Loss30 + Sym15 (redundant) | $41,484 | -5,020 |

## 3. EOD Ticker Sweep
| Ticker added | EOD P&L | Ann/yr | vs KS4 |
|---|---:|---:|---:|
| 5 tickers (original) | $8,689 | $42,573 | -3,931 |
| + META | $9,537 | $43,421 | -3,083 |
| + GOOG | $8,120 | $42,004 | -4,500 |
| + TSLA * | $12,620 | $46,504 | +0 |
| + AMZN | $8,798 | $42,682 | -3,822 |
| + NVDA | $8,335 | $42,219 | -4,285 |
| All 10 no fence | $11,325 | $45,209 | -1,295 |

## 4. EOD Notional per Leg
| Notional | EOD P&L | Ann/yr | vs KS4 |
|---|---:|---:|---:|
| 20% | $6,949 | $40,833 | -5,671 |
| 25% | $8,808 | $42,692 | -3,812 |
| 30% | $10,693 | $44,577 | -1,927 |
| 35% * | $12,620 | $46,504 | +0 |
| 40% | $14,588 | $48,472 | +1,968 |
| 50% | $18,619 | $52,503 | +5,999 |

## 5. EOD Structure (vs KS5 morning)
| Structure | EOD P&L | Ann/yr | vs KS5 |
|---|---:|---:|---:|
| 1L+1S 35% * | $17,077 | $52,518 | +0 |
| Best-any top-1 | $8,598 | $42,482 | -10,036 |
| Best-any top-2 | $7,926 | $41,810 | -10,708 |
| 2L+2S 17% | $7,275 | $45,274 | -7,244 |
| 2L+2S 20% | $8,575 | $46,235 | -6,283 |
| 2L+2S 25% | $10,832 | $47,903 | -4,615 |

## 6. ORB 85-Variant Sweep — Top Results
| Variant | Key params | Ann/yr | vs KS4 |
|---|---|---:|---:|
| sw5_v16_vix25 | VWAP 16bps + VIX 25 | $52,612 | +6,108 |
| sw4_v15_vix25_cd8 | VWAP 15 + VIX 25 + cd8 | $52,589 | +6,085 |
| sw3_vwap15_vix25 * | VWAP 15 + VIX 25 ← KS5 | $52,518 | +6,014 |
| sw4_v12/13/14_vix25 | VWAP 12-14 + VIX 25 (plateau) | $52,518 | +6,014 |
| sw4_v15_vix27 | VWAP 15 + VIX 27 | $52,434 | +5,930 |
| sw2_vwap20_vix25 | VWAP 20 + VIX 25 | $51,220 | +4,716 |
| sw_vwap_15 | VWAP 15 alone | $50,076 | +3,572 |
| sw2_vix25 | VIX 25 alone | $49,743 | +3,239 |
| sw2_cut1130_vix25 | VIX 25 + cutoff 11:30 | $48,658 | +2,154 |
| sw2_rr3_vix25 | RR 3.0 + VIX 25 | $48,232 | +1,728 |
| sw_vwap_30 | VWAP 30bps | $44,591 | -1,913 |
| sw_atr_1.5 | ATR stop 1.5x | $44,308 | -2,196 |
| sw_spy_m20 | SPY gate -20bps | $43,123 | -3,381 |
| sw_atr_2.0 | ATR stop 2.0x | $42,892 | -3,612 |
| sw_vix_18 | VIX ceiling 18 | $36,955 | -9,549 |
| sw_gap_1pct | Gap filter 1% | $20,227 | -26,277 |
| sw_cd_5 | Cooldown 5min | $16,367 | -30,137 |
| sw_rr_3.0 | RR 3.0 alone | $16,327 | -30,177 |
| sw_cutoff_1030 | Time cutoff 10:30 | $7,506 | -38,998 |
| sw_vwap_0 | No VWAP gate | $1,114 | -45,390 |

## 7. Earnings Skip Test
| Variant | Entries | Ann/yr | vs KS5 |
|---|---:|---:|---:|
| Skip ON * | 460 (15 blocked) | $51,898 | -620 |
| Skip OFF   | 465              | $52,518 | 0    |
| *Result: earnings entries have 40% win rate vs 57% overall; keep skip ON* | | | |

---
*PROD = current production. KS4 baseline = $46,504/yr. KS5 = $52,518/yr.*