"""v5.5.5 — bar archive source-switch tests.

The bar-archive write happens inline in trade_genius.py's scan loop and
is not factored into a helper. To avoid having to spin up the full
scan-loop machinery just to assert a single field selection, this test
file does two things:

1. Source-grep guard: parse trade_genius.py and assert the exact code
   pattern that prefers _ws_consumer.current_volume(...) over Yahoo's
   vols[idx] is present, and that et_bucket is now populated from
   volume_profile.session_bucket(...). If a future refactor regresses
   either, this test fails loudly.
2. Behavioral check: faithfully reproduce the post-v5.5.5 selection
   logic in a tiny inline helper and verify it picks the WS value when
   present, falls back to Yahoo on None, and populates et_bucket. The
   reproduced logic is intentionally small enough to eyeball-match
   against the prod code in the source-grep step.

Standalone runner, no pytest dep.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import volume_profile  # noqa: E402

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Source-grep guards
# ---------------------------------------------------------------------------

def _read_tg_src() -> str:
    # v5.11.0 PR4 \u2014 the archive write inline-block moved out of
    # trade_genius.py's scan_loop body into engine/scan.py. Concatenate
    # both so the existing source-grep guards keep working unchanged
    # whichever file owns the pattern.
    tg = (REPO / "trade_genius.py").read_text(encoding="utf-8")
    es = (REPO / "engine" / "scan.py").read_text(encoding="utf-8")
    return tg + "\n" + es


def test_archive_prefers_ws_consumer_when_available() -> None:
    src = _read_tg_src()
    # The v5.5.5 selection must call current_volume on _ws_consumer and
    # gate it on session_bucket(now_et) being not None.
    assert "_ws_consumer.current_volume(" in src, src[:200]
    assert "session_bucket(now_et)" in src
    # The fallback chain still references the Yahoo source variable.
    assert "yahoo_vol" in src or "vols[idx]" in src


def test_archive_falls_back_to_yahoo_when_ws_returns_none() -> None:
    src = _read_tg_src()
    # The pattern must explicitly test for "ws_vol is not None" before
    # overriding the Yahoo default \u2014 otherwise None would silently
    # blow away vols[idx].
    assert "if ws_vol is not None" in src, (
        "Archive must only override iex_volume when the WS path returned a "
        "real int; a None must fall back to the Yahoo value"
    )


def test_archive_populates_et_bucket() -> None:
    src = _read_tg_src()
    # et_bucket: None was the v5.5.2..v5.5.4 hard-coded value. v5.5.5
    # must populate it from session_bucket(now_et).
    assert '"et_bucket": et_bucket,' in src, (
        "canon_bar must use the locally-resolved et_bucket, not None"
    )
    # And the resolution must happen via volume_profile.session_bucket.
    assert "et_bucket = volume_profile.session_bucket(now_et)" in src


# ---------------------------------------------------------------------------
# Behavioral reproduction of the inline selection
# ---------------------------------------------------------------------------

class _FakeWS:
    def __init__(self, mapping: dict[tuple[str, str], int | None]):
        self._m = mapping

    def current_volume(self, ticker: str, bucket: str) -> int | None:
        return self._m.get((ticker, bucket))


def _select_iex_volume(ws, ticker: str, yahoo_vol: int | None,
                      now_et: datetime) -> tuple[int | None, str | None]:
    """Mirror of the post-v5.5.5 inline logic in trade_genius.py.

    Kept intentionally small so source review can confirm parity.
    """
    iex_volume = yahoo_vol
    et_bucket: str | None = None
    try:
        et_bucket = volume_profile.session_bucket(now_et)
        if et_bucket is not None and ws is not None:
            ws_vol = ws.current_volume(ticker, et_bucket)
            if ws_vol is not None:
                iex_volume = int(ws_vol)
    except Exception:
        pass
    return iex_volume, et_bucket


def test_behavior_ws_int_is_used() -> None:
    # 10:31 ET on a known weekday (2026-04-27 = Mon).
    now_et = datetime(2026, 4, 27, 10, 31, tzinfo=ET)
    bucket = volume_profile.session_bucket(now_et)
    assert bucket == "1031", bucket
    ws = _FakeWS({("AAPL", "1031"): 5245})
    iex, et = _select_iex_volume(ws, "AAPL", yahoo_vol=0, now_et=now_et)
    assert iex == 5245, iex
    assert et == "1031", et


def test_behavior_ws_none_falls_back_to_yahoo() -> None:
    now_et = datetime(2026, 4, 27, 10, 31, tzinfo=ET)
    ws = _FakeWS({("AAPL", "1031"): None})
    iex, et = _select_iex_volume(ws, "AAPL", yahoo_vol=999, now_et=now_et)
    assert iex == 999, iex
    assert et == "1031", et


def test_behavior_no_ws_consumer_keeps_yahoo() -> None:
    now_et = datetime(2026, 4, 27, 10, 31, tzinfo=ET)
    iex, et = _select_iex_volume(None, "AAPL", yahoo_vol=42, now_et=now_et)
    assert iex == 42, iex
    assert et == "1031", et


def test_behavior_outside_rth_keeps_yahoo() -> None:
    # Pre-open 09:00 ET \u2014 session_bucket returns None.
    now_et = datetime(2026, 4, 27, 9, 0, tzinfo=ET)
    assert volume_profile.session_bucket(now_et) is None
    ws = _FakeWS({("AAPL", "1031"): 5245})
    iex, et = _select_iex_volume(ws, "AAPL", yahoo_vol=7, now_et=now_et)
    # Yahoo wins because the WS gate (session_bucket != None) fails.
    assert iex == 7, iex
    assert et is None, et


# ---------------------------------------------------------------------------

TESTS = [
    test_archive_prefers_ws_consumer_when_available,
    test_archive_falls_back_to_yahoo_when_ws_returns_none,
    test_archive_populates_et_bucket,
    test_behavior_ws_int_is_used,
    test_behavior_ws_none_falls_back_to_yahoo,
    test_behavior_no_ws_consumer_keeps_yahoo,
    test_behavior_outside_rth_keeps_yahoo,
]


def main() -> int:
    fails = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  +  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            import traceback
            print(f"  X  {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    total = len(TESTS)
    print(f"\n  {total - fails} passed · {fails} failed · {total} total\n")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
