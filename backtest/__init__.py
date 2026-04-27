"""v5.4.0 offline backtest CLI package.

Replays SHADOW_CONFIGS over archived 1m bars in /data/bars/ and
optionally validates predicted entries against the prod shadow_positions
table in state.db.

Usage:
    python -m backtest.replay --start 2026-04-20 --end 2026-04-24 \\
        --config GEMINI_A [--validate] [--out ./backtest_out/]
"""

__version__ = "5.4.0"
