"""simulator.mocks.mock_telegram -- intercept Telegram bot API HTTP calls.

Production path:
    telegram_io.send_telegram -> urllib.request.urlopen(POST
    https://api.telegram.org/bot<TOKEN>/sendMessage with form data
    {chat_id, text, parse_mode}).

In simulator mode we just capture every send into
scenario_state["telegram_sends"] and return the canonical "ok": true
response. The Telegram receive path (commands like /status) is NOT
mocked -- the bot does long-polling for those, which the simulator
side-steps by never running the Telegram event loop.
"""
from __future__ import annotations

import io
import json
import urllib.parse
from typing import Any


def handle(req: Any, scenario_state: dict):
    from simulator.mocks.mock_errors import telegram_failure, http_error_resp
    url = _get_url(req)
    path = url.split("api.telegram.org", 1)[-1] if "api.telegram.org" in url else url
    method = path.rsplit("/", 1)[-1].split("?")[0]

    fail = telegram_failure(scenario_state)
    if fail is not None:
        # Record the attempted send before erroring out so assertions see it.
        scenario_state["telegram_sends"].append({
            "method": method, "url": url, "status": fail[0],
            "error": "injected failure",
        })
        http_error_resp(*fail)

    body_bytes = b""
    try:
        body_bytes = req.data or b""  # type: ignore[attr-defined]
    except Exception:
        pass

    parsed = {}
    if body_bytes:
        try:
            text = body_bytes.decode("utf-8", errors="replace")
            parsed = dict(urllib.parse.parse_qsl(text)) if "=" in text else json.loads(text)
        except Exception:
            parsed = {"_raw": body_bytes.decode("utf-8", errors="replace")}

    scenario_state["telegram_sends"].append({
        "method": method,
        "chat_id": parsed.get("chat_id"),
        "text": parsed.get("text"),
        "parse_mode": parsed.get("parse_mode"),
        "url": url,
    })

    # Return a Telegram-shaped ok response.
    payload = {"ok": True, "result": {"message_id": len(scenario_state["telegram_sends"])}}
    return _resp(json.dumps(payload).encode())


def _get_url(req: Any) -> str:
    if hasattr(req, "full_url"):
        return req.full_url
    if hasattr(req, "get_full_url"):
        return req.get_full_url()
    return str(req)


def _resp(body: bytes):
    stream = io.BytesIO(body)
    stream.headers = {"Content-Type": "application/json"}  # type: ignore[attr-defined]
    stream.status = 200  # type: ignore[attr-defined]
    def _read(*args):
        return body if not args else body[: args[0]]
    stream.read = _read  # type: ignore[attr-defined]
    stream.__enter__ = lambda self=stream: stream  # type: ignore[attr-defined]
    stream.__exit__ = lambda self, *a: False  # type: ignore[attr-defined]
    return stream
