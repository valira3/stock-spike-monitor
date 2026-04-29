"""v5.12.0 PR 2 \u2014 TradeGeniusGene extraction.

Gene is the second Genius executor, identical in behavior to Val but
with its own GENE_ env prefix, state files, and Telegram bot. The class
body is verbatim from `trade_genius.py` pre-PR2.
"""
from __future__ import annotations

from executors.base import TradeGeniusBase


class TradeGeniusGene(TradeGeniusBase):
    """Gene \u2014 second Genius executor, identical in behavior to Val but
    with its own GENE_ env prefix, state files, and Telegram bot. Shipped
    in v4.0.0-beta alongside the 3-tab dashboard."""
    NAME = "Gene"
    ENV_PREFIX = "GENE_"
