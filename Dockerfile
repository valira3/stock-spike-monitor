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
# v5.1.0 — Forensic Volume Filter module (imported by trade_genius.py).
COPY volume_profile.py .
# v5.1.2 — Forensic capture: indicator math + 1m bar JSONL archive
# (imported by trade_genius.py).
COPY indicators.py .
COPY bar_archive.py .
# v5.1.8 — SQLite-backed persistence for fired_set + v5_long_tracks.
COPY persistence.py .
# v5.2.0 — shadow strategy P&L tracker (imported by trade_genius.py).
COPY shadow_pnl.py .

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
