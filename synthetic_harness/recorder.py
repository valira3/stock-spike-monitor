"""OutputRecorder — captures observable side-effects of trade_genius.

Captures:
  * send_telegram(text, chat_id=None)
  * paper_log(msg)
  * _emit_signal(event)
  * trade_log_append(row)
  * save_paper_state()
  * _update_gate_snapshot(ticker)
  * _record_near_miss(**row)

reset() clears all queues — call between actions.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field


@dataclass
class OutputRecorder:
    telegram_outbox: list = field(default_factory=list)
    paper_log_outbox: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    trade_log_writes: list = field(default_factory=list)
    save_paper_state_calls: int = 0
    gate_snapshot_calls: list = field(default_factory=list)
    near_miss_writes: list = field(default_factory=list)

    def reset(self) -> None:
        self.telegram_outbox.clear()
        self.paper_log_outbox.clear()
        self.signals.clear()
        self.trade_log_writes.clear()
        self.save_paper_state_calls = 0
        self.gate_snapshot_calls.clear()
        self.near_miss_writes.clear()

    # ---- capture functions (signatures match trade_genius) ----

    def capture_telegram(self, text, chat_id=None):
        self.telegram_outbox.append(str(text))

    def capture_paper_log(self, msg):
        self.paper_log_outbox.append(str(msg))

    def capture_emit_signal(self, event: dict):
        # Strip non-deterministic timestamp_utc — the FrozenClock should
        # already provide a stable value, but copy defensively.
        self.signals.append(copy.deepcopy(event))

    def capture_trade_log_append(self, row: dict):
        self.trade_log_writes.append(copy.deepcopy(row))
        return True

    def capture_save_paper_state(self, *args, **kwargs):
        self.save_paper_state_calls += 1

    def capture_update_gate_snapshot(self, ticker: str):
        self.gate_snapshot_calls.append(ticker)

    def capture_record_near_miss(self, **row):
        self.near_miss_writes.append(copy.deepcopy(row))

    def to_dict(self) -> dict:
        return {
            "telegram_outbox": list(self.telegram_outbox),
            "paper_log_outbox": list(self.paper_log_outbox),
            "signals": copy.deepcopy(self.signals),
            "trade_log_writes": copy.deepcopy(self.trade_log_writes),
            "save_paper_state_calls": self.save_paper_state_calls,
            "gate_snapshot_calls": list(self.gate_snapshot_calls),
            "near_miss_writes": copy.deepcopy(self.near_miss_writes),
        }
