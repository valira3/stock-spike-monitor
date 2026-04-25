"""install / uninstall — monkeypatch trade_genius for a scenario run.

Swaps in the FrozenClock, SyntheticMarket, and OutputRecorder. Returns
a dict of original attributes for restoration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from synthetic_harness.clock import FrozenClock, make_frozen_datetime_class
from synthetic_harness.market import SyntheticMarket
from synthetic_harness.recorder import OutputRecorder


# Module attributes the harness swaps. install() captures the original
# values; uninstall() restores them.
PATCH_TARGETS = (
    "_now_et",
    "_now_cdt",
    "_utc_now_iso",
    "fetch_1min_bars",
    "get_fmp_quote",
    "tiger_di",
    "send_telegram",
    "paper_log",
    "_emit_signal",
    "trade_log_append",
    "save_paper_state",
    "_update_gate_snapshot",
    "_record_near_miss",
    "datetime",
    "_cycle_bar_cache",
)


def install(module, clock: FrozenClock, market: SyntheticMarket,
            recorder: OutputRecorder) -> dict:
    """Replace trade_genius attributes with synthetic equivalents.

    Returns the saved-original dict to feed into uninstall().
    """
    saved = {k: getattr(module, k) for k in PATCH_TARGETS}

    module._now_et = clock.now_et
    module._now_cdt = clock.now_cdt
    module._utc_now_iso = clock.utc_now_iso
    module.fetch_1min_bars = market.fetch_1min_bars
    module.get_fmp_quote = market.get_fmp_quote
    module.tiger_di = market.tiger_di
    module.send_telegram = recorder.capture_telegram
    module.paper_log = recorder.capture_paper_log
    module._emit_signal = recorder.capture_emit_signal
    module.trade_log_append = recorder.capture_trade_log_append
    module.save_paper_state = recorder.capture_save_paper_state
    module._update_gate_snapshot = recorder.capture_update_gate_snapshot
    module._record_near_miss = recorder.capture_record_near_miss
    module.datetime = make_frozen_datetime_class(clock)
    # Empty the per-cycle bar cache so our patched fetch is always
    # consulted (the real one short-circuits to whatever is cached).
    module._cycle_bar_cache = {}

    return saved


def uninstall(module, saved: dict) -> None:
    for k, v in saved.items():
        setattr(module, k, v)
