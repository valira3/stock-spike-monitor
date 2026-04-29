"""v5.14.0 backtest data-access package.

The original v5.4.0 backtest CLI replayed the (now-retired) SHADOW_CONFIGS
volume gate against archived 1m bars. In v5.14.0 the shadow strategy and
its `shadow_positions` table were removed; this package was reduced to a
thin data-access layer over:

  * the per-day, per-ticker JSONL bar archive at /data/bars/<UTC>/<TICKER>.jsonl
    (still written by `bar_archive.write_bar`)
  * the live trade log at trade_log.jsonl (written by
    `trade_genius.trade_log_append` whenever a real position closes)
  * the persisted open-position mirror in `executor_positions` (state.db)

Future backtest entry points should consume `load_bars` plus
`load_prod_trades_from_log` to compare predicted entries/exits against
the actual main-portfolio activity. The full SHADOW_CONFIGS replay
engine was deleted with the rest of the shadow strategy.
"""

__version__ = "5.14.0"
