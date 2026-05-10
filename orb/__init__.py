"""orb -- v10 anchor strategy live engine.

This package implements the v10 anchor Opening Range Breakout strategy
in production form. The reference behavior is the standalone backtest
at tools/orb_backtest.py (v10-anchor tag on main).

Modules:
  state       -- per-portfolio per-ticker FSM + market-wide OR window
  day_gates   -- session-start filters (VIX, earnings, gap, kill-switch)
  risk_book   -- per-portfolio concurrent risk cap (thread-safe)
  exits       -- 1R / 2.5R / move-to-BE exit evaluator (PR2)
  engine      -- public surface; on_bar / should_enter (PR2)

All modules: CLEAN look-ahead per framework rule #7b. Every signal
consumes only data with timestamp <= decision time.

Multi-portfolio note: OR window state is MARKET-WIDE (one per ticker).
Trade state (FSM phase, trades_today, in_position) is PER-PORTFOLIO.
Risk book is one instance per portfolio. Day gates evaluate uniformly
across all portfolios (VIX gate is market-wide).
"""

__all__ = ["state", "day_gates", "risk_book"]
