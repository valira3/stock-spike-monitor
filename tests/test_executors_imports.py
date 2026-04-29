"""v5.12.0 PR1/PR2 regression: executors package public surface is reachable.

Cheap import-time guard \u2014 ensures the executors/{base,val,gene} extraction
did not break the public names on either the new home (`executors`) or
the back-compat re-exports inside `trade_genius`.
"""
import os
import sys
from pathlib import Path


def test_import_tradegeniusbase_from_executors():
    """`from executors import TradeGeniusBase` works post-extraction."""
    from executors import TradeGeniusBase
    assert TradeGeniusBase is not None
    assert TradeGeniusBase.__module__ == "executors.base"


def test_tradegenius_base_reexported_in_main_module(monkeypatch):
    """trade_genius.TradeGeniusBase must be the same class as
    executors.base.TradeGeniusBase. The deprecation alias in
    trade_genius.py is removed in v5.12.0 PR 5; until then both names
    resolve to the same object."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import executors.base
    import trade_genius
    assert trade_genius.TradeGeniusBase is executors.base.TradeGeniusBase


def test_tradegenius_base_constructible(monkeypatch):
    """Instantiating a minimal subclass does not crash. Mirrors the
    Val/Gene smoke patterns (smoke_test.py:~662): set the required
    Alpaca env vars on a per-prefix basis, then construct."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    from executors import TradeGeniusBase

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    assert inst.NAME == "TestStub"
    assert inst.mode == "paper"
    assert inst.paper_key == "dummy_paper_key"


def test_import_tradegeniusval_from_executors():
    """`from executors import TradeGeniusVal` works post-PR2."""
    from executors import TradeGeniusVal
    assert TradeGeniusVal is not None
    assert TradeGeniusVal.__module__ == "executors.val"


def test_import_tradegeniusgene_from_executors():
    """`from executors import TradeGeniusGene` works post-PR2."""
    from executors import TradeGeniusGene
    assert TradeGeniusGene is not None
    assert TradeGeniusGene.__module__ == "executors.gene"


def test_tradegeniusval_subclasses_base():
    from executors import TradeGeniusVal, TradeGeniusBase
    assert issubclass(TradeGeniusVal, TradeGeniusBase)


def test_tradegeniusgene_subclasses_base():
    from executors import TradeGeniusGene, TradeGeniusBase
    assert issubclass(TradeGeniusGene, TradeGeniusBase)


def test_val_name_and_prefix():
    from executors import TradeGeniusVal
    assert TradeGeniusVal.NAME == "Val"
    assert TradeGeniusVal.ENV_PREFIX == "VAL_"


def test_gene_name_and_prefix():
    from executors import TradeGeniusGene
    assert TradeGeniusGene.NAME == "Gene"
    assert TradeGeniusGene.ENV_PREFIX == "GENE_"


def test_val_reexported_in_main_module(monkeypatch):
    """trade_genius.TradeGeniusVal must be executors.val.TradeGeniusVal."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import executors.val
    import trade_genius
    assert trade_genius.TradeGeniusVal is executors.val.TradeGeniusVal


def test_gene_reexported_in_main_module(monkeypatch):
    """trade_genius.TradeGeniusGene must be executors.gene.TradeGeniusGene."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import executors.gene
    import trade_genius
    assert trade_genius.TradeGeniusGene is executors.gene.TradeGeniusGene


def test_import_bootstrap_helpers():
    """v5.12.0 PR 3: executors.bootstrap exposes the three helpers."""
    from executors.bootstrap import (
        build_val_executor,
        build_gene_executor,
        install_globals,
    )
    assert callable(build_val_executor)
    assert callable(build_gene_executor)
    assert callable(install_globals)


def test_build_val_returns_none_when_env_unset(monkeypatch):
    """No VAL_ALPACA_PAPER_KEY \u2192 build_val_executor() returns None
    without raising."""
    monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
    from executors.bootstrap import build_val_executor
    assert build_val_executor() is None


def test_build_gene_returns_none_when_env_unset(monkeypatch):
    """No GENE_ALPACA_PAPER_KEY \u2192 build_gene_executor() returns None."""
    monkeypatch.delenv("GENE_ALPACA_PAPER_KEY", raising=False)
    from executors.bootstrap import build_gene_executor
    assert build_gene_executor() is None


def test_no_deprecation_aliases_remain_in_trade_genius():
    """Guard: no v5.11.x deprecation alias blocks should exist in trade_genius.py.

    After v5.12.0 PR 5, the file must not contain any `# vX.Y.Z deprecation
    aliases \u2014 removed in v5.12.0` import blocks. The executors re-exports
    are the only import-from-extracted-module patterns allowed.
    """
    with open(Path(__file__).parent.parent / "trade_genius.py") as f:
        src = f.read()
    assert "deprecation aliases" not in src, "deprecation alias comments still present"
    assert "removed in v5.12.0" not in src, "v5.12.0 alias-removal markers still present"


def test_install_globals_writes_to_both_namespaces(monkeypatch):
    """install_globals(val=, gene=) must publish into both
    trade_genius and telegram_commands module namespaces so the
    `globals().get('val_executor')` lookup at telegram_commands.py:647
    keeps working."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    import trade_genius
    import telegram_commands
    from executors.bootstrap import install_globals

    saved_tg_val = getattr(trade_genius, "val_executor", None)
    saved_tg_gene = getattr(trade_genius, "gene_executor", None)
    saved_tc_val = getattr(telegram_commands, "val_executor", None)
    saved_tc_gene = getattr(telegram_commands, "gene_executor", None)

    sentinel_val = object()
    sentinel_gene = object()
    try:
        install_globals(val=sentinel_val, gene=sentinel_gene)
        assert trade_genius.val_executor is sentinel_val
        assert trade_genius.gene_executor is sentinel_gene
        assert telegram_commands.val_executor is sentinel_val
        assert telegram_commands.gene_executor is sentinel_gene
    finally:
        trade_genius.val_executor = saved_tg_val
        trade_genius.gene_executor = saved_tg_gene
        telegram_commands.val_executor = saved_tc_val
        telegram_commands.gene_executor = saved_tc_gene
