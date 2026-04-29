"""v5.12.0 \u2014 executors package.

Houses the Alpaca-backed executor sub-bot infrastructure extracted from
`trade_genius.py`. PR 1 introduces `base` (TradeGeniusBase, ~1,135 lines,
37 methods); PR 2 will add `val` and `gene` (subclasses); PR 3 will add
`bootstrap` (build_val_executor / build_gene_executor / install_globals).

Boot log line `[EXEC] modules loaded: base` is emitted at trade_genius
startup so a missed Dockerfile COPY surfaces as ImportError on boot
rather than mid-session.
"""
from __future__ import annotations

from executors.base import TradeGeniusBase

LOADED_MODULES = ("base",)

__all__ = [
    "TradeGeniusBase",
    "LOADED_MODULES",
]
