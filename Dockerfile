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
COPY bot_version.py .
COPY telegram_commands.py .
# v10.0.1 -- send_telegram + report_error + _format_error_telegram carved
# out of trade_genius.py. trade_genius re-exports the public surface but
# the import resolves against this file at runtime; missing it makes
# every `from telegram_io import ...` fail and crashloops the container.
COPY telegram_io.py .
COPY paper_state.py .
COPY side.py .
COPY error_state.py .
# v10.0.1 -- tiger_buffalo_v5.py, eye_of_tiger.py, v5_10_1_integration.py,
# v5_10_6_snapshot.py, v5_13_2_snapshot.py, market_brief.py, volume_bucket.py,
# volume_warmup.py COPY lines deleted along with the legacy modules themselves.
# Constants + sizing helpers migrated to engine/legacy_constants.py.
# v5.9.0 — QQQ Regime Shield (5m EMA3/EMA9 cross) module.
COPY qqq_regime.py .
# v6.11.0 — SPY Regime Classifier (C25 short amplification gate).
COPY spy_regime.py .
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
# v7.14.0 — v10 ORB strategy package (shadow mode in scan.py at this
# point; live trading wiring lands in v7.15.0). Missing this COPY
# would cause ModuleNotFoundError at trade_genius import time --
# the same class of regression that crash-looped v5.10.1.
COPY orb/ ./orb/
# v7.13.0 — VIX daily history + earnings calendar consumed by the
# orb runtime at session start.
COPY data/external/ ./data/external/
COPY tools/orb_earnings_calendar.py ./tools/orb_earnings_calendar.py
COPY tools/orb_vix_loader.py ./tools/orb_vix_loader.py
# v7.102.0 — emit_signal_bus_init_complete helper. Imported by
# executors/bootstrap.py at startup to log [SIGNAL-BUS-INIT-COMPLETE].
# Missing this COPY would crash trade_genius at boot with
# ModuleNotFoundError: tools.signal_bus_audit.
COPY tools/signal_bus_audit.py ./tools/signal_bus_audit.py
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
# v10.0.1 -- earnings_watcher/ package deleted (Tiger Sentinel chain retired).

# v6.6.0 — ingest_config.py (root-level tunable constants used by
# engine/ingest_gate.py and ingest/sla.py). Missing this COPY causes
# ModuleNotFoundError: ingest_config when /test checks the ingest gate.
COPY ingest_config.py .
# v10.0.1 -- scripts/premarket_check.py deleted; the scripts/ COPY is
# only kept to ship the in-container preflight + monitor + run helpers.
COPY scripts/ ./scripts/

# Dashboard module + static UI (env-gated; bot runs without DASHBOARD_PASSWORD set)
COPY dashboard_server.py .
COPY dashboard_static/ ./dashboard_static/

# Persistence directory (mount a volume here)
RUN mkdir -p /data

ENV PAPER_STATE_PATH=/data/paper_state.json
ENV PAPER_LOG_PATH=/data/investment.log
ENV TICKERS_FILE=/data/tickers.json
ENV STATE_DB_PATH=/data/state.db
# v10.0.1 -- pin trade log under /data so it survives Railway redeploys.
# Without this, orb/trade_log.py falls back to CWD ("trade_log.jsonl")
# and writes to the container root, wiping the closed-trade history on
# every redeploy. Railway dashboard env can still override.
ENV TRADE_LOG_PATH=/data/trade_log.jsonl

CMD ["python", "trade_genius.py"]
