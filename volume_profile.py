"""volume_profile.py \u2014 Forensic Volume Filter.

Builds a 55-trading-day per-minute volume baseline for each watched ticker
using Alpaca SIP historical 1m bars (free-plan compliant: end < now-16min),
normalizes the published median to the IEX scale (the live-feed scale on
the free plan), and exposes a §17.2 V-P1 grid evaluator (`evaluate_g4`).

Live volumes are read from a persistent Alpaca /iex websocket bar stream
(free-plan cap: 30 symbols). Live enforcement is gated behind
VOL_GATE_ENFORCE; when 0 the gate logs but does not change any entry
decision.

Public surface (all sync; no asyncio in callers' codepaths):
    is_trading_day, trading_days_back, session_bucket
    build_profile, save_profile, load_profile, is_profile_stale
    evaluate_g4
    rebuild_all_profiles
    WebsocketBarConsumer

No new dependencies beyond stdlib + the already-pinned alpaca-py.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants (Val-locked design — see v5.1.0 brief)
# ---------------------------------------------------------------------------

PROFILE_DIR = os.getenv("VOLUME_PROFILE_DIR", "/data/volume_profiles")
PROFILE_VERSION = "v5.1.0"
WINDOW_TRADING_DAYS = 55
STALE_HOURS = 36
WS_SYMBOL_CAP_FREE_IEX = 30

# 16 minutes safety margin past the free-plan SIP 15-minute restriction.
SIP_END_BACKOFF_MIN = 16

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# NYSE holidays for 2026 and 2027. Verified against the official NYSE
# calendar; if the bot is still running in 2028 these need to be extended.
NYSE_HOLIDAYS: frozenset[str] = frozenset(
    {
        # 2026
        "2026-01-01",  # New Year's Day
        "2026-01-19",  # MLK Day
        "2026-02-16",  # Presidents Day
        "2026-04-03",  # Good Friday
        "2026-05-25",  # Memorial Day
        "2026-06-19",  # Juneteenth
        "2026-07-03",  # Jul 3 (Jul 4 falls Saturday \u2014 Friday observance)
        "2026-09-07",  # Labor Day
        "2026-11-26",  # Thanksgiving
        "2026-12-25",  # Christmas
        # 2027
        "2027-01-01",
        "2027-01-18",
        "2027-02-15",
        "2027-03-26",  # Good Friday
        "2027-05-31",
        "2027-06-18",  # Juneteenth observed (Jun 19 is Sat)
        "2027-07-05",  # Jul 5 (Jul 4 falls Sunday \u2014 Monday observance)
        "2027-09-06",
        "2027-11-25",
        "2027-12-24",  # Dec 25 falls Saturday \u2014 Friday observance
    }
)

# Early-close days. Map ET date string -> last regular-session minute label.
EARLY_CLOSE_DATES: dict[str, str] = {
    "2026-11-27": "13:00",  # day after Thanksgiving
    "2026-12-24": "13:00",
    "2027-11-26": "13:00",
    "2027-12-23": "13:00",
}

REGULAR_OPEN = dtime(
    9, 31
)  # first bucket label (the 09:30-bar minute is excluded; 09:31 = first complete minute bar)
REGULAR_CLOSE = dtime(16, 0)  # exclusive — last bucket is 15:59
EARLY_CLOSE_DEFAULT = dtime(13, 0)


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def is_trading_day(d: date) -> bool:
    """True if `d` is a NYSE regular trading day (weekday + not holiday)."""
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return False
    if d.isoformat() in NYSE_HOLIDAYS:
        return False
    return True


def trading_days_back(end_d: date, n: int) -> list[date]:
    """Return the `n` most recent trading days ending on or before `end_d`.

    Returned list is ascending (oldest first). Includes `end_d` itself if
    it's a trading day.
    """
    if n <= 0:
        return []
    out: list[date] = []
    cur = end_d
    while len(out) < n:
        if is_trading_day(cur):
            out.append(cur)
        cur -= timedelta(days=1)
    out.reverse()
    return out


def _early_close_time(d: date) -> dtime | None:
    label = EARLY_CLOSE_DATES.get(d.isoformat())
    if not label:
        return None
    hh, mm = label.split(":")
    return dtime(int(hh), int(mm))


def session_bucket(ts_et: datetime) -> str | None:
    """ET timestamp -> 'HHMM' bucket key, or None if outside the session.

    The first bar bucket of the day is '0931' (the 09:30 bar is the
    auction print and Val does not want it in the baseline). The last
    regular-session bucket is '1559'. On early-close days the cutoff is
    honoured (e.g. on a 13:00 close the last bucket is '1259').
    """
    if ts_et.tzinfo is None:
        # Be strict — we only ever want ET-aware timestamps in this path.
        return None
    if ts_et.tzinfo != ET:
        ts_et = ts_et.astimezone(ET)

    d = ts_et.date()
    if not is_trading_day(d):
        return None

    t = ts_et.time().replace(second=0, microsecond=0)
    if t < REGULAR_OPEN:
        return None

    early = _early_close_time(d)
    if early is not None:
        # Last valid bucket is one minute before the close.
        cutoff = early
    else:
        cutoff = REGULAR_CLOSE
    if t >= cutoff:
        return None

    return f"{t.hour:02d}{t.minute:02d}"


def previous_session_bucket(ts_et: datetime) -> str | None:
    """Returns the bucket key for the minute that JUST closed at ts_et.

    The Alpaca IEX websocket only delivers a 1-minute bar at the END of
    the minute, so the still-forming current bucket is empty until the
    minute closes. Shadow-gate readers should ask for the just-closed
    bucket instead.

    Examples (regular session):
        10:27:30 ET -> session_bucket(10:26:00) == '1026'
        10:28:00 ET -> session_bucket(10:27:00) == '1027'

    Returns None when the just-closed minute falls outside the regular
    session (premarket, post-close, weekend, holiday).
    """
    if ts_et.tzinfo is None:
        return None
    if ts_et.tzinfo != ET:
        ts_et = ts_et.astimezone(ET)
    floored = ts_et.replace(second=0, microsecond=0)
    prev = floored - timedelta(minutes=1)
    return session_bucket(prev)


# ---------------------------------------------------------------------------
# Profile build/save/load
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _profile_path(ticker: str) -> str:
    safe = ticker.upper().replace("/", "_")
    return os.path.join(PROFILE_DIR, f"{safe}.json")


def save_profile(ticker: str, profile: dict) -> str:
    """Atomic write (tmp + os.replace), same pattern as v5.0.3 chat-map."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    path = _profile_path(ticker)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profile, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    return path


def load_profile(ticker: str) -> dict | None:
    """Return the on-disk profile, or None if missing/unparseable."""
    path = _profile_path(ticker)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[VOLPROFILE] load_profile %s failed: %s", ticker, e)
        return None


def is_profile_stale(profile: dict, now_utc: datetime) -> bool:
    """True if the profile is older than STALE_HOURS or version-mismatched."""
    if not profile:
        return True
    if profile.get("version") != PROFILE_VERSION:
        return True
    ts = profile.get("build_ts_utc")
    if not ts:
        return True
    try:
        # Accept both 'Z' and '+00:00' suffixes.
        if ts.endswith("Z"):
            built = datetime.fromisoformat(ts[:-1]).replace(tzinfo=UTC)
        else:
            built = datetime.fromisoformat(ts)
            if built.tzinfo is None:
                built = built.replace(tzinfo=UTC)
    except Exception:
        return True
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    return (now_utc - built) > timedelta(hours=STALE_HOURS)


# ---------------------------------------------------------------------------
# Alpaca client helpers (lazy import — keeps the module unit-testable)
# ---------------------------------------------------------------------------


def _historical_client(alpaca_key: str, alpaca_secret: str):
    from alpaca.data.historical import StockHistoricalDataClient

    return StockHistoricalDataClient(alpaca_key, alpaca_secret)


def _fetch_1m_bars(
    client, ticker: str, start_utc: datetime, end_utc: datetime, feed: str
) -> list[dict]:
    """Fetch 1-minute bars for `ticker` over [start_utc, end_utc]. Returns
    a list of dicts: {"ts_et": datetime, "volume": int}. Pages through
    next_page_token implicitly (alpaca-py handles pagination).
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    feed_enum = DataFeed.SIP if feed == "sip" else DataFeed.IEX
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Minute,
        start=start_utc,
        end=end_utc,
        feed=feed_enum,
    )
    resp = client.get_stock_bars(req)
    raw = resp.data.get(ticker, []) if hasattr(resp, "data") else []
    out: list[dict] = []
    for b in raw:
        # alpaca-py returns timezone-aware UTC timestamps.
        ts_utc = getattr(b, "timestamp", None)
        if ts_utc is None:
            continue
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=UTC)
        out.append({"ts_et": ts_utc.astimezone(ET), "volume": int(getattr(b, "volume", 0) or 0)})
    return out


def build_profile(
    ticker: str,
    end_dt_utc: datetime,
    alpaca_key: str,
    alpaca_secret: str,
) -> dict:
    """Build a 55-trading-day profile for `ticker`.

    See module docstring + brief §17.1 for the full design.
    """
    if end_dt_utc.tzinfo is None:
        end_dt_utc = end_dt_utc.replace(tzinfo=UTC)
    # Free-plan SIP restriction: end must be at least 15 min in the past.
    end_safe = end_dt_utc - timedelta(minutes=SIP_END_BACKOFF_MIN)
    end_d = end_safe.astimezone(ET).date()

    days = trading_days_back(end_d, WINDOW_TRADING_DAYS)
    if not days:
        raise RuntimeError(f"no trading days resolved for {ticker}")

    start_dt_et = datetime.combine(days[0], REGULAR_OPEN, tzinfo=ET)
    start_dt_utc = start_dt_et.astimezone(UTC)

    client = _historical_client(alpaca_key, alpaca_secret)
    sip_bars = _fetch_1m_bars(client, ticker, start_dt_utc, end_safe, "sip")
    iex_bars = _fetch_1m_bars(client, ticker, start_dt_utc, end_safe, "iex")

    # Index IEX bars by (date, bucket) for cheap lookup.
    iex_index: dict[tuple[str, str], int] = {}
    for b in iex_bars:
        bucket = session_bucket(b["ts_et"])
        if bucket is None:
            continue
        iex_index[(b["ts_et"].date().isoformat(), bucket)] = b["volume"]

    # Per-bucket SIP and IEX volume samples (one per day).
    sip_by_bucket: dict[str, list[int]] = {}
    iex_by_bucket: dict[str, list[int]] = {}

    # For the IEX/SIP scaling ratio we sum across the full window per feed.
    sip_total_for_ratio = 0
    iex_total_for_ratio = 0

    for b in sip_bars:
        bucket = session_bucket(b["ts_et"])
        if bucket is None:
            continue
        d_iso = b["ts_et"].date().isoformat()
        sip_v = b["volume"]
        if sip_v <= 0:
            # Drop anomalous days/minutes (holiday miss, feed bug).
            continue
        iex_v = iex_index.get((d_iso, bucket), 0)
        sip_by_bucket.setdefault(bucket, []).append(sip_v)
        iex_by_bucket.setdefault(bucket, []).append(iex_v)
        sip_total_for_ratio += sip_v
        iex_total_for_ratio += iex_v

    # Per-ticker IEX/SIP ratio (single scalar). Guard zero division.
    if sip_total_for_ratio <= 0:
        ratio = 0.0
    else:
        ratio = iex_total_for_ratio / sip_total_for_ratio

    buckets: dict[str, dict] = {}
    for bucket, sip_samples in sip_by_bucket.items():
        if not sip_samples:
            continue
        # Build the IEX-scale baseline. If we have direct IEX samples for the
        # bucket, use those; otherwise scale SIP by the global ratio. The
        # direct-sample path is the §17.1 default.
        iex_samples = [v for v in iex_by_bucket.get(bucket, []) if v > 0]
        if iex_samples:
            samples = iex_samples
        else:
            samples = [int(round(v * ratio)) for v in sip_samples if v > 0]
        if not samples:
            continue
        median_v = int(statistics.median(samples))
        if len(samples) >= 4:
            qs = statistics.quantiles(samples, n=20)  # 19 cut points => p5..p95
            p75_v = int(qs[14])
            p90_v = int(qs[17])
        else:
            sorted_samples = sorted(samples)
            p75_v = int(sorted_samples[max(0, int(0.75 * (len(sorted_samples) - 1)))])
            p90_v = int(sorted_samples[max(0, int(0.90 * (len(sorted_samples) - 1)))])
        buckets[bucket] = {
            "median": median_v,
            "p75": p75_v,
            "p90": p90_v,
            "n": len(samples),
        }

    return {
        "version": PROFILE_VERSION,
        "ticker": ticker,
        "feed_baseline": "sip",
        "feed_live": "iex",
        "iex_sip_ratio": round(ratio, 6),
        "window_trading_days": WINDOW_TRADING_DAYS,
        "build_ts_utc": _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# G4 evaluator (V-P1 grid)
# ---------------------------------------------------------------------------

# Module-level toggle. Trade_genius flips this False if TICKERS exceeds the
# free-plan IEX websocket cap (30 symbols). When False, evaluate_g4 returns
# a "DISABLED" result and the bot trades normally.
VOLUME_PROFILE_ENABLED: bool = True


def _bucket_median(profile: dict | None, bucket: str) -> int | None:
    if not profile:
        return None
    b = profile.get("buckets", {}).get(bucket)
    if not b:
        return None
    return int(b.get("median", 0)) or None


def evaluate_g4(
    ticker: str,
    minute_bucket: str,
    current_volume: int,
    profile: dict | None,
    qqq_current_volume: int,
    qqq_profile: dict | None,
    stage: int,
) -> dict:
    """V-P1 grid evaluator. See brief §17.2.

    Stage 1 (Jab):    ticker >= 120% AND qqq >= 100%   (V-P1-R1 + V-P1-R2)
    Stage 2 (Strike): ticker >= 100%                   (V-P1-R3)

    Returns a dict with keys: green, reason, ticker_pct, qqq_pct, rule.
    """
    if not VOLUME_PROFILE_ENABLED:
        return {
            "green": False,
            "reason": "DISABLED",
            "ticker_pct": None,
            "qqq_pct": None,
            "rule": "V-P1-R0",
        }

    rule = "V-P1-R1" if stage == 1 else "V-P1-R3"

    if profile is None:
        return {
            "green": False,
            "reason": f"NO_PROFILE_{ticker}",
            "ticker_pct": None,
            "qqq_pct": None,
            "rule": rule,
        }
    if is_profile_stale(profile, _utc_now()):
        return {
            "green": False,
            "reason": f"STALE_PROFILE_{ticker}",
            "ticker_pct": None,
            "qqq_pct": None,
            "rule": rule,
        }

    median_v = _bucket_median(profile, minute_bucket)
    if not median_v:
        return {
            "green": False,
            "reason": f"NO_BUCKET_{ticker}_{minute_bucket}",
            "ticker_pct": None,
            "qqq_pct": None,
            "rule": rule,
        }

    ticker_pct = (current_volume / median_v) * 100.0

    if stage == 1:
        # Stage 1 also needs the QQQ "Market Tide" confirmation.
        if qqq_profile is None:
            return {
                "green": False,
                "reason": "NO_PROFILE_QQQ",
                "ticker_pct": round(ticker_pct, 2),
                "qqq_pct": None,
                "rule": "V-P1-R2",
            }
        if is_profile_stale(qqq_profile, _utc_now()):
            return {
                "green": False,
                "reason": "STALE_PROFILE_QQQ",
                "ticker_pct": round(ticker_pct, 2),
                "qqq_pct": None,
                "rule": "V-P1-R2",
            }
        qqq_median = _bucket_median(qqq_profile, minute_bucket)
        if not qqq_median:
            return {
                "green": False,
                "reason": f"NO_BUCKET_QQQ_{minute_bucket}",
                "ticker_pct": round(ticker_pct, 2),
                "qqq_pct": None,
                "rule": "V-P1-R2",
            }
        qqq_pct = (qqq_current_volume / qqq_median) * 100.0

        green = (ticker_pct >= 120.0) and (qqq_pct >= 100.0)
        if green:
            reason = "OK_STAGE1"
        elif ticker_pct < 120.0 and qqq_pct < 100.0:
            reason = "LOW_TICKER_AND_QQQ"
        elif ticker_pct < 120.0:
            reason = "LOW_TICKER"
        else:
            reason = "LOW_QQQ"
        return {
            "green": green,
            "reason": reason,
            "ticker_pct": round(ticker_pct, 2),
            "qqq_pct": round(qqq_pct, 2),
            "rule": rule,
        }

    # Stage 2 (Strike): ticker maintenance only.
    green = ticker_pct >= 100.0
    return {
        "green": green,
        "reason": "OK_STAGE2" if green else "LOW_TICKER",
        "ticker_pct": round(ticker_pct, 2),
        "qqq_pct": None,
        "rule": rule,
    }


# ---------------------------------------------------------------------------
# v5.1.1 \u2014 env-driven A/B toggles for the live volume gate.
# ---------------------------------------------------------------------------
# v5.1.0 was hard-coded ticker \u2265120% AND QQQ \u2265100%. v5.1.1 lets us
# A/B-test ticker-only vs QQQ-only vs both anchors via env without
# redeploying. The "active config" (read from env at module-import
# time) is the one that would gate trades if VOL_GATE_ENFORCE=1. It
# defaults to TICKER+QQQ at 70/100 with enforcement OFF.


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def load_active_config() -> dict:
    """Return the env-driven 'active' config. Read at module load and on
    each call (cheap; os.getenv is dict-lookup) so tests can monkey-patch
    env without re-importing the module."""
    enforce = _env_bool("VOL_GATE_ENFORCE", False)
    ticker_enabled = _env_bool("VOL_GATE_TICKER_ENABLED", True)
    index_enabled = _env_bool("VOL_GATE_INDEX_ENABLED", True)
    ticker_pct = _env_int("VOL_GATE_TICKER_PCT", 70)
    index_pct = _env_int("VOL_GATE_QQQ_PCT", 100)
    index_symbol = (os.getenv("VOL_GATE_INDEX_SYMBOL") or "QQQ").strip().upper() or "QQQ"
    return {
        "enforce": enforce,
        "ticker_enabled": ticker_enabled,
        "index_enabled": index_enabled,
        "ticker_pct": ticker_pct,
        "index_pct": index_pct,
        "index_symbol": index_symbol,
    }


# ---------------------------------------------------------------------------
# Nightly rebuild
# ---------------------------------------------------------------------------


def rebuild_all_profiles(
    tickers: list[str],
    alpaca_key: str,
    alpaca_secret: str,
    end_dt_utc: datetime | None = None,
) -> dict[str, str]:
    """Rebuild profiles for every ticker. Returns {ticker: status} where
    status is 'ok' or 'error: <msg>'. Caller handles cadence / scheduling.
    """
    if end_dt_utc is None:
        end_dt_utc = _utc_now()
    out: dict[str, str] = {}
    for t in tickers:
        try:
            logger.info("[VOLPROFILE] rebuilding %s...", t)
            prof = build_profile(t, end_dt_utc, alpaca_key, alpaca_secret)
            save_profile(t, prof)
            out[t] = "ok"
        except Exception as e:
            logger.error("[VOLPROFILE] rebuild %s failed: %s", t, e)
            out[t] = f"error: {type(e).__name__}: {e}"
    return out


# ---------------------------------------------------------------------------
# Websocket bar consumer
# ---------------------------------------------------------------------------


class WebsocketBarConsumer:
    """Persistent /iex websocket subscription. Maintains an in-memory
    {ticker: {minute_bucket: latest_bar_volume}} table. Reconnects with
    jittered backoff and replays the last 5 minutes via REST on resume.

    The consumer runs in a daemon thread; callers read snapshots through
    `current_volume(ticker, bucket)` synchronously.
    """

    def __init__(self, tickers: list[str], alpaca_key: str, alpaca_secret: str):
        self._tickers = list(tickers)
        self._key = alpaca_key
        self._secret = alpaca_secret
        self._volumes: dict[str, dict[str, int]] = {t: {} for t in self._tickers}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream = None
        # v5.5.5 \u2014 observability + watchdog state.
        self._bars_received: int = 0
        self._last_bar_ts: datetime | None = None
        self._last_handler_error: str | None = None
        self._first_sample_logged: int = 0
        self._start_ts: datetime | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_reconnects: int = 0
        # Silence threshold (seconds) before the watchdog forces a reconnect.
        # Configurable via VOLPROFILE_WATCHDOG_SEC; clamped to >= 30s so a
        # misconfigured tiny value can't churn the WS.
        try:
            raw = int(os.getenv("VOLPROFILE_WATCHDOG_SEC", "120") or "120")
        except ValueError:
            raw = 120
        self._silence_threshold_sec: int = max(30, raw)

    # ---- public ----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._start_ts = datetime.now(UTC)
        self._thread = threading.Thread(target=self._run_forever, name="VolProfileWS", daemon=True)
        self._thread.start()
        # v5.5.5 \u2014 watchdog: detect "WS idle" (handshake succeeded but no
        # bar messages arriving) and force a reconnect.
        if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="VolProfileWatchdog",
                daemon=True,
            )
            self._watchdog_thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._stream is not None:
                self._stream.stop()
        except Exception:
            pass

    def current_volume(self, ticker: str, minute_bucket: str) -> int | None:
        with self._lock:
            buckets = self._volumes.get(ticker)
            if not buckets:
                return None
            v = buckets.get(minute_bucket)
            return int(v) if v is not None else None

    def stats_snapshot(self) -> dict:
        """v5.5.5 \u2014 thread-safe observability snapshot for /api/ws_state."""
        with self._lock:
            last = self._last_bar_ts.isoformat() if self._last_bar_ts else None
            return {
                "bars_received": self._bars_received,
                "last_bar_ts": last,
                "last_handler_error": self._last_handler_error,
                "volumes_size_per_symbol": {sym: len(d) for sym, d in self._volumes.items()},
                "tickers": list(self._tickers),
                "watchdog_reconnects": self._watchdog_reconnects,
                "silence_threshold_sec": self._silence_threshold_sec,
            }

    def time_since_last_bar_seconds(self) -> float | None:
        """Seconds since the last bar was processed by ``_on_bar``.

        None when no bar has ever been received in this consumer's lifetime.
        """
        with self._lock:
            last = self._last_bar_ts
        if last is None:
            return None
        now = datetime.now(UTC)
        return max(0.0, (now - last).total_seconds())

    # ---- internals ----

    async def _on_bar(self, bar) -> None:
        # v5.5.4 hotfix: alpaca-py StockDataStream.subscribe_bars() requires a
        # coroutine function (async def). Registering a plain `def` handler
        # raises "handler must be a coroutine function" inside run() and
        # crash-loops the WS consumer. The body itself is purely sync \u2014 we
        # just need the function to be a coroutine function.
        try:
            sym = getattr(bar, "symbol", None)
            ts = getattr(bar, "timestamp", None)
            vol = getattr(bar, "volume", None)
            if sym is None or ts is None or vol is None:
                return
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            bucket = session_bucket(ts.astimezone(ET))
            if bucket is None:
                return
            with self._lock:
                self._volumes.setdefault(sym, {})[bucket] = int(vol)
                # v5.5.5 \u2014 observability: count + timestamp every successful
                # bar so the watchdog and /api/ws_state can discriminate
                # "WS idle" from "handler error" from "everything is fine".
                self._bars_received += 1
                self._last_bar_ts = datetime.now(UTC)
                received = self._bars_received
                logged = self._first_sample_logged
                if logged < 5:
                    self._first_sample_logged = logged + 1
                    sample_now = True
                else:
                    sample_now = False
            if sample_now:
                logger.info(
                    "[VOLPROFILE] sample bar #%d sym=%s ts=%s vol=%s bucket=%s",
                    received,
                    sym,
                    ts,
                    vol,
                    bucket,
                )
            if received % 100 == 0:
                logger.info(
                    "[VOLPROFILE] heartbeat: total=%d last_sym=%s",
                    received,
                    sym,
                )
        except Exception as e:
            self._last_handler_error = f"{type(e).__name__}: {e}"
            logger.warning("[VOLPROFILE] ws bar handler error: %s", e)

    def _replay_last_5min(self) -> None:
        try:
            client = _historical_client(self._key, self._secret)
            end = _utc_now()
            start = end - timedelta(minutes=5)
            for t in self._tickers:
                try:
                    bars = _fetch_1m_bars(client, t, start, end, "iex")
                    for b in bars:
                        bucket = session_bucket(b["ts_et"])
                        if bucket is None:
                            continue
                        with self._lock:
                            self._volumes.setdefault(t, {})[bucket] = int(b["volume"])
                except Exception as e:
                    logger.warning("[VOLPROFILE] replay %s failed: %s", t, e)
        except Exception as e:
            logger.warning("[VOLPROFILE] replay skipped: %s", e)

    def _watchdog_loop(self) -> None:
        """v5.5.5 \u2014 monitor WS bar liveness; force reconnect on long silence.

        Polls every 30s. While the regular session is open (RTH gate), if
        ``_bars_received == 0`` for >= silence-threshold seconds since
        ``start()`` was called, OR more than silence-threshold seconds have
        elapsed since the last bar arrived, calls ``self._stream.stop()`` so
        ``_run_forever``'s outer reconnect loop kicks in. Always
        defensively catches its own exceptions \u2014 the watchdog must never
        crash silently.
        """
        poll_sec = 30.0
        while not self._stop.is_set():
            # First: sleep up front so we don't fire immediately on start.
            # ``Event.wait`` returns True if .set() was called \u2014 in that
            # case break out cleanly.
            if self._stop.wait(poll_sec):
                return
            try:
                # RTH gate: only force reconnects during regular trading
                # hours. Outside the session a stalled WS is expected.
                now_et = datetime.now(ET)
                if session_bucket(now_et) is None:
                    continue
                last = self.time_since_last_bar_seconds()
                threshold = self._silence_threshold_sec
                if last is None:
                    # Never received a bar: measure from start() time.
                    started = self._start_ts
                    if started is None:
                        continue
                    elapsed = (datetime.now(UTC) - started).total_seconds()
                    if elapsed < threshold:
                        continue
                else:
                    if last < threshold:
                        continue
                    elapsed = last
                logger.warning(
                    "[VOLPROFILE] watchdog: no bars for %.0fs (received=%d) \u2014 forcing reconnect",
                    elapsed,
                    self._bars_received,
                )
                self._watchdog_reconnects += 1
                stream = self._stream
                if stream is not None:
                    try:
                        stream.stop()
                    except Exception as e:
                        logger.warning(
                            "[VOLPROFILE] watchdog stream.stop() failed: %s",
                            e,
                        )
            except Exception as e:
                logger.warning("[VOLPROFILE] watchdog loop error: %s", e)
                # Never let the watchdog die.
                continue

    def _run_forever(self) -> None:
        # Lazy import — keeps the rest of the module testable in
        # environments without the websocket stack ready.
        try:
            from alpaca.data.live import StockDataStream
            from alpaca.data.enums import DataFeed
        except Exception as e:
            logger.error("[VOLPROFILE] alpaca-py live imports unavailable: %s", e)
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._stream = StockDataStream(self._key, self._secret, feed=DataFeed.IEX)
                self._stream.subscribe_bars(self._on_bar, *self._tickers)
                self._replay_last_5min()
                logger.info("[VOLPROFILE] /iex websocket connecting (n=%d)", len(self._tickers))
                # StockDataStream.run() blocks until the connection drops.
                self._stream.run()
                # If run() returns cleanly, treat it as a disconnect we
                # want to reconnect from (unless stop was requested).
                if self._stop.is_set():
                    return
                logger.warning("[VOLPROFILE] websocket run() returned; reconnecting")
            except Exception as e:
                logger.warning("[VOLPROFILE] websocket error: %s; reconnecting", e)
            # Jittered backoff capped at 30s; brief told us "every 5s with
            # jittered backoff".
            sleep_for = min(30.0, backoff)
            # Mild jitter so multiple processes don't sync up after an
            # outage.
            sleep_for += (hash((time.time_ns(), id(self))) % 100) / 100.0
            time.sleep(sleep_for)
            backoff = min(30.0, backoff * 1.7)
