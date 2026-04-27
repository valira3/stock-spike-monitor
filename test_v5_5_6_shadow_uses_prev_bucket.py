"""v5.5.6 — assert shadow callers read the just-closed bucket.

Bug context: with v5.5.5, the shadow gate computed
`session_bucket(datetime.now(ET))` which returns the still-forming
current minute. The Alpaca IEX websocket only delivers a 1m bar at the
END of that minute, so `_ws_consumer.current_volume(ticker, bucket)`
always returned None for the current bucket -> cur_v=0 -> every shadow
verdict t_pct=0 qqq_pct=0 -> BLOCK. The race was confirmed live on
v5.5.5: /api/ws_state showed `volumes_size_per_symbol=5` per ticker
while shadow lines simultaneously logged `cur_v=0`.

Fix: shadow callers now use volume_profile.previous_session_bucket,
which returns the minute that JUST closed at the wall clock. That
bucket IS in `_ws_consumer._volumes` within ~100ms of close.

Tests:
  - _shadow_log_g4 emits a [V510-SHADOW] line whose ticker_pct derives
    from cur_v=5000 (the WS bar in the just-closed bucket), not 0.
  - _v512_emit_candidate_log emits a [V510-CAND] line whose bucket is
    the just-closed bucket (and t_pct derives from it accordingly).

Standalone runner (matches v5.5.5 test harness style):

    python test_v5_5_6_shadow_uses_prev_bucket.py
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Mirror smoke_test.py's environment so importing trade_genius does
# not try to dial Telegram or the Railway state volume.
os.environ["SSM_SMOKE_TEST"] = "1"
os.environ.setdefault("CHAT_ID", "999999999")
os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
os.environ.setdefault("TELEGRAM_TOKEN",
                      "0000000000:AAAA_smoke_placeholder_token_0000000")
_tmp_state = Path("/tmp/ssm_v556_state")
_tmp_state.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("STATE_DB_PATH", str(_tmp_state / "state.db"))

import volume_profile as vp  # noqa: E402
import trade_genius as m  # noqa: E402

ET = vp.ET
UTC = timezone.utc


# A fixed wall-clock during a regular trading day. 2026-04-27 is a
# Monday. At 10:27:30 ET, the just-closed minute is '1026'; the
# still-forming current minute is '1027'.
FIXED_NOW_ET = datetime(2026, 4, 27, 10, 27, 30, tzinfo=ET)


class _FakeNowDatetime(datetime):
    """datetime subclass whose .now(tz=...) returns a fixed instant."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FIXED_NOW_ET.replace(tzinfo=None)
        return FIXED_NOW_ET.astimezone(tz)


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


def _attach() -> _LogCapture:
    cap = _LogCapture()
    cap.setLevel(logging.DEBUG)
    m.logger.addHandler(cap)
    m.logger.setLevel(logging.DEBUG)
    return cap


def _detach(cap: _LogCapture) -> None:
    m.logger.removeHandler(cap)


class _StubConsumer:
    """Minimal duck-type stand-in for WebsocketBarConsumer."""

    def __init__(self, volumes: dict[str, dict[str, int]]) -> None:
        self._volumes = volumes

    def current_volume(self, ticker: str, bucket: str) -> int | None:
        v = self._volumes.get(ticker, {}).get(bucket)
        return int(v) if v is not None else None


def _profile_with_bucket(median: int, bucket: str = "1026") -> dict:
    """Build a duck-type profile whose _bucket_median returns `median`
    for `bucket`. Uses the live PROFILE_VERSION and a recent build_ts so
    is_profile_stale() returns False."""
    fresh = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    return {
        "version": vp.PROFILE_VERSION,
        "build_ts_utc": fresh,
        "buckets": {bucket: {"median": median, "stdev": 0}},
    }


def _swap(symbol: str, value):
    """Save the existing module attr, swap in `value`, return the saved
    value. Returns sentinel `_ABSENT` if the attr was not set."""
    sentinel = object()
    prev = getattr(m, symbol, sentinel)
    setattr(m, symbol, value)
    return prev, sentinel


def _restore(symbol: str, prev_pair) -> None:
    prev, sentinel = prev_pair
    if prev is sentinel:
        try:
            delattr(m, symbol)
        except AttributeError:
            pass
    else:
        setattr(m, symbol, prev)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_shadow_log_g4_uses_prev_bucket() -> None:
    """[V510-SHADOW] ticker_pct derives from cur_v=5000 (the just-closed
    minute), not 0."""
    cap = _attach()
    saved = []
    saved.append(("_ws_consumer", _swap("_ws_consumer",
                                        _StubConsumer({
                                            "AAPL": {"1026": 5000},
                                            "QQQ": {"1026": 1000},
                                        }))))
    saved.append(("_volume_profile_cache", _swap("_volume_profile_cache", {
        "AAPL": _profile_with_bucket(5000, "1026"),
        "QQQ": _profile_with_bucket(1000, "1026"),
    })))
    saved.append(("VOLUME_PROFILE_ENABLED", _swap("VOLUME_PROFILE_ENABLED", True)))
    saved.append(("datetime", _swap("datetime", _FakeNowDatetime)))

    try:
        m._shadow_log_g4("AAPL", stage=1, existing_decision="HOLD")
        msgs = [m_ for m_ in cap.messages()
                if m_.startswith("[V510-SHADOW] ")]
        assert msgs, f"no [V510-SHADOW] line emitted; got {cap.messages()!r}"
        line = msgs[0]
        # bucket=1026 — the just-closed minute, not 1027.
        assert "bucket=1026" in line, line
        assert "bucket=1027" not in line, line
        # ticker_pct derives from cur_v=5000 / median=5000 = 100, NOT 0.
        assert "ticker_pct=100" in line, line
        # qqq_pct derives from 1000/1000 = 100.
        assert "qqq_pct=100" in line, line
    finally:
        for sym, pair in reversed(saved):
            _restore(sym, pair)
        _detach(cap)


def test_v512_emit_candidate_log_uses_prev_bucket() -> None:
    """[V510-CAND] line should report bucket=1026 (just-closed) and
    t_pct derived from cur_v=5000, not 0."""
    cap = _attach()
    saved = []
    saved.append(("_ws_consumer", _swap("_ws_consumer",
                                        _StubConsumer({
                                            "AAPL": {"1026": 5000},
                                            "QQQ": {"1026": 1000},
                                        }))))
    saved.append(("_volume_profile_cache", _swap("_volume_profile_cache", {
        "AAPL": _profile_with_bucket(5000, "1026"),
        "QQQ": _profile_with_bucket(1000, "1026"),
    })))
    saved.append(("VOLUME_PROFILE_ENABLED", _swap("VOLUME_PROFILE_ENABLED", True)))
    saved.append(("datetime", _swap("datetime", _FakeNowDatetime)))

    try:
        m._v512_emit_candidate_log(
            "AAPL", stage=1, entered=False,
            bars={"current_price": 100.0, "stop": 99.0,
                  "highs": [], "lows": [], "closes": []},
        )
        msgs = [m_ for m_ in cap.messages()
                if m_.startswith("[V510-CAND] ")]
        assert msgs, f"no [V510-CAND] line emitted; got {cap.messages()!r}"
        line = msgs[0]
        assert "bucket=1026" in line, line
        assert "bucket=1027" not in line, line
        # t_pct derived from cur_v=5000 / median=5000 -> 100.
        assert "t_pct=100" in line, line
        assert "qqq_pct=100" in line, line
    finally:
        for sym, pair in reversed(saved):
            _restore(sym, pair)
        _detach(cap)


def test_shadow_log_g4_outside_session_returns_silently() -> None:
    """If the just-closed minute is outside the session (e.g. just after
    open at 09:31:00), the shadow log returns without emitting and
    without raising."""
    cap = _attach()

    class _After0931(datetime):
        @classmethod
        def now(cls, tz=None):
            ts = datetime(2026, 4, 27, 9, 31, 0, tzinfo=ET)
            return ts.astimezone(tz) if tz is not None else ts.replace(tzinfo=None)

    saved = []
    saved.append(("_ws_consumer", _swap("_ws_consumer",
                                        _StubConsumer({"AAPL": {}}))))
    saved.append(("_volume_profile_cache", _swap("_volume_profile_cache", {})))
    saved.append(("VOLUME_PROFILE_ENABLED", _swap("VOLUME_PROFILE_ENABLED", True)))
    saved.append(("datetime", _swap("datetime", _After0931)))

    try:
        m._shadow_log_g4("AAPL", stage=1, existing_decision="HOLD")
        # No [V510-SHADOW] info line should fire — bucket is None.
        bad = [m_ for m_ in cap.messages() if m_.startswith("[V510-SHADOW] ")]
        assert not bad, bad
    finally:
        for sym, pair in reversed(saved):
            _restore(sym, pair)
        _detach(cap)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_shadow_log_g4_uses_prev_bucket,
    test_v512_emit_candidate_log_uses_prev_bucket,
    test_shadow_log_g4_outside_session_returns_silently,
]


def main() -> int:
    fails = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  +  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  X  {fn.__name__}: {type(e).__name__}: {e}")
    total = len(TESTS)
    print(f"\n  {total - fails} passed \u00b7 {fails} failed \u00b7 {total} total\n")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
