"""v5.11.2 regression: broker package public surface is reachable.

Cheap import-time guard \u2014 ensures none of the four broker submodules has
a broken import chain after future refactors.
"""
def test_broker_stops_imports():
    from broker import stops
    assert stops._capped_long_stop is not None
    assert stops._capped_short_stop is not None
    assert stops.retighten_all_stops is not None
    assert stops._ladder_stop_long is not None

def test_broker_orders_imports():
    from broker import orders
    assert orders.check_breakout is not None
    assert orders.execute_breakout is not None
    assert orders.close_breakout is not None
    assert orders.paper_shares_for is not None

def test_broker_positions_imports():
    from broker import positions
    assert positions.manage_positions is not None
    assert positions.manage_short_positions is not None
    assert positions._v5104_maybe_fire_entry_2 is not None

def test_broker_lifecycle_imports():
    from broker import lifecycle
    assert lifecycle.check_entry is not None
    assert lifecycle.execute_entry is not None
    assert lifecycle.close_position is not None
    assert lifecycle.eod_close is not None

def test_trade_genius_deprecation_aliases(monkeypatch):
    """Deprecation aliases in trade_genius.py route to broker.* modules."""
    import sys
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius as tg
    assert tg._capped_long_stop.__module__ == "broker.stops"
    assert tg.execute_breakout.__module__ == "broker.orders"
    assert tg.manage_positions.__module__ == "broker.positions"
    assert tg.eod_close.__module__ == "broker.lifecycle"
