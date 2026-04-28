"""
TradeGenius v3.5.1 — Eye of the Tiger 2.0 (paper book)
===========================================================================
ORB Momentum Breakout + Wounded Buffalo Short on a user-defined ticker
universe. Paper book only; live execution arrives in v4.0.0 via the
Alpaca-backed TradeGenius executors (Val + Gene).
Infrastructure: Telegram bot, paper trading, dashboard, scheduler.
"""

import os
from pathlib import Path
import json
import re
import time
import logging
import threading
import urllib.request
import asyncio
import signal
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from telegram import (
    BotCommand, BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats, BotCommandScopeDefault, Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest as TelegramBadRequest
# v4.8.0 \u2014 Side enum + SideConfig table for the long/short collapse.
# side.py is a pure module (no imports from trade_genius), so a plain
# top-level import is safe and avoids the __main__ aliasing dance that
# paper_state.py / telegram_commands.py need.
from side import Side, CONFIGS  # noqa: E402

# v5.0.0 \u2014 Tiger/Buffalo two-stage state machine. Pure module, safe to
# import top-level. Canonical spec lives in STRATEGY.md at the repo
# root; this module's helpers cite rule IDs (e.g. L-P2-R3) that map
# 1:1 to that spec. The runtime integration is gating-only: v5 sits in
# front of the v4 entry/close paths and decides when to fire each
# stage. Unit-sizing math is preserved unchanged from v4 (50/50 staging
# means "50% of the v4 unit, then add the other 50%").
import tiger_buffalo_v5 as v5  # noqa: E402
# v5.1.0 \u2014 forensic volume filter (shadow mode). Top-level module so the
# v5.0.2 infra-guard test catches a missing Dockerfile COPY for it.
import volume_profile  # noqa: E402
# v5.1.2 \u2014 forensic capture: bar archive + indicators.
import indicators  # noqa: E402
import bar_archive  # noqa: E402
import persistence  # noqa: E402
import shadow_pnl  # noqa: E402

from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler,
    CommandHandler, ContextTypes, TypeHandler,
)

# ============================================================
# CONFIG FROM ENVIRONMENT VARIABLES
# ============================================================
TELEGRAM_TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID                 = os.getenv("CHAT_ID")
# v3.4.41 — treat empty string as unset so Railway vars left blank still
# fall back to the hardcoded owner ID.
_RH_OWNER_DEFAULT       = "5165570192"

# v3.6.0 — Telegram owner whitelist. Every Telegram update is checked
# against this set by a group=-1 TypeHandler before any other handler
# fires; non-owners are silently dropped (no reply, server-side log only).
# Comma-separated Telegram user ids (positive integers), NOT chat ids.
# Default includes Val so DM resets always work from the default deploy.
# v3.6.0 renamed from RH_OWNER_USER_IDS; the old env var is no longer read.
_TRADEGENIUS_OWNERS_RAW = os.getenv("TRADEGENIUS_OWNER_IDS", "").strip() or _RH_OWNER_DEFAULT
TRADEGENIUS_OWNER_IDS   = {
    u.strip() for u in _TRADEGENIUS_OWNERS_RAW.split(",") if u.strip()
}

BOT_NAME    = "TradeGenius"
BOT_VERSION = "5.7.1"

# v3.4.21: release notes are split into two surfaces.
#
#  CURRENT_MAIN_NOTE
#    - Just the release that is actively being deployed.
#    - Used by the startup "deployed" card so each deploy shows only
#      what shipped this time (no accumulating carry-over list).
#    - MUST begin with the current BOT_VERSION string and MUST NOT
#      mention any prior version (enforced by smoke test).
#
#  MAIN_RELEASE_NOTE
#    - Rolling history: CURRENT note + previous few versions.
#    - Used by /version (typed and menu) so the history is still
#      available on demand.
#    - The Telegram 34-char mobile-width rule still applies to every
#      line of both surfaces.
CURRENT_MAIN_NOTE = (
    "v5.7.1 \u2014 Bison & Buffalo.\n"
    "Titan exit FSM: hard stop on 2\n"
    "consec 1m closes outside OR,\n"
    "BE move on 2nd green/red 5m,\n"
    "5m 9-EMA trail seeded 10:15.\n"
    "Velocity Fuse: >1.0% adverse\n"
    "intra-candle move triggers an\n"
    "immediate market exit. DI<25\n"
    "exits dropped for Titans only;\n"
    "non-Titans keep legacy logic.\n"
    "New logs: [V571-EXIT_PHASE],\n"
    "[V571-VELOCITY_FUSE],\n"
    "[V571-EMA_SEED]. exit_reason\n"
    "gains hard_stop_2c, be_stop,\n"
    "ema_trail, velocity_fuse."
)

# Main-bot release note: short tail of recent releases.
_MAIN_HISTORY_TAIL = (
    "v5.5.11 \u2014 AS OF hotfix.\n"
    "Shadow tab AS OF cell stayed\n"
    "blank on v5.5.10 because the\n"
    "_scFmtTs formatter lives in a\n"
    "sibling IIFE and was not\n"
    "reachable from the summary\n"
    "band; the ReferenceError got\n"
    "swallowed by a try/catch.\n"
    "Inlined a self-contained ET\n"
    "formatter inside\n"
    "_shadowSummaryBand so the\n"
    "cell now renders a real\n"
    "MM/DD HH:MM ET stamp.\n"
    "No trading-decision change.\n"
    "\n"
    "v5.5.10 \u2014 persist positions.\n"
    "Executor self.positions now\n"
    "rehydrates from state.db at\n"
    "boot, so a Val/Gene restart\n"
    "during a live session is\n"
    "silent (no false orphan\n"
    "Telegram). Reconcile is now\n"
    "a true safety net: clean\n"
    "match \u2192 no alert, broker\n"
    "orphan \u2192 graft + alert,\n"
    "stale local \u2192 quiet heal.\n"
    "Shadow tab AS OF now reads\n"
    "server_time (was blank).\n"
    "No trading-decision change.\n"
    "\n"
    "v5.5.9 \u2014 shadow charts polish.\n"
    "Shadow tab now shows a per-\n"
    "ticker unrealized bar chart\n"
    "for configs that have open\n"
    "positions but no closed\n"
    "trades yet. Empty configs are\n"
    "hidden from the chart groups.\n"
    "Top summary band shows total\n"
    "open + unrealized. Strategies\n"
    "rows tint by today's P&L and\n"
    "the header is sticky.\n"
    "No trading-decision change.\n"
    "\n"
    "v5.5.8 \u2014 SHORT entry row.\n"
    "Main tab Today's Trades now\n"
    "shows the SHORT entry row for\n"
    "every closed short (was 1 row\n"
    "instead of 2). Synthesized\n"
    "from the cover record; open\n"
    "shorts also surface their\n"
    "entry leg. Storage unchanged.\n"
    "No trading-decision change.\n"
    "\n"
    "v5.5.7 \u2014 Main tab fix.\n"
    "Today's trades summary now\n"
    "counts SHORT/COVER as opens\n"
    "and closes (was 0/0 before).\n"
    "COVER row tail shows P&L too.\n"
    "Main tab gains a LAST SIGNAL\n"
    "card scoped to the paper book.\n"
    "No trading-decision change.\n"
    "\n"
    "v5.5.6 \u2014 shadow race fix.\n"
    "Shadow gate now reads the\n"
    "just-closed minute bucket,\n"
    "not the still-forming one.\n"
    "No trading-decision change.\n"
    "\n"
    "v5.5.5 \u2014 WS observability.\n"
    "Shadow WS now counts every\n"
    "bar, logs first 5 + every\n"
    "100th, and exposes stats on\n"
    "/api/ws_state. Watchdog forces\n"
    "reconnect after 120s of RTH\n"
    "silence. Bar archive iex_vol\n"
    "now prefers WS over Yahoo and\n"
    "fills et_bucket. No trading-\n"
    "decision change.\n"
    "\n"
    "v5.5.4 \u2014 WS handler hotfix.\n"
    "Shadow WS bar handler is now\n"
    "an async def coroutine.\n"
    "alpaca-py StockDataStream\n"
    "rejected the v5.5.3 sync\n"
    "handler and crash-looped\n"
    "every ~6s.\n"
    "\n"
    "v5.5.3 \u2014 shadow cred fix.\n"
    "_start_volume_profile now\n"
    "reads VAL_ALPACA_PAPER_KEY\n"
    "first, then legacy keys.\n"
    "On miss, [SHADOW DISABLED]\n"
    "logs and dashboard shows\n"
    "a banner instead of silent\n"
    "empty volumes.\n"
    "\n"
    "v5.5.2 \u2014 bar archive wired.\n"
    "_v512_archive_minute_bar is\n"
    "now called from the scan\n"
    "loop so /data/bars/YYYY-MM-DD\n"
    "actually fills. 90d retention\n"
    "runs at EOD. Smoke guard pins\n"
    "the call site against future\n"
    "refactor regressions.\n"
    "\n"
    "v5.5.1 \u2014 chart interactivity.\n"
    "Shadow tab charts now show\n"
    "rich tooltips on hover and\n"
    "click-to-isolate a config\n"
    "across all three groups.\n"
    "Click again or X clears it.\n"
    "No trading-logic change.\n"
    "\n"
    "v5.4.2 \u2014 doc refresh.\n"
    "ARCHITECTURE.md and\n"
    "trade_genius_algo.pdf\n"
    "regenerated to v5.4.1 state.\n"
    "Adds \u00a720 backtest CLI and\n"
    "\u00a721 Shadow tab charts.\n"
    "No code-logic change.\n"
    "\n"
    "v5.4.1 \u2014 shadow charts.\n"
    "Shadow tab now shows equity\n"
    "curves, day P&L heatmap, and\n"
    "rolling 20-trade win-rate\n"
    "sparklines per config. New\n"
    "/api/shadow_charts endpoint,\n"
    "30s server cache, 60s tab\n"
    "polling. 3 new smoke tests.\n"
    "\n"
    "v5.4.0 \u2014 offline backtest\n"
    "CLI. backtest/ package +\n"
    "python -m backtest.replay\n"
    "with replay-vs-prod\n"
    "validation. See ARCHITECTURE\n"
    "\u00a720 for usage. No live\n"
    "trading-logic change.\n"
    "\n"
    "v5.2.0 \u2014 real-time MTM P&L\n"
    "tracker for all 7 SHADOW_\n"
    "CONFIGS on the main dashboard.\n"
    "Each config owns a virtual\n"
    "portfolio sized off paper-book\n"
    "equity. Open positions are\n"
    "marked to market every scan\n"
    "cycle; exits mirror the paper\n"
    "bot's HARD_EJECT/trail/EOD\n"
    "path. New panel on Main tab.\n"
    "\n"
    "v5.1.9 \u2014 REHUNT_VOL_CONFIRM\n"
    "+ OOMPH_ALERT shadow configs.\n"
    "Both pure observation. No\n"
    "trading-decision change.\n"
    "\n"
    "v5.1.8 \u2014 SQLite persistence\n"
    "for fired_set (timed-job\n"
    "idempotency) and v5_long_\n"
    "tracks (Tiger/Buffalo state).\n"
    "New persistence.py wraps a\n"
    "WAL-mode SQLite store at\n"
    "STATE_DB_PATH (default\n"
    "/data/state.db on Railway).\n"
    "Solves EOD double-fire risk\n"
    "on container restarts and\n"
    "the non-atomic json.dump\n"
    "corruption on the v5 tracks.\n"
    "One-shot migration imports\n"
    "tracks from paper_state.json\n"
    "then renames to .migrated.bak.\n"
    "No trading-decision change.\n"
    "\n"
    "v5.1.6 \u2014 BUCKET_FILL_100\n"
    "5th shadow config\n"
    "+ [V510-VEL]/[IDX]/[DI]\n"
    "intraminute capture logs.\n"
    "No trading-decision change.\n"
    "\n"
    "v5.1.5 \u2014 fix: /test no\n"
    "longer times out with\n"
    "\"Command failed: Timed\n"
    "out\". Per-step edit_text\n"
    "calls inside the cmd_test\n"
    "loop triggered Telegram\n"
    "per-chat edit rate-limit;\n"
    "final edit raced httpx\n"
    "read-timeout. Now one\n"
    "edit at end + reply_text\n"
    "fallback. Underlying\n"
    "_test_* steps were always\n"
    "healthy.\n"
    "\n"
    "v5.1.4 \u2014 equity-aware\n"
    "sizing for live executors.\n"
    "Each entry now capped at\n"
    "min(DOLLARS_PER_ENTRY,\n"
    "equity * MAX_PCT_PER_ENTRY,\n"
    "cash - MIN_RESERVE_CASH).\n"
    "Defaults: 10% of equity,\n"
    "$500 reserve. Falls back\n"
    "to legacy fixed sizing on\n"
    "get_account() error. Paper\n"
    "book unchanged.\n"
    "\n"
    "v5.1.3 \u2014 chore: removed\n"
    "unused Finnhub SPY-quote\n"
    "fallback from /health\n"
    "diagnostic. FMP already\n"
    "returns SPY in the same\n"
    "diagnostic. No trading-path\n"
    "impact. FINNHUB_TOKEN env\n"
    "var is no longer read.\n"
    "\n"
    "v5.1.2 \u2014 forensic capture\n"
    "+ GEMINI_A shadow config.\n"
    "Tier-1: 1m bar JSONL archive\n"
    "to /data/bars/, every-minute\n"
    "[V510-MINUTE] log,\n"
    "[V510-CAND] log on every\n"
    "entry consideration (fired or\n"
    "not), entry log line carries\n"
    "bid/ask + account state.\n"
    "Tier-2: [V510-FSM] state-\n"
    "transition log, indicator\n"
    "snapshot on [V510-CAND]\n"
    "(rsi14/ema9/ema21/atr14/\n"
    "vwap_dist_pct/spread_bps).\n"
    "GEMINI_A 110/85 added as a\n"
    "4th hard-coded SHADOW_CONFIG\n"
    "(only config with positive\n"
    "net swing in Apr 20-24 BT).\n"
    "Defaults preserve v5.1.1\n"
    "behavior; VOL_GATE_ENFORCE=0\n"
    "stays. Still SHADOW MODE.\n"
    "\n"
    "v5.1.1 \u2014 SHADOW A/B: env-\n"
    "driven toggles + 3 parallel\n"
    "shadow verdicts per candidate\n"
    "(TICKER+QQQ 70/100, TICKER_\n"
    "ONLY 70, QQQ_ONLY 100). Still\n"
    "SHADOW MODE.\n"
    "\n"
    "v5.1.0 \u2014 SHADOW: Anaplan\n"
    "forensic volume gate. 55-day\n"
    "per-minute volume baseline\n"
    "from SIP, normalized to IEX\n"
    "scale. Stage 1 wanted ticker\n"
    "\u2265120% AND QQQ \u2265100%; Stage 2\n"
    "maintenance \u2265100%. Logs only.\n"
    "Free IEX cap 30 symbols.\n"
    "v5.1.1 makes those toggles\n"
    "env-driven without changing\n"
    "the default behavior.\n"
    "\n"
    "v5.0.4 \u2014 revert: alpaca-key\n"
    "fallback from v5.0.3 was wrong.\n"
    "Paper keys and live keys are\n"
    "INDEPENDENT credentials and must\n"
    "not be silently substituted.\n"
    "GENE_ALPACA_KEY on Railway is\n"
    "actually a live key; the v5.0.3\n"
    "fallback would have routed paper\n"
    "trades through live. Reverted.\n"
    "Chat-map auto-learn from v5.0.3\n"
    "is unaffected and stays.\n"
    "\n"
    "v5.0.3 \u2014 hotfix for prod:\n"
    "Val saw zero trade DMs on\n"
    "Fri despite 15 BUYs / 10\n"
    "SELLs firing. Executor\n"
    "notifier was silently a\n"
    "no-op because TELEGRAM_\n"
    "CHAT_ID env var was never\n"
    "set on Railway. Now each\n"
    "owner just DMs their\n"
    "executor bot once (any\n"
    "message); chat_id is\n"
    "auto-learned and persisted\n"
    "to /data/executor_chats_*\n"
    ".json. Trade confirms fan\n"
    "out to every learned\n"
    "owner. (v5.0.3 also shipped\n"
    "an alpaca-key fallback that\n"
    "was reverted in v5.0.4 \u2014\n"
    "see current note.)\n"
    "\n"
    "v5.0.2 \u2014 hotfix for prod:\n"
    "Dockerfile was missing\n"
    "COPY tiger_buffalo_v5.py,\n"
    "so v5.0.0/v5.0.1 prod\n"
    "crashed on import. Fixed +\n"
    "added a smoke test that\n"
    "asserts every imported\n"
    "top-level module appears\n"
    "in the Dockerfile (the\n"
    "v4.11.0 footgun, again).\n"
    "\n"
    "Bundles v5.0.1: DMI period\n"
    "fixed 14 \u2192 15 to match\n"
    "Genes spec and v4 code.\n"
    "\n"
    "v5.0.0 \u2014 Tiger/Buffalo:\n"
    "two-stage state machine\n"
    "replaces v4 ORB Breakout\n"
    "(long) and Wounded Buffalo\n"
    "(short). Per-ticker FSM:\n"
    "IDLE \u2192 ARMED (4 gates) \u2192\n"
    "STAGE_1 (50% on, DI 25\n"
    "double-tap) \u2192 STAGE_2\n"
    "(full size, DI 30 +\n"
    "profit, stop \u2192 entry) \u2192\n"
    "TRAILING (5m HL/LH\n"
    "ratchet) \u2192 EXITED \u2192\n"
    "RE_HUNT once \u2192\n"
    "LOCKED_FOR_DAY. Short\n"
    "side: DI<25 hard eject is\n"
    "priority-1 over stop.\n"
    "Spec: STRATEGY.md.\n"
    "\n"
    "v4.13.0 \u2014 major indices:\n"
    "ticker now also shows real\n"
    "S&P 500/Nasdaq/Dow/Russell\n"
    "2K/VIX cash indices via\n"
    "Yahoo, plus an inline futures\n"
    "badge ([ES +0.40%]) on each\n"
    "so on weekends and overnight\n"
    "you see what futures are\n"
    "pricing for the open. ETF\n"
    "rows (SPY/QQQ/DIA/IWM/VIX)\n"
    "stay on top; if Yahoo fails\n"
    "we paint a 'data delayed'\n"
    "note and keep the ETF feed.\n"
    "\n"
    "v4.12.0 \u2014 ticker upgrade:\n"
    "SPY/QQQ/DIA/IWM/VIX strip\n"
    "now auto-marquees when its\n"
    "items overflow the screen\n"
    "(slow ~30s loop, pauses on\n"
    "hover or tap). Outside RTH\n"
    "each row gets an AH/PRE\n"
    "badge with the move vs the\n"
    "relevant close. Respects\n"
    "prefers-reduced-motion.\n"
    "\n"
    "v4.11.5 \u2014 two cleanups:\n"
    "(1) LIVE pill always shows\n"
    "the recycle countdown\n"
    "(\u267B NNs). When the backend\n"
    "has no schedule (weekend\n"
    "or scanner idle) we paint\n"
    "\u267B -- instead of falling\n"
    "back to the old counting-\n"
    "up tick NNs label.\n"
    "(2) Synthetic harness\n"
    "replay now strips\n"
    "trade_genius_version from\n"
    "both observed and golden\n"
    "before compare.\n"
    "\n"
    "v4.11.4 \u2014 hotfix x2:\n"
    "(1) Repoint the post-\n"
    "deploy smoke workflow\n"
    "DASHBOARD_URL from the\n"
    "old SSM domain (404 since\n"
    "the TradeGenius rename) to\n"
    "tradegenius.up.railway.\n"
    "app. CI smoke green for\n"
    "the first time since\n"
    "v4.9.3.\n"
    "(2) Trim brand-row pad\n"
    "10px to 6px in the 400px\n"
    "band so the clock T no\n"
    "longer hairline-clips at\n"
    "390 viewports.\n"
    "\n"
    "v4.11.3 \u2014 hotfix:\n"
    "v4.11.2 fixed 430px (Pro\n"
    "Max) but 390px (iPhone\n"
    "13/14/15) still clipped\n"
    "the clock. Add a new\n"
    "<=400px media band:\n"
    "clock 10px, gap 4px,\n"
    "version slug 9.5px.\n"
    "\n"
    "v4.11.2 \u2014 hotfix:\n"
    "v4.11.0 added the health\n"
    "pill to the brand row, but\n"
    "on iPhone Pro Max class\n"
    "viewports (390/430 px) the\n"
    "row overflowed: clock got\n"
    "clipped and LIVE pill\n"
    "tick wrapped. Shrink\n"
    "clock 13->11px under\n"
    "existing 500 px media.\n"
    "\n"
    "v4.11.1 \u2014 hotfix:\n"
    "v4.11.0 added a new module\n"
    "error_state.py but the\n"
    "Dockerfile COPY whitelist\n"
    "wasn't updated, so the\n"
    "container crashed on boot\n"
    "with ModuleNotFoundError.\n"
    "Prod 502 for ~3 hours.\n"
    "One-line Dockerfile fix.\n"
    "\n"
    "v4.11.0 \u2014 feature:\n"
    "health pill replaces the\n"
    "noisy log tail card. Brand\n"
    "row gains a small dot+count\n"
    "pill next to LIVE: green at\n"
    "0 errors today, amber on\n"
    "warnings only, red on any\n"
    "ERROR/CRITICAL. Tap to\n"
    "expand last 10 events.\n"
    "\n"
    "v4.10.2 \u2014 hotfix:\n"
    "Val/Gene tab Fetch-failed\n"
    "banner (cross-IIFE bridge\n"
    "for applyGateTriState) +\n"
    "mobile clock nowrap lifted\n"
    "to 500 px so iPhone Pro\n"
    "Max class fits one row.\n"
    "\n"
    "v4.10.1 \u2014 hotfix:\n"
    "finish the two v4.10.0\n"
    "fixes that shipped half-\n"
    "complete (collapsed empty\n"
    "Open Positions card +\n"
    "mobile void v2).\n"
    "\n"
    "v4.10.0 \u2014 ui polish:\n"
    "5 dashboard fixes (mobile\n"
    "compact ticker, mobile\n"
    "void v1, collapsed empty\n"
    "Positions card v1, log\n"
    "wrap, GATE tri-state).\n"
    "\n"
    "v4.9.3 \u2014 cleanup:\n"
    "delete 6 dead SideConfig\n"
    "fields + 2 dead methods\n"
    "from side.py. No behavior\n"
    "change; 50/50 harness\n"
    "replays byte-equal and\n"
    "the v4.9.2 validator\n"
    "still passes at import.\n"
    "\n"
    "v4.9.2 \u2014 hardening:\n"
    "fail-fast import guard for\n"
    "SideConfig *_attr fields.\n"
    "trade_genius.py now asserts\n"
    "every globals() name used\n"
    "by the unified breakout\n"
    "bodies exists at import,\n"
    "so a renamed module-level\n"
    "dict raises AssertionError\n"
    "at load instead of a\n"
    "KeyError mid-session. No\n"
    "bot behavior change.\n"
    "\n"
    "v4.9.1 \u2014 ci/dashboard fix:\n"
    "new unauthenticated\n"
    "/api/version endpoint so\n"
    "the post-deploy GHA poller\n"
    "can confirm Railway has\n"
    "rolled out the new\n"
    "BOT_VERSION without\n"
    "carrying a session cookie.\n"
    "Workflow now polls\n"
    "/api/version. Adds unit\n"
    "tests for the login\n"
    "rate-limiter.\n"
    "\n"
    "v4.9.0 \u2014 refactor:\n"
    "Stage B2 real collapse.\n"
    "check_breakout,\n"
    "execute_breakout, and\n"
    "close_breakout now have\n"
    "ONE unified body each,\n"
    "parameterized by\n"
    "SideConfig. The 6 legacy\n"
    "long/short twin bodies\n"
    "and SSM_USE_COLLAPSED\n"
    "feature flag are deleted.\n"
    "trade_genius.py shrinks\n"
    "by ~700 LOC. 50/50\n"
    "synthetic goldens replay\n"
    "byte-equal.\n"
    "\n"
    "v4.8.2 \u2014 testing:\n"
    "25 edge-case scenarios\n"
    "added to synthetic harness.\n"
    "Corpus now 50 scenarios;\n"
    "smoke lifts to 132 tests.\n"
    "\n"
    "v4.8.1 \u2014 testing:\n"
    "synthetic harness added\n"
    "with 25 named scenarios\n"
    "and 25 goldens. CLI\n"
    "python -m synthetic_harness\n"
    "{list,record,replay,diff}.\n"
    "smoke_test.py --synthetic\n"
    "lifts coverage from 82\n"
    "to 107 tests. Pure\n"
    "addition \u2014 zero behavior\n"
    "change to trade_genius.\n"
    "\n"
    "v4.8.0 \u2014 refactor:\n"
    "long/short collapsed via\n"
    "Side enum. check_breakout,\n"
    "execute_breakout, and\n"
    "close_breakout replace 6\n"
    "near-mirror functions.\n"
    "Zero user-visible change.\n"
    "\n"
    "v4.7.0 \u2014 refactor +\n"
    "risk fixes: long/short\n"
    "entry/execute/close are now\n"
    "structural mirrors. Bug fix:\n"
    "shorts now respect daily\n"
    "loss limit. Bug fix:\n"
    "daily_short_entry_count now\n"
    "resets on new day. Bug fix:\n"
    "scan_loop now calls\n"
    "execute_short_entry after\n"
    "check_short_entry returns\n"
    "True (symmetric control\n"
    "flow). New helpers:\n"
    "_check_daily_loss_limit and\n"
    "_ticker_today_realized_pnl.\n"
    "\n"
    "v4.6.0 \u2014 refactor:\n"
    "extracted paper-state I/O\n"
    "(save/load/reset + lock +\n"
    "_state_loaded) into a new\n"
    "paper_state.py module.\n"
    "Pure code motion \u2014 zero\n"
    "behavior change.\n"
    "\n"
    "v4.5.4 \u2014 deploy fix:\n"
    "telegram_commands.py now\n"
    "aliases __main__ in\n"
    "sys.modules so prod's\n"
    "`python trade_genius.py`\n"
    "entrypoint doesn't trigger\n"
    "a circular re-execution.\n"
    "\n"
    "v4.5.3 \u2014 deploy fix:\n"
    "Dockerfile now copies\n"
    "telegram_commands.py into\n"
    "the container image.\n"
    "\n"
    "v4.5.2 \u2014 refactor:\n"
    "extracted main-bot Telegram\n"
    "command handlers into\n"
    "telegram_commands.py.\n"
    "\n"
    "v4.5.1 \u2014 refactor:\n"
    "dashboard index.html split\n"
    "into index.html + app.css +\n"
    "app.js for cleaner separation\n"
    "of concerns. Pure code\n"
    "motion \u2014 zero visual change.\n"
    "\n"
    "v4.4.1 \u2014 regime fix:\n"
    "scan_loop now refreshes the\n"
    "MarketMode banner every\n"
    "cycle, not just intraday \u2014\n"
    "banner no longer sticks on\n"
    "POWER after 15:55 ET.\n"
    "gates.scan_paused reflects\n"
    "after-hours idle too.\n"
    "\n"
    "v4.4.0 \u2014 security:\n"
    "all bot commands + /reset\n"
    "callbacks require user_id in\n"
    "TRADEGENIUS_OWNER_IDS. The\n"
    "chat-based authorization\n"
    "fallback is removed. CHAT_ID\n"
    "kept for routing only.\n"
    "\n"
    "v4.3.4 \u2014 dashboard UI:\n"
    "row-2 refresh countdown\n"
    "zero-pads seconds; #h-tick\n"
    "pins tabular-nums so digit\n"
    "widths stay stable.\n"
    "\n"
    "v4.3.3 \u2014 dashboard API:\n"
    "/api/state gates.per_ticker\n"
    "now serializes extension_pct\n"
    "per ticker so the dashboard\n"
    "can surface how far past OR\n"
    "each break has traveled.\n"
    "\n"
    "v4.3.2 \u2014 dashboard UI:\n"
    "row-2 \"scan in Ns\" label\n"
    "replaced with \u267B recycle\n"
    "glyph \u2014 countdown reads\n"
    "\u267B Ns now. aria-label +\n"
    "title still say \"next scan\n"
    "in Ns\" for screen readers.\n"
    "\n"
    "v4.3.1 \u2014 dashboard UI:\n"
    "row-2 fits inline at 375px\n"
    "iPhone \u2014 nowrap + tighter\n"
    "gaps, 12-13px clock, LIVE\n"
    "pill padding trimmed. Clock\n"
    "drops seconds at \u2264360px.\n"
    "\n"
    "v4.3.0 \u2014 entry guards:\n"
    "reject entries extended past\n"
    "OR by more than 1.5%\n"
    "(ENTRY_EXTENSION_MAX_PCT) and\n"
    "reject entries that would\n"
    "need stop capping when\n"
    "ENTRY_STOP_CAP_REJECT=1.\n"
    "Fixes 2026-04-24 META chase.\n"
    "\n"
    "v4.2.2 \u2014 dashboard UI:\n"
    "row-2 clock right-aligned,\n"
    "white+bold HH:MM:SS+tz; rows\n"
    "retuned for 375px iPhone.\n"
    "\n"
    "v4.2.1 \u2014 dashboard UI:\n"
    "row-2 time clock restored\n"
    "(HH:MM ET) + Today's Trades\n"
    "collapsed to one line per\n"
    "fill with aligned cols.\n"
    "\n"
    "v4.2.0 \u2014 dashboard UI:\n"
    "redundant fourth header row\n"
    "deleted across Main/Val/Gene;\n"
    "\"\u00b7 live\" suffix + Sign Out\n"
    "button also dropped.\n"
    "\n"
    "v4.1.9 \u2014 dashboard M11:\n"
    "h_stream snapshot() now\n"
    "served from a 10s TTL\n"
    "cache shared across every\n"
    "SSE client.\n"
    "\n"
    "v4.1.8 \u2014 dashboard M7:\n"
    "Robinhood toggle removed;\n"
    "~70 lines of dead HTML/CSS/\n"
    "JS + localStorage + slice()\n"
    "indirection deleted.\n"
    "\n"
    "v4.1.7 \u2014 dashboard H7:\n"
    "_today_trades de-duplicates\n"
    "by (ticker,time,side,action)\n"
    "so a cross-list cover no\n"
    "longer double-counts. Smoke\n"
    "test guards the contract.\n"
    "\n"
    "v4.1.6 \u2014 dashboard H6:\n"
    "_fetch_indices tags VIX row\n"
    "with reason sentinel; real\n"
    "equities with transient 0\n"
    "quote no longer conflated\n"
    "with VIX no-feed case.\n"
    "\n"
    "v4.1.5 \u2014 audit cleanup:\n"
    "check_entry dead index_ok\n"
    "removed; /test edit_text\n"
    "narrows except to Telegram\n"
    "BadRequest and logs at\n"
    "DEBUG (6 sites).\n"
    "\n"
    "v4.1.4 \u2014 dashboard H2:\n"
    "Val/Gene tab landing no\n"
    "longer shows blank shared\n"
    "KPIs for up to 15s if Main\n"
    "hasn't polled yet. Tab\n"
    "switch warms __tgLastState\n"
    "via one-shot /api/state.\n"
    "\n"
    "v4.1.3 \u2014 trade_genius H3:\n"
    "cross-day cooldown prune now\n"
    "compares everything in ET;\n"
    "UTC exit times converted to\n"
    "ET before the 09:30 cutoff\n"
    "test; DST + midnight ET no\n"
    "longer drift the cutoff.\n"
    "\n"
    "v4.1.2 \u2014 trade_genius MED:\n"
    "load_paper_state clears dicts\n"
    "before merging state; mpl\n"
    "warmup logs at debug; dead\n"
    "try/except around last_signal\n"
    "assignment removed.\n"
    "\n"
    "v4.1.1 \u2014 trade_genius HIGH:\n"
    "signal bus register/emit under\n"
    "a lock; save_paper_state builds\n"
    "snapshot inside lock; entry\n"
    "paths reject current_price<=0;\n"
    "daily halt short-pnl uses\n"
    "_is_today, not date==.\n"
    "\n"
    "v4.1.0 \u2014 trade_genius audit\n"
    "CRITICAL: partial state-load\n"
    "no longer overwrites disk;\n"
    "every position carries a real\n"
    "UTC entry_ts_utc so trade log\n"
    "hold_seconds is populated.\n"
    "\n"
    "v4.0.9 \u2014 audit MEDIUM batch:\n"
    "dashboard hygiene: Alpaca key\n"
    "regex allows mixed-case\n"
    "suffixes, _serialize_positions\n"
    "_safe_float-guards numeric\n"
    "reads, day_pnl KPI hides color\n"
    "when null, login rebranded,\n"
    "dead renderTpSync removed.\n"
    "\n"
    "v4.0.8 \u2014 audit HIGH batch:\n"
    "dashboard correctness: login\n"
    "accepts multipart, trade log\n"
    "rejects stale tp portfolio,\n"
    "log tail renders structured\n"
    "ts/level, SSE watchdog de-\n"
    "duplicates reconnects.\n"
    "\n"
    "v4.0.7 \u2014 dashboard hardening:\n"
    "login XFF guard, 32-byte\n"
    "session secret floor, Alpaca\n"
    "key redaction in /stream,\n"
    "HTML-escaped login errors.\n"
    "\n"
    "v4.0.3-beta \u2014 OR seed fix:\n"
    "Pull 9:30 ET OR from Alpaca\n"
    "at boot (#87); staleness\n"
    "guard widened 1.5% \u2192 5%.\n"
    "\n"
    "v4.0.2-beta \u2014 DI seed at boot:\n"
    "Pull 5m bars from Alpaca at\n"
    "scanner startup (#86); DI\n"
    "gate armed on first scan,\n"
    "not 70 min in.\n"
    "\n"
    "v4.0.1-beta \u2014 UI + gate fixes:\n"
    "dashboard row reorder, Val/\n"
    "Gene tabs mirror Main, shared\n"
    "market-state + per-exec\n"
    "trades, scanner OR latch\n"
    "fix, volume fiction removed,\n"
    "DI exposed as real gate.\n"
    "\n"
    "v4.0.0-beta \u2014 Gene + dashboard:\n"
    "second Alpaca executor Gene\n"
    "mirrors main signals, matches\n"
    "Val semantics. Dashboard now\n"
    "has 3 tabs (Main/Val/Gene)\n"
    "with paper/live badges, an\n"
    "index ticker strip, and the\n"
    "shorts P&L sign fix.\n"
    "\n"
    "v4.0.0-alpha \u2014 Val executor:\n"
    "main emits signals, Val\n"
    "mirrors to Alpaca paper.\n"
    "/mode val paper | live\n"
    "confirm. Strict paper/live\n"
    "segregation. Separate\n"
    "Val Telegram bot. Async\n"
    "fire-and-forget dispatch.\n"
    "\n"
    "v3.6.0 \u2014 Telegram auth guard:\n"
    "every update checked against\n"
    "TRADEGENIUS_OWNER_IDS before\n"
    "any handler fires. Non-owners\n"
    "silently dropped.\n"
    "\n"
    "v3.5.1 \u2014 TradeGenius rename:\n"
    "stock_spike_monitor.py \u2192\n"
    "trade_genius.py. Dashboard,\n"
    "Telegram startup card, and\n"
    "Docker/Railway/nixpacks\n"
    "entry points all updated.\n"
    "\n"
    "v3.5.0 \u2014 Deletion Pass:\n"
    "removed TP webhook, TP book,\n"
    "dual-bot wiring, RH IMAP +\n"
    "Gmail poll, /tp_sync +\n"
    "/rh_* commands. \u22122,110\n"
    "lines in the main file.\n"
    "\n"
    "v3.4.47 \u2014 Eye of the\n"
    "Tiger 2.0: 2-bar OR confirm\n"
    "+ DI+(5m,15) > 25 gate +\n"
    "Hard Eject exit."
)
MAIN_RELEASE_NOTE = CURRENT_MAIN_NOTE + "\n\n" + _MAIN_HISTORY_TAIL
# Backwards-compat alias — any remaining references default to main.
RELEASE_NOTE = MAIN_RELEASE_NOTE

FMP_API_KEY = os.getenv("FMP_API_KEY", "VqYj2Jujrc8IvUOe4CR1g0tRf0qlB4AV")

# Human-readable exit reason labels
REASON_LABELS = {
    "STOP": "\U0001f6d1 Hard Stop",
    "TRAIL": "\U0001f3af Trail Stop",
    "RED_CANDLE": "\U0001f56f Red Candle (lost daily polarity)",
    # Long global eject — v3.4.28 Sovereign Regime Shield: SPY AND QQQ
    # 1m finalized close BELOW their PDC. Older labels retained so rows
    # in the persistent trade log from prior versions still render.
    "LORDS_LEFT":      "\U0001f451 Lords Left (SPY+QQQ 1m < PDC)",
    "LORDS_LEFT[1m]":  "\U0001f451 Lords Left (SPY/QQQ < AVWAP)",   # legacy v2.9.8
    "LORDS_LEFT[5m]":  "\U0001f451 Lords Left (SPY+QQQ 5m < AVWAP)",  # legacy v3.2.0–v3.4.27
    "POLARITY_SHIFT": "\U0001f504 Polarity Shift (price > PDC)",
    # Short global eject — v3.4.28 Sovereign Regime Shield mirror.
    "BULL_VACUUM":     "\U0001f300 Bull Vacuum (SPY+QQQ 1m > PDC)",
    "BULL_VACUUM[1m]": "\U0001f300 Bull Vacuum (SPY/QQQ > AVWAP)",  # legacy v2.9.8
    "BULL_VACUUM[5m]": "\U0001f300 Bull Vacuum (SPY+QQQ 5m > AVWAP)",  # legacy v3.2.0–v3.4.27
    "EOD": "\U0001f514 End of Day",
}

# ============================================================
# LOGGING
# ============================================================
LOG_FILE = "trade_genius.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================
# SIGNAL BUS (v4.0.0-alpha)
# ============================================================
# Main's paper book is the brain; executor bots (TradeGeniusVal, and
# in v4.0.0-beta TradeGeniusGene) subscribe to this bus and mirror
# signals onto Alpaca. Dispatch is async fire-and-forget: each listener
# runs in its own daemon thread so the main loop never blocks on an
# Alpaca round-trip and a single bad listener can't take the bus down.
#
# Event schema (dict):
#   {
#     "kind": "ENTRY_LONG" | "ENTRY_SHORT" | "EXIT_LONG" | "EXIT_SHORT" | "EOD_CLOSE_ALL",
#     "ticker": "AAPL",               # omitted on EOD_CLOSE_ALL
#     "price": 175.42,                # main's reference price
#     "reason": "BREAKOUT" | "STOP" | "TRAIL" | "RED_CANDLE" | ... ,
#     "timestamp_utc": "2026-04-24T13:45:12Z",
#     "main_shares": 57,              # audit-only: shares main paper book traded
#   }
_signal_listeners: list = []
_signal_listeners_lock = threading.Lock()

# v5.5.7 \u2014 Most recent signal emitted by the main paper book. The
# per-executor TradeGeniusBase already keeps its own ``last_signal`` for
# the Val/Gene exec panels; this module-level mirror is the equivalent
# for the Main (internal paper) tab so the dashboard's /api/state can
# surface it the same way as the executor payloads.
last_signal: "dict | None" = None


def register_signal_listener(fn):
    """Subscribe a callable fn(event: dict) -> None to the signal bus.

    Idempotent: re-registering the same callable is a no-op. Prevents
    double-execution of ENTRY/EXIT against Alpaca when an executor's
    ``start()`` is called more than once (e.g. supervisor re-spawn, a
    module reload during hot-patching, or a paranoid init-retry path).
    The read-test-append is held under ``_signal_listeners_lock`` so
    two concurrent ``start()`` calls cannot both observe "not present"
    and both append the same callable.
    """
    with _signal_listeners_lock:
        if fn in _signal_listeners:
            logger.info(
                "signal_bus: listener already registered, skipping (%s) total=%d",
                getattr(fn, "__qualname__", repr(fn)), len(_signal_listeners),
            )
            return
        _signal_listeners.append(fn)
        total = len(_signal_listeners)
    logger.info(
        "signal_bus: listener registered (%s) total=%d",
        getattr(fn, "__qualname__", repr(fn)), total,
    )


def _emit_signal(event: dict) -> None:
    """Fire an event to every listener in its own daemon thread.

    Async fire-and-forget: main's paper book never blocks on Alpaca.
    Per-listener exceptions are logged but never break the bus.
    """
    # v5.5.7 \u2014 capture the latest event for the Main-tab LAST SIGNAL
    # card before dispatching, so even a listener-less moment (or a
    # crashing listener) still updates what the dashboard renders.
    global last_signal
    try:
        last_signal = {
            "kind": event.get("kind", ""),
            "ticker": event.get("ticker", ""),
            "price": float(event.get("price", 0.0) or 0.0),
            "reason": event.get("reason", ""),
            "timestamp_utc": event.get("timestamp_utc", _utc_now_iso()),
        }
    except Exception:
        last_signal = None

    # Snapshot the listener list so a concurrent register/unregister can't
    # mutate what we iterate. Held under the same lock as registration.
    with _signal_listeners_lock:
        listeners = list(_signal_listeners)
    if not listeners:
        return

    def _wrap(fn, ev):
        try:
            fn(ev)
        except Exception:
            logger.exception(
                "signal_bus: listener %s raised on event %r",
                getattr(fn, "__qualname__", repr(fn)),
                ev.get("kind"),
            )

    for fn in listeners:
        threading.Thread(
            target=_wrap, args=(fn, event), daemon=True,
        ).start()


# ============================================================
# TRADEGENIUS EXECUTOR BASE (v4.0.0-alpha)
# ============================================================
class TradeGeniusBase:
    """Shared base for Alpaca-backed executor bots.

    Subscribes to main's signal bus on startup, manages paper/live mode
    with its own Alpaca client, maintains its own state file, and runs
    its own Telegram bot with its own _auth_guard. Subclasses set NAME
    and ENV_PREFIX \u2014 all behavior lives here.

    Strict paper/live segregation: two state files are kept per bot,
    `tradegenius_{name_lower}_paper.json` and `..._live.json`; a mode
    flip reloads the correct file. A live flip additionally requires an
    explicit `confirm` token AND a sanity check (get_account on the live
    creds must succeed and report ACTIVE).
    """

    NAME = "BASE"        # override: "Val", "Gene"
    ENV_PREFIX = ""      # override: "VAL_", "GENE_"

    def __init__(self):
        p = self.ENV_PREFIX
        self.paper_key = os.getenv(p + "ALPACA_PAPER_KEY", "").strip()
        self.paper_secret = os.getenv(p + "ALPACA_PAPER_SECRET", "").strip()
        self.live_key = os.getenv(p + "ALPACA_LIVE_KEY", "").strip()
        self.live_secret = os.getenv(p + "ALPACA_LIVE_SECRET", "").strip()
        # Per-bot Telegram token env var: VAL_TELEGRAM_TG / GENE_TELEGRAM_TG
        # (matches what's provisioned on Railway). Note: this is distinct from
        # the main TradeGenius bot's TELEGRAM_TOKEN at module scope.
        self.telegram_token = os.getenv(p + "TELEGRAM_TG", "").strip()
        # v5.0.3 \u2014 TELEGRAM_CHAT_ID is no longer required. Kept as an
        # optional seed for the auto-learned chat-map (back-compat: if an
        # operator had hand-set this previously, it still works on first
        # boot before any owner DMs the bot). See `_owner_chats` below.
        self.telegram_chat_id = os.getenv(p + "TELEGRAM_CHAT_ID", "").strip()
        # Unified owner list: all executor bots share the SAME owner set
        # as main (TRADEGENIUS_OWNER_IDS). One list to maintain on Railway.
        # No per-bot VAL_/GENE_TELEGRAM_OWNER_IDS — intentionally removed.
        self.owner_ids = set(TRADEGENIUS_OWNER_IDS)
        try:
            self.dollars_per_entry = float(
                os.getenv(p + "DOLLARS_PER_ENTRY", "10000")
            )
        except ValueError:
            self.dollars_per_entry = 10000.0
        # v5.1.4 \u2014 equity-aware sizing caps for the LIVE executor
        # path. Each entry is sized as
        #   min(dollars_per_entry,
        #       equity * max_pct_per_entry/100,
        #       cash - min_reserve_cash)
        # so a smaller account never blindly fires a fixed $10k entry
        # that Alpaca then rejects on the 4th signal. Paper book sizing
        # is unaffected.
        try:
            self.max_pct_per_entry = float(
                os.getenv(p + "MAX_PCT_PER_ENTRY", "10.0")
            )
        except ValueError:
            self.max_pct_per_entry = 10.0
        try:
            self.min_reserve_cash = float(
                os.getenv(p + "MIN_RESERVE_CASH", "500.0")
            )
        except ValueError:
            self.min_reserve_cash = 500.0
        self.mode = "paper"
        # Client is built lazily on first use so __init__ never touches
        # the network (smoke tests, missing keys, etc.).
        self.client = None
        self._state = {"mode": "paper", "last_updated": None}
        self._load_state()
        # Own Telegram Application instance, created in start().
        self._tg_app = None
        # v4.0.0-beta — last signal seen from the bus (for dashboard).
        # Populated by _on_signal; None until first event arrives.
        self.last_signal: "dict | None" = None
        # v5.0.3 \u2014 auto-learned owner chat-map. Each entry is
        # owner_id_str -> chat_id_int. Persisted on disk under
        # `<PREFIX>EXECUTOR_CHATS_PATH` (default
        # /data/executor_chats_<name>.json on Railway). Updated whenever
        # an owner DMs this executor bot (see _record_owner_chat hooked
        # into _auth_guard). Trade confirmations fan out to every entry.
        default_chats_path = f"/data/executor_chats_{self.NAME.lower()}.json"
        self._owner_chats_path = (
            os.getenv(p + "EXECUTOR_CHATS_PATH", "").strip()
            or default_chats_path
        )
        self._owner_chats: dict[str, int] = {}
        self._load_owner_chats()
        # Back-compat seed: if <PREFIX>TELEGRAM_CHAT_ID is set and we
        # don't yet have any learned chats, treat it as the seed value
        # for every owner. Once an owner DMs the bot, _record_owner_chat
        # will overwrite that owner's slot with their real chat_id.
        if self.telegram_chat_id and not self._owner_chats:
            try:
                seed = int(self.telegram_chat_id)
                for oid in self.owner_ids:
                    self._owner_chats[oid] = seed
            except ValueError:
                logger.warning(
                    "[%s] %sTELEGRAM_CHAT_ID is not an int (%r); ignoring as seed",
                    self.NAME, p, self.telegram_chat_id,
                )
        # Track whether we've already logged the "empty chat-map" warning
        # so the warning fires once per process, not on every signal.
        self._empty_chats_warned = False
        # v5.2.1 \u2014 executor-side view of broker positions, keyed by
        # ticker. Populated by _record_position on successful submit and
        # by _reconcile_broker_positions at boot. Used to detect orphans
        # the bot does not know about (broker accepted, client timed out).
        self.positions: dict = {}
        # v5.5.10 \u2014 rehydrate from state.db BEFORE
        # _reconcile_broker_positions runs (called from start()) so a
        # plain reboot during a live session sees persisted == broker
        # and stays silent. Wrapped: a bad load must never crash boot.
        try:
            self._load_persisted_positions()
        except Exception:
            logger.exception(
                "[%s] _load_persisted_positions failed \u2014 continuing with empty dict",
                self.NAME,
            )

    # ---------- state files ----------
    def _state_file(self, mode: str = None) -> str:
        m = (mode or self.mode).strip().lower()
        return f"tradegenius_{self.NAME.lower()}_{m}.json"

    def _save_state(self) -> None:
        self._state["mode"] = self.mode
        self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
        path = self._state_file()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
        except Exception:
            logger.exception("[%s] save state failed (%s)", self.NAME, path)

    def _load_state(self) -> None:
        # First load: if a persisted mode file exists for EITHER mode,
        # prefer the most recently written one so a live-mode restart
        # stays in live. If neither exists, default to paper.
        paper_path = self._state_file("paper")
        live_path = self._state_file("live")
        candidates = []
        for m, p in (("paper", paper_path), ("live", live_path)):
            if os.path.exists(p):
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    mtime = 0.0
                candidates.append((mtime, m, p))
        if not candidates:
            return
        candidates.sort(reverse=True)
        _, chosen_mode, chosen_path = candidates[0]
        try:
            with open(chosen_path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
            self.mode = self._state.get("mode", chosen_mode)
        except Exception:
            logger.exception("[%s] load state failed (%s)", self.NAME, chosen_path)

    # ---------- Alpaca client ----------
    def _build_alpaca_client(self, mode: str = None):
        """Return a TradingClient for the requested (or current) mode.

        Uses alpaca-py 0.43.2: `paper=True` routes to paper-api.alpaca.markets,
        `paper=False` to api.alpaca.markets. Optional env var overrides
        ALPACA_ENDPOINT_PAPER / ALPACA_ENDPOINT_TRADE are passed via
        url_override when set.

        IMPORTANT: alpaca-py's RESTClient builds the final URL as
            base_url + "/" + api_version + path
        i.e. it ALWAYS appends "/v2". So url_override must be the HOST
        (e.g. https://paper-api.alpaca.markets) and must NOT already
        include a trailing /v2. We defensively strip any trailing "/v2"
        or "/v2/" so a misconfigured Railway env var can't cause
        double-prefixed URLs (https://.../v2/v2/account -> 404).
        """
        from alpaca.trading.client import TradingClient  # lazy import
        m = (mode or self.mode).strip().lower()
        if m == "live":
            key, secret = self.live_key, self.live_secret
            url_override = os.getenv("ALPACA_ENDPOINT_TRADE", "").strip() or None
            paper = False
        else:
            key, secret = self.paper_key, self.paper_secret
            url_override = os.getenv("ALPACA_ENDPOINT_PAPER", "").strip() or None
            paper = True
        if url_override:
            # Strip any trailing /v2 or /v2/ the user may have included
            cleaned = url_override.rstrip("/")
            if cleaned.endswith("/v2"):
                cleaned = cleaned[:-3]
            url_override = cleaned or None
        kwargs = {"paper": paper}
        if url_override:
            kwargs["url_override"] = url_override
        return TradingClient(key, secret, **kwargs)

    def _ensure_client(self):
        if self.client is None:
            try:
                self.client = self._build_alpaca_client()
            except Exception:
                logger.exception("[%s] alpaca client build failed", self.NAME)
                self.client = None
        return self.client

    # ---------- sanity check before live flip ----------
    def _live_sanity_check(self):
        """Build a TEMP live client, verify it resolves to a non-paper,
        ACTIVE account, log account_number/cash/buying_power.

        Returns (ok: bool, message: str).
        """
        if not (self.live_key and self.live_secret):
            return (False, "live keys not set in env")
        try:
            tmp = self._build_alpaca_client(mode="live")
            acct = tmp.get_account()
        except Exception as e:
            logger.exception("[%s] live sanity check failed", self.NAME)
            return (False, f"get_account raised: {e}")
        status = str(getattr(acct, "status", "")).upper()
        account_number = getattr(acct, "account_number", "?")
        cash = getattr(acct, "cash", "?")
        buying_power = getattr(acct, "buying_power", "?")
        logger.info(
            "[%s] live sanity: account=%s status=%s cash=%s bp=%s",
            self.NAME, account_number, status, cash, buying_power,
        )
        if "ACTIVE" not in status:
            return (False, f"account not ACTIVE (status={status})")
        return (
            True,
            f"live OK \u2014 acct={account_number} status={status} "
            f"cash={cash} bp={buying_power}",
        )

    # ---------- mode control ----------
    def set_mode(self, new_mode: str, confirm_token: str = None):
        """Flip paper/live. Live requires confirm_token=='confirm' AND
        _live_sanity_check. Returns (ok, message)."""
        nm = (new_mode or "").strip().lower()
        if nm == "paper":
            self.mode = "paper"
            try:
                self.client = self._build_alpaca_client()
            except Exception:
                logger.exception("[%s] rebuild paper client failed", self.NAME)
                self.client = None
            self._save_state()
            # v5.5.10 \u2014 reload positions for the new mode bucket.
            self.positions = {}
            try:
                self._load_persisted_positions()
            except Exception:
                logger.exception(
                    "[%s] reload persisted positions on mode flip failed",
                    self.NAME,
                )
            return (True, "mode set to paper")
        if nm == "live":
            if confirm_token != "confirm":
                return (
                    False,
                    "live flip requires the literal 'confirm' token: "
                    "/mode val live confirm",
                )
            ok, msg = self._live_sanity_check()
            if not ok:
                return (False, f"live sanity failed: {msg}")
            self.mode = "live"
            try:
                self.client = self._build_alpaca_client()
            except Exception as e:
                logger.exception("[%s] rebuild live client failed", self.NAME)
                return (False, f"client rebuild after sanity failed: {e}")
            self._save_state()
            # v5.5.10 \u2014 reload positions for the new mode bucket.
            self.positions = {}
            try:
                self._load_persisted_positions()
            except Exception:
                logger.exception(
                    "[%s] reload persisted positions on mode flip failed",
                    self.NAME,
                )
            return (True, f"mode set to live \u2014 {msg}")
        return (False, f"unknown mode: {new_mode!r} (expected 'paper' or 'live')")

    # ---------- signal listener ----------
    def _shares_for(self, price: float, ticker: "str | None" = None) -> int:
        """v5.1.4 \u2014 equity-aware live sizing.

        Computes shares as
          floor(min(dollars_per_entry,
                    equity * max_pct_per_entry/100,
                    cash - min_reserve_cash) / price)
        and falls back to the legacy fixed-size path
        (`int(dollars_per_entry // price)`) if `get_account()` or the
        float casts raise. The bot must NEVER hard-fail on a network
        blip \u2014 always log and fall through.
        """
        if price is None or price <= 0:
            return 0
        legacy_qty = max(1, int(self.dollars_per_entry // price))
        client = self._ensure_client()
        if client is None:
            return legacy_qty
        try:
            acct = client.get_account()
            equity = float(getattr(acct, "equity", 0) or 0)
            cash = float(getattr(acct, "cash", 0) or 0)
            _bp = float(getattr(acct, "buying_power", 0) or 0)
        except Exception as e:
            logger.warning(
                "[%s] [SIZING_FALLBACK] get_account failed (%s) \u2014 "
                "using legacy fixed-size sizing $%.0f / $%.2f = %d sh",
                self.NAME, e, self.dollars_per_entry, price, legacy_qty,
            )
            return legacy_qty
        equity_cap = equity * (self.max_pct_per_entry / 100.0)
        cash_available = max(0.0, cash - self.min_reserve_cash)
        effective = min(self.dollars_per_entry, equity_cap, cash_available)
        if effective < price:
            logger.info(
                "[%s] [INSUFFICIENT_EQUITY] ticker=%s price=$%.2f "
                "cash=$%.2f reserve=$%.2f cap=$%.2f",
                self.NAME, ticker if ticker else "n/a", price,
                cash, self.min_reserve_cash, equity_cap,
            )
            return 0
        if effective < self.dollars_per_entry:
            logger.info(
                "[%s] [SIZE_CAPPED] %s requested=$%.0f effective=$%.0f "
                "equity=$%.0f cash=$%.0f cap=$%.0f reserve=$%.0f",
                self.NAME, ticker if ticker else "n/a",
                self.dollars_per_entry, effective,
                equity, cash, equity_cap, self.min_reserve_cash,
            )
        return max(1, int(effective // price))

    # ---------- chat-map persistence (v5.0.3) ----------
    def _load_owner_chats(self) -> None:
        """Load the persisted owner_id -> chat_id map from disk.

        Missing file is fine (first boot or volume reset) — leaves the
        map empty and we wait for an owner to DM the bot. Corrupted
        file logs and leaves the map empty (the next /start will
        rewrite it).
        """
        path = self._owner_chats_path
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            logger.exception(
                "[%s] failed to load owner-chats file (%s); starting empty",
                self.NAME, path,
            )
            return
        if not isinstance(raw, dict):
            logger.warning(
                "[%s] owner-chats file %s has unexpected shape %s; ignoring",
                self.NAME, path, type(raw).__name__,
            )
            return
        for k, v in raw.items():
            try:
                self._owner_chats[str(k)] = int(v)
            except (TypeError, ValueError):
                continue

    def _save_owner_chats(self) -> None:
        """Atomic write of the chat-map to disk."""
        path = self._owner_chats_path
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._owner_chats, f, indent=2)
            os.replace(tmp, path)
        except Exception:
            logger.exception("[%s] save owner-chats failed (%s)", self.NAME, path)

    def _record_owner_chat(self, owner_id: str, chat_id: int) -> None:
        """Update self._owner_chats and persist if the value changed.

        Called from _auth_guard on every inbound message from a verified
        owner so any DM (including /start) auto-registers the chat_id
        without the user needing to run a special command.
        """
        if not owner_id or chat_id is None:
            return
        owner_id = str(owner_id)
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return
        if self._owner_chats.get(owner_id) == chat_id:
            return
        self._owner_chats[owner_id] = chat_id
        self._save_owner_chats()
        logger.info(
            "[%s] learned owner chat: owner_id=%s chat_id=%s (now %d entries)",
            self.NAME, owner_id, chat_id, len(self._owner_chats),
        )

    def _send_own_telegram(self, text: str) -> None:
        """Post to this executor's OWN Telegram chats.

        v5.0.3: fan out to every learned owner chat in self._owner_chats
        (auto-learned from inbound /start). If the map is empty, log
        once and bail — this surfaces the misconfiguration in startup
        logs instead of silently dropping every trade confirmation
        (which is what shipped pre-5.0.3).
        """
        if not self.telegram_token:
            return
        if not self._owner_chats:
            if not self._empty_chats_warned:
                logger.warning(
                    "[%s] notifications EMPTY \u2014 DM this executor's bot "
                    "/start to enable trade confirmations (chat-map at %s)",
                    self.NAME, self._owner_chats_path,
                )
                self._empty_chats_warned = True
            return
        import urllib.parse
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        # Iterate a snapshot so a concurrent _record_owner_chat can't
        # mutate the dict mid-loop.
        for owner_id, chat_id in list(self._owner_chats.items()):
            try:
                data = urllib.parse.urlencode({
                    "chat_id": chat_id,
                    "text": text,
                }).encode("utf-8")
                req = urllib.request.Request(url, data=data, method="POST")
                urllib.request.urlopen(req, timeout=10).read()
            except Exception:
                logger.exception(
                    "[%s] telegram send failed (owner_id=%s chat_id=%s)",
                    self.NAME, owner_id, chat_id,
                )

    # ---------- v5.2.1 idempotency + reconcile ----------
    def _build_client_order_id(self, ticker: str, direction: str) -> str:
        """Deterministic client_order_id for Alpaca submit_order.

        Format: f"{NAME}-{ticker}-{utc_iso_minute}-{direction}".
        Two signals for the same (executor, ticker, minute, direction)
        collapse to the same coid \u2014 Alpaca rejects the dup, the bot
        treats the rejection as success (broker has the original).
        """
        sym = "".join(c for c in (ticker or "").upper() if c.isalnum())
        utc_iso_minute = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
        name = (self.NAME or "BASE").upper()
        return f"{name}-{sym}-{utc_iso_minute}-{direction}"

    def _submit_order_idempotent(self, client, req, coid: str):
        """Wrap client.submit_order with duplicate-coid \u2192 success handling.

        On APIError whose message says the client_order_id must be unique
        (HTTP 422 from Alpaca), look up the existing order by coid and
        return it as if the submit had just succeeded. Re-raise anything
        else.
        """
        try:
            return client.submit_order(req)
        except Exception as e:
            msg = str(e).lower()
            if "client order id" in msg and ("unique" in msg or "duplicate" in msg):
                try:
                    existing = client.get_order_by_client_id(coid)
                except Exception:
                    logger.exception(
                        "[%s] [IDEMPOTENCY] dup rejected but lookup failed coid=%s",
                        self.NAME, coid,
                    )
                    raise
                logger.warning(
                    "[%s] [IDEMPOTENCY] submit_order duplicate rejected as expected: "
                    "coid=%s order_id=%s",
                    self.NAME, coid, getattr(existing, "id", "?"),
                )
                return existing
            raise

    # v5.5.10 \u2014 persistence helpers for self.positions. Backed by
    # the executor_positions table in state.db so a process restart
    # during a live session no longer looks like a divergence.
    def _load_persisted_positions(self) -> None:
        """Hydrate self.positions from state.db (executor_positions).

        Called from __init__ BEFORE _reconcile_broker_positions runs
        in start(). Silent no-op if the table is empty (first boot).
        """
        try:
            rows = persistence.load_executor_positions(self.NAME, self.mode)
        except Exception:
            logger.exception(
                "[%s] persistence.load_executor_positions failed",
                self.NAME,
            )
            return
        if not rows:
            return
        self.positions.update(rows)
        logger.info(
            "[%s] rehydrated %d persisted position(s) from state.db",
            self.NAME, len(rows),
        )

    def _persist_position(self, ticker: str) -> None:
        """INSERT OR REPLACE the row for ticker. Best-effort."""
        pos = self.positions.get(ticker)
        if not pos:
            return
        try:
            persistence.save_executor_position(
                self.NAME, self.mode, ticker, pos,
            )
        except Exception:
            logger.exception(
                "[%s] persistence.save_executor_position failed for %s",
                self.NAME, ticker,
            )

    def _delete_persisted_position(self, ticker: str) -> None:
        """DELETE the row for ticker. Best-effort."""
        try:
            persistence.delete_executor_position(
                self.NAME, self.mode, ticker,
            )
        except Exception:
            logger.exception(
                "[%s] persistence.delete_executor_position failed for %s",
                self.NAME, ticker,
            )

    def _remove_position(self, ticker: str) -> None:
        """Remove ticker from both self.positions and state.db.

        Single hook for every position-close path. The dict pop is
        defensive (a stale-then-gone case is fine); the DB delete
        always runs so a stray row never lingers.
        """
        self.positions.pop(ticker, None)
        self._delete_persisted_position(ticker)

    def _record_position(self, ticker: str, side: str, qty: int, entry_price: float) -> None:
        """Stamp an executor-side record after a successful submit."""
        self.positions[ticker] = {
            "ticker": ticker,
            "side": side,
            "qty": int(qty),
            "entry_price": float(entry_price) if entry_price else 0.0,
            "entry_ts_utc": datetime.now(timezone.utc).isoformat(),
            "source": "SIGNAL",
            "stop": None,
            "trail": None,
        }
        # v5.5.10 \u2014 mirror to state.db so a restart sees this row.
        self._persist_position(ticker)

    def _reconcile_broker_positions(self) -> None:
        """Run once at boot. Pull broker positions, graft any orphans.

        v5.5.10 reframe: this runs AFTER _load_persisted_positions has
        rehydrated self.positions from state.db, so it becomes a true
        safety net rather than the primary state-bootstrap path. Three
        outcomes:

          1. Persisted set == broker set: clean reconcile, INFO log,
             no Telegram (the common reboot case).
          2. Broker has tickers persisted does not: true divergence \u2014
             graft + WARN log + Telegram with "(true divergence)".
          3. Persisted has tickers broker does not: stale local state.
             Quietly self-heal by removing the row. WARN log only,
             no Telegram, no close/exit path called.

        For grafted orphans we keep source='RECONCILE' and persist
        the new row so the next reboot stays silent.
        """
        client = self._ensure_client()
        if client is None:
            logger.warning(
                "[%s] [RECONCILE] no alpaca client \u2014 skipping",
                self.NAME,
            )
            return
        try:
            broker_positions = client.get_all_positions()
        except Exception as e:
            logger.error(
                "[%s] [RECONCILE] get_all_positions failed: %s",
                self.NAME, e,
            )
            return

        broker_by_ticker: dict = {}
        for bp in broker_positions or []:
            ticker = getattr(bp, "symbol", None)
            if not ticker:
                continue
            broker_by_ticker[ticker] = bp

        broker_tickers = set(broker_by_ticker.keys())
        persisted_tickers = set(self.positions.keys())

        # Outcome 3: stale local state \u2014 quiet self-heal.
        for ticker in sorted(persisted_tickers - broker_tickers):
            logger.warning(
                "[%s] [RECONCILE] stale local position: ticker=%s \u2014 "
                "broker says no position, removing",
                self.NAME, ticker,
            )
            self._remove_position(ticker)

        # Outcome 2: graft broker orphans (true divergence).
        grafted = 0
        for ticker in sorted(broker_tickers - persisted_tickers):
            bp = broker_by_ticker[ticker]
            try:
                qty_int = int(bp.qty)
            except Exception:
                logger.exception(
                    "[%s] [RECONCILE] bad qty on %s, skipping",
                    self.NAME, ticker,
                )
                continue
            side = "LONG" if qty_int > 0 else "SHORT"
            try:
                entry_px = float(bp.avg_entry_price)
            except Exception:
                entry_px = 0.0
            self.positions[ticker] = {
                "ticker": ticker,
                "side": side,
                "qty": abs(qty_int),
                "entry_price": entry_px,
                "entry_ts_utc": datetime.now(timezone.utc).isoformat(),
                "source": "RECONCILE",
                "stop": None,
                "trail": None,
            }
            self._persist_position(ticker)
            grafted += 1
            logger.warning(
                "[%s] [RECONCILE] grafted broker orphan: ticker=%s side=%s qty=%d entry=%.2f",
                self.NAME, ticker, side, abs(qty_int), entry_px,
            )

        # Outcome 1: clean reconcile \u2014 silent INFO log, no Telegram.
        if grafted == 0:
            logger.info(
                "[%s] [RECONCILE] clean: %d position(s) match broker",
                self.NAME, len(broker_tickers),
            )
            return

        try:
            self._send_own_telegram(
                f"\u26a0\ufe0f Reconcile: grafted {grafted} broker orphan(s) "
                f"on {self.NAME} boot (true divergence)"
            )
        except Exception:
            logger.exception(
                "[%s] [RECONCILE] telegram fan-out raised", self.NAME,
            )

    def _on_signal(self, event: dict) -> None:
        """Listener callback: dispatch on event['kind']."""
        kind = event.get("kind", "")
        ticker = event.get("ticker", "")
        price = event.get("price", 0.0) or 0.0
        reason = event.get("reason", "")
        label = f"{self.NAME} {self.mode}"

        # v4.0.0-beta — remember the most recent event for the dashboard
        # (last_signal line on the per-executor tab). Captured before any
        # dispatch so we still record what was seen even if Alpaca errors.
        # v4.1.2: the old try/except here was unreachable: `price` is
        # normalised to 0.0-or-numeric at line 534 so `float(price)` can't
        # raise, and dict-literal assignment has no failure mode. Dropped.
        self.last_signal = {
            "kind": kind,
            "ticker": ticker,
            "price": float(price) if price else 0.0,
            "reason": reason,
            "timestamp_utc": event.get("timestamp_utc", _utc_now_iso()),
        }

        client = self._ensure_client()
        if client is None:
            logger.warning(
                "[%s] skip %s %s \u2014 no alpaca client", self.NAME, kind, ticker,
            )
            return

        try:
            from alpaca.trading.requests import MarketOrderRequest, ClosePositionRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
        except Exception:
            logger.exception("[%s] alpaca imports failed", self.NAME)
            return

        try:
            if kind == "ENTRY_LONG":
                qty = self._shares_for(price, ticker=ticker)
                if qty <= 0:
                    return
                coid = self._build_client_order_id(ticker, "LONG")
                order = self._submit_order_idempotent(client, MarketOrderRequest(
                    symbol=ticker, qty=qty,
                    side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                    client_order_id=coid,
                ), coid)
                oid = getattr(order, "id", "?")
                self._record_position(ticker, "LONG", qty, price)
                msg = f"\u2705 {label}: {ticker} BUY {qty} shares @ market (order_id={oid})"
                logger.info(msg)
                self._send_own_telegram(msg)
            elif kind == "ENTRY_SHORT":
                qty = self._shares_for(price, ticker=ticker)
                if qty <= 0:
                    return
                coid = self._build_client_order_id(ticker, "SHORT")
                order = self._submit_order_idempotent(client, MarketOrderRequest(
                    symbol=ticker, qty=qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                    client_order_id=coid,
                ), coid)
                oid = getattr(order, "id", "?")
                self._record_position(ticker, "SHORT", qty, price)
                msg = f"\u2705 {label}: {ticker} SELL {qty} shares short @ market (order_id={oid})"
                logger.info(msg)
                self._send_own_telegram(msg)
            elif kind in ("EXIT_LONG", "EXIT_SHORT"):
                client.close_position(ticker)
                # v5.5.10 \u2014 drop the local + persisted row so a
                # reboot does not see this as a stale position.
                self._remove_position(ticker)
                msg = f"\u2705 {label}: {ticker} CLOSE ({reason})"
                logger.info(msg)
                self._send_own_telegram(msg)
            elif kind == "EOD_CLOSE_ALL":
                client.close_all_positions(cancel_orders=True)
                # v5.5.10 \u2014 wipe every local + persisted row.
                for tkr in list(self.positions.keys()):
                    self._remove_position(tkr)
                msg = f"\u2705 {label}: EOD close_all_positions"
                logger.info(msg)
                self._send_own_telegram(msg)
            else:
                logger.warning("[%s] unknown signal kind %r", self.NAME, kind)
        except Exception as e:
            err = f"\u274c {label}: {ticker or kind} failed: {e}"
            logger.exception("[%s] dispatch failed on %s", self.NAME, kind)
            self._send_own_telegram(err)

    # ---------- own Telegram bot ----------
    async def _auth_guard(self, update, context):
        """Owner-whitelist guard identical in pattern to main's guard.

        v5.0.3 \u2014 also auto-learns the owner's chat_id from any
        inbound message and persists it to disk via _record_owner_chat,
        so trade confirmations get fanned out to the right DM without
        the operator hand-setting <PREFIX>TELEGRAM_CHAT_ID on Railway.
        """
        eff_user = getattr(update, "effective_user", None)
        uid = str(eff_user.id) if eff_user and getattr(eff_user, "id", None) is not None else ""
        if uid and uid in self.owner_ids:
            # v5.0.3: capture the chat_id this owner is DMing us from.
            # effective_chat is the canonical PTB hook; fall back to
            # message.chat where present for older-style updates.
            chat = getattr(update, "effective_chat", None)
            chat_id = getattr(chat, "id", None) if chat is not None else None
            if chat_id is None:
                msg = getattr(update, "message", None)
                if msg is not None:
                    sub = getattr(msg, "chat", None)
                    chat_id = getattr(sub, "id", None) if sub is not None else None
            if chat_id is not None:
                try:
                    self._record_owner_chat(uid, int(chat_id))
                except Exception:
                    logger.exception("[%s] _record_owner_chat raised", self.NAME)
            return
        logger.warning(
            "[%s] auth_guard dropped non-owner (user_id=%r)", self.NAME, uid or "(none)",
        )
        raise ApplicationHandlerStop

    async def cmd_mode(self, update, context):
        """/mode paper  |  /mode live confirm"""
        args = context.args if context and hasattr(context, "args") else []
        if not args:
            await update.message.reply_text(
                f"{self.NAME} mode: {self.mode}\n"
                f"Usage: /mode paper  |  /mode live confirm"
            )
            return
        new_mode = args[0]
        token = args[1] if len(args) > 1 else None
        ok, msg = self.set_mode(new_mode, confirm_token=token)
        marker = "\u2705" if ok else "\u274c"
        await update.message.reply_text(f"{marker} {self.NAME}: {msg}")

    async def cmd_status(self, update, context):
        client = self._ensure_client()
        lines = [f"{self.NAME} status", f"  mode: {self.mode}"]
        if client is None:
            lines.append("  alpaca: (no client \u2014 keys missing?)")
        else:
            try:
                acct = client.get_account()
                lines.append(
                    f"  acct: {getattr(acct, 'account_number', '?')} "
                    f"status={getattr(acct, 'status', '?')}"
                )
                lines.append(f"  cash: {getattr(acct, 'cash', '?')}")
                lines.append(f"  bp:   {getattr(acct, 'buying_power', '?')}")
                try:
                    positions = client.get_all_positions()
                    lines.append(f"  positions: {len(positions)}")
                    for p in positions[:10]:
                        lines.append(
                            f"    {getattr(p, 'symbol', '?')}: "
                            f"{getattr(p, 'qty', '?')} @ {getattr(p, 'avg_entry_price', '?')}"
                        )
                except Exception as e:
                    lines.append(f"  positions: (fetch failed: {e})")
            except Exception as e:
                lines.append(f"  alpaca error: {e}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_halt(self, update, context):
        """Emergency close_all_positions."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(
                f"\u274c {self.NAME}: no alpaca client"
            )
            return
        try:
            client.close_all_positions(cancel_orders=True)
            # v5.5.10 \u2014 drop every local + persisted row so a
            # reboot does not see them as stale positions.
            for tkr in list(self.positions.keys()):
                self._remove_position(tkr)
            await update.message.reply_text(
                f"\u2705 {self.NAME}: HALT \u2014 close_all_positions fired"
            )
        except Exception as e:
            await update.message.reply_text(
                f"\u274c {self.NAME}: halt failed: {e}"
            )

    async def cmd_version(self, update, context):
        await update.message.reply_text(
            f"{self.NAME} executor v{BOT_VERSION}\n"
            f"mode: {self.mode}"
        )

    # --- v4.0.1: expanded executor-bot command surface --------------

    async def cmd_ping(self, update, context):
        """/ping — liveness check (bot up + client reachable)."""
        client = self._ensure_client()
        alpaca_ok = client is not None
        await update.message.reply_text(
            f"\U0001f3d3 {self.NAME}: pong\n"
            f"  version: v{BOT_VERSION}\n"
            f"  mode: {self.mode}\n"
            f"  alpaca client: {'ok' if alpaca_ok else 'missing'}"
        )

    async def cmd_cash(self, update, context):
        """/cash — quick account balance glance."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(
                f"\u274c {self.NAME}: no alpaca client"
            )
            return
        try:
            acct = client.get_account()
            cash = float(getattr(acct, "cash", 0) or 0)
            bp   = float(getattr(acct, "buying_power", 0) or 0)
            eq   = float(getattr(acct, "equity", 0) or 0)
            # v5.1.4 \u2014 surface the equity-aware sizing caps so
            # operators can see what the next entry will be sized at.
            equity_cap = eq * (self.max_pct_per_entry / 100.0)
            cash_avail = max(0.0, cash - self.min_reserve_cash)
            next_entry = min(
                self.dollars_per_entry, equity_cap, cash_avail,
            )
            await update.message.reply_text(
                f"\U0001f4b0 {self.NAME} ({self.mode})\n"
                f"  cash:   ${cash:,.2f}\n"
                f"  equity: ${eq:,.2f}\n"
                f"  bp:     ${bp:,.2f}\n"
                f"  cap:    ${equity_cap:,.2f} "
                f"({self.max_pct_per_entry:.1f}% of equity)\n"
                f"  reserve:${self.min_reserve_cash:,.2f}\n"
                f"  next entry: ${next_entry:,.2f}"
            )
        except Exception as e:
            await update.message.reply_text(
                f"\u274c {self.NAME}: cash fetch failed: {e}"
            )

    async def cmd_positions(self, update, context):
        """/positions — compact positions list only."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(
                f"\u274c {self.NAME}: no alpaca client"
            )
            return
        try:
            positions = client.get_all_positions()
            if not positions:
                await update.message.reply_text(
                    f"{self.NAME}: no open positions"
                )
                return
            lines = [f"{self.NAME} positions ({len(positions)})"]
            for p in positions[:25]:
                sym = getattr(p, "symbol", "?")
                qty = getattr(p, "qty", "?")
                avg = getattr(p, "avg_entry_price", "?")
                try:
                    upl = float(getattr(p, "unrealized_pl", 0) or 0)
                    pct = float(getattr(p, "unrealized_plpc", 0) or 0) * 100
                    lines.append(
                        f"  {sym}: {qty} @ {avg} "
                        f"pnl=${upl:+,.2f} ({pct:+.2f}%)"
                    )
                except Exception:
                    lines.append(f"  {sym}: {qty} @ {avg}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(
                f"\u274c {self.NAME}: positions fetch failed: {e}"
            )

    async def cmd_orders(self, update, context):
        """/orders — recent orders (last 10)."""
        client = self._ensure_client()
        if client is None:
            await update.message.reply_text(
                f"\u274c {self.NAME}: no alpaca client"
            )
            return
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL, limit=10,
            )
            orders = client.get_orders(filter=req)
            if not orders:
                await update.message.reply_text(
                    f"{self.NAME}: no recent orders"
                )
                return
            lines = [f"{self.NAME} recent orders ({len(orders)})"]
            for o in orders:
                sym   = getattr(o, "symbol", "?")
                side  = getattr(getattr(o, "side", None), "value", "?")
                qty   = getattr(o, "qty", "?") or getattr(o, "notional", "?")
                stat  = getattr(getattr(o, "status", None), "value", "?")
                filled = getattr(o, "filled_avg_price", None)
                tail = f" @ {filled}" if filled else ""
                lines.append(f"  {sym} {side} {qty} [{stat}]{tail}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(
                f"\u274c {self.NAME}: orders fetch failed: {e}"
            )

    async def cmd_signal(self, update, context):
        """/signal — show last signal received from main's bus."""
        sig = self.last_signal
        if not sig:
            await update.message.reply_text(
                f"{self.NAME}: no signals received yet"
            )
            return
        try:
            import json as _json
            pretty = _json.dumps(sig, indent=2, default=str)[:1500]
        except Exception:
            pretty = str(sig)[:1500]
        await update.message.reply_text(
            f"{self.NAME} last signal:\n{pretty}"
        )

    # -----------------------------------------------------------------

    # Commands shown in Telegram's BotFather / slash menu. Keep short
    # descriptions — Telegram truncates aggressively on mobile.
    TG_MENU_COMMANDS = [
        ("status",    "Account, positions, and P&L"),
        ("positions", "Open positions only"),
        ("orders",    "Recent orders (last 10)"),
        ("cash",      "Account balance snapshot"),
        ("signal",    "Last signal from main"),
        ("mode",      "Show or change mode (paper / live)"),
        ("halt",      "Emergency halt \u2014 flatten all"),
        ("ping",      "Liveness check"),
        ("version",   "Show running version"),
        ("help",      "List available commands"),
    ]

    async def cmd_help(self, update, context):
        """/help — list all available commands for this bot."""
        lines = [f"{self.NAME} commands:"]
        for cmd, desc in self.TG_MENU_COMMANDS:
            lines.append(f"/{cmd} \u2014 {desc}")
        await update.message.reply_text("\n".join(lines))

    async def _post_init_register_menu(self, app):
        """PTB post_init hook: register slash-command menu with Telegram
        so commands show up in the in-chat `/` picker automatically —
        no manual BotFather /setcommands needed."""
        try:
            cmds = [BotCommand(c, d) for c, d in self.TG_MENU_COMMANDS]
            await app.bot.set_my_commands(
                cmds, scope=BotCommandScopeAllPrivateChats()
            )
            logger.info("[%s] registered %d telegram menu commands",
                        self.NAME, len(cmds))
        except Exception:
            logger.exception("[%s] set_my_commands failed", self.NAME)

    async def _tg_main(self):
        """Async entry point for the executor's Telegram bot. Uses the
        low-level Application lifecycle (initialize/start/updater.start_polling)
        instead of app.run_polling() — because run_polling() tries to
        install OS signal handlers via loop.add_signal_handler(), which
        Python disallows outside the main thread (set_wakeup_fd only
        works in main thread of the main interpreter). Executor bots
        run on their own background threads, so we must drive the
        Application lifecycle manually."""
        # NOTE: we intentionally do NOT use .post_init() here — that hook
        # is only fired by Application.run_polling() / run_webhook(), and
        # we drive the lifecycle manually (initialize/start/updater) to
        # avoid the set_wakeup_fd main-thread restriction. Instead we
        # call _post_init_register_menu directly after initialize() below.
        app = (
            Application.builder()
            .token(self.telegram_token)
            .build()
        )
        self._tg_app = app
        app.add_handler(TypeHandler(Update, self._auth_guard), group=-1)
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("orders", self.cmd_orders))
        app.add_handler(CommandHandler("cash", self.cmd_cash))
        app.add_handler(CommandHandler("signal", self.cmd_signal))
        app.add_handler(CommandHandler("halt", self.cmd_halt))
        app.add_handler(CommandHandler("ping", self.cmd_ping))
        app.add_handler(CommandHandler("version", self.cmd_version))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("start", self.cmd_help))
        await app.initialize()
        # Register the slash-command menu with Telegram now that the
        # Bot instance is usable (post app.initialize()). Failures are
        # logged inside the helper and never block startup.
        await self._post_init_register_menu(app)
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("[%s] telegram loop running (token=...%s)",
                    self.NAME, self.telegram_token[-6:])
        # Park forever — updater polls in its own task. Exits only when
        # the thread/process is torn down.
        try:
            await asyncio.Event().wait()
        finally:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:
                pass

    def _run_tg_loop(self):
        """Run this executor's own Telegram polling loop in its own
        thread. Creates its own asyncio event loop (PTB requires one)."""
        if not self.telegram_token:
            logger.info("[%s] telegram token unset \u2014 skipping tg loop", self.NAME)
            return
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            logger.info("[%s] telegram loop starting", self.NAME)
            loop.run_until_complete(self._tg_main())
        except Exception:
            logger.exception("[%s] telegram loop crashed", self.NAME)

    # ---------- startup ----------
    def start(self):
        """Subscribe to main's signal bus and start own Telegram loop."""
        register_signal_listener(self._on_signal)
        # Try to build the alpaca client eagerly so startup logs surface
        # missing/bad creds; failures are already caught + logged.
        self._ensure_client()
        # v5.2.1 \u2014 reconcile broker-side positions into self.positions
        # before the scan loop starts so orphan trades (broker accepted
        # but client timed out on a prior boot) get managed as normal.
        # Wrapped: a bad reconcile must not block scanner startup.
        try:
            self._reconcile_broker_positions()
        except Exception:
            logger.exception(
                "[%s] [RECONCILE] unexpected failure \u2014 continuing startup",
                self.NAME,
            )
        logger.info("[%s] started in %s mode", self.NAME, self.mode)
        # Own Telegram bot in a background thread so main.run_telegram_bot()
        # can still own the main-process asyncio loop.
        t = threading.Thread(target=self._run_tg_loop, daemon=True, name=f"{self.NAME}_tg")
        t.start()


class TradeGeniusVal(TradeGeniusBase):
    """Val \u2014 first Genius executor. Alpaca paper by default; Val flips
    to live via `/mode live confirm` on Val's own Telegram bot, or via
    `/mode val live confirm` on main's bot."""
    NAME = "Val"
    ENV_PREFIX = "VAL_"


class TradeGeniusGene(TradeGeniusBase):
    """Gene \u2014 second Genius executor, identical in behavior to Val but
    with its own GENE_ env prefix, state files, and Telegram bot. Shipped
    in v4.0.0-beta alongside the 3-tab dashboard."""
    NAME = "Gene"
    ENV_PREFIX = "GENE_"


# Global executor instances (populated at startup if enabled). Referenced
# by main-bot's /mode {val,gene} router; left None when disabled / no keys.
val_executor: "TradeGeniusBase | None" = None
gene_executor: "TradeGeniusBase | None" = None


ET = ZoneInfo("America/New_York")
CDT = ZoneInfo("America/Chicago")   # user display timezone


def _now_et() -> datetime:
    """Current time in ET — for market-hour gate logic only."""
    return datetime.now(timezone.utc).astimezone(ET)


def _now_cdt() -> datetime:
    """Current time in CDT — for all user-facing display."""
    return datetime.now(timezone.utc).astimezone(CDT)


def _utc_now_iso() -> str:
    """UTC ISO timestamp string for internal storage."""
    return datetime.now(timezone.utc).isoformat()


def _to_cdt_hhmm(iso_str: str) -> str:
    """Decode a stored ISO timestamp to 'HH:MM CDT' for display.
    Handles both UTC-stored (new) and ET-stored (legacy) strings."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)   # legacy ET-stored fallback
        return dt.astimezone(CDT).strftime("%H:%M CDT")
    except Exception:
        return iso_str


def _to_cdt_hhmmss(iso_str: str) -> str:
    """Decode a stored ISO timestamp to 'HH:MM:SS' (CDT) for display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(CDT).strftime("%H:%M:%S")
    except Exception:
        return iso_str


def _parse_time_to_cdt(ts):
    """Normalise any stored timestamp format to HH:MM CDT."""
    if not ts:
        return "??:??"
    ts = str(ts).strip()
    # ISO format with timezone offset (stored as UTC)
    if "T" in ts and ("+" in ts or ts.endswith("Z")):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            cdt_dt = dt.astimezone(CDT)
            return cdt_dt.strftime("%H:%M")
        except Exception:
            pass
    # HH:MM:SS or HH:MM — already local (CDT), just truncate
    parts = ts.split(":")
    if len(parts) >= 2:
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return ts[:5]


def _is_today(ts_str: str) -> bool:
    """Check if an ISO timestamp string is from today (ET-based)."""
    if not ts_str:
        return False
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        today_et = _now_et().date()
        return dt.astimezone(ET).date() == today_et
    except Exception:
        return False


# ── Matplotlib (optional — graceful skip if not installed) ──────────────
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import io as _io
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# Pre-warm matplotlib font manager in background thread so first chart
# call doesn't block the event loop for ~30-50 seconds.
if MATPLOTLIB_AVAILABLE:
    def _warm_matplotlib():
        try:
            fig, ax = plt.subplots()
            plt.close(fig)
        except Exception as e:
            # v4.1.2: don't swallow silently — a broken matplotlib install
            # will make `/dayreport` fail later, and a DEBUG line here gives
            # the operator a breadcrumb when chart generation explodes.
            logger.debug("matplotlib warmup failed: %s", e)
    threading.Thread(target=_warm_matplotlib, daemon=True).start()


def _parse_date_arg(args):
    """Parse optional date argument from command args. Returns date in ET."""
    import datetime as _dt
    today = _now_et().date()
    if not args:
        return today
    raw = " ".join(args).strip().lower()
    if raw == "yesterday":
        d = today - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    # Try YYYY-MM-DD
    try:
        return _dt.date.fromisoformat(raw)
    except ValueError:
        pass
    # Try integer = last N days (for /perf)
    try:
        n = int(raw)
        if 1 <= n <= 365:
            return today - timedelta(days=n)
    except ValueError:
        pass
    # Try "Apr 17" or "April 17"
    for fmt in ["%b %d", "%B %d"]:
        try:
            parsed = _dt.datetime.strptime(raw, fmt)
            return parsed.replace(year=today.year).date()
        except ValueError:
            pass
    # Try weekday names
    days_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    for abbr, num in days_map.items():
        if raw.startswith(abbr):
            delta = (today.weekday() - num) % 7
            if delta == 0:
                delta = 7
            return today - timedelta(days=delta)
    return today  # fallback


# Short reason labels for compact /dayreport display
_SHORT_REASON = {
    "\U0001f6d1": "\U0001f6d1 Stop",
    "\U0001f512": "\U0001f512 Trail",
    "\U0001f56f": "\U0001f56f Red Candle",
    "\U0001f451": "\U0001f451 Lords Left",
    "\U0001f504": "\U0001f504 Polarity Shift",
    "\U0001f300": "\U0001f300 Bull Vacuum",
    "\U0001f4c9": "\U0001f4c9 PDC Break",
    "\U0001f514": "\U0001f514 EOD",
}


# ============================================================
# PAPER TRADING CONFIG
# ============================================================
PAPER_LOG              = os.getenv("PAPER_LOG_PATH", "investment.log")
PAPER_STATE_FILE       = os.getenv("PAPER_STATE_PATH", "paper_state.json")
# v3.4.27 — persistent trade log. Default path is a sibling of the
# paper state file so it lands on the same volume automatically. The
# file is append-only JSONL — one closed trade per line. Survives
# redeploys when written to the mounted volume.
TRADE_LOG_FILE         = os.getenv(
    "TRADE_LOG_PATH",
    os.path.join(os.path.dirname(PAPER_STATE_FILE) or ".", "trade_log.jsonl"),
)
PAPER_STARTING_CAPITAL = 100_000.0

# Investment logger (separate file)
inv_logger = logging.getLogger("investment")
inv_logger.setLevel(logging.INFO)
_inv_fh = logging.FileHandler(PAPER_LOG, encoding="utf-8")
_inv_fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
inv_logger.addHandler(_inv_fh)
inv_logger.propagate = False


def paper_log(msg: str):
    """Write a timestamped line to investment.log and the main logger."""
    inv_logger.info(msg)
    logger.info("[PAPER] %s", msg)


# ============================================================
# STRATEGY CONSTANTS
# ============================================================
# ------------------------------------------------------------
# v3.4.32: the ticker universe is now editable at runtime from
# Telegram via /add_ticker, /remove_ticker, /tickers. The list
# is persisted to TICKERS_FILE so edits survive restarts.
#
# DESIGN NOTES
#   - TICKERS and TRADE_TICKERS stay as module-level mutable
#     lists so every `for t in TICKERS` loop picks up changes
#     without plumbing a getter through ~25 call sites.
#   - SPY and QQQ are PINNED — they drive the Sovereign Regime
#     shield and the RSI regime classifier. They can be added
#     by the defaults but can never be removed via /remove.
#   - TRADE_TICKERS is kept in sync via _rebuild_trade_tickers()
#     which clears the list in place and re-extends from the
#     current TICKERS minus the pinned set.
#   - Persistence is fail-soft: if the JSON is missing, unreadable,
#     or empty, we fall back to TICKERS_DEFAULT. Callers never see
#     an exception.
#   - QBTS is included in the defaults so a fresh deploy (no
#     tickers.json yet) already tracks it.
# ------------------------------------------------------------
TICKERS_FILE = os.getenv("TICKERS_FILE", "tickers.json")
TICKERS_PINNED = ("SPY", "QQQ")   # always present, never removable
TICKERS_DEFAULT = [
    "AAPL", "MSFT", "NVDA", "TSLA", "META",
    "GOOG", "AMZN", "AVGO", "NFLX", "ORCL", "QBTS", "SPY", "QQQ",
]

# v5.7.0 \u2014 Ten Titans universe. Used by the Strike 2+ Expansion Gate
# (HOD/LOD-based unlimited re-entry) below. Alphabetically ordered;
# exactly 10 tickers. Non-Titan tickers added via [WATCHLIST_ADD]
# continue to use the v5.6.0 R3 re-hunt cap.
TITAN_TICKERS: list = [
    "AAPL", "AMZN", "AVGO", "GOOG", "META", "MSFT", "NFLX",
    "NVDA", "ORCL", "TSLA",
]
ENABLE_UNLIMITED_TITAN_STRIKES: bool = True
DAILY_LOSS_LIMIT_DOLLARS: float = -500.0
# v5.7.1 \u2014 Bison & Buffalo exit FSM (Titans only). When False, Titan
# tickers fall back to the legacy DI/structural exits used by non-Titan
# tickers. VELOCITY_FUSE_PCT is the strict >1.0% adverse intra-candle
# threshold (LONG fires <99%, SHORT fires >101% of the current 1m open).
ENABLE_BISON_BUFFALO_EXITS: bool = True
VELOCITY_FUSE_PCT: float = 0.01
TICKERS_MAX = 40            # sanity upper bound to protect cycle budget
TICKER_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")

TICKERS = list(TICKERS_DEFAULT)
TRADE_TICKERS = [t for t in TICKERS if t not in TICKERS_PINNED]


# ------------------------------------------------------------
# v5.1.0 \u2014 Forensic Volume Filter (SHADOW MODE).
# ------------------------------------------------------------
# In v5.1.0 the gate logs every minute via the [V510-SHADOW] prefix
# but does NOT change any entry decision. v5.1.1 will flip enforcement
# on after Val reviews a week of shadow data.
VOLUME_PROFILE_ENABLED: bool = True
_volume_profile_cache: dict = {}        # {ticker: profile dict}
_ws_consumer = None                      # set by _start_volume_profile()
# v5.5.3 \u2014 set True only when _start_volume_profile() resolved
# market-data credentials and started the WS consumer. Surfaced to the
# dashboard via /api/state.shadow_data_status.
SHADOW_DATA_AVAILABLE: bool = False


def _start_volume_profile() -> None:
    """Boot the shadow-mode volume layer once at process start.

    Hard-disables itself if the watchlist exceeds the free-plan IEX
    websocket symbol cap (30). On disable, evaluate_g4 returns DISABLED
    and the bot trades normally.
    """
    global VOLUME_PROFILE_ENABLED, _ws_consumer, SHADOW_DATA_AVAILABLE
    if len(TICKERS) > volume_profile.WS_SYMBOL_CAP_FREE_IEX:
        logger.warning(
            "[VOLPROFILE] watchlist=%d > 30 symbols, exceeds free IEX cap; "
            "upgrade to Algo Trader Plus or reduce watchlist. "
            "Volume profile DISABLED.",
            len(TICKERS),
        )
        VOLUME_PROFILE_ENABLED = False
        volume_profile.VOLUME_PROFILE_ENABLED = False
        return

    # Seed cache from disk (best-effort).
    for t in TICKERS:
        prof = volume_profile.load_profile(t)
        if prof is not None:
            _volume_profile_cache[t] = prof

    # v5.5.3 \u2014 cred lookup chain:
    #   VAL_ALPACA_PAPER_KEY/SECRET (prod) \u2192
    #   ALPACA_PAPER_KEY/SECRET (legacy)   \u2192
    #   ALPACA_KEY/SECRET (legacy)         \u2192 fail.
    # Market-data-only use of Val's Alpaca paper key. Shadow strategies
    # have their own ledger; do NOT call /v2/positions, /v2/account, or
    # any trading endpoint from this code path.
    key = (os.getenv("VAL_ALPACA_PAPER_KEY")
           or os.getenv("ALPACA_PAPER_KEY")
           or os.getenv("ALPACA_KEY")
           or "")
    secret = (os.getenv("VAL_ALPACA_PAPER_SECRET")
              or os.getenv("ALPACA_PAPER_SECRET")
              or os.getenv("ALPACA_SECRET")
              or "")
    if not key or not secret:
        logger.warning(
            "[SHADOW DISABLED] no Alpaca market-data credentials found "
            "(set VAL_ALPACA_PAPER_KEY/SECRET or ALPACA_PAPER_KEY/SECRET); "
            "shadow_positions will not record any rows this session."
        )
        SHADOW_DATA_AVAILABLE = False
        return

    # Synchronous startup rebuild if any profile is missing/stale.
    now = datetime.now(timezone.utc)
    needs_rebuild = [
        t for t in TICKERS
        if (t not in _volume_profile_cache
            or volume_profile.is_profile_stale(_volume_profile_cache[t], now))
    ]
    if needs_rebuild:
        logger.info(
            "[VOLPROFILE] startup rebuild needed for %d/%d tickers",
            len(needs_rebuild), len(TICKERS),
        )
        try:
            volume_profile.rebuild_all_profiles(needs_rebuild, key, secret)
            for t in needs_rebuild:
                prof = volume_profile.load_profile(t)
                if prof is not None:
                    _volume_profile_cache[t] = prof
        except Exception as e:
            logger.error("[VOLPROFILE] startup rebuild crashed: %s", e)

    # Spawn the websocket consumer (daemon thread).
    try:
        _ws_consumer = volume_profile.WebsocketBarConsumer(
            list(TICKERS), key, secret,
        )
        _ws_consumer.start()
        SHADOW_DATA_AVAILABLE = True
    except Exception as e:
        logger.error("[VOLPROFILE] websocket startup failed: %s", e)
        _ws_consumer = None
        SHADOW_DATA_AVAILABLE = False

    # Nightly rebuild thread (21:00 ET).
    def _nightly_loop():
        from zoneinfo import ZoneInfo as _ZI
        et = _ZI("America/New_York")
        while True:
            try:
                now_et = datetime.now(tz=et)
                target = now_et.replace(hour=21, minute=0, second=0, microsecond=0)
                if target <= now_et:
                    target = target + timedelta(days=1)
                sleep_s = max(60.0, (target - now_et).total_seconds())
                time.sleep(sleep_s)
                logger.info("[VOLPROFILE] nightly rebuild starting...")
                volume_profile.rebuild_all_profiles(list(TICKERS), key, secret)
                for t in TICKERS:
                    prof = volume_profile.load_profile(t)
                    if prof is not None:
                        _volume_profile_cache[t] = prof
                logger.info("[VOLPROFILE] nightly rebuild done")
            except Exception as e:
                logger.error("[VOLPROFILE] nightly thread error: %s", e)
                time.sleep(300)

    threading.Thread(
        target=_nightly_loop, name="VolProfileNightly", daemon=True,
    ).start()


def _shadow_log_g4(ticker: str, stage: int, existing_decision) -> None:
    """Emit shadow log lines per candidate evaluation. Failure-tolerant: if
    anything in the gate path raises, log and move on. SHADOW MODE: the
    caller's decision is untouched (v5.1.1 keeps VOL_GATE_ENFORCE=0).

    v5.1.1 emits four log lines per call:
      - the original [V510-SHADOW] line (kept for back-compat with the
        v5.1.0 grep + Apr 20-24 backtest tooling)
      - three [V510-SHADOW][CFG=...] lines for the fixed analysis configs
        TICKER+QQQ at 70/100, TICKER_ONLY at 70, QQQ_ONLY at 100. These
        emit on every candidate regardless of which env-driven config is
        active.
    """
    if not VOLUME_PROFILE_ENABLED:
        return
    try:
        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
        # v5.5.6: shadow gate evaluates the just-closed minute, not the
        # still-forming one. The IEX websocket only delivers the bar at
        # the END of each minute, so reading the current bucket always
        # races the WS bar close-out and produces cur_v=0.
        bucket = volume_profile.previous_session_bucket(now_et)
        if bucket is None:
            return  # outside session \u2014 nothing to evaluate
        idx_symbol = volume_profile.load_active_config().get("index_symbol", "QQQ")
        cur_v = 0
        cur_qqq = 0
        if _ws_consumer is not None:
            cur_v = _ws_consumer.current_volume(ticker, bucket) or 0
            cur_qqq = _ws_consumer.current_volume(idx_symbol, bucket) or 0
        ticker_profile = _volume_profile_cache.get(ticker)
        idx_profile = _volume_profile_cache.get(idx_symbol)

        # v5.1.6: intraminute velocity capture. Emits [V510-VEL] on the
        # FIRST tick (within a candle) where running_vol >= bucket size.
        # Pure observation \u2014 no trading-path effect.
        try:
            if ticker_profile is not None:
                bucket_size = volume_profile._bucket_median(ticker_profile, bucket)
                qqq_pct_for_vel = None
                if idx_profile is not None:
                    qmed = volume_profile._bucket_median(idx_profile, bucket)
                    if qmed:
                        qqq_pct_for_vel = int(round((cur_qqq / qmed) * 100.0))
                _v516_check_velocity(
                    ticker, bucket, now_et, cur_v, bucket_size,
                    qqq_pct=qqq_pct_for_vel,
                )
        except Exception:
            pass

        # (1) Original v5.1.0 shadow line \u2014 unchanged for back-compat.
        g4 = volume_profile.evaluate_g4(
            ticker=ticker,
            minute_bucket=bucket,
            current_volume=cur_v,
            profile=ticker_profile,
            qqq_current_volume=cur_qqq,
            qqq_profile=idx_profile,
            stage=stage,
        )
        logger.info(
            "[V510-SHADOW] ticker=%s bucket=%s stage=%d g4=%s "
            "ticker_pct=%s qqq_pct=%s reason=%s entry_decision=%s",
            ticker, bucket, stage,
            "GREEN" if g4["green"] else "RED",
            g4.get("ticker_pct"), g4.get("qqq_pct"),
            g4["reason"], existing_decision,
        )

        # (2) Three parallel analysis configs \u2014 emit greppable lines.
        for cfg in volume_profile.SHADOW_CONFIGS:
            try:
                res = volume_profile.evaluate_g4_config(
                    ticker=ticker,
                    minute_bucket=bucket,
                    current_volume=cur_v,
                    profile=ticker_profile,
                    index_current_volume=cur_qqq,
                    index_profile=idx_profile,
                    ticker_enabled=cfg["ticker_enabled"],
                    index_enabled=cfg["index_enabled"],
                    ticker_pct=cfg["ticker_pct"],
                    index_pct=cfg["index_pct"],
                )
                # PCT label per config: TICKER+QQQ shows both, the
                # single-anchor configs show only the live anchor.
                if cfg["ticker_enabled"] and cfg["index_enabled"]:
                    pct_label = "%d/%d" % (cfg["ticker_pct"], cfg["index_pct"])
                    pct_fields = "t_pct=%s qqq_pct=%s" % (
                        res.get("ticker_pct"), res.get("qqq_pct"))
                elif cfg["ticker_enabled"]:
                    pct_label = "%d" % cfg["ticker_pct"]
                    pct_fields = "t_pct=%s" % res.get("ticker_pct")
                else:
                    pct_label = "%d" % cfg["index_pct"]
                    pct_fields = "qqq_pct=%s" % res.get("qqq_pct")
                logger.info(
                    "[V510-SHADOW][CFG=%s][PCT=%s] ticker=%s bucket=%s stage=%d "
                    "%s verdict=%s reason=%s entry_decision=%s",
                    cfg["name"], pct_label, ticker, bucket, stage,
                    pct_fields, res["verdict"], res["reason"], existing_decision,
                )
                # v5.2.0 \u2014 if the live bot would have entered AND this
                # config's verdict is PASS, open a virtual shadow long
                # position. Stage 1 only \u2014 stage 2 calls are existing
                # position maintenance (no new entries).
                if (stage == 1
                        and str(existing_decision) == "ENTER"
                        and res.get("verdict") == "PASS"):
                    try:
                        bars_v520 = fetch_1min_bars(ticker)
                        px_v520 = (bars_v520 or {}).get("current_price")
                    except Exception:
                        px_v520 = None
                    if px_v520:
                        _v520_open_shadow(
                            cfg["name"], ticker, "long", float(px_v520))
            except Exception as e:
                logger.warning(
                    "[V510-SHADOW] cfg=%s eval error %s: %s",
                    cfg.get("name"), ticker, e)
    except Exception as e:
        logger.warning("[V510-SHADOW] eval error %s: %s", ticker, e)


# ---------------------------------------------------------------------------
# v5.2.0 \u2014 Shadow strategy P&L tracker integration.
#
# Each SHADOW_CONFIGS entry that emits a would-have-entered verdict opens
# a virtual position via shadow_pnl.tracker(). Sizing reuses the v5.1.4
# equity-aware formula but pulls equity/cash from the MAIN PAPER PORTFOLIO
# (Tiger/Buffalo book \u2014 the same one tracked via v5_long_tracks) so
# shadow P&L is directly comparable to the paper bot's P&L. No Alpaca
# round-trip is involved in the shadow flow. Failure-tolerant: any error
# in the shadow path logs a warning and lets the live bot continue.
# ---------------------------------------------------------------------------

# Sizing caps for shadow positions. Mirror v5.1.4's defaults so shadow
# P&L stays comparable to the live executors' sizing rules. Overridable
# via env so a paper-only tuning run does not need a code change.
_V520_SHADOW_MAX_PCT_PER_ENTRY = float(
    os.getenv("PAPER_MAX_PCT_PER_ENTRY", "10.0") or 10.0)
_V520_SHADOW_MIN_RESERVE_CASH = float(
    os.getenv("PAPER_MIN_RESERVE_CASH", "500.0") or 500.0)


def _v520_paper_equity_snapshot() -> dict | None:
    """Snapshot the MAIN PAPER PORTFOLIO equity/cash for shadow sizing.

    The paper book is the canonical comparator for shadow strategies
    (Tiger/Buffalo, tracked via v5_long_tracks). Equity is derived as

        paper_equity = paper_cash + sum(long mark-to-market value)
                       \u2212 sum(short buy-back liability)

    using the SAME accounting convention as the dashboard's `_equity()`
    helper. We use entry price as a fallback when a live mark is not
    available so a missing 1m bar never blocks a shadow open.

    Returns None when the paper book has no cash field (e.g. fixture
    boot order). Callers treat None as "skip shadow open this cycle".
    """
    try:
        cash = globals().get("paper_cash", None)
        if cash is None:
            return None
        cash_f = float(cash)
        long_mv = 0.0
        for tkr, pos in (positions or {}).items():
            try:
                shares = float(pos.get("shares", 0) or 0)
                if shares <= 0:
                    continue
                mark = None
                try:
                    bars = fetch_1min_bars(tkr)
                    if bars and bars.get("current_price"):
                        mark = float(bars["current_price"])
                except Exception:
                    mark = None
                if mark is None or mark <= 0:
                    mark = float(pos.get("entry_price", 0) or 0)
                long_mv += mark * shares
            except Exception:
                continue
        short_liab = 0.0
        for tkr, pos in (short_positions or {}).items():
            try:
                shares = float(pos.get("shares", 0) or 0)
                if shares <= 0:
                    continue
                mark = None
                try:
                    bars = fetch_1min_bars(tkr)
                    if bars and bars.get("current_price"):
                        mark = float(bars["current_price"])
                except Exception:
                    mark = None
                if mark is None or mark <= 0:
                    mark = float(pos.get("entry_price", 0) or 0)
                short_liab += mark * shares
            except Exception:
                continue
        equity = cash_f + long_mv - short_liab
        return {
            "equity": equity,
            "cash": cash_f,
            "dollars_per_entry": float(
                globals().get("PAPER_DOLLARS_PER_ENTRY", 10000.0) or 10000.0),
            "max_pct_per_entry": _V520_SHADOW_MAX_PCT_PER_ENTRY,
            "min_reserve_cash": _V520_SHADOW_MIN_RESERVE_CASH,
        }
    except Exception as e:
        logger.warning("[V520-SHADOW-PNL] paper equity snapshot failed: %s", e)
        return None


def _v520_open_shadow(
    config_name: str, ticker: str, side: str, entry_price: float,
) -> None:
    """Open a virtual shadow position for `config_name` on `ticker`.

    Sizing pulls the paper portfolio's equity; if that snapshot fails
    we skip silently \u2014 the live trading path is unaffected.
    """
    if entry_price is None or entry_price <= 0:
        return
    snap = _v520_paper_equity_snapshot()
    if snap is None:
        return
    try:
        rid = shadow_pnl.tracker().open_position(
            config_name=config_name, ticker=ticker, side=side,
            entry_ts_utc=datetime.now(tz=timezone.utc),
            entry_price=float(entry_price),
            equity_snapshot=snap,
        )
        if rid is not None:
            logger.info(
                "[V520-SHADOW-PNL] OPEN cfg=%s ticker=%s side=%s "
                "entry=$%.2f paper_eq=$%.0f paper_cash=$%.0f",
                config_name, ticker, side, float(entry_price),
                snap["equity"], snap["cash"],
            )
    except Exception as e:
        logger.warning(
            "[V520-SHADOW-PNL] open failed cfg=%s t=%s: %s",
            config_name, ticker, e)


def _v520_mtm_ticker(ticker: str, current_price: float) -> None:
    """Mark-to-market every open shadow position on `ticker`. Called
    once per scan cycle per ticker. Failure-tolerant.
    """
    try:
        if current_price is None or current_price <= 0:
            return
        shadow_pnl.tracker().mark_to_market(
            ticker=ticker, current_price=float(current_price),
            current_ts=datetime.now(tz=timezone.utc),
        )
    except Exception as e:
        logger.warning("[V520-SHADOW-PNL] mtm error t=%s: %s", ticker, e)


# v5.2.1 M3: canonical shadow-config registry. SHADOW_CONFIGS is a
# tuple of dicts (each with a "name" key); REHUNT_VOL_CONFIRM and
# OOMPH_ALERT are event-driven extras that own their own virtual
# positions but live outside SHADOW_CONFIGS. This helper returns the
# union so close-fanout / EOD code paths iterate every config that can
# possibly hold an open shadow position.
_V521_EXTRA_SHADOW_CONFIG_NAMES: tuple[str, ...] = (
    "REHUNT_VOL_CONFIRM", "OOMPH_ALERT",
)


def _v521_all_shadow_config_names() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for cfg in volume_profile.SHADOW_CONFIGS:
        n = cfg.get("name") if isinstance(cfg, dict) else None
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    for n in _V521_EXTRA_SHADOW_CONFIG_NAMES:
        if n not in seen:
            seen.add(n)
            names.append(n)
    return names


def _v520_close_shadow_all(
    ticker: str, exit_price: float, reason: str,
) -> None:
    """Close every open shadow position on `ticker` across ALL configs
    using the live exit price + reason. Mirrors the live bot's exit
    decisions one-for-one so shadow P&L is comparable.
    """
    try:
        if exit_price is None or exit_price <= 0:
            return
        tr = shadow_pnl.tracker()
        # v5.2.1 M3: iterate the canonical registry instead of a
        # hardcoded subset. Any new SHADOW_CONFIGS entry, plus the
        # event-driven extras, is picked up automatically.
        for cfg_name in _v521_all_shadow_config_names():
            tr.close_position(
                config_name=cfg_name, ticker=ticker,
                exit_ts_utc=datetime.now(tz=timezone.utc),
                exit_price=float(exit_price),
                exit_reason=str(reason or "STOP"),
            )
    except Exception as e:
        logger.warning("[V520-SHADOW-PNL] close error t=%s: %s", ticker, e)


# ---------------------------------------------------------------------------
# v5.1.9 \u2014 two new shadow configs on top of v5.1.6's 5.
#
#   REHUNT_VOL_CONFIRM \u2014 event-driven. After every HARD_EJECT_TIGER exit,
#       watch the SAME ticker for the next 10 minutes. On the FIRST
#       1-min bar where vol vs bucket median is >=100% AND DI on the
#       exit side is still >25, emit one
#         [V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] ...
#       line. Pure observation \u2014 no real trade fires.
#
#   OOMPH_ALERT \u2014 per-minute gate, but it inverts the today flow.
#       Today minute 1 needs DI only and minute 2 needs DI+volume.
#       OOMPH puts the volume burden on minute 1 (DI>25 AND
#       BUCKET_FILL>=100% on the same side) and only requires DI>25 on
#       minute 2 confirmation. Emits one
#         [V510-SHADOW][CFG=OOMPH_ALERT] ...
#       line on the minute-2 confirmation. Per-ticker prev-minute state
#       is held in memory (sufficient for shadow analysis; the weekly
#       Saturday backtest pairs OOMPH entries with subsequent exits).
#
# Both configs are pure observation. VOL_GATE_ENFORCE=0 still gates all
# real trades; these emitters never call execute_entry / open_short.
# ---------------------------------------------------------------------------

# REHUNT window in minutes after a HARD_EJECT_TIGER exit.
_V519_REHUNT_WINDOW_MIN = 10
# Volume vs bucket median threshold (percent) for the re-hunt confirmation.
_V519_REHUNT_VOL_PCT = 100
# DI threshold for the re-hunt confirmation \u2014 mirrors TIGER_V2_DI_THRESHOLD.
_V519_REHUNT_DI_MIN = 25.0

# v5.2.1 M4: state keyed on (ticker, side) tuple where side \u2208
# {"LONG", "SHORT"}. Prior keying on `ticker` alone allowed a long+short
# whipsaw on the same ticker on the same minute to clobber one of the
# two arms. Both arms now coexist and are evaluated independently.
# `fired` is set True once we've emitted the first qualifying re-hunt
# line in the window so we don't re-emit on subsequent confirming
# minutes.
_v519_rehunt_watch: dict[tuple[str, str], dict] = {}


def _v519_arm_rehunt_watch(ticker: str, side: str, exit_ts_utc) -> None:
    """Arm a REHUNT_VOL_CONFIRM watch on (ticker, side) for the next
    10 minutes.

    Called from the hard-eject path right after the close fires. `side`
    is 'long' or 'short' \u2014 the side we just exited; the re-hunt looks
    for DI on that same side to still be >25, i.e. the regime hasn't
    flipped, so a vol confirmation is meaningful.
    """
    try:
        side_key = (side or "").lower()
        if side_key not in ("long", "short"):
            logger.warning(
                "[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] arm: unknown side=%r "
                "ticker=%s", side, ticker,
            )
            return
        _v519_rehunt_watch[(ticker, side_key)] = {
            "side": side_key,
            "exit_ts_utc": exit_ts_utc,
            "fired": False,
        }
    except Exception as e:
        logger.warning("[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] arm error %s: %s",
                       ticker, e)


def _v519_check_rehunt(ticker: str) -> None:
    """If `ticker` has any active re-hunt watches, evaluate each
    (ticker, side) arm independently for the current minute. Emit one
    [V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] line on the FIRST qualifying
    bar within the 10-minute window per side. After that the arm is
    consumed (fired=True); after the window elapses, the arm is
    dropped.

    v5.2.1 M4: keyed on (ticker, side) so long+short whipsaws on the
    same minute don't clobber each other.

    Pure observation. Failure-tolerant.
    """
    if not VOLUME_PROFILE_ENABLED:
        return
    try:
        # Snapshot all keys for this ticker so we can mutate the dict
        # while iterating (drop expired arms).
        keys = [k for k in list(_v519_rehunt_watch.keys()) if k[0] == ticker]
        for key in keys:
            _v519_check_rehunt_arm(ticker, key)
    except Exception as e:
        logger.warning("[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] check error %s: %s",
                       ticker, e)


def _v519_check_rehunt_arm(ticker: str, key: tuple[str, str]) -> None:
    try:
        watch = _v519_rehunt_watch.get(key)
        if not watch:
            return

        now_utc = datetime.now(tz=timezone.utc)
        exit_ts = watch.get("exit_ts_utc")
        if exit_ts is None:
            _v519_rehunt_watch.pop(key, None)
            return
        offset_sec = (now_utc - exit_ts).total_seconds()
        offset_min = int(offset_sec // 60) + 1  # bar-1 == "+1m" in spec
        if offset_sec < 0 or offset_min > _V519_REHUNT_WINDOW_MIN:
            _v519_rehunt_watch.pop(key, None)
            return
        if watch.get("fired"):
            return

        side = watch.get("side") or ""
        di_p, di_m = tiger_di(ticker)
        di_val = di_p if side == "long" else di_m
        if di_val is None or float(di_val) <= _V519_REHUNT_DI_MIN:
            return  # DI side weakened \u2014 don't shadow-fire this minute

        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
        # v5.5.6: read the just-closed minute, not the still-forming one.
        bucket = volume_profile.previous_session_bucket(now_et)
        if bucket is None:
            return
        prof = _volume_profile_cache.get(ticker)
        if prof is None:
            return
        med = volume_profile._bucket_median(prof, bucket)
        if not med:
            return
        cur_v = 0
        if _ws_consumer is not None:
            cur_v = _ws_consumer.current_volume(ticker, bucket) or 0
        vol_pct = int(round((cur_v / med) * 100.0))
        if vol_pct < _V519_REHUNT_VOL_PCT:
            return

        # Shadow re-entry price: latest close on the live 1m bar list.
        shadow_entry_price = None
        try:
            bars = fetch_1min_bars(ticker)
            if bars:
                closes = bars.get("closes") or []
                for c in reversed(closes):
                    if c is not None:
                        shadow_entry_price = float(c)
                        break
                if shadow_entry_price is None:
                    shadow_entry_price = bars.get("current_price")
        except Exception:
            pass

        watch["fired"] = True
        try:
            exit_ts_iso = exit_ts.isoformat()
        except Exception:
            exit_ts_iso = "null"
        logger.info(
            "[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] ticker=%s side=%s "
            "exit_ts=%s rehunt_offset_min=%d vol_pct=%s "
            "di_plus=%s di_minus=%s shadow_entry_price=%s",
            ticker, side, exit_ts_iso, offset_min, _fmt_num(vol_pct),
            _fmt_num(di_p), _fmt_num(di_m),
            _fmt_num(shadow_entry_price),
        )
        # v5.2.0 \u2014 open virtual position for REHUNT_VOL_CONFIRM.
        if shadow_entry_price:
            _v520_open_shadow(
                "REHUNT_VOL_CONFIRM", ticker, side,
                float(shadow_entry_price),
            )
    except Exception as e:
        logger.warning("[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] eval error %s: %s",
                       ticker, e)


# OOMPH_ALERT prev-minute qualified state: ticker -> dict
# {minute_bucket, side, di, vol_pct} when minute 1 passed; else absent.
_v519_oomph_prev: dict[str, dict] = {}

# DI threshold for OOMPH_ALERT \u2014 same 25 surface as the rest of v5.
_V519_OOMPH_DI_MIN = 25.0
# BUCKET_FILL threshold for OOMPH_ALERT minute 1.
_V519_OOMPH_VOL_PCT = 100


def _v519_check_oomph(ticker: str, bars: dict | None = None) -> None:
    """Per-minute OOMPH_ALERT check.

    Each call represents one minute of evaluation. For each side (long /
    short) we determine whether the current minute is "qualified":

      - long-qualified  : DI+ > 25 AND vol_pct >= 100
      - short-qualified : DI- > 25 AND vol_pct >= 100

    If on this call EITHER side was qualified at the PREVIOUS minute and
    the current-minute DI on that same side is still > 25, we emit one
      [V510-SHADOW][CFG=OOMPH_ALERT]
    line. Then we update the prev-minute state with whichever side(s)
    qualify on the current minute.

    Pure observation. Failure-tolerant.
    """
    if not VOLUME_PROFILE_ENABLED:
        return
    try:
        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
        # v5.5.6: read the just-closed minute, not the still-forming one.
        bucket = volume_profile.previous_session_bucket(now_et)
        if bucket is None:
            return
        di_p, di_m = tiger_di(ticker)

        # vol_pct against bucket median (ticker-only, mirrors BUCKET_FILL_100).
        vol_pct = None
        prof = _volume_profile_cache.get(ticker)
        if prof is not None:
            med = volume_profile._bucket_median(prof, bucket)
            if med:
                cur_v = 0
                if _ws_consumer is not None:
                    cur_v = _ws_consumer.current_volume(ticker, bucket) or 0
                vol_pct = int(round((cur_v / med) * 100.0))

        # Check minute-2 confirmation against prev-minute state.
        prev = _v519_oomph_prev.get(ticker)
        if prev is not None and prev.get("minute_bucket") != bucket:
            side = prev.get("side") or ""
            di_now_val = di_p if side == "long" else di_m
            if (di_now_val is not None
                    and float(di_now_val) > _V519_OOMPH_DI_MIN):
                shadow_entry_price = None
                try:
                    use_bars = bars
                    if use_bars is None:
                        use_bars = fetch_1min_bars(ticker)
                    if use_bars:
                        closes = use_bars.get("closes") or []
                        for c in reversed(closes):
                            if c is not None:
                                shadow_entry_price = float(c)
                                break
                        if shadow_entry_price is None:
                            shadow_entry_price = use_bars.get("current_price")
                except Exception:
                    pass
                logger.info(
                    "[V510-SHADOW][CFG=OOMPH_ALERT] ticker=%s side=%s "
                    "minute1_ts=%s minute1_di=%s minute1_vol_pct=%s "
                    "minute2_ts=%s minute2_di=%s shadow_entry_price=%s",
                    ticker, side,
                    prev.get("minute_bucket") or "null",
                    _fmt_num(prev.get("di")),
                    _fmt_num(prev.get("vol_pct")),
                    bucket,
                    _fmt_num(di_now_val),
                    _fmt_num(shadow_entry_price),
                )
                # v5.2.0 \u2014 open virtual position for OOMPH_ALERT.
                if shadow_entry_price:
                    _v520_open_shadow(
                        "OOMPH_ALERT", ticker, side,
                        float(shadow_entry_price),
                    )

        # Update prev-minute state: was this minute itself a qualifier?
        long_qual = (di_p is not None
                     and float(di_p) > _V519_OOMPH_DI_MIN
                     and vol_pct is not None
                     and vol_pct >= _V519_OOMPH_VOL_PCT)
        short_qual = (di_m is not None
                      and float(di_m) > _V519_OOMPH_DI_MIN
                      and vol_pct is not None
                      and vol_pct >= _V519_OOMPH_VOL_PCT)

        if long_qual:
            _v519_oomph_prev[ticker] = {
                "minute_bucket": bucket, "side": "long",
                "di": di_p, "vol_pct": vol_pct,
            }
        elif short_qual:
            _v519_oomph_prev[ticker] = {
                "minute_bucket": bucket, "side": "short",
                "di": di_m, "vol_pct": vol_pct,
            }
        else:
            # Don't carry stale qualification past a non-qualifying minute.
            _v519_oomph_prev.pop(ticker, None)
    except Exception as e:
        logger.warning("[V510-SHADOW][CFG=OOMPH_ALERT] eval error %s: %s",
                       ticker, e)


# ---------------------------------------------------------------------------
# v5.1.2 \u2014 forensic capture emitters.
#
# These emit greppable log lines so post-hoc backtests can replay any
# "what if the threshold/indicator were different" scenario without a
# redeploy. None of these change the trading decision; they are pure
# observation layers.
# ---------------------------------------------------------------------------

# Enumerated reasons used by [V510-CAND]. Kept as constants so the
# smoke tests can assert the surface area is fixed.
CAND_REASON_NO_BREAKOUT = "NO_BREAKOUT"
CAND_REASON_STAGE_NOT_READY = "STAGE_NOT_READY"
CAND_REASON_ALREADY_OPEN = "ALREADY_OPEN"
CAND_REASON_COOL_DOWN = "COOL_DOWN"
CAND_REASON_MAX_POSITIONS = "MAX_POSITIONS"
CAND_REASON_BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"

CAND_REASONS = (
    CAND_REASON_NO_BREAKOUT,
    CAND_REASON_STAGE_NOT_READY,
    CAND_REASON_ALREADY_OPEN,
    CAND_REASON_COOL_DOWN,
    CAND_REASON_MAX_POSITIONS,
    CAND_REASON_BREAKOUT_CONFIRMED,
)


def _fmt_num(v) -> str:
    """Render a number for log lines. None \u2192 'null' so logs are
    machine-parseable; ints stay ints; floats keep 4dp."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    try:
        return ("%.4f" % float(v)).rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        return "null"


def _v512_log_minute(
    ticker: str,
    bucket: str | None,
    t_pct,
    qqq_pct,
    close,
    vol,
) -> None:
    """Emit one [V510-MINUTE] line per ticker per minute close.

    All numerics may be None \u2014 they render as 'null'. Failure-
    tolerant: never raises.
    """
    try:
        logger.info(
            "[V510-MINUTE] ticker=%s bucket=%s t_pct=%s qqq_pct=%s close=%s vol=%s",
            ticker, bucket if bucket is not None else "null",
            _fmt_num(t_pct), _fmt_num(qqq_pct),
            _fmt_num(close), _fmt_num(vol),
        )
    except Exception as e:
        logger.warning("[V510-MINUTE] emit error %s: %s", ticker, e)


# v5.1.6 \u2014 intraminute velocity capture state.
#
# Tracks the FIRST second-mark within each (ticker, minute) where running
# IEX volume crossed 100% of the bucket median. Emitted once per minute
# per ticker; reset on minute boundary. Pure observation \u2014 used only by
# the [V510-VEL] log line. No trading-path effect.
_v516_vel_state: dict[str, tuple[str, int]] = {}


def _v516_log_velocity(
    ticker: str,
    minute_bucket: str | None,
    second: int,
    running_vol,
    bucket_size,
    pct,
    qqq_pct,
) -> None:
    """Emit one [V510-VEL] line on the FIRST tick where running volume
    crosses 100% of bucket median for the active candle. Failure-tolerant.
    """
    try:
        logger.info(
            "[V510-VEL] ticker=%s minute=%s second=%d running_vol=%s "
            "bucket=%s pct=%s qqq_pct=%s",
            ticker, minute_bucket if minute_bucket is not None else "null",
            int(second), _fmt_num(running_vol), _fmt_num(bucket_size),
            _fmt_num(pct), _fmt_num(qqq_pct),
        )
    except Exception as e:
        logger.warning("[V510-VEL] emit error %s: %s", ticker, e)


def _v516_check_velocity(
    ticker: str,
    minute_bucket: str | None,
    now_et,
    running_vol,
    bucket_size,
    qqq_pct=None,
) -> None:
    """If running_vol >= bucket_size and we have not yet logged a
    [V510-VEL] for this (ticker, minute), log one. Computes the second-
    mark from `now_et` minute-boundary. Stateful but failure-tolerant.
    """
    try:
        if minute_bucket is None or bucket_size in (None, 0):
            return
        rv = float(running_vol or 0)
        bs = float(bucket_size)
        if bs <= 0.0 or rv < bs:
            return
        # Already logged for this (ticker, minute)?
        prev = _v516_vel_state.get(ticker)
        if prev is not None and prev[0] == minute_bucket:
            return
        try:
            second = int(now_et.second)
        except Exception:
            second = 0
        pct = round((rv / bs) * 100.0, 2)
        _v516_vel_state[ticker] = (minute_bucket, second)
        _v516_log_velocity(
            ticker, minute_bucket, second,
            running_vol, bucket_size, pct, qqq_pct,
        )
    except Exception as e:
        logger.warning("[V510-VEL] check error %s: %s", ticker, e)


def _v516_log_index(
    spy_close,
    spy_pdc,
    qqq_close,
    qqq_pdc,
) -> None:
    """Emit one [V510-IDX] line per candidate consideration. Captures
    SPY+QQQ close vs prior-day close so post-hoc analysis can validate
    the L-P1 / S-P1 index-direction leg of the Bucket-Fill Protocol.
    """
    try:
        sa = "null"
        qa = "null"
        try:
            if spy_close is not None and spy_pdc is not None:
                sa = "Y" if float(spy_close) > float(spy_pdc) else "N"
        except (TypeError, ValueError):
            sa = "null"
        try:
            if qqq_close is not None and qqq_pdc is not None:
                qa = "Y" if float(qqq_close) > float(qqq_pdc) else "N"
        except (TypeError, ValueError):
            qa = "null"
        logger.info(
            "[V510-IDX] spy_close=%s spy_pdc=%s spy_above=%s "
            "qqq_close=%s qqq_pdc=%s qqq_above=%s",
            _fmt_num(spy_close), _fmt_num(spy_pdc), sa,
            _fmt_num(qqq_close), _fmt_num(qqq_pdc), qa,
        )
    except Exception as e:
        logger.warning("[V510-IDX] emit error: %s", e)


def _v516_log_di(
    ticker: str,
    di_plus_prev,
    di_plus_now,
    di_minus_prev,
    di_minus_now,
    *,
    di_threshold: float = 25.0,
) -> None:
    """Emit one [V510-DI] line per candidate. `double_tap_long` is Y iff
    DI+ at t-1 and t are BOTH > threshold; `double_tap_short` mirrors on
    DI-. Required for L-P2 / S-P2 validation in shadow.
    """
    try:
        def _tap(prev, now):
            try:
                if prev is None or now is None:
                    return "null"
                return "Y" if (float(prev) > di_threshold and
                               float(now) > di_threshold) else "N"
            except (TypeError, ValueError):
                return "null"
        logger.info(
            "[V510-DI] ticker=%s di_plus_t-1=%s di_plus_t=%s "
            "di_minus_t-1=%s di_minus_t=%s "
            "double_tap_long=%s double_tap_short=%s",
            ticker, _fmt_num(di_plus_prev), _fmt_num(di_plus_now),
            _fmt_num(di_minus_prev), _fmt_num(di_minus_now),
            _tap(di_plus_prev, di_plus_now),
            _tap(di_minus_prev, di_minus_now),
        )
    except Exception as e:
        logger.warning("[V510-DI] emit error %s: %s", ticker, e)


def _v512_log_candidate(
    ticker: str,
    bucket: str | None,
    stage: int,
    fsm_state: str,
    entered: bool,
    reason: str,
    *,
    t_pct=None,
    qqq_pct=None,
    close=None,
    stop=None,
    rsi14_=None,
    ema9_=None,
    ema21_=None,
    atr14_=None,
    vwap_dist_pct_=None,
    spread_bps_=None,
) -> None:
    """Emit one [V510-CAND] line per entry consideration.

    Fires on EVERY consideration \u2014 fired AND not-fired \u2014 so the
    asymmetric blind spot from v5.1.1 is closed. Indicator fields can
    be None and will render as 'null'.
    """
    try:
        logger.info(
            "[V510-CAND] ticker=%s bucket=%s stage=%d fsm_state=%s "
            "entered=%s reason=%s t_pct=%s qqq_pct=%s close=%s stop=%s "
            "rsi14=%s ema9=%s ema21=%s atr14=%s "
            "vwap_dist_pct=%s spread_bps=%s",
            ticker, bucket if bucket is not None else "null", int(stage),
            fsm_state, "YES" if entered else "NO", reason,
            _fmt_num(t_pct), _fmt_num(qqq_pct),
            _fmt_num(close), _fmt_num(stop),
            _fmt_num(rsi14_), _fmt_num(ema9_), _fmt_num(ema21_),
            _fmt_num(atr14_), _fmt_num(vwap_dist_pct_), _fmt_num(spread_bps_),
        )
    except Exception as e:
        logger.warning("[V510-CAND] emit error %s: %s", ticker, e)


def _v512_log_fsm_transition(
    ticker: str,
    from_state: str,
    to_state: str,
    reason: str,
    bucket: str | None = None,
) -> None:
    """Emit [V510-FSM] only on actual transitions. No-ops (from==to)
    must NOT log \u2014 the test asserts this."""
    try:
        if from_state == to_state:
            return
        logger.info(
            "[V510-FSM] ticker=%s from=%s to=%s reason=%s bucket=%s",
            ticker, from_state, to_state, reason,
            bucket if bucket is not None else "null",
        )
    except Exception as e:
        logger.warning("[V510-FSM] emit error %s: %s", ticker, e)


def _v512_log_entry_extension(
    ticker: str,
    *,
    bid=None,
    ask=None,
    cash=None,
    equity=None,
    open_positions=None,
    total_exposure_pct=None,
    current_drawdown_pct=None,
) -> None:
    """Emit [V510-ENTRY] alongside the existing entry log line. Carries
    bid/ask + account state so post-hoc analysis has the snapshot
    without re-reading the broker.
    """
    try:
        logger.info(
            "[V510-ENTRY] ticker=%s bid=%s ask=%s cash=%s equity=%s "
            "open_positions=%s total_exposure_pct=%s current_drawdown_pct=%s",
            ticker, _fmt_num(bid), _fmt_num(ask),
            _fmt_num(cash), _fmt_num(equity),
            _fmt_num(open_positions),
            _fmt_num(total_exposure_pct),
            _fmt_num(current_drawdown_pct),
        )
    except Exception as e:
        logger.warning("[V510-ENTRY] emit error %s: %s", ticker, e)


def _v512_emit_candidate_log(
    ticker: str,
    *,
    stage: int = 1,
    entered: bool = False,
    bars: dict | None = None,
) -> None:
    """Compose the [V510-CAND] line for one entry-consideration moment.

    Pulls bucket + t_pct/qqq_pct from the websocket consumer + profile
    cache (best-effort), and indicators from the locally-cached 1m bar
    history when present. All inputs are optional \u2014 anything we
    can't compute is None and renders as 'null'.
    """
    try:
        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
        # v5.5.6: shadow path \u2014 read the just-closed minute, not the
        # still-forming one (avoids the IEX WS bar close-out race).
        bucket = volume_profile.previous_session_bucket(now_et)
    except Exception:
        bucket = None
    t_pct = None
    qqq_pct = None
    try:
        if bucket is not None and _ws_consumer is not None and VOLUME_PROFILE_ENABLED:
            idx_symbol = volume_profile.load_active_config().get("index_symbol", "QQQ")
            cur_v = _ws_consumer.current_volume(ticker, bucket) or 0
            cur_q = _ws_consumer.current_volume(idx_symbol, bucket) or 0
            tp = _volume_profile_cache.get(ticker)
            qp = _volume_profile_cache.get(idx_symbol)
            if tp is not None:
                med = volume_profile._bucket_median(tp, bucket)
                if med:
                    t_pct = int(round((cur_v / med) * 100.0))
            if qp is not None:
                med = volume_profile._bucket_median(qp, bucket)
                if med:
                    qqq_pct = int(round((cur_q / med) * 100.0))
    except Exception:
        pass

    close_v = None
    stop_v = None
    rsi_v = ema9_v = ema21_v = atr_v = vwap_v = spread_v = None
    try:
        if bars and isinstance(bars, dict):
            close_v = bars.get("current_price")
            stop_v = bars.get("stop")
    except Exception:
        pass

    reason = (CAND_REASON_BREAKOUT_CONFIRMED if entered
              else CAND_REASON_NO_BREAKOUT)
    fsm_state = "ARMED" if entered else "OBSERVE"

    _v512_log_candidate(
        ticker, bucket, stage, fsm_state, entered, reason,
        t_pct=t_pct, qqq_pct=qqq_pct,
        close=close_v, stop=stop_v,
        rsi14_=rsi_v, ema9_=ema9_v, ema21_=ema21_v,
        atr14_=atr_v, vwap_dist_pct_=vwap_v, spread_bps_=spread_v,
    )

    # v5.1.6: full L-P1/S-P1 index validation per candidate.
    try:
        spy_close_v = None
        qqq_close_v = None
        try:
            spy_bars_l = fetch_1min_bars("SPY")
            if spy_bars_l:
                closes = spy_bars_l.get("closes") or []
                # last non-None close
                for c in reversed(closes):
                    if c is not None:
                        spy_close_v = float(c)
                        break
                if spy_close_v is None:
                    spy_close_v = spy_bars_l.get("current_price") or None
        except Exception:
            pass
        try:
            qqq_bars_l = fetch_1min_bars("QQQ")
            if qqq_bars_l:
                closes = qqq_bars_l.get("closes") or []
                for c in reversed(closes):
                    if c is not None:
                        qqq_close_v = float(c)
                        break
                if qqq_close_v is None:
                    qqq_close_v = qqq_bars_l.get("current_price") or None
        except Exception:
            pass
        _v516_log_index(
            spy_close_v, pdc.get("SPY"),
            qqq_close_v, pdc.get("QQQ"),
        )
    except Exception as e:
        logger.warning("[V510-IDX] hook error %s: %s", ticker, e)

    # v5.1.6: L-P2/S-P2 "double-tap" DI+/DI- validation per candidate.
    try:
        import indicators as _ind
        bar_list_full: list[dict] = []
        if bars and isinstance(bars, dict):
            highs = bars.get("highs") or []
            lows = bars.get("lows") or []
            closes_l = bars.get("closes") or []
            n = min(len(highs), len(lows), len(closes_l))
            for i in range(n):
                h = highs[i]; l = lows[i]; c = closes_l[i]
                if h is None or l is None or c is None:
                    continue
                bar_list_full.append({"high": float(h), "low": float(l),
                                       "close": float(c)})
        di_plus_now = _ind.di_plus(bar_list_full) if bar_list_full else None
        di_minus_now = _ind.di_minus(bar_list_full) if bar_list_full else None
        di_plus_prev = (_ind.di_plus(bar_list_full[:-1])
                        if len(bar_list_full) >= 2 else None)
        di_minus_prev = (_ind.di_minus(bar_list_full[:-1])
                         if len(bar_list_full) >= 2 else None)
        _v516_log_di(
            ticker, di_plus_prev, di_plus_now,
            di_minus_prev, di_minus_now,
        )
    except Exception as e:
        logger.warning("[V510-DI] hook error %s: %s", ticker, e)


def _v512_quote_snapshot(ticker: str):
    """Return (bid, ask) for `ticker`, or (None, None) on failure. The
    Alpaca data client is not always reachable from tests, so we treat
    any exception as "no quote available"."""
    try:
        client = _historical_data_client() if "_historical_data_client" in globals() else None
        if client is None:
            return (None, None)
        from alpaca.data.requests import StockLatestQuoteRequest  # type: ignore
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        q = client.get_stock_latest_quote(req)
        rec = q.get(ticker) if isinstance(q, dict) else None
        if rec is None:
            return (None, None)
        bid = getattr(rec, "bid_price", None)
        ask = getattr(rec, "ask_price", None)
        return (bid, ask)
    except Exception:
        return (None, None)


def _v512_archive_minute_bar(ticker: str, bar: dict) -> None:
    """Persist a 1m bar to /data/bars/YYYY-MM-DD/{TICKER}.jsonl.

    Failure-tolerant. Respects the 30-symbol IEX cap and the active
    TICKERS list (skips persistence for anything outside it). Caller
    is expected to invoke this once per minute close per ticker.
    """
    try:
        sym = (ticker or "").strip().upper()
        if not sym:
            return
        # Stay within the same caps as v5.1.0 so we never persist for
        # symbols we are not actively tracking.
        try:
            if sym not in TICKERS and sym not in ("QQQ", "SPY"):
                return
            if len(TICKERS) > volume_profile.WS_SYMBOL_CAP_FREE_IEX:
                return
        except Exception:
            pass
        bar_archive.write_bar(sym, bar)
    except Exception as e:
        logger.warning("[V510-BAR] archive error %s: %s", ticker, e)


def _normalise_ticker(sym) -> str:
    """Uppercase + strip the common '$' / whitespace noise.
    Returns '' for anything that doesn't pass the symbol regex."""
    if not sym:
        return ""
    s = str(sym).strip().lstrip("$").upper()
    return s if TICKER_SYM_RE.match(s) else ""


def _rebuild_trade_tickers() -> None:
    """Sync TRADE_TICKERS with TICKERS — in place.
    Must run after every mutation of TICKERS so the scan loop,
    RSI regime classifier, and dashboard snapshot see the same
    tradable set.
    """
    TRADE_TICKERS.clear()
    for t in TICKERS:
        if t not in TICKERS_PINNED:
            TRADE_TICKERS.append(t)


def _load_tickers_file() -> list:
    """Read TICKERS_FILE and return a normalised, de-duplicated,
    order-preserving list. Fail-soft — any error returns [].
    """
    try:
        if not os.path.exists(TICKERS_FILE):
            return []
        with open(TICKERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        items = raw.get("tickers") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        seen, out = set(), []
        for sym in items:
            s = _normalise_ticker(sym)
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out
    except Exception as e:
        logger.warning("tickers.json load failed, using defaults: %s", e)
        return []


def _save_tickers_file() -> bool:
    """Atomically persist the current TICKERS list. Returns True on
    success. Uses a tmp+rename so a crash mid-write can never leave
    a half-written file.
    """
    try:
        payload = {
            "tickers": list(TICKERS),
            "updated_utc": _utc_now_iso(),
            "bot_version": BOT_VERSION,
        }
        tmp = TICKERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=False)
        os.replace(tmp, TICKERS_FILE)
        return True
    except Exception as e:
        logger.error("tickers.json save failed: %s", e)
        return False


def _init_tickers() -> None:
    """Populate TICKERS from disk on startup; fall back to defaults
    (which include QBTS and the pinned SPY/QQQ). Always ensures the
    pinned symbols are present no matter what was on disk.
    """
    from_disk = _load_tickers_file()
    base = from_disk if from_disk else list(TICKERS_DEFAULT)
    # Ensure pinned symbols are always in the set.
    for p in TICKERS_PINNED:
        if p not in base:
            base.append(p)
    # Cap at TICKERS_MAX just in case a hand-edited file went wild.
    base = base[:TICKERS_MAX]
    TICKERS.clear()
    TICKERS.extend(base)
    _rebuild_trade_tickers()
    # If the file didn't exist or was empty, persist the seeded
    # defaults so the on-disk list matches memory immediately.
    if not from_disk:
        _save_tickers_file()
    logger.info("Ticker universe loaded: %d tickers (%s)",
                len(TICKERS), ", ".join(TICKERS))


def _fill_metrics_for_ticker(ticker: str) -> dict:
    """Populate every metric a newly-added ticker needs so the very
    next scan cycle can evaluate it without cold-starting any data.

    v3.4.33: thorough fill — primes PDC (dual source), OR high/low
    (post-09:35 ET), a warm-up RSI snapshot, and a liveness probe on
    1-minute bars. Returns a dict describing what was filled; the
    caller uses this to tell the user exactly what is ready and what
    is still pending.

    Keys in the returned dict:
      bars    : bool  — 1-minute bars are reachable for this symbol
      pdc     : bool  — previous-day close cached in pdc[ticker]
      pdc_src : str   — 'fmp' | 'bars' | 'none'
      or      : bool  — opening range populated (high and low)
      or_pending : bool — we're pre-09:35 ET; collect_or() will fill
      rsi     : bool  — RSI warm-up value computed (not cached, just
                        proves the bar history is long enough)
      rsi_val : float | None — the warm-up value, for display only
      errors  : list[str]    — human-readable problems, truncated
                               to short phrases by the caller
    """
    filled = {
        "bars": False,
        "pdc": False, "pdc_src": "none",
        "or": False, "or_pending": False,
        "rsi": False, "rsi_val": None,
        "errors": [],
    }
    now_et = _now_et()
    or_window_end = now_et.replace(hour=9, minute=35,
                                   second=0, microsecond=0)
    past_or_window = now_et >= or_window_end

    # 1) PDC via FMP quote — works any time of day, including pre-open.
    try:
        q = get_fmp_quote(ticker)
        if q and q.get("previousClose"):
            pdc[ticker] = float(q["previousClose"])
            filled["pdc"] = True
            filled["pdc_src"] = "fmp"
        else:
            filled["errors"].append("no PDC from FMP")
    except Exception as e:
        filled["errors"].append("FMP error: %s" % str(e)[:40])
        logger.warning("fill_metrics FMP %s failed: %s", ticker, e)

    # 2) Bars liveness probe + OR fill (if past 09:35) + RSI warm-up
    #    + PDC fallback (if FMP missed it). All three piggy-back on
    #    the same fetch so we only hit the data provider once.
    try:
        bars = fetch_1min_bars(ticker)
        if bars and bars.get("timestamps"):
            filled["bars"] = True

            # PDC fallback from bars snapshot.
            if not filled["pdc"] and bars.get("pdc"):
                pdc[ticker] = float(bars["pdc"])
                filled["pdc"] = True
                filled["pdc_src"] = "bars"

            # OR fill — only if we're past 09:35 ET.
            if past_or_window:
                open_ts = int(or_window_end.replace(hour=9, minute=30)
                              .timestamp())
                end_ts = int(or_window_end.timestamp())
                max_hi, min_lo = None, None
                for i, ts in enumerate(bars["timestamps"]):
                    if open_ts <= ts < end_ts:
                        h = bars["highs"][i] or bars["closes"][i]
                        lo = bars["lows"][i] or bars["closes"][i]
                        if h is not None:
                            max_hi = h if max_hi is None else max(max_hi, h)
                        if lo is not None:
                            min_lo = lo if min_lo is None else min(min_lo, lo)
                if max_hi is not None and min_lo is not None:
                    or_high[ticker] = max_hi
                    or_low[ticker] = min_lo
                    filled["or"] = True
                elif max_hi is not None:
                    or_high[ticker] = max_hi
                    filled["errors"].append("OR low missing")
                else:
                    filled["errors"].append(
                        "no bars in 09:30\u201309:35")
            else:
                # Pre-09:35 is not an error — explicitly flag pending.
                filled["or_pending"] = True

            # RSI warm-up — compute from available closes. This doesn't
            # cache anything (the scanner recomputes each cycle from
            # live bars), but it proves the bar history is deep enough
            # for _compute_rsi to return a real number on the next scan.
            closes = [c for c in (bars.get("closes") or []) if c is not None]
            if len(closes) >= RSI_PERIOD + 1:
                try:
                    rsi_val = _compute_rsi(closes)
                    if rsi_val is not None:
                        filled["rsi"] = True
                        filled["rsi_val"] = float(rsi_val)
                except Exception as e:
                    filled["errors"].append(
                        "RSI warm-up: %s" % str(e)[:30])
            else:
                filled["errors"].append(
                    "RSI needs %d closes" % (RSI_PERIOD + 1))
        else:
            filled["errors"].append("no 1m bars")
    except Exception as e:
        filled["errors"].append("bars error: %s" % str(e)[:40])
        logger.warning("fill_metrics bars %s failed: %s", ticker, e)

    return filled


def add_ticker(sym: str) -> dict:
    """Add a ticker to the live universe. Idempotent.

    Returns {ok, ticker, added, reason, metrics} where:
      - ok=False + reason=...   on validation failure
      - ok=True + added=False   if already present (no-op)
      - ok=True + added=True    on a fresh add (file saved, metrics filled)
    """
    t = _normalise_ticker(sym)
    if not t:
        return {"ok": False, "reason": "invalid symbol", "ticker": sym}
    if t in TICKERS:
        return {"ok": True, "added": False, "ticker": t,
                "reason": "already tracked"}
    if len(TICKERS) >= TICKERS_MAX:
        return {"ok": False, "ticker": t,
                "reason": "at max (%d) \u2014 remove one first" % TICKERS_MAX}
    TICKERS.append(t)
    _rebuild_trade_tickers()
    _save_tickers_file()
    metrics = _fill_metrics_for_ticker(t)
    logger.info("ticker added: %s (pdc=%s or=%s)",
                t, metrics["pdc"], metrics["or"])
    # v5.6.1 D6 \u2014 [WATCHLIST_ADD] hook for replay universe-reconstruction.
    try:
        _v561_log_watchlist_add(t, reason="manual")
    except Exception:
        pass
    return {"ok": True, "added": True, "ticker": t, "metrics": metrics}


def remove_ticker(sym: str) -> dict:
    """Remove a ticker from the live universe. Idempotent.

    Pinned tickers (SPY, QQQ) are always refused.
    Open positions on the removed ticker keep managing until they
    close — this only stops new entries from being opened.
    """
    t = _normalise_ticker(sym)
    if not t:
        return {"ok": False, "reason": "invalid symbol", "ticker": sym}
    if t in TICKERS_PINNED:
        return {"ok": False, "ticker": t,
                "reason": "%s is pinned (regime anchor)" % t}
    if t not in TICKERS:
        return {"ok": True, "removed": False, "ticker": t,
                "reason": "not tracked"}
    TICKERS.remove(t)
    _rebuild_trade_tickers()
    _save_tickers_file()
    # Leave or_high/or_low/pdc entries behind — any still-open
    # position on this ticker relies on them to manage exits.
    logger.info("ticker removed: %s", t)
    # v5.6.1 D6 \u2014 [WATCHLIST_REMOVE] hook for replay reconstruction.
    try:
        _v561_log_watchlist_remove(t, reason="manual")
    except Exception:
        pass
    open_long = t in positions
    open_short = t in short_positions
    return {"ok": True, "removed": True, "ticker": t,
            "had_open": bool(open_long or open_short)}

# v3.4.45 — paper sizing is now dollar-based like RH. SHARES is kept
# as a legacy fallback only (used when price is unavailable in test
# paths). Production entries call paper_shares_for(price) instead.
SHARES         = 10
PAPER_DOLLARS_PER_ENTRY = float(os.getenv("PAPER_DOLLARS_PER_ENTRY", "10000"))
STOP_OFFSET    = 0.50    # Initial stop: entry - $0.50
# Trail: +1.0% trigger, max(price*1.0%, $1.00) distance — see manage_positions()
TRAIL_TRIGGER  = 1.00    # Legacy constant (unused — trail is now percentage-based)
TRAIL_STEP     = 0.50    # Legacy constant (unused — trail is now percentage-based)

SCAN_INTERVAL  = 60      # seconds between scans
YAHOO_TIMEOUT  = 8       # seconds
YAHOO_HEADERS  = {"User-Agent": "Mozilla/5.0"}

# v3.4.47 — Eye of the Tiger 2.0 protocol configuration
TIGER_V2_DI_THRESHOLD = float(os.getenv("TIGER_V2_DI_THRESHOLD", "25"))
TIGER_V2_REQUIRE_VOL  = os.getenv("TIGER_V2_REQUIRE_VOL", "false").lower() in ("1", "true", "yes")

# ============================================================
# GLOBAL STATE
# ============================================================

# OR data — populated at 09:35 ET
or_high: dict = {}                  # ticker -> OR high price
or_low: dict = {}                   # ticker -> OR low price (Wounded Buffalo)
pdc: dict = {}                      # ticker -> previous day close
or_collected_date: str = ""         # date string, prevents re-collection
# v4.0.3-beta — per-ticker counter of OR staleness SKIPs this session.
# Exposed in /api/state so silent "OR vs live drift" failures are
# visible without tailing Railway logs.
or_stale_skip_count: dict = {}      # ticker -> int

# AVWAP state \u2014 REMOVED in v3.4.34, RESTORED in v5.6.0 with new
# semantics. Session-open anchored AVWAP (anchor at 09:30 ET regular
# session open; reset daily; recomputed on every 1m bar close from the
# bar archive). Used by the v5.6.0 unified permission gates:
#   L-P1: G1 = Index.Last > Index.Opening_AVWAP
#         G3 = Ticker.Last > Ticker.Opening_AVWAP
#   S-P1: mirrored with strict <.
# AVWAP None (no bars yet) -> G1/G3 fail deterministically. Persisted
# state keys ("avwap_data", "avwap_last_ts") from pre-v3.4.34 are still
# silently ignored by load_paper_state for backwards compatibility.
# The v5.6.0 AVWAP is recomputed on the fly from the per-cycle 1m bar
# cache, no persistence required.


def _opening_avwap(ticker: str) -> float | None:
    """Session-open anchored VWAP for ``ticker``.

    Anchors at 09:30 ET regular-session open and includes every closed
    1m bar from then through the most recent close. Returns None if:
      - no bars are available yet (very first 9:30 second), OR
      - cumulative volume is zero across the included bars.

    Strict-pass gate semantics (v5.6.0): callers must treat None as
    a hard FAIL (do not enter on insufficient data).
    """
    bars = fetch_1min_bars(ticker)
    if not bars:
        return None
    timestamps = bars.get("timestamps") or []
    highs = bars.get("highs") or []
    lows = bars.get("lows") or []
    closes = bars.get("closes") or []
    volumes = bars.get("volumes") or []
    if not timestamps:
        return None

    now_et = _now_et()
    session_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    open_epoch = session_open_et.timestamp()

    num = 0.0
    den = 0.0
    n = min(len(timestamps), len(highs), len(lows), len(closes), len(volumes))
    for i in range(n):
        ts = timestamps[i]
        if ts is None or ts < open_epoch:
            continue
        h = highs[i]
        l = lows[i]
        c = closes[i]
        v = volumes[i]
        if h is None or l is None or c is None or v is None or v <= 0:
            continue
        tp = (float(h) + float(l) + float(c)) / 3.0
        num += tp * float(v)
        den += float(v)
    if den <= 0.0:
        return None
    return num / den


def _v560_log_gate(ticker: str, side: str, gate: str, value, threshold, result: bool) -> None:
    """v5.6.0 forensic gate-eval logger. One line per G1/G3/G4 evaluation.

    Saturday's report parses these to validate the unified-AVWAP gate set.
    Format: ``[V560-GATE] ticker=AAPL side=LONG gate=G1 value=425.10 threshold=425.04 result=True``.
    """
    val_s = "None" if value is None else "%.4f" % float(value)
    thr_s = "None" if threshold is None else "%.4f" % float(threshold)
    logger.info(
        "[V560-GATE] ticker=%s side=%s gate=%s value=%s threshold=%s result=%s",
        ticker, side, gate, val_s, thr_s, bool(result),
    )


# ------------------------------------------------------------
# v5.6.1 \u2014 Data-collection helpers (logging + writer extensions).
# Pure observers; do not affect gate logic. See spec
# /home/user/workspace/specs/v5_6_1_data_collection_improvements.md.
# ------------------------------------------------------------
V561_INDEX_TICKER = "QQQ"
V561_OR_DIR_DEFAULT = "/data/or"


def _v561_fmt_num(v) -> str:
    """Render a float/None as a stable token for log lines.

    None -> ``null`` (matches the gate_state JSON null semantics).
    Numbers -> 4dp string with no trailing whitespace.
    """
    if v is None:
        return "null"
    try:
        return "%.4f" % float(v)
    except (TypeError, ValueError):
        return "null"


def _v561_gate_state_dict(
    *,
    g1: bool | None,
    g3: bool | None,
    g4: bool | None,
    pass_: bool | None,
    ticker_price: float | None,
    ticker_avwap: float | None,
    index_price: float | None,
    index_avwap: float | None,
    or_high: float | None,
    or_low: float | None,
) -> dict:
    """Build the canonical gate_state payload used by both [V560-GATE]
    and [SKIP] gate_state= lines. Booleans are coerced; floats kept None
    when unknown so JSON encodes them as null."""
    def _fb(x):
        return None if x is None else bool(x)

    def _ff(x):
        if x is None:
            return None
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    return {
        "g1": _fb(g1),
        "g3": _fb(g3),
        "g4": _fb(g4),
        "pass": _fb(pass_),
        "ticker_price": _ff(ticker_price),
        "ticker_avwap": _ff(ticker_avwap),
        "index_price": _ff(index_price),
        "index_avwap": _ff(index_avwap),
        "or_high": _ff(or_high),
        "or_low": _ff(or_low),
    }


def _v561_log_v560_gate_rich(
    *,
    ticker: str,
    side: str,
    ts_utc: str,
    ticker_price,
    ticker_avwap,
    index_price,
    index_avwap,
    or_high,
    or_low,
    g1: bool,
    g3: bool,
    g4: bool,
    pass_: bool,
    reason: str | None,
) -> None:
    """v5.6.1 \u2014 single richened [V560-GATE] line.

    Carries every field a replay needs to pair a SKIP/PASS with the
    underlying numbers without consulting the bar archive.
    """
    logger.info(
        "[V560-GATE] ticker=%s side=%s ts=%s "
        "ticker_price=%s ticker_avwap=%s "
        "index_price=%s index_avwap=%s "
        "or_high=%s or_low=%s "
        "g1=%s g3=%s g4=%s pass=%s reason=%s",
        ticker, side, ts_utc,
        _v561_fmt_num(ticker_price), _v561_fmt_num(ticker_avwap),
        _v561_fmt_num(index_price), _v561_fmt_num(index_avwap),
        _v561_fmt_num(or_high), _v561_fmt_num(or_low),
        bool(g1), bool(g3), bool(g4), bool(pass_),
        ("null" if reason is None else str(reason)),
    )


def _v561_log_skip(
    *,
    ticker: str,
    reason: str,
    ts_utc: str,
    gate_state: dict | None,
) -> None:
    """v5.6.1 \u2014 unified [SKIP] line with gate_state.

    `gate_state=None` -> emits literal ``gate_state=null`` (used for
    pre-gate skips like cooldown / loss-cap / data-not-ready). When the
    SKIP fires after gates have evaluated, pass the dict from
    `_v561_gate_state_dict`.
    """
    if gate_state is None:
        gs_json = "null"
    else:
        try:
            gs_json = json.dumps(gate_state, separators=(",", ":"),
                                 sort_keys=True)
        except (TypeError, ValueError):
            gs_json = "null"
    logger.info(
        "[SKIP] ticker=%s reason=%s ts=%s gate_state=%s",
        ticker, reason, ts_utc, gs_json,
    )


def _v561_compose_entry_id(ticker: str, entry_ts_utc: str) -> str:
    """Deterministic entry id: ``<TICKER>-<YYYYMMDDHHMMSS>``.

    The compact ts uses the entry_ts_utc as-is, stripping non-digits;
    if entry_ts_utc is missing/unparseable, falls back to the current
    UTC clock so the id is always populated.
    """
    sym = (ticker or "").strip().upper() or "UNK"
    raw = entry_ts_utc or ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 14:
        digits = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    else:
        digits = digits[:14]
    return f"{sym}-{digits}"


def _v561_log_entry(
    *,
    ticker: str,
    side: str,
    entry_id: str,
    entry_ts_utc: str,
    entry_price: float,
    qty: int,
    strike_num: int = 1,
) -> None:
    """v5.6.1 \u2014 [ENTRY] line carrying entry_id for pairing.

    Strictly additive: this is in addition to the legacy
    [V510-ENTRY] line. Replay pairs by entry_id. v5.7.0 adds
    `strike_num` so log readers can count strikes without
    state-replay.
    """
    logger.info(
        "[ENTRY] ticker=%s side=%s entry_id=%s entry_ts=%s "
        "entry_price=%.4f qty=%d strike_num=%d",
        ticker, side, entry_id, entry_ts_utc,
        float(entry_price), int(qty), int(strike_num),
    )


def _v561_log_trade_closed(
    *,
    ticker: str,
    side: str,
    entry_id: str,
    entry_ts_utc: str,
    entry_price: float,
    exit_ts_utc: str,
    exit_price: float,
    exit_reason: str,
    qty: int,
    pnl_dollars: float,
    pnl_pct: float,
    hold_seconds: int,
    strike_num: int = 1,
    daily_realized_pnl: float | None = None,
) -> None:
    """v5.6.1 \u2014 [TRADE_CLOSED] lifecycle line.

    Emitted on every exit (stop, target, time, eod, manual).
    Replay pairs to [ENTRY] via entry_id. v5.7.0 adds
    `strike_num` and the running `daily_realized_pnl` so the
    kill-switch path can be reproduced offline. When
    `daily_realized_pnl` is omitted the helper folds this trade
    into the day's running total via `_v570_record_trade_close`
    so the logged value is always the post-this-close cumulative.
    """
    if daily_realized_pnl is None:
        try:
            daily_realized_pnl = _v570_record_trade_close(pnl_dollars)
        except Exception:
            daily_realized_pnl = float(pnl_dollars or 0.0)
    logger.info(
        "[TRADE_CLOSED] ticker=%s side=%s entry_id=%s "
        "entry_ts=%s entry_price=%.4f "
        "exit_ts=%s exit_price=%.4f exit_reason=%s "
        "qty=%d pnl_dollars=%.4f pnl_pct=%.4f hold_seconds=%d "
        "strike_num=%d daily_realized_pnl=%.4f",
        ticker, side, entry_id,
        entry_ts_utc, float(entry_price),
        exit_ts_utc, float(exit_price), exit_reason,
        int(qty), float(pnl_dollars), float(pnl_pct),
        int(hold_seconds),
        int(strike_num), float(daily_realized_pnl),
    )


def _v561_log_universe(tickers: list | tuple) -> None:
    """v5.6.1 \u2014 boot-time [UNIVERSE] one-shot.

    Tickers are uppercased, deduped, and sorted alphabetically for a
    stable line. Emitted once at module init.
    """
    seen, out = set(), []
    for t in tickers or []:
        s = (t or "").strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    out.sort()
    logger.info("[UNIVERSE] tickers=%s", ",".join(out))


def _v561_log_watchlist_add(ticker: str, reason: str = "manual",
                             ts_utc: str | None = None) -> None:
    """v5.6.1 \u2014 [WATCHLIST_ADD] hook. Currently called manually; the
    static-universe path doesn't mutate at runtime, but the hook is
    wired so future oomph/news-driven adds emit a structured line.
    """
    ts = ts_utc or _utc_now_iso()
    sym = (ticker or "").strip().upper()
    logger.info("[WATCHLIST_ADD] ticker=%s ts=%s reason=%s", sym, ts, reason)


def _v561_log_watchlist_remove(ticker: str, reason: str = "manual",
                                ts_utc: str | None = None) -> None:
    """v5.6.1 \u2014 [WATCHLIST_REMOVE] hook. Mirror of WATCHLIST_ADD."""
    ts = ts_utc or _utc_now_iso()
    sym = (ticker or "").strip().upper()
    logger.info("[WATCHLIST_REMOVE] ticker=%s ts=%s reason=%s", sym, ts, reason)


# ------------------------------------------------------------
# v5.7.0 \u2014 Unlimited Titan Strikes. HOD/LOD-gated unlimited
# re-entries on the Ten Titans only. Strike 1 takes the unchanged
# v5.6.0 L-P1/S-P1 permission gates; Strike 2+ runs the new
# Expansion Gate (HOD/LOD break + IndexAVWAP). Spec:
# /home/user/workspace/specs/v5_7_0_unlimited_titan_strikes.md.
# ------------------------------------------------------------

# Per-ticker per-side per-day strike counter. Reset at session
# start (9:30 ET). Strike N counts how many entries on this side
# have already fired today; strike_num for the next attempt is
# (count + 1).
_v570_strike_counts: dict = {}   # key=(ticker,side) -> int
_v570_strike_date: str = ""

# Per-ticker per-day session HOD/LOD tracker. Seeded from the
# first 9:30 ET print onward. Pre-market values do NOT seed.
_v570_session_hod: dict = {}     # {ticker: float}
_v570_session_lod: dict = {}     # {ticker: float}
_v570_session_date: str = ""

# Daily realized P&L, recomputed cumulatively from [TRADE_CLOSED]
# emissions. Resets at 9:30 ET next session.
_v570_daily_realized_pnl: float = 0.0
_v570_daily_pnl_date: str = ""

# Kill-switch latch. True once realized P&L breaches the floor;
# resets at the next session boundary alongside the strike
# counters.
_v570_kill_switch_latched: bool = False
_v570_kill_switch_logged: bool = False


def _v570_is_titan(ticker: str) -> bool:
    """Return True iff `ticker` is in the Ten Titans universe."""
    return (ticker or "").strip().upper() in TITAN_TICKERS


def _v570_session_today_str() -> str:
    """Today as ET date string \u2014 anchors the daily counters."""
    try:
        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.utcnow()
    return now_et.strftime("%Y-%m-%d")


def _v570_reset_if_new_session() -> None:
    """Reset strike counters / HOD-LOD / daily P&L / kill switch
    when a new ET session begins. Idempotent."""
    global _v570_strike_date, _v570_session_date, _v570_daily_pnl_date
    global _v570_kill_switch_latched, _v570_kill_switch_logged
    global _v570_daily_realized_pnl
    today = _v570_session_today_str()
    if _v570_strike_date != today:
        _v570_strike_counts.clear()
        _v570_strike_date = today
    if _v570_session_date != today:
        _v570_session_hod.clear()
        _v570_session_lod.clear()
        _v570_session_date = today
    if _v570_daily_pnl_date != today:
        _v570_daily_realized_pnl = 0.0
        _v570_daily_pnl_date = today
        _v570_kill_switch_latched = False
        _v570_kill_switch_logged = False


def _v570_strike_count(ticker: str, side: str) -> int:
    """Return the number of entries already filled today on
    (ticker, side). The next attempt is strike_num = count + 1."""
    _v570_reset_if_new_session()
    return int(_v570_strike_counts.get(
        (ticker.upper(), side.upper()), 0))


def _v570_record_entry(ticker: str, side: str) -> int:
    """Increment the strike counter on a successful ENTRY and
    return the strike_num that was just consumed."""
    _v570_reset_if_new_session()
    key = (ticker.upper(), side.upper())
    new_n = int(_v570_strike_counts.get(key, 0)) + 1
    _v570_strike_counts[key] = new_n
    return new_n


def _v570_update_session_hod_lod(
    ticker: str, current_price: float | None,
) -> tuple[float | None, float | None, bool, bool]:
    """Update the per-ticker session HOD/LOD with the current
    print and return ``(prev_hod, prev_lod, hod_break, lod_break)``.

    `prev_hod`/`prev_lod` are the values BEFORE this tick was
    folded in (None if this is the first print of the session).
    `hod_break` is True iff the current price is strictly greater
    than the prior HOD; mirror for `lod_break`. After the call,
    the stored HOD/LOD are updated to include this tick.

    Pre-market behavior: this helper does NOT seed before 9:30 ET
    \u2014 callers gate themselves with `_v570_is_session_open()`.
    """
    _v570_reset_if_new_session()
    sym = (ticker or "").strip().upper()
    if not sym or current_price is None or current_price <= 0:
        return None, None, False, False
    prev_hod = _v570_session_hod.get(sym)
    prev_lod = _v570_session_lod.get(sym)
    px = float(current_price)
    hod_break = (prev_hod is not None and px > prev_hod)
    lod_break = (prev_lod is not None and px < prev_lod)
    if prev_hod is None or px > prev_hod:
        _v570_session_hod[sym] = px
    if prev_lod is None or px < prev_lod:
        _v570_session_lod[sym] = px
    return prev_hod, prev_lod, hod_break, lod_break


def _v570_is_session_open() -> bool:
    """True at/after 9:30 ET on a weekday."""
    try:
        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    except Exception:
        return True
    if now_et.weekday() >= 5:
        return False
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return now_et >= open_t


def _v570_log_strike(
    *,
    ticker: str,
    side: str,
    ts_utc: str,
    strike_num: int,
    is_first: bool,
    hod: float | None,
    lod: float | None,
    hod_break: bool,
    lod_break: bool,
    expansion_gate_pass: bool,
) -> None:
    """v5.7.0 \u2014 emit a single [V570-STRIKE] line for replay.

    Always emitted on every entry-path evaluation. For Strike 1
    `expansion_gate_pass` is reported as False (the field is only
    meaningful on Strike 2+).
    """
    logger.info(
        "[V570-STRIKE] ticker=%s side=%s ts=%s strike_num=%d is_first=%s "
        "hod=%s lod=%s hod_break=%s lod_break=%s "
        "expansion_gate_pass=%s",
        ticker, side, ts_utc, int(strike_num), bool(is_first),
        _v561_fmt_num(hod), _v561_fmt_num(lod),
        bool(hod_break), bool(lod_break),
        bool(expansion_gate_pass),
    )


def _v570_expansion_gate_pass(
    *,
    side: str,
    current_price: float | None,
    prev_hod: float | None,
    prev_lod: float | None,
    index_price: float | None,
    index_avwap: float | None,
) -> bool:
    """v5.7.0 \u2014 Strike 2+ Expansion Gate.

    LONG passes iff price > prior session HOD (strict) AND
    index_price > index_avwap (strict, same as v5.6.0 G1).
    SHORT mirrors with strict ``<``. AVWAP None FAILs.
    """
    if current_price is None or index_price is None or index_avwap is None:
        return False
    if side.upper() == "LONG":
        if prev_hod is None:
            return False
        return (float(current_price) > float(prev_hod)
                and float(index_price) > float(index_avwap))
    if prev_lod is None:
        return False
    return (float(current_price) < float(prev_lod)
            and float(index_price) < float(index_avwap))


def _v570_log_kill_switch(realized_pnl: float, ts_utc: str) -> None:
    """v5.7.0 \u2014 single [KILL_SWITCH] line on first breach."""
    logger.info(
        "[KILL_SWITCH] reason=daily_loss_limit triggered_at=%s "
        "realized_pnl=%.4f",
        ts_utc, float(realized_pnl),
    )


# ------------------------------------------------------------
# v5.7.1 \u2014 Bison & Buffalo exit FSM log emitters.
# ------------------------------------------------------------
# Spec: specs/v5_7_1_stop_loss_optimization.md \u00a75.
# These emit one line per phase transition / per fuse fire / per
# EMA seed (once per ticker per session). Pure I/O \u2014 callers
# decide when to invoke; the helpers keep the wire format stable.
def _v571_log_exit_phase(
    *,
    ticker: str,
    side: str,
    entry_id: str,
    from_phase: str,
    to_phase: str,
    trigger: str,
    current_stop: float | None,
    ts_utc: str,
) -> None:
    """v5.7.1 \u2014 [V571-EXIT_PHASE] phase-transition line."""
    logger.info(
        "[V571-EXIT_PHASE] ticker=%s side=%s entry_id=%s "
        "from_phase=%s to_phase=%s trigger=%s "
        "current_stop=%s ts=%s",
        ticker, side, entry_id, from_phase, to_phase, trigger,
        _v561_fmt_num(current_stop), ts_utc,
    )


def _v571_log_velocity_fuse(
    *,
    ticker: str,
    side: str,
    candle_open: float,
    current_price: float,
    pct_move: float,
    ts_utc: str,
) -> None:
    """v5.7.1 \u2014 [V571-VELOCITY_FUSE] intra-candle circuit breaker."""
    logger.info(
        "[V571-VELOCITY_FUSE] ticker=%s side=%s candle_open=%.4f "
        "current_price=%.4f pct_move=%.4f ts=%s",
        ticker, side, float(candle_open), float(current_price),
        float(pct_move), ts_utc,
    )


def _v571_log_ema_seed(*, ticker: str, ema_value: float, ts_utc: str) -> None:
    """v5.7.1 \u2014 [V571-EMA_SEED] one-shot 9-EMA initialization at 10:15 ET."""
    logger.info(
        "[V571-EMA_SEED] ticker=%s ema_value=%.4f ts=%s",
        ticker, float(ema_value), ts_utc,
    )


def _v570_record_trade_close(pnl_dollars: float) -> float:
    """Update cumulative daily realized P&L on a [TRADE_CLOSED]
    emission and trigger the kill switch the moment the floor is
    breached. Returns the updated cumulative P&L."""
    global _v570_daily_realized_pnl
    global _v570_kill_switch_latched, _v570_kill_switch_logged
    _v570_reset_if_new_session()
    _v570_daily_realized_pnl += float(pnl_dollars or 0.0)
    if (_v570_daily_realized_pnl <= DAILY_LOSS_LIMIT_DOLLARS
            and not _v570_kill_switch_latched):
        _v570_kill_switch_latched = True
        if not _v570_kill_switch_logged:
            try:
                _v570_log_kill_switch(
                    _v570_daily_realized_pnl, _utc_now_iso(),
                )
            finally:
                _v570_kill_switch_logged = True
    return _v570_daily_realized_pnl


def _v570_kill_switch_active() -> bool:
    """Return True iff the daily-loss kill switch has latched."""
    _v570_reset_if_new_session()
    return bool(_v570_kill_switch_latched)


def _v561_archive_qqq_bar(bars: dict | None) -> None:
    """v5.6.1 \u2014 D1: T-off the QQQ stream into /data/bars/<UTC>/QQQ.jsonl.

    `bars` is the dict returned by fetch_1min_bars("QQQ"); we project
    the last-closed bar onto the canonical bar_archive schema. Failure-
    tolerant: a bad QQQ snapshot must never disrupt the trading scan.
    """
    try:
        if not bars:
            return
        closes = bars.get("closes") or []
        ts_arr = bars.get("timestamps") or []
        idx = None
        if len(closes) >= 2 and closes[-2] is not None:
            idx = -2
        elif len(closes) >= 1 and closes[-1] is not None:
            idx = -1
        if idx is None:
            return
        opens = bars.get("opens") or []
        highs = bars.get("highs") or []
        lows = bars.get("lows") or []
        vols = bars.get("volumes") or []
        ts_val = ts_arr[idx] if abs(idx) <= len(ts_arr) else None
        try:
            ts_iso = (datetime.utcfromtimestamp(int(ts_val))
                      .strftime("%Y-%m-%dT%H:%M:%SZ")
                      if ts_val is not None else None)
        except Exception:
            ts_iso = None
        et_bucket: str | None = None
        try:
            now_et = datetime.now(tz=ZoneInfo("America/New_York"))
            et_bucket = volume_profile.session_bucket(now_et)
        except Exception:
            et_bucket = None
        canon_bar = {
            "ts": ts_iso,
            "et_bucket": et_bucket,
            "open":  opens[idx] if abs(idx) <= len(opens) else None,
            "high":  highs[idx] if abs(idx) <= len(highs) else None,
            "low":   lows[idx]  if abs(idx) <= len(lows)  else None,
            "close": closes[idx],
            "iex_volume": vols[idx] if abs(idx) <= len(vols) else None,
            "iex_sip_ratio_used": None,
            "bid": None,
            "ask": None,
            "last_trade_price": bars.get("current_price"),
        }
        bar_archive.write_bar(
            V561_INDEX_TICKER, canon_bar,
            base_dir=bar_archive.DEFAULT_BASE_DIR,
        )
    except Exception as e:
        logger.warning("[V561-QQQ-BAR] archive error: %s", e)


def _v561_persist_or_snapshot(
    ticker: str,
    *,
    base_dir: str | os.PathLike = V561_OR_DIR_DEFAULT,
    today_utc: str | None = None,
) -> str | None:
    """v5.6.1 \u2014 D2: persist OR_High / OR_Low to
    `/data/or/<UTC-date>/<TICKER>.json` once per ticker per session.

    Returns the file path on success, or None on failure (logged at
    warning level, never raised). Reads `or_high[ticker]` / `or_low[ticker]`
    from the live module-level dicts; if either is None the snapshot is
    still written with null values so replay can detect the gap.
    """
    try:
        sym = (ticker or "").strip().upper()
        if not sym:
            return None
        day = today_utc or datetime.utcnow().strftime("%Y-%m-%d")
        dir_path = Path(base_dir) / day
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{sym}.json"
        payload = {
            "ticker": sym,
            "or_high": or_high.get(sym),
            "or_low": or_low.get(sym),
            "computed_at_utc": _utc_now_iso(),
        }
        tmp = str(file_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, file_path)
        return str(file_path)
    except Exception as e:
        logger.warning("[V561-OR-SNAP] persist %s failed: %s", ticker, e)
        return None


# Set tracking which tickers have had their OR snapshot persisted today.
# Keyed by `<UTC-date>:<TICKER>` so a session boundary auto-resets.
_v561_or_snap_taken: set = set()


def _v561_maybe_persist_or_snapshots(now_et=None) -> int:
    """v5.6.1 \u2014 idempotent OR-snapshot dispatcher. Run once per scan
    cycle from inside scan_loop; persists any ticker whose snapshot is
    not yet taken today and whose OR is seeded.

    Returns the number of new files written this call. After 9:35 ET
    every tracked ticker should have a row; pre-9:35 nothing fires.
    """
    try:
        if now_et is None:
            now_et = _now_et()
        # Only fire after the OR window has closed (9:35 ET +).
        if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 35):
            return 0
        today_utc = datetime.utcnow().strftime("%Y-%m-%d")
        # Universe = TRADE_TICKERS plus the index ticker (QQQ) since v5.6.1
        # archives QQQ bars and replay needs the matching OR snapshot to
        # validate the index G1 gate (QQQ has no OR_High/Low gate but the
        # snapshot is harmless and keeps the schema uniform).
        universe = list(TRADE_TICKERS)
        if V561_INDEX_TICKER not in universe:
            universe.append(V561_INDEX_TICKER)
        n = 0
        for sym in universe:
            key = f"{today_utc}:{sym}"
            if key in _v561_or_snap_taken:
                continue
            if sym not in or_high and sym not in or_low:
                # OR not yet seeded \u2014 try again next cycle.
                continue
            path = _v561_persist_or_snapshot(sym, today_utc=today_utc)
            if path:
                _v561_or_snap_taken.add(key)
                n += 1
        return n
    except Exception as e:
        logger.warning("[V561-OR-SNAP] dispatcher error: %s", e)
        return 0


def _v561_reset_or_snap_state() -> None:
    """Reset the per-session OR-snapshot dedup set. Called from
    reset_daily_state() so a new RTH session re-emits snapshots."""
    _v561_or_snap_taken.clear()


# Positions
positions: dict = {}
# positions[ticker] = {
#   "entry_price": float,
#   "shares": int,           # always 10
#   "stop": float,           # current stop price
#   "trail_active": bool,    # True once +$1.00 profit hit
#   "trail_high": float,     # highest price seen since trail activated
#   "entry_count": int,      # 1 or 2
#   "entry_time": str,       # ISO timestamp
# }

# Entry counts per day (reset daily)
daily_entry_count: dict = {}   # ticker -> count (max 5)
daily_entry_date: str = ""

# Paper trading log (today's trades)
paper_trades: list = []

# Paper cash and all-time trades
paper_cash: float = PAPER_STARTING_CAPITAL
paper_all_trades: list = []

# Trade history persistence (Feature 1)
trade_history: list = []        # ALL closed paper trades, max 500
TRADE_HISTORY_MAX = 500

# v4.6.0: _state_loaded moved to paper_state.py (single owner of the flag).

# Short positions (Wounded Buffalo strategy)
short_positions: dict = {}           # paper short: {ticker: {entry_price, shares, stop, trail_stop, trail_active, entry_time, date, side}}
daily_short_entry_count: dict = {}   # {ticker: int} — resets daily, separate from long count
daily_short_entry_date: str = ""     # v4.7.0 — mirror of daily_entry_date for shorts
short_trade_history: list = []       # max 500 closed paper shorts

# v5.0.0 \u2014 Tiger/Buffalo two-stage state-machine tracks. Per-ticker per-
# direction. Schema and transitions defined in STRATEGY.md (canonical
# spec) and tiger_buffalo_v5.py. Persisted in paper_state.json under
# the "v5_tracks" key. v4 paper_state files load with empty tracks
# (defaults to IDLE) \u2014 see paper_state.py load_paper_state.
v5_long_tracks: dict = {}    # {ticker: track_dict}
v5_short_tracks: dict = {}   # {ticker: track_dict}
# C-R1: at most one direction is active per ticker per session.
v5_active_direction: dict = {}  # {ticker: "long"|"short"|None}

# Daily loss limit (Feature 2)
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-500"))
_trading_halted: bool = False
_trading_halted_reason: str = ""

# ============================================================
# MARKET MODE (scaffolding — NO behavior change in this version)
# ============================================================
# Classifies each scan cycle into one of four behavioral regimes and
# exposes a corresponding (frozen, clamped) profile of parameters.
# This version ONLY logs the classification and exposes it via /mode;
# no entry/exit code reads the profile yet. The goal is to observe the
# classifier in production for a week before wiring any parameter to it.
#
# Design principles for when this is wired up:
#   1. Adaptive logic only makes things MORE conservative than baseline,
#      never looser. The baseline is the floor; profiles can raise it.
#   2. Every adaptive parameter is bounded — see CLAMP_* below. A runaway
#      classifier cannot push any value outside these bounds.
#   3. Hard floors (DAILY_LOSS_LIMIT, min trail distance $1.00, min 1
#      share) are constants outside the profile system. They never move.

class MarketMode:
    OPEN       = "OPEN"        # 09:35 - 11:00 ET — OR breakout window
    CHOP       = "CHOP"        # 11:00 - 14:00 ET — lunch chop
    POWER      = "POWER"       # 14:00 - 15:30 ET — power hour
    DEFENSIVE  = "DEFENSIVE"   # triggered by realized P&L <= half loss limit
    CLOSED     = "CLOSED"      # outside market hours / weekend

# Clamps: hard bounds any adaptive value must stay inside.
# baseline values (what the bot uses TODAY, before this scaffold):
#   trail_pct      = 0.010    max entries/ticker/day = 5
#   shares         = 10       min score gate         = (none, all signals pass)
CLAMP_TRAIL_PCT         = (0.006, 0.018)   # 0.6% - 1.8%
CLAMP_MAX_ENTRIES       = (1, 5)
CLAMP_SHARES            = (1, 10)
CLAMP_MIN_SCORE_DELTA   = (0.0, 0.15)      # added to baseline score gate

def _clamp(val, bounds):
    lo, hi = bounds
    return max(lo, min(hi, val))

# Each profile is a frozen dict of the tunables + the rationale.
# All numeric values are pre-clamped by construction via _clamp().
# If you edit these, keep every value inside its CLAMP_* range.
MODE_PROFILES = {
    MarketMode.OPEN: {
        "trail_pct":         _clamp(0.012, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(5,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(10,    CLAMP_SHARES),
        "min_score_delta":   _clamp(0.00,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      True,
        "note":              "OR breakout window — baseline risk",
    },
    MarketMode.CHOP: {
        "trail_pct":         _clamp(0.008, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(2,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(10,    CLAMP_SHARES),
        "min_score_delta":   _clamp(0.10,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      True,
        "note":              "Lunch chop — tighter trails, fewer re-entries",
    },
    MarketMode.POWER: {
        "trail_pct":         _clamp(0.010, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(3,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(10,    CLAMP_SHARES),
        "min_score_delta":   _clamp(0.05,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      True,
        "note":              "Power hour — baseline with entry cutoff at 15:30",
    },
    MarketMode.DEFENSIVE: {
        "trail_pct":         _clamp(0.006, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(1,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(5,     CLAMP_SHARES),
        "min_score_delta":   _clamp(0.15,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      False,
        "note":              "Down >=50% of daily loss limit — size down, shorts off",
    },
    MarketMode.CLOSED: {
        "trail_pct":         _clamp(0.010, CLAMP_TRAIL_PCT),
        "max_entries":       _clamp(5,     CLAMP_MAX_ENTRIES),
        "shares":            _clamp(10,    CLAMP_SHARES),
        "min_score_delta":   _clamp(0.00,  CLAMP_MIN_SCORE_DELTA),
        "allow_shorts":      True,
        "note":              "Market closed — no action",
    },
}

# Last-computed mode snapshot, refreshed each scan. Read by /mode.
_current_mode: str = MarketMode.CLOSED
_current_mode_reason: str = "not yet classified"
_current_mode_pnl: float = 0.0
_current_mode_ts = None

# ============================================================
# MARKET MODE OBSERVERS (v3.1 scaffolding — observation only)
# ============================================================
# Three independent observers that do NOT gate any trade. They run
# alongside the MarketMode classifier, are logged on transitions, and
# surface in /mode. After a week of observation we'll decide which
# (if any) deserve to actually influence trading behavior.
#
# 1) BREADTH   — SPY/QQQ vs their PDC → BULLISH/NEUTRAL/BEARISH
#    (v3.4.34: anchor swapped from AVWAP to PDC)
# 2) RSI       — 14-period on resampled 5-min bars, SPY+QQQ aggregate
#                  → OVERBOUGHT/NEUTRAL/OVERSOLD; plus a per-ticker dict
# 3) TICKER    — per-ticker today realized P&L + current per-ticker RSI
#    HEAT        → lists of tickers that are already red or already at
#                  extremes, surfaced in /mode for pattern-spotting
#
# Thresholds are deliberately conservative for the observation phase.
# If a knob is eventually wired, it'll use these same thresholds or
# tighter ones, never looser.

BREADTH_TOLERANCE_PCT    = 0.001   # ±0.1% around PDC counts as NEUTRAL
RSI_OVERBOUGHT           = 70.0
RSI_OVERSOLD             = 30.0
RSI_PERIOD               = 14
RSI_BAR_MINUTES          = 5
RSI_MIN_BARS_REQUIRED    = RSI_PERIOD + 1   # Wilder RSI needs P+1 closes
TICKER_RED_THRESHOLD_USD = -5.0    # tickers with today P&L <= this are "red"

# Observer snapshot — refreshed each scan.
_current_breadth: str = "UNKNOWN"
_current_breadth_detail: str = ""
_current_rsi_regime: str = "UNKNOWN"
_current_rsi_detail: str = ""
_current_rsi_per_ticker: dict = {}      # ticker -> float RSI
_current_ticker_pnl: dict = {}          # ticker -> realized P&L today
_current_ticker_red: list = []          # list of (ticker, pnl) sorted worst-first
_current_ticker_extremes: list = []     # list of (ticker, rsi, "OB"/"OS")

# v3.4.21 — per-ticker entry-gate snapshot for dashboard rendering.
# Populated by _update_gate_snapshot() on every scan cycle.
# Shape: {ticker: {
#     "side": "LONG"|"SHORT",
#     "break": bool,              # 1m close crossed OR (above/below)
#     "polarity": bool,           # price vs PDC on the right side
#     "index": bool,              # SPY/QQQ on the right side of PDC
#     "di": bool|None,            # DI+/DI- >= TIGER_V2_DI_THRESHOLD;
#                                 # None = warmup (DI not yet computable)
#     "ts": iso timestamp,
# }}
# v3.5.x: vol_pct / vol_ok removed — Tiger 2.0 replaced the volume gate
# with DI+/DI-, and TIGER_V2_REQUIRE_VOL defaults to False. Keeping the
# fields on the snapshot was decorative and misled diagnosis.
# Read-only from outside the scan loop; never cleared mid-scan.
_gate_snapshot: dict = {}

# v3.4.21 — near-miss ring buffer. Breakouts that cleared the price
# gate (1m close past OR) but were declined by volume confirmation.
# Bounded to last 20 entries. Exposed via /api/state and /near_misses.
# Records only — no effect on entry decisions (fail-closed stays).
_NEAR_MISS_MAX = 20
_near_miss_log: list = []


def _record_near_miss(**row):
    """Prepend a near-miss record. Trim to _NEAR_MISS_MAX.

    Expected keys: ticker, side, reason, close, level, vol_bar, vol_avg,
    vol_pct, ts. Missing keys are allowed — stored as-is.
    """
    global _near_miss_log
    row.setdefault("ts", datetime.now(timezone.utc).isoformat())
    _near_miss_log.insert(0, row)
    if len(_near_miss_log) > _NEAR_MISS_MAX:
        _near_miss_log = _near_miss_log[:_NEAR_MISS_MAX]


def _update_gate_snapshot(ticker):
    """Recompute the dashboard gate snapshot for ``ticker`` from the
    current OR envelope and live price.

    Side + break are derived purely from OR envelope each cycle (no
    latch). When inside the envelope, side falls back to the nearest
    edge for the polarity preview but break is False.

    Emits a structured ``GATE_EVAL`` log line for audit.
    """
    if ticker not in or_high or ticker not in or_low:
        return
    or_h = or_high[ticker]
    or_l = or_low[ticker]
    pdc_val = pdc.get(ticker)

    bars = fetch_1min_bars(ticker)
    if not bars:
        return
    price = bars.get("current_price")
    if price is None or price <= 0:
        return

    fmp_q = get_fmp_quote(ticker)
    if fmp_q:
        fmp_price = fmp_q.get("price")
        if fmp_price and fmp_price > 0:
            price = fmp_price
        fmp_pdc = fmp_q.get("previousClose")
        if fmp_pdc and fmp_pdc > 0:
            pdc_val = fmp_pdc

    if price > or_h:
        side = "LONG"
        break_ok = True
    elif price < or_l:
        side = "SHORT"
        break_ok = True
    else:
        side = "LONG" if abs(price - or_h) < abs(price - or_l) else "SHORT"
        break_ok = False

    if pdc_val and pdc_val > 0:
        polarity_ok = (price > pdc_val) if side == "LONG" else (price < pdc_val)
    else:
        polarity_ok = False

    spy_pdc_val = pdc.get("SPY")
    qqq_pdc_val = pdc.get("QQQ")
    index_ok = None
    if spy_pdc_val and qqq_pdc_val and spy_pdc_val > 0 and qqq_pdc_val > 0:
        spy_bars = fetch_1min_bars("SPY")
        qqq_bars = fetch_1min_bars("QQQ")
        if spy_bars and qqq_bars:
            spy_p = spy_bars.get("current_price")
            qqq_p = qqq_bars.get("current_price")
            if spy_p and qqq_p:
                if side == "LONG":
                    index_ok = (spy_p > spy_pdc_val) and (qqq_p > qqq_pdc_val)
                else:
                    index_ok = (spy_p < spy_pdc_val) and (qqq_p < qqq_pdc_val)

    di_plus, di_minus = tiger_di(ticker)
    if di_plus is None or di_minus is None:
        di_ok = None  # warmup
    elif side == "LONG":
        di_ok = di_plus >= TIGER_V2_DI_THRESHOLD
    else:
        di_ok = di_minus >= TIGER_V2_DI_THRESHOLD

    # v4.3.0 \u2014 extension_pct: signed distance of price past the
    # relevant OR edge. LONG = (price \u2212 OR_High)/OR_High*100;
    # SHORT = (OR_Low \u2212 price)/OR_Low*100. None if OR not seeded.
    extension_pct: float | None
    if side == "LONG" and or_h and or_h > 0:
        extension_pct = round((price - or_h) / or_h * 100.0, 2)
    elif side == "SHORT" and or_l and or_l > 0:
        extension_pct = round((or_l - price) / or_l * 100.0, 2)
    else:
        extension_pct = None

    _gate_snapshot[ticker] = {
        "side": side,
        "break": bool(break_ok),
        "polarity": bool(polarity_ok),
        "index": index_ok,
        "di": di_ok,
        "extension_pct": extension_pct,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    idx_str = "None" if index_ok is None else str(bool(index_ok))
    di_str = "None" if di_ok is None else str(bool(di_ok))
    logger.info(
        "GATE_EVAL ticker=%s price=%.2f or_hi=%.2f or_lo=%.2f "
        "side=%s break=%s polarity=%s index=%s di=%s",
        ticker, price, or_h, or_l, side, bool(break_ok),
        bool(polarity_ok), idx_str, di_str,
    )


def _classify_breadth():
    """Observer 1: breadth from SPY/QQQ vs their PDC.
    Returns (label, detail). Never raises.
    v3.4.34: anchor swapped from AVWAP → PDC.
    """
    try:
        # fetch_1min_bars is cycle-cached — if the scan loop already
        # fetched SPY/QQQ this cycle we reuse; otherwise we fetch once.
        spy_bars = fetch_1min_bars("SPY")
        qqq_bars = fetch_1min_bars("QQQ")
        spy_px = spy_bars.get("current_price") if spy_bars else None
        qqq_px = qqq_bars.get("current_price") if qqq_bars else None
        spy_anchor = pdc.get("SPY") or 0
        qqq_anchor = pdc.get("QQQ") or 0
        if not (spy_px and qqq_px and spy_anchor and qqq_anchor):
            return ("UNKNOWN", "SPY/QQQ price or PDC not ready")

        spy_diff = (spy_px - spy_anchor) / spy_anchor
        qqq_diff = (qqq_px - qqq_anchor) / qqq_anchor
        tol = BREADTH_TOLERANCE_PCT

        def _side(d):
            if d >  tol: return "above"
            if d < -tol: return "below"
            return "at"

        spy_side = _side(spy_diff)
        qqq_side = _side(qqq_diff)
        detail = "SPY %+.2f%% %s PDC | QQQ %+.2f%% %s PDC" % (
            spy_diff * 100, spy_side, qqq_diff * 100, qqq_side)

        if spy_side == "above" and qqq_side == "above":
            return ("BULLISH", detail)
        if spy_side == "below" and qqq_side == "below":
            return ("BEARISH", detail)
        return ("NEUTRAL", detail)
    except Exception as e:
        logger.debug("_classify_breadth failed: %s", e)
        return ("UNKNOWN", "breadth computation failed")


def _resample_to_5min(timestamps, closes):
    """Resample a 1-min close series into 5-min bar closes.

    Each 5-min bar closes on the last 1-min close whose epoch second falls
    inside the [bar_start, bar_start+300) window aligned to UTC minute
    boundaries (9:30:00, 9:35:00, 9:40:00, …). Partial/forming bars are
    dropped — only complete 5-min intervals contribute.

    Returns a list of floats (oldest-first). Robust to None closes and to
    timestamps in any order (will sort).
    """
    if not timestamps or not closes or len(timestamps) != len(closes):
        return []
    # Pair and drop Nones, then sort by timestamp ascending.
    pairs = [(int(t), float(c)) for t, c in zip(timestamps, closes)
             if t is not None and c is not None]
    if not pairs:
        return []
    pairs.sort(key=lambda p: p[0])

    # Bucket by floor(ts / 300). Last close in each bucket is the bar close.
    buckets = {}
    for ts, c in pairs:
        bucket = ts // 300
        buckets[bucket] = c   # overwrites — last wins

    # Drop the most recent (possibly forming) bucket so we only return
    # closed bars. Safe heuristic: if the last pair's ts doesn't reach
    # (bucket+1)*300 - 60, the bar is still forming. We conservatively
    # drop the newest bucket always — partial bars are noisy for RSI.
    ordered = sorted(buckets.keys())
    if len(ordered) >= 1:
        ordered = ordered[:-1]   # drop newest (possibly partial)
    return [buckets[b] for b in ordered]


def _compute_rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI on a list of closes (oldest-first).
    Returns float in [0, 100], or None if not enough data.
    """
    if not closes or len(closes) < period + 1:
        return None
    try:
        gains = 0.0
        losses = 0.0
        # Seed average gain/loss over the first `period` deltas.
        for i in range(1, period + 1):
            delta = closes[i] - closes[i - 1]
            if delta > 0: gains += delta
            else:         losses += -delta
        avg_gain = gains / period
        avg_loss = losses / period

        # Wilder smoothing for remaining deltas.
        for i in range(period + 1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gain = delta if delta > 0 else 0.0
            loss = -delta if delta < 0 else 0.0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
    except Exception:
        return None


# ============================================================
# v3.4.47 — Eye of the Tiger 2.0 helpers
# ============================================================

def _resample_to_5min_ohlc(timestamps, opens, highs, lows, closes):
    """Resample 1m OHLC into 5m OHLC.

    Returns dict with lists 'highs', 'lows', 'closes'
    (oldest-first), only fully-closed bars.
    Uses floor(ts/300) bucketing like _resample_to_5min.
    Drops the newest bucket (may be forming).
    """
    if not timestamps or not closes:
        return None
    # Build per-bucket dicts: store max high, min low, last close.
    buckets_high = {}
    buckets_low = {}
    buckets_close = {}
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        h = highs[i] if i < len(highs) else None
        lo = lows[i] if i < len(lows) else None
        c = closes[i] if i < len(closes) else None
        if h is None or lo is None or c is None:
            continue
        bucket = int(ts) // 300
        if bucket not in buckets_high:
            buckets_high[bucket] = h
            buckets_low[bucket] = lo
            buckets_close[bucket] = c
        else:
            buckets_high[bucket] = max(buckets_high[bucket], h)
            buckets_low[bucket] = min(buckets_low[bucket], lo)
            buckets_close[bucket] = c  # last close wins
    ordered = sorted(buckets_high.keys())
    if len(ordered) <= 1:
        return None
    # Drop newest bucket (may be forming)
    ordered = ordered[:-1]
    return {
        "highs":  [buckets_high[b]  for b in ordered],
        "lows":   [buckets_low[b]   for b in ordered],
        "closes": [buckets_close[b] for b in ordered],
    }


DI_PERIOD = 15  # Gene's spec: "DI+ (15 period, 5m)"


def _compute_di(highs, lows, closes, period=DI_PERIOD):
    """Wilder DI+ and DI-.

    Returns (di_plus, di_minus) as floats, or
    (None, None) if insufficient data.

    Wilder formula:
      +DM[i] = high[i]-high[i-1] if that > low[i-1]-low[i] AND >0 else 0
      -DM[i] = low[i-1]-low[i] if that > high[i]-high[i-1] AND >0 else 0
      TR[i]  = max(high[i]-low[i],
                   |high[i]-close[i-1]|, |low[i]-close[i-1]|)
    Smoothing (Wilder):
      first_val = sum of first `period` values
      new = prev - prev/period + current
    Needs at least period+1 bars.
    """
    n = len(closes)
    if n < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
        return None, None
    try:
        # Compute raw DM and TR for each bar i >= 1
        raw_pdm = []
        raw_ndm = []
        raw_tr  = []
        for i in range(1, n):
            up_move   = highs[i]  - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            pdm = up_move   if (up_move   > down_move and up_move   > 0) else 0.0
            ndm = down_move if (down_move > up_move   and down_move > 0) else 0.0
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]),
            )
            raw_pdm.append(pdm)
            raw_ndm.append(ndm)
            raw_tr.append(tr)

        # Seed: sum of first `period` values
        smooth_pdm = sum(raw_pdm[:period])
        smooth_ndm = sum(raw_ndm[:period])
        smooth_tr  = sum(raw_tr[:period])

        # Wilder smoothing for remaining values
        for i in range(period, len(raw_tr)):
            smooth_pdm = smooth_pdm - smooth_pdm / period + raw_pdm[i]
            smooth_ndm = smooth_ndm - smooth_ndm / period + raw_ndm[i]
            smooth_tr  = smooth_tr  - smooth_tr  / period + raw_tr[i]

        if smooth_tr == 0:
            return None, None
        di_plus  = 100.0 * smooth_pdm / smooth_tr
        di_minus = 100.0 * smooth_ndm / smooth_tr
        return di_plus, di_minus
    except Exception:
        return None, None


# ------------------------------------------------------------
# DI seed buffer (v4.0.2-beta)
# ------------------------------------------------------------
# Without seeding, DI starts null on every boot and takes
# ~DI_PERIOD*2 = ~30 closed 5m bars (75 min of live data) before
# tiger_di() can return a non-null value. _seed_di_buffer() pulls
# historical 5m bars from Alpaca at scanner startup so DI is armed
# on the very first scan cycle.
#
# Storage: per-ticker list of closed 5m OHLC dicts, oldest-first.
#   { ticker: [ {"bucket": int, "high": f, "low": f, "close": f}, ... ] }
# tiger_di() merges these with live-resampled 5m bars, deduped by
# bucket (= ts // 300), so as the live session accumulates the
# seed is transparently superseded.
_DI_SEED_CACHE: dict = {}


def _alpaca_data_client():
    """Build a read-only StockHistoricalDataClient using whatever
    Alpaca paper credentials are in the environment. Tries Val first,
    then Gene. Returns None if no keys are set or alpaca-py import
    fails \u2014 caller must tolerate a None return.
    """
    key = os.getenv("VAL_ALPACA_PAPER_KEY", "").strip() \
          or os.getenv("GENE_ALPACA_PAPER_KEY", "").strip()
    secret = os.getenv("VAL_ALPACA_PAPER_SECRET", "").strip() \
             or os.getenv("GENE_ALPACA_PAPER_SECRET", "").strip()
    if not key or not secret:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        return StockHistoricalDataClient(key, secret)
    except Exception as e:
        logger.debug("alpaca data client build failed: %s", e)
        return None


def _seed_di_buffer(ticker):
    """Seed the DI 5m buffer for `ticker` from Alpaca historical bars.

    Priority stream (oldest \u2192 newest for DI math):
      today-RTH \u2192 today-premarket \u2192 prior-day-RTH
    but we feed oldest-first so the order inside the buffer is
    chronological: prior-day-RTH, then today-premarket, then today-RTH.
    The "priority" really means \u2014 if we already have enough
    today-RTH bars, we don't need to reach back further.

    If the DI_PREMARKET_SEED env flag is "0", premarket bars are
    skipped (kill switch for premarket-noise concerns).

    Safe to call on restart mid-session. Idempotent \u2014 overwrites
    any prior seed for the ticker. On any Alpaca failure logs a
    warning and continues; DI will warm up from live ticks.

    Returns dict {"bars_today_rth": N, "bars_premarket": N,
                  "bars_prior_day": N, "di_after_seed": float|None}.
    """
    result = {
        "bars_today_rth": 0, "bars_premarket": 0,
        "bars_prior_day": 0, "di_after_seed": None,
    }
    client = _alpaca_data_client()
    if client is None:
        logger.debug("DI_SEED %s skipped \u2014 no alpaca data client", ticker)
        return result

    premarket_on = os.getenv("DI_PREMARKET_SEED", "1").strip() not in (
        "0", "false", "False", "",
    )

    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except Exception as e:
        logger.debug("DI_SEED %s import failed: %s", ticker, e)
        return result

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today_0400 = now_et.replace(hour=4,  minute=0,  second=0, microsecond=0)
    today_0930 = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    yday = now_et - timedelta(days=1)
    # Step back over weekend to last weekday
    while yday.weekday() >= 5:
        yday = yday - timedelta(days=1)
    yday_rth_end   = yday.replace(hour=16, minute=0, second=0, microsecond=0)
    yday_rth_start = yday.replace(hour=14, minute=50, second=0, microsecond=0)

    def _fetch(start, end):
        try:
            req = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Minute,
                start=start.astimezone(timezone.utc),
                end=end.astimezone(timezone.utc),
                feed="iex",
            )
            resp = client.get_stock_bars(req)
            data = getattr(resp, "data", {}) or {}
            rows = data.get(ticker, []) or []
            return rows
        except Exception as e:
            logger.warning("DI_SEED %s alpaca fetch %s\u2192%s failed: %s",
                           ticker, start, end, e)
            return []

    # Fetch today 04:00 ET \u2192 now (premarket + whatever RTH has happened)
    today_rows = _fetch(today_0400, now_et)

    # Bucket 1m rows into 5m OHLC, tagged by classification.
    # today_0930_ts = unix seconds of today's 09:30 ET
    today_0930_ts = int(today_0930.timestamp())

    today_rth_buckets   = {}
    today_pre_buckets   = {}

    for row in today_rows:
        ts = getattr(row, "timestamp", None)
        if ts is None:
            continue
        try:
            # alpaca timestamps are tz-aware datetimes
            epoch = int(ts.timestamp())
        except Exception:
            continue
        h  = float(getattr(row, "high",  0) or 0)
        lo = float(getattr(row, "low",   0) or 0)
        c  = float(getattr(row, "close", 0) or 0)
        if h <= 0 or lo <= 0 or c <= 0:
            continue
        bucket = epoch // 300
        target = today_rth_buckets if epoch >= today_0930_ts else today_pre_buckets
        if bucket not in target:
            target[bucket] = {"bucket": bucket, "high": h, "low": lo, "close": c}
        else:
            target[bucket]["high"]  = max(target[bucket]["high"],  h)
            target[bucket]["low"]   = min(target[bucket]["low"],   lo)
            target[bucket]["close"] = c

    # Drop newest bucket if it could still be forming (now < bucket_end)
    def _finalize(buckets):
        ordered = sorted(buckets.keys())
        if not ordered:
            return []
        last = ordered[-1]
        last_end_ts = (last + 1) * 300
        if int(now_et.timestamp()) < last_end_ts:
            ordered = ordered[:-1]
        return [buckets[b] for b in ordered]

    today_rth_list = _finalize(today_rth_buckets)
    today_pre_list = _finalize(today_pre_buckets) if premarket_on else []
    result["bars_today_rth"]  = len(today_rth_list)
    result["bars_premarket"]  = len(today_pre_list)

    seeded_enough = len(today_rth_list) + len(today_pre_list) >= DI_PERIOD * 2
    prior_day_list = []
    if not seeded_enough:
        prior_rows = _fetch(yday_rth_start, yday_rth_end)
        prior_buckets = {}
        for row in prior_rows:
            ts = getattr(row, "timestamp", None)
            if ts is None:
                continue
            try:
                epoch = int(ts.timestamp())
            except Exception:
                continue
            h  = float(getattr(row, "high",  0) or 0)
            lo = float(getattr(row, "low",   0) or 0)
            c  = float(getattr(row, "close", 0) or 0)
            if h <= 0 or lo <= 0 or c <= 0:
                continue
            bucket = epoch // 300
            if bucket not in prior_buckets:
                prior_buckets[bucket] = {"bucket": bucket, "high": h,
                                          "low": lo, "close": c}
            else:
                prior_buckets[bucket]["high"]  = max(prior_buckets[bucket]["high"],  h)
                prior_buckets[bucket]["low"]   = min(prior_buckets[bucket]["low"],   lo)
                prior_buckets[bucket]["close"] = c
        prior_day_list = [prior_buckets[b] for b in sorted(prior_buckets.keys())]
        result["bars_prior_day"] = len(prior_day_list)

    # Combine chronologically: prior-day \u2192 today-premarket \u2192 today-RTH
    combined = prior_day_list + today_pre_list + today_rth_list
    # Dedup by bucket, keep last
    dedup = {}
    for b in combined:
        dedup[b["bucket"]] = b
    final_list = [dedup[k] for k in sorted(dedup.keys())]
    _DI_SEED_CACHE[ticker] = final_list

    # Compute DI on the seeded state for logging
    if len(final_list) >= DI_PERIOD + 1:
        highs  = [b["high"]  for b in final_list]
        lows   = [b["low"]   for b in final_list]
        closes = [b["close"] for b in final_list]
        dp, _dm = _compute_di(highs, lows, closes)
        result["di_after_seed"] = dp

    logger.info(
        "DI_SEED ticker=%s bars_today_rth=%d bars_premarket=%d "
        "bars_prior_day=%d di_after_seed=%s",
        ticker, result["bars_today_rth"], result["bars_premarket"],
        result["bars_prior_day"],
        ("%.2f" % result["di_after_seed"])
        if result["di_after_seed"] is not None else "None",
    )
    return result


def _seed_di_all(tickers):
    """Run _seed_di_buffer for every ticker and emit a summary line."""
    seeded = 0
    skipped = 0
    for t in tickers:
        try:
            r = _seed_di_buffer(t)
            if r.get("di_after_seed") is not None:
                seeded += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("DI_SEED %s crashed: %s", t, e)
            skipped += 1
    logger.info(
        "DI_SEED_DONE tickers=%d seeded_with_nonnull_di=%d skipped=%d",
        len(tickers), seeded, skipped,
    )


# ------------------------------------------------------------
# Opening Range seed (v4.0.3-beta)
# ------------------------------------------------------------
# Mirrors the DI seeder: on startup (or mid-session restart), pull
# today's 9:30 ET +/- OR_WINDOW_MINUTES from Alpaca historical 1m
# bars and write or_high / or_low / pdc directly. Idempotent \u2014
# the scheduled 9:35 ET collect_or() still runs on fresh boots that
# happen before the open. On any Alpaca failure the seeder logs a
# warning and returns; the existing Yahoo+FMP path in collect_or()
# continues to work.
OR_WINDOW_MINUTES = int(os.getenv("OR_WINDOW_MINUTES", "5") or "5")


def _seed_opening_range(ticker):
    """Seed or_high[ticker]/or_low[ticker]/pdc[ticker] from Alpaca
    historical 1m bars covering today's 09:30 ET \u2192 09:30+OR_WINDOW_MINUTES
    ET window. Returns dict with keys: or_high, or_low, bars_used.

    Only seeds when the OR window is complete (now_et >= window end).
    Pre-open or pre-9:35-ET restarts return bars_used=0 so the
    scheduled 09:35 ET collect_or() can run cleanly.
    """
    result = {"or_high": None, "or_low": None, "bars_used": 0}
    client = _alpaca_data_client()
    if client is None:
        logger.debug("OR_SEED %s skipped \u2014 no alpaca data client", ticker)
        return result
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except Exception as e:
        logger.debug("OR_SEED %s import failed: %s", ticker, e)
        return result

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    window_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    window_end = window_start + timedelta(minutes=OR_WINDOW_MINUTES)
    if now_et < window_end:
        logger.debug("OR_SEED %s skipped \u2014 window not complete (now_et=%s < end=%s)",
                     ticker, now_et.strftime("%H:%M"),
                     window_end.strftime("%H:%M"))
        return result

    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=window_start.astimezone(timezone.utc),
            end=window_end.astimezone(timezone.utc),
            feed="iex",
        )
        resp = client.get_stock_bars(req)
        data = getattr(resp, "data", {}) or {}
        rows = data.get(ticker, []) or []
    except Exception as e:
        logger.warning("OR_SEED %s alpaca fetch failed: %s", ticker, e)
        return result

    max_hi = None
    min_lo = None
    bars_used = 0
    window_start_ts = int(window_start.timestamp())
    window_end_ts = int(window_end.timestamp())
    for row in rows:
        ts = getattr(row, "timestamp", None)
        if ts is None:
            continue
        try:
            epoch = int(ts.timestamp())
        except Exception:
            continue
        if epoch < window_start_ts or epoch >= window_end_ts:
            continue
        h = float(getattr(row, "high", 0) or 0)
        lo = float(getattr(row, "low", 0) or 0)
        if h <= 0 or lo <= 0:
            continue
        if max_hi is None or h > max_hi:
            max_hi = h
        if min_lo is None or lo < min_lo:
            min_lo = lo
        bars_used += 1

    if max_hi is None or min_lo is None:
        logger.warning("OR_SEED %s \u2014 no usable bars in window", ticker)
        return result

    or_high[ticker] = max_hi
    or_low[ticker] = min_lo
    result["or_high"] = max_hi
    result["or_low"] = min_lo
    result["bars_used"] = bars_used
    logger.info(
        "OR_SEED ticker=%s or_high=%.2f or_low=%.2f bars_used=%d "
        "window_et=%s-%s source=alpaca_historical",
        ticker, max_hi, min_lo, bars_used,
        window_start.strftime("%H:%M"), window_end.strftime("%H:%M"),
    )
    return result


def _seed_opening_range_all(tickers):
    """Run _seed_opening_range for every ticker and emit a summary.

    Marks or_collected_date=today once at least one ticker is seeded,
    so the scheduled 09:35 ET collect_or() does not overwrite the
    fresher Alpaca-sourced OR. Safe on a before-open restart \u2014
    returns immediately when the OR window is not yet complete.
    """
    global or_collected_date
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today = now_et.strftime("%Y-%m-%d")
    window_end = now_et.replace(hour=9, minute=30, second=0, microsecond=0) \
                    + timedelta(minutes=OR_WINDOW_MINUTES)
    if now_et < window_end:
        logger.info(
            "OR_SEED_DONE tickers=0 seeded=0 skipped=%d \u2014 pre-OR-window",
            len(tickers),
        )
        return
    seeded = 0
    skipped = 0
    for t in tickers:
        try:
            r = _seed_opening_range(t)
            if r.get("bars_used", 0) > 0:
                seeded += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("OR_SEED %s crashed: %s", t, e)
            skipped += 1
    if seeded > 0:
        or_collected_date = today
    logger.info(
        "OR_SEED_DONE tickers=%d seeded=%d skipped=%d",
        len(tickers), seeded, skipped,
    )


def _resample_to_5min_ohlc_buckets(timestamps, highs, lows, closes):
    """Like _resample_to_5min_ohlc but returns list of bucket dicts.
    Oldest-first, newest (possibly forming) bucket dropped.
    Returns [] on empty input.
    """
    if not timestamps or not closes:
        return []
    buckets = {}
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        h  = highs[i]  if i < len(highs)  else None
        lo = lows[i]   if i < len(lows)   else None
        c  = closes[i] if i < len(closes) else None
        if h is None or lo is None or c is None:
            continue
        bucket = int(ts) // 300
        if bucket not in buckets:
            buckets[bucket] = {"bucket": bucket, "high": h, "low": lo, "close": c}
        else:
            buckets[bucket]["high"]  = max(buckets[bucket]["high"],  h)
            buckets[bucket]["low"]   = min(buckets[bucket]["low"],   lo)
            buckets[bucket]["close"] = c
    ordered = sorted(buckets.keys())
    if len(ordered) <= 1:
        return []
    ordered = ordered[:-1]
    return [buckets[b] for b in ordered]


def tiger_di(ticker):
    """Return (di_plus, di_minus) for a ticker using 5m OHLC
    resampled from fetch_1min_bars, or (None, None) if not ready.

    Merges any pre-seeded 5m bars (_DI_SEED_CACHE) with live-resampled
    bars so DI is available from the first scan after boot. Both
    streams are keyed by real epoch buckets (ts // 300); overlapping
    buckets prefer the live value (last-write-wins).
    """
    bars = fetch_1min_bars(ticker)
    live_list = []
    if bars and bars.get("timestamps"):
        live_list = _resample_to_5min_ohlc_buckets(
            bars["timestamps"],
            bars.get("highs",  []),
            bars.get("lows",   []),
            bars.get("closes", []),
        )

    seed = _DI_SEED_CACHE.get(ticker) or []
    merged = {}
    for b in seed:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])
    for b in live_list:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])

    if not merged:
        return None, None
    keys = sorted(merged.keys())
    highs  = [merged[k][0] for k in keys]
    lows   = [merged[k][1] for k in keys]
    closes = [merged[k][2] for k in keys]
    if len(closes) < DI_PERIOD + 1:
        return None, None
    return _compute_di(highs, lows, closes)


# ============================================================
# v5.0.0 \u2014 Tiger/Buffalo state-machine integration helpers
# ============================================================
# Spec: STRATEGY.md (canonical). Pure-function rule logic lives in
# tiger_buffalo_v5.py; this block is the runtime glue that pulls live
# market data into the spec helpers and persists track state.
def v5_get_track(ticker: str, direction: str) -> dict:
    """Return the live track for (ticker, direction), creating an IDLE
    record if absent. C-R1 mutex is enforced separately by callers.
    """
    if direction == v5.DIR_LONG:
        bucket = v5_long_tracks
    elif direction == v5.DIR_SHORT:
        bucket = v5_short_tracks
    else:
        raise ValueError(f"unknown direction {direction!r}")
    if ticker not in bucket:
        bucket[ticker] = v5.new_track(direction)
    return bucket[ticker]


def v5_di_1m_5m(ticker):
    """Compute DI+ and DI- on both 1m and 5m timeframes for a ticker.
    Used by L-P2-R1 / S-P2-R1 (gates need both timeframes).

    Returns dict with keys 'di_plus_1m', 'di_minus_1m', 'di_plus_5m',
    'di_minus_5m'. Any value can be None when warmup is incomplete.

    DMI period = 15 per C-R2 (matches Gene's spec and the canonical
    v4 DI_PERIOD = 15 constant). v5 and v4 now compute DMI on the
    same period so signals between the v5 decision engine and the v4
    dashboard / executor agree byte-for-byte.
    """
    bars = fetch_1min_bars(ticker)
    out = {
        "di_plus_1m": None, "di_minus_1m": None,
        "di_plus_5m": None, "di_minus_5m": None,
    }
    if not bars:
        return out
    closes_1m = [c for c in bars.get("closes", []) if c is not None]
    highs_1m  = [h for h in bars.get("highs",  []) if h is not None]
    lows_1m   = [lo for lo in bars.get("lows", []) if lo is not None]
    n = min(len(closes_1m), len(highs_1m), len(lows_1m))
    if n >= v5.DMI_PERIOD + 1:
        dp, dm = _compute_di(highs_1m[:n], lows_1m[:n], closes_1m[:n],
                             period=v5.DMI_PERIOD)
        out["di_plus_1m"], out["di_minus_1m"] = dp, dm
    # 5m \u2014 reuse tiger_di which already merges seed + live 5m buckets
    # and normalizes on DI_PERIOD = 15. v5 now matches v4's period
    # exactly (C-R2 corrected v5.0.0 \u2192 v5.0.1 per Gene's flag), so the
    # v5 5m DI is the same value tiger_di emits.
    live_5m = _resample_to_5min_ohlc_buckets(
        bars.get("timestamps", []),
        bars.get("highs",  []),
        bars.get("lows",   []),
        bars.get("closes", []),
    )
    seed = _DI_SEED_CACHE.get(ticker) or []
    merged = {}
    for b in seed:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])
    for b in live_5m:
        merged[b["bucket"]] = (b["high"], b["low"], b["close"])
    if merged:
        keys = sorted(merged.keys())
        h5 = [merged[k][0] for k in keys]
        l5 = [merged[k][1] for k in keys]
        c5 = [merged[k][2] for k in keys]
        if len(c5) >= v5.DMI_PERIOD + 1:
            dp5, dm5 = _compute_di(h5, l5, c5, period=v5.DMI_PERIOD)
            out["di_plus_5m"], out["di_minus_5m"] = dp5, dm5
    return out


def v5_first_hour_high(ticker):
    """L-P1-G4: high of the 09:30-10:30 ET window on the current session.

    Returns float or None if the window has not yet completed enough
    bars to compute. Reads from fetch_1min_bars (same per-cycle cache).
    """
    bars = fetch_1min_bars(ticker)
    if not bars:
        return None
    timestamps = bars.get("timestamps") or []
    highs = bars.get("highs") or []
    if not timestamps or not highs:
        return None
    # 09:30..10:30 ET. Convert each ts to ET via _now_et's tz, but we
    # only need date math: build window in ET then compare epochs.
    now_et = _now_et()
    window_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    window_close = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
    open_epoch = window_open.timestamp()
    close_epoch = window_close.timestamp()
    fh_high = None
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        if ts < open_epoch or ts >= close_epoch:
            continue
        h = highs[i] if i < len(highs) else None
        if h is None:
            continue
        fh_high = h if fh_high is None else max(fh_high, h)
    return fh_high


def v5_opening_range_low_5m(ticker):
    """S-P1-G4: low of the 09:30-09:35 ET 5m candle.

    The v4 'or_low' dict is computed off the same window, so we read
    that directly when present; falls back to a fresh scan otherwise.
    """
    if ticker in or_low:
        return or_low[ticker]
    return None


def v5_lock_all_tracks(reason: str) -> int:
    """Force every v5 track to LOCKED_FOR_DAY. Used by:
      - C-R4 daily-loss-limit
      - C-R5 EOD force-close
      - C-R6 Sovereign Regime Shield

    Returns the count of tracks locked (excluding those already locked).
    """
    locked = 0
    for bucket in (v5_long_tracks, v5_short_tracks):
        for tk, track in bucket.items():
            if track.get("state") != v5.STATE_LOCKED:
                v5.transition_to_locked(track)
                locked += 1
    if locked:
        logger.info("v5: locked %d tracks (%s)", locked, reason)
    return locked


def _tiger_two_bar_long(closes, or_h):
    """True if the last 2 closed 1m closes are both > OR high.

    Requires len(closes) >= 2. Fail-closed: missing data -> False.
    """
    if not closes or len(closes) < 2:
        return False
    return closes[-1] > or_h and closes[-2] > or_h


def _tiger_two_bar_short(closes, or_l):
    """True if the last 2 closed 1m closes are both < OR low.

    Requires len(closes) >= 2. Fail-closed: missing data -> False.
    """
    if not closes or len(closes) < 2:
        return False
    return closes[-1] < or_l and closes[-2] < or_l


def _rsi_for_ticker(ticker):
    """Compute current RSI(14) on 5-min bars for a ticker, using cached bars.
    Returns float or None. Never raises.
    """
    try:
        bars = fetch_1min_bars(ticker)   # cached per cycle
        if not bars:
            return None
        closes_5m = _resample_to_5min(bars.get("timestamps", []),
                                      bars.get("closes", []))
        if len(closes_5m) < RSI_MIN_BARS_REQUIRED:
            return None
        return _compute_rsi(closes_5m)
    except Exception as e:
        logger.debug("_rsi_for_ticker %s failed: %s", ticker, e)
        return None


def _classify_rsi_regime():
    """Observer 2: aggregate RSI regime from SPY+QQQ 5-min RSI.
    Returns (label, detail, per_ticker_dict). Never raises.
    """
    per_ticker = {}
    try:
        spy_rsi = _rsi_for_ticker("SPY")
        qqq_rsi = _rsi_for_ticker("QQQ")
        if spy_rsi is not None: per_ticker["SPY"] = spy_rsi
        if qqq_rsi is not None: per_ticker["QQQ"] = qqq_rsi

        # Per-ticker RSI for the trade universe. Uses the cycle cache,
        # so if scan_loop already fetched these bars this cycle there's
        # no extra network call.
        for t in TRADE_TICKERS:
            v = _rsi_for_ticker(t)
            if v is not None:
                per_ticker[t] = v

        if spy_rsi is None or qqq_rsi is None:
            return ("UNKNOWN", "SPY/QQQ RSI not ready (need %d closed 5m bars)" %
                    RSI_MIN_BARS_REQUIRED, per_ticker)

        avg = (spy_rsi + qqq_rsi) / 2.0
        detail = "SPY %.1f | QQQ %.1f | avg %.1f" % (spy_rsi, qqq_rsi, avg)
        if avg >= RSI_OVERBOUGHT: return ("OVERBOUGHT", detail, per_ticker)
        if avg <= RSI_OVERSOLD:   return ("OVERSOLD",   detail, per_ticker)
        return ("NEUTRAL", detail, per_ticker)
    except Exception as e:
        logger.debug("_classify_rsi_regime failed: %s", e)
        return ("UNKNOWN", "RSI regime computation failed", per_ticker)


def _per_ticker_today_pnl():
    """Observer 3a: realized P&L today, bucketed by ticker.
    Returns dict ticker -> float. Never raises.
    Reads paper_trades (long SELLs) AND short_trade_history (short COVERs).
    Short COVERs never appear in paper_trades — they live in short_trade_history.
    """
    try:
        today_str = _now_et().strftime("%Y-%m-%d")
        out = {}
        for t in paper_trades:
            if t.get("date") != today_str: continue
            if t.get("action") != "SELL": continue
            tk = t.get("ticker", "?")
            out[tk] = out.get(tk, 0.0) + (t.get("pnl", 0) or 0)
        for t in short_trade_history:
            if t.get("date") != today_str: continue
            tk = t.get("ticker", "?")
            out[tk] = out.get(tk, 0.0) + (t.get("pnl", 0) or 0)
        return out
    except Exception as e:
        logger.debug("_per_ticker_today_pnl failed: %s", e)
        return {}


def _classify_ticker_heat(per_ticker_pnl, per_ticker_rsi):
    """Observer 3b: build the ticker-heat lists for /mode and logs.
    Returns (red_list, extremes_list):
      red_list:       [(ticker, pnl), …] worst-first, pnl <= RED threshold
      extremes_list:  [(ticker, rsi, "OB"|"OS"), …] tickers in RSI extremes
    """
    try:
        red = [(tk, p) for tk, p in per_ticker_pnl.items()
               if p <= TICKER_RED_THRESHOLD_USD]
        red.sort(key=lambda x: x[1])   # most negative first

        extremes = []
        for tk, r in per_ticker_rsi.items():
            if r >= RSI_OVERBOUGHT: extremes.append((tk, r, "OB"))
            elif r <= RSI_OVERSOLD: extremes.append((tk, r, "OS"))
        extremes.sort(key=lambda x: x[1], reverse=True)   # highest RSI first
        return (red, extremes)
    except Exception as e:
        logger.debug("_classify_ticker_heat failed: %s", e)
        return ([], [])


def _compute_today_realized_pnl() -> float:
    """Realized P&L today across longs + shorts for the paper portfolio.
    Unrealized P&L is excluded on purpose — we want the number that
    drives the DAILY_LOSS_LIMIT halt, which is realized-only.

    Storage asymmetry (critical): long SELLs go to paper_trades with
    action="SELL"; short COVERs are written ONLY to short_trade_history
    (never to paper_trades). We must read both lists or short P&L is
    silently dropped from the DEFENSIVE-mode gate.
    """
    today_str = _now_et().strftime("%Y-%m-%d")
    pnl = 0.0
    for t in paper_trades:
        if t.get("date") == today_str and t.get("action") == "SELL":
            pnl += t.get("pnl", 0) or 0
    for t in short_trade_history:
        if t.get("date") == today_str:
            pnl += t.get("pnl", 0) or 0
    return pnl


def _today_pnl_breakdown() -> tuple:
    """Returns (sells_list, covers_list, total_pnl, wins, losses, n_trades)
    for today. Single source of truth used by EOD summaries, /dashboard,
    and weekly digest helpers.
    """
    today_str = _now_et().strftime("%Y-%m-%d")
    sells = [t for t in paper_trades
             if t.get("action") == "SELL" and t.get("date", "") == today_str]
    covers = [t for t in short_trade_history
              if t.get("date", "") == today_str]
    combined = list(sells) + list(covers)
    total = sum((t.get("pnl", 0) or 0) for t in combined)
    wins = sum(1 for t in combined if (t.get("pnl", 0) or 0) >= 0)
    losses = len(combined) - wins
    return (sells, covers, total, wins, losses, len(combined))


def get_current_mode(now_et=None) -> tuple:
    """Classify the current market mode. Returns (mode, reason, pnl_used).
    Priority: CLOSED > DEFENSIVE > time-of-day bucket.
    """
    if now_et is None:
        now_et = _now_et()

    # CLOSED: weekends and outside the same window scan_loop() skips.
    if now_et.weekday() >= 5:
        return (MarketMode.CLOSED, "weekend", 0.0)
    before_open = now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 35)
    after_close = now_et.hour >= 16 or (now_et.hour == 15 and now_et.minute >= 55)
    if before_open or after_close:
        return (MarketMode.CLOSED, "outside market hours", 0.0)

    # DEFENSIVE: realized P&L today is at or below half the daily loss limit.
    # Uses paper portfolio's P&L as the canonical risk signal (TP mirrors it).
    today_pnl = _compute_today_realized_pnl()
    half_limit = DAILY_LOSS_LIMIT / 2.0   # e.g. -500 / 2 = -250
    if today_pnl <= half_limit:
        reason = "realized P&L $%+.2f <= half limit $%+.2f" % (today_pnl, half_limit)
        return (MarketMode.DEFENSIVE, reason, today_pnl)

    # Time-of-day buckets.
    hm = now_et.hour * 60 + now_et.minute
    if hm < 11 * 60:
        return (MarketMode.OPEN,  "09:35-11:00 ET", today_pnl)
    if hm < 14 * 60:
        return (MarketMode.CHOP,  "11:00-14:00 ET", today_pnl)
    return (MarketMode.POWER,     "14:00-15:55 ET", today_pnl)


def _refresh_market_mode():
    """Recompute the cached mode + observers. Called at the top of every
    scan cycle. Pure observation — no side effects beyond updating module
    state and emitting log lines on transitions.
    """
    global _current_mode, _current_mode_reason, _current_mode_pnl, _current_mode_ts
    global _current_breadth, _current_breadth_detail
    global _current_rsi_regime, _current_rsi_detail, _current_rsi_per_ticker
    global _current_ticker_pnl, _current_ticker_red, _current_ticker_extremes

    prev_mode     = _current_mode
    prev_breadth  = _current_breadth
    prev_rsi      = _current_rsi_regime

    now_et = _now_et()

    # Core mode classifier.
    mode, reason, pnl = get_current_mode(now_et)
    _current_mode        = mode
    _current_mode_reason = reason
    _current_mode_pnl    = pnl
    _current_mode_ts     = now_et
    if mode != prev_mode:
        logger.info("MarketMode: %s -> %s (%s)", prev_mode, mode, reason)

    # Observers — each is individually safe and independent. A failure in
    # one never blocks the others or affects the core mode. All skipped
    # entirely when market is CLOSED (no meaningful data to classify).
    if mode == MarketMode.CLOSED:
        _current_breadth = "UNKNOWN"
        _current_breadth_detail = "market closed"
        _current_rsi_regime = "UNKNOWN"
        _current_rsi_detail = "market closed"
        _current_rsi_per_ticker = {}
        _current_ticker_pnl = {}
        _current_ticker_red = []
        _current_ticker_extremes = []
        return

    try:
        _current_breadth, _current_breadth_detail = _classify_breadth()
    except Exception:
        logger.exception("breadth observer failed (ignored)")
        _current_breadth, _current_breadth_detail = ("UNKNOWN", "observer crashed")
    if _current_breadth != prev_breadth:
        logger.info("MarketMode.breadth: %s -> %s (%s)",
                    prev_breadth, _current_breadth, _current_breadth_detail)

    try:
        rsi_label, rsi_detail, rsi_map = _classify_rsi_regime()
        _current_rsi_regime      = rsi_label
        _current_rsi_detail      = rsi_detail
        _current_rsi_per_ticker  = rsi_map
    except Exception:
        logger.exception("RSI observer failed (ignored)")
        _current_rsi_regime, _current_rsi_detail, _current_rsi_per_ticker = (
            "UNKNOWN", "observer crashed", {})
    if _current_rsi_regime != prev_rsi:
        logger.info("MarketMode.rsi: %s -> %s (%s)",
                    prev_rsi, _current_rsi_regime, _current_rsi_detail)

    try:
        _current_ticker_pnl = _per_ticker_today_pnl()
        red, extremes = _classify_ticker_heat(_current_ticker_pnl,
                                              _current_rsi_per_ticker)
        _current_ticker_red      = red
        _current_ticker_extremes = extremes
    except Exception:
        logger.exception("ticker-heat observer failed (ignored)")
        _current_ticker_pnl = {}
        _current_ticker_red = []
        _current_ticker_extremes = []


# Scan pause (Feature 8) — user-set via Telegram /pause /resume.
_scan_paused: bool = False
# Auto-idle flag — True when scan_loop is short-circuiting because it's
# outside market hours (weekends, pre-09:35, post-15:55). Updated at the
# top of every scan cycle, independent of market hours, so the dashboard
# banner reflects reality after the close instead of sticking on POWER.
_scan_idle_hours: bool = False
_regime_bullish = None          # None=unknown, True/False tracks last known regime
_last_exit_time: dict = {}     # ticker -> datetime (UTC) of last exit
_last_scan_time = None           # datetime (UTC), updated each scan cycle

# User config
user_config: dict = {"trading_mode": "paper"}

# v4.6.0: _paper_save_lock moved to paper_state.py.


# ============================================================
# NOTIFICATION ROUTING HELPER (Fix B)
# ============================================================


# ============================================================
# STATE PERSISTENCE \u2014 moved to paper_state.py in v4.6.0.
# Re-exported below so existing callsites keep working.
# ============================================================

# ============================================================
# v3.4.27 — PERSISTENT TRADE LOG (append-only JSONL)
# ============================================================
# Every closed trade (longs via close_position, shorts via
# close_short_position, and their TP counterparts) writes one JSON
# line to TRADE_LOG_FILE. The file lives on the Railway volume so it
# survives redeploys. Append-only — never rewritten, never rotated
# (a year of typical volume is ~3 MB).
#
# Schema (v1):
#   schema_version: int       — 1
#   bot_version:    str       — BOT_VERSION at write time
#   date:           str       — YYYY-MM-DD (trade close date, ET)
#   portfolio:      str       — "paper" | "tp"
#   ticker:         str
#   side:           str       — "LONG" | "SHORT"
#   shares:         int
#   entry_price:    float
#   exit_price:     float
#   entry_time:     str       — HH:MM:SS or ISO (as stored)
#   exit_time:      str       — ISO-8601 UTC
#   hold_seconds:   float|null
#   pnl:            float     — signed dollars
#   pnl_pct:        float     — signed percent (0.23 = +0.23%)
#   reason:         str       — EOD | TRAIL | STOP | RETRO_CAP |
#                               BULL_VACUUM | LORDS_LEFT |
#                               BULL_VACUUM[5m] | LORDS_LEFT[5m] |
#                               ...
#   entry_num:      int       — add-on index (longs only; 1 for shorts)
#   trail_active_at_exit:   bool|null
#   trail_stop_at_exit:     float|null
#   trail_anchor_at_exit:   float|null  (trail_high for long, trail_low for short)
#   hard_stop_at_exit:      float|null
#   effective_stop_at_exit: float|null  (trail_stop if armed, else hard stop)
#
# All writes are best-effort: any IO error is logged and swallowed so
# a broken disk never breaks trade execution.
# ============================================================

TRADE_LOG_SCHEMA_VERSION = 1
_trade_log_lock = threading.Lock()
_trade_log_last_error = None  # surfaced via /api/state for visibility


def _trade_log_snapshot_pos(pos):
    """Extract trail + stop diagnostic fields from a position dict.

    Accepts both long (trail_high) and short (trail_low) shapes.
    Returns a dict of None-safe values. Used at close time so the
    row captures exactly what the exit decision saw.
    """
    if not isinstance(pos, dict):
        return {
            "trail_active_at_exit": None,
            "trail_stop_at_exit": None,
            "trail_anchor_at_exit": None,
            "hard_stop_at_exit": None,
            "effective_stop_at_exit": None,
        }
    trail_active = bool(pos.get("trail_active", False))
    trail_stop = pos.get("trail_stop")
    # Either long (trail_high) or short (trail_low) populates anchor.
    trail_anchor = pos.get("trail_high", pos.get("trail_low"))
    hard_stop = pos.get("stop")
    effective_stop = (
        trail_stop if (trail_active and trail_stop is not None) else hard_stop
    )
    def _as_float(v):
        return float(v) if v is not None else None
    return {
        "trail_active_at_exit": trail_active,
        "trail_stop_at_exit": _as_float(trail_stop),
        "trail_anchor_at_exit": _as_float(trail_anchor),
        "hard_stop_at_exit": _as_float(hard_stop),
        "effective_stop_at_exit": _as_float(effective_stop),
    }


def trade_log_append(row):
    """Append a single closed-trade row to the persistent trade log.

    Best-effort: failures are logged and swallowed, never raised. The
    lock guards against the (rare) case of two close paths firing at
    once — writes are atomic at the OS level for small lines on
    POSIX, but the lock keeps log order deterministic and protects
    the _trade_log_last_error surface from races.
    """
    global _trade_log_last_error
    # Defensive: never let a caller ship missing required fields.
    required = ("ticker", "side", "pnl", "reason")
    for f in required:
        if f not in row:
            _trade_log_last_error = f"missing field: {f}"
            logger.warning("[TRADE_LOG] skipping row missing %s: %s",
                           f, row)
            return False
    full = {
        "schema_version": TRADE_LOG_SCHEMA_VERSION,
        "bot_version": BOT_VERSION,
    }
    full.update(row)
    line = json.dumps(full, default=str, separators=(",", ":"))
    try:
        with _trade_log_lock:
            # Open append+ with explicit newline to keep JSONL clean.
            with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        _trade_log_last_error = None
        return True
    except OSError as e:
        _trade_log_last_error = f"{type(e).__name__}: {e}"
        logger.error(
            "[TRADE_LOG] append failed (%s). Path=%s. Trade still "
            "executed — only persistence failed.",
            e, TRADE_LOG_FILE,
        )
        return False


def trade_log_read_tail(limit=500, since_date=None, portfolio=None):
    """Read the tail of the trade log, optionally filtered.

    Returns a list of dicts, newest-last (same order as on disk).
    Filtering is applied AFTER reading — trade log is small enough
    that this is fine. Failures return an empty list; never raises.

    Args:
      limit:       max rows to return (newest)
      since_date:  optional "YYYY-MM-DD"; only rows with date >= this
      portfolio:   optional "paper" or "tp" filter
    """
    if not os.path.exists(TRADE_LOG_FILE):
        return []
    try:
        with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.error("[TRADE_LOG] read failed: %s", e)
        return []
    rows = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            # Defensively skip corrupted lines rather than blowing up
            # the whole read.
            continue
    if since_date:
        rows = [r for r in rows if r.get("date", "") >= since_date]
    if portfolio:
        rows = [r for r in rows if r.get("portfolio") == portfolio]
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


# ============================================================
# TELEGRAM MESSAGING
# ============================================================
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
                    logger.warning("Telegram 429 — sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                time.sleep(0.3)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 2 ** attempt
                    logger.warning("Telegram 429 — sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                logger.error("Telegram send error (attempt %d): %s", attempt + 1, e)
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.error("Telegram send error (attempt %d): %s", attempt + 1, e)
                time.sleep(2 ** attempt)


# ============================================================
# v4.11.0 \u2014 health-pill error reporting
# ============================================================
# report_error() is the single entry point for "operator should be
# paged about this" events. It does three things, in order:
#   1. Logs via the existing logger so existing log surfaces still see
#      the event (file logs, stderr, the dashboard ring buffer prior
#      to v4.11.0 \u2014 the dashboard log tail card itself was deleted in
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


def _executor_inst(name: str):
    """Return the live executor instance for "val"/"gene", or None."""
    n = (name or "").strip().lower()
    if n == "val":
        return val_executor
    if n == "gene":
        return gene_executor
    return None


def _format_error_telegram(executor: str, code: str, summary: str, detail: str = "") -> str:
    """Format a Telegram error message respecting the \u226434 chars/line rule.

    Layout:
      \U0001f6a8 X \u00b7 CODE
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
    """Page-the-operator entry point. See module-level docstring above.

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
            inst = _executor_inst(ex)
            if inst is not None:
                inst._send_own_telegram(text)
            else:
                # Executor not enabled \u2014 fall back to main bot so the
                # operator still gets paged.
                send_telegram(text)
        else:
            send_telegram(text)
    except Exception:
        logger.exception("report_error: telegram dispatch failed")
        return False
    return True


# ============================================================
# YAHOO FINANCE DATA
# ============================================================
# Per-scan-cycle cache for 1-min bars. scan_loop() calls
# _clear_cycle_bar_cache() at the start of each cycle; any call to
# fetch_1min_bars within the same cycle reuses the cached response.
# This lets observers (RSI, breadth) read the same bars the scan loop
# already fetched without doubling network calls.
_cycle_bar_cache: dict = {}


def _clear_cycle_bar_cache():
    """Reset the per-cycle bar cache. Called at the top of scan_loop()."""
    _cycle_bar_cache.clear()


def fetch_1min_bars(ticker):
    """Fetch 1-min intraday bars from Yahoo Finance.

    Returns dict with keys: timestamps, opens, highs, lows, closes,
    volumes, current_price, pdc.  Returns None on failure.

    Results are cached per scan cycle (see _cycle_bar_cache).
    """
    cached = _cycle_bar_cache.get(ticker)
    if cached is not None:
        # Sentinel for negative cache (prior fetch failed): keep returning
        # None for the rest of the cycle rather than retrying.
        return cached if cached != "__FAILED__" else None

    t0 = time.time()
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/%s"
        "?interval=1m&range=1d&includePrePost=false" % ticker
    )
    try:
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=YAHOO_TIMEOUT) as resp:
            data = json.loads(resp.read())

        result = data.get("chart", {}).get("result")
        if not result:
            logger.debug("Yahoo %s: empty result (%.2fs)", ticker, time.time() - t0)
            _cycle_bar_cache[ticker] = "__FAILED__"
            return None
        r = result[0]
        meta = r.get("meta", {})
        quote = r.get("indicators", {}).get("quote", [{}])[0]
        timestamps = r.get("timestamp", [])

        if not timestamps:
            logger.debug("Yahoo %s: no timestamps (%.2fs)", ticker, time.time() - t0)
            _cycle_bar_cache[ticker] = "__FAILED__"
            return None

        logger.debug("Yahoo %s: %.2fs", ticker, time.time() - t0)
        out = {
            "timestamps": timestamps,
            "opens": quote.get("open", []),
            "highs": quote.get("high", []),
            "lows": quote.get("low", []),
            "closes": quote.get("close", []),
            "volumes": quote.get("volume", []),
            "current_price": meta.get("regularMarketPrice", 0),
            "pdc": (meta.get("previousClose")
                    or meta.get("chartPreviousClose")
                    or 0),
        }
        _cycle_bar_cache[ticker] = out
        return out
    except Exception as e:
        logger.debug("fetch_1min_bars %s failed: %s (%.2fs)", ticker, e, time.time() - t0)
        _cycle_bar_cache[ticker] = "__FAILED__"
        return None


def get_last_1min_close(ticker):
    """Return the close price of the most recently completed 1-min bar.

    Uses the existing Yahoo Finance fetch.  The last element in the closes
    array may be the currently-forming bar, so we prefer the second-to-last
    entry when available.  Returns None on any failure (fail-safe).
    """
    bars = fetch_1min_bars(ticker)
    if not bars:
        return None
    closes = [c for c in bars.get("closes", []) if c is not None]
    if len(closes) >= 2:
        return closes[-2]          # last completed bar
    if len(closes) == 1:
        return closes[-1]          # only one bar — best we have
    return None


# ============================================================
# FMP REAL-TIME QUOTES
# ============================================================
def get_fmp_quote(ticker):
    """Fetch real-time quote from FMP. Returns dict or None on error."""
    t0 = time.time()
    try:
        url = (
            "https://financialmodelingprep.com/stable/quote"
            "?symbol=%s&apikey=%s" % (ticker, FMP_API_KEY)
        )
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data and isinstance(data, list) and len(data) > 0:
            logger.debug("FMP %s: %.2fs", ticker, time.time() - t0)
            return data[0]
    except Exception as e:
        logger.warning("FMP quote error for %s: %s (%.2fs)", ticker, e, time.time() - t0)
    return None


# v4.0.3-beta — env-tunable staleness guard threshold. The old 1.5%
# fired for routine intraday moves on volatile names (OKLO, QBTS,
# LEU regularly drift >5% within a session) which killed every
# signal. 5% is a real "something's broken" guard, not a "normal
# volatility" guard.
OR_STALE_THRESHOLD = float(os.getenv("OR_STALE_THRESHOLD", "0.05") or "0.05")


def _or_price_sane(or_price, live_price, threshold=None):
    """Return True if OR price is within threshold of live price.

    threshold defaults to OR_STALE_THRESHOLD (env-configurable,
    5% by default). Pass an explicit value to override.
    """
    if threshold is None:
        threshold = OR_STALE_THRESHOLD
    if not or_price or not live_price:
        return True  # can't validate, allow
    diff = abs(or_price - live_price) / live_price
    return diff <= threshold


def _entry_bar_volume(volumes, lookback=5):
    """Pick the most recent closed bar's volume, walking back through
    null/zero entries that indicate the data source hasn't populated
    the bar yet (seen when Yahoo returns a fresh series where the last
    closed bar is still settling).

    Convention: volumes[-1] is the in-progress bar, volumes[-2] is the
    most recently closed bar. Start there and walk back up to
    `lookback` bars, returning the first non-null, positive value.

    Returns (vol, ready):
      - (vol, True)  when a valid bar was found
      - (0,   False) when every candidate bar was null/zero — caller
                     must treat this as DATA NOT READY, NOT as low-vol.

    Failure-closed: a DATA NOT READY result must cause the caller to
    skip the entry attempt. This keeps behavior no looser than baseline
    (a missing-data bar never entered a trade before this fix either).
    """
    if not volumes or len(volumes) < 2:
        return 0, False
    # Walk back from volumes[-2] (last closed bar) through `lookback`
    # prior bars. Index range: [-2, -3, ..., -2-(lookback-1)].
    for offset in range(2, 2 + lookback):
        if offset > len(volumes):
            break
        v = volumes[-offset]
        if v is not None and v > 0:
            return v, True
    return 0, False


# v3.4.21 — Stop cap for late/extended entries.
#
# Baseline stop = OR_High − $0.90 (long) or PDC + $0.90 (short). That
# anchor is appropriate when price breaks at the OR trigger, but on a
# bar that closes well past the level the baseline stop sits far below
# (or above) the entry, inflating risk. Example from v3.4.20: MSFT long
# entered at $425.93 vs OR_High $420.16, baseline stop $419.26 = $6.67
# risk = −1.56% on entry.
#
# Cap: stop distance must not exceed MAX_STOP_PCT of the entry price.
# Final stop = tighter of {baseline, entry ± MAX_STOP_PCT}.
# Invariant (locked design principle): cap can only TIGHTEN the stop,
# never loosen it — a stop closer to entry than baseline is always
# more conservative for both long and short.
MAX_STOP_PCT = 0.0075  # 0.75% max from entry

# v4.3.0 \u2014 Extended-entry guard.
#
# On 2026-04-24 12:42 CDT, META entered long at $677.06 while OR_High
# was $659.85 \u2014 entry was +2.61% above OR. All four gates
# (break/polarity/index/DI) were green, so the stop-cap kicked in and
# clamped the stop to entry \u2212 0.75% = $671.98. 32 min later
# HARD_EJECT_TIGER fired at -0.3% when DI+ wobbled. A capped stop on an
# entry already extended past the OR trigger has near-zero room for
# noise \u2192 predictable stop-out.
#
# ENTRY_EXTENSION_MAX_PCT rejects any long whose price is more than
# this % above OR_High (symmetric for shorts below OR_Low).
# ENTRY_STOP_CAP_REJECT rejects entries that would need stop capping
# \u2014 this is a second, narrower guard: the cap is itself a signal
# that the entry bar closed too far past the OR edge.
ENTRY_EXTENSION_MAX_PCT = float(os.getenv("ENTRY_EXTENSION_MAX_PCT", "1.5"))
ENTRY_STOP_CAP_REJECT = os.getenv("ENTRY_STOP_CAP_REJECT", "1") == "1"


# v3.4.25 — Breakeven ratchet (Stage 1)
# ----------------------------------------------------------------
# Once a position is in profit by BREAKEVEN_RATCHET_PCT, pull the
# stop to entry price (breakeven). This closes the gap between the
# fixed 0.75% entry cap and the 1% trail-arm threshold — without it,
# a short that moves +0.8% in our favor still has its stop pinned
# 0.75% above entry (i.e., 1.58% above current market), so a wick
# back would give back ~2x the current profit.
#
# Locked design preserved:
#   - MORE conservative than baseline, never looser. Breakeven is
#     always tighter than entry±0.75% by construction.
#   - Fail-closed: missing data → no ratchet, leave existing stop
#     alone.
#   - Trail interaction: if trail is already armed, ratchet is a
#     no-op (trail is ≥ as tight as breakeven already).
BREAKEVEN_RATCHET_PCT = 0.0050  # +0.50% profit arms breakeven


def _breakeven_long_stop(entry_price, current_price, current_stop,
                         arm_pct=BREAKEVEN_RATCHET_PCT):
    """Return the ratcheted long stop, or the existing stop unchanged.

    A long is in +arm_pct profit when current_price ≥ entry * (1+arm_pct).
    When armed, the stop pulls up to entry (breakeven). We return
    max(current_stop, entry) so the ratchet can only tighten.

    Returns (new_stop, armed). `armed` is True if the threshold is
    met, regardless of whether the stop actually moved (it may
    already be at or above entry).
    """
    arm_price = entry_price * (1.0 + arm_pct)
    if current_price < arm_price:
        return current_stop, False
    # Armed — stop can never go below entry (never looser).
    new_stop = round(max(current_stop, entry_price), 2)
    return new_stop, True


def _breakeven_short_stop(entry_price, current_price, current_stop,
                          arm_pct=BREAKEVEN_RATCHET_PCT):
    """Return the ratcheted short stop, or the existing stop unchanged.

    A short is in +arm_pct profit when current_price ≤ entry * (1−arm_pct).
    When armed, the stop pulls down to entry. We return
    min(current_stop, entry) so the ratchet can only tighten.
    """
    arm_price = entry_price * (1.0 - arm_pct)
    if current_price > arm_price:
        return current_stop, False
    new_stop = round(min(current_stop, entry_price), 2)
    return new_stop, True


def _capped_long_stop(or_high_val, entry_price, max_pct=MAX_STOP_PCT):
    """Compute long stop with 0.75%-from-entry cap.

    Returns (stop_price, capped, baseline_stop) — `capped` is True when
    the entry-relative floor was tighter than the OR baseline.
    """
    baseline = or_high_val - 0.90
    floor = entry_price * (1.0 - max_pct)
    # For longs, "tighter" = higher stop (closer to entry from below).
    final = max(baseline, floor)
    return round(final, 2), final > baseline, round(baseline, 2)


def _capped_short_stop(pdc_val, entry_price, max_pct=MAX_STOP_PCT):
    """Compute short stop with 0.75%-from-entry cap.

    Returns (stop_price, capped, baseline_stop). For shorts, "tighter"
    = lower stop (closer to entry from above).
    """
    baseline = pdc_val + 0.90
    ceiling = entry_price * (1.0 + max_pct)
    final = min(baseline, ceiling)
    return round(final, 2), final < baseline, round(baseline, 2)


def _validate_side_config_attrs() -> None:
    """Fail fast at module load if any SideConfig *_attr field references
    a name that doesn't exist in this module. Without this, a renamed
    module-level dict (e.g. positions -> open_positions) silently rots
    until the first entry of the day raises KeyError mid-session.
    """
    g = globals()
    for cfg in CONFIGS.values():
        for attr in (
            cfg.or_attr,
            cfg.positions_attr,
            cfg.daily_count_attr,
            cfg.daily_date_attr,
            cfg.trade_history_attr,
            cfg.capped_stop_fn_name,
        ):
            assert attr in g, (
                f"SideConfig({cfg.side.value}) references missing "
                f"global {attr!r} in trade_genius.py"
            )


_validate_side_config_attrs()


# v3.4.36 — Profit-Lock Ladder (peak-anchored give-back)
# ----------------------------------------------------------------
# Six-tier ratchet driven by peak gain %. Peak is trail_high for
# long, trail_low for short. v3.4.35's gain-anchored tiers (entry +
# X%) made the gap between peak and stop WIDEN as peak grew — the
# opposite of the trailing-stop instinct. v3.4.36 inverts this: the
# stop sits a shrinking % below peak, so the tighter the trade
# works, the less give-back is allowed.
#
#   Peak gain %  Long give-back  Short give-back  Phase
#   -----------  --------------  ---------------  -------
#   < 1.0%       initial stop    initial stop     Bullet
#   ≥ 1.0%      peak − 0.50%    peak + 0.50%     Arm
#   ≥ 2.0%      peak − 0.40%    peak + 0.40%     Lock
#   ≥ 3.0%      peak − 0.30%    peak + 0.30%     Tight
#   ≥ 4.0%      peak − 0.20%    peak + 0.20%     Tighter
#   ≥ 5.0%      peak − 0.10%    peak + 0.10%     Harvest
#
# Design:
#   - PEAK-ANCHORED: stop is always defined as a % below peak (for
#     long) or above peak (for short). As peak ratchets up, the stop
#     ratchets up with it; the gap between them shrinks at higher
#     tiers.
#   - ONE-WAY: the returned stop is always max(existing_trail, tier)
#     for longs / min(existing_trail, tier) for shorts — never
#     looser. If a pullback happens, trail_high doesn't move and the
#     stop holds exactly where it was.
#   - SUB-1% TIER: returns `initial_stop` (the OR-based structural
#     stop). Legacy positions without initial_stop fall back to
#     pos["stop"].
#   - NEVER LOOSER THAN INITIAL: final result is clamped by
#     max(tier_stop, initial_stop) for long — the structural stop is
#     a permanent floor. Mirrors with min(...) for short.
LADDER_TIERS_LONG = [
    # (peak_gain_trigger, give_back_pct_below_peak)
    (0.05, 0.0010),   # ≥ 5% → peak − 0.10% (Harvest)
    (0.04, 0.0020),   # ≥ 4% → peak − 0.20% (Tighter)
    (0.03, 0.0030),   # ≥ 3% → peak − 0.30% (Tight)
    (0.02, 0.0040),   # ≥ 2% → peak − 0.40% (Lock)
    (0.01, 0.0050),   # ≥ 1% → peak − 0.50% (Arm)
]
# v3.4.35 had a separate LADDER_HARVEST_FRACTION; v3.4.36 rolls that
# concept into the tier table (the ≥5% tier is just the tightest
# give-back). Alias kept so any external readers don't crash; value is
# now the ≥5% give-back fraction itself.
LADDER_HARVEST_FRACTION = 0.0010


def _ladder_stop_long(pos):
    """Return the profit-lock ladder stop for a long position.

    Uses pos["trail_high"] as the peak. Stop is peak − give_back%
    where give_back shrinks as peak grows. Below +1% peak, returns
    `initial_stop` (structural stop only). Falls back to pos["stop"]
    when initial_stop is absent (legacy positions).

    Never looser than `initial_stop` — returns max(tier_stop,
    initial_stop) so the structural floor is permanent.
    """
    entry = pos.get("entry_price") or 0.0
    if entry <= 0:
        return pos.get("stop", 0)
    peak = pos.get("trail_high", entry) or entry
    peak_gain_pct = (peak - entry) / entry
    initial = pos.get("initial_stop", pos.get("stop", 0))
    # Iterate highest tier first so first match wins.
    for trigger, give_back_pct in LADDER_TIERS_LONG:
        if peak_gain_pct >= trigger:
            tier_stop = peak * (1.0 - give_back_pct)
            return round(max(tier_stop, initial), 2)
    # Below 1% gain — structural stop only.
    return initial


def _ladder_stop_short(pos):
    """Return the profit-lock ladder stop for a short position.

    Mirror of _ladder_stop_long. Uses pos["trail_low"] as the peak
    (lowest price reached). Peak gain % = (entry − low) / entry.
    Stop is peak + give_back% where give_back shrinks as peak
    deepens. Never looser (higher) than `initial_stop`.
    """
    entry = pos.get("entry_price") or 0.0
    if entry <= 0:
        return pos.get("stop", 0)
    peak = pos.get("trail_low", entry) or entry
    peak_gain_pct = (entry - peak) / entry
    initial = pos.get("initial_stop", pos.get("stop", 0))
    for trigger, give_back_pct in LADDER_TIERS_LONG:
        if peak_gain_pct >= trigger:
            tier_stop = peak * (1.0 + give_back_pct)
            # Tighter = lower for short, so take min with initial.
            return round(min(tier_stop, initial), 2)
    return initial


# ============================================================
# v3.4.23 — Retro-cap: retighten existing positions
# ------------------------------------------------------------
# The cap (v3.4.21) only fired at entry. Positions that were open
# before the cap shipped, or positions whose stop somehow got past
# the cap, still carried a potentially-wide baseline stop. This helper
# walks every open long/short position and enforces the 0.75% cap
# relative to entry. When the trail is already armed it is left alone
# (trail is always tighter than a fixed 0.75% cap by construction).
# When the newly-capped stop has already been breached by market
# price, we force the exit now with reason=RETRO_CAP rather than wait
# for the next scan — the cap is a hard risk ceiling, not a hint.
# Designed to be safe to call repeatedly: cycle-idempotent.
# ============================================================

def _retighten_long_stop(ticker, pos, current_price,
                         force_exit=True):
    """Retighten a single long position's stop.

    Two layers (cap + breakeven ratchet), applied based on trail state.

    When trail is NOT armed (v3.4.23 + v3.4.25 behavior):
      1. 0.75% cap: floor = entry * (1 − MAX_STOP_PCT).
      2. Breakeven ratchet: once current ≥ entry * (1+0.50%), pull
         pos["stop"] up to entry.

    When trail IS armed (v3.4.26 new behavior):
      Cap layer is skipped — trail was designed to replace it.
      Ratchet still runs but acts on pos["trail_stop"] instead of
      pos["stop"], because once trail is armed, manage_positions uses
      trail_stop for exit decisions. If the trail armed on an
      unfavorable dip (trail_low close to entry, trail_stop below
      entry), the ratchet pulls the effective exit stop up to entry.
      Pure tighten — never loosens.

    Returns one of:
      ("already_tight", stop, None) — nothing tightens further.
      ("tightened", old_stop, new)  — cap tightened pos["stop"].
      ("ratcheted", old_stop, new)  — ratchet tightened pos["stop"].
      ("ratcheted_trail", old_ts, new_ts)
                                    — ratchet tightened trail_stop
                                      while trail is armed.
      ("exit", new_stop, None)      — new stop breached; exited with
                                      reason=RETRO_CAP.
    """
    entry_price = pos["entry_price"]

    # v3.4.26 — trail-armed branch. Ratchet acts on trail_stop.
    if pos.get("trail_active"):
        current_trail = pos.get("trail_stop")
        if current_trail is None:
            # No trail_stop yet (shouldn't happen once armed, but
            # fail-safe) — leave it to manage_positions on next tick.
            return ("already_tight", pos["stop"], None)
        # Only fire ratchet if we're at or above the +0.50% arm.
        arm_price = entry_price * (1.0 + BREAKEVEN_RATCHET_PCT)
        if current_price < arm_price:
            return ("already_tight", current_trail, None)
        # Pure tighten: trail floor never falls below entry once armed.
        new_trail = round(max(current_trail, entry_price), 2)
        if new_trail <= current_trail:
            return ("already_tight", current_trail, None)
        old_trail = current_trail
        pos["trail_stop"] = new_trail
        logger.info(
            "[BREAKEVEN] %s LONG trail_stop ratcheted to entry: "
            "$%.2f → $%.2f (entry=$%.2f, current=$%.2f, "
            "trail_active=True)",
            ticker, old_trail, new_trail, entry_price, current_price,
        )
        return ("ratcheted_trail", old_trail, new_trail)

    current_stop = pos["stop"]

    # Layer 1: 0.75% cap (v3.4.23).
    floor = round(entry_price * (1.0 - MAX_STOP_PCT), 2)
    capped_stop = max(current_stop, floor)  # tighter = higher for long

    # Layer 2: breakeven ratchet (v3.4.25). Stacks on top of cap —
    # breakeven is always ≥ (entry − 0.75%), so this only tightens.
    ratcheted_stop, armed = _breakeven_long_stop(
        entry_price, current_price, capped_stop,
    )

    new_stop = ratcheted_stop
    if new_stop <= current_stop:
        return ("already_tight", current_stop, None)

    old_stop = current_stop
    pos["stop"] = new_stop
    # Classify which layer caused the tighten — informative logging.
    if armed and new_stop > floor:
        status = "ratcheted"
        logger.info(
            "[BREAKEVEN] %s LONG stop ratcheted to entry: $%.2f → $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    else:
        status = "tightened"
        logger.info(
            "[RETRO_CAP] %s LONG stop tightened: $%.2f → $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    # If the market has already broken the new stop, exit now.
    if force_exit and current_price <= new_stop:
        logger.warning(
            "[RETRO_CAP] %s LONG already breached at tighten time "
            "(current=$%.2f ≤ new_stop=$%.2f) — exiting immediately.",
            ticker, current_price, new_stop,
        )
        close_position(ticker, current_price, reason="RETRO_CAP")
        return ("exit", new_stop, None)
    return (status, old_stop, new_stop)


def _retighten_short_stop(ticker, pos, current_price,
                          force_exit=True):
    """Retighten a single short position's stop (cap + breakeven).

    Same return shape as _retighten_long_stop. For shorts, "tighter" =
    lower stop (closer to entry from above).

    v3.4.26: when trail_active=True, cap is skipped but the breakeven
    ratchet runs against pos["trail_stop"] — manage_short_positions
    uses trail_stop for exit decisions once armed.
    """
    entry_price = pos["entry_price"]

    # v3.4.26 — trail-armed branch. Ratchet acts on trail_stop.
    if pos.get("trail_active"):
        current_trail = pos.get("trail_stop")
        if current_trail is None:
            return ("already_tight", pos["stop"], None)
        arm_price = entry_price * (1.0 - BREAKEVEN_RATCHET_PCT)
        if current_price > arm_price:
            return ("already_tight", current_trail, None)
        # For shorts, tighter = lower. Cap at entry from above.
        new_trail = round(min(current_trail, entry_price), 2)
        if new_trail >= current_trail:
            return ("already_tight", current_trail, None)
        old_trail = current_trail
        pos["trail_stop"] = new_trail
        logger.info(
            "[BREAKEVEN] %s SHORT trail_stop ratcheted to entry: "
            "$%.2f → $%.2f (entry=$%.2f, current=$%.2f, "
            "trail_active=True)",
            ticker, old_trail, new_trail, entry_price, current_price,
        )
        return ("ratcheted_trail", old_trail, new_trail)

    current_stop = pos["stop"]

    # Layer 1: 0.75% cap (v3.4.23).
    ceiling = round(entry_price * (1.0 + MAX_STOP_PCT), 2)
    capped_stop = min(current_stop, ceiling)  # tighter = lower for short

    # Layer 2: breakeven ratchet (v3.4.25).
    ratcheted_stop, armed = _breakeven_short_stop(
        entry_price, current_price, capped_stop,
    )

    new_stop = ratcheted_stop
    if new_stop >= current_stop:
        return ("already_tight", current_stop, None)

    old_stop = current_stop
    pos["stop"] = new_stop
    if armed and new_stop < ceiling:
        status = "ratcheted"
        logger.info(
            "[BREAKEVEN] %s SHORT stop ratcheted to entry: $%.2f → $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    else:
        status = "tightened"
        logger.info(
            "[RETRO_CAP] %s SHORT stop tightened: $%.2f → $%.2f "
            "(entry=$%.2f, current=$%.2f)",
            ticker, old_stop, new_stop, entry_price, current_price,
        )
    if force_exit and current_price >= new_stop:
        logger.warning(
            "[RETRO_CAP] %s SHORT already breached at tighten time "
            "(current=$%.2f ≥ new_stop=$%.2f) — exiting immediately.",
            ticker, current_price, new_stop,
        )
        close_short_position(ticker, current_price, "RETRO_CAP")
        return ("exit", new_stop, None)
    return (status, old_stop, new_stop)


def retighten_all_stops(force_exit=True, fetch_prices=True):
    """Retighten every open position's stop to the 0.75% cap.

    Returns a summary dict: {tightened: int, exited: int, no_op: int,
    already_tight: int, errors: int, details: list[dict]}

    Safe to call repeatedly — if all stops are already tight, it's a
    no-op. When fetch_prices is False, uses entry_price as a
    best-effort proxy for "current" (startup mode, before any scanner
    cycles have run).
    """
    # v3.4.25: separate counter for breakeven-ratchet tightenings, so
    # logging and /retighten output can distinguish cap vs ratchet.
    # v3.4.26: ratcheted_trail counts breakeven-ratchet tightenings
    # applied to trail_stop (when trail is armed).
    summary = {"tightened": 0, "ratcheted": 0, "ratcheted_trail": 0,
               "exited": 0, "no_op": 0, "already_tight": 0,
               "errors": 0, "details": []}

    def _current(ticker, fallback):
        if not fetch_prices:
            return fallback
        try:
            bars = fetch_1min_bars(ticker)
            if bars and bars.get("current_price"):
                return bars["current_price"]
        except Exception as e:
            logger.warning("[RETRO_CAP] %s fetch_1min_bars failed: %s",
                           ticker, e)
        return fallback

    # Longs (paper only)
    for ticker in list(positions.keys()):
        pos = positions.get(ticker)
        if not pos:
            continue
        try:
            cur = _current(ticker, pos["entry_price"])
            status, old, new = _retighten_long_stop(
                ticker, pos, cur, force_exit=force_exit,
            )
            key = "exited" if status == "exit" else status
            summary[key] = summary.get(key, 0) + 1
            summary["details"].append({
                "ticker": ticker, "side": "LONG",
                "status": status,
                "old_stop": old, "new_stop": new,
            })
        except Exception as e:
            summary["errors"] += 1
            # v4.11.0 \u2014 report_error: trading-path retighten failure.
            report_error(
                executor="main",
                code="RETRO_CAP_LONG_FAILED",
                severity="error",
                summary=f"Retro cap LONG failed: {ticker}",
                detail=f"{type(e).__name__}: {e}",
            )

    # Shorts (paper only)
    for ticker in list(short_positions.keys()):
        pos = short_positions.get(ticker)
        if not pos:
            continue
        try:
            cur = _current(ticker, pos["entry_price"])
            status, old, new = _retighten_short_stop(
                ticker, pos, cur, force_exit=force_exit,
            )
            key = "exited" if status == "exit" else status
            summary[key] = summary.get(key, 0) + 1
            summary["details"].append({
                "ticker": ticker, "side": "SHORT",
                "status": status,
                "old_stop": old, "new_stop": new,
            })
        except Exception as e:
            summary["errors"] += 1
            # v4.11.0 \u2014 report_error: trading-path retighten failure.
            report_error(
                executor="main",
                code="RETRO_CAP_SHORT_FAILED",
                severity="error",
                summary=f"Retro cap SHORT failed: {ticker}",
                detail=f"{type(e).__name__}: {e}",
            )

    if (summary["tightened"] or summary["ratcheted"]
            or summary["ratcheted_trail"] or summary["exited"]):
        logger.info(
            "[RETRO_CAP] cycle summary: %d tightened, %d ratcheted, "
            "%d trail-ratcheted, %d exited, %d already-tight, "
            "%d no-op",
            summary["tightened"], summary["ratcheted"],
            summary["ratcheted_trail"], summary["exited"],
            summary["already_tight"], summary["no_op"],
        )
    return summary


# ============================================================
# OR COLLECTION (Opening Range)
# ============================================================
def collect_or():
    """Collect Opening Range data at 09:35 ET.

    For each ticker: find bars in [09:30, 09:35) ET, record max high as OR_High
    and previous day close as PDC.
    """
    global or_collected_date
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date == today:
        logger.info("OR already collected for %s, skipping", today)
        return

    logger.info("Collecting Opening Range for %s ...", today)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    or_end = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    open_ts = int(market_open.timestamp())
    end_ts = int(or_end.timestamp())

    for ticker in TICKERS:
        try:
            bars = fetch_1min_bars(ticker)
            if not bars:
                logger.warning("OR: No bars for %s", ticker)
                continue

            # Filter bars in [09:30, 09:35) window
            max_high = None
            min_low = None
            for i, ts in enumerate(bars["timestamps"]):
                if open_ts <= ts < end_ts:
                    h = bars["highs"][i]
                    if h is None:
                        h = bars["closes"][i]
                    if h is not None:
                        if max_high is None or h > max_high:
                            max_high = h
                    lo = bars["lows"][i]
                    if lo is None:
                        lo = bars["closes"][i]
                    if lo is not None:
                        if min_low is None or lo < min_low:
                            min_low = lo

            if max_high is None:
                logger.warning("OR: No bars in [09:30,09:35) for %s", ticker)
                continue

            or_high[ticker] = max_high
            if min_low is not None:
                or_low[ticker] = min_low
            pdc[ticker] = bars["pdc"]

            # FMP cross-check: prefer tighter (smaller) OR range
            fmp_q = get_fmp_quote(ticker)
            if fmp_q:
                fmp_high = fmp_q.get("dayHigh")
                fmp_low = fmp_q.get("dayLow")
                fmp_pdc = fmp_q.get("previousClose")
                if fmp_high and fmp_high < or_high[ticker]:
                    pct = abs(fmp_high - or_high[ticker]) / or_high[ticker] * 100
                    if pct > 2:
                        logger.info("OR FMP adj %s High: %.2f->%.2f (%.1f%%)",
                                    ticker, or_high[ticker], fmp_high, pct)
                        or_high[ticker] = fmp_high
                if fmp_low and ticker in or_low and fmp_low > or_low[ticker]:
                    pct = abs(fmp_low - or_low[ticker]) / or_low[ticker] * 100
                    if pct > 2:
                        logger.info("OR FMP adj %s Low: %.2f->%.2f (%.1f%%)",
                                    ticker, or_low[ticker], fmp_low, pct)
                        or_low[ticker] = fmp_low
                if fmp_pdc and fmp_pdc > 0:
                    pdc[ticker] = fmp_pdc

            or_low_val = or_low.get(ticker, 0)
            logger.info("OR collected: %s OR_high=%.2f OR_low=%.2f PDC=%.2f",
                        ticker, or_high[ticker], or_low_val, pdc[ticker])
        except Exception as e:
            logger.error("OR collection error for %s: %s", ticker, e)

    # ------ Retry missing tickers (3 attempts, 60s apart) ------
    OR_RETRY_MAX = 3
    for attempt in range(1, OR_RETRY_MAX + 1):
        missing = [t for t in TICKERS if t not in or_high]
        if not missing:
            break
        logger.info("OR retry %d/%d for: %s", attempt, OR_RETRY_MAX,
                     ", ".join(missing))
        time.sleep(60)
        for ticker in missing:
            try:
                bars = fetch_1min_bars(ticker)
                if not bars:
                    continue
                max_high = None
                min_low = None
                for i, ts in enumerate(bars["timestamps"]):
                    if open_ts <= ts < end_ts:
                        h = bars["highs"][i]
                        if h is None:
                            h = bars["closes"][i]
                        if h is not None:
                            if max_high is None or h > max_high:
                                max_high = h
                        lo = bars["lows"][i]
                        if lo is None:
                            lo = bars["closes"][i]
                        if lo is not None:
                            if min_low is None or lo < min_low:
                                min_low = lo
                if max_high is None:
                    continue
                or_high[ticker] = max_high
                if min_low is not None:
                    or_low[ticker] = min_low
                pdc[ticker] = bars["pdc"]
                # FMP cross-check on retry too
                fmp_q = get_fmp_quote(ticker)
                if fmp_q:
                    fmp_high = fmp_q.get("dayHigh")
                    fmp_low = fmp_q.get("dayLow")
                    fmp_pdc = fmp_q.get("previousClose")
                    if fmp_high and fmp_high < or_high[ticker]:
                        or_high[ticker] = fmp_high
                    if fmp_low and ticker in or_low and fmp_low > or_low[ticker]:
                        or_low[ticker] = fmp_low
                    if fmp_pdc and fmp_pdc > 0:
                        pdc[ticker] = fmp_pdc
                logger.info("OR retry OK: %s OR_H=%.2f OR_L=%.2f",
                            ticker, or_high[ticker], or_low.get(ticker, 0))
            except Exception as e:
                logger.warning("OR retry failed for %s: %s", ticker, e)

    # ------ FMP fallback for anything still missing ------
    still_missing = [t for t in TICKERS if t not in or_high]
    for ticker in still_missing:
        try:
            fmp = get_fmp_quote(ticker)
            if fmp and fmp.get("dayHigh") and fmp.get("dayLow"):
                or_high[ticker] = fmp["dayHigh"]
                or_low[ticker] = fmp["dayLow"]
                if fmp.get("previousClose") and fmp["previousClose"] > 0:
                    pdc[ticker] = fmp["previousClose"]
                logger.warning("OR fallback to FMP for %s: high=%.2f low=%.2f",
                               ticker, fmp["dayHigh"], fmp["dayLow"])
        except Exception as e:
            logger.warning("OR FMP fallback failed for %s: %s", ticker, e)

    final_missing = [t for t in TICKERS if t not in or_high]
    if final_missing:
        logger.warning("OR FINAL: still missing after retries: %s",
                        ", ".join(final_missing))
        send_telegram("\u26a0\ufe0f OR missing after retries + FMP: %s"
                      % ", ".join(final_missing))

    or_collected_date = today
    save_paper_state()

    # Send summary
    lines = ["Opening Range Collected (%s):" % today]
    for t in TICKERS:
        if t in or_high:
            orl = or_low.get(t, 0)
            lines.append("  %s  OR_H=%.2f  OR_L=%.2f  PDC=%.2f"
                          % (t, or_high[t], orl, pdc.get(t, 0)))
        else:
            lines.append("  %s  MISSING" % t)
    send_telegram("\n".join(lines))


# ============================================================
# v3.4.34 — AVWAP fully removed
# ============================================================
# The AVWAP entry gates (check_entry, check_short_entry), the
# regime-change alert, the breadth observer (_classify_breadth),
# and the v3.2.0 dual-index 5-min AVWAP ejector (_dual_index_eject)
# were all superseded by the v3.4.28 Sovereign Regime Shield, which
# anchors on PDC via _sovereign_regime_eject. One anchor across
# entries, alerts, breadth, and ejects.
#
# Previously at this site: update_avwap(), _last_finalized_5min_close(),
# _dual_index_eject(). All callers migrated to pdc.get() + the 1-minute
# finalized-close helper below. Removed in v3.4.34.
# ============================================================


# ============================================================
# v3.4.28 — SOVEREIGN REGIME SHIELD (PDC-based eject)
# ============================================================
# Why: AVWAP is a rolling mean — it drifts intraday, so an AVWAP-
# cross eject can fire on slow sideways tape ("regime flim-flam")
# even though the true structural level (yesterday's close) is
# unchanged. PDC is a single static number per index per day, so
# a PDC cross is a hard structural break rather than a drift.
#
# Rule (same for both sides, mirrored):
#
#   Long  eject iff  SPY_1m_close  < SPY_PDC  AND QQQ_1m_close  < QQQ_PDC
#   Short eject iff  SPY_1m_close  > SPY_PDC  AND QQQ_1m_close  > QQQ_PDC
#
# Hysteresis (spec): divergence — one index above PDC, one below —
# means regime is UNCHANGED and no eject fires. We achieve this
# trivially by requiring the AND to hold on both closes.
#
# Bar cadence: previous FULLY-CLOSED 1-minute bar (the one ending
# at the most recent minute boundary), NOT the in-progress bar.
# Matches the spec: "wait for the 1-minute bar to finalize."
#
# Fail-closed: any missing input (no bars, no PDC, too few closes)
# → return False (do NOT eject). Locked design principle: fail-
# closed means stay in the trade; adaptive logic never loosens
# baseline, only tightens.
def _last_finalized_1min_close(ticker):
    """Close of the most recent FINALIZED 1-minute bar.

    fetch_1min_bars() returns the entire intraday series including
    the in-progress minute as the last element. We return the
    second-to-last close so the caller always sees a bar that is
    truly sealed (no more ticks can modify it).

    Returns None on insufficient data.
    """
    bars = fetch_1min_bars(ticker)
    if not bars:
        return None
    closes = [c for c in (bars.get("closes") or []) if c is not None]
    if len(closes) < 2:
        return None
    return closes[-2]


def _sovereign_regime_eject(side):
    """Dual-index 1m-close vs PDC eject gate with hysteresis.

    Args:
        side: 'long'  \u2192 True iff BOTH SPY_1m_close < SPY_PDC
                              AND QQQ_1m_close < QQQ_PDC
              'short' \u2192 True iff BOTH SPY_1m_close > SPY_PDC
                              AND QQQ_1m_close > QQQ_PDC

    Returns False (no eject) on ANY missing/ambiguous input,
    including the divergence case (SPY and QQQ on opposite sides
    of their respective PDCs). Both behaviors are intentional and
    enforce the hysteresis buffer from the spec.
    """
    if side not in ("long", "short"):
        return False

    spy_pdc = pdc.get("SPY")
    qqq_pdc = pdc.get("QQQ")
    if not spy_pdc or not qqq_pdc or spy_pdc <= 0 or qqq_pdc <= 0:
        # PDC not yet collected (pre-open cycle, or data fetch
        # failed). Stay-in-trade is the only safe default.
        return False

    spy_close = _last_finalized_1min_close("SPY")
    qqq_close = _last_finalized_1min_close("QQQ")
    if spy_close is None or qqq_close is None:
        return False  # <2 finalized 1-min bars yet

    if side == "long":
        # Eject longs only when BOTH indices close below PDC.
        # The AND naturally enforces the divergence hysteresis.
        return (spy_close < spy_pdc) and (qqq_close < qqq_pdc)
    else:
        # Mirror for shorts: BOTH above PDC.
        return (spy_close > spy_pdc) and (qqq_close > qqq_pdc)


# ============================================================
# v4.7.0 — Shared helpers for long/short entry symmetry
# ============================================================
def _ticker_today_realized_pnl(ticker: str) -> float:
    """Sum today's realized P&L for `ticker` from long+short closed trades."""
    pnl = sum(
        (t.get("pnl") or 0) for t in trade_history
        if t.get("ticker") == ticker and _is_today(t.get("exit_time_iso") or t.get("entry_time_iso", ""))
    )
    pnl += sum(
        (t.get("pnl") or 0) for t in short_trade_history
        if t.get("ticker") == ticker and _is_today(t.get("exit_time_iso") or t.get("entry_time_iso", ""))
    )
    return pnl


def _check_daily_loss_limit(ticker: str) -> bool:
    """Return True if entry should proceed; False if daily loss limit
    halts trading.

    Side effects on breach: sets _trading_halted=True, sets
    _trading_halted_reason, sends a Telegram alert. Mirrors the
    legacy block previously inlined in execute_entry only.
    """
    global _trading_halted, _trading_halted_reason

    if _trading_halted:
        logger.info("Trading halted — skipping entry for %s", ticker)
        return False

    now_et = _now_et()
    today_str = now_et.strftime("%Y-%m-%d")

    today_pnl = sum(
        (t.get("pnl") or 0) for t in paper_trades
        if t.get("date") == today_str and t.get("action") == "SELL"
    )
    today_pnl += sum(
        (t.get("pnl") or 0) for t in short_trade_history
        if _is_today(t.get("exit_time_iso") or "") and t.get("action") == "COVER"
    )

    for pos_ticker, pos in list(positions.items()):
        fmp = get_fmp_quote(pos_ticker)
        live_px = fmp.get("price", 0) if fmp else 0
        if live_px > 0:
            today_pnl += (live_px - pos["entry_price"]) * (pos.get("shares") or 0)

    for pos_ticker, pos in list(short_positions.items()):
        fmp = get_fmp_quote(pos_ticker)
        live_px = fmp.get("price", 0) if fmp else 0
        if live_px > 0:
            today_pnl += (pos["entry_price"] - live_px) * (pos.get("shares") or 0)

    logger.info("Daily P&L check: $%.2f (limit $%.2f)", today_pnl, DAILY_LOSS_LIMIT)
    if today_pnl <= DAILY_LOSS_LIMIT:
        _trading_halted = True
        pnl_fmt = "%+.2f" % today_pnl
        limit_fmt = "%.2f" % DAILY_LOSS_LIMIT
        _trading_halted_reason = "Daily loss limit hit: $%s" % pnl_fmt
        halt_msg = (
            "STOP Trading halted — daily loss limit hit\n"
            "Today P&L: $%s\n"
            "Limit: $%s\n"
            "No new entries until tomorrow."
        ) % (pnl_fmt, limit_fmt)
        send_telegram(halt_msg)
        # C-R4: daily-loss-limit forces every v5 track to LOCKED_FOR_DAY.
        try:
            v5_lock_all_tracks("daily_loss_limit")
        except Exception:
            logger.exception("v5_lock_all_tracks failed (daily loss limit)")
        return False

    return True


# ============================================================
# ENTRY CHECK
# ============================================================
# v4.9.0 \u2014 unified entry gate. The legacy long/short twins
# (_legacy_check_entry / _legacy_check_short_entry) were deleted; this
# single body is parameterized by SideConfig. Synthetic harness goldens
# enforce byte-equal behavior against the v4.8.2 baseline.
def check_breakout(ticker, side):
    """Side-parameterized entry gate.

    Returns (True, bars_dict) if all entry conditions for `side` are
    met, else (False, None).
    """
    cfg = CONFIGS[side]
    or_dict = globals()[cfg.or_attr]
    positions_dict = globals()[cfg.positions_attr]
    daily_count = globals()[cfg.daily_count_attr]
    capped_stop_fn = globals()[cfg.capped_stop_fn_name]

    if _trading_halted:
        return False, None
    if _scan_paused:
        return False, None

    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    # Timing gate: after 09:35 ET (OR window close + 2-bar confirm)
    market_open = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    if now_et < market_open:
        return False, None
    eod_time = now_et.replace(hour=15, minute=55, second=0, microsecond=0)
    if now_et >= eod_time:
        return False, None

    # Reset daily entry counts if new day
    if globals()[cfg.daily_date_attr] != today:
        daily_count.clear()
        globals()[cfg.daily_date_attr] = today

    # v5.7.0 \u2014 sovereign daily-loss kill switch. Once latched, every
    # entry path returns SKIP daily_loss_limit_hit until the next
    # session boundary. Existing open positions exit on their own
    # normal exits; this gate only blocks NEW entries.
    if _v570_kill_switch_active():
        _v561_log_skip(
            ticker=ticker, reason="daily_loss_limit_hit",
            ts_utc=_utc_now_iso(), gate_state=None,
        )
        return False, None

    # OR data available
    if ticker not in or_dict or ticker not in pdc:
        return False, None

    # Daily entry cap (max 5). v5.7.0 \u2014 bypassed for Ten Titans
    # when ENABLE_UNLIMITED_TITAN_STRIKES is True; Titan re-entry
    # is governed by the Strike 2+ Expansion Gate further down.
    _v570_titan = _v570_is_titan(ticker)
    _v570_unlimited = bool(ENABLE_UNLIMITED_TITAN_STRIKES) and _v570_titan
    if not _v570_unlimited:
        if daily_count.get(ticker, 0) >= 5:
            return False, None

    # Already in a position on this side for this ticker (paper)
    if ticker in positions_dict:
        return False, None

    # Re-entry cooldown: 15 min after any exit on this ticker
    last_exit = _last_exit_time.get(ticker)
    if last_exit:
        elapsed = (datetime.now(timezone.utc) - last_exit).total_seconds()
        if elapsed < 900:
            mins_left = int((900 - elapsed) / 60) + 1
            logger.info("SKIP %s [COOLDOWN] %dm left", ticker, mins_left)
            _v561_log_skip(ticker=ticker,
                           reason="COOLDOWN:%dm" % mins_left,
                           ts_utc=_utc_now_iso(), gate_state=None)
            return False, None

    # Per-ticker daily loss cap: skip if down > $50 on this ticker today (both sides)
    ticker_pnl_today = _ticker_today_realized_pnl(ticker)
    if ticker_pnl_today < -50.0:
        logger.info("SKIP %s [LOSS CAP] ticker P&L today: $%.2f", ticker, ticker_pnl_today)
        _v561_log_skip(ticker=ticker,
                       reason="LOSS_CAP:%.2f" % ticker_pnl_today,
                       ts_utc=_utc_now_iso(), gate_state=None)
        return False, None

    # Fetch current bar (Yahoo)
    bars = fetch_1min_bars(ticker)
    if not bars:
        return False, None

    current_price = bars["current_price"]
    # v4.1.1: a 0 or negative current_price (Yahoo has shipped 0.0 quotes
    # on thinly traded names during pre-market extensions) would bypass
    # every downstream sanity gate because those gates fail-open when
    # fed 0/None. Reject here.
    if not current_price or current_price <= 0:
        return False, None
    closes = [c for c in bars["closes"] if c is not None]
    last_close = closes[-1] if closes else current_price

    # FMP primary quote — override price and PDC if available
    fmp_q = get_fmp_quote(ticker)
    if fmp_q:
        fmp_price = fmp_q.get("price")
        if fmp_price and fmp_price > 0:
            current_price = fmp_price
            last_close = fmp_price
        fmp_pdc = fmp_q.get("previousClose")
        if fmp_pdc and fmp_pdc > 0:
            pdc[ticker] = fmp_pdc

    # OR sanity check: OR-edge must be within OR_STALE_THRESHOLD of live price.
    if not _or_price_sane(or_dict[ticker], current_price):
        pct = abs(or_dict[ticker] - current_price) / current_price * 100
        or_stale_skip_count[ticker] = or_stale_skip_count.get(ticker, 0) + 1
        logger.warning(
            "SKIP %s %s \u2014 %s $%.2f is %.1f%% from live $%.2f (stale?)",
            ticker, cfg.skip_label, cfg.or_side_label,
            or_dict[ticker], pct, current_price,
        )
        return False, None

    or_edge_val = or_dict[ticker]
    pdc_val_e = pdc[ticker]
    # 2-bar OR breakout/breakdown confirmation (Tiger 2.0).
    if cfg.side.is_long:
        price_break = _tiger_two_bar_long(closes, or_edge_val)
    else:
        price_break = _tiger_two_bar_short(closes, or_edge_val)
    polarity_ok = (
        current_price > pdc_val_e if cfg.side.is_long
        else current_price < pdc_val_e
    )

    volumes = bars.get("volumes", [])
    vol_pct = None
    vol_ok = False
    vol_ready_flag = True
    entry_bar_vol = 0.0
    avg_vol = 0.0
    if len(volumes) >= 5:
        valid_vols = [v for v in volumes[:-1] if v is not None and v > 0]
        avg_vol = sum(valid_vols) / len(valid_vols) if valid_vols else 0
        entry_bar_vol, vol_ready = _entry_bar_volume(volumes)
        vol_ready_flag = vol_ready
        if vol_ready and avg_vol > 0:
            vol_pct = (entry_bar_vol / avg_vol) * 100.0
            vol_ok = vol_pct >= 150.0

    # Volume confirmation: entry bar volume >= 1.5x session average.
    # Gated by TIGER_V2_REQUIRE_VOL (default False); Tiger 2.0 replaces
    # the vol filter with DI.
    if TIGER_V2_REQUIRE_VOL and len(volumes) >= 5:
        if not vol_ready_flag:
            logger.info("SKIP %s [DATA NOT READY] no closed bar with volume in last 5", ticker)
            if price_break:
                _record_near_miss(
                    ticker=ticker, side=cfg.log_side_label, reason="DATA_NOT_READY",
                    close=round(last_close, 2), level=round(or_edge_val, 2),
                    vol_bar=None, vol_avg=None, vol_pct=None,
                )
            return False, None
        if avg_vol > 0 and entry_bar_vol < avg_vol * 1.5:
            logger.info("SKIP %s [LOW VOL] entry bar %.0f vs avg %.0f", ticker, entry_bar_vol, avg_vol)
            if price_break:
                _record_near_miss(
                    ticker=ticker, side=cfg.log_side_label, reason="LOW_VOL",
                    close=round(last_close, 2), level=round(or_edge_val, 2),
                    vol_bar=int(entry_bar_vol), vol_avg=int(avg_vol),
                    vol_pct=round(vol_pct, 1) if vol_pct is not None else None,
                )
            return False, None

    # 2-bar break confirmation (kept as breakout precondition; the v5.6.0
    # G4 strict comparator below is the *permission* gate.)
    if not price_break:
        return False, None

    # v5.6.0 \u2014 Unified AVWAP permission gates (G1/G3/G4). G2 retired.
    # Index = QQQ only. Strict comparators (equality FAILs). AVWAP None
    # or pre-9:35 OR FAILs deterministically.
    qqq_bars = fetch_1min_bars("QQQ")
    if not qqq_bars:
        return False, None
    qqq_last = qqq_bars.get("current_price")
    qqq_avwap = _opening_avwap("QQQ")
    ticker_avwap = _opening_avwap(ticker)
    or_high_val = or_high.get(ticker)
    or_low_val = or_low.get(ticker)
    side_label = "LONG" if cfg.side.is_long else "SHORT"

    if cfg.side.is_long:
        g1 = v5.gate_g1_long(qqq_last, qqq_avwap)
        g3 = v5.gate_g3_long(current_price, ticker_avwap)
        g4 = v5.gate_g4_long(current_price, or_high_val)
        g4_threshold = or_high_val
    else:
        g1 = v5.gate_g1_short(qqq_last, qqq_avwap)
        g3 = v5.gate_g3_short(current_price, ticker_avwap)
        g4 = v5.gate_g4_short(current_price, or_low_val)
        g4_threshold = or_low_val

    # Forensic logging \u2014 legacy per-gate [V560-GATE] lines retained
    # so existing parsers keep working alongside the v5.6.1 richened
    # single-line emission below.
    _v560_log_gate(ticker, side_label, "G1", qqq_last, qqq_avwap, g1)
    _v560_log_gate(ticker, side_label, "G3", current_price, ticker_avwap, g3)
    _v560_log_gate(ticker, side_label, "G4", current_price, g4_threshold, g4)

    pass_flag = bool(g1 and g3 and g4)
    failed = []
    if not g1:
        failed.append("G1")
    if not g3:
        failed.append("G3")
    if not g4:
        failed.append("G4")
    reason_str = (",".join(failed) if failed else None)
    _gate_ts = _utc_now_iso()

    # v5.7.0 \u2014 strike accounting. Update session HOD/LOD with the
    # current print and figure out which strike this would be.
    _prev_hod, _prev_lod, _hod_break, _lod_break = (
        _v570_update_session_hod_lod(ticker, current_price)
    )
    _strike_num = _v570_strike_count(ticker, side_label) + 1
    _is_first = (_strike_num == 1)
    _is_titan_unlimited = (
        bool(ENABLE_UNLIMITED_TITAN_STRIKES) and _v570_is_titan(ticker)
    )

    if _is_first or not _is_titan_unlimited:
        # Strike 1 path (or non-Titan / flag off) \u2014 unchanged
        # v5.6.0 L-P1/S-P1 permission gates apply. [V560-GATE] is
        # emitted alongside [V570-STRIKE].
        _v561_log_v560_gate_rich(
            ticker=ticker, side=side_label, ts_utc=_gate_ts,
            ticker_price=current_price, ticker_avwap=ticker_avwap,
            index_price=qqq_last, index_avwap=qqq_avwap,
            or_high=or_high_val, or_low=or_low_val,
            g1=g1, g3=g3, g4=g4, pass_=pass_flag,
            reason=reason_str,
        )
        _gate_state = _v561_gate_state_dict(
            g1=g1, g3=g3, g4=g4, pass_=pass_flag,
            ticker_price=current_price, ticker_avwap=ticker_avwap,
            index_price=qqq_last, index_avwap=qqq_avwap,
            or_high=or_high_val, or_low=or_low_val,
        )
        _v570_log_strike(
            ticker=ticker, side=side_label, ts_utc=_gate_ts,
            strike_num=_strike_num, is_first=_is_first,
            hod=_prev_hod, lod=_prev_lod,
            hod_break=_hod_break, lod_break=_lod_break,
            expansion_gate_pass=False,
        )
        if not pass_flag:
            _v561_log_skip(
                ticker=ticker,
                reason="V560_GATE_BLOCK:%s" % ",".join(failed),
                ts_utc=_gate_ts, gate_state=_gate_state,
            )
            return False, None
    else:
        # v5.7.0 Strike 2+ path for Titans. [V560-GATE] is replaced
        # by the Expansion Gate (HOD/LOD break + IndexAVWAP). Strike
        # 1 gates do NOT apply on this path \u2014 the original L-P1/S-P1
        # check fired on the first entry; subsequent strikes ride the
        # trend continuation signal.
        _expansion_pass = _v570_expansion_gate_pass(
            side=side_label,
            current_price=current_price,
            prev_hod=_prev_hod, prev_lod=_prev_lod,
            index_price=qqq_last, index_avwap=qqq_avwap,
        )
        _gate_state = _v561_gate_state_dict(
            g1=None, g3=None, g4=None, pass_=_expansion_pass,
            ticker_price=current_price, ticker_avwap=ticker_avwap,
            index_price=qqq_last, index_avwap=qqq_avwap,
            or_high=or_high_val, or_low=or_low_val,
        )
        _v570_log_strike(
            ticker=ticker, side=side_label, ts_utc=_gate_ts,
            strike_num=_strike_num, is_first=False,
            hod=_prev_hod, lod=_prev_lod,
            hod_break=_hod_break, lod_break=_lod_break,
            expansion_gate_pass=_expansion_pass,
        )
        if not _expansion_pass:
            _v561_log_skip(
                ticker=ticker,
                reason="V570_EXPANSION_BLOCK",
                ts_utc=_gate_ts, gate_state=_gate_state,
            )
            return False, None

    # Tiger 2.0 DI gate: DI+(long) / DI-(short) must exceed threshold.
    di_plus, di_minus = tiger_di(ticker)
    di_value = di_plus if cfg.side.is_long else di_minus
    if di_value is None:
        logger.info(
            "SKIP %s [DI WARMUP] need %d+1 5m bars",
            ticker, DI_PERIOD,
        )
        _v561_log_skip(ticker=ticker, reason="DI_WARMUP",
                       ts_utc=_utc_now_iso(), gate_state=_gate_state)
        return False, None
    if di_value < TIGER_V2_DI_THRESHOLD:
        logger.info(
            "SKIP %s [%s] %.1f < %d",
            ticker, cfg.di_sign_label, di_value, TIGER_V2_DI_THRESHOLD,
        )
        _v561_log_skip(
            ticker=ticker,
            reason="DI_BELOW_THRESHOLD:%.1f<%d" % (di_value, TIGER_V2_DI_THRESHOLD),
            ts_utc=_utc_now_iso(), gate_state=_gate_state,
        )
        return False, None

    # v4.3.0 \u2014 Extended-entry guard. Reject when price is more than
    # ENTRY_EXTENSION_MAX_PCT past the OR edge (above for longs, below
    # for shorts).
    if or_edge_val and or_edge_val > 0:
        if cfg.side.is_long:
            extension_pct = (current_price - or_edge_val) / or_edge_val * 100.0
        else:
            extension_pct = (or_edge_val - current_price) / or_edge_val * 100.0
        if extension_pct > ENTRY_EXTENSION_MAX_PCT:
            logger.info(
                "SKIP %s [EXTENDED] price=$%.2f %s=$%.2f ext=%.2f%%",
                ticker, current_price, cfg.or_side_short_label,
                or_edge_val, extension_pct,
            )
            _v561_log_skip(
                ticker=ticker,
                reason="EXTENDED:%.2f%%" % extension_pct,
                ts_utc=_utc_now_iso(), gate_state=_gate_state,
            )
            return False, None

    # v4.3.0 \u2014 Stop-cap rejection. If the final stop would need to
    # be capped to entry \u00b1 MAX_STOP_PCT (baseline too loose), treat
    # as a late/extended bar and skip.
    if ENTRY_STOP_CAP_REJECT:
        cap_arg = or_edge_val if cfg.side.is_long else pdc_val_e
        _sp, _capped_flag, _base_stop = capped_stop_fn(cap_arg, current_price)
        if _capped_flag:
            logger.info(
                "SKIP %s [STOP_CAPPED] baseline=$%.2f requested_cap=$%.2f",
                ticker, _base_stop, _sp,
            )
            _v561_log_skip(
                ticker=ticker, reason="STOP_CAPPED",
                ts_utc=_utc_now_iso(), gate_state=_gate_state,
            )
            return False, None

    return True, bars


def paper_shares_for(price: float) -> int:
    """Dollar-sized paper order: floor(PAPER_DOLLARS_PER_ENTRY / price),
    min 1. Returns 0 only when price <= 0 (invalid).

    v3.4.45 — paper now sizes by notional like RH does, scaled to the
    $100k paper book (default $10k/entry vs RH's $1.5k/$25k). This
    fixes the old flat 10-share behavior that made $400 NVDA cost 80x
    more risk per entry than $5 QBTS.
    """
    if price <= 0:
        return 0
    return max(1, int(PAPER_DOLLARS_PER_ENTRY // price))


# ============================================================
# v3.5.0 — Paper-only entry path (RH path removed).
# ============================================================
# Paper remains on _trading_halted / execute_entry.


# ============================================================
# EXECUTE ENTRY (paper)
# ============================================================
def execute_breakout(ticker, current_price, side):
    """Side-parameterized entry executor.

    v4.9.0 \u2014 unified body. The legacy long/short twins were deleted;
    this single body is parameterized by SideConfig. The synthetic
    harness goldens enforce byte-equal Telegram + paper_log output
    against the v4.8.2 baseline.
    """
    global paper_cash
    cfg = CONFIGS[side]
    positions_dict = globals()[cfg.positions_attr]
    daily_count = globals()[cfg.daily_count_attr]
    capped_stop_fn = globals()[cfg.capped_stop_fn_name]

    # Daily loss limit (shared between long/short).
    if not _check_daily_loss_limit(ticker):
        return

    now_et = _now_et()
    limit_price = round(current_price + cfg.limit_offset, 2)
    or_dict = globals()[cfg.or_attr]
    if cfg.side.is_long:
        cap_arg = or_dict.get(ticker, current_price)
    else:
        cap_arg = pdc.get(ticker, current_price)
    stop_price, _stop_capped, _stop_baseline = capped_stop_fn(
        cap_arg, current_price
    )
    if _stop_capped:
        if cfg.side.is_long:
            logger.info(
                "%s stop capped: baseline=$%.2f -> capped=$%.2f (entry=$%.2f, %.2f%% cap)",
                ticker, _stop_baseline, stop_price, current_price, MAX_STOP_PCT * 100,
            )
        else:
            logger.info(
                "%s short stop capped: baseline=$%.2f -> capped=$%.2f (entry=$%.2f, %.2f%% cap)",
                ticker, _stop_baseline, stop_price, current_price, MAX_STOP_PCT * 100,
            )

    # Dollar-sized paper entry; shares scale with price.
    shares = paper_shares_for(current_price)
    notional = current_price * shares
    if shares <= 0:
        if cfg.side.is_long:
            logger.warning("[paper] skip %s \u2014 invalid price $%.2f",
                           ticker, current_price)
        else:
            logger.warning("[paper] skip short %s \u2014 invalid price $%.2f",
                           ticker, current_price)
        return

    # Long entry needs cash to buy; short entry credits cash on open.
    if cfg.side.is_long and notional > paper_cash:
        logger.info(
            "[paper] skip %s \u2014 insufficient cash (need $%.2f, have $%.2f)",
            ticker, notional, paper_cash,
        )
        return

    entry_num = daily_count.get(ticker, 0) + 1
    now_str = _now_cdt().strftime("%H:%M:%S")
    now_hhmm = _now_cdt().strftime("%H:%M CDT")
    now_date = now_et.strftime("%Y-%m-%d")

    _entry_ts_utc = _utc_now_iso()
    _entry_id = _v561_compose_entry_id(ticker, _entry_ts_utc)
    # v5.7.0 \u2014 record this entry against the per-ticker per-side
    # strike counter and stash strike_num on the position so the
    # paired [TRADE_CLOSED] can echo it back.
    _v570_side_label = "LONG" if cfg.side.is_long else "SHORT"
    try:
        _v570_strike_num = _v570_record_entry(ticker, _v570_side_label)
    except Exception:
        _v570_strike_num = 1
    pos = {
        "entry_price": current_price,
        "shares": shares,
        "stop": stop_price,
        "initial_stop": stop_price,
        "trail_active": False,
        cfg.trail_peak_attr: current_price,
        "entry_count": entry_num,
        "entry_time": now_str,
        "entry_ts_utc": _entry_ts_utc,
        "entry_id": _entry_id,
        "strike_num": _v570_strike_num,
        "date": now_date,
        "pdc": pdc.get(ticker, 0),
    }
    if cfg.side.is_short:
        pos["side"] = "SHORT"
        pos["trail_stop"] = None
    positions_dict[ticker] = pos
    daily_count[ticker] = entry_num
    # v5.6.1 D4 \u2014 [ENTRY] line with entry_id for replay pairing.
    try:
        _v561_log_entry(
            ticker=ticker,
            side=_v570_side_label,
            entry_id=_entry_id,
            entry_ts_utc=_entry_ts_utc,
            entry_price=float(current_price),
            qty=int(shares),
            strike_num=int(_v570_strike_num),
        )
    except Exception as _e:
        logger.warning("[V561-ENTRY] emit error %s: %s", ticker, _e)

    # Paper accounting: long debits, short credits.
    paper_cash += cfg.entry_cash_delta(shares, current_price)

    # Long BUYs are appended to paper_trades / paper_all_trades; short
    # opens are intentionally NOT appended (short_trade_history is the
    # source of truth for shorts and avoids double-counting on /trades).
    if cfg.side.is_long:
        trade = {
            "action": "BUY",
            "ticker": ticker,
            "price": current_price,
            "limit_price": limit_price,
            "shares": shares,
            "cost": notional,
            "stop": stop_price,
            "entry_num": entry_num,
            "time": now_hhmm,
            "date": now_date,
        }
        paper_trades.append(trade)
        paper_all_trades.append(trade)

    paper_log(
        "%s %s %d @ $%.2f (limit $%.2f) stop=$%.2f entry#%d"
        % (cfg.paper_log_entry_verb, ticker, shares, current_price,
           limit_price, stop_price, entry_num)
    )

    # v5.1.2 \u2014 emit forensic entry snapshot. Strictly additive: this
    # logger.info call goes nowhere observable to the synthetic
    # harness (recorder only captures send_telegram / paper_log /
    # _emit_signal / trade_log_append / save_paper_state, so the
    # byte-equal goldens stay green).
    try:
        bid_v, ask_v = _v512_quote_snapshot(ticker)
        equity_v = paper_cash + sum(
            float(p.get("entry_price", 0.0)) * int(p.get("shares", 0))
            for p in positions.values()
        )
        open_pos = len(positions) + len(short_positions)
        # Exposure as % of equity (sum of long notional only \u2014
        # shorts net to credit). Guard against div-by-zero.
        long_notional = sum(
            float(p.get("entry_price", 0.0)) * int(p.get("shares", 0))
            for p in positions.values()
        )
        expo_pct = (long_notional / equity_v * 100.0) if equity_v > 0 else 0.0
        # Drawdown is rough \u2014 we don't track high-water-mark in
        # paper_state so report 0 unless caller wants more later.
        dd_pct = 0.0
        _v512_log_entry_extension(
            ticker,
            bid=bid_v, ask=ask_v,
            cash=round(paper_cash, 2),
            equity=round(equity_v, 2),
            open_positions=open_pos,
            total_exposure_pct=round(expo_pct, 4),
            current_drawdown_pct=dd_pct,
        )
    except Exception as e:
        logger.warning("[V510-ENTRY] snapshot error %s: %s", ticker, e)

    or_edge_e = or_dict.get(ticker, 0)
    pdc_e = pdc.get(ticker, 0)
    SEP_E = "\u2500" * 34
    stop_label = (
        cfg.stop_capped_label if _stop_capped else cfg.stop_baseline_label
    )
    if cfg.side.is_long:
        sig_lines = "Signal : ORB Breakout \u2191\n"
        sig_lines += "  1m close > OR High \u2713\n"
        sig_lines += "  Price > PDC \u2713\n"
        sig_lines += "  SPY > PDC \u2713\n"
        sig_lines += "  QQQ > PDC \u2713\n"
        msg = (
            "\U0001f4c8 LONG ENTRY %s  #%d\n"
            "%s\n"
            "Price  : $%.2f  (limit $%.2f)\n"
            "Shares : %d   Cost: $%s\n"
            "Stop   : $%.2f  (%s)\n"
            "OR High: $%.2f   PDC: $%.2f\n"
            "%s"
            "Time   : %s\n"
            "%s"
        ) % (ticker, entry_num, SEP_E,
             current_price, limit_price,
             shares, format(notional, ",.2f"),
             stop_price, stop_label, or_edge_e, pdc_e, sig_lines, now_hhmm, SEP_E)
    else:
        sig_lines = "Signal   : Wounded Buffalo \u2193\n"
        sig_lines += "  1m close < OR Low \u2713\n"
        sig_lines += "  Price < PDC \u2713\n"
        sig_lines += "  SPY < PDC \u2713\n"
        sig_lines += "  QQQ < PDC \u2713\n"
        msg = (
            "\U0001fa78 SHORT ENTRY #%d\n"
            "%s\n"
            "Ticker   : %s\n"
            "Entry    : $%.2f (limit)\n"
            "Shares   : %d   Proceeds: $%s\n"
            "Stop     : $%.2f (%s)\n"
            "OR Low   : $%.2f\n"
            "PDC      : $%.2f\n"
            "%s"
            "Time     : %s\n"
            "%s"
        ) % (entry_num, SEP_E, ticker, current_price,
             shares, format(notional, ",.2f"),
             stop_price, stop_label, or_edge_e, pdc_e, sig_lines, now_hhmm, SEP_E)
    send_telegram(msg)

    save_paper_state()

    _emit_signal({
        "kind": cfg.entry_signal_kind,
        "ticker": ticker,
        "price": float(current_price),
        "reason": cfg.entry_signal_reason,
        "timestamp_utc": _utc_now_iso(),
        "main_shares": int(shares),
    })


# ============================================================
# CLOSE POSITION
# ============================================================
def close_breakout(ticker, price, side, reason="STOP"):
    """Side-parameterized close.

    v4.9.0 \u2014 unified body. The legacy long/short twins were deleted;
    this single body is parameterized by SideConfig. Synthetic-harness
    goldens enforce byte-equal Telegram + paper_log + trade_log output
    against the v4.8.2 baseline.
    """
    global paper_cash
    cfg = CONFIGS[side]
    positions_dict = globals()[cfg.positions_attr]
    history_list = globals()[cfg.trade_history_attr]

    if ticker not in positions_dict:
        return

    _last_exit_time[ticker] = datetime.now(timezone.utc)

    pos = positions_dict.pop(ticker)
    entry_price = pos["entry_price"]
    shares = pos["shares"]
    pnl_val = cfg.realized_pnl(entry_price, price, shares)
    if entry_price:
        if cfg.side.is_long:
            pnl_pct = (price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - price) / entry_price * 100
    else:
        pnl_pct = 0
    now_et = _now_et()
    now_hhmm = _now_cdt().strftime("%H:%M CDT")
    now_date = now_et.strftime("%Y-%m-%d")

    entry_time_str = pos.get("entry_time", "")
    entry_hhmm = _to_cdt_hhmm(entry_time_str) if entry_time_str else ""

    # Paper accounting: long credits sale proceeds, short debits cover cost.
    notional = price * shares  # "proceeds" for long, "cover_total" for short
    paper_cash += cfg.close_cash_delta(shares, price)

    # Long SELLs are appended to paper_trades / paper_all_trades; short
    # COVERs are intentionally NOT appended (short_trade_history is the
    # source of truth so /trades doesn't double-count).
    if cfg.side.is_long:
        trade = {
            "action": "SELL",
            "ticker": ticker,
            "price": price,
            "shares": shares,
            "pnl": round(pnl_val, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "entry_price": entry_price,
            "time": now_hhmm,
            "date": now_date,
        }
        paper_trades.append(trade)
        paper_all_trades.append(trade)

    history_record = {
        "ticker": ticker,
        "side": cfg.history_side_label,
        "action": cfg.paper_log_close_verb,
        "shares": shares,
        "entry_price": entry_price,
        "exit_price": price,
        "pnl": round(pnl_val, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "entry_time": entry_hhmm,
        "exit_time": now_hhmm,
        "entry_time_iso": pos.get("entry_ts_utc") or entry_time_str,
        "exit_time_iso": _utc_now_iso(),
        "entry_num": pos.get("entry_count", 1),
        "date": now_date,
    }
    history_list.append(history_record)
    if len(history_list) > TRADE_HISTORY_MAX:
        history_list[:] = history_list[-TRADE_HISTORY_MAX:]

    # v5.2.0 \u2014 mirror live exit decision to all shadow configs. Same
    # ticker/price/reason as the live close, so shadow P&L tracks the
    # exact same exit logic. Failure-tolerant.
    try:
        _v520_close_shadow_all(ticker, price, reason)
    except Exception as e:
        logger.warning("[V520-SHADOW-PNL] close hook %s: %s", ticker, e)

    # Persistent trade log (paper close).
    _entry_iso = pos.get("entry_ts_utc") or entry_time_str or ""
    _hold_s = None
    try:
        if _entry_iso:
            _ent_dt = datetime.fromisoformat(_entry_iso)
            if _ent_dt.tzinfo is None:
                _ent_dt = _ent_dt.replace(tzinfo=timezone.utc)
            _hold_s = (datetime.now(timezone.utc) - _ent_dt).total_seconds()
    except (TypeError, ValueError):
        _hold_s = None
    _log_row = {
        "date": now_date,
        "portfolio": "paper",
        "ticker": ticker,
        "side": cfg.log_side_label,
        "shares": int(shares),
        "entry_price": float(entry_price),
        "exit_price": float(price),
        "entry_time": entry_time_str,
        "exit_time": _utc_now_iso(),
        "hold_seconds": _hold_s,
        "pnl": round(pnl_val, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "entry_num": int(pos.get("entry_count", 1)),
    }
    _log_row.update(_trade_log_snapshot_pos(pos))
    trade_log_append(_log_row)

    paper_log("%s %s %d @ $%.2f reason=%s pnl=$%.2f (%.1f%%)"
              % (cfg.paper_log_close_verb, ticker, shares, price,
                 reason, pnl_val, pnl_pct))

    # v5.6.1 D4 \u2014 [TRADE_CLOSED] lifecycle line. Pairs to [ENTRY] via
    # entry_id. Reason maps the legacy short token to the spec'd
    # canonical exit_reason vocabulary (stop|target|time|eod|manual).
    # v5.7.1 \u2014 also passes through the Bison/Buffalo Titan exit
    # vocabulary (hard_stop_2c|be_stop|ema_trail|velocity_fuse).
    try:
        _entry_id_close = pos.get("entry_id") or _v561_compose_entry_id(
            ticker, pos.get("entry_ts_utc") or "")
        _reason_lc = str(reason or "").lower()
        _v571_reasons = {
            "hard_stop_2c", "be_stop", "ema_trail", "velocity_fuse",
        }
        if _reason_lc in _v571_reasons:
            _exit_reason = _reason_lc
        elif "trail" in _reason_lc or "stop" in _reason_lc:
            _exit_reason = "stop"
        elif "target" in _reason_lc or "tp" in _reason_lc:
            _exit_reason = "target"
        elif "eod" in _reason_lc or "close" in _reason_lc:
            _exit_reason = "eod"
        elif "time" in _reason_lc or "shield" in _reason_lc:
            _exit_reason = "time"
        elif "manual" in _reason_lc:
            _exit_reason = "manual"
        else:
            _exit_reason = _reason_lc or "manual"
        _v561_log_trade_closed(
            ticker=ticker,
            side=("LONG" if cfg.side.is_long else "SHORT"),
            entry_id=_entry_id_close,
            entry_ts_utc=(pos.get("entry_ts_utc") or entry_time_str or ""),
            entry_price=float(entry_price or 0.0),
            exit_ts_utc=_utc_now_iso(),
            exit_price=float(price),
            exit_reason=_exit_reason,
            qty=int(shares),
            pnl_dollars=float(pnl_val),
            pnl_pct=float(pnl_pct),
            hold_seconds=int(_hold_s) if _hold_s is not None else 0,
            strike_num=int(pos.get("strike_num") or 1),
        )
    except Exception as _e:
        logger.warning("[V561-TRADE-CLOSED] emit error %s: %s", ticker, _e)

    exit_emoji_glyph = "\u2705" if pnl_val >= 0 else "\u274c"
    entry_total_val = round(entry_price * shares, 2)
    SEP_X = "\u2500" * 34
    reason_label = REASON_LABELS.get(reason, reason)
    if reason == "TRAIL":
        peak = pos.get(cfg.trail_peak_attr, price)
        t_dist = max(round(peak * 0.010, 2), 1.00)
        reason_label = "\U0001f3af Trail Stop (1.0%% / $%.2f)" % t_dist
    if cfg.side.is_long:
        msg = (
            "%s EXIT %s\n"
            "%s\n"
            "Shares : %d\n"
            "Entry  : $%.2f  \u2192  $%.2f\n"
            "Cost   : $%s  \u2192  $%s\n"
            "P&L    : $%+.2f  (%+.1f%%)\n"
            "Reason : %s\n"
            "In: %s   Out: %s\n"
            "%s"
        ) % (exit_emoji_glyph, ticker, SEP_X,
             shares, entry_price, price,
             format(entry_total_val, ",.2f"), format(notional, ",.2f"),
             pnl_val, pnl_pct, reason_label, entry_hhmm, now_hhmm, SEP_X)
    else:
        msg = (
            "%s SHORT CLOSED\n"
            "%s\n"
            "Ticker : %s\n"
            "Shares : %d\n"
            "Entry  : $%.2f  (total $%s)\n"
            "Cover  : $%.2f  (total $%s)\n"
            "P&L    : $%+.2f  (%+.1f%%)\n"
            "Reason : %s\n"
            "In: %s   Out: %s\n"
            "%s"
        ) % (exit_emoji_glyph, SEP_X, ticker, shares,
             entry_price, format(entry_total_val, ",.2f"),
             price, format(notional, ",.2f"),
             pnl_val, pnl_pct, reason_label, entry_hhmm, now_hhmm, SEP_X)
    send_telegram(msg)

    save_paper_state()

    _emit_signal({
        "kind": cfg.exit_signal_kind,
        "ticker": ticker,
        "price": float(price),
        "reason": reason,
        "timestamp_utc": _utc_now_iso(),
        "main_shares": int(shares),
    })


# ============================================================
# MANAGE POSITIONS (stop + trail logic)
# ============================================================
def manage_positions():
    """Check stops and update trailing stops for all open positions."""
    tickers_to_close = []

    # v3.4.23 — enforce 0.75% entry cap on every open long position
    # before the regular stop/trail pass. This catches pre-cap positions
    # and any position whose stored stop has drifted wider than the cap.
    # Also fires immediate exit on positions that have already breached
    # the retro-tightened stop. Idempotent — fast when everything is
    # already tight.
    retighten_all_stops(force_exit=True, fetch_prices=True)

    # ── Sovereign Regime Shield (v3.4.28) ────────────────────────────────────
    # Exit all longs ONLY when BOTH SPY and QQQ have a finalized 1-min close
    # BELOW their respective Prior Day Close (PDC). PDC is one static price
    # per day — a cross of it is a structural break, not intraday drift.
    # AND-logic enforces divergence hysteresis: if only one index is below
    # PDC (or data is missing), regime is UNCHANGED. See v3.4.28 CHANGELOG.
    lords_left = _sovereign_regime_eject("long")

    for ticker in list(positions.keys()):
        bars = fetch_1min_bars(ticker)
        if not bars:
            continue

        current_price = bars["current_price"]
        pos = positions[ticker]

        # v3.4.35 — Stop hit. "TRAIL" when the ladder has ratcheted past
        # the initial structural stop (capital already safe), else "STOP"
        # (initial structural stop hit with no profit locked).
        if current_price <= pos["stop"]:
            # Derive TRAIL vs STOP from whether the stop has actually
            # ratcheted above entry (i.e. capital was locked in). The
            # previous `pos.get("trail_active")` flag was set true the
            # first time peak_gain hit +1 % and was never unset — so a
            # position that went +1 %, came back, and hit the *initial*
            # structural stop was still attributed as "TRAIL" even
            # though no profit was locked. Derive from stop level.
            reason = "TRAIL" if pos["stop"] > pos["entry_price"] else "STOP"
            tickers_to_close.append((ticker, current_price, reason))
            continue

        # ── Sovereign Regime Shield: BOTH SPY+QQQ 1m_close < PDC ─────────────
        if lords_left:
            tickers_to_close.append((ticker, current_price, "LORDS_LEFT"))
            continue

        # ── Eye of the Tiger: "The Red Candle" — lost Daily Polarity ─────────
        # Fires when 1-min confirmed close < day open OR < PDC
        closes = [c for c in bars.get("closes", []) if c is not None]
        ticker_1min_close = closes[-2] if len(closes) >= 2 else (closes[-1] if closes else current_price)
        opens = [o for o in bars.get("opens", []) if o is not None]
        day_open = opens[0] if opens else None
        pos_pdc = pos.get("pdc") or pos.get("prev_close")
        lost_polarity = False
        if day_open is not None and ticker_1min_close < day_open:
            lost_polarity = True
        if pos_pdc and ticker_1min_close < pos_pdc:
            lost_polarity = True
        if lost_polarity:
            tickers_to_close.append((ticker, current_price, "RED_CANDLE"))
            continue

        entry_price = pos["entry_price"]

        # v3.4.35 — Profit-Lock Ladder replaces the 1%/$1 armed-trail.
        # Update peak (trail_high) every tick — ladder reads this.
        if current_price > pos.get("trail_high", entry_price):
            pos["trail_high"] = current_price
        peak = pos["trail_high"]
        peak_gain_pct = (peak - entry_price) / entry_price if entry_price > 0 else 0.0

        # Compute ladder stop; ratchet pos["stop"] upward only.
        ladder_stop = _ladder_stop_long(pos)
        if ladder_stop > pos.get("stop", 0):
            old_stop = pos.get("stop", 0)
            pos["stop"] = ladder_stop
            logger.info(
                "[LADDER] %s LONG stop ratcheted $%.2f \u2192 $%.2f "
                "(peak=$%.2f, +%.2f%%)",
                ticker, old_stop, ladder_stop, peak, peak_gain_pct * 100,
            )

        # Arm cosmetic trail_active / trail_stop once past the 1% gate
        # (Bullet phase ends). Keeps /api/state + exit-reason attribution
        # (TRAIL vs STOP in _finalize_pos) working.
        if peak_gain_pct >= 0.01:
            if not pos.get("trail_active"):
                pos["trail_active"] = True
                logger.info(
                    "Trail armed for %s at $%.2f (+%.2f%% peak) — ladder active",
                    ticker, current_price, peak_gain_pct * 100,
                )
            pos["trail_stop"] = pos["stop"]

        # Exit when current price crosses the ladder stop.
        if current_price <= pos["stop"]:
            # Derive TRAIL vs STOP from whether the stop has actually
            # ratcheted above entry (i.e. capital was locked in). The
            # previous `pos.get("trail_active")` flag was set true the
            # first time peak_gain hit +1 % and was never unset — so a
            # position that went +1 %, came back, and hit the *initial*
            # structural stop was still attributed as "TRAIL" even
            # though no profit was locked. Derive from stop level.
            reason = "TRAIL" if pos["stop"] > pos["entry_price"] else "STOP"
            tickers_to_close.append((ticker, current_price, reason))
            continue

    # Close positions outside the loop to avoid mutation during iteration
    for ticker, price, reason in tickers_to_close:
        close_position(ticker, price, reason)


# ============================================================
# CLOSE TP POSITION (independent TP long close)
# ============================================================


# ============================================================
# MANAGE TP POSITIONS (independent stop + trail logic)
# ============================================================



# ============================================================
# MANAGE SHORT POSITIONS (stop + trail logic)
# ============================================================
def manage_short_positions():
    """Check stops and trailing stops for all open short positions."""
    global short_positions

    # v3.4.23 — enforce 0.75% entry cap retroactively on every open
    # short (see manage_positions for rationale). Note: manage_positions
    # and manage_short_positions are called back-to-back by the scan
    # loop, so calling retighten_all_stops from both is redundant-but-
    # cheap. Kept in both for defensive symmetry: if a future refactor
    # reorders or skips one manager, the cap still holds for the other
    # book.
    retighten_all_stops(force_exit=True, fetch_prices=True)

    # ── Sovereign Regime Shield (v3.4.28) ────────────────────────────────────
    # Exit all shorts ONLY when BOTH SPY and QQQ have a finalized 1-min close
    # ABOVE their respective Prior Day Close (PDC). Mirror of the long-side
    # Sovereign Regime Shield — a PDC cross is structural, not drift. AND-
    # logic suppresses ejects on SPY/QQQ divergence. See v3.4.28 CHANGELOG.
    bull_vacuum = _sovereign_regime_eject("short")

    for ticker in list(short_positions.keys()):
        pos = short_positions[ticker]
        entry_price = pos["entry_price"]
        shares = pos["shares"]

        bars = fetch_1min_bars(ticker)
        if not bars:
            continue
        current_price = bars["current_price"]

        # v3.4.35 — Profit-Lock Ladder replaces the 1%/$1 armed-trail.
        # Track trail_low every tick (peak = deepest price reached).
        trail_low = pos.get("trail_low", entry_price)
        if current_price < trail_low:
            trail_low = current_price
            pos["trail_low"] = trail_low
        peak_gain_pct = (entry_price - trail_low) / entry_price if entry_price > 0 else 0.0

        # Compute ladder stop; ratchet pos["stop"] downward only (tighter).
        ladder_stop = _ladder_stop_short(pos)
        if ladder_stop < pos.get("stop", float("inf")):
            old_stop = pos.get("stop", 0)
            pos["stop"] = ladder_stop
            logger.info(
                "[LADDER] %s SHORT stop ratcheted $%.2f \u2192 $%.2f "
                "(trail_low=$%.2f, +%.2f%%)",
                ticker, old_stop, ladder_stop, trail_low, peak_gain_pct * 100,
            )

        # Arm cosmetic trail_active / trail_stop past the 1% gate.
        if peak_gain_pct >= 0.01:
            if not pos.get("trail_active"):
                pos["trail_active"] = True
                logger.info(
                    "Trail armed for %s SHORT at $%.2f (+%.2f%% peak) — ladder active",
                    ticker, current_price, peak_gain_pct * 100,
                )
            pos["trail_stop"] = pos["stop"]

        stop = pos["stop"]
        trail_active = pos.get("trail_active", False)

        # Exit on stop hit. TRAIL vs STOP per ladder-armed state.
        exit_reason = None
        if current_price >= stop:
            exit_reason = "TRAIL" if trail_active else "STOP"


        # ── Sovereign Regime Shield: BOTH SPY+QQQ 1m_close > PDC ─────────────
        if not exit_reason and bull_vacuum:
            exit_reason = "BULL_VACUUM"

        # ── Eye of the Tiger: "The Polarity Shift" — Price > PDC ─────────────
        # Uses completed 1m bar close (per-ticker; not part of the index shield)
        if not exit_reason:
            ticker_pdc = pdc.get(ticker, 0)
            if ticker_pdc > 0:
                ps_closes = [c for c in bars.get("closes", []) if c is not None]
                ps_1min_close = ps_closes[-2] if len(ps_closes) >= 2 else (ps_closes[-1] if ps_closes else current_price)
                if ps_1min_close > ticker_pdc:
                    exit_reason = "POLARITY_SHIFT"

        if exit_reason:
            close_short_position(ticker, current_price, exit_reason)



# ============================================================
# v4.9.0 \u2014 Public entry/close API \u2014 thin wrappers
# ============================================================
# check_breakout / execute_breakout / close_breakout above are the
# canonical unified bodies. The public names below preserve the call
# sites that scan_loop, manage_positions, manage_short_positions,
# eod_close, and the dashboard server use. They forward to the unified
# bodies via Side.LONG / Side.SHORT.
def check_entry(ticker):
    return check_breakout(ticker, Side.LONG)


def check_short_entry(ticker):
    return check_breakout(ticker, Side.SHORT)


def execute_entry(ticker, current_price):
    return execute_breakout(ticker, current_price, Side.LONG)


def execute_short_entry(ticker, current_price):
    return execute_breakout(ticker, current_price, Side.SHORT)


def close_position(ticker, price, reason="STOP"):
    return close_breakout(ticker, price, Side.LONG, reason)


def close_short_position(ticker, price, reason="STOP"):
    return close_breakout(ticker, price, Side.SHORT, reason)


# ============================================================
# EOD CLOSE
# ============================================================
def eod_close():
    """Force-close all open long AND short positions at 15:55 ET."""
    # v4.0.0-alpha — notify executors to flatten everything on Alpaca.
    # Per-position close events still fire from close_position /
    # close_short_position below; this event lets executors shortcut with
    # a single close_all_positions call if they prefer.
    _emit_signal({
        "kind": "EOD_CLOSE_ALL",
        "ticker": "",
        "price": 0.0,
        "reason": "EOD",
        "timestamp_utc": _utc_now_iso(),
        "main_shares": 0,
    })
    n_long = len(positions)
    n_short = len(short_positions)

    if not positions and not short_positions:
        logger.info("EOD close: no open positions (long or short)")

    if positions:
        logger.info("EOD close: closing %d long positions", n_long)
        longs_to_close = []
        for ticker in list(positions.keys()):
            bars = fetch_1min_bars(ticker)
            if bars:
                price = bars["current_price"]
            else:
                price = positions[ticker]["entry_price"]
            longs_to_close.append((ticker, price))
        for ticker, price in longs_to_close:
            close_position(ticker, price, reason="EOD")

    if short_positions:
        logger.info("EOD close: closing %d short positions", n_short)
        shorts_to_close = []
        for ticker in list(short_positions.keys()):
            bars = fetch_1min_bars(ticker)
            if bars:
                price = bars["current_price"]
            else:
                price = short_positions[ticker]["entry_price"]
            shorts_to_close.append((ticker, price))
        for ticker, price in shorts_to_close:
            close_short_position(ticker, price, "EOD")

    # v5.2.0 \u2014 close any orphan shadow positions (configs whose
    # would-have-entered ticker is not held live) at EOD.
    # v5.2.1 H2: last_marks now falls back to entry_price when
    # last_mark_price is missing, mirroring the live long/short EOD
    # pattern above (price = entry_price when bars unavailable). The
    # tracker's close_all_for_eod additionally force-closes any
    # remaining orphan with EOD_NO_MARK + entry_price as the exit so
    # nothing is silently left open.
    try:
        last_marks: dict[str, float] = {}
        tr = shadow_pnl.tracker()
        with tr._lock:
            for cfg_positions in tr._open.values():
                for sp in cfg_positions:
                    if sp.last_mark_price is not None:
                        last_marks[sp.ticker] = sp.last_mark_price
                    elif sp.ticker not in last_marks:
                        last_marks[sp.ticker] = float(sp.entry_price)
        tr.close_all_for_eod(last_marks)
    except Exception as e:
        logger.warning("[V520-SHADOW-PNL] EOD shadow close failed: %s", e)

    _, _, total_pnl, wins, losses, n_trades = _today_pnl_breakdown()
    msg = (
        f"EOD CLOSE Complete\n"
        f"  Trades: {n_trades}  W/L: {wins}/{losses}\n"
        f"  Day P&L: ${total_pnl:+.2f}\n"
        f"  Cash: ${paper_cash:,.2f}"
    )
    send_telegram(msg)
    # C-R5: EOD force-close flattens any open v5 position regardless of
    # state \u2014 we lock every track so the next session starts fresh
    # rather than resuming a half-mid-state machine.
    try:
        v5_lock_all_tracks("eod")
    except Exception:
        logger.exception("v5_lock_all_tracks failed (eod)")
    # v5.5.2 \u2014 enforce 90-day retention on the bar archive once per
    # day at EOD. Failure-tolerant; never raises.
    try:
        deleted = bar_archive.cleanup_old_dirs(retain_days=90)
        if deleted:
            logger.info("[V510-BAR] retention cleanup removed %d dated dirs",
                        len(deleted))
    except Exception as e:
        logger.warning("[V510-BAR] retention cleanup failed: %s", e)
    save_paper_state()


# ============================================================
# MORNING OR NOTIFICATION (Feature 3)
# ============================================================
def send_or_notification():
    """Send morning OR card at 09:36 ET. Retry if OR data not ready."""
    def _do_send():
        now_et = _now_et()
        today = now_et.strftime("%Y-%m-%d")

        for attempt in range(3):
            if or_collected_date == today and len(or_high) > 0:
                break
            if attempt < 2:
                logger.info("OR notification: data not ready, retry %d/3 in 30s", attempt + 1)
                time.sleep(30)

        if or_collected_date != today:
            logger.warning("OR notification: OR data not ready after retries, skipping")
            return

        SEP = "\u2500" * 34
        lines = [
            "\U0001f4d0 OR LEVELS \u2014 8:36 CT",
            SEP,
        ]

        for t in TRADE_TICKERS:
            orh = or_high.get(t)
            orl = or_low.get(t)
            pdc_val = pdc.get(t)
            if orh is None or pdc_val is None:
                lines.append("%s   --" % t)
                continue
            # Fetch current price for status
            bars = fetch_1min_bars(t)
            cur_price = bars["current_price"] if bars else 0
            if cur_price > pdc_val:
                status_icon = "\U0001f7e2"
            elif cur_price < pdc_val:
                status_icon = "\U0001f534"
            else:
                status_icon = "\u2b1c"
            orl_str = "%.2f" % orl if orl is not None else "--"
            lines.append(
                "%s  H:$%.2f  L:$%s  PDC:$%.2f  %s"
                % (t, orh, orl_str, pdc_val, status_icon)
            )

        lines.append(SEP)

        # SPY/QQQ vs PDC (v3.4.34: swapped from AVWAP)
        spy_bars = fetch_1min_bars("SPY")
        qqq_bars = fetch_1min_bars("QQQ")
        spy_price = spy_bars["current_price"] if spy_bars else 0
        qqq_price = qqq_bars["current_price"] if qqq_bars else 0
        spy_pdc_d = pdc.get("SPY") or 0
        qqq_pdc_d = pdc.get("QQQ") or 0

        spy_above = spy_price > spy_pdc_d if spy_pdc_d > 0 else False
        qqq_above = qqq_price > qqq_pdc_d if qqq_pdc_d > 0 else False
        spy_icon = "\u2705 above" if spy_above else "\u274c below"
        qqq_icon = "\u2705 above" if qqq_above else "\u274c below"

        spy_pdc_fmt = "%.2f" % spy_pdc_d if spy_pdc_d > 0 else "n/a"
        qqq_pdc_fmt = "%.2f" % qqq_pdc_d if qqq_pdc_d > 0 else "n/a"

        lines.append("SPY PDC: $%s  %s" % (spy_pdc_fmt, spy_icon))
        lines.append("QQQ PDC: $%s  %s" % (qqq_pdc_fmt, qqq_icon))

        both_active = spy_above and qqq_above
        both_bearish = (not spy_above) and (not qqq_above)
        filter_status = "LONG ACTIVE" if both_active else ("SHORT ACTIVE" if both_bearish else "PARTIAL/INACTIVE")
        lines.append("Index filters: %s" % filter_status)
        lines.append(SEP)
        lines.append("Watching for breakouts (long) and breakdowns (short).")

        msg = "\n".join(lines)
        send_telegram(msg)

    threading.Thread(target=_do_send, daemon=True).start()


# ============================================================
# AUTO EOD REPORT (Feature 4)
# ============================================================
def _build_eod_report(today: str) -> str:
    """Build EOD report text for the paper portfolio.

    v3.4.6: includes shorts. Previously only counted long SELLs (action='SELL'
    in paper_trades), so paper short COVERs (logged to short_trade_history
    with action='COVER') were silently dropped. All-time totals also excluded
    short P&L. This rebuilds the report from trade_history + short_trade_history
    so longs and shorts are both counted, with a per-trade label.
    """
    SEP = "\u2500" * 34
    long_hist = trade_history
    short_hist = short_trade_history
    title = "PAPER PORTFOLIO"

    # Today's closed trades (longs + shorts), filtered by date
    today_longs = [t for t in long_hist if t.get("date", "") == today]
    today_shorts = [t for t in short_hist if t.get("date", "") == today]
    today_all = today_longs + today_shorts

    n_trades = len(today_all)
    n_long = len(today_longs)
    n_short = len(today_shorts)
    wins = sum(1 for t in today_all if (t.get("pnl") or 0) >= 0)
    losses = n_trades - wins
    win_rate = (wins / n_trades * 100) if n_trades > 0 else 0
    day_pnl = sum((t.get("pnl") or 0) for t in today_all)

    # All-time across longs + shorts
    all_long_pnl = sum((t.get("pnl") or 0) for t in long_hist)
    all_short_pnl = sum((t.get("pnl") or 0) for t in short_hist)
    all_time_pnl = all_long_pnl + all_short_pnl
    all_wins = (
        sum(1 for t in long_hist if (t.get("pnl") or 0) >= 0)
        + sum(1 for t in short_hist if (t.get("pnl") or 0) >= 0)
    )
    all_n = len(long_hist) + len(short_hist)
    all_losses = all_n - all_wins
    all_wr = (all_wins / all_n * 100) if all_n else 0

    lines = [
        "\U0001f4ca EOD Report \u2014 %s" % today,
        SEP,
        title,
        "  Trades today:  %d  (L:%d S:%d)" % (n_trades, n_long, n_short),
        "  Wins / Losses: %d / %d" % (wins, losses),
        "  Win Rate:      %.1f%%" % win_rate,
        "  Day P&L:      $%+.2f" % day_pnl,
        SEP,
    ]
    # Sort by exit time so the per-trade list reads chronologically
    today_all_sorted = sorted(
        today_all,
        key=lambda t: t.get("exit_time_iso") or t.get("exit_time") or "",
    )
    for t in today_all_sorted:
        tk = t.get("ticker", "?")
        sh = t.get("shares", 0)
        t_pnl = t.get("pnl") or 0
        t_pct = t.get("pnl_pct") or 0
        t_reason = t.get("reason", "?")
        side = (t.get("side") or "long").upper()
        side_tag = "S" if side == "SHORT" else "L"
        lines.append("  [%s] %s  %dsh  $%+.2f (%+.1f%%)  %s"
                     % (side_tag, tk, sh, t_pnl, t_pct, t_reason))
    lines.append(SEP)
    lines.append("  All-time P&L:  $%+.2f" % all_time_pnl)
    lines.append("  All-time W/L:  %d / %d  (%.1f%%)"
                 % (all_wins, all_losses, all_wr))

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3990] + "\n... (truncated)"
    return msg


def send_eod_report():
    """Auto EOD report at 15:58 ET. Paper only.

    v3.4.6: includes paper shorts (previously dropped because the report
    filtered paper_trades for action='SELL', which excludes COVER records).
    """
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")
    send_telegram(_build_eod_report(today))


# ============================================================
# WEEKLY DIGEST (Feature 9)
# ============================================================
def send_weekly_digest():
    """Weekly digest — Sunday 18:00 ET. Paper only."""
    SEP = "\u2500" * 34
    now_et = _now_et()
    cutoff = now_et - timedelta(days=7)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    week_label = _now_cdt().strftime("Week of %b %d")

    def _build_digest(history, label):
        week_trades = [
            t for t in history
            if t.get("date", "") >= cutoff_str
        ]
        if not week_trades:
            return "\U0001f4c5 Weekly Digest \u2014 %s\n%s\n%s\nNo trades this week." % (
                week_label, SEP, label
            )

        n = len(week_trades)
        wins = sum(1 for t in week_trades if t.get("pnl", 0) >= 0)
        losses = n - wins
        wr = (wins / n * 100) if n > 0 else 0
        week_pnl = sum(t.get("pnl", 0) for t in week_trades)

        # Best day
        day_pnls = {}
        for t in week_trades:
            d = t.get("date", "")
            day_pnls[d] = day_pnls.get(d, 0) + t.get("pnl", 0)
        best_day_date = max(day_pnls, key=day_pnls.get)
        # Convert date to day name
        try:
            best_day_dt = datetime.strptime(best_day_date, "%Y-%m-%d")
            best_day_name = best_day_dt.strftime("%a")
        except Exception:
            best_day_name = best_day_date
        best_day_pnl = day_pnls[best_day_date]

        # Best trade
        best_trade = max(week_trades, key=lambda t: t.get("pnl", 0))
        best_ticker = best_trade.get("ticker", "?")
        best_pnl = best_trade.get("pnl", 0)

        # Top performers by ticker
        ticker_pnls = {}
        ticker_counts = {}
        for t in week_trades:
            tk = t.get("ticker", "?")
            ticker_pnls[tk] = ticker_pnls.get(tk, 0) + t.get("pnl", 0)
            ticker_counts[tk] = ticker_counts.get(tk, 0) + 1
        sorted_tickers = sorted(ticker_pnls.keys(), key=lambda k: ticker_pnls[k], reverse=True)
        top3 = sorted_tickers[:3]

        lines = [
            "\U0001f4c5 Weekly Digest \u2014 %s" % week_label,
            SEP,
            label,
            "  Trades:    %d  (W:%d  L:%d)" % (n, wins, losses),
            "  Win Rate:  %.1f%%" % wr,
            "  Week P&L: $%+.2f" % week_pnl,
            "  Best day:  %s $%+.2f" % (best_day_name, best_day_pnl),
            "  Best trade: %s $%+.2f" % (best_ticker, best_pnl),
            SEP,
            "Top performers this week:",
        ]
        for tk in top3:
            lines.append("  %s  %d trades  $%+.2f" % (tk, ticker_counts[tk], ticker_pnls[tk]))
        lines.append(SEP)
        lines.append("Next week: OR strategy continues.")
        lines.append("All 8 tickers monitored from 8:45 CT.")

        msg = "\n".join(lines)
        if len(msg) > 4000:
            msg = msg[:3990] + "\n... (truncated)"
        return msg

    # Merge long + short history so weekly digest covers all closed trades.
    # Long closes live in trade_history; short COVERs live in short_trade_history.
    paper_combined = list(trade_history) + list(short_trade_history)
    paper_digest = _build_digest(paper_combined, "PAPER PORTFOLIO")
    send_telegram(paper_digest)


# ============================================================
# SYSTEM HEALTH TEST
# ============================================================
def _run_system_test_sync(label: str) -> None:
    """Run system health checks and send report (blocking I/O — run in executor)."""
    SEP = "\u2500" * 30
    issues = 0
    lines = []

    # A. FMP API check
    try:
        spy_q = get_fmp_quote("SPY")
        qqq_q = get_fmp_quote("QQQ")
        spy_price = float(spy_q.get("price", 0)) if spy_q else 0
        qqq_price = float(qqq_q.get("price", 0)) if qqq_q else 0
        if spy_price > 0 and qqq_price > 0:
            lines.append(
                "FMP: \u2705 SPY $%.2f | QQQ $%.2f" % (spy_price, qqq_price)
            )
        else:
            issues += 1
            lines.append("FMP: \u274c no price data")
    except Exception as exc:
        issues += 1
        lines.append("FMP: \u274c %s" % exc)

    # B. State health check
    try:
        with open(PAPER_STATE_FILE, "r", encoding="utf-8") as f:
            ps = json.load(f)
        p_cash = ps.get("paper_cash", 0)
        lines.append(
            "State: \u2705 paper $%s"
            % format(int(p_cash), ",")
        )
    except Exception as exc:
        issues += 1
        lines.append("State: \u274c %s" % exc)

    # C. Positions count
    n_paper = len(positions) + len(short_positions)
    lines.append("Pos: %d paper" % n_paper)

    # D. Scanner health
    if _last_scan_time is None:
        lines.append("Scanner: \u23f8 Not started")
    else:
        age = (datetime.now(timezone.utc) - _last_scan_time).total_seconds()
        if age < 90:
            lines.append("Scanner: \u2705 Active (%ds ago)" % int(age))
        else:
            mins = int(age) // 60
            secs = int(age) % 60
            issues += 1
            lines.append(
                "Scanner: \u274c STALLED (%dm %ds ago)" % (mins, secs)
            )

    # E. OR status — only for 8:31 CT label
    if label == "8:31 CT":
        n_or = sum(1 for t in TRADE_TICKERS if t in or_high)
        lines.append("ORs set: %d / %d tickers" % (n_or, len(TRADE_TICKERS)))

    # Build message
    if issues == 0:
        footer = "\u2705 All systems GO"
    else:
        footer = "\u26a0\ufe0f %d issue(s) found \u2014 check logs" % issues

    body = "\n".join(lines)
    msg = (
        "\U0001f9ea System Test [%s]\n"
        "%s\n"
        "%s\n"
        "%s\n"
        "%s"
    ) % (label, SEP, body, SEP, footer)

    send_telegram(msg)


def _fire_system_test(label: str) -> None:
    """Sync wrapper to fire _run_system_test_sync from scheduler thread."""
    try:
        _run_system_test_sync(label)
    except Exception as exc:
        # v4.11.0 \u2014 report_error: scheduled health check failure.
        report_error(
            executor="main",
            code="SYSTEM_TEST_FAILED",
            severity="error",
            summary=f"System test failed: {label}",
            detail=f"{type(exc).__name__}: {exc}",
        )


def _test_fmp():
    """Test FMP API — returns status string."""
    try:
        spy_q = get_fmp_quote("SPY")
        qqq_q = get_fmp_quote("QQQ")
        spy_price = float(spy_q.get("price", 0)) if spy_q else 0
        qqq_price = float(qqq_q.get("price", 0)) if qqq_q else 0
        if spy_price > 0 and qqq_price > 0:
            return "\u2705 SPY $%.2f | QQQ $%.2f" % (spy_price, qqq_price)
        return "\u274c no price data"
    except Exception as exc:
        return "\u274c %s" % exc


def _test_state():
    """Test state files — returns status string."""
    try:
        with open(PAPER_STATE_FILE, "r", encoding="utf-8") as f:
            ps = json.load(f)
        p_cash = ps.get("paper_cash", 0)
        return "\u2705 paper $%s" % format(int(p_cash), ",")
    except Exception as exc:
        return "\u274c %s" % exc


def _test_positions():
    """Test positions — returns status string."""
    n_paper = len(positions) + len(short_positions)
    return "%d paper" % n_paper


def _test_scanner():
    """Test scanner health — returns status string."""
    if _last_scan_time is None:
        return "\u23f8 Not started"
    age = (datetime.now(timezone.utc) - _last_scan_time).total_seconds()
    if age < 90:
        return "\u2705 Active (%ds ago)" % int(age)
    mins = int(age) // 60
    secs = int(age) % 60
    return "\u274c STALLED (%dm %ds ago)" % (mins, secs)


def _build_test_progress(results):
    """Format the interactive test progress message."""
    SEP = "\u2500" * 30
    steps = [
        ("FMP API", "fmp"),
        ("State files", "state"),
        ("Positions", "pos"),
        ("Scanner", "scanner"),
    ]
    body_lines = []
    for label, key in steps:
        status = results.get(key, "\u23f3")
        body_lines.append("  %-12s %s" % (label + ":", status))
    body = "\n".join(body_lines)

    issues = 0
    for key in ("fmp", "fhb", "state", "scanner"):
        val = results.get(key, "")
        if val.startswith("\u274c"):
            issues += 1

    if all(k in results for _, k in steps):
        if issues == 0:
            footer = "\u2705 All systems GO"
        else:
            footer = "\u26a0\ufe0f %d issue(s) found \u2014 check logs" % issues
        return "\U0001f9ea System Test [Manual]\n%s\n%s\n%s\n%s" % (SEP, body, SEP, footer)

    return "\U0001f9ea System Test [Manual]\n%s\n%s" % (SEP, body)


# ============================================================
# v3.4.47 — HARD EJECT (Eye of the Tiger 2.0)
# ============================================================
def _tiger_hard_eject_check():
    """Hard Eject: close any open position whose DI or index
    regime has flipped against it.

    Called once per scan cycle BEFORE the new-entry scan.
    Longs: eject if DI+ < threshold OR both indices < PDC.
    Shorts: eject if DI- < threshold OR both indices > PDC.
    Applies to paper (positions, short_positions).
    """
    # Index regime flags (reuse cached bars from this cycle)
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_pdc_v = pdc.get("SPY")
    qqq_pdc_v = pdc.get("QQQ")

    index_flip_down = False  # both indices below PDC -> eject longs
    index_flip_up   = False  # both indices above PDC -> eject shorts
    if (spy_bars and qqq_bars
            and spy_pdc_v and qqq_pdc_v
            and spy_pdc_v > 0 and qqq_pdc_v > 0):
        spy_cur = spy_bars["current_price"]
        qqq_cur = qqq_bars["current_price"]
        index_flip_down = (spy_cur < spy_pdc_v
                           and qqq_cur < qqq_pdc_v)
        index_flip_up   = (spy_cur > spy_pdc_v
                           and qqq_cur > qqq_pdc_v)

    # -- Long positions (paper) --
    for ticker in list(positions):
        di_plus, _di_m = tiger_di(ticker)
        di_weak = (di_plus is not None
                   and di_plus < TIGER_V2_DI_THRESHOLD)
        if di_weak or index_flip_down:
            price = positions[ticker].get("entry_price", 0)
            bars_t = fetch_1min_bars(ticker)
            if bars_t:
                price = bars_t["current_price"] or price
            logger.info(
                "HARD_EJECT_TIGER long %s di+=%s idx_flip=%s",
                ticker, di_plus, index_flip_down,
            )
            close_position(ticker, price,
                           reason="HARD_EJECT_TIGER")
            # v5.1.9 \u2014 arm REHUNT_VOL_CONFIRM watch on this ticker.
            try:
                _v519_arm_rehunt_watch(
                    ticker, "long", datetime.now(tz=timezone.utc))
            except Exception as e:
                logger.warning(
                    "[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] arm long %s: %s",
                    ticker, e)


    # -- Short positions (paper) --
    for ticker in list(short_positions):
        _di_p, di_minus = tiger_di(ticker)
        di_weak = (di_minus is not None
                   and di_minus < TIGER_V2_DI_THRESHOLD)
        if di_weak or index_flip_up:
            price = short_positions[ticker].get("entry_price", 0)
            bars_t = fetch_1min_bars(ticker)
            if bars_t:
                price = bars_t["current_price"] or price
            logger.info(
                "HARD_EJECT_TIGER short %s di-=%s idx_flip=%s",
                ticker, di_minus, index_flip_up,
            )
            close_short_position(ticker, price, reason="HARD_EJECT_TIGER")
            # v5.1.9 \u2014 arm REHUNT_VOL_CONFIRM watch on this ticker.
            try:
                _v519_arm_rehunt_watch(
                    ticker, "short", datetime.now(tz=timezone.utc))
            except Exception as e:
                logger.warning(
                    "[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] arm short %s: %s",
                    ticker, e)


# ============================================================
# SCAN LOOP
# ============================================================
def scan_loop():
    """Main scan: manage positions, check new entries. Runs every 60s."""
    global _scan_idle_hours
    now_et = _now_et()

    # v4.4.1 — Refresh the MarketMode banner BEFORE the after-hours
    # early returns. Without this, once the clock crosses 15:55 ET the
    # cached _current_mode / _current_mode_reason stayed frozen on the
    # last pre-close values (e.g. POWER "14:00-15:55 ET") and /api/state
    # kept serving them until the next open. Pure observation — safe to
    # fail silently; it cannot affect trading. Runs at idle cycles too,
    # not just during trading cycles.
    try:
        _refresh_market_mode()
    except Exception:
        logger.exception("_refresh_market_mode failed (ignored — observation only, runs at idle cycles too)")

    # Idle-state flag drives gates.scan_paused on the dashboard so the
    # UI can tell "scanner is not scanning right now" after hours without
    # reading internal mode globals.
    is_weekend = now_et.weekday() >= 5
    before_open = now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 35)
    after_close = now_et.hour >= 16 or (now_et.hour == 15 and now_et.minute >= 55)
    _scan_idle_hours = bool(is_weekend or before_open or after_close)

    # Skip weekends
    if is_weekend:
        return

    # v5.6.1 D2(a) \u2014 pre-9:35 ET writer warm-up. Between 9:29:30 ET and
    # 9:35:00 ET we run a stripped-down archive pass so the 9:30:00 first
    # tick is captured (closes the OR window backfill gap). We skip the
    # full entry/manage scan (gates aren't active until 9:35) but persist
    # the 1m bar for QQQ + every TRADE_TICKER. Failure-tolerant.
    _pre_open_window = (
        now_et.hour == 9 and 29 <= now_et.minute < 35
        and (now_et.minute > 29 or now_et.second >= 30)
    )
    if before_open and _pre_open_window:
        try:
            _clear_cycle_bar_cache()
            _qqq_pre = fetch_1min_bars(V561_INDEX_TICKER)
            if _qqq_pre:
                _v561_archive_qqq_bar(_qqq_pre)
            for _t_pre in TRADE_TICKERS:
                try:
                    _b_pre = fetch_1min_bars(_t_pre)
                    if not _b_pre:
                        continue
                    _closes_pre = _b_pre.get("closes") or []
                    _ts_arr_pre = _b_pre.get("timestamps") or []
                    _idx_pre = None
                    if len(_closes_pre) >= 2 and _closes_pre[-2] is not None:
                        _idx_pre = -2
                    elif len(_closes_pre) >= 1 and _closes_pre[-1] is not None:
                        _idx_pre = -1
                    if _idx_pre is None:
                        continue
                    _opens_pre = _b_pre.get("opens") or []
                    _highs_pre = _b_pre.get("highs") or []
                    _lows_pre = _b_pre.get("lows") or []
                    _vols_pre = _b_pre.get("volumes") or []
                    _ts_val_pre = (_ts_arr_pre[_idx_pre]
                                   if abs(_idx_pre) <= len(_ts_arr_pre)
                                   else None)
                    try:
                        _ts_iso_pre = (datetime.utcfromtimestamp(int(_ts_val_pre))
                                       .strftime("%Y-%m-%dT%H:%M:%SZ")
                                       if _ts_val_pre is not None else None)
                    except Exception:
                        _ts_iso_pre = None
                    _bar_pre = {
                        "ts": _ts_iso_pre,
                        "et_bucket": None,
                        "open":  _opens_pre[_idx_pre] if abs(_idx_pre) <= len(_opens_pre) else None,
                        "high":  _highs_pre[_idx_pre] if abs(_idx_pre) <= len(_highs_pre) else None,
                        "low":   _lows_pre[_idx_pre]  if abs(_idx_pre) <= len(_lows_pre)  else None,
                        "close": _closes_pre[_idx_pre],
                        "iex_volume": _vols_pre[_idx_pre] if abs(_idx_pre) <= len(_vols_pre) else None,
                        "iex_sip_ratio_used": None,
                        "bid": None,
                        "ask": None,
                        "last_trade_price": _b_pre.get("current_price"),
                    }
                    _v512_archive_minute_bar(_t_pre, _bar_pre)
                except Exception as _e_pre:
                    logger.warning("[V561-PREOPEN-BAR] %s: %s", _t_pre, _e_pre)
        except Exception as _e_pre_outer:
            logger.warning("[V561-PREOPEN] cycle hook error: %s", _e_pre_outer)
        # Pre-open: archive only, no entry/manage. Return after archive.
        return

    # Skip outside market hours (09:35 - 15:55 ET)
    if before_open or after_close:
        return

    cycle_start = time.time()
    global _last_scan_time
    _last_scan_time = datetime.now(timezone.utc)

    # Clear the per-cycle 1-min bar cache BEFORE anything else. Any call
    # to fetch_1min_bars inside this cycle will populate it on first hit
    # and reuse on subsequent hits. Observers read through the same cache.
    _clear_cycle_bar_cache()

    n_pos = len(positions)
    n_short = len(short_positions)
    logger.info("Scanning %d stocks | pos=%d short=%d | mode=%s",
                len(TRADE_TICKERS), n_pos, n_short, _current_mode)

    # ── Regime change alert ───────────────────────────────────────────────
    # v3.4.34: anchor swapped from AVWAP → PDC to match the
    # v3.4.28 Sovereign Regime Shield ejector. One anchor across
    # the whole system; no more divergent alerts vs. real ejects.
    # Fail-closed on missing PDC: no alert fires if either index
    # PDC is unseeded (same semantics as _sovereign_regime_eject).
    global _regime_bullish
    spy_pdc_r = pdc.get("SPY")
    qqq_pdc_r = pdc.get("QQQ")
    if spy_pdc_r and qqq_pdc_r and spy_pdc_r > 0 and qqq_pdc_r > 0:
        spy_bars_r = fetch_1min_bars("SPY")
        qqq_bars_r = fetch_1min_bars("QQQ")
        if spy_bars_r and qqq_bars_r:
            spy_cur_r = spy_bars_r["current_price"]
            qqq_cur_r = qqq_bars_r["current_price"]
            now_bullish = (spy_cur_r > spy_pdc_r) and (qqq_cur_r > qqq_pdc_r)
            if _regime_bullish is None:
                _regime_bullish = now_bullish
            elif now_bullish != _regime_bullish:
                _regime_bullish = now_bullish
                now_hhmm_r = _now_cdt().strftime("%H:%M CDT")
                if now_bullish:
                    regime_msg = (
                        "\U0001f7e2 REGIME: BULLISH\n"
                        "SPY $%.2f > PDC $%.2f\n"
                        "QQQ $%.2f > PDC $%.2f\n"
                        "The Lords are back.  %s"
                    ) % (spy_cur_r, spy_pdc_r, qqq_cur_r, qqq_pdc_r, now_hhmm_r)
                else:
                    regime_msg = (
                        "\U0001f534 REGIME: BEARISH\n"
                        "SPY $%.2f < PDC $%.2f\n"
                        "QQQ $%.2f < PDC $%.2f\n"
                        "The Lords have left.  %s"
                    ) % (spy_cur_r, spy_pdc_r, qqq_cur_r, qqq_pdc_r, now_hhmm_r)
                send_telegram(regime_msg)

    # v5.6.1 D1 \u2014 archive QQQ 1m bar each cycle so the index ticker is
    # persisted alongside the 8 trade tickers. Failure-tolerant; never
    # blocks the scan.
    try:
        _qqq_bars_archive = fetch_1min_bars(V561_INDEX_TICKER)
        if _qqq_bars_archive:
            _v561_archive_qqq_bar(_qqq_bars_archive)
    except Exception as _e:
        logger.warning("[V561-QQQ-BAR] cycle hook error: %s", _e)

    # v5.6.1 D2 \u2014 persist OR_High/OR_Low snapshots once per ticker per
    # session, after 9:35 ET when the OR window is closed and the gate
    # code's or_high/or_low dicts are seeded.
    try:
        _v561_maybe_persist_or_snapshots(now_et=now_et)
    except Exception as _e:
        logger.warning("[V561-OR-SNAP] cycle hook error: %s", _e)

    # Always manage existing positions (stops/trails) even when paused
    try:
        manage_positions()
    except Exception as e:
        # v4.11.0 \u2014 report_error replaces the previous logger.error +
        # ad-hoc send_telegram pair. The Telegram message now follows
        # the unified \u226434-char-line health-pill format and is gated
        # by the per-(executor,code) 5-min dedup.
        report_error(
            executor="main",
            code="MANAGE_POSITIONS_EXCEPTION",
            severity="error",
            summary="manage_positions crashed",
            detail=f"{type(e).__name__}: {str(e)[:200]}",
        )
    try:
        manage_short_positions()
    except Exception as e:
        report_error(
            executor="main",
            code="MANAGE_SHORT_POSITIONS_EXCEPTION",
            severity="error",
            summary="manage_short_positions crashed",
            detail=f"{type(e).__name__}: {str(e)[:200]}",
        )

    # v3.4.47 — Hard Eject: close positions whose DI or regime
    # has flipped against them (runs before new-entry scan).
    try:
        _tiger_hard_eject_check()
    except Exception as e:
        # v4.11.0 \u2014 report_error: hard-eject path failure surfaces
        # as a paged event so the operator can investigate why an
        # eject did not run.
        report_error(
            executor="main",
            code="HARD_EJECT_EXCEPTION",
            severity="error",
            summary="_tiger_hard_eject_check crashed",
            detail=f"{type(e).__name__}: {str(e)[:200]}",
        )

    # Feature 8: scan pause — only block NEW entries
    if _scan_paused:
        logger.info("SCAN CYCLE done in %.2fs — paused (manage only)", time.time() - cycle_start)
        return

    # Check for new entries on tradable tickers (long + short).
    # v3.4.40 — paper and Robinhood are now evaluated INDEPENDENTLY.
    # check_entry() is the shared signal/indicator gate; the portfolio-
    # side decision (halt, cash, concurrency, per-ticker cap) is per-
    # book. A paper-held ticker no longer blocks RH from entering, and
    # vice versa.
    for ticker in TRADE_TICKERS:
        # Refresh the dashboard gate snapshot from the current OR
        # envelope before any entry gates run. Side + break are derived
        # purely from OR vs price each cycle (no latch).
        try:
            _update_gate_snapshot(ticker)
        except Exception as e:
            logger.error("_update_gate_snapshot error %s: %s", ticker, e)
        # Long entry check — run once per ticker and fan out to both books.
        try:
            # v5.2.1 H3 \u2014 mark-to-market shadow positions UNCONDITIONALLY,
            # regardless of whether paper currently holds the ticker. Prior
            # behavior gated MTM behind `not paper_holds` which silently
            # froze shadow marks the moment paper opened a position on
            # the same ticker.
            try:
                _bars_for_mtm = fetch_1min_bars(ticker)
                if _bars_for_mtm and _bars_for_mtm.get("current_price"):
                    _v520_mtm_ticker(ticker, _bars_for_mtm["current_price"])
            except Exception as e:
                logger.warning("[V520-SHADOW-PNL] mtm hook %s: %s", ticker, e)
            # v5.5.2 \u2014 persist the most-recently-completed 1m bar to
            # /data/bars/YYYY-MM-DD/{TICKER}.jsonl so the offline backtest
            # CLI has something to replay. fetch_1min_bars already cached
            # by _cycle_bar_cache so this is free; we project the parallel
            # arrays onto the canonical bar_archive.BAR_SCHEMA_FIELDS dict
            # before passing. Wrapped in its own try/except: a write
            # failure must never disrupt the trading scan.
            try:
                if _bars_for_mtm:
                    closes = _bars_for_mtm.get("closes") or []
                    ts_arr = _bars_for_mtm.get("timestamps") or []
                    # Prefer the second-to-last entry (last is often the
                    # currently-forming bar); fall back to the last when
                    # only one bar is available.
                    idx = None
                    if len(closes) >= 2 and closes[-2] is not None:
                        idx = -2
                    elif len(closes) >= 1 and closes[-1] is not None:
                        idx = -1
                    if idx is not None:
                        opens = _bars_for_mtm.get("opens") or []
                        highs = _bars_for_mtm.get("highs") or []
                        lows = _bars_for_mtm.get("lows") or []
                        vols = _bars_for_mtm.get("volumes") or []
                        ts_val = ts_arr[idx] if abs(idx) <= len(ts_arr) else None
                        try:
                            ts_iso = (datetime.utcfromtimestamp(int(ts_val))
                                      .strftime("%Y-%m-%dT%H:%M:%SZ")
                                      if ts_val is not None else None)
                        except Exception:
                            ts_iso = None
                        # v5.5.5 \u2014 prefer the WS consumer's IEX volume for
                        # the current bucket. Yahoo's intraday endpoint
                        # frequently returns volume=0/null on the leading-edge
                        # bar, leaving the offline backtest CLI replaying
                        # against zeroes. Fall back to the Yahoo value when
                        # the WS path is unavailable, outside RTH, or has
                        # not yet captured this bucket.
                        yahoo_vol = vols[idx] if abs(idx) <= len(vols) else None
                        iex_volume = yahoo_vol
                        et_bucket: str | None = None
                        try:
                            now_et = datetime.now(tz=ZoneInfo("America/New_York"))
                            et_bucket = volume_profile.session_bucket(now_et)
                            if (
                                et_bucket is not None
                                and _ws_consumer is not None
                            ):
                                ws_vol = _ws_consumer.current_volume(
                                    ticker, et_bucket,
                                )
                                if ws_vol is not None:
                                    iex_volume = int(ws_vol)
                        except Exception as _e:
                            # Never let observability break the trading scan.
                            logger.warning(
                                "[V510-BAR] ws-source switch %s: %s",
                                ticker, _e,
                            )
                        canon_bar = {
                            "ts": ts_iso,
                            "et_bucket": et_bucket,
                            "open":  opens[idx] if abs(idx) <= len(opens) else None,
                            "high":  highs[idx] if abs(idx) <= len(highs) else None,
                            "low":   lows[idx]  if abs(idx) <= len(lows)  else None,
                            "close": closes[idx],
                            "iex_volume": iex_volume,
                            "iex_sip_ratio_used": None,
                            "bid": None,
                            "ask": None,
                            "last_trade_price": _bars_for_mtm.get("current_price"),
                        }
                        _v512_archive_minute_bar(ticker, canon_bar)
            except Exception as e:
                logger.warning("[V510-BAR] archive hook %s: %s", ticker, e)
            # Fast path: if paper already holds this ticker, skip the
            # signal compute. Otherwise run check_entry so the signal
            # decision is made once for the scan cycle.
            paper_holds = ticker in positions
            if not paper_holds:
                ok, bars = check_entry(ticker)
                # v5.1.0 SHADOW: log the V-P1 grid result for this minute
                # without changing the entry decision. Stage is inferred
                # from whether the bot is currently in Stage_1 or
                # Stage_2 for this ticker; default to Stage 1 (Jab) for
                # the new-entry decision.
                _shadow_log_g4(ticker, stage=1, existing_decision=("ENTER" if ok else "HOLD"))
                # v5.1.2 \u2014 [V510-CAND] for every entry consideration
                # (closes the asymmetric blind-spot from v5.1.1).
                try:
                    _v512_emit_candidate_log(ticker, stage=1, entered=bool(ok and bars), bars=bars)
                except Exception as e:
                    logger.warning("[V510-CAND] hook error %s: %s", ticker, e)
                # v5.1.9 \u2014 REHUNT_VOL_CONFIRM and OOMPH_ALERT shadow probes.
                try:
                    _v519_check_rehunt(ticker)
                except Exception as e:
                    logger.warning(
                        "[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM] hook %s: %s",
                        ticker, e)
                try:
                    _v519_check_oomph(ticker, bars=bars)
                except Exception as e:
                    logger.warning(
                        "[V510-SHADOW][CFG=OOMPH_ALERT] hook %s: %s",
                        ticker, e)
                if ok and bars:
                    px = bars["current_price"]
                    try:
                        execute_entry(ticker, px)
                    except Exception as e:
                        # v4.11.0 \u2014 report_error: paper-book entry
                        # exception. Operator should know why a long
                        # signal failed to execute.
                        report_error(
                            executor="main",
                            code="PAPER_ENTRY_EXCEPTION",
                            severity="error",
                            summary=f"Paper entry exception: {ticker}",
                            detail=f"{type(e).__name__}: {str(e)[:200]}",
                        )
        except Exception as e:
            logger.error("Entry check error %s: %s", ticker, e)
        # Short entry check (Wounded Buffalo) — same call/execute pattern as long.
        try:
            paper_short_holds = ticker in short_positions
            if not paper_short_holds:
                ok, bars = check_short_entry(ticker)
                if ok and bars:
                    px = bars["current_price"]
                    try:
                        execute_short_entry(ticker, px)
                    except Exception as e:
                        # v4.11.0 \u2014 report_error: paper-book short
                        # entry exception.
                        report_error(
                            executor="main",
                            code="PAPER_SHORT_ENTRY_EXCEPTION",
                            severity="error",
                            summary=f"Paper short entry exception: {ticker}",
                            detail=f"{type(e).__name__}: {str(e)[:200]}",
                        )
        except Exception as e:
            logger.error("Short entry check error %s: %s", ticker, e)

    logger.info("SCAN CYCLE done in %.2fs — %d tickers", time.time() - cycle_start, len(TRADE_TICKERS))


# ============================================================
# RESET DAILY STATE
# ============================================================
def reset_daily_state():
    """Reset OR data and daily counts for new trading day.
    (v3.4.34: AVWAP reset removed — AVWAP state no longer tracked.)
    """
    global or_collected_date, daily_entry_date, _trading_halted, _trading_halted_reason
    global daily_short_entry_count, daily_short_entry_date, _regime_bullish, _current_rsi_regime

    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date != today:
        or_high.clear()
        or_low.clear()
        pdc.clear()
        or_stale_skip_count.clear()
        or_collected_date = ""

    if daily_entry_date != today:
        daily_entry_count.clear()
        daily_short_entry_count.clear()
        paper_trades.clear()
        daily_entry_date = today
        daily_short_entry_date = today
        # v5.6.1 \u2014 OR-snapshot dedup keyed by UTC date; clear at the
        # session boundary so tomorrow re-emits.
        try:
            _v561_reset_or_snap_state()
        except Exception:
            logger.exception("reset_daily_state: _v561 OR snap reset failed")
        # v5.0.0 \u2014 fresh session: clear all v5 state-machine tracks so
        # tomorrow's first ARMED transition gets a clean tab. C-R5 / C-R6
        # only LOCK; only the daily reset clears.
        v5_long_tracks.clear()
        v5_short_tracks.clear()
        v5_active_direction.clear()
        # v4.11.0 \u2014 health-pill: clear today's error counts at the
        # same boundary as the existing daily counters so the pill
        # rolls back to green at session reset.
        try:
            _error_state.reset_daily()
        except Exception:
            logger.exception("reset_daily_state: error_state.reset_daily failed")

    _trading_halted = False
    _trading_halted_reason = ""

    # Cross-day cooldown pruning: _last_exit_time persists across restarts,
    # so yesterday's 15:54 exit would keep today's 09:35 first-5-min entry
    # under the 15-min post-exit cooldown. Drop any entry whose exit
    # occurred before today's 09:30 ET session open.
    #
    # Invariant: all date/session comparisons here are done in ET (trading
    # timezone). _last_exit_time values are stored as UTC-aware datetimes,
    # so each stored value is converted to ET before comparing against
    # today's 09:30 ET session open. Using a single timezone (ET) for both
    # sides avoids subtle DST-boundary and midnight-ET off-by-one issues.
    try:
        session_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        stale_keys = [
            k for k, v in list(_last_exit_time.items())
            if v is not None and v.astimezone(ET) < session_open_et
        ]
        for k in stale_keys:
            _last_exit_time.pop(k, None)
        if stale_keys:
            logger.info(
                "reset_daily_state: pruned %d stale _last_exit_time entries",
                len(stale_keys),
            )
    except Exception:
        logger.exception("reset_daily_state: _last_exit_time prune failed")

    # Regime-transition alert is driven by a module-global first-seen
    # comparison. Without a daily reset, a mid-session restart comparing
    # the freshly-computed regime to a stale value would fire a spurious
    # "REGIME SHIFT" alert on the first scan. Clear at session open so
    # first-of-day classification is a clean first transition.
    _regime_bullish = None
    _current_rsi_regime = "UNKNOWN"


# ============================================================
# SCHEDULER THREAD
# ============================================================
def scheduler_thread():
    """Background scheduler — all times in ET."""
    DAY_NAMES = [
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    ]

    # v5.1.8 \u2014 fired_set is now persisted in SQLite via persistence.py
    # (table: fired_set). Replaces the in-memory set so an EOD job that
    # fired before a Railway restart at 15:59:30 ET cannot double-fire
    # at 16:00 after the container comes back up.
    last_scan = _now_et() - timedelta(seconds=SCAN_INTERVAL + 1)
    last_state_save = _now_et() - timedelta(minutes=6)
    last_fired_prune = _now_et()

    # Job table: (day, "HH:MM", function)
    # Note: times are ET.  8:20 CT = 9:20 ET, 8:31 CT = 9:31 ET
    JOBS = [
        ("daily", "09:20", lambda: _fire_system_test("8:20 CT")),
        ("daily", "09:30", reset_daily_state),
        ("daily", "09:31", lambda: _fire_system_test("8:31 CT")),
        ("daily", "09:35",
         lambda: threading.Thread(target=collect_or, daemon=True).start()),
        ("daily", "09:36", send_or_notification),
        ("daily", "15:55", eod_close),
        ("daily", "15:58", send_eod_report),
        ("sunday", "18:00", send_weekly_digest),
    ]

    logger.info("Scheduler started — market times ET, display CDT (UTC offset: %s)",
                datetime.now(timezone.utc).strftime("%z"))

    while True:
        now_et = _now_et()
        now_hhmm = now_et.strftime("%H:%M")
        now_day = DAY_NAMES[now_et.weekday()]
        fire_key = now_et.strftime("%Y-%m-%d") + "-" + now_hhmm

        # Timed jobs
        for day, hhmm, fn in JOBS:
            job_key = fire_key + "-" + day + "-" + hhmm
            if now_hhmm != hhmm:
                continue
            match = (
                (day == "daily" and now_et.weekday() < 5)
                or day == "everyday"
                or day == now_day
            )
            if match and not persistence.was_fired(job_key):
                persistence.mark_fired(job_key)
                fn_name = getattr(fn, "__name__", "lambda")
                logger.info("Firing scheduled job: %s %s ET -> %s",
                            day, hhmm, fn_name)
                try:
                    fn()
                except Exception as e:
                    logger.error("Scheduled job error (%s %s): %s",
                                 day, hhmm, e, exc_info=True)

        # Prune fired_set rows from prior days. v5.1.8: SQLite-backed,
        # so we run this once an hour rather than on every loop.
        if (now_et - last_fired_prune).total_seconds() >= 3600:
            last_fired_prune = now_et
            today_prefix = now_et.strftime("%Y-%m-%d")
            try:
                persistence.prune_fired(today_prefix)
            except Exception as e:
                logger.warning("persistence.prune_fired failed: %s", e)

        # Scan loop — every SCAN_INTERVAL seconds
        elapsed = (now_et - last_scan).total_seconds()
        if elapsed >= SCAN_INTERVAL:
            last_scan = now_et
            try:
                scan_loop()
            except Exception as e:
                # v4.11.0 \u2014 report_error: top-level scan-loop catch.
                # If the whole cycle threw, the operator must know.
                report_error(
                    executor="main",
                    code="SCAN_LOOP_EXCEPTION",
                    severity="error",
                    summary="scan_loop crashed",
                    detail=f"{type(e).__name__}: {str(e)[:200]}",
                )

        # Periodic state save — every 5 minutes
        state_elapsed = (now_et - last_state_save).total_seconds() / 60
        if state_elapsed >= 5:
            last_state_save = now_et
            threading.Thread(target=save_paper_state, daemon=True).start()

        time.sleep(30)


# ============================================================
# HEALTH CHECK (keep Railway deployment alive)
# ============================================================
def health_ping():
    """Periodic health check log line — keeps the process visible."""
    while True:
        logger.debug("Health ping — alive")
        time.sleep(300)


# ============================================================
# PERFORMANCE STATS HELPER
# ============================================================
def _compute_perf_stats(history, date_filter=None):
    """Compute performance stats from a trade history list.
    If date_filter is given, only include trades on/after that date string.
    Returns dict with stats or None if no trades.
    """
    trades = history
    if date_filter:
        trades = [t for t in history if t.get("date", "") >= date_filter]
    if not trades:
        return None
    n = len(trades)
    wins = sum(1 for t in trades if t.get("pnl", 0) >= 0)
    losses = n - wins
    wr = (wins / n * 100) if n > 0 else 0
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    win_trades = [t for t in trades if t.get("pnl", 0) >= 0]
    loss_trades = [t for t in trades if t.get("pnl", 0) < 0]
    avg_win = (sum(t["pnl"] for t in win_trades) / len(win_trades)) if win_trades else 0
    avg_loss = (sum(t["pnl"] for t in loss_trades) / len(loss_trades)) if loss_trades else 0
    best = max(trades, key=lambda t: t.get("pnl", 0))
    worst = min(trades, key=lambda t: t.get("pnl", 0))
    return {
        "n": n, "wins": wins, "losses": losses, "wr": wr,
        "total_pnl": total_pnl, "avg_win": avg_win, "avg_loss": avg_loss,
        "best": best, "worst": worst,
    }


def _compute_streak(history):
    """Compute current consecutive win/loss streak from most recent trade backward."""
    if not history:
        return "N/A"
    sorted_h = sorted(history, key=lambda t: (t.get("date", ""), t.get("exit_time", "")))
    last = sorted_h[-1]
    is_win = last.get("pnl", 0) >= 0
    count = 0
    for t in reversed(sorted_h):
        t_win = t.get("pnl", 0) >= 0
        if t_win == is_win:
            count += 1
        else:
            break
    label = "W" if is_win else "L"
    return "%d%s (current)" % (count, label)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================


def _dashboard_sync():
    """Build dashboard text (blocking I/O — run in executor)."""
    SEP = "\u2500" * 34
    now_et = _now_et()
    time_cdt = _now_cdt().strftime("%I:%M %p CDT")
    today = now_et.strftime("%Y-%m-%d")

    weekday = now_et.weekday() < 5
    in_hours = (
        weekday
        and now_et.hour >= 9
        and (now_et.hour < 15 or (now_et.hour == 15 and now_et.minute < 55))
    )
    market_status = "OPEN" if in_hours else "CLOSED"

    # Index filters — fetch live prices
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0.0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0.0
    spy_pdc_d = pdc.get("SPY") or 0
    qqq_pdc_d = pdc.get("QQQ") or 0
    spy_ok = spy_price > spy_pdc_d if spy_pdc_d > 0 else False
    qqq_ok = qqq_price > qqq_pdc_d if qqq_pdc_d > 0 else False
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"

    lines = [
        "\U0001f4ca DASHBOARD  %s" % time_cdt,
        SEP,
    ]

    # Paper portfolio only — Day P&L includes long SELLs + short COVERs
    n_pos = len(positions) + len(short_positions)
    _, _, day_pnl, _, _, _ = _today_pnl_breakdown()

    total_value = paper_cash
    for ticker, pos in positions.items():
        bars = fetch_1min_bars(ticker)
        if bars:
            total_value += bars["current_price"] * pos["shares"]
        else:
            total_value += pos["entry_price"] * pos["shares"]
    # Shorts: subtract current buy-back liability (the proceeds are
    # already in paper_cash). See short-accounting note on /positions.
    for s_ticker, s_pos in short_positions.items():
        s_bars = fetch_1min_bars(s_ticker)
        s_cur = s_bars["current_price"] if s_bars else s_pos["entry_price"]
        total_value -= s_cur * s_pos["shares"]

    paper_cash_fmt = format(paper_cash, ",.2f")
    total_value_fmt = format(total_value, ",.2f")
    day_pnl_fmt = format(day_pnl, "+,.2f")
    lines += [
        "\U0001f4c4 PAPER PORTFOLIO",
        "  Cash:       $%s" % paper_cash_fmt,
        "  Positions:  %d open" % n_pos,
        "  Today P&L:  $%s" % day_pnl_fmt,
        "  Est. Value: $%s" % total_value_fmt,
    ]

    lines += [
        SEP,
        "\U0001f4c8 INDEX FILTERS",
        "  SPY  $%.2f  PDC $%.2f  %s" % (spy_price, spy_pdc_d, spy_icon),
        "  QQQ  $%.2f  PDC $%.2f  %s" % (qqq_price, qqq_pdc_d, qqq_icon),
        "  Market: %s" % market_status,
        SEP,
        "\U0001f4d0 TODAY'S OR LEVELS",
    ]

    # OR levels (High + Low)
    or_ready = or_collected_date == today
    if or_ready:
        for t in TRADE_TICKERS:
            orh_val = or_high.get(t)
            orl_val = or_low.get(t)
            if orh_val is not None:
                orl_str = "%.2f" % orl_val if orl_val is not None else "--"
                lines.append("  %s  H:$%.2f  L:$%s" % (t, orh_val, orl_str))
            else:
                lines.append("  %s --" % t)
    else:
        lines.append("  (OR not collected yet)")

    return "\n".join(lines)


def _status_text_sync():
    """Build full status text (blocking I/O — run in executor)."""
    now_et = _now_et()
    sep = "\u2500" * 34

    # Paper portfolio
    n_pos = len(positions)
    header = "Open Positions (%d)" % n_pos
    lines = [header, sep]

    total_unreal_pnl = 0.0
    total_market_value = 0.0

    if not positions:
        lines.append("No open positions")
    else:
        for ticker, pos in positions.items():
            bars = fetch_1min_bars(ticker)
            entry_p = pos["entry_price"]
            shares = pos["shares"]
            if bars:
                cur = bars["current_price"]
                pos_pnl = (cur - entry_p) * shares
                pos_pnl_pct = ((cur - entry_p) / entry_p * 100) if entry_p else 0
                mkt_val = cur * shares
                total_unreal_pnl += pos_pnl
                total_market_value += mkt_val
                if pos.get("trail_active") and pos.get("trail_stop") and pos["trail_stop"] > 0:
                    peak = pos.get("trail_high", 0)
                    stop_line = "  Stop:   $%.2f [\U0001f3af trail | peak $%.2f]" % (pos["trail_stop"], peak)
                else:
                    stop_line = "  Stop:   $%.2f [stop]" % pos["stop"]
                lines.append("%s  %d shares" % (ticker, shares))
                lines.append(
                    "  Entry:  $%.2f  ->  Now: $%.2f" % (entry_p, cur)
                )
                lines.append(
                    "  P&L:   $%+.2f (%+.1f%%)" % (pos_pnl, pos_pnl_pct)
                )
                lines.append(
                    "  Value:  $%s" % format(mkt_val, ",.2f")
                )
                lines.append(stop_line)
            else:
                mkt_val = entry_p * shares
                total_market_value += mkt_val
                lines.append("%s  %d shares" % (ticker, shares))
                lines.append("  Entry:  $%.2f  ->  price unavailable" % entry_p)
                lines.append("  Stop:   $%.2f" % pos["stop"])
            lines.append(sep)

    # Totals
    if positions:
        lines.append("Total Unrealized P&L: $%+.2f" % total_unreal_pnl)
        lines.append("Total Market Value:   $%s" % format(total_market_value, ",.2f"))

    # Today's completed trades (always show, date-filtered, includes shorts)
    today_date = now_et.strftime("%Y-%m-%d")
    today_sells = [t for t in paper_trades
                   if t.get("action") == "SELL" and t.get("date") == today_date]
    short_today = [t for t in short_trade_history if t.get("date") == today_date]
    day_pnl = (sum(t.get("pnl", 0) for t in today_sells)
               + sum(t.get("pnl", 0) for t in short_today))
    day_trades = len(today_sells) + len(short_today)
    lines.append("Day P&L: $%+.2f  (%d trades)" % (day_pnl, day_trades))

    # Short positions (paper)
    lines.append(sep)
    lines.append("\U0001fa78 SHORT POSITIONS (Wounded Buffalo)")
    lines.append(sep)
    if not short_positions:
        lines.append("No short positions open.")
    else:
        for s_ticker, s_pos in short_positions.items():
            s_entry = s_pos["entry_price"]
            s_shares = s_pos["shares"]
            s_bars = fetch_1min_bars(s_ticker)
            if s_bars:
                s_cur = s_bars["current_price"]
                s_pnl = (s_entry - s_cur) * s_shares
                if s_pos.get("trail_active") and s_pos.get("trail_stop") and s_pos["trail_stop"] > 0:
                    s_low = s_pos.get("trail_low", 0)
                    s_stop_txt = "$%.2f [\U0001f3af trail | low  $%.2f]" % (s_pos["trail_stop"], s_low)
                else:
                    s_stop_txt = "$%.2f [stop]" % s_pos["stop"]
                lines.append("%s  Entry $%.2f  Stop %s"
                             % (s_ticker, s_entry, s_stop_txt))
                lines.append("      Current $%.2f  P&L $%+.2f"
                             % (s_cur, s_pnl))
            else:
                lines.append("%s  Entry $%.2f  Stop $%.2f  (price unavailable)"
                             % (s_ticker, s_entry, s_pos["stop"]))

    lines.append("Paper Cash:           $%s" % format(paper_cash, ",.2f"))

    # Portfolio equity summary. See note in the TP branch above for the
    # short accounting fix (v3.3.3 hotfix).
    short_unreal = 0.0
    short_liability = 0.0
    for s_ticker, s_pos in short_positions.items():
        s_bars = fetch_1min_bars(s_ticker)
        cur_px = s_bars["current_price"] if s_bars else s_pos["entry_price"]
        short_unreal += (s_pos["entry_price"] - cur_px) * s_pos["shares"]
        short_liability += cur_px * s_pos["shares"]
    all_unreal = total_unreal_pnl + short_unreal
    equity = paper_cash + total_market_value - short_liability
    vs_start = equity - PAPER_STARTING_CAPITAL
    lines.append(sep)
    lines.append("\U0001f4bc Portfolio Snapshot")
    lines.append("  Cash:          $%s" % format(paper_cash, ",.2f"))
    lines.append("  Long MV:       $%s" % format(total_market_value, ",.2f"))
    if short_liability > 0:
        lines.append("  Short Liab:    $%s" % format(short_liability, ",.2f"))
    lines.append("  Total Equity:  $%s" % format(equity, ",.2f"))
    lines.append("  Unrealized P&L:    $%+.2f" % all_unreal)
    lines.append("  vs Start:        $%+.2f  (started at $%s)"
                 % (vs_start, format(PAPER_STARTING_CAPITAL, ",.0f")))
    lines.append(sep)

    # OR status
    if or_collected_date == now_et.strftime("%Y-%m-%d"):
        lines.append("OR: collected")
    else:
        lines.append("OR: not yet collected")

    # PDC status (v3.4.34: swapped from AVWAP)
    spy_pdc_s = pdc.get("SPY") or 0
    qqq_pdc_s = pdc.get("QQQ") or 0
    if spy_pdc_s > 0:
        lines.append("SPY PDC: $%.2f" % spy_pdc_s)
    if qqq_pdc_s > 0:
        lines.append("QQQ PDC: $%.2f" % qqq_pdc_s)

    return "\n".join(lines)


def _build_positions_text():
    """Build positions text for refresh callback."""
    now_et = _now_et()
    sep = "\u2500" * 34
    pos_dict = positions
    short_dict = short_positions
    trades_list = paper_trades
    short_hist = short_trade_history
    cash = paper_cash
    label = "Open Positions"
    cash_label = "Paper Cash"

    n_pos = len(pos_dict)
    lines = ["%s (%d)" % (label, n_pos), sep]
    total_unreal = 0.0
    total_market_value = 0.0
    if not pos_dict:
        lines.append("No open positions")
    else:
        for ticker, pos in pos_dict.items():
            bars = fetch_1min_bars(ticker)
            ep = pos["entry_price"]
            sh = pos["shares"]
            if bars:
                cur = bars["current_price"]
                pnl = (cur - ep) * sh
                pct = ((cur - ep) / ep * 100) if ep else 0
                mkt_val = cur * sh
                total_unreal += pnl
                total_market_value += mkt_val
                if pos.get("trail_active") and pos.get("trail_stop") and pos["trail_stop"] > 0:
                    peak = pos.get("trail_high", 0)
                    stop_line = "  Stop:   $%.2f [\U0001f3af trail | peak $%.2f]" % (pos["trail_stop"], peak)
                else:
                    stop_line = "  Stop:   $%.2f [stop]" % pos["stop"]
                lines.append("%s  %d shares" % (ticker, sh))
                lines.append("  Entry:  $%.2f  ->  Now: $%.2f" % (ep, cur))
                lines.append("  P&L:   $%+.2f (%+.1f%%)" % (pnl, pct))
                lines.append("  Value:  $%s" % format(mkt_val, ",.2f"))
                lines.append(stop_line)
            else:
                mkt_val = ep * sh
                total_market_value += mkt_val
                lines.append("%s  %d shares" % (ticker, sh))
                lines.append("  Entry:  $%.2f  ->  price unavailable" % ep)
            lines.append(sep)
    if pos_dict:
        lines.append("Total Unrealized P&L: $%+.2f" % total_unreal)
        lines.append("Total Market Value:   $%s" % format(total_market_value, ",.2f"))
    today = now_et.strftime("%Y-%m-%d")
    today_sells = [t for t in trades_list if t.get("action") == "SELL" and t.get("date") == today]
    short_today = [t for t in short_hist if t.get("date") == today]
    day_pnl = sum(t.get("pnl", 0) for t in today_sells) + sum(t.get("pnl", 0) for t in short_today)
    day_trades = len(today_sells) + len(short_today)
    lines.append("Day P&L: $%+.2f  (%d trades)" % (day_pnl, day_trades))
    lines.append(sep)
    lines.append("\U0001fa78 SHORT POSITIONS (Wounded Buffalo)")
    lines.append(sep)
    if not short_dict:
        lines.append("No short positions open.")
    else:
        for s_ticker, s_pos in short_dict.items():
            s_entry = s_pos["entry_price"]
            s_shares = s_pos["shares"]
            s_bars = fetch_1min_bars(s_ticker)
            if s_bars:
                s_cur = s_bars["current_price"]
                s_pnl = (s_entry - s_cur) * s_shares
                if s_pos.get("trail_active") and s_pos.get("trail_stop") and s_pos["trail_stop"] > 0:
                    s_low = s_pos.get("trail_low", 0)
                    s_stop_txt = "$%.2f [\U0001f3af trail | low  $%.2f]" % (s_pos["trail_stop"], s_low)
                else:
                    s_stop_txt = "$%.2f [stop]" % s_pos["stop"]
                lines.append("%s  Entry $%.2f  Stop %s" % (s_ticker, s_entry, s_stop_txt))
                lines.append("      Current $%.2f  P&L $%+.2f" % (s_cur, s_pnl))
            else:
                lines.append("%s  Entry $%.2f  Stop $%.2f  (price unavailable)"
                             % (s_ticker, s_entry, s_pos["stop"]))
    lines.append("%s:           $%s" % (cash_label, format(cash, ",.2f")))

    # Portfolio equity summary. Short accounting fix (v3.3.3): the
    # short-sale proceeds are already in cash; the short contributes
    # only its unrealized P&L to equity. Previously we added
    # entry_price * shares as "market value", which double-counted
    # the proceeds and inflated equity by roughly the short principal.
    short_unreal = 0.0
    short_liability = 0.0
    for s_ticker, s_pos in short_dict.items():
        s_bars = fetch_1min_bars(s_ticker)
        cur_px = s_bars["current_price"] if s_bars else s_pos["entry_price"]
        short_unreal += (s_pos["entry_price"] - cur_px) * s_pos["shares"]
        short_liability += cur_px * s_pos["shares"]
    all_unreal = total_unreal + short_unreal
    equity = cash + total_market_value - short_liability
    _start_cap = PAPER_STARTING_CAPITAL
    vs_start = equity - _start_cap
    snap_label = "\U0001f4bc Portfolio Snapshot"
    lines.append(sep)
    lines.append(snap_label)
    lines.append("  Cash:          $%s" % format(cash, ",.2f"))
    lines.append("  Long MV:       $%s" % format(total_market_value, ",.2f"))
    if short_liability > 0:
        lines.append("  Short Liab:    $%s" % format(short_liability, ",.2f"))
    lines.append("  Total Equity:  $%s" % format(equity, ",.2f"))
    lines.append("  Unrealized P&L:    $%+.2f" % all_unreal)
    lines.append("  vs Start:        $%+.2f  (started at $%s)"
                 % (vs_start, format(_start_cap, ",.0f")))
    lines.append(sep)

    return "\n".join(lines)


async def positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle refresh button tap on /status and /positions.

    Appends a 'Refreshed HH:MM:SS CDT' footer so each tap produces a
    visibly different message \u2014 Telegram rejects edits whose body
    and markup are identical to the current message with
    'Message is not modified'. If that race still wins (rapid double
    tap in the same second), we swallow the error silently; the user
    already got the button-tap acknowledgment via query.answer().
    """
    query = update.callback_query
    await query.answer("Refreshing...")
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(None, _build_positions_text)
    # Ensure content changes between taps even if prices and positions
    # are momentarily identical (common outside market hours).
    stamp = _now_cdt().strftime("%H:%M:%S CDT")
    msg = "%s\n\u21bb Refreshed %s" % (msg, stamp)
    refresh_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
    ]])
    try:
        await query.edit_message_text(msg, reply_markup=refresh_kb)
    except Exception as e:
        # Harmless race ("Message is not modified") \u2014 don't surface.
        logger.debug("positions_callback edit failed: %s", e)


def _dayreport_time(t, key):
    """Extract display time HH:MM from a trade record (CDT)."""
    iso = t.get(key + "_iso", "")
    if iso:
        return _parse_time_to_cdt(iso)
    raw = t.get(key, "")
    if raw and ":" in raw:
        return _parse_time_to_cdt(raw)
    return "..."


def _dayreport_sort_key(t):
    """Sort key for chronological ordering of trades."""
    iso = t.get("exit_time_iso", "")
    if iso:
        return iso
    return t.get("exit_time", "") or t.get("date", "")


def _short_reason(reason_key):
    """Map a reason key to short dayreport label."""
    full = REASON_LABELS.get(reason_key, reason_key)
    # Match by leading emoji character
    if full:
        first_char = full[0]
        if first_char in _SHORT_REASON:
            return _SHORT_REASON[first_char]
    return full


def _fmt_pnl(val):
    """Format P&L with unicode minus."""
    if val < 0:
        return "\u2212$%.2f" % abs(val)
    return "+$%.2f" % val


def _chart_dayreport(trades, day_label):
    """Generate trade P&L bar chart with cumulative line. Returns BytesIO or None."""
    if not MATPLOTLIB_AVAILABLE or not trades:
        return None
    try:
        pnls = [(t.get("pnl") or 0) for t in trades]
        colors = ["#00cc66" if p >= 0 else "#ff4444" for p in pnls]
        fig, ax = plt.subplots(figsize=(8, 4))
        xs = list(range(1, len(pnls) + 1))
        ax.bar(xs, pnls, color=colors)
        ax.axhline(0, color="white", linewidth=0.5)
        ax.set_facecolor("#1a1a2e")
        fig.patch.set_facecolor("#1a1a2e")
        ax.tick_params(colors="white")
        ax.set_title("Trade P&L \u2014 %s" % day_label, color="white")
        ax.set_xlabel("Trade #", color="white")
        ax.set_ylabel("P&L ($)", color="white")
        for spine in ax.spines.values():
            spine.set_color("#444")
        # Cumulative line
        cum = []
        running = 0
        for p in pnls:
            running += p
            cum.append(running)
        ax2 = ax.twinx()
        ax2.plot(xs, cum, color="cyan", linewidth=2, label="Cumulative")
        ax2.tick_params(colors="white")
        ax2.set_ylabel("Cumulative ($)", color="white")
        for spine in ax2.spines.values():
            spine.set_color("#444")
        plt.tight_layout()
        buf = _io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.warning("Chart generation failed: %s", e)
        return None


def _chart_equity_curve(history, label):
    """Generate equity curve line chart. Returns BytesIO or None."""
    if not MATPLOTLIB_AVAILABLE or not history:
        return None
    try:
        # Group by date and compute daily P&L
        daily = {}
        for t in history:
            d = t.get("date", "")
            if d:
                daily[d] = daily.get(d, 0) + (t.get("pnl") or 0)
        if not daily:
            return None
        dates_sorted = sorted(daily.keys())
        daily_pnls = [daily[d] for d in dates_sorted]
        cum = []
        running = 0
        for p in daily_pnls:
            running += p
            cum.append(running)
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(range(len(cum)), cum, color="cyan", linewidth=2)
        ax.fill_between(range(len(cum)), cum, alpha=0.15, color="cyan")
        ax.axhline(0, color="white", linewidth=0.5)
        ax.set_facecolor("#1a1a2e")
        fig.patch.set_facecolor("#1a1a2e")
        ax.tick_params(colors="white")
        ax.set_title("Equity Curve \u2014 %s" % label, color="white")
        ax.set_xlabel("Trading Day", color="white")
        ax.set_ylabel("Cumulative P&L ($)", color="white")
        for spine in ax.spines.values():
            spine.set_color("#444")
        # X-axis date labels
        if len(dates_sorted) <= 15:
            ax.set_xticks(range(len(dates_sorted)))
            short_labels = [d[5:] for d in dates_sorted]  # MM-DD
            ax.set_xticklabels(short_labels, rotation=45, ha="right", fontsize=8, color="white")
        plt.tight_layout()
        buf = _io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.warning("Equity chart generation failed: %s", e)
        return None


def _chart_portfolio_pie(pos_dict, short_dict, cash):
    """Generate portfolio allocation pie chart. Returns BytesIO or None."""
    if not MATPLOTLIB_AVAILABLE:
        return None
    if not pos_dict and not short_dict:
        return None
    try:
        from collections import OrderedDict
        slices = OrderedDict()
        for ticker, pos in pos_dict.items():
            bars = fetch_1min_bars(ticker)
            if bars:
                mkt_val = bars["current_price"] * pos["shares"]
            else:
                mkt_val = pos["entry_price"] * pos["shares"]
            lbl = "%s (L)" % ticker
            slices[lbl] = abs(mkt_val)
        for ticker, pos in short_dict.items():
            bars = fetch_1min_bars(ticker)
            if bars:
                mkt_val = bars["current_price"] * pos["shares"]
            else:
                mkt_val = pos["entry_price"] * pos["shares"]
            lbl = "%s (S)" % ticker
            slices[lbl] = abs(mkt_val)
        if cash > 0:
            slices["Cash"] = cash
        if not slices:
            return None
        labels = list(slices.keys())
        sizes = list(slices.values())
        # Color palette
        base_colors = ["#00cc66", "#ff4444", "#4488ff", "#ffaa00", "#cc44ff",
                       "#00cccc", "#ff6688", "#88cc00", "#ff8800", "#8844ff"]
        colors = []
        ci = 0
        for lbl in labels:
            if lbl == "Cash":
                colors.append("#666666")
            else:
                colors.append(base_colors[ci % len(base_colors)])
                ci += 1
        fig, ax = plt.subplots(figsize=(6, 6))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, colors=colors, autopct="%.1f%%",
            startangle=90, textprops={"color": "white", "fontsize": 10}
        )
        for t in autotexts:
            t.set_color("white")
            t.set_fontsize(9)
        ax.set_title("Portfolio Allocation", color="white", fontsize=14)
        plt.tight_layout()
        buf = _io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.warning("Pie chart generation failed: %s", e)
        return None


def _open_positions_as_pseudo_trades(target_date=None):
    """Build synthetic trade records for currently-open positions.

    v3.3.1: /perf and /dayreport historically only read
    `trade_history` / `short_trade_history`, which are populated on
    exit (sell / cover) \u2014 never on entry. An open-but-uncovered
    position was invisible to both commands even though /status showed
    it fine. This helper produces pseudo-trade records that slot into
    the same rendering pipeline (they have no exit_* fields, so
    _format_dayreport_section treats them as 'time \u2192 open').

    Unrealized P&L is computed from live 1-min bars; if bars are
    unavailable we fall back to 0 (fail-safe \u2014 we do NOT invent a
    price).

    Returns (long_opens, short_opens). Each list is date-filtered to
    `target_date` (YYYY-MM-DD) when provided; otherwise all opens.
    """
    long_pos = positions
    short_pos = short_positions

    long_opens = []
    for ticker, pos in long_pos.items():
        date_str = pos.get("date", "")
        if target_date and date_str != target_date:
            continue
        entry_p = pos.get("entry_price", 0)
        shares = pos.get("shares", 0)
        bars = fetch_1min_bars(ticker)
        cur = bars["current_price"] if bars else None
        if cur is not None and entry_p:
            unreal = round((cur - entry_p) * shares, 2)
            unreal_pct = round((cur - entry_p) / entry_p * 100, 2)
        else:
            unreal = 0.0
            unreal_pct = 0.0
        long_opens.append({
            "ticker": ticker,
            "side": "long",
            "action": "OPEN",
            "shares": shares,
            "entry_price": entry_p,
            "exit_price": cur if cur is not None else entry_p,
            "pnl": unreal,
            "pnl_pct": unreal_pct,
            "unrealized": True,
            "reason": "OPEN",
            "entry_time": pos.get("entry_time", ""),
            "entry_time_iso": pos.get("entry_time", ""),
            "date": date_str,
            "entry_num": pos.get("entry_count", 1),
        })

    short_opens = []
    for ticker, pos in short_pos.items():
        date_str = pos.get("date", "")
        if target_date and date_str != target_date:
            continue
        entry_p = pos.get("entry_price", 0)
        shares = pos.get("shares", 0)
        bars = fetch_1min_bars(ticker)
        cur = bars["current_price"] if bars else None
        if cur is not None and entry_p:
            unreal = round((entry_p - cur) * shares, 2)
            unreal_pct = round((entry_p - cur) / entry_p * 100, 2)
        else:
            unreal = 0.0
            unreal_pct = 0.0
        short_opens.append({
            "ticker": ticker,
            "side": "short",
            "action": "OPEN",
            "shares": shares,
            "entry_price": entry_p,
            "exit_price": cur if cur is not None else entry_p,
            "pnl": unreal,
            "pnl_pct": unreal_pct,
            "unrealized": True,
            "reason": "OPEN",
            "entry_time": pos.get("entry_time", ""),
            "entry_time_iso": pos.get("entry_time", ""),
            "date": date_str,
        })

    return long_opens, short_opens


def _format_dayreport_section(trades, header, count_label):
    """Format one portfolio section for /dayreport (compact 2-line).

    header: e.g. '\U0001f4ca Day Report \u2014 Thu Apr 16' or '' for
        subsequent sections.
    count_label: e.g. 'Paper' or 'TP'.

    v3.3.1: Trades flagged `unrealized=True` (from
    _open_positions_as_pseudo_trades) are shown separately in the
    summary header so the 'closed P&L' number doesn't include live
    marks, and the trade list renders them as '\u2192open' via the
    existing has_exit branch below.
    """
    SEP = "\u2500" * 26
    lines = []
    if header:
        lines.append(header)

    trades_sorted = sorted(trades, key=_dayreport_sort_key) if trades else []
    realized = [t for t in trades_sorted if not t.get("unrealized")]
    unrealized = [t for t in trades_sorted if t.get("unrealized")]
    realized_pnl = sum(t.get("pnl", 0) for t in realized)
    unreal_pnl = sum(t.get("pnl", 0) for t in unrealized)

    lines.append(SEP)
    lines.append("%s: %d closed  P&L: %s"
                 % (count_label, len(realized), _fmt_pnl(realized_pnl)))
    if unrealized:
        lines.append("  Open: %d  Unreal: %s"
                     % (len(unrealized), _fmt_pnl(unreal_pnl)))
    lines.append(SEP)

    for idx, t in enumerate(trades_sorted, 1):
        ticker = t.get("ticker", "?")
        side = t.get("side", "long")
        arrow = "\u2191" if side == "long" else "\u2193"
        entry_p = t.get("entry_price", 0)
        exit_p = t.get("exit_price", t.get("price", 0))
        t_pnl = t.get("pnl", 0)
        reason = t.get("reason", "?")
        in_time = _dayreport_time(t, "entry_time")
        out_time = _dayreport_time(t, "exit_time")

        # Open position: no exit yet
        has_exit = bool(t.get("exit_time_iso") or t.get("exit_time"))
        if has_exit:
            time_span = "%s\u2192%s" % (in_time, out_time)
            price_str = "$%.2f\u2192$%.2f" % (entry_p, exit_p)
        else:
            time_span = "%s\u2192open" % in_time
            price_str = "$%.2f" % entry_p

        line1 = "%2d. %s %s  %s  %s" % (idx, ticker, arrow, time_span, _fmt_pnl(t_pnl))
        line2 = "    %s  %s" % (price_str, _short_reason(reason))
        lines.append(line1)
        lines.append(line2)

    return "\n".join(lines)


async def _reply_in_chunks(message, text, max_len=3800, reply_markup=None):
    """Send text in ≤max_len-char chunks, splitting on newlines."""
    lines = text.split('\n')
    chunk = []
    length = 0
    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if length + line_len > max_len and chunk:
            await message.reply_text('\n'.join(chunk))
            chunk = []
            length = 0
        chunk.append(line)
        length += line_len
    if chunk:
        await message.reply_text('\n'.join(chunk), reply_markup=reply_markup)


def _collect_day_rows(target_str, today_str):
    """Collect all trade-log rows for one day, normalized.

    Returns a list of dicts:
      {"tm": "HH:MM", "ticker": str,
       "action": "BUY"|"SELL"|"SHORT"|"COVER",
       "shares": int, "price": float,
       "stop": float (BUY/SHORT only),
       "pnl": float (SELL/COVER only),
       "pnl_pct": float (SELL/COVER only)}

    v3.4.7: previously the same-day branch only pulled from paper_trades,
    which never contain shorts. Today's shorts (open or closed) were
    silently invisible. Now we pull from four sources for the today
    branch and synthesize rows from history for past dates.
    """
    rows = []
    is_today = (target_str == today_str)

    live_long = paper_trades
    long_hist = trade_history
    short_hist = short_trade_history
    open_shorts = short_positions

    if is_today:
        # Long opens + closes are already in paper_trades
        for t in live_long:
            if t.get("date", "") != target_str:
                continue
            rows.append({
                "tm": t.get("time", "--:--") or "--:--",
                "ticker": t.get("ticker", "?"),
                "action": t.get("action", "?"),
                "shares": t.get("shares", 0) or 0,
                "price": t.get("price", 0) or 0,
                "stop": t.get("stop", 0) or 0,
                "pnl": t.get("pnl", 0) or 0,
                "pnl_pct": t.get("pnl_pct", 0) or 0,
            })
        # Closed shorts today — synthesize an OPEN row + a COVER row
        for t in short_hist:
            if t.get("date", "") != target_str:
                continue
            ticker = t.get("ticker", "?")
            shares = t.get("shares", 0) or 0
            rows.append({
                "tm": (t.get("entry_time") or "--:--")[:5],
                "ticker": ticker, "action": "SHORT", "shares": shares,
                "price": t.get("entry_price", 0) or 0,
                "stop": 0, "pnl": 0, "pnl_pct": 0,
            })
            rows.append({
                "tm": (t.get("exit_time") or "--:--")[:5],
                "ticker": ticker, "action": "COVER", "shares": shares,
                "price": t.get("exit_price", 0) or 0,
                "stop": 0,
                "pnl": t.get("pnl", 0) or 0,
                "pnl_pct": t.get("pnl_pct", 0) or 0,
            })
        # Currently-open shorts from today — add a SHORT open row only
        for ticker, pos in open_shorts.items():
            if pos.get("date", "") != target_str:
                continue
            rows.append({
                "tm": (pos.get("entry_time") or "--:--")[:5],
                "ticker": ticker, "action": "SHORT",
                "shares": pos.get("shares", 0) or 0,
                "price": pos.get("entry_price", 0) or 0,
                "stop": pos.get("stop", 0) or 0,
                "pnl": 0, "pnl_pct": 0,
            })
    else:
        # Past dates: synthesize from history
        for t in long_hist:
            if t.get("date", "") != target_str:
                continue
            ticker = t.get("ticker", "?")
            shares = t.get("shares", 0) or 0
            rows.append({
                "tm": (t.get("entry_time") or "--:--")[:5],
                "ticker": ticker, "action": "BUY", "shares": shares,
                "price": t.get("entry_price", 0) or 0,
                "stop": 0, "pnl": 0, "pnl_pct": 0,
            })
            rows.append({
                "tm": (t.get("exit_time") or "--:--")[:5],
                "ticker": ticker, "action": "SELL", "shares": shares,
                "price": t.get("exit_price", 0) or 0,
                "stop": 0,
                "pnl": t.get("pnl", 0) or 0,
                "pnl_pct": t.get("pnl_pct", 0) or 0,
            })
        for t in short_hist:
            if t.get("date", "") != target_str:
                continue
            ticker = t.get("ticker", "?")
            shares = t.get("shares", 0) or 0
            rows.append({
                "tm": (t.get("entry_time") or "--:--")[:5],
                "ticker": ticker, "action": "SHORT", "shares": shares,
                "price": t.get("entry_price", 0) or 0,
                "stop": 0, "pnl": 0, "pnl_pct": 0,
            })
            rows.append({
                "tm": (t.get("exit_time") or "--:--")[:5],
                "ticker": ticker, "action": "COVER", "shares": shares,
                "price": t.get("exit_price", 0) or 0,
                "stop": 0,
                "pnl": t.get("pnl", 0) or 0,
                "pnl_pct": t.get("pnl_pct", 0) or 0,
            })

    # Sort by time; "--:--" sinks to the end but keeps relative order.
    rows.sort(key=lambda r: (r["tm"] == "--:--", r["tm"]))
    return rows


def _log_sync(target_str, day_label):
    """Build trade log text (pure CPU — run in executor). Returns text or None."""
    SEP = "\u2500" * 34
    today_str = _now_et().strftime("%Y-%m-%d")
    rows = _collect_day_rows(target_str, today_str)
    if not rows:
        return None

    lines = [
        "\U0001f4cb Trade Log \u2014 %s" % day_label,
        SEP,
    ]
    OPENS = ("BUY", "SHORT")
    CLOSES = ("SELL", "COVER")
    n_closed = 0
    day_pnl = 0.0
    for r in rows:
        tm = r["tm"]
        ticker = r["ticker"]
        action = r["action"]
        shares = r["shares"]
        price = r["price"]
        if action in OPENS:
            stop = r["stop"]
            lines.append(
                "%s  %-5s %s  %d @ $%.2f  stop $%.2f"
                % (tm, action, ticker, shares, price, stop)
            )
        else:
            n_closed += 1
            pnl_v = r["pnl"]
            pnl_p = r["pnl_pct"]
            day_pnl += pnl_v
            lines.append(
                "%s  %-5s %s  %d @ $%.2f  P&L: $%+.2f (%+.2f%%)"
                % (tm, action, ticker, shares, price, pnl_v, pnl_p)
            )

    n_open = len(positions) + len(short_positions)
    lines.append(SEP)
    lines.append("Completed: %d trades  Open: %d positions" % (n_closed, n_open))
    lines.append("Day P&L: ${:+,.2f}".format(day_pnl))
    return "\n".join(lines)


def _replay_sync(target_str, day_label):
    """Build replay text (pure CPU — run in executor). Returns text or None."""
    SEP = "\u2500" * 34
    today_str = _now_et().strftime("%Y-%m-%d")

    # Normalize every source into a common row shape:
    #   {"tm": "HH:MM", "ticker": str, "action": "BUY"|"SELL"|"SHORT"|"COVER",
    #    "price": float, "pnl": float (0 for opens)}
    # Same-day source (paper_trades) already uses time/price/action.
    # Historical sources (trade_history / short_trade_history) store one
    # record per CLOSED trade with entry_time/entry_price and
    # exit_time/exit_price, so we synthesize both an open row and a
    # close row for each.
    rows = []

    def _push_live(src):
        for t in src:
            if t.get("date", "") != target_str:
                continue
            rows.append({
                "tm": t.get("time", "--:--"),
                "ticker": t.get("ticker", "?"),
                "action": t.get("action", "?"),
                "price": t.get("price", 0) or 0,
                "pnl": t.get("pnl", 0) or 0,
            })

    def _push_history(src, open_action, close_action):
        for t in src:
            if t.get("date", "") != target_str:
                continue
            ticker = t.get("ticker", "?")
            rows.append({
                "tm": t.get("entry_time", "--:--") or "--:--",
                "ticker": ticker,
                "action": open_action,
                "price": t.get("entry_price", 0) or 0,
                "pnl": 0,
            })
            rows.append({
                "tm": t.get("exit_time", "--:--") or "--:--",
                "ticker": ticker,
                "action": close_action,
                "price": t.get("exit_price", 0) or 0,
                "pnl": t.get("pnl", 0) or 0,
            })

    def _push_open_shorts(src):
        # Currently-open short positions on the target date — add a SHORT
        # row only (no close row yet). v3.4.7: replay missed today's open
        # shorts because paper_trades never holds shorts.
        for ticker, pos in src.items():
            if pos.get("date", "") != target_str:
                continue
            rows.append({
                "tm": (pos.get("entry_time") or "--:--")[:5],
                "ticker": ticker,
                "action": "SHORT",
                "price": pos.get("entry_price", 0) or 0,
                "pnl": 0,
            })

    prefix = ""
    if target_str == today_str:
        _push_live(paper_trades)
        # v3.4.7: today's shorts (closed + open) live elsewhere
        _push_history(short_trade_history, "SHORT", "COVER")
        _push_open_shorts(short_positions)
    else:
        _push_history(trade_history, "BUY", "SELL")
        _push_history(short_trade_history, "SHORT", "COVER")

    # Sort by time; unknown "--:--" sinks to the end but keeps relative order.
    rows.sort(key=lambda r: (r["tm"] == "--:--", r["tm"]))
    if not rows:
        return None

    lines = [
        "\U0001f504 %sTrade Replay \u2014 %s" % (prefix, day_label),
        SEP,
    ]
    cum_pnl = 0.0
    open_count = 0
    wins = 0
    losses = 0
    OPENS = ("BUY", "SHORT")
    for r in rows:
        tm = r["tm"]
        ticker = r["ticker"]
        action = r["action"]
        price = r["price"]
        if action in OPENS:
            open_count += 1
            lines.append(
                "%s \u2192 %-5s %s  $%.2f  [positions: %d]"
                % (tm, action, ticker, price, open_count)
            )
        else:
            open_count = max(0, open_count - 1)
            pnl_val = r["pnl"]
            cum_pnl += pnl_val
            if pnl_val > 0:
                wins += 1
            else:
                losses += 1
            cum_fmt = "%+.2f" % cum_pnl
            lines.append(
                "%s \u2192 %-5s %s  $%.2f  $%+.2f   cumP&L: $%s"
                % (tm, action, ticker, price, pnl_val, cum_fmt)
            )
    lines.append(SEP)
    n_sells = wins + losses
    cum_pnl_fmt = "%+.2f" % cum_pnl
    lines.append(
        "Final P&L: $%s  |  Trades: %d  |  W: %d  L: %d"
        % (cum_pnl_fmt, n_sells, wins, losses)
    )
    return "\n".join(lines)


# ============================================================
# /near_misses COMMAND (v3.4.21)
# ============================================================


# ============================================================
# /retighten COMMAND (v3.4.23)
# ============================================================


# ============================================================
# /trade_log COMMAND — last 10 persistent-log entries (v3.4.27)
# ============================================================


# ============================================================
# /tp_sync COMMAND — TradersPost broker sync status (v3.4.15)
# ============================================================


# ============================================================
# /rh_enable /rh_disable /rh_status \u2014 live-trading kill switch
# ============================================================


# ============================================================
# /mode COMMAND — market mode classifier (observation only)
# ============================================================


# ============================================================
# /algo COMMAND
# ============================================================


# ============================================================
# /strategy COMMAND
# ============================================================


# ============================================================
# /reset COMMAND (Fix C)
# ============================================================

# Window in seconds during which a "Confirm" tap is accepted after the
# /reset command was issued. Beyond this, the callback is rejected — this
# prevents scrolling up to an old /reset message tomorrow and tapping
# Confirm by accident.
RESET_CONFIRM_WINDOW_SEC = 60


def _reset_buttons(action: str) -> InlineKeyboardMarkup:
    """Build a Confirm/Cancel keyboard where Confirm carries a fresh ts."""
    ts = int(time.time())
    confirm_data = "reset_%s_confirm:%d" % (action, ts)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 Confirm", callback_data=confirm_data),
        InlineKeyboardButton("\u274c Cancel", callback_data="reset_cancel"),
    ]])



# ============================================================
# /perf COMMAND (Feature 5)
# ============================================================
def _perf_compute(long_history, short_hist, date_filter, single_day, today,
                  label, perf_label, long_opens=None, short_opens=None):
    """Synchronous helper: crunch all perf stats + chart. Runs in executor.

    v3.3.1: `long_opens` / `short_opens` are lists of pseudo-trades for
    currently-open positions (see `_open_positions_as_pseudo_trades`).
    They are NOT folded into the realized-performance math (would
    pollute win-rate / totals with live marks). They render as a
    dedicated 'Open Positions' section so the user can see unrealized
    P&L alongside historical stats.
    """
    long_opens = long_opens or []
    short_opens = short_opens or []
    SEP = "\u2500" * 34

    if single_day:
        filt_long = [t for t in long_history if t.get("date", "") == date_filter]
        filt_short = [t for t in short_hist if t.get("date", "") == date_filter]
    elif date_filter:
        filt_long = [t for t in long_history if t.get("date", "") >= date_filter]
        filt_short = [t for t in short_hist if t.get("date", "") >= date_filter]
    else:
        filt_long = list(long_history)
        filt_short = list(short_hist)

    lines = [
        "\U0001f4c8 Performance \u2014 %s \u2014 %s" % (label, perf_label),
        SEP,
    ]

    # Open Positions section (v3.3.1)
    if long_opens or short_opens:
        lines.append("\U0001f4cc Open Positions")
        total_unreal = 0.0
        for p in long_opens:
            tk = p.get("ticker", "?")
            ep = p.get("entry_price", 0)
            sh = p.get("shares", 0)
            cp = p.get("exit_price", ep)
            pl = p.get("pnl", 0)
            pct = p.get("pnl_pct", 0)
            total_unreal += pl
            lines.append("  \u2191 %s  %d sh  $%.2f \u2192 $%.2f"
                         % (tk, sh, ep, cp))
            lines.append("      Unreal: $%+.2f (%+.2f%%)" % (pl, pct))
        for p in short_opens:
            tk = p.get("ticker", "?")
            ep = p.get("entry_price", 0)
            sh = p.get("shares", 0)
            cp = p.get("exit_price", ep)
            pl = p.get("pnl", 0)
            pct = p.get("pnl_pct", 0)
            total_unreal += pl
            lines.append("  \u2193 %s  %d sh  $%.2f \u2192 $%.2f"
                         % (tk, sh, ep, cp))
            lines.append("      Unreal: $%+.2f (%+.2f%%)" % (pl, pct))
        lines.append("  Total Unrealized: $%+.2f" % total_unreal)
        lines.append(SEP)

    # LONG Performance
    lines.append("\U0001f4c8 LONG Performance")
    all_stats = _compute_perf_stats(filt_long)
    if all_stats:
        best_tk = all_stats["best"].get("ticker", "?")
        best_pnl = all_stats["best"].get("pnl", 0)
        worst_tk = all_stats["worst"].get("ticker", "?")
        worst_pnl = all_stats["worst"].get("pnl", 0)
        lines.append("  Trades:    %d  (W:%d  L:%d)" % (
            all_stats["n"], all_stats["wins"], all_stats["losses"]))
        lines.append("  Win Rate:  %.1f%%" % all_stats["wr"])
        lines.append("  Total P&L: $%+.2f" % all_stats["total_pnl"])
        lines.append("  Avg Win:   $%+.2f  Avg Loss: $%+.2f"
                     % (all_stats["avg_win"], all_stats["avg_loss"]))
        lines.append("  Best:      %s $%+.2f" % (best_tk, best_pnl))
        lines.append("  Worst:     %s $%+.2f" % (worst_tk, worst_pnl))
    else:
        lines.append("  No long trades")
    lines.append(SEP)

    # SHORT Performance
    lines.append("\U0001f4c9 SHORT Performance")
    short_stats = _compute_perf_stats(filt_short)
    if short_stats:
        s_best_tk = short_stats["best"].get("ticker", "?")
        s_best_pnl = short_stats["best"].get("pnl", 0)
        s_worst_tk = short_stats["worst"].get("ticker", "?")
        s_worst_pnl = short_stats["worst"].get("pnl", 0)
        lines.append("  Trades:    %d  (W:%d  L:%d)" % (
            short_stats["n"], short_stats["wins"], short_stats["losses"]))
        lines.append("  Win Rate:  %.1f%%" % short_stats["wr"])
        lines.append("  Total P&L: $%+.2f" % short_stats["total_pnl"])
        lines.append("  Avg Win:   $%+.2f  Avg Loss: $%+.2f"
                     % (short_stats["avg_win"], short_stats["avg_loss"]))
        lines.append("  Best:      %s $%+.2f" % (s_best_tk, s_best_pnl))
        lines.append("  Worst:     %s $%+.2f" % (s_worst_tk, s_worst_pnl))
    else:
        lines.append("  No short trades")
    lines.append(SEP)

    # Combined today
    today_long = _compute_perf_stats(long_history, date_filter=today)
    today_short = _compute_perf_stats(short_hist, date_filter=today)
    lines.append("Today")
    if today_long:
        lines.append("  Long:  %d trades  P&L $%+.2f"
                     % (today_long["n"], today_long["total_pnl"]))
    if today_short:
        lines.append("  Short: %d trades  P&L $%+.2f"
                     % (today_short["n"], today_short["total_pnl"]))
    if not today_long and not today_short:
        lines.append("  No trades today")
    lines.append(SEP)

    # Streak (combined)
    combined = list(long_history) + list(short_hist)
    streak = _compute_streak(combined)
    lines.append("Streak: %s" % streak)

    msg = "\n".join(lines)

    # Chart: Equity curve
    chart_buf = None
    if MATPLOTLIB_AVAILABLE:
        chart_hist = filt_long + filt_short
        if chart_hist:
            chart_buf = _chart_equity_curve(chart_hist, perf_label)

    return msg, chart_buf


# ============================================================
# /price COMMAND (Feature 6)
# ============================================================
def _price_sync(ticker):
    """Build price text (blocking I/O — run in executor). Returns text or None."""
    SEP = "\u2500" * 34

    bars = fetch_1min_bars(ticker)
    if not bars:
        return None

    cur_price = bars["current_price"]
    pdc_val = bars["pdc"]
    change = cur_price - pdc_val
    change_pct = (change / pdc_val * 100) if pdc_val else 0

    header = "\U0001f4b0 %s  $%.2f  $%+.2f (%+.2f%%)" % (ticker, cur_price, change, change_pct)

    if ticker not in TRADE_TICKERS:
        return header

    lines = [header, SEP]

    # OR High
    orh = or_high.get(ticker)
    if orh is not None:
        dist = cur_price - orh
        if cur_price > orh:
            or_status = "\u2705 Above (by $%.2f)" % dist
        else:
            or_status = "\u274c Below (by $%.2f)" % abs(dist)
        lines.append("OR High:  $%.2f  %s" % (orh, or_status))
    else:
        lines.append("OR High:  not collected")

    # OR Low
    orl = or_low.get(ticker)
    if orl is not None:
        dist_low = cur_price - orl
        if cur_price < orl:
            orl_status = "\U0001fa78 Below (by $%.2f)" % abs(dist_low)
        else:
            orl_status = "\u2705 Above (by $%.2f)" % dist_low
        lines.append("OR Low:   $%.2f  %s" % (orl, orl_status))
    else:
        lines.append("OR Low:   not collected")

    # PDC
    pdc_strat = pdc.get(ticker)
    if pdc_strat is not None:
        if cur_price > pdc_strat:
            pdc_status = "\u2705 Above (green)"
        else:
            pdc_status = "\u274c Below (red)"
        lines.append("PDC:      $%.2f  %s" % (pdc_strat, pdc_status))
    else:
        lines.append("PDC:      $%.2f" % pdc_val)

    # SPY/QQQ vs PDC (v3.4.34: swapped from AVWAP)
    spy_pdc_t = pdc.get("SPY") or 0
    qqq_pdc_t = pdc.get("QQQ") or 0
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price_val = spy_bars["current_price"] if spy_bars else 0
    qqq_price_val = qqq_bars["current_price"] if qqq_bars else 0
    spy_ok = (spy_price_val > spy_pdc_t) if (spy_bars and spy_pdc_t > 0) else False
    qqq_ok = (qqq_price_val > qqq_pdc_t) if (qqq_bars and qqq_pdc_t > 0) else False
    spy_below = (spy_price_val < spy_pdc_t) if (spy_bars and spy_pdc_t > 0) else False
    qqq_below = (qqq_price_val < qqq_pdc_t) if (qqq_bars and qqq_pdc_t > 0) else False
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"
    filter_status = "active" if (spy_ok and qqq_ok) else "inactive"
    lines.append("SPY/QQQ:  %s %s Index filters %s" % (spy_icon, qqq_icon, filter_status))
    lines.append(SEP)

    # Long entry eligible?
    in_position = ticker in positions
    at_max_entries = daily_entry_count.get(ticker, 0) >= 5
    index_ok = spy_ok and qqq_ok
    long_eligible = not in_position and not at_max_entries and index_ok and not _trading_halted

    if long_eligible:
        lines.append("Long eligible:  YES")
    else:
        reasons = []
        if in_position:
            reasons.append("in position")
        if at_max_entries:
            reasons.append("5 entries today")
        if not index_ok:
            reasons.append("index filter fails")
        if _trading_halted:
            reasons.append("trading halted")
        reason_str = ", ".join(reasons)
        lines.append("Long eligible:  NO (%s)" % reason_str)

    # Short entry eligible?
    in_short = ticker in short_positions
    at_max_shorts = daily_short_entry_count.get(ticker, 0) >= 5
    index_bearish = spy_below and qqq_below
    below_or_low = (orl is not None and cur_price < orl)
    below_pdc_short = (pdc_strat is not None and cur_price < pdc_strat)
    short_eligible = (not in_short and not at_max_shorts and index_bearish
                      and below_or_low and below_pdc_short and not _trading_halted)

    if short_eligible:
        lines.append("Short eligible: YES")
    else:
        s_reasons = []
        if in_short:
            s_reasons.append("in short position")
        if at_max_shorts:
            s_reasons.append("5 short entries today")
        if not index_bearish:
            s_reasons.append("index filter not bearish")
        if not below_or_low:
            s_reasons.append("above OR Low")
        if not below_pdc_short:
            s_reasons.append("above PDC")
        if _trading_halted:
            s_reasons.append("trading halted")
        s_reason_str = ", ".join(s_reasons)
        lines.append("Short eligible: NO (%s)" % s_reason_str)

    return "\n".join(lines)


# ============================================================
# /proximity COMMAND (v3.3.0)
# ============================================================
def _proximity_sync():
    """Build proximity text (blocking I/O \u2014 run in executor).

    Shows how far each ticker is from its OR-breakout trigger, plus the
    SPY/QQQ vs PDC global gate. Read-only diagnostic view \u2014 does
    NOT change any trade logic or adaptive parameters.
    v3.4.34: anchor swapped from AVWAP to PDC.

    Every visible line is <= 34 chars incl. leading 2-space indent so it
    renders without wrap inside a Telegram mobile monospace block.

    Returns (text, None) on success or (None, err_msg) on no-data.
    """
    SEP = "\u2500" * 34
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date != today:
        return None, "OR not collected yet \u2014 runs at 8:35 CT."

    # Pick the positions dicts for open-trade markers
    longs_dict = positions
    shorts_dict = short_positions

    # --- Global: SPY/QQQ vs PDC (the long gate, v3.4.34) ---
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0.0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0.0
    spy_pdc_p = pdc.get("SPY") or 0
    qqq_pdc_p = pdc.get("QQQ") or 0

    spy_have = spy_price > 0 and spy_pdc_p > 0
    qqq_have = qqq_price > 0 and qqq_pdc_p > 0
    spy_ok = spy_have and spy_price > spy_pdc_p
    qqq_ok = qqq_have and qqq_price > qqq_pdc_p
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"

    long_ok = spy_ok and qqq_ok
    # Short anchor is the mirror: SPY AND QQQ both BELOW PDC enables shorts.
    short_ok = (spy_have and qqq_have
                and spy_price < spy_pdc_p
                and qqq_price < qqq_pdc_p)

    if long_ok:
        verdict = "LONGS enabled"
    elif short_ok:
        verdict = "SHORTS enabled"
    else:
        verdict = "NO NEW TRADES"

    now_ct = now_et.astimezone(CDT)
    hdr_time = now_ct.strftime("%H:%M CT")

    lines = [
        "\U0001f3af PROXIMITY \u2014 %s" % hdr_time,
        SEP,
    ]

    # Index rows: "SPY $707.67 \u2705 vs $708.78"
    def _idx_row(tag, px, av, icon):
        if not (px > 0 and av > 0):
            return "%s  --" % tag
        return "%s $%.2f %s vs $%.2f" % (tag, px, icon, av)

    lines.append(_idx_row("SPY", spy_price, spy_pdc_p, spy_icon))
    lines.append(_idx_row("QQQ", qqq_price, qqq_pdc_p, qqq_icon))
    lines.append("Gate: %s" % verdict)
    lines.append(SEP)

    # --- Per-ticker rows ---
    # Build one snapshot per ticker: price, gap_long (px - OR_High),
    # gap_short (px - OR_Low), polarity vs PDC, open-position marker.
    rows = []  # list of dicts
    for t in TRADE_TICKERS:
        orh = or_high.get(t)
        orl = or_low.get(t)
        pdc_val = pdc.get(t)
        bars = fetch_1min_bars(t)
        px = bars["current_price"] if bars else 0.0
        # Open-position marker: long takes precedence if somehow both
        # (shouldn't happen, but defensive).
        has_long = t in longs_dict
        has_short = t in shorts_dict
        if has_long:
            open_mark = "\U0001f7e2"  # green circle
        elif has_short:
            open_mark = "\U0001f534"  # red circle
        else:
            open_mark = ""
        if not (px > 0):
            rows.append({"t": t, "px": 0.0, "orh": orh, "orl": orl,
                         "pdc": pdc_val, "gl": None, "gs": None,
                         "pol": None, "mark": open_mark})
            continue
        gl = (px - orh) if (orh is not None) else None
        gs = (px - orl) if (orl is not None) else None
        pol = None
        if pdc_val is not None:
            pol = 1 if px > pdc_val else (-1 if px < pdc_val else 0)
        rows.append({"t": t, "px": px, "orh": orh, "orl": orl,
                     "pdc": pdc_val, "gl": gl, "gs": gs, "pol": pol,
                     "mark": open_mark})

    # ---- LONGS table: sorted by distance to OR High ----
    # Already above OR High (gl >= 0) first (closest to / past trigger),
    # then the rest ascending by |gl|. Unknowns go last.
    def _long_key(r):
        gl = r["gl"]
        if gl is None:
            return (2, 0.0)
        if gl >= 0:
            # Above trigger: rank by how far above (closer to trigger first)
            return (0, gl)
        return (1, -gl)  # below trigger: ascending gap

    longs_sorted = sorted(rows, key=_long_key)
    lines.append("LONGS \u2014 gap to OR High")
    for r in longs_sorted:
        t = r["t"]
        gl = r["gl"]
        orh = r["orh"]
        px = r["px"]
        om = r["mark"]
        # Open-marker replaces the 2-space indent when present (emoji
        # occupies ~2 monospace cells). Falls back to "  " otherwise so
        # tickers align cleanly.
        lead = om if om else "  "
        if gl is None or orh is None or px <= 0:
            lines.append("%s%-4s  --" % (lead, t))
            continue
        pct = (gl / orh) * 100.0 if orh else 0.0
        trig = "\u2705 " if gl >= 0 else "  "
        sign = "+" if gl >= 0 else "-"
        lines.append("%s%-4s %s%s$%.2f (%s%.2f%%)"
                     % (lead, t, trig, sign, abs(gl), sign, abs(pct)))
    lines.append(SEP)

    # ---- SHORTS table: sorted ascending by gap to OR Low ----
    # Most-negative first = already below OR Low (short trigger hit or past).
    def _short_key(r):
        gs = r["gs"]
        if gs is None:
            return (1, 0.0)
        return (0, gs)  # ascending: most negative first

    shorts_sorted = sorted(rows, key=_short_key)
    lines.append("SHORTS \u2014 gap to OR Low")
    for r in shorts_sorted:
        t = r["t"]
        gs = r["gs"]
        orl = r["orl"]
        px = r["px"]
        om = r["mark"]
        lead = om if om else "  "
        if gs is None or orl is None or px <= 0:
            lines.append("%s%-4s  --" % (lead, t))
            continue
        pct = (gs / orl) * 100.0 if orl else 0.0
        trig = "\u2705 " if gs <= 0 else "  "
        sign = "+" if gs >= 0 else "-"
        lines.append("%s%-4s %s%s$%.2f (%s%.2f%%)"
                     % (lead, t, trig, sign, abs(gs), sign, abs(pct)))
    lines.append(SEP)

    # ---- Prices & Polarity vs PDC (compact) ----
    # One cell = "<mark or 2sp><TICKER> $PRICE <arrow>" e.g.
    # "  AAPL $234.56 \u2191" or "\U0001f7e2NVDA $198.00 \u2193". Two
    # cells per row fit within 34ch mobile limit in the common case.
    # If a pair would exceed the budget (e.g. a 4-digit price on one
    # side and an emoji lead on the other), render that pair as two
    # separate rows instead of wrapping.
    lines.append("Prices & Polarity vs PDC")

    def _price_cell(r):
        pol = r["pol"]
        px = r["px"]
        om = r["mark"]
        lead = om if om else "  "
        if pol is None:
            arrow = "?"
        elif pol > 0:
            arrow = "\u2191"
        elif pol < 0:
            arrow = "\u2193"
        else:
            arrow = "="
        if px > 0:
            return "%s%-4s $%.2f %s" % (lead, r["t"], px, arrow)
        return "%s%-4s  --    %s" % (lead, r["t"], arrow)

    def _cell_width(cell):
        # Emoji in lead counts as 2 cells on mobile but 1 codepoint.
        w = len(cell)
        if cell.startswith(("\U0001f7e2", "\U0001f534")):
            w += 1
        return w

    chunk = []
    for r in rows:
        chunk.append(_price_cell(r))
        if len(chunk) == 2:
            combined = "  ".join(chunk)
            # 34 ch mobile budget; fall back to 1-per-row if over.
            if _cell_width(chunk[0]) + 2 + _cell_width(chunk[1]) <= 34:
                lines.append(combined)
            else:
                lines.append(chunk[0])
                lines.append(chunk[1])
            chunk = []
    if chunk:
        lines.append(chunk[0])

    # Legend if any open markers present
    any_long = any(r["mark"] == "\U0001f7e2" for r in rows)
    any_short = any(r["mark"] == "\U0001f534" for r in rows)
    if any_long or any_short:
        legend_bits = []
        if any_long:
            legend_bits.append("\U0001f7e2 long open")
        if any_short:
            legend_bits.append("\U0001f534 short open")
        lines.append(SEP)
        lines.append("  " + "  ".join(legend_bits))

    return "\n".join(lines), None


def _proximity_keyboard():
    """Inline keyboard for /proximity: Refresh + Menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Refresh",
                              callback_data="proximity_refresh")],
        [InlineKeyboardButton("\U0001f3e0 Menu", callback_data="open_menu")],
    ])


async def proximity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle refresh button tap on /proximity."""
    query = update.callback_query
    await query.answer("Refreshing...")
    loop = asyncio.get_event_loop()
    text, err = await loop.run_in_executor(None, _proximity_sync)
    if text is None:
        # Edit to show the error and drop refresh button (no data to refresh)
        try:
            await query.edit_message_text(
                err or "Proximity unavailable.",
                reply_markup=_menu_button(),
            )
        except Exception as e:
            logger.debug("proximity_callback edit (no-data) failed: %s", e)
        return
    body = "```\n" + text + "\n```"
    try:
        await query.edit_message_text(
            body,
            parse_mode="Markdown",
            reply_markup=_proximity_keyboard(),
        )
    except Exception as e:
        # Common case: "Message is not modified" when nothing changed
        # between ticks. Swallow silently \u2014 the user got their ack.
        logger.debug("proximity_callback edit failed: %s", e)


# ============================================================
# /orb COMMAND (Feature 7)
# ============================================================
def _orb_sync():
    """Build ORB text (blocking I/O — run in executor). Returns text or None."""
    SEP = "\u2500" * 34
    now_et = _now_et()
    today = now_et.strftime("%Y-%m-%d")

    if or_collected_date != today:
        return None

    lines = [
        "\U0001f4d0 TODAY'S OR LEVELS \u2014 %s" % today,
        SEP,
    ]

    for t in TRADE_TICKERS:
        orh = or_high.get(t)
        orl = or_low.get(t)
        pdc_val = pdc.get(t)
        if orh is None:
            lines.append("%s   --" % t)
            continue
        orl_str = "%.2f" % orl if orl is not None else "--"
        pdc_str = "%.2f" % pdc_val if pdc_val is not None else "--"
        lines.append(
            "%s   High $%.2f  Low $%s  PDC $%s"
            % (t, orh, orl_str, pdc_str)
        )

    lines.append(SEP)

    # SPY/QQQ vs PDC (v3.4.34: swapped from AVWAP)
    spy_bars = fetch_1min_bars("SPY")
    qqq_bars = fetch_1min_bars("QQQ")
    spy_price = spy_bars["current_price"] if spy_bars else 0
    qqq_price = qqq_bars["current_price"] if qqq_bars else 0
    spy_pdc_u = pdc.get("SPY") or 0
    qqq_pdc_u = pdc.get("QQQ") or 0
    spy_ok = spy_price > spy_pdc_u if spy_pdc_u > 0 else False
    qqq_ok = qqq_price > qqq_pdc_u if qqq_pdc_u > 0 else False
    spy_icon = "\u2705" if spy_ok else "\u274c"
    qqq_icon = "\u2705" if qqq_ok else "\u274c"

    spy_pdc_fmt = "%.2f" % spy_pdc_u if spy_pdc_u > 0 else "n/a"
    qqq_pdc_fmt = "%.2f" % qqq_pdc_u if qqq_pdc_u > 0 else "n/a"
    lines.append("SPY PDC: $%s  %s" % (spy_pdc_fmt, spy_icon))
    lines.append("QQQ PDC: $%s  %s" % (qqq_pdc_fmt, qqq_icon))

    # Entries today
    entry_parts = []
    for t in TRADE_TICKERS:
        cnt = daily_entry_count.get(t, 0)
        if cnt > 0:
            entry_parts.append("%sx%d" % (t, cnt))
    if entry_parts:
        entries_str = " ".join(entry_parts)
        lines.append("Entries today: %s" % entries_str)

    return "\n".join(lines)


# ============================================================
# /monitoring COMMAND (Feature 8)
# ============================================================


async def monitoring_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard taps for /monitoring."""
    global _scan_paused
    query = update.callback_query
    await query.answer()
    if query.data == "monitoring_pause":
        _scan_paused = True
        await query.edit_message_text(
            "\U0001f50d Scanner: PAUSED\n  Tap below to resume.\n  Existing positions still managed.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25b6\ufe0f Resume Scanner", callback_data="monitoring_resume")
            ]])
        )
    elif query.data == "monitoring_resume":
        _scan_paused = False
        await query.edit_message_text(
            "\U0001f50d Scanner: ACTIVE\n  Watching for breakouts.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u23f8 Pause Scanner", callback_data="monitoring_pause")
            ]])
        )


# ============================================================
# MENU KEYBOARD BUILDER + MENU BUTTON HELPER
# ============================================================
def _build_menu_keyboard():
    """Main /menu keyboard \u2014 daily-use commands only.

    Ten tiles in a 2-column grid plus a full-width Advanced button that
    opens the secondary keyboard built by `_build_advanced_menu_keyboard`.
    """
    return [
        [
            InlineKeyboardButton("\U0001f4ca Dashboard", callback_data="menu_dashboard"),
            InlineKeyboardButton("\U0001f4c8 Status", callback_data="menu_positions"),
        ],
        [
            InlineKeyboardButton("\U0001f4c9 Perf", callback_data="menu_perf"),
            InlineKeyboardButton("\U0001f4b0 Price", callback_data="menu_price_prompt"),
        ],
        [
            InlineKeyboardButton("\U0001f4d0 OR", callback_data="menu_orb"),
            InlineKeyboardButton("\U0001f3af Proximity", callback_data="menu_proximity"),
        ],
        [
            InlineKeyboardButton("\U0001f39b\ufe0f Mode", callback_data="menu_mode"),
            InlineKeyboardButton("\u2753 Help", callback_data="menu_help"),
        ],
        [
            InlineKeyboardButton("\U0001f50d Monitor", callback_data="menu_monitoring"),
        ],
        [
            InlineKeyboardButton("\u2699\ufe0f Advanced", callback_data="menu_advanced"),
        ],
    ]


def _build_advanced_menu_keyboard():
    """Advanced /menu keyboard \u2014 rarely-needed commands.

    Accessible via the 'Advanced' button on the main menu. Includes a
    Back button to return to the main keyboard.
    """
    return [
        # Reports
        [
            InlineKeyboardButton("\U0001f4c5 Day Report", callback_data="menu_dayreport"),
            InlineKeyboardButton("\U0001f4dc Log", callback_data="menu_log"),
        ],
        [
            InlineKeyboardButton("\U0001f3ac Replay", callback_data="menu_replay"),
        ],
        # Market data recovery / system
        [
            InlineKeyboardButton("\U0001f504 OR Recover", callback_data="menu_or_recover"),
            InlineKeyboardButton("\U0001f9ea Test", callback_data="menu_test"),
        ],
        # Reference
        [
            InlineKeyboardButton("\U0001f4d8 Strategy", callback_data="menu_strategy"),
            InlineKeyboardButton("\U0001f4d6 Algo", callback_data="menu_algo"),
        ],
        [
            InlineKeyboardButton("\u2139\ufe0f Version", callback_data="menu_version"),
            InlineKeyboardButton("\u26a0\ufe0f Reset", callback_data="menu_reset"),
        ],
        # Nav
        [
            InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="menu_back"),
        ],
    ]


def _menu_button():
    """Return a one-button InlineKeyboardMarkup with a Menu tap."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f5c2 Menu", callback_data="open_menu")]])


# ============================================================
# /menu COMMAND — Quick tap-grid
# ============================================================


async def _cb_open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the single Menu button tap — show full menu."""
    await update.callback_query.answer()
    keyboard = _build_menu_keyboard()
    await update.callback_query.message.reply_text(
        "\U0001f4f1 Quick Menu\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


class _CallbackUpdateShim:
    """Minimal Update-like wrapper that lets cmd_* handlers be invoked from
    an inline-button callback. The handlers only touch update.message.*
    (reply_text / reply_photo / reply_chat_action / reply_document) and
    update.effective_message / update.effective_user, so we forward those
    to the callback_query's message/user.
    """
    __slots__ = ("_query",)

    def __init__(self, query):
        self._query = query

    def get_bot(self):
        return self._query.get_bot()

    @property
    def message(self):
        return self._query.message

    @property
    def effective_message(self):
        return self._query.message

    @property
    def effective_user(self):
        return self._query.from_user

    @property
    def effective_chat(self):
        return self._query.message.chat

    @property
    def callback_query(self):
        # Some code paths may still want the raw query; preserve it.
        return self._query


async def _invoke_from_callback(query, context, handler, *, args=None):
    """Run a cmd_* handler as if it came from a regular message.

    `args` optionally overrides context.args (e.g. to inject a date). The
    override is scoped to this call only; context.args is restored after.
    """
    shim = _CallbackUpdateShim(query)
    saved_args = context.args
    try:
        context.args = list(args) if args is not None else []
        await handler(shim, context)
    finally:
        context.args = saved_args


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle taps on /menu inline buttons."""
    query = update.callback_query
    await query.answer()

    # --- Navigation between main and advanced submenus ---
    if query.data == "menu_advanced":
        try:
            await query.edit_message_text(
                "\u2699\ufe0f Advanced\n" + "\u2500" * 30,
                reply_markup=InlineKeyboardMarkup(_build_advanced_menu_keyboard()),
            )
        except Exception:
            await query.message.reply_text(
                "\u2699\ufe0f Advanced",
                reply_markup=InlineKeyboardMarkup(_build_advanced_menu_keyboard()),
            )
        return
    if query.data == "menu_back":
        try:
            await query.edit_message_text(
                "\U0001f4f1 Quick Menu\n" + "\u2500" * 30,
                reply_markup=InlineKeyboardMarkup(_build_menu_keyboard()),
            )
        except Exception:
            await query.message.reply_text(
                "\U0001f4f1 Quick Menu",
                reply_markup=InlineKeyboardMarkup(_build_menu_keyboard()),
            )
        return

    # --- Lightweight callbacks that replace the menu message in-place ---
    if query.data == "menu_price_prompt":
        await query.edit_message_text("Use /price TICKER (e.g. /price AAPL)")
        return

    if query.data == "menu_version":
        note = MAIN_RELEASE_NOTE
        await query.edit_message_text(
            "%s v%s\n%s" % (BOT_NAME, BOT_VERSION, note))
        return

    if query.data == "menu_strategy":
        await query.edit_message_text("\u23f3 Loading...")
        SEP = "\u2500" * 26
        text = (
            "Strategy v%s\n%s\n" % (BOT_VERSION, SEP)
            + "Long: ORB Breakout after 8:45 CT\n"
            "Short: Wounded Buffalo after 8:45 CT\n"
            "Trail: +1.0%% trigger | min $1.00\n"
            "Size: 10 shares | Max 5/ticker/day\n"
            "%s\nUse /strategy for full details" % SEP
        )
        await query.message.reply_text(text)
        return

    # --- Handlers that execute a real command via the shim ---
    # These don't edit the menu message; they reply with the command's output.
    if query.data == "menu_help":
        await _invoke_from_callback(query, context, telegram_commands.cmd_help)
        return
    if query.data == "menu_algo":
        await _invoke_from_callback(query, context, telegram_commands.cmd_algo)
        return
    if query.data == "menu_mode":
        await _invoke_from_callback(query, context, telegram_commands.cmd_mode)
        return
    if query.data == "menu_log":
        await _invoke_from_callback(query, context, telegram_commands.cmd_log)
        return
    if query.data == "menu_replay":
        await _invoke_from_callback(query, context, telegram_commands.cmd_replay)
        return
    if query.data == "menu_or_recover":
        await _invoke_from_callback(query, context, telegram_commands.cmd_or_now)
        return
    if query.data == "menu_reset":
        # /reset is a two-step confirm flow; delegate to its handler and let
        # it show the same confirmation keyboard it shows on the typed command.
        await _invoke_from_callback(query, context, telegram_commands.cmd_reset)
        return

    await query.edit_message_text("\u23f3 Loading...")

    if query.data == "menu_dashboard":
        # Show the same full dashboard that /dashboard produces.
        # The menu message itself has already been edited to "\u23f3 Loading..."
        # above, so we just swap it out with the real dashboard text.
        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(None, _dashboard_sync)
        except Exception:
            logger.exception("menu_dashboard: _dashboard_sync failed")
            await query.message.reply_text(
                "\u26a0\ufe0f Dashboard failed. Try again.",
                reply_markup=_menu_button(),
            )
            return
        try:
            if len(text) > 3800:
                await _reply_in_chunks(query.message, text, reply_markup=_menu_button())
            else:
                await query.message.reply_text(text, reply_markup=_menu_button())
        except Exception:
            logger.exception("menu_dashboard: send failed")
    elif query.data == "menu_positions":
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, _build_positions_text)
        refresh_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions_refresh")
        ]])
        await query.message.reply_text(msg, reply_markup=refresh_kb)
    elif query.data == "menu_orb":
        now_et = _now_et()
        today = now_et.strftime("%Y-%m-%d")
        if or_collected_date != today:
            await query.message.reply_text("OR not collected yet \u2014 runs at 8:35 CT.")
        else:
            orb_lines = ["\U0001f4d0 TODAY'S OR LEVELS \u2014 %s" % today]
            for t in TRADE_TICKERS:
                orh = or_high.get(t)
                if orh is None:
                    orb_lines.append("%s   --" % t)
                else:
                    orl = or_low.get(t)
                    pdc_val = pdc.get(t)
                    orl_s = "%.2f" % orl if orl else "--"
                    pdc_s = "%.2f" % pdc_val if pdc_val else "--"
                    orb_lines.append("%s  H:$%.2f  L:$%s  PDC:$%s" % (t, orh, orl_s, pdc_s))
            await query.message.reply_text("\n".join(orb_lines))
    elif query.data == "menu_dayreport":
        await _invoke_from_callback(query, context, telegram_commands.cmd_dayreport)
    elif query.data == "menu_proximity":
        await _invoke_from_callback(query, context, telegram_commands.cmd_proximity)
    elif query.data == "menu_perf":
        await _invoke_from_callback(query, context, telegram_commands.cmd_perf)
    elif query.data == "menu_monitoring":
        status = "PAUSED" if _scan_paused else "ACTIVE"
        if _scan_paused:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25b6\ufe0f Resume Scanner", callback_data="monitoring_resume")
            ]])
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("\u23f8 Pause Scanner", callback_data="monitoring_pause")
            ]])
        await query.message.reply_text(
            "\U0001f50d Scanner: %s" % status, reply_markup=kb)
    elif query.data == "menu_test":
        await query.message.reply_text("Running /test ...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _run_system_test_sync, "Manual")


def _fetch_or_for_ticker(ticker):
    """Try Yahoo then FMP to recover OR data for a single ticker. Returns dict or None."""
    now_et = _now_et()
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    or_end = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    open_ts = int(market_open.timestamp())
    end_ts = int(or_end.timestamp())

    # Try Yahoo 1-min bars first
    try:
        bars = fetch_1min_bars(ticker)
        if bars:
            max_high = None
            min_low = None
            for i, ts in enumerate(bars["timestamps"]):
                if open_ts <= ts < end_ts:
                    h = bars["highs"][i]
                    if h is None:
                        h = bars["closes"][i]
                    if h is not None:
                        if max_high is None or h > max_high:
                            max_high = h
                    lo = bars["lows"][i]
                    if lo is None:
                        lo = bars["closes"][i]
                    if lo is not None:
                        if min_low is None or lo < min_low:
                            min_low = lo
            if max_high is not None:
                or_high[ticker] = max_high
                if min_low is not None:
                    or_low[ticker] = min_low
                if bars.get("pdc") and bars["pdc"] > 0:
                    pdc[ticker] = bars["pdc"]
                return {"high": max_high, "low": min_low if min_low else 0, "src": "Yahoo"}
    except Exception as e:
        logger.warning("or_now Yahoo failed for %s: %s", ticker, e)

    # FMP fallback
    try:
        fmp = get_fmp_quote(ticker)
        if fmp and fmp.get("dayHigh") and fmp.get("dayLow"):
            or_high[ticker] = fmp["dayHigh"]
            or_low[ticker] = fmp["dayLow"]
            if fmp.get("previousClose") and fmp["previousClose"] > 0:
                pdc[ticker] = fmp["previousClose"]
            return {"high": fmp["dayHigh"], "low": fmp["dayLow"], "src": "FMP"}
    except Exception as e:
        logger.warning("or_now FMP failed for %s: %s", ticker, e)

    return None


def _or_now_sync():
    """Re-collect missing OR data (blocking I/O — run in executor). Returns text or None."""
    missing = [t for t in TICKERS if t not in or_high]
    if not missing:
        return None

    results = []
    recovered = 0
    still_fail = 0

    for ticker in missing:
        result = _fetch_or_for_ticker(ticker)
        if result is not None:
            recovered += 1
            results.append(
                "%s: \u2705 high=%.2f low=%.2f (%s)"
                % (ticker, result["high"], result["low"], result["src"])
            )
            logger.info("or_now recovered %s: high=%.2f low=%.2f (%s)",
                        ticker, result["high"], result["low"], result["src"])
        else:
            still_fail += 1
            results.append("%s: \u274c still missing" % ticker)
            logger.warning("or_now: %s still missing after Yahoo + FMP", ticker)

    if recovered > 0:
        save_paper_state()

    SEP = "\u2500" * 34
    lines = ["\U0001f504 OR Recovery Complete", SEP]
    lines.extend(results)
    lines.append(SEP)
    lines.append("%d recovered | %d still missing" % (recovered, still_fail))
    return "\n".join(lines)


# ============================================================
# /ticker COMMAND  (v3.4.33 — unified add/remove/list)
# ============================================================
# One command with sub-switches:
#   /ticker list         — show the tracked universe
#   /ticker add SYM      — add + prime PDC/OR/RSI/bars
#   /ticker remove SYM   — drop (SPY/QQQ are pinned, refused)
#
# Back-compat aliases registered as hidden handlers so any saved
# shortcuts still work:
#   /tickers          → /ticker list
#   /add_ticker SYM   → /ticker add SYM
#   /remove_ticker    → /ticker remove SYM
#
# All replies stay within the 34-char Telegram mobile-width budget.
# Mutation and persistence live in add_ticker() / remove_ticker()
# above; these handlers format the response.

_TICKER_USAGE = (
    "Usage: /ticker <sub> [SYM]\n"
    "\n"
    "  /ticker list\n"
    "  /ticker add SYM\n"
    "  /ticker remove SYM\n"
    "\n"
    "Example: /ticker add QBTS"
)


def _fmt_tickers_list() -> str:
    """Render the current ticker universe in a 34-char-safe table.
    Pinned tickers are flagged with an asterisk. Split into rows of
    5 symbols (≈ 30 chars at worst) so every line stays within the
    Telegram mobile code-block width.
    """
    n_total = len(TICKERS)
    n_trade = len(TRADE_TICKERS)
    # Build rows of up to 5 symbols each — SPY and QQQ get a trailing
    # '*' to show they're pinned, so worst case per row is 5*(5+1)+4=34.
    def _tag(t):
        return t + "*" if t in TICKERS_PINNED else t
    rows, row = [], []
    for t in TICKERS:
        row.append(_tag(t))
        if len(row) == 5:
            rows.append(" ".join(row))
            row = []
    if row:
        rows.append(" ".join(row))
    body = "\n".join(rows) if rows else "(empty)"
    return (
        "\U0001f4cb Tracked Tickers\n"
        "%s\n%s\n%s\n"
        "%d total  \u00b7  %d tradable\n"
        "* = pinned (regime anchor)"
    ) % ("\u2500" * 26, body, "\u2500" * 26, n_total, n_trade)


def _fmt_add_reply(res: dict) -> str:
    """Format the reply for /ticker add. 34-char-safe."""
    t = res.get("ticker", "?")
    if not res.get("ok"):
        return "\u274c Can't add %s\n%s" % (t, res.get("reason", "unknown"))
    if not res.get("added"):
        return "\u2139\ufe0f %s already tracked" % t
    metrics = res.get("metrics") or {}
    pdc_ok = metrics.get("pdc")
    pdc_src = metrics.get("pdc_src", "none")
    or_ok = metrics.get("or")
    or_pending = metrics.get("or_pending")
    rsi_ok = metrics.get("rsi")
    rsi_val = metrics.get("rsi_val")
    bars_ok = metrics.get("bars")
    pdc_val = pdc.get(t)
    orh_val = or_high.get(t)
    orl_val = or_low.get(t)

    # Each metric gets one 34-char-safe status line.
    m_lines = []

    # Bars liveness probe — the foundation everything else depends on.
    m_lines.append(
        "Bars:  " + ("\u2705 reachable" if bars_ok
                     else "\u26a0 unreachable"))

    # PDC with source tag so the user knows which provider answered.
    if pdc_ok and pdc_val is not None:
        src_tag = " (%s)" % pdc_src if pdc_src in ("fmp", "bars") else ""
        m_lines.append("PDC:   $%.2f%s" % (pdc_val, src_tag))
    else:
        m_lines.append("PDC:   \u2014 (pending)")

    # OR high – low, or an explicit pending / missing reason.
    if or_ok and orh_val is not None and orl_val is not None:
        m_lines.append("OR:    $%.2f \u2013 $%.2f" % (orl_val, orh_val))
    elif or_pending:
        m_lines.append("OR:    pending 09:35 ET")
    else:
        m_lines.append("OR:    \u2014 (retry /or_now)")

    # RSI warm-up — proves bar history is deep enough.
    if rsi_ok and rsi_val is not None:
        m_lines.append("RSI:   %.1f (warm)" % rsi_val)
    else:
        m_lines.append("RSI:   \u2014 (warms on scan)")

    errs = [e for e in (metrics.get("errors") or []) if e]
    tail = ""
    if errs:
        # Truncate per-line to stay within the 34-char budget.
        tail = "\nnote: " + errs[0][:26]
    return (
        "\u2705 Added %s\n"
        "%s\n"
        "%s\n"
        "%s\n"
        "Next scan will trade it.%s"
    ) % (t, "\u2500" * 26, "\n".join(m_lines), "\u2500" * 26, tail)


def _fmt_remove_reply(res: dict) -> str:
    """Format the reply for /ticker remove. 34-char-safe."""
    t = res.get("ticker", "?")
    if not res.get("ok"):
        return "\u274c Can't remove %s\n%s" % (t, res.get("reason", "unknown"))
    if not res.get("removed"):
        return "\u2139\ufe0f %s wasn't tracked" % t
    tail = ""
    if res.get("had_open"):
        tail = (
            "\nOpen position stays open\n"
            "and manages until close."
        )
    return (
        "\u2705 Removed %s\n"
        "%s\n"
        "No new entries on %s.%s"
    ) % (t, "\u2500" * 26, t, tail)


# v3.4.44: former /tickers, /add_ticker, /remove_ticker back-compat
# aliases were removed. Use /ticker list | add SYM | remove SYM instead.


# ============================================================
# TELEGRAM BOT SETUP
# ============================================================
# Commands shown in the Telegram / menu (user-facing).
#
# v3.4.44 menu cleanup: the popup is scoped to everyday-use commands.
# These typed commands still work but are intentionally hidden from
# the popup to keep it tight:
#   - /help, /test, /near_misses (advanced / rarely used)
#   - /tp_sync on the TP bot (duplicate of /rh_sync)
# These aliases were removed entirely (no handler, no popup):
#   /positions, /eod, /or_now, /tickers, /add_ticker, /remove_ticker.
MAIN_BOT_COMMANDS = [
    BotCommand("dashboard", "Full market snapshot"),
    BotCommand("status", "Open positions + P&L"),
    BotCommand("perf", "Performance stats (optional date)"),
    BotCommand("price", "Live quote for a ticker"),
    BotCommand("orb", "OR levels (add 'recover' to recollect)"),
    BotCommand("proximity", "Gap to breakout (long/short)"),
    BotCommand("mode", "Current market mode (observation)"),
    BotCommand("dayreport", "Trades + P&L (optional date)"),
    BotCommand("log", "Trade log (optional date)"),
    BotCommand("replay", "Trade timeline (optional date)"),
    BotCommand("monitoring", "Pause/resume scanner"),
    BotCommand("menu", "Quick command menu"),
    BotCommand("strategy", "Strategy summary"),
    BotCommand("algo", "Algorithm reference PDF"),
    BotCommand("version", "Release notes"),
    BotCommand("retighten", "Retighten stops to 0.75% cap"),
    BotCommand("trade_log", "Last 10 closed trades (persistent)"),
    BotCommand("ticker", "Ticker: list | add SYM | remove SYM"),
    # v3.4.38 — Robinhood live-trading kill switch.
    BotCommand("rh_status", "Robinhood kill-switch state"),
    BotCommand("rh_enable", "Enable Robinhood live trading"),
    BotCommand("rh_disable", "Disable Robinhood live trading"),
    BotCommand("reset", "Reset portfolio"),
]

# TP bot: main bot's commands plus /rh_sync (Robinhood-only).
# v3.4.38 — kill-switch commands (rh_enable/disable/status) are main-bot
# only, so strip them from the TP menu.
# v3.4.44 — /tp_sync popup entry removed (duplicate of /rh_sync); the
# typed /tp_sync handler stays as a silent alias so saved shortcuts work.
_RH_KILL_SWITCH_CMDS = {"rh_enable", "rh_disable", "rh_status"}
TP_BOT_COMMANDS = [
    bc for bc in MAIN_BOT_COMMANDS if bc.command not in _RH_KILL_SWITCH_CMDS
] + [
    BotCommand("rh_sync", "Robinhood broker sync status"),
]


async def _set_bot_commands(app: Application) -> None:
    """Register / menu commands on startup (all scopes) + send startup menu."""
    try:
        # Clear default scope first (removes any stale commands from old versions)
        await app.bot.set_my_commands(MAIN_BOT_COMMANDS, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(MAIN_BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(MAIN_BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())
        logger.info("Registered %d bot commands (all scopes)", len(MAIN_BOT_COMMANDS))
    except Exception as e:
        logger.warning("Failed to set bot commands: %s", e)
    # Send startup menu
    await _send_startup_menu(app.bot, CHAT_ID)


async def _send_startup_menu(bot, chat_id):
    """Send the interactive menu to a chat on startup/deploy."""
    reply_markup = InlineKeyboardMarkup(_build_menu_keyboard())
    startup_text = (
        "\U0001f7e2 %s v%s online\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\U0001f5c2 Menu"
    ) % (BOT_NAME, BOT_VERSION)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=startup_text,
            reply_markup=reply_markup,
        )
        logger.info("Startup menu sent to %s", chat_id)
    except Exception as e:
        logger.warning("Startup menu send failed for %s: %s", chat_id, e)


def send_startup_message():
    """Send tailored deployment card to main and TP bots.

    v3.4.16: main card stays paper-only (no TP cash/positions, no TP
    release notes). TP card shows TP portfolio + TP release notes.
    """
    SEP = "\u2500" * 34
    now_et = _now_et()
    weekday = now_et.weekday() < 5
    in_hours = (
        weekday
        and now_et.hour >= 9
        and (now_et.hour < 15 or (now_et.hour == 15 and now_et.minute < 55))
    )
    market_status = "OPEN" if in_hours else "CLOSED"

    universe = " ".join(TRADE_TICKERS)
    n_paper_pos = len(positions)
    paper_cash_fmt = f"{paper_cash:,.2f}"

    main_msg = (
        f"\U0001f680 v{BOT_VERSION} deployed\n"
        f"{CURRENT_MAIN_NOTE}\n"
        f"{SEP}\n"
        f"Universe: {universe}\n"
        f"Strategy: ORB Long + Wounded Buffalo Short | PDC anchor\n"
        f"Scan:     every {SCAN_INTERVAL}s  |  Trail: Bison +1.0% / min $1.00\n"
        f"Stops:    Long OR_High\u2212$0.90  |  Short PDC+$0.90\n"
        f"{SEP}\n"
        f"\U0001f4c4 Paper:  ${paper_cash_fmt} cash | {n_paper_pos} positions\n"
        f"Market:   {market_status}\n"
        f"{SEP}\n"
        f"/help for all commands"
    )
    send_telegram(main_msg)


# v3.6.0 — Telegram owner auth guard.
# Installed as a group=-1 TypeHandler so it fires BEFORE any default
# group=0 handler. Non-owners are silently dropped: no reply is sent,
# the update is logged server-side, and ApplicationHandlerStop prevents
# any downstream handler (command, callback, etc.) from running.
#
# Edge cases (also silently dropped):
#   * update.effective_user is None — e.g. channel posts, edited
#     messages with no sender.
#   * user id not a string member of TRADEGENIUS_OWNER_IDS.
async def _auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Drop every Telegram update that isn't from a whitelisted owner."""
    eff_user = getattr(update, "effective_user", None)
    user_id_str = str(eff_user.id) if eff_user and getattr(eff_user, "id", None) is not None else ""
    if user_id_str and user_id_str in TRADEGENIUS_OWNER_IDS:
        return  # authorized — let downstream handlers run

    eff_chat = getattr(update, "effective_chat", None)
    chat_id_str = str(eff_chat.id) if eff_chat and getattr(eff_chat, "id", None) is not None else ""
    update_id = getattr(update, "update_id", None)
    logger.warning(
        "auth_guard: dropped non-owner update (update_id=%s user_id=%r chat_id=%r)",
        update_id, user_id_str or "(none)", chat_id_str or "(none)",
    )
    raise ApplicationHandlerStop


# v4.6.0 \u2014 paper-state I/O lives in paper_state.py. Re-exported here so
# existing callsites (telegram_commands.py, smoke_test.py, internal
# uses) keep resolving the names from `trade_genius`. Must come BEFORE
# `import telegram_commands` because telegram_commands does
# `from trade_genius import save_paper_state, _do_reset_paper`.
import paper_state  # noqa: E402
from paper_state import save_paper_state, load_paper_state, _do_reset_paper  # noqa: E402,F401

# v4.5.0 — defer import to avoid circular (telegram_commands imports from trade_genius).
import telegram_commands  # noqa: E402


def run_telegram_bot():
    """Start Telegram bot (paper-only, single bot)."""
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .post_init(_set_bot_commands)
           .build())

    # v3.6.0 — Owner auth guard: every update is screened against
    # TRADEGENIUS_OWNER_IDS before any downstream handler sees it.
    # Must be installed FIRST (group=-1) so it runs before the default
    # group=0 command/callback handlers.
    app.add_handler(TypeHandler(Update, _auth_guard), group=-1)

    app.add_handler(CommandHandler("help", telegram_commands.cmd_help))
    app.add_handler(CommandHandler("dashboard", telegram_commands.cmd_dashboard))
    app.add_handler(CommandHandler("status", telegram_commands.cmd_status))
    app.add_handler(CommandHandler("log", telegram_commands.cmd_log))
    app.add_handler(CommandHandler("replay", telegram_commands.cmd_replay))
    app.add_handler(CommandHandler("dayreport", telegram_commands.cmd_dayreport))
    app.add_handler(CommandHandler("version", telegram_commands.cmd_version))
    app.add_handler(CommandHandler("near_misses", telegram_commands.cmd_near_misses))
    app.add_handler(CommandHandler("retighten", telegram_commands.cmd_retighten))
    app.add_handler(CommandHandler("trade_log", telegram_commands.cmd_trade_log))
    app.add_handler(CommandHandler("mode", telegram_commands.cmd_mode))
    app.add_handler(CommandHandler("reset", telegram_commands.cmd_reset))
    app.add_handler(CommandHandler("perf", telegram_commands.cmd_perf))
    app.add_handler(CommandHandler("price", telegram_commands.cmd_price))
    app.add_handler(CommandHandler("orb", telegram_commands.cmd_orb))
    app.add_handler(CommandHandler("proximity", telegram_commands.cmd_proximity))
    app.add_handler(CommandHandler("monitoring", telegram_commands.cmd_monitoring))
    app.add_handler(CommandHandler("algo", telegram_commands.cmd_algo))
    app.add_handler(CommandHandler("strategy", telegram_commands.cmd_strategy))
    app.add_handler(CommandHandler("test", telegram_commands.cmd_test))
    app.add_handler(CommandHandler("menu", telegram_commands.cmd_menu))
    # v3.4.32 — runtime ticker universe management
    app.add_handler(CommandHandler("ticker", telegram_commands.cmd_ticker))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(monitoring_callback, pattern="^monitoring_"))
    app.add_handler(CallbackQueryHandler(telegram_commands.reset_callback, pattern="^reset_"))
    app.add_handler(CallbackQueryHandler(positions_callback, pattern="^positions_"))
    app.add_handler(CallbackQueryHandler(proximity_callback, pattern="^proximity_refresh$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(_cb_open_menu, pattern="^open_menu$"))

    async def _error_handler(update, context):
        logger.error("Unhandled exception: %s", context.error, exc_info=context.error)
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "\u26a0\ufe0f Command failed: " + str(context.error)[:100]
                )
            except Exception:
                pass

    app.add_error_handler(_error_handler)

    app.run_polling()


# ============================================================
# STARTUP CATCH-UP
# ============================================================
def startup_catchup():
    """If restarting after 09:35 ET on a weekday, collect OR immediately."""
    now_et = _now_et()
    if now_et.weekday() >= 5:
        return
    today = now_et.strftime("%Y-%m-%d")

    # OR catch-up
    past_or_time = (now_et.hour > 9 or (now_et.hour == 9 and now_et.minute >= 35))
    if past_or_time and or_collected_date != today:
        logger.info("Catch-up: OR data stale, collecting now")
        threading.Thread(target=collect_or, daemon=True).start()


# ============================================================
# ENTRY POINT
# ============================================================
# v3.4.32 — load the editable ticker universe from tickers.json
# before anything else so load_paper_state() and retighten see the
# right TICKERS list (e.g. if a newly-added QBTS already has an
# open paper position persisted from a previous session).
_init_tickers()

# v5.6.1 D6 \u2014 one-shot [UNIVERSE] boot line. Comma-separated alpha-
# sorted tickers including the QQQ index ticker (now archived under
# /data/bars/<UTC>/QQQ.jsonl). Replay reconstructs the active universe
# from this single line instead of deduping GATE_EVAL log records.
try:
    _v561_universe_boot = list(TRADE_TICKERS)
    if V561_INDEX_TICKER not in _v561_universe_boot:
        _v561_universe_boot.append(V561_INDEX_TICKER)
    _v561_log_universe(_v561_universe_boot)
except Exception as _ue:
    logger.warning("[V561-UNIVERSE] boot emit failed: %s", _ue)

load_paper_state()

# v5.1.0 \u2014 boot the forensic volume layer (shadow mode). Safe to run
# even without Alpaca credentials; the module will log and proceed.
try:
    _start_volume_profile()
except Exception as _vpe:
    logger.error("[VOLPROFILE] _start_volume_profile crashed: %s", _vpe, exc_info=True)

# v3.4.23 — on startup, retighten every open position's stop to the
# 0.75% cap. Positions that were opened before the cap shipped (or
# that somehow have a drifted stop) get tightened here. force_exit is
# ON but fetch_prices is OFF: at process start the scanner loop
# hasn't run yet, so we'd hit Yahoo cold and probably get stale quotes
# anyway. Use entry_price as the "current" proxy — by construction
# the new capped stop can't be breached at entry_price (entry ±0.75%
# never equals entry), so force_exit is effectively silent on startup.
# The immediate-exit path fires from the first manage cycle instead,
# where real quotes are available.
try:
    _retro = retighten_all_stops(force_exit=True, fetch_prices=False)
    if _retro.get("tightened") or _retro.get("exited"):
        logger.info("[RETRO_CAP] startup: tightened %d, exited %d",
                    _retro.get("tightened", 0),
                    _retro.get("exited", 0))
except Exception as _e:
    logger.error("[RETRO_CAP] startup retighten failed: %s",
                 _e, exc_info=True)

# Live dashboard (read-only web UI). Env-gated: off unless DASHBOARD_PASSWORD is set.
# Runs in its own thread with its own asyncio loop — never touches PTB's loop.
try:
    import dashboard_server
    dashboard_server.start_in_thread()
except Exception as _dash_err:
    logger.warning("Dashboard failed to start (bot continues): %s", _dash_err)

# Startup summary
logger.info(
    "=== STARTUP SUMMARY === v%s | paper: $%.2f cash, %d pos, %d trades",
    BOT_VERSION, paper_cash, len(positions), len(trade_history),
)
# v5.6.0 \u2014 confirms the unified-AVWAP gate set is active on every boot.
logger.info(
    "[V560] Unified AVWAP gates: L-P1 (G1/G3/G4), S-P1 (G1/G3/G4)"
)

# Smoke-test guard — lets smoke_test.py import this module without booting
# the Telegram client, scheduler, OR-collector, or dashboard. The test
# script sets SSM_SMOKE_TEST=1 before import. This is the ONLY place
# where that env var is read.
if os.getenv("SSM_SMOKE_TEST", "").strip() == "1":
    logger.info("SSM_SMOKE_TEST=1 \u2014 skipping catch-up, scheduler, and Telegram loop")
else:
    # v4.0.3-beta \u2014 OR seed from Alpaca historical bars BEFORE the
    # catch-up hook, so a mid-session restart lands with correct OR
    # values rather than yesterday's persisted or_high/or_low or a
    # wrong-window fallback from collect_or()'s Yahoo/FMP path.
    # Failures are non-fatal: startup_catchup() still runs and will
    # invoke collect_or() via the existing Yahoo+FMP chain.
    try:
        _seed_opening_range_all(list(TICKERS))
    except Exception:
        logger.exception("OR_SEED startup failed \u2014 continuing without seed")

    # Startup catch-up
    startup_catchup()

    # v4.0.2-beta \u2014 DI seed from Alpaca historical bars so the DI gate
    # is armed on the first scan cycle rather than waiting ~70 min
    # of live RTH. Failures here are non-fatal: DI simply warms up
    # naturally from live ticks as before.
    try:
        _seed_di_all(list(TRADE_TICKERS))
    except Exception:
        logger.exception("DI_SEED startup failed \u2014 continuing without seed")

    # Background threads
    threading.Thread(target=scheduler_thread, daemon=True).start()
    threading.Thread(target=health_ping, daemon=True).start()

    # v4.0.0-alpha — TradeGeniusVal executor (opt-in via env).
    # Enabled by default if paper keys are present; VAL_ENABLED=0 force-disables.
    # Silently skipped if disabled or creds missing so deploys without Alpaca
    # keys still boot cleanly.
    _val_enabled = os.getenv("VAL_ENABLED", "1").strip() not in ("0", "false", "False", "")
    _val_has_keys = bool(os.getenv("VAL_ALPACA_PAPER_KEY", "").strip())
    if _val_enabled and _val_has_keys:
        try:
            val_executor = TradeGeniusVal()
            val_executor.start()
            logger.info("[Val] started in %s mode", val_executor.mode)
        except Exception:
            logger.exception("[Val] startup failed \u2014 main continues")
            val_executor = None
    else:
        logger.info(
            "[Val] skipped (VAL_ENABLED=%s, VAL_ALPACA_PAPER_KEY set=%s)",
            os.getenv("VAL_ENABLED", "1"), _val_has_keys,
        )

    # v4.0.0-beta — TradeGeniusGene executor (opt-in via env, same pattern).
    _gene_enabled = os.getenv("GENE_ENABLED", "1").strip() not in ("0", "false", "False", "")
    _gene_has_keys = bool(os.getenv("GENE_ALPACA_PAPER_KEY", "").strip())
    if _gene_enabled and _gene_has_keys:
        try:
            gene_executor = TradeGeniusGene()
            gene_executor.start()
            logger.info("[Gene] started in %s mode", gene_executor.mode)
        except Exception:
            logger.exception("[Gene] startup failed \u2014 main continues")
            gene_executor = None
    else:
        logger.info(
            "[Gene] skipped (GENE_ENABLED=%s, GENE_ALPACA_PAPER_KEY set=%s)",
            os.getenv("GENE_ENABLED", "1"), _gene_has_keys,
        )

    logger.info("%s v%s started", BOT_NAME, BOT_VERSION)
    send_startup_message()
    run_telegram_bot()
