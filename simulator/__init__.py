"""simulator/ -- swap-in test mode for the trading bot.

When SIMULATOR_MODE=1 is set (or simulator.mocks.install_all() is called
manually), every external service the bot touches is replaced by a
deterministic in-process mock:

  - Alpaca TradingClient + StockHistoricalDataClient
  - FMP REST endpoints (quote, earnings calendar)
  - Yahoo Finance chart endpoint
  - Telegram bot API (sendMessage, sendPhoto)
  - Railway GraphQL (no-op stubs)

The v10 decision pipeline (orb.live_runtime + engine.scan + broker.*
+ executors.*) runs unchanged. The simulator just sits at the
network boundary.

CLI usage:
    python -m simulator.runner --scenario golden_orb_long
    python -m simulator.runner --replay 2026-05-15
    python -m simulator.runner --list

Programmatic usage:
    from simulator import SimulatorRunner
    r = SimulatorRunner.from_scenario("golden_orb_long")
    r.run()
    assert "AAPL" in r.executed_entries
    assert r.telegram_sends == []
"""
from __future__ import annotations

from simulator.runner import SimulatorRunner

__all__ = ["SimulatorRunner"]
