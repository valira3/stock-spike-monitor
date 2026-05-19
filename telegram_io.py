"""Telegram messaging primitives.

History. Lived in trade_genius.py from v3.4.x through v9.1.140. Carved
out in v10.0.1 as part of the post-architectural-review monolith
reduction. trade_genius.py keeps back-compat re-exports for the
callers that import these names from `trade_genius` (broker/orders.py,
engine/scan.py via callbacks, executors/base.py, telegram_ui/*,
market_brief.py, smoke_test.py, and the tests).

What lives here:
  - send_telegram(text, chat_id=None)        bot -> chat low-level send
  - _format_error_telegram(executor, code, summary, detail) Telegram
                                              text wrapped to mobile
                                              code-block width (<=34
                                              chars per line)
  - report_error(executor, code, severity, summary, detail) the
                                              page-the-operator entry
                                              point: logs, records to
                                              error_state ring, and
                                              dispatches via the right
                                              channel (main bot or
                                              executor's own bot)

What stays in trade_genius.py:
  - val_executor / gene_executor lifetime, since the executor bootstrap
    builds these instances; report_error reaches them through the
    `set_executor_lookup` setter wired below at import time.

Token / chat-id are read from the same env vars trade_genius.py used:
TELEGRAM_TOKEN and CHAT_ID.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")


# --- Executor lookup injection -------------------------------------------
# report_error needs to route "val" / "gene" pages to the executor's
# own bot when the executor is enabled. The executor instances live in
# trade_genius (val_executor / gene_executor module attrs after
# bootstrap). We don't import them directly to keep this module
# trade_genius-independent; the host process wires the lookup at import
# time via `set_executor_lookup`.
_executor_lookup: Callable[[str], object] = lambda name: None


def set_executor_lookup(fn: Callable[[str], object]) -> None:
    """Register the function that returns the executor instance for a
    given name ('val' / 'gene'). trade_genius.py calls this at import
    time with its `_executor_inst` helper.
    """
    global _executor_lookup
    _executor_lookup = fn


# --- send_telegram -------------------------------------------------------


def send_telegram(text, chat_id=None):
    """Send text message to Telegram. Splits long messages. Retries on 429."""
    cid = chat_id or CHAT_ID
    if not text or not text.strip() or not TELEGRAM_TOKEN or not cid:
        return

    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 3800:
            if current:
                parts.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        parts.append(current.rstrip())

    total = len(parts)
    for i, part in enumerate(parts, 1):
        prefix = "%d/%d " % (i, total) if total > 1 else ""
        payload = json.dumps({"chat_id": cid, "text": prefix + part}).encode()
        url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN
        for attempt in range(5):
            try:
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status = resp.status
                if status == 429:
                    wait = 2 ** attempt
                    logger.warning("Telegram 429 -- sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                time.sleep(0.3)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 2 ** attempt
                    logger.warning("Telegram 429 -- sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                logger.error("Telegram send error (attempt %d): %s", attempt + 1, e)
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.error("Telegram send error (attempt %d): %s", attempt + 1, e)
                time.sleep(2 ** attempt)


# --- report_error --------------------------------------------------------
# report_error() is the single entry point for "operator should be
# paged about this" events. It does three things, in order:
#   1. Logs via the existing logger so existing log surfaces still see
#      the event (file logs, stderr, the dashboard ring buffer prior
#      to v4.11.0 -- the dashboard log tail card itself was deleted in
#      this release, but the underlying logger handlers stay).
#   2. Appends to error_state so the dashboard health pill counter +
#      tap-to-expand list reflect the event.
#   3. If error_state's dedup gate says "send", routes a Telegram
#      message to the right channel: main bot for "main" events,
#      executor's own bot for "val" / "gene".
#
# The 5-min dedup is per (executor, code) so a flapping ORDER_REJECT
# does not spam the channel; the dashboard count still increments on
# every event.
import error_state as _error_state


_ET_ZONE = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    return datetime.now(_ET_ZONE)


def _utc_now_iso() -> str:
    from datetime import timezone as _tz
    return datetime.now(_tz.utc).isoformat()


def _format_error_telegram(executor: str, code: str, summary: str, detail: str = "") -> str:
    """Format a Telegram error message respecting the <=34 chars/line rule.

    Layout:
      [siren] X [middot] CODE
      <summary>
      <detail line(s)>

      ts: HH:MM:SS ET
    """
    ex_label = (executor or "").upper()
    head = f"\U0001f6a8 {ex_label} \u00b7 {code}"

    def _wrap(text: str, width: int = 34) -> list[str]:
        out: list[str] = []
        for raw_line in (text or "").splitlines() or [""]:
            line = raw_line.rstrip()
            if len(line) <= width:
                out.append(line)
                continue
            # Greedy word-wrap. If a single word is >width, hard-split it.
            words = line.split(" ")
            buf = ""
            for w in words:
                if not buf:
                    if len(w) <= width:
                        buf = w
                    else:
                        # Hard-split overlong word.
                        while len(w) > width:
                            out.append(w[:width])
                            w = w[width:]
                        buf = w
                elif len(buf) + 1 + len(w) <= width:
                    buf = buf + " " + w
                else:
                    out.append(buf)
                    if len(w) <= width:
                        buf = w
                    else:
                        while len(w) > width:
                            out.append(w[:width])
                            w = w[width:]
                        buf = w
            if buf:
                out.append(buf)
        return out

    parts: list[str] = []
    parts.append(head if len(head) <= 34 else head[:34])
    parts.extend(_wrap(summary))
    if detail:
        parts.extend(_wrap(detail))

    try:
        ts = _now_et().strftime("%H:%M:%S ET")
    except Exception:
        ts = ""
    if ts:
        parts.append("")
        parts.append(f"ts: {ts}")
    return "\n".join(parts)


def report_error(executor: str, code: str, severity: str, summary: str,
                 detail: str = "") -> bool:
    """Page-the-operator entry point.

    Returns True iff a Telegram message was actually dispatched (i.e.
    the dedup gate elapsed). Dashboard count always increments.
    """
    # 1. Log via existing logger. Preserve the same level mapping the
    #    rest of the codebase uses: "warning" -> WARNING, otherwise
    #    ERROR. CRITICAL events still log at ERROR; the distinction is
    #    only relevant for the dashboard pill color.
    sev = (severity or "").strip().lower()
    log_msg = f"[{(executor or '').upper()}/{code}] {summary}"
    try:
        if sev == "warning":
            logger.warning(log_msg)
        else:
            logger.error(log_msg)
    except Exception:
        pass

    # 2. Append to error_state ring + check dedup gate.
    try:
        ts_iso = _utc_now_iso()
    except Exception:
        ts_iso = ""
    try:
        should_send = _error_state.record_error(
            executor=executor,
            code=code,
            severity=severity,
            summary=summary,
            detail=detail,
            ts=ts_iso,
        )
    except Exception:
        # Never let error reporting itself raise.
        logger.exception("report_error: error_state.record_error failed")
        return False

    if not should_send:
        return False

    # 3. Dispatch to the right Telegram channel.
    try:
        text = _format_error_telegram(executor, code, summary, detail)
    except Exception:
        logger.exception("report_error: format failed")
        return False

    ex = (executor or "").strip().lower()
    try:
        if ex in ("val", "gene"):
            inst = _executor_lookup(ex)
            if inst is not None:
                inst._send_own_telegram(text)
            else:
                # Executor not enabled -- fall back to main bot so the
                # operator still gets paged.
                send_telegram(text)
        else:
            send_telegram(text)
    except Exception:
        logger.exception("report_error: telegram dispatch failed")
        return False
    return True
