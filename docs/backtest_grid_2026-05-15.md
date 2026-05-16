# TradeGenius Backtest Grid — 2026-05-15
Corpus: SIP bars Jan 2025-May 2026 (341 days). Compounded $100k. Ann=x(252/341).
KS4 combined baseline: $46,504/yr. KS5 (current): $52,518/yr.

## 1. Keystone Versions
| Version | Morn | EOD | Ann/yr | Delta |
|---|---:|---:|---:|---:|
| KS v2.1 | $56k | $14k | $41,485 | +0 |
| KS v3 sym10 | $45k | $12k | $41,914 | +429 |
| KS v4 +TSLA | $46k | $17k | $46,504 | +4,590 |
| KS v5 PROD * | $54k | $17k | $52,518 | +6,014 |

## 2. Cooldown Sweep (morning ORB, EOD fixed)
| Variant | Morn $k | Ann/yr | vs KS4 |
|---|---:|---:|---:|
| No cooldown | $43.2k | $44,549 | -1,955 |
| Loss 30m | $44.6k | $45,544 | -960 |
| Sym 10m PROD * | $45.9k | $46,504 | -0 |
| Sym 15m | $45.0k | $45,845 | -659 |
| Sym 20m | $44.6k | $45,605 | -899 |
| Sym 30m | $44.4k | $45,414 | -1,090 |

## 3. EOD Ticker Sweep (morning fixed at KS4)
| Variant | EOD $k | Ann/yr | vs KS4 |
|---|---:|---:|---:|
| 5 tickers (orig) | $11.8k | $42,573 | -3,930 |
| + META | $12.9k | $43,421 | -3,083 |
| + GOOG | $11.0k | $42,004 | -4,500 |
| + TSLA PROD * | $17.1k | $46,504 | -0 |
| + AMZN | $11.9k | $42,682 | -3,822 |
| + NVDA | $11.3k | $42,219 | -4,285 |
| All 10 no fence | $15.3k | $45,209 | -1,295 |

## 4. EOD Notional Sweep (TSLA fence)
| Notional/leg | EOD $k | Ann/yr | vs KS4 |
|---|---:|---:|---:|
| 20% | $9.4k | $40,833 | -5,671 |
| 25% | $11.9k | $42,692 | -3,812 |
| 30% | $14.5k | $44,577 | -1,927 |
| 35% PROD * | $17.1k | $46,504 | -0 |
| 40% | $19.7k | $48,472 | +1,968 |
| 50% | $25.2k | $52,503 | +5,999 |

## 5. EOD Structure Sweep
| Variant | EOD $k | Ann/yr | vs KS5 |
|---|---:|---:|---:|
| 1L+1S 35% PROD * | $17.1k | $52,518 | -0 |
| Best-any top-1 | $11.6k | $48,496 | -4,022 |
| Best-any top-2 | $10.7k | $47,824 | -4,694 |
| 2L+2S 17% | $7.3k | $45,274 | -7,244 |
| 2L+2S 20% | $8.6k | $46,235 | -6,283 |
| 2L+2S 25% | $10.8k | $47,903 | -4,615 |

## 6. ORB 85-Variant Sweep Top 25 (EOD fixed KS4)
| Variant | Morn $k | Ann/yr | vs KS4 |
|---|---:|---:|---:|
| sw2_cut1130_vix25 | $0.0k | $12,620 | -33,884 |
| sw2_risk125 | $0.0k | $12,620 | -33,884 |
| sw2_rr3_atr2 | $0.0k | $12,620 | -33,884 |
| sw2_rr3_vix25 | $0.0k | $12,620 | -33,884 |
| sw2_vix25 | $0.0k | $12,620 | -33,884 |
| sw2_vwap20_vix25 | $0.0k | $12,620 | -33,884 |
| sw3_vwap15_vix25 PROD * | $0.0k | $12,620 | -33,884 |
| sw3_vwap15_vix25_cd7 | $0.0k | $12,620 | -33,884 |
| sw3_vwap18_vix25 | $0.0k | $12,620 | -33,884 |
| sw3_vwap20_vix23 | $0.0k | $12,620 | -33,884 |
| sw3_vwap20_vix25_atr15 | $0.0k | $12,620 | -33,884 |
| sw3_vwap20_vix25_cd7 | $0.0k | $12,620 | -33,884 |
| sw3_vwap20_vix25_cut1130 | $0.0k | $12,620 | -33,884 |
| sw3_vwap20_vix25_or15 | $0.0k | $12,620 | -33,884 |
| sw3_vwap20_vix25_rr2 | $0.0k | $12,620 | -33,884 |
| sw3_vwap20_vix25_rr3 | $0.0k | $12,620 | -33,884 |
| sw3_vwap20_vix25_spy20 | $0.0k | $12,620 | -33,884 |
| sw3_vwap20_vix25_spy60 | $0.0k | $12,620 | -33,884 |
| sw4_v10_vix25 | $0.0k | $12,620 | -33,884 |
| sw4_v12_vix25 | $0.0k | $12,620 | -33,884 |
| sw4_v13_vix25 | $0.0k | $12,620 | -33,884 |
| sw4_v15_vix24 | $0.0k | $12,620 | -33,884 |
| sw4_v15_vix25_cd12 | $0.0k | $12,620 | -33,884 |
| sw4_v15_vix25_cd5 | $0.0k | $12,620 | -33,884 |
| sw4_v15_vix25_cd8 PROD * | $0.0k | $12,620 | -33,884 |

## 7. Earnings Skip Test
| Variant | Entries | Ann/yr | vs KS5 |
|---|---:|---:|---:|
| Skip ON PROD * | 460 | $51,898 | -620 |
| Skip OFF | 465 | $52,518 | -0 |

*PROD = current production config