# ─────────────────────────────────────────────────────────────────────────────
# TradeGenius — Docker deployment
# Use this if you're deploying to Docker, Fly.io, Render, or any container host
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

WORKDIR /app

# System deps for matplotlib / lxml
RUN apt-get update && apt-get install -y \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trade_genius.py .
COPY telegram_commands.py .
COPY paper_state.py .
COPY side.py .
COPY error_state.py .
# v5.0.0 — Tiger/Buffalo state-machine module (imported by trade_genius.py).
COPY tiger_buffalo_v5.py .
# v5.9.0 — QQQ Regime Shield (5m EMA3/EMA9 cross) module.
COPY qqq_regime.py .
# v5.10.0 — Eye-of-the-Tiger pure-function evaluators + volume baseline.
# v5.10.1 — Live-hot-path integration glue (orchestrator).
# Missing these COPY lines is what crash-looped the v5.10.1 Railway
# deploy (ModuleNotFoundError on `import eye_of_tiger`); v5.10.3 wires
# them in. Verified by tests/test_startup_smoke.py and the
# scripts/preflight.sh dockerfile-mirror check.
COPY eye_of_tiger.py .
COPY volume_bucket.py .
COPY v5_10_1_integration.py .
# v5.10.6 \u2014 dashboard /api/state v5.10 panel helper. Missing this COPY
# would crash dashboard_server's snapshot() with ModuleNotFoundError.
COPY v5_10_6_snapshot.py .
COPY v5_13_2_snapshot.py .
# v5.13.6 \u2014 per-position lifecycle event log. Missing this COPY would
# crash trade_genius at boot since broker.orders / broker.positions
# import the module for entry/sentinel/exit hooks.
COPY lifecycle_logger.py .
# v5.1.0 — Forensic Volume Filter module (imported by trade_genius.py).
COPY volume_profile.py .
# v5.1.2 — Forensic capture: indicator math + 1m bar JSONL archive
# (imported by trade_genius.py).
COPY indicators.py .
COPY bar_archive.py .
# v5.31.0 -- Forensic capture writers (decisions/exits/macro/daily) used by
# trade_genius._qqq_weather_tick (macro), broker/orders (entry+exit), engine/
# scan (indicator snapshots), broker/lifecycle (daily OHLC). Top-level COPY
# required so the Railway container can resolve the lazy `from forensic_capture
# import ...` imports inside those call sites.
COPY forensic_capture.py .
# v5.1.8 — SQLite-backed persistence for fired_set + v5_long_tracks.
COPY persistence.py .
# v5.14.0 — shadow_pnl.py removed (shadow strategy retired). Backtest CLI
# kept for replay tooling but reads from trade_log.jsonl + executor_positions.
COPY backtest/ ./backtest/
# v5.11.0 — engine/ package extraction (PR1: bars). Must COPY the whole
# package; missing this would crash trade_genius at boot with
# ModuleNotFoundError: engine. Subsequent v5.11.x PRs append more
# modules under engine/ (seeders, phase_machine, scan, callbacks).
COPY engine/ ./engine/
# v5.11.1 — telegram_ui/ package extraction (PR1: charts). Same rule:
# missing this COPY would crash trade_genius at boot with
# ModuleNotFoundError: telegram_ui. Subsequent v5.11.1 PRs append
# sync, menu, and runtime modules.
COPY telegram_ui/ ./telegram_ui/
# v5.11.2 — broker/ package extraction (PR1: stops). Same rule:
# missing this COPY would crash trade_genius at boot with
# ModuleNotFoundError: broker. Subsequent v5.11.2 PRs append
# orders, positions, and lifecycle modules.
COPY broker/ ./broker/
# v5.12.0 — executors/ package extraction (PR1: base). Same rule:
# missing this COPY would crash trade_genius at boot with
# ModuleNotFoundError: executors. Subsequent v5.12.0 PRs append
# val, gene, and bootstrap modules.
COPY executors/ ./executors/
# v6.5.0 — ingest/ package (always-on Algo Plus ingest module). Same
# rule: missing this COPY would crash trade_genius at boot with
# ModuleNotFoundError: ingest (line 51 of trade_genius.py imports
# ingest.algo_plus as ingest_algo_plus). Holds AlgoPlusIngest,
# BarAssembler, ConnectionHealth, GapDetector, RestBackfillWorker.
COPY ingest/ ./ingest/

# Dashboard module + static UI (env-gated; bot runs without DASHBOARD_PASSWORD set)
COPY dashboard_server.py .
COPY dashboard_static/ ./dashboard_static/

# Persistence directory (mount a volume here)
RUN mkdir -p /data

ENV PAPER_STATE_PATH=/data/paper_state.json
ENV PAPER_LOG_PATH=/data/investment.log
ENV TICKERS_FILE=/data/tickers.json
ENV STATE_DB_PATH=/data/state.db

CMD ["python", "trade_genius.py"]
