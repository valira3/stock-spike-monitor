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

# Dashboard module + static UI (env-gated; bot runs without DASHBOARD_PASSWORD set)
COPY dashboard_server.py .
COPY dashboard_static/ ./dashboard_static/

# Persistence directory (mount a volume here)
RUN mkdir -p /data

ENV PAPER_STATE_PATH=/data/paper_state.json
ENV PAPER_LOG_PATH=/data/investment.log
ENV TICKERS_FILE=/data/tickers.json

CMD ["python", "trade_genius.py"]
