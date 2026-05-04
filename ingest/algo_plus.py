"""ingest/algo_plus.py \u2014 Always-On Algo Plus ingest module.

v6.5.0 M-1: Implements the always-on bar ingest pipeline for TradeGenius.

Components:
  - ConnectionHealth: 5-state machine (CONNECTING / LIVE / DEGRADED /
    RECONNECTING / REST_ONLY) with a thread-safe module-level singleton.
  - BarAssembler: validates schema, fills trade_count + bar_vwap, writes
    via bar_archive.write_bar(). Sets feed_source=\"sip\" on every bar.
  - GapDetector: detects spans of >= GAP_THRESHOLD_MINUTES consecutive
    missing 1-minute bars from the daily JSONL archive.
  - RestBackfillWorker: background thread that dequeues (ticker, start_ts,
    end_ts) gap tuples, fetches via Alpaca REST (feed=sip, limit=1000),
    deduplicates against existing JSONL timestamps, and writes new bars.
  - AlgoPlusIngest: top-level orchestrator. start() / stop().
  - ingest_loop(): long-running daemon target with exponential backoff.
  - _resolve_alpaca_creds(): VAL_ALPACA_PAPER_KEY -> GENE_ALPACA_PAPER_KEY
    -> (None, None). Emits [INGEST SHADOW DISABLED] WARNING when no creds.
  - _ingest_health_snapshot(): returns the dict served by P-6 /api/state.

Constraints:
  - No literal em-dashes. All em-dash characters encoded as \\u2014.
  - Forbidden terms: not present (use fetch/poll/collect instead).
  - Thread-safe writes; does not share mutable state with scan loop.
  - write_bar() failures are swallowed per bar_archive contract.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger("ingest.algo_plus")

# ---------------------------------------------------------------------------
# Backoff schedule (seconds)
# ---------------------------------------------------------------------------
_BACKOFF_SCHEDULE = [5, 10, 20, 40, 80, 160, 300]

GAP_THRESHOLD_MINUTES = 3  # gaps of >= 3 consecutive missing 1-min bars trigger backfill


def _backoff(attempt: int) -> float:
    """Return backoff duration for the given attempt index (0-based)."""
    return float(_BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)])


# ---------------------------------------------------------------------------
# ConnectionHealth state machine
# ---------------------------------------------------------------------------

CONNECTING = "CONNECTING"
LIVE = "LIVE"
DEGRADED = "DEGRADED"
RECONNECTING = "RECONNECTING"
REST_ONLY = "REST_ONLY"

_VALID_STATES = {CONNECTING, LIVE, DEGRADED, RECONNECTING, REST_ONLY}


class ConnectionHealth:
    """Thread-safe connection health state machine.

    States: CONNECTING / LIVE / DEGRADED / RECONNECTING / REST_ONLY.
    Module-level singleton accessed via the module functions .set() / .get().
    """

    def __init__(self) -> None:
        self._state: str = CONNECTING
        self._lock = threading.Lock()
        self._last_bar_ts: Optional[float] = None  # wall-clock time of last bar received

    def set(self, state: str) -> None:
        if state not in _VALID_STATES:
            raise ValueError(f"Invalid ConnectionHealth state: {state!r}")
        with self._lock:
            old = self._state
            self._state = state
        if old != state:
            logger.info("[INGEST] ConnectionHealth: %s -> %s", old, state)

    def get(self) -> str:
        with self._lock:
            return self._state

    def record_bar(self) -> None:
        """Record that a bar was received right now (updates last_bar wall time)."""
        with self._lock:
            self._last_bar_ts = time.monotonic()

    def last_bar_age_s(self) -> Optional[float]:
        """Seconds since last bar was received, or None if no bar yet."""
        with self._lock:
            if self._last_bar_ts is None:
                return None
            return time.monotonic() - self._last_bar_ts


# Module-level singleton
_health = ConnectionHealth()


def get_health() -> ConnectionHealth:
    """Return the module-level ConnectionHealth singleton."""
    return _health


# ---------------------------------------------------------------------------
# Credential resolution (P-1 / M-1)
# ---------------------------------------------------------------------------

def _resolve_alpaca_creds() -> Tuple[Optional[str], Optional[str]]:
    """Resolve Alpaca API credentials for the ingest worker.

    Resolution order (per spec section 3.3 and P-1 correction):
      1. VAL_ALPACA_PAPER_KEY / VAL_ALPACA_PAPER_SECRET
      2. GENE_ALPACA_PAPER_KEY / GENE_ALPACA_PAPER_SECRET

    Returns (key, secret) tuple, or (None, None) if neither set.
    Emits [INGEST SHADOW DISABLED] WARNING when no credentials are found.
    """
    key = os.getenv("VAL_ALPACA_PAPER_KEY", "").strip()
    secret = os.getenv("VAL_ALPACA_PAPER_SECRET", "").strip()
    if key and secret:
        logger.info("[INGEST] using VAL_ALPACA_PAPER_KEY credentials")
        return key, secret

    key = os.getenv("GENE_ALPACA_PAPER_KEY", "").strip()
    secret = os.getenv("GENE_ALPACA_PAPER_SECRET", "").strip()
    if key and secret:
        logger.info("[INGEST] using GENE_ALPACA_PAPER_KEY credentials")
        return key, secret

    logger.warning(
        "[INGEST SHADOW DISABLED] no Alpaca creds found "
        "(VAL_ALPACA_PAPER_KEY / GENE_ALPACA_PAPER_KEY unset). "
        "Always-on ingest will not start; shadow_positions will not record."
    )
    return None, None


# ---------------------------------------------------------------------------
# BarAssembler
# ---------------------------------------------------------------------------

class BarAssembler:
    """Validates incoming bar data, fills optional fields, writes to archive.

    Each bar written includes feed_source=\"sip\" to tag SIP provenance
    per M-4 (BAR_SCHEMA_FIELDS now includes feed_source).
    """

    def accept(self, ticker: str, bar_data: dict) -> bool:
        """Validate, enrich, and persist a bar dict.

        Returns True on successful write, False on any failure.
        Failures are logged but never re-raised (bar_archive contract).
        """
        if not isinstance(bar_data, dict):
            logger.debug("[INGEST] BarAssembler: rejected non-dict bar for %s", ticker)
            return False

        # Require minimum fields
        ts = bar_data.get("ts") or bar_data.get("timestamp")
        if ts is None:
            logger.debug("[INGEST] BarAssembler: missing ts for %s", ticker)
            return False

        bar: dict = dict(bar_data)

        # Normalise timestamp to ISO string if it is a datetime
        if isinstance(ts, datetime):
            bar["ts"] = ts.isoformat()
        elif not isinstance(ts, str):
            bar["ts"] = str(ts)

        # Fill trade_count and bar_vwap if not already present (Alpaca SIP
        # supplies these; Yahoo-path bars default to None per schema).
        bar.setdefault("trade_count", bar_data.get("trade_count"))
        bar.setdefault("bar_vwap", bar_data.get("vwap"))

        # Tag feed provenance (M-4)
        bar["feed_source"] = "sip"

        try:
            import bar_archive as _ba
            _ba.write_bar(ticker, bar)
            _health.record_bar()
            return True
        except Exception as e:
            logger.debug("[INGEST] BarAssembler: write_bar failed for %s: %s", ticker, e)
            return False


# ---------------------------------------------------------------------------
# GapDetector
# ---------------------------------------------------------------------------

def _bar_data_dir(date_str: str) -> str:
    """Return the path to today's bar directory."""
    base = os.environ.get("BAR_ARCHIVE_BASE", "/data/bars")
    return os.path.join(base, date_str)


def _read_ts_set(ticker: str, date_str: str) -> set:
    """Read all timestamp strings already in the JSONL for (ticker, date)."""
    ts_set: set = set()
    path = os.path.join(_bar_data_dir(date_str), f"{ticker}.jsonl")
    if not os.path.exists(path):
        return ts_set
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    ts_val = row.get("ts")
                    if ts_val is not None:
                        ts_set.add(str(ts_val))
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        pass
    return ts_set


def _count_bars_today(date_str: str) -> int:
    """Count total bars written across all tickers for today."""
    d = _bar_data_dir(date_str)
    if not os.path.exists(d):
        return 0
    total = 0
    try:
        for fname in os.listdir(d):
            if not fname.endswith(".jsonl"):
                continue
            path = os.path.join(d, fname)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    total += sum(1 for ln in fh if ln.strip())
            except OSError:
                pass
    except OSError:
        pass
    return total


class GapDetector:
    """Detects spans of >= GAP_THRESHOLD_MINUTES consecutive missing 1-min bars.

    Reads /data/bars/TODAY/{ticker}.jsonl, extracts all 'ts' values,
    and finds time spans within (session_start, now) where no bar exists.
    Returns a list of (gap_start_utc, gap_end_utc) datetime tuples.
    """

    def detect_gaps(
        self,
        ticker: str,
        session_start: datetime,
        now: datetime,
    ) -> list:
        """Return list of (gap_start_utc, gap_end_utc) tuples for ticker on today's session.

        Only reports gaps of >= GAP_THRESHOLD_MINUTES consecutive missing minutes.
        """
        date_str = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        ts_set = _read_ts_set(ticker, date_str)

        gaps: list = []
        if session_start.tzinfo is None:
            session_start = session_start.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Walk minute-by-minute through the session window
        cursor = session_start.replace(second=0, microsecond=0)
        gap_start: Optional[datetime] = None

        while cursor < now:
            # Check if a bar exists for this minute bucket
            # Alpaca bar timestamps use the open of the minute
            ts_candidates = [
                cursor.isoformat(),
                cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                cursor.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                cursor.strftime("%Y-%m-%dT%H:%M:%S"),
            ]
            bar_present = any(ts in ts_set for ts in ts_candidates)

            if not bar_present:
                if gap_start is None:
                    gap_start = cursor
            else:
                if gap_start is not None:
                    gap_len = int((cursor - gap_start).total_seconds() / 60)
                    if gap_len >= GAP_THRESHOLD_MINUTES:
                        gaps.append((gap_start, cursor))
                    gap_start = None

            cursor += timedelta(minutes=1)

        # Close trailing gap
        if gap_start is not None:
            gap_len = int((now - gap_start).total_seconds() / 60)
            if gap_len >= GAP_THRESHOLD_MINUTES:
                gaps.append((gap_start, now))

        return gaps


# ---------------------------------------------------------------------------
# RestBackfillWorker
# ---------------------------------------------------------------------------

class RestBackfillWorker:
    """Background thread that dequeues (ticker, start_ts, end_ts) gap tuples
    and fetches missing bars from Alpaca REST (feed=sip, 1Min, limit=1000).

    Deduplicates against existing JSONL timestamps before writing.
    Sleep of 0.35s between requests keeps rate within Algo Plus 200 req/min.
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._key: Optional[str] = None
        self._secret: Optional[str] = None

    def configure(self, key: str, secret: str) -> None:
        self._key = key
        self._secret = secret

    def enqueue(self, ticker: str, start_ts: datetime, end_ts: datetime) -> None:
        self._q.put((ticker, start_ts, end_ts))

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="ingest_backfill",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                ticker, start_ts, end_ts = self._q.get(timeout=5.0)
            except queue.Empty:
                continue
            try:
                self._backfill(ticker, start_ts, end_ts)
            except Exception as e:
                logger.warning("[INGEST] backfill error for %s: %s", ticker, e)
            time.sleep(0.35)

    def _backfill(self, ticker: str, start_ts: datetime, end_ts: datetime) -> None:
        """Fetch bars for (ticker, start_ts, end_ts) and write new ones only."""
        _backfill_start = time.time()  # v6.6.0: elapsed timer for SLA
        if not self._key or not self._secret:
            return
        try:
            from alpaca.data import StockHistoricalDataClient  # type: ignore
            from alpaca.data.requests import StockBarsRequest  # type: ignore
            from alpaca.data.timeframe import TimeFrame  # type: ignore
        except Exception as e:
            logger.debug("[INGEST] backfill: alpaca-py unavailable: %s", e)
            return

        client = StockHistoricalDataClient(
            api_key=self._key,
            secret_key=self._secret,
        )

        date_str = start_ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
        existing_ts = _read_ts_set(ticker, date_str)

        try:
            req = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Minute,
                start=start_ts,
                end=end_ts,
                feed="sip",
                limit=1000,
            )
            resp = client.get_stock_bars(req)
        except Exception as e:
            logger.debug("[INGEST] backfill REST failed %s: %s", ticker, e)
            return

        rows: list = []
        try:
            if hasattr(resp, "data"):
                rows = resp.data.get(ticker, []) or []
        except Exception:
            rows = []

        written = 0
        for b in rows:
            try:
                ts_obj = getattr(b, "timestamp", None)
                if ts_obj is None:
                    continue
                if ts_obj.tzinfo is None:
                    ts_obj = ts_obj.replace(tzinfo=timezone.utc)
                ts_str = ts_obj.isoformat()
                if ts_str in existing_ts:
                    continue
                bar_dict = {
                    "ts": ts_str,
                    "open": float(getattr(b, "open", 0) or 0),
                    "high": float(getattr(b, "high", 0) or 0),
                    "low": float(getattr(b, "low", 0) or 0),
                    "close": float(getattr(b, "close", 0) or 0),
                    "iex_volume": None,
                    "iex_sip_ratio_used": None,
                    "bid": None,
                    "ask": None,
                    "last_trade_price": None,
                    "trade_count": getattr(b, "trade_count", None),
                    "bar_vwap": getattr(b, "vwap", None),
                    "feed_source": "sip",
                }
                import bar_archive as _ba
                _ba.write_bar(ticker, bar_dict)
                existing_ts.add(ts_str)
                written += 1
            except Exception as e:
                logger.debug("[INGEST] backfill write error for %s: %s", ticker, e)

        if written:
            logger.info("[INGEST] backfilled %d bars for %s (%s -> %s)",
                        written, ticker,
                        start_ts.isoformat(), end_ts.isoformat())

        # v6.6.0 Pillar B: record backfill completion + inline verification (Decision A3)
        _elapsed_s = time.time() - _backfill_start
        try:
            from ingest.audit import AuditLog as _AL
            _AL.record_backfill_completed(
                ticker=ticker,
                gap_start=start_ts,
                gap_end=end_ts,
                bars_written=written,
            )
            _verify_gap_closed(ticker, start_ts, end_ts)
        except Exception as _ae:
            logger.debug("[INGEST] audit record failed: %s", _ae)

        # v6.6.0 Pillar A: record backfill latency for SLA
        try:
            from ingest.sla import record_backfill_completed as _sla_backfill
            _sla_backfill(ticker, start_ts, end_ts, written, _elapsed_s)
        except Exception:
            pass


def _verify_gap_closed(
    ticker: str,
    start_ts: "datetime",
    end_ts: "datetime",
) -> None:
    """Re-scan the archive after backfill and update audit status.

    Decision A3: inline verification — one extra archive read per gap.
    Sets status to 'closed' if all minutes covered, else 'missing'.
    """
    try:
        date_str = start_ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
        existing_ts = _read_ts_set(ticker, date_str)
        from datetime import timedelta as _td
        expected_ts = set()
        cur = start_ts.astimezone(timezone.utc)
        end_utc = end_ts.astimezone(timezone.utc)
        while cur < end_utc:
            expected_ts.add(cur.isoformat())
            cur = cur + _td(minutes=1)
        gaps_remaining = len(expected_ts - existing_ts)
        status = "closed" if gaps_remaining == 0 else "missing"
        from ingest.audit import AuditLog as _AL
        _AL.record_verification(ticker=ticker, gap_start=start_ts, status=status)
        if status == "missing":
            logger.warning(
                "[INGEST] gap verification MISSING for %s (%s -> %s): "
                "%d minute(s) still absent after backfill",
                ticker, start_ts.isoformat(), end_ts.isoformat(), gaps_remaining,
            )
    except Exception as e:
        logger.debug("[INGEST] _verify_gap_closed error for %s: %s", ticker, e)


# ---------------------------------------------------------------------------
# AlgoPlusIngest orchestrator
# ---------------------------------------------------------------------------

# Module-level stats for health snapshot
_ingest_stats: dict = {
    "status": "unconfigured",
    "last_bar_age_s": None,
    "open_gaps_today": 0,
    "bars_today": 0,
    "ws_state": CONNECTING,
}
_ingest_stats_lock = threading.Lock()


def _update_ingest_stats(**kwargs: object) -> None:
    with _ingest_stats_lock:
        _ingest_stats.update(kwargs)
    # v6.6.0 Pillar A: propagate stats to SLA collector (Decision A1)
    try:
        from ingest.sla import update_global_stats as _sla_update
        _sla_update(
            last_bar_age_s=_health.last_bar_age_s(),
            open_gaps_today=kwargs.get("open_gaps_today"),
        )
    except Exception:
        pass


class AlgoPlusIngest:
    """Top-level orchestrator for always-on Algo Plus ingest.

    Holds StockDataStream, BarAssembler, ConnectionHealth, GapDetector,
    RestBackfillWorker. Use start() / stop().
    """

    def __init__(self, key: str, secret: str) -> None:
        self._key = key
        self._secret = secret
        self._assembler = BarAssembler()
        self._gap_detector = GapDetector()
        self._backfill = RestBackfillWorker()
        self._backfill.configure(key, secret)
        self._stream = None
        self._tickers: list = []
        self._stop_event = threading.Event()

    def _get_tickers(self) -> list:
        """Return TICKERS from trade_genius if importable, else empty list."""
        try:
            import trade_genius as _tg
            return list(getattr(_tg, "TICKERS", []) or [])
        except Exception:
            return []

    def start(self) -> None:
        """Start the WebSocket stream and backfill worker."""
        self._stop_event.clear()
        self._backfill.start()
        self._tickers = self._get_tickers()
        _health.set(CONNECTING)
        _update_ingest_stats(status="unconfigured", ws_state=CONNECTING)
        # v6.5.0 \u2014 register module-level singleton so gap_detect_task()
        # in trade_genius.py can find the active backfill worker queue.
        import sys as _sys
        _mod = _sys.modules.get(__name__)
        if _mod is not None:
            setattr(_mod, "_current_ingest", self)
        self._run_ws_ingest()

    def stop(self) -> None:
        """Signal the orchestrator to stop."""
        self._stop_event.set()
        self._backfill.stop()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            self._stream = None
        # v6.5.0 \u2014 clear singleton on stop.
        import sys as _sys
        _mod = _sys.modules.get(__name__)
        if _mod is not None and getattr(_mod, "_current_ingest", None) is self:
            setattr(_mod, "_current_ingest", None)

    def _run_ws_ingest(self) -> None:
        """Connect WebSocket, subscribe to tickers, block until disconnect."""
        try:
            from alpaca.data.live import StockDataStream  # type: ignore
            from alpaca.data.enums import DataFeed  # type: ignore
        except ImportError:
            logger.warning("[INGEST] alpaca-py StockDataStream unavailable; REST_ONLY mode")
            _health.set(REST_ONLY)
            _update_ingest_stats(status="offline", ws_state=REST_ONLY)
            return

        if not self._tickers:
            self._tickers = self._get_tickers()

        if not self._tickers:
            logger.warning("[INGEST] no tickers configured; WebSocket not started")
            return

        stream = StockDataStream(
            api_key=self._key,
            secret_key=self._secret,
            feed=DataFeed.SIP,
        )
        self._stream = stream

        assembler = self._assembler

        async def _on_bar(bar):  # type: ignore
            try:
                ticker = str(getattr(bar, "symbol", "")).upper()
                if not ticker:
                    return
                bar_dict = {
                    "ts": getattr(bar, "timestamp", None),
                    "open": float(getattr(bar, "open", 0) or 0),
                    "high": float(getattr(bar, "high", 0) or 0),
                    "low": float(getattr(bar, "low", 0) or 0),
                    "close": float(getattr(bar, "close", 0) or 0),
                    "iex_volume": None,
                    "iex_sip_ratio_used": None,
                    "bid": None,
                    "ask": None,
                    "last_trade_price": None,
                    "trade_count": getattr(bar, "trade_count", None),
                    "bar_vwap": getattr(bar, "vwap", None),
                    "feed_source": "sip",
                }
                assembler.accept(ticker, bar_dict)
                if _health.get() != LIVE:
                    _health.set(LIVE)
                    _update_ingest_stats(status="live", ws_state=LIVE)
            except Exception as e:
                logger.debug("[INGEST] on_bar callback error: %s", e)

        try:
            stream.subscribe_bars(_on_bar, *self._tickers)
            _health.set(CONNECTING)
            stream.run()
        except Exception as e:
            logger.warning("[INGEST] WebSocket stream error: %s", e)
            _health.set(DEGRADED)
            _update_ingest_stats(status="degraded", ws_state=DEGRADED)
            raise
        finally:
            self._stream = None


# ---------------------------------------------------------------------------
# ingest_loop (daemon target)
# ---------------------------------------------------------------------------

def ingest_loop() -> None:
    """Long-running daemon target. Boots ingest; re-connects on failure.

    Pattern per spec section 4.1:
      try: _run_ws_ingest()
      except: log + set DEGRADED + sleep(_backoff())
    Backoff schedule [5, 10, 20, 40, 80, 160, 300] seconds.
    """
    key, secret = _resolve_alpaca_creds()
    if key is None:
        # [INGEST SHADOW DISABLED] already logged in _resolve_alpaca_creds
        _update_ingest_stats(status="unconfigured", ws_state=CONNECTING)
        return  # do not start the ingest loop at all

    ingest = AlgoPlusIngest(key, secret)
    attempt = 0

    while True:
        try:
            _health.set(CONNECTING)
            _update_ingest_stats(status="unconfigured", ws_state=CONNECTING)
            ingest._run_ws_ingest()
            # If _run_ws_ingest returns without exception, the stream
            # ended cleanly. Treat as a graceful reconnect opportunity.
            attempt = 0
        except Exception as e:
            logger.error("[INGEST] worker crashed (attempt %d): %s", attempt, e)
            _health.set(DEGRADED)
            _update_ingest_stats(status="degraded", ws_state=DEGRADED)
            sleep_s = _backoff(attempt)
            logger.info("[INGEST] backoff %gs before reconnect", sleep_s)
            time.sleep(sleep_s)
            attempt += 1
            if attempt >= 3:
                _health.set(REST_ONLY)
                _update_ingest_stats(status="offline", ws_state=REST_ONLY)


# ---------------------------------------------------------------------------
# _ingest_health_snapshot (P-6)
# ---------------------------------------------------------------------------

def _ingest_health_snapshot() -> dict:
    """Return the ingest_status dict for /api/state (P-6).

    Fields: status, last_bar_age_s, open_gaps_today, bars_today, ws_state.
    """
    ws_state = _health.get()
    age = _health.last_bar_age_s()

    # Derive shadow_data_status from connection state and last bar age
    if ws_state == LIVE:
        if age is None or age < 90:
            shadow_status = "live"
        else:
            shadow_status = "degraded"
    elif ws_state in (DEGRADED, RECONNECTING):
        shadow_status = "degraded"
    elif ws_state == REST_ONLY:
        shadow_status = "offline"
    else:
        shadow_status = "unconfigured"

    with _ingest_stats_lock:
        snap = dict(_ingest_stats)

    snap["ws_state"] = ws_state
    snap["status"] = shadow_status
    snap["last_bar_age_s"] = round(age, 1) if age is not None else None

    # Count bars written today
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        today = _dt.now(_ZI("America/New_York")).strftime("%Y-%m-%d")
        snap["bars_today"] = _count_bars_today(today)
    except Exception:
        snap["bars_today"] = 0

    # v6.6.0 Pillar A: embed ingest_health from SLA collector
    try:
        from ingest.sla import get_health_snapshot as _sla_snap
        snap["ingest_health"] = _sla_snap()
    except Exception:
        snap["ingest_health"] = {"global": {"color": "green"}, "gate_mode": "dry_run"}

    # v6.6.0 Pillar B: embed gap_audit summary
    try:
        from ingest.audit import AuditLog as _AL
        snap["gap_audit"] = _AL.daily_summary()
    except Exception:
        snap["gap_audit"] = {}

    # v6.6.0 Pillar C: embed gate override state
    import os as _os
    snap["gate_override_active"] = (
        _os.environ.get("SSM_INGEST_GATE_DISABLED") == "1"
        or _os.environ.get("SSM_INGEST_GATE_MODE", "dry_run") == "off"
    )

    return snap
