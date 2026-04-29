"""v5.12.0 PR 2 \u2014 TradeGeniusVal extraction.

Val is the first Genius executor. Behavior is unchanged from the prior
in-line definition in `trade_genius.py`; only the home module changed.
The class body is verbatim from `trade_genius.py` pre-PR2.
"""
from __future__ import annotations

from executors.base import TradeGeniusBase


class TradeGeniusVal(TradeGeniusBase):
    """Val \u2014 first Genius executor. Alpaca paper by default; Val flips
    to live via `/mode live confirm` on Val's own Telegram bot, or via
    `/mode val live confirm` on main's bot."""
    NAME = "Val"
    ENV_PREFIX = "VAL_"
