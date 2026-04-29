"""v5.12.0 PR 3 \u2014 executor wiring helpers.

Encapsulates the env-driven creation of Val/Gene executors and the
bootstrap step that publishes the instances into both `trade_genius`
and `telegram_commands` module namespaces (so existing
`globals().get("val_executor")` lookups in `telegram_commands.py:647`
continue to work after v5.12.0).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from executors.val import TradeGeniusVal
from executors.gene import TradeGeniusGene

logger = logging.getLogger(__name__)


def build_val_executor() -> Optional[TradeGeniusVal]:
    """Return a started TradeGeniusVal if env-enabled, else None.

    Mirrors the boot block previously in trade_genius.py: opt-in via
    VAL_ENABLED (default on) AND VAL_ALPACA_PAPER_KEY present. On any
    construction failure logs and returns None so a missing-keys deploy
    still boots cleanly.
    """
    enabled = os.getenv("VAL_ENABLED", "1").strip() not in ("0", "false", "False", "")
    has_keys = bool(os.getenv("VAL_ALPACA_PAPER_KEY", "").strip())
    if not (enabled and has_keys):
        logger.info(
            "[Val] skipped (VAL_ENABLED=%s, VAL_ALPACA_PAPER_KEY set=%s)",
            os.getenv("VAL_ENABLED", "1"), has_keys,
        )
        return None
    try:
        inst = TradeGeniusVal()
        inst.start()
        logger.info("[Val] started in %s mode", inst.mode)
        return inst
    except Exception:
        logger.exception("[Val] startup failed \u2014 main continues")
        return None


def build_gene_executor() -> Optional[TradeGeniusGene]:
    """Return a started TradeGeniusGene if env-enabled, else None.

    Same pattern as build_val_executor but for Gene / GENE_ALPACA_PAPER_KEY.
    """
    enabled = os.getenv("GENE_ENABLED", "1").strip() not in ("0", "false", "False", "")
    has_keys = bool(os.getenv("GENE_ALPACA_PAPER_KEY", "").strip())
    if not (enabled and has_keys):
        logger.info(
            "[Gene] skipped (GENE_ENABLED=%s, GENE_ALPACA_PAPER_KEY set=%s)",
            os.getenv("GENE_ENABLED", "1"), has_keys,
        )
        return None
    try:
        inst = TradeGeniusGene()
        inst.start()
        logger.info("[Gene] started in %s mode", inst.mode)
        return inst
    except Exception:
        logger.exception("[Gene] startup failed \u2014 main continues")
        return None


def install_globals(
    *,
    val: Optional[TradeGeniusVal] = None,
    gene: Optional[TradeGeniusGene] = None,
) -> None:
    """Publish val/gene executors into trade_genius and telegram_commands namespaces.

    telegram_commands.py:647 uses `globals().get(f"{which}_executor")`
    which expects `val_executor` and `gene_executor` to exist as
    module-level globals on telegram_commands. This helper makes that
    wiring explicit and testable.
    """
    import trade_genius
    import telegram_commands
    trade_genius.val_executor = val
    trade_genius.gene_executor = gene
    telegram_commands.val_executor = val
    telegram_commands.gene_executor = gene
