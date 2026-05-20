"""simulator.repset -- curated representative-day list for fast iteration.

Picked from the 2026-05-20 full-year scan_loop run output. The set
covers the major outcome categories so a small batch (~12 days, ~30s
runtime with 6 workers) gives a fast feedback loop while iterating on
the engine.

Categories:
  ZERO       no entries fired (29% of full year)
  STOPS_ONLY entries only stopped out (32% of full year)
  TARGETS    only V10_TARGET wins
  MIXED      entries closed at BE / partial / EOD prep
  BIG_LOSS   day P&L < -$1500
  BIG_WIN    day P&L > +$1500
"""

DAYS_ZERO = [
    "2025-01-02",   # year start, no broad-universe picks?
    "2025-01-29",   # quiet day
    "2025-05-19",   # spring quiet day for variety
]

DAYS_STOPS = [
    "2025-01-13",   # 1 stop, -$396
    "2025-01-14",   # 1 stop, -$488
    "2025-01-08",   # big_loss: 2 stops, -$1541
]

DAYS_TARGETS = [
    "2025-02-11",   # 1 target, +$781
    "2025-03-28",   # 1 target, +$867
    "2026-04-15",   # big_win: 2 targets, +$1666
]

DAYS_MIXED = [
    "2025-01-06",   # +$1049 (mixed exit reasons)
    "2025-01-03",   # BE-stop only (no profit, no loss)
]

# Tiny set for the tightest iteration loops (single tick of each category).
TINY = [
    "2025-01-02",   # zero
    "2025-01-13",   # stops-only
    "2025-03-28",   # targets-only
    "2026-04-15",   # big_win
    "2025-01-08",   # big_loss
]

ALL = DAYS_ZERO + DAYS_STOPS + DAYS_TARGETS + DAYS_MIXED


def comma_separate(dates) -> str:
    return ",".join(dates)


if __name__ == "__main__":
    import sys
    if "--tiny" in sys.argv:
        print(comma_separate(TINY))
    else:
        print(comma_separate(ALL))
