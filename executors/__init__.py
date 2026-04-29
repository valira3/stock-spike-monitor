"""v5.12.0 \u2014 executors package.

Houses the Alpaca-backed executor sub-bot infrastructure extracted from
`trade_genius.py`. PR 1 introduced `base` (TradeGeniusBase, ~1,135 lines,
37 methods); PR 2 adds `val` and `gene` (subclasses); PR 3 will add
`bootstrap` (build_val_executor / build_gene_executor / install_globals).

Boot log line `[EXEC] modules loaded: base, val, gene` is emitted at
trade_genius startup so a missed Dockerfile COPY surfaces as ImportError
on boot rather than mid-session.
"""
from __future__ import annotations

from executors.base import TradeGeniusBase
from executors.val import TradeGeniusVal
from executors.gene import TradeGeniusGene

LOADED_MODULES = ("base", "val", "gene")

__all__ = [
    "TradeGeniusBase",
    "TradeGeniusVal",
    "TradeGeniusGene",
    "LOADED_MODULES",
]
