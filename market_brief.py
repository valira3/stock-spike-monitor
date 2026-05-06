# -*- coding: utf-8 -*-
"""market_brief \u2014 daily pre-open expectations summary for Telegram.

Builder for a one-shot market-expectations message:

    1. EW universe today (BMO + AMC tickers, counts, sample names)
    2. Macro snapshot (SPY / QQQ / VIX with pre-market deltas, ES futures)
    3. Pre-market movers among the EW universe
    4. Today's catalysts (earnings count for the day, FMP economic events)

Pure builder \u2014 no Telegram I/O, no scheduler hooks. The function
``build_market_brief()`` returns a single string ready to drop into
``send_telegram(...)``. Callers are:

    * /brief Telegram command (telegram_commands.cmd_brief)
    * Main keyboard "Brief" button (telegram_ui.menu)
    * Daily 08:00 ET scheduler entry in trade_genius.scheduler_thread()

All external HTTP is wrapped in try/except \u2014 partial failures degrade
to "n/a" rows, never raise. The longest single fetch is the per-EW-ticker
quote loop, which is bounded by ``MAX_PREMARKET_TICKERS`` and a small
thread pool.
"""
from __future__ import annotations

import concurrent.futures as _cf
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---- Constants ----------------------------------------------------------
EW_DATA_DIR_CANDIDATES = ("/data/earnings_watcher", "/tmp/earnings_watcher")
EVALUATED_TODAY_FILENAME = "evaluated_today.json"

# Cap how many EW tickers we fetch live quotes for in the movers section.
# 30 keeps the brief well under Telegram's 4096-char limit and the whole
# fetch under ~3s on the small thread pool below.
MAX_PREMARKET_TICKERS = 30
MAX_MOVERS_SHOWN = 8
MOVERS_THREADS = 8
HTTP_TIMEOUT = 6  # seconds

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
MACRO_SYMBOLS = ("SPY", "QQQ", "^VIX", "ES=F")
MACRO_LABELS = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "^VIX": "VIX",
    "ES=F": "ES",
}

# FMP endpoints (stable v3-style). Both keyed via FMP_API_KEY.
FMP_ECON_URL = (
    "https://financialmodelingprep.com/stable/economic-calendar"
    "?from={d}&to={d}&apikey={k}"
)
FMP_EARN_URL = (
    "https://financialmodelingprep.com/stable/earnings-calendar"
    "?from={d}&to={d}&apikey={k}"
)
FMP_QUOTE_URL = (
    "https://financialmodelingprep.com/stable/quote"
    "?symbol={t}&apikey={k}"
)


# ---- Time helpers -------------------------------------------------------
def _now_ct() -> datetime:
    """Current time in America/Chicago. Uses zoneinfo when available, else
    falls back to UTC \u2014 the brief still renders, dates just say UTC."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        return datetime.now(timezone.utc)


def _today_iso_et() -> str:
    """Today's date in US/Eastern (used for EW + earnings-calendar lookups).

    EW writes evaluated_today.json keyed by ET date because BMO/AMC windows
    are defined relative to NYSE open/close. We mirror that convention here.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


# ---- EW universe --------------------------------------------------------
def _read_evaluated_today() -> Dict[str, Any]:
    """Return the parsed evaluated_today.json, or {} if missing/malformed.

    Schema written by earnings_watcher.runner:
        {"<YYYY-MM-DD>": {"premarket": [tickers...], "afterhours": [...]}}
    """
    for base in EW_DATA_DIR_CANDIDATES:
        p = os.path.join(base, EVALUATED_TODAY_FILENAME)
        try:
            if os.path.exists(p):
                with open(p, "r") as fh:
                    return json.load(fh) or {}
        except Exception as exc:
            logger.warning("market_brief: read %s failed: %s", p, exc)
    return {}


def _ew_universe_today() -> Tuple[List[str], List[str]]:
    """Return (premarket_tickers, afterhours_tickers) for today's ET date.

    Empty lists if the file is missing, today's key is absent, or a list is
    just not present yet (e.g., AMC list before market open).
    """
    j = _read_evaluated_today()
    today = _today_iso_et()
    day = j.get(today) or {}
    pm = list(day.get("premarket") or [])
    ah = list(day.get("afterhours") or [])
    return pm, ah


# ---- HTTP wrappers ------------------------------------------------------
def _http_get_json(url: str, timeout: int = HTTP_TIMEOUT) -> Optional[Any]:
    """GET + json.loads with a uniform try/except. Returns None on failure."""
    try:
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.debug("market_brief: GET %s failed: %s", url, exc)
        return None


def _yahoo_quote(symbol: str) -> Optional[Dict[str, float]]:
    """One symbol from Yahoo v8 chart, with pre/post included.

    Returns ``{"last", "prev_close"}`` or None. ``regularMarketPrice`` from
    this endpoint reflects the latest trade including pre-market when the
    request asks for ``includePrePost=true``.
    """
    enc = urllib.parse.quote(symbol, safe="")
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/%s"
        "?interval=1m&range=1d&includePrePost=true" % enc
    )
    data = _http_get_json(url)
    if not data:
        return None
    try:
        results = (data or {}).get("chart", {}).get("result") or []
        if not results:
            return None
        meta = results[0].get("meta") or {}
        last = meta.get("regularMarketPrice")
        prev = meta.get("previousClose")
        if prev is None:
            prev = meta.get("chartPreviousClose")
        if last is None or prev is None:
            return None
        return {"last": float(last), "prev_close": float(prev)}
    except Exception:
        return None


def _yahoo_batch(symbols: List[str]) -> Dict[str, Dict[str, float]]:
    """Batch _yahoo_quote across a small thread pool. Missing keys = failed."""
    if not symbols:
        return {}
    out: Dict[str, Dict[str, float]] = {}
    workers = min(len(symbols), MOVERS_THREADS)
    try:
        with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_yahoo_quote, s): s for s in symbols}
            for fut in _cf.as_completed(futs, timeout=HTTP_TIMEOUT + 4):
                sym = futs[fut]
                try:
                    res = fut.result()
                    if res:
                        out[sym] = res
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("market_brief: yahoo batch failed: %s", exc)
    return out


def _fmp_quote(ticker: str, api_key: str) -> Optional[Dict[str, Any]]:
    """One FMP /stable/quote row. Returns the dict or None.

    FMP gives us pre-market price + change vs prior close in one shot
    (``price``, ``previousClose``, ``volume``). We prefer this over Yahoo
    for EW movers because FMP volume tracks exchange-tape (more reliable
    than Yahoo's 15-min-delayed pre-market vol)."""
    url = FMP_QUOTE_URL.format(t=urllib.parse.quote(ticker, safe=""), k=api_key)
    data = _http_get_json(url)
    if not data or not isinstance(data, list) or not data:
        return None
    return data[0]


# ---- Sub-builders -------------------------------------------------------
def _build_universe_block(pm: List[str], ah: List[str]) -> str:
    """Section 1: EW universe overview \u2014 BMO + AMC counts and sample names."""
    lines = ["EW universe today"]
    if not pm and not ah:
        lines.append("  (no universe published yet)")
        return "\n".join(lines)
    if pm:
        sample = ", ".join(pm[:6])
        more = max(0, len(pm) - 6)
        suffix = " +%d" % more if more else ""
        lines.append("  BMO: %d \u2014 %s%s" % (len(pm), sample, suffix))
    else:
        lines.append("  BMO: 0")
    if ah:
        sample = ", ".join(ah[:6])
        more = max(0, len(ah) - 6)
        suffix = " +%d" % more if more else ""
        lines.append("  AMC: %d \u2014 %s%s" % (len(ah), sample, suffix))
    else:
        lines.append("  AMC: 0")
    return "\n".join(lines)


def _fmt_pct(last: float, prev: float) -> str:
    if not prev:
        return "n/a"
    pct = (last - prev) / prev * 100.0
    sign = "+" if pct >= 0 else ""
    return "%s%.2f%%" % (sign, pct)


def _render_macro(quotes: Dict[str, Dict[str, float]]) -> str:
    """Section 2 renderer \u2014 takes pre-fetched quotes so the narrative
    can reuse the same numbers without a second network round-trip."""
    lines = ["Macro"]
    if not quotes:
        lines.append("  (macro feed unavailable)")
        return "\n".join(lines)
    for sym in MACRO_SYMBOLS:
        q = quotes.get(sym)
        label = MACRO_LABELS.get(sym, sym)
        if not q:
            lines.append("  %-4s n/a" % label)
            continue
        last = q["last"]
        prev = q["prev_close"]
        # VIX is a level, not a price \u2014 keep one decimal and skip $ sign.
        if sym == "^VIX":
            lines.append(
                "  %-4s %.2f  %s vs prior close"
                % (label, last, _fmt_pct(last, prev))
            )
        else:
            lines.append(
                "  %-4s $%.2f  %s vs prior close"
                % (label, last, _fmt_pct(last, prev))
            )
    return "\n".join(lines)


def _movers_rows(
    pm: List[str], ah: List[str], api_key: str
) -> List[Tuple[str, float, float, float]]:
    """Section 3 fetcher \u2014 returns sorted (ticker, last, prev, vol) rows.

    Capped at MAX_PREMARKET_TICKERS lookups (BMO first, then AMC fill-in).
    The list is sorted by abs %change desc so the renderer just slices it.
    """
    if not api_key:
        return []
    universe = list(dict.fromkeys((pm or []) + (ah or [])))[:MAX_PREMARKET_TICKERS]
    if not universe:
        return []

    rows: List[Tuple[str, float, float, float]] = []
    workers = min(len(universe), MOVERS_THREADS)
    try:
        with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_fmp_quote, t, api_key): t for t in universe}
            for fut in _cf.as_completed(futs, timeout=HTTP_TIMEOUT + 6):
                t = futs[fut]
                try:
                    q = fut.result()
                    if not q:
                        continue
                    last = float(q.get("price") or 0.0)
                    prev = float(q.get("previousClose") or 0.0)
                    vol = float(q.get("volume") or 0.0)
                    if last and prev:
                        rows.append((t, last, prev, vol))
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("market_brief: movers fetch failed: %s", exc)

    rows.sort(key=lambda r: abs((r[1] - r[2]) / r[2]) if r[2] else 0.0, reverse=True)
    return rows


def _render_movers(
    rows: List[Tuple[str, float, float, float]],
    api_key_present: bool = True,
) -> str:
    """Section 3 renderer \u2014 takes pre-fetched movers list."""
    lines = ["Pre-market movers (EW universe)"]
    if not api_key_present:
        lines.append("  (FMP_API_KEY not set)")
        return "\n".join(lines)
    if not rows:
        lines.append("  (no live quotes available)")
        return "\n".join(lines)
    for t, last, prev, vol in rows[:MAX_MOVERS_SHOWN]:
        pct = _fmt_pct(last, prev)
        if vol >= 1_000_000:
            vol_s = "%.1fM" % (vol / 1_000_000.0)
        elif vol >= 1_000:
            vol_s = "%.0fK" % (vol / 1_000.0)
        else:
            vol_s = "%d" % int(vol)
        lines.append("  %-5s $%-8.2f %-8s vol %s" % (t, last, pct, vol_s))
    return "\n".join(lines)


def _classify_econ_impact(row: Dict[str, Any]) -> int:
    """Return an integer impact score so we can rank econ rows.

    FMP economic-calendar rows include ``impact`` (Low/Medium/High) and
    ``country``. We weight US+High highest.
    """
    impact = (row.get("impact") or "").lower()
    country = (row.get("country") or "").upper()
    base = {"high": 3, "medium": 2, "low": 1}.get(impact, 0)
    if country in ("US", "USA"):
        base += 2
    return base


def _catalysts_rows(api_key: str) -> Dict[str, Any]:
    """Section 4 fetcher \u2014 returns ``{earnings_total, earnings_bmo,
    earnings_amc, econ_top}`` with already-ranked top econ rows. The
    raw econ rows are kept in the dict so the narrative can scan their
    impact + timestamp without re-fetching.
    """
    out: Dict[str, Any] = {
        "api_key_present": bool(api_key),
        "earnings_total": 0,
        "earnings_bmo": 0,
        "earnings_amc": 0,
        "econ_top": [],
    }
    if not api_key:
        return out
    today = _today_iso_et()

    earn_url = FMP_EARN_URL.format(d=today, k=api_key)
    earn = _http_get_json(earn_url) or []
    if isinstance(earn, list):
        out["earnings_total"] = len(earn)
        for row in earn:
            tm = (row.get("time") or "").lower()
            if "bmo" in tm or "before" in tm:
                out["earnings_bmo"] += 1
            elif "amc" in tm or "after" in tm:
                out["earnings_amc"] += 1

    econ_url = FMP_ECON_URL.format(d=today, k=api_key)
    econ = _http_get_json(econ_url) or []
    if isinstance(econ, list) and econ:
        econ_sorted = sorted(econ, key=_classify_econ_impact, reverse=True)
        out["econ_top"] = [r for r in econ_sorted if _classify_econ_impact(r) >= 2][:4]
    return out


def _render_catalysts(data: Dict[str, Any]) -> str:
    """Section 4 renderer."""
    lines = ["Catalysts today"]
    if not data.get("api_key_present"):
        lines.append("  (FMP_API_KEY not set)")
        return "\n".join(lines)

    n_total = int(data.get("earnings_total") or 0)
    n_bmo = int(data.get("earnings_bmo") or 0)
    n_amc = int(data.get("earnings_amc") or 0)
    if n_bmo or n_amc:
        lines.append(
            "  Earnings: %d total (BMO %d / AMC %d)" % (n_total, n_bmo, n_amc)
        )
    else:
        lines.append("  Earnings: %d total" % n_total)

    top = data.get("econ_top") or []
    if top:
        lines.append("  Econ:")
        for row in top:
            event = (row.get("event") or row.get("name") or "?")[:48]
            country = row.get("country") or "?"
            impact = row.get("impact") or ""
            ts = row.get("date") or row.get("time") or ""
            t_disp = ""
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                try:
                    from zoneinfo import ZoneInfo
                    dt_ct = dt.astimezone(ZoneInfo("America/Chicago"))
                    t_disp = dt_ct.strftime("%H:%M CT")
                except Exception:
                    t_disp = dt.strftime("%H:%M UTC")
            except Exception:
                t_disp = str(ts)[-5:] if ts else ""
            impact_tag = ("[%s]" % impact[0].upper()) if impact else ""
            lines.append(
                "    %s %s %s %s" % (t_disp or "--:--", country, impact_tag, event)
            )
    else:
        lines.append("  Econ: no high-impact events")
    return "\n".join(lines)


# ---- Narrative ---------------------------------------------------------
#
# Deterministic, rule-based read of the gathered numbers \u2014 NOT an LLM
# call. Stable, fast, auditable, no extra dependency. The narrative
# reasons over four signals:
#
#   1. SPY pre-market drift  -> early bias for the day; the V611 SPY
#      regime classifier itself only fires at 10:00 ET on the actual
#      9:30->10:00 30-minute return, so we deliberately do NOT claim
#      a regime band here \u2014 just an early read.
#   2. VIX level + delta     -> volatility expectation; high VIX flips
#      the algo to defensive (slower scans, tighter stops in practice).
#   3. EW universe size      -> how busy earnings_watcher will be at
#      market open (BMO names trigger right at 8:30 CT).
#   4. Top movers magnitude  -> any name that has already moved >5%
#      pre-market is at risk of being a gap-and-fade entry.
#
# Every branch returns a 1-line bullet. The narrative caps at ~6 lines.

# Volatility thresholds
_VIX_QUIET = 14.0     # below this, expect calm session
_VIX_ELEVATED = 20.0  # above this, expect chop / trap risk
_VIX_HIGH = 25.0      # above this, expect heavy whipsaws

# Pre-market SPY drift bands (mirroring V611 regime grammar so the
# narrative\u2019s vocabulary matches the dashboard once 10:00 fires).
_SPY_GAP_UP = 0.50    # pct
_SPY_GAP_DOWN = -0.50
_SPY_FLAT = 0.15

_MOVER_GAP_RISK_PCT = 5.0  # |%change| at or above this is gap-fade risk


def _classify_macro(macro: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    """Pull SPY pre-market drift, VIX level/delta into a small dict.

    Returns a dict with keys ``spy_pct``, ``qqq_pct``, ``vix_last``,
    ``vix_pct`` \u2014 any field that could not be computed is None.
    """
    out: Dict[str, Any] = {
        "spy_pct": None, "qqq_pct": None, "vix_last": None, "vix_pct": None,
    }
    spy = macro.get("SPY")
    if spy and spy["prev_close"]:
        out["spy_pct"] = (spy["last"] - spy["prev_close"]) / spy["prev_close"] * 100.0
    qqq = macro.get("QQQ")
    if qqq and qqq["prev_close"]:
        out["qqq_pct"] = (qqq["last"] - qqq["prev_close"]) / qqq["prev_close"] * 100.0
    vix = macro.get("^VIX")
    if vix:
        out["vix_last"] = vix["last"]
        if vix["prev_close"]:
            out["vix_pct"] = (vix["last"] - vix["prev_close"]) / vix["prev_close"] * 100.0
    return out


def _build_narrative(
    macro: Dict[str, Dict[str, float]],
    movers: List[Tuple[str, float, float, float]],
    pm: List[str],
    ah: List[str],
    catalysts_top: List[Dict[str, Any]],
) -> str:
    """Section 5: TradeGenius likely-actions narrative.

    All inputs are raw outputs from the other section builders so the
    narrative shares the same evidence the user is already looking at.
    Bullets are emitted in priority order: regime first, vol second,
    EW load third, mover risk fourth, catalyst risk last.
    """
    bullets: List[str] = []
    m = _classify_macro(macro)

    spy_pct = m["spy_pct"]
    vix_last = m["vix_last"]
    vix_pct = m["vix_pct"]

    # 1) Regime read (pre-market hint, not a V611 commitment).
    if spy_pct is None:
        bullets.append(
            "\u2022 SPY pre-market unread \u2014 V611 regime will classify at 10:00 ET; "
            "hold to defaults until then."
        )
    elif spy_pct >= _SPY_GAP_UP:
        bullets.append(
            "\u2022 SPY +%.2f%% pre-market \u2014 long bias; expect ORB-breakout "
            "longs to fire first if the open holds. Regime locks at 10:00 ET."
            % spy_pct
        )
    elif spy_pct <= _SPY_GAP_DOWN:
        bullets.append(
            "\u2022 SPY %.2f%% pre-market \u2014 short bias; Wounded Buffalo / "
            "regime-B short amplification likely if 9:30\u201310:00 follows through."
            % spy_pct
        )
    elif abs(spy_pct) <= _SPY_FLAT:
        bullets.append(
            "\u2022 SPY flat (%.2f%%) pre-market \u2014 chop risk; expect lower "
            "entry counts until a directional break after OR."
            % spy_pct
        )
    else:
        bullets.append(
            "\u2022 SPY %+.2f%% pre-market \u2014 mild drift; default scan posture, "
            "watch OR for confirmation." % spy_pct
        )

    # 2) Volatility regime.
    if vix_last is not None:
        if vix_last >= _VIX_HIGH:
            bullets.append(
                "\u2022 VIX %.1f (high) \u2014 expect wide ranges and stop-runs; "
                "trailing stops will trigger early. Consider /monitoring pause if "
                "signals look forced." % vix_last
            )
        elif vix_last >= _VIX_ELEVATED:
            bullets.append(
                "\u2022 VIX %.1f (elevated) \u2014 chop more likely; spreads on EW "
                "names will widen at the open." % vix_last
            )
        elif vix_last <= _VIX_QUIET:
            bullets.append(
                "\u2022 VIX %.1f (quiet) \u2014 trend persistence likely; chandelier "
                "trails will give names room to run." % vix_last
            )
        # else: normal vol \u2014 no bullet, keep narrative tight.

    # 3) EW load.
    n_pm = len(pm or [])
    n_ah = len(ah or [])
    if n_pm >= 30:
        bullets.append(
            "\u2022 EW heavy: %d BMO names today \u2014 expect a busy 8:30 CT "
            "window with multiple parallel evaluations." % n_pm
        )
    elif n_pm >= 10:
        bullets.append(
            "\u2022 EW: %d BMO names \u2014 normal cadence at the open." % n_pm
        )
    elif n_pm == 0 and n_ah == 0:
        bullets.append(
            "\u2022 EW universe empty today \u2014 only RTH (eye_of_tiger) signals "
            "will fire."
        )

    # 4) Mover gap-fade risk \u2014 only if any name has moved hard.
    big_movers = []
    for t, last, prev, _vol in (movers or []):
        if not prev:
            continue
        pct = (last - prev) / prev * 100.0
        if abs(pct) >= _MOVER_GAP_RISK_PCT:
            big_movers.append((t, pct))
    if big_movers:
        big_movers.sort(key=lambda r: abs(r[1]), reverse=True)
        names = ", ".join(
            "%s %+.1f%%" % (t, p) for t, p in big_movers[:3]
        )
        bullets.append(
            "\u2022 Gap-fade watch: %s \u2014 large pre-market moves are at risk "
            "of stop-running their initial entries." % names
        )

    # 5) Catalyst risk \u2014 only if a high-impact item lands inside RTH.
    rth_high_impact = []
    for row in catalysts_top or []:
        impact = (row.get("impact") or "").lower()
        if impact != "high":
            continue
        ts_raw = row.get("date") or row.get("time") or ""
        # Parse ISO timestamp; if it falls inside 8:30\u201315:00 CT, flag it.
        try:
            dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            try:
                from zoneinfo import ZoneInfo
                dt_ct = dt.astimezone(ZoneInfo("America/Chicago"))
            except Exception:
                dt_ct = dt
            hh, mm = dt_ct.hour, dt_ct.minute
            inside_rth = (8, 30) <= (hh, mm) <= (15, 0)
            if inside_rth:
                rth_high_impact.append(
                    (dt_ct.strftime("%H:%M CT"), (row.get("event") or "event")[:40])
                )
        except Exception:
            continue
    if rth_high_impact:
        first = rth_high_impact[0]
        bullets.append(
            "\u2022 Catalyst risk: %s %s \u2014 expect a vol spike; algo trails "
            "may trigger faster around the print." % (first[0], first[1])
        )

    if not bullets:
        bullets.append(
            "\u2022 No standout signals \u2014 default scan posture; let the algo "
            "work."
        )

    return "TradeGenius read\n" + "\n".join("  " + b for b in bullets)


# ---- Top-level builder --------------------------------------------------
def build_market_brief(
    bot_version: str = "",
    fmp_api_key: Optional[str] = None,
) -> str:
    """Assemble the full Telegram brief. Always returns a string \u2014 even
    if every sub-fetch fails, the structure is preserved with ``n/a`` rows.

    Args:
        bot_version: Display version string (e.g. ``"6.18.0"``). Caller
            normally passes ``trade_genius.BOT_VERSION``.
        fmp_api_key: FMP key. Defaults to ``os.environ['FMP_API_KEY']``;
            if unset, sections that need it render ``(FMP_API_KEY not set)``.
    """
    t0 = time.time()
    if fmp_api_key is None:
        fmp_api_key = os.environ.get("FMP_API_KEY", "")

    pm, ah = _ew_universe_today()
    ts = _now_ct().strftime("%a %b %d  %H:%M CT")

    SEP = "\u2500" * 30
    header = "Market brief \u2014 %s" % ts
    if bot_version:
        header += "  (v%s)" % bot_version

    # Fetch macro/movers/catalysts ONCE so the narrative can reuse the
    # raw numbers without re-hitting the network.
    macro_quotes = _yahoo_batch(list(MACRO_SYMBOLS))
    movers = _movers_rows(pm, ah, fmp_api_key)
    catalysts = _catalysts_rows(fmp_api_key)

    parts = [
        header,
        SEP,
        _build_universe_block(pm, ah),
        SEP,
        _render_macro(macro_quotes),
        SEP,
        _render_movers(movers, api_key_present=bool(fmp_api_key)),
        SEP,
        _render_catalysts(catalysts),
        SEP,
        _build_narrative(macro_quotes, movers, pm, ah, catalysts.get("econ_top") or []),
    ]

    elapsed = time.time() - t0
    parts.append(SEP)
    parts.append("built in %.1fs  \u2022  market opens 8:30 CT" % elapsed)

    return "\n".join(parts)
