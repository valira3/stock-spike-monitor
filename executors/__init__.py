"""v5.12.0 \u2014 executors package.

Houses the Alpaca-backed executor sub-bot infrastructure extracted from
`trade_genius.py`. PR 1 introduced `base` (TradeGeniusBase, ~1,135 lines,
37 methods); PR 2 added `val` and `gene` (subclasses); PR 3 adds
`bootstrap` (build_val_executor / build_gene_executor / install_globals).

Boot log line `[EXEC] modules loaded: base, val, gene, bootstrap` is
emitted at trade_genius startup so a missed Dockerfile COPY surfaces as
ImportError on boot rather than mid-session.
"""
from __future__ import annotations

from executors.base import TradeGeniusBase
from executors.val import TradeGeniusVal
from executors.gene import TradeGeniusGene
from executors.bootstrap import (
    build_val_executor,
    build_gene_executor,
    install_globals,
)

LOADED_MODULES = ("base", "val", "gene", "bootstrap")

__all__ = [
    "TradeGeniusBase",
    "TradeGeniusVal",
    "TradeGeniusGene",
    "build_val_executor",
    "build_gene_executor",
    "install_globals",
    "LOADED_MODULES",
]
