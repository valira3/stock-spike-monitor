"""Per-rule compliance tests for the Tiger Sovereign trading spec.

This module owns one test per rule ID extracted from ``STRATEGY.md``
(Tiger Sovereign spec v2026-04-28h, adopted in the v5.13.0 series).

Each test ASSERTS the spec-mandated behavior. Rules already complied
with by v5.12.0 PASS. Rules NOT YET implemented are tagged with
``@pytest.mark.spec_gap(pr, rule_id)`` and are expected to FAIL until
the named PR closes the gap. The marker is metadata only — pytest still
runs the test and reports failure. See ``tests/spec_gap_report.py`` for
a grouped inventory.

Naming convention: ``test_<rule_id_with_underscores>``
(e.g. ``L-P4-C-S1`` → ``test_L_P4_C_S1``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STRATEGY_PATH = REPO_ROOT / "STRATEGY.md"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _strategy_text() -> str:
    return STRATEGY_PATH.read_text(encoding="utf-8")


def _spec_text(rule_id: str) -> str:
    """Return a one-line summary for a rule ID by scanning STRATEGY.md.

    The mapping below is keyed off the spec's literal wording. We do not
    parse the markdown structurally — we look for the canonical sentence
    fragment for each rule and return it. This way the test docstrings
    quote the spec, and a spec edit that changes the wording surfaces
    here as a missing match.
    """
    fragments = {
        # LONG / BISON
        "L-P1-S1": "Is QQQ (5m) Price ABOVE the 9 EMA",
        "L-P1-S2": "Is QQQ (Current) Price ABOVE the 9:30 AM Anchor VWAP",
        "L-P2-S3": "100% of 55-day rolling average for this minute",
        "L-P2-S4": "TWO (2) consecutive 1m candles ABOVE its 5m Opening Range High",
        "L-P3-S5": "5m DI+ > 25 AND 1m DI+ > 25 AND Price at New High of Day",
        "L-P3-S6": "1m DI+ cross ABOVE 30 AND Price print a FRESH NHOD",
        "L-P4-A": "trade lost $500 OR has price dropped 1% in a single minute",
        "L-P4-B": "5-minute candle close BELOW the 5m 9-EMA",
        "L-P4-C-S1": "(OR High + 0.93%)",
        "L-P4-C-S2": "every **+0.25%** increment above 0.93%",
        "L-P4-C-S3": "(OR High + 1.88%)",
        "L-P4-C-S4": "Final 50% remains in trade, continuing the **+0.25% / +0.25%** ratchet",
        # SHORT / WOUNDED BUFFALO
        "S-P1-S1": "Is QQQ (5m) Price BELOW the 9 EMA",
        "S-P1-S2": "Is QQQ (Current) Price BELOW the 9:30 AM Anchor VWAP",
        "S-P2-S3": "100% of 55-day rolling average for this minute",  # mirror
        "S-P2-S4": "TWO (2) consecutive 1m candles BELOW its 5m Opening Range Low",
        "S-P3-S5": "5m DI- > 25 AND 1m DI- > 25 AND Price at New Low of Day",
        "S-P3-S6": "1m DI- cross ABOVE 30 AND Price print a FRESH NLOD",
        "S-P4-A": "trade lost $500 OR has price spiked 1% in a single minute",
        "S-P4-B": "5-minute candle close ABOVE the 5m 9-EMA",
        "S-P4-C-S1": "(OR Low - 0.93%)",
        "S-P4-C-S2": "every **-0.25%** increment below -0.93%",
        "S-P4-C-S3": "(OR Low - 1.88%)",
        "S-P4-C-S4": "Final 50% remains in trade, continuing the **-0.25% / -0.25%** ratchet",
        # SHARED
        "SHARED-CUTOFF": "prohibited from opening any NEW positions after **15:44:59 EST**",
        "SHARED-CB": "total losses for the day reach -$1,500",
        "SHARED-EOD": "15:49:59 EST",
        "SHARED-HUNT": "continue to look for new trades until the 15:44:59 cutoff",
        "SHARED-ORDER-PROFIT": "All profit-taking exits must be executed via **LIMIT orders**",
        "SHARED-ORDER-STOP": "All defensive stops",
    }
    needle = fragments.get(rule_id)
    assert needle is not None, f"unknown rule id: {rule_id}"
    body = _strategy_text()
    assert needle in body, (
        f"rule {rule_id}: spec text drifted. Expected fragment "
        f"{needle!r} not found in STRATEGY.md. Update fragment mapping."
    )
    # Return the line containing the fragment for richer messages.
    for line in body.splitlines():
        if needle in line:
            return line.strip()
    return needle


def _trade_genius_text() -> str:
    return (REPO_ROOT / "trade_genius.py").read_text(encoding="utf-8")


def _const_value(name: str) -> str | None:
    """Extract a top-level ``NAME = ...`` value from trade_genius.py.

    Returns the right-hand side as a string, or None if not found.
    """
    src = _trade_genius_text()
    m = re.search(rf"^{re.escape(name)}\s*[:\w\[\],\s]*=\s*(.+)$", src, re.MULTILINE)
    if not m:
        return None
    return m.group(1).strip()


# ---------------------------------------------------------------------------
# LONG (Bison) — Phase 1: Global Market Shield
# ---------------------------------------------------------------------------


def test_L_P1_S1():
    """L-P1-S1: QQQ (5m) Price ABOVE 9 EMA gates LONG permit.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P1-S1")
    assert spec  # ensure spec text is present

    # The QQQ regime gate is implemented in qqq_regime.py / trade_genius.py.
    # We assert the structural marker that the codebase uses a 9-EMA on
    # QQQ 5m bars for the long permit.
    qqq = (REPO_ROOT / "qqq_regime.py").read_text(encoding="utf-8")
    assert "ema9" in qqq.lower() or "9_ema" in qqq.lower() or "9-ema" in qqq.lower(), (
        "L-P1-S1: qqq_regime.py must reference the 9-EMA used for the "
        "long permit. Spec: " + spec
    )


def test_L_P1_S2():
    """L-P1-S2: QQQ (Current) Price ABOVE the 9:30 AM Anchor VWAP.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P1-S2")
    src = _trade_genius_text()
    # v5.10.0 introduced ``_opening_avwap`` per ARCHITECTURE.
    assert "_opening_avwap" in src or "anchor_vwap" in src.lower(), (
        "L-P1-S2: trade_genius.py must compute a 9:30 anchor VWAP "
        "for the long permit. Spec: " + spec
    )


# ---------------------------------------------------------------------------
# LONG — Phase 2: Ticker-Specific Permits
# ---------------------------------------------------------------------------


def test_L_P2_S3():
    """L-P2-S3: Volume >= 100% of 55-day rolling average for this minute.

    Rule is enforced when VOLUME_GATE_ENABLED=True. Default runtime is False
    (gate DISABLED, auto-pass via DISABLED_BY_FLAG path — see v5.13.1 and
    STRATEGY.md "Operational Overrides"). The 55-day baseline plumbing is
    still required to exist so the rule can be re-enabled at runtime
    without code changes; this test pins that source-level invariant.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P2-S3")
    src = _trade_genius_text()
    # Require a concentrated reference: a single line / short window
    # mentioning "55" alongside "rolling" / "minute" volume baseline.
    pattern = re.compile(
        r"55[-_ ]?day.{0,80}(rolling|minute|baseline)|"
        r"(rolling|minute|baseline).{0,80}55[-_ ]?day",
        re.IGNORECASE,
    )
    assert pattern.search(src), (
        "L-P2-S3: spec requires volume gate at >=100% of 55-day rolling "
        "per-minute baseline. No such check found in trade_genius.py. "
        "Spec: " + spec
    )


def test_L_P2_S4():
    """L-P2-S4: TWO consecutive 1m candles closed ABOVE 5m OR High.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P2-S4")
    src = _trade_genius_text()
    assert (
        "two_consecutive" in src.lower()
        or "2_consecutive" in src.lower()
        or "consecutive_1m" in src.lower()
    ), (
        "L-P2-S4: spec requires two consecutive 1m closes above OR "
        "High; literal gate not found. Spec: " + spec
    )


# ---------------------------------------------------------------------------
# LONG — Phase 3: The Strike
# ---------------------------------------------------------------------------


def test_L_P3_S5():
    """L-P3-S5: 5m DI+ > 25 AND 1m DI+ > 25 AND price at NHOD → BUY 50%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P3-S5")
    di_thr = _const_value("TIGER_V2_DI_THRESHOLD")
    assert di_thr is not None, "TIGER_V2_DI_THRESHOLD constant not found"
    # Default value is os.getenv(..., "25") — the literal "25" must appear.
    assert '"25"' in di_thr or "25" in di_thr, (
        f"L-P3-S5: DI threshold must be 25 (got {di_thr!r}). Spec: {spec}"
    )


def test_L_P3_S6():
    """L-P3-S6: 1m DI+ cross above 30 AND fresh NHOD → BUY remaining 50%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P3-S6")
    src = _trade_genius_text()
    # Spec requires a literal threshold of 30 for the Entry-2 1m DI+ gate.
    # Today the codebase reuses TIGER_V2_DI_THRESHOLD (25) for entry 2.
    assert re.search(r"DI[_\s]*PLUS[_\s]*ENTRY2[_\s]*THRESHOLD\s*=\s*30", src) or re.search(
        r"di_plus[^\n]{0,60}>\s*30", src
    ), (
        "L-P3-S6: spec requires a stricter DI+ > 30 gate for Entry 2; "
        "no DI=30 threshold found. Spec: " + spec
    )


# ---------------------------------------------------------------------------
# LONG — Phase 4: Sentinel Loop
# ---------------------------------------------------------------------------


def test_L_P4_A():
    """L-P4-A: Trade lost $500 OR price dropped 1% in single minute → exit 100%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P4-A")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), (
        "L-P4-A: engine/sentinel.py not present; PR 2 introduces "
        "Alarm A. Spec: " + spec
    )
    body = sentinel_path.read_text(encoding="utf-8")
    assert "check_alarm_a" in body, "L-P4-A: check_alarm_a() not defined."


def test_L_P4_B():
    """L-P4-B: 5-minute candle CLOSE below 5m 9-EMA → exit 100%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P4-B")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), (
        "L-P4-B: engine/sentinel.py not present; PR 2 introduces "
        "Alarm B. Spec: " + spec
    )
    body = sentinel_path.read_text(encoding="utf-8")
    assert "check_alarm_b" in body, "L-P4-B: check_alarm_b() not defined."


def test_L_P4_C_S1():
    """L-P4-C-S1: At OR_High +0.93% sell 25% LIMIT, move stop to OR_High +0.40%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P4-C-S1")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), (
        "L-P4-C-S1: engine/sentinel.py not present; PR 3 introduces "
        "the Titan Grip ratchet. Spec: " + spec
    )
    body = sentinel_path.read_text(encoding="utf-8")
    assert "0.93" in body and "0.40" in body, (
        "L-P4-C-S1: Stage 1 thresholds 0.93% / 0.40% must be present "
        "in engine/sentinel.py. Spec: " + spec
    )


def test_L_P4_C_S2():
    """L-P4-C-S2: Every +0.25% step above 0.93% moves stop +0.25%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P4-C-S2")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), (
        "L-P4-C-S2: engine/sentinel.py absent; PR 3. Spec: " + spec
    )
    body = sentinel_path.read_text(encoding="utf-8")
    assert "0.25" in body, (
        "L-P4-C-S2: micro-ratchet step 0.25% missing. Spec: " + spec
    )


def test_L_P4_C_S3():
    """L-P4-C-S3: At OR_High +1.88% sell second 25% LIMIT.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P4-C-S3")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), (
        "L-P4-C-S3: engine/sentinel.py absent; PR 3. Spec: " + spec
    )
    body = sentinel_path.read_text(encoding="utf-8")
    assert "1.88" in body, (
        "L-P4-C-S3: second-harvest threshold 1.88% missing. Spec: " + spec
    )


def test_L_P4_C_S4():
    """L-P4-C-S4: Final 50% runner with continued +0.25% ratchet.

    Spec line: ``{spec}``
    """
    spec = _spec_text("L-P4-C-S4")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), (
        "L-P4-C-S4: engine/sentinel.py absent; PR 3. Spec: " + spec
    )
    body = sentinel_path.read_text(encoding="utf-8")
    assert "runner" in body.lower(), (
        "L-P4-C-S4: Stage 4 runner logic must be present. Spec: " + spec
    )


# ---------------------------------------------------------------------------
# SHORT (Wounded Buffalo) — mirrors of long rules
# ---------------------------------------------------------------------------


def test_S_P1_S1():
    """S-P1-S1: QQQ (5m) Price BELOW 9 EMA gates SHORT permit.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P1-S1")
    assert spec
    qqq = (REPO_ROOT / "qqq_regime.py").read_text(encoding="utf-8")
    assert "ema9" in qqq.lower() or "9_ema" in qqq.lower() or "9-ema" in qqq.lower(), (
        "S-P1-S1: qqq_regime.py must reference 9-EMA for short permit. "
        "Spec: " + spec
    )


def test_S_P1_S2():
    """S-P1-S2: QQQ (Current) Price BELOW the 9:30 anchor VWAP.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P1-S2")
    src = _trade_genius_text()
    assert "_opening_avwap" in src or "anchor_vwap" in src.lower(), (
        "S-P1-S2: short permit must use anchor VWAP. Spec: " + spec
    )


def test_S_P2_S3():
    """S-P2-S3: Volume >= 100% of 55-day rolling avg (mirror of L-P2-S3).

    Rule is enforced when VOLUME_GATE_ENABLED=True. Default runtime is False
    (gate DISABLED, auto-pass via DISABLED_BY_FLAG path — see v5.13.1 and
    STRATEGY.md "Operational Overrides"). The 55-day baseline plumbing is
    still required to exist so the rule can be re-enabled at runtime
    without code changes.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P2-S3")
    src = _trade_genius_text()
    pattern = re.compile(
        r"55[-_ ]?day.{0,80}(rolling|minute|baseline)|"
        r"(rolling|minute|baseline).{0,80}55[-_ ]?day",
        re.IGNORECASE,
    )
    assert pattern.search(src), (
        "S-P2-S3: 55-day rolling per-minute baseline missing. Spec: " + spec
    )


def test_S_P2_S4():
    """S-P2-S4: TWO consecutive 1m candles closed BELOW 5m OR Low.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P2-S4")
    src = _trade_genius_text()
    assert (
        "two_consecutive" in src.lower()
        or "2_consecutive" in src.lower()
        or "consecutive_1m" in src.lower()
    ), (
        "S-P2-S4: spec requires two consecutive 1m closes below OR Low; "
        "literal gate not found. Spec: " + spec
    )


def test_S_P3_S5():
    """S-P3-S5: 5m DI- > 25 AND 1m DI- > 25 AND price at NLOD → SELL SHORT 50%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P3-S5")
    di_thr = _const_value("TIGER_V2_DI_THRESHOLD")
    assert di_thr is not None
    assert '"25"' in di_thr or "25" in di_thr, (
        f"S-P3-S5: DI threshold must be 25 (got {di_thr!r}). Spec: {spec}"
    )


def test_S_P3_S6():
    """S-P3-S6: 1m DI- cross above 30 AND fresh NLOD → SELL SHORT remaining 50%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P3-S6")
    src = _trade_genius_text()
    assert re.search(r"DI[_\s]*MINUS[_\s]*ENTRY2[_\s]*THRESHOLD\s*=\s*30", src) or re.search(
        r"di_minus[^\n]{0,60}>\s*30", src
    ), (
        "S-P3-S6: spec requires a stricter DI- > 30 gate for Entry 2. "
        "Spec: " + spec
    )


def test_S_P4_A():
    """S-P4-A: Trade lost $500 OR price spiked 1% in single minute → exit 100%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P4-A")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), "S-P4-A: engine/sentinel.py absent. " + spec
    body = sentinel_path.read_text(encoding="utf-8")
    assert "check_alarm_a" in body, "S-P4-A: check_alarm_a() missing."


def test_S_P4_B():
    """S-P4-B: 5-minute candle CLOSE above 5m 9-EMA → exit 100%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P4-B")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), "S-P4-B: engine/sentinel.py absent. " + spec
    body = sentinel_path.read_text(encoding="utf-8")
    assert "check_alarm_b" in body, "S-P4-B: check_alarm_b() missing."


def test_S_P4_C_S1():
    """S-P4-C-S1: Price <= OR_Low - 0.93% buy-cover 25% LIMIT, stop to OR_Low - 0.40%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P4-C-S1")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), "S-P4-C-S1: engine/sentinel.py absent. " + spec
    body = sentinel_path.read_text(encoding="utf-8")
    assert "0.93" in body and "0.40" in body, (
        "S-P4-C-S1: short Stage 1 thresholds missing. Spec: " + spec
    )


def test_S_P4_C_S2():
    """S-P4-C-S2: Every -0.25% step below -0.93% moves stop -0.25%.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P4-C-S2")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), "S-P4-C-S2: engine/sentinel.py absent. " + spec
    body = sentinel_path.read_text(encoding="utf-8")
    assert "0.25" in body, (
        "S-P4-C-S2: short micro-ratchet step missing. Spec: " + spec
    )


def test_S_P4_C_S3():
    """S-P4-C-S3: At OR_Low - 1.88% buy-cover second 25% LIMIT.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P4-C-S3")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), "S-P4-C-S3: engine/sentinel.py absent. " + spec
    body = sentinel_path.read_text(encoding="utf-8")
    assert "1.88" in body, (
        "S-P4-C-S3: short second-harvest threshold missing. Spec: " + spec
    )


def test_S_P4_C_S4():
    """S-P4-C-S4: Final 50% runner with continued -0.25% ratchet.

    Spec line: ``{spec}``
    """
    spec = _spec_text("S-P4-C-S4")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    assert sentinel_path.exists(), "S-P4-C-S4: engine/sentinel.py absent. " + spec
    body = sentinel_path.read_text(encoding="utf-8")
    assert "runner" in body.lower(), (
        "S-P4-C-S4: short Stage 4 runner logic missing. Spec: " + spec
    )


# ---------------------------------------------------------------------------
# SHARED rules
# ---------------------------------------------------------------------------


def test_SHARED_CUTOFF():
    """SHARED-CUTOFF: New-position cutoff at 15:44:59 ET.

    Spec line: ``{spec}``
    """
    spec = _spec_text("SHARED-CUTOFF")
    src = _trade_genius_text()
    # PR 5 must introduce the 15:44 cutoff. Today the cutoff is 15:30
    # ("Power Hour"); we assert the spec-mandated value is present.
    assert "15:44" in src, (
        "SHARED-CUTOFF: 15:44:59 ET cutoff not found in trade_genius.py. "
        "Today the cutoff is 15:30 (Power Hour). Spec: " + spec
    )


def test_SHARED_CB():
    """SHARED-CB: Daily circuit breaker at -$1,500.

    Spec line: ``{spec}``
    """
    spec = _spec_text("SHARED-CB")
    daily = _const_value("DAILY_LOSS_LIMIT_DOLLARS")
    assert daily is not None, "DAILY_LOSS_LIMIT_DOLLARS constant missing"
    assert "-1500" in daily.replace(" ", "") or "-1500.0" in daily.replace(" ", ""), (
        f"SHARED-CB: daily loss limit must be -$1,500 (got {daily!r}). "
        f"Spec: {spec}"
    )


def test_SHARED_EOD():
    """SHARED-EOD: EOD flush at 15:49:59 ET.

    Spec line: ``{spec}``
    """
    spec = _spec_text("SHARED-EOD")
    src = _trade_genius_text()
    # Today the EOD flush is at 15:59:50 ET. PR 5 moves it to 15:49:59.
    assert "15:49" in src, (
        "SHARED-EOD: 15:49:59 ET EOD flush not found in trade_genius.py. "
        "Today EOD is at 15:59. Spec: " + spec
    )


def test_SHARED_HUNT():
    """SHARED-HUNT: Unlimited hunting until 15:44:59 cutoff.

    Spec line: ``{spec}``
    """
    spec = _spec_text("SHARED-HUNT")
    # The "unlimited hunting until cutoff" assertion is meaningful only
    # once SHARED-CUTOFF is moved to 15:44. We treat it as a PR-5 gap
    # because it depends on the cutoff change.
    src = _trade_genius_text()
    assert "15:44" in src, (
        "SHARED-HUNT: precondition is the 15:44 cutoff (PR 5). "
        "Spec: " + spec
    )


def test_SHARED_ORDER_PROFIT():
    """SHARED-ORDER-PROFIT: All profit-taking exits via LIMIT orders.

    Spec line: ``{spec}``
    """
    spec = _spec_text("SHARED-ORDER-PROFIT")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    order_types_path = REPO_ROOT / "broker" / "order_types.py"
    assert sentinel_path.exists(), (
        "SHARED-ORDER-PROFIT: engine/sentinel.py absent. Spec: " + spec
    )
    assert order_types_path.exists(), (
        "SHARED-ORDER-PROFIT: broker/order_types.py absent. Spec: " + spec
    )
    body = sentinel_path.read_text(encoding="utf-8")
    ot_body = order_types_path.read_text(encoding="utf-8")
    assert "LIMIT" in body, (
        "SHARED-ORDER-PROFIT: harvest exits must reference LIMIT. "
        "Spec: " + spec
    )
    # PR 6: order_types.py owns the reason→type mapping. Harvest
    # reasons must map to LIMIT.
    from broker.order_types import (
        order_type_for_reason,
        REASON_STAGE1_HARVEST,
        REASON_STAGE3_HARVEST,
        ORDER_TYPE_LIMIT,
    )
    assert order_type_for_reason(REASON_STAGE1_HARVEST) == ORDER_TYPE_LIMIT
    assert order_type_for_reason(REASON_STAGE3_HARVEST) == ORDER_TYPE_LIMIT


def test_SHARED_ORDER_STOP():
    """SHARED-ORDER-STOP: All defensive stops via STOP MARKET orders.

    Spec line: ``{spec}``
    """
    spec = _spec_text("SHARED-ORDER-STOP")
    sentinel_path = REPO_ROOT / "engine" / "sentinel.py"
    order_types_path = REPO_ROOT / "broker" / "order_types.py"
    assert sentinel_path.exists(), (
        "SHARED-ORDER-STOP: engine/sentinel.py absent. Spec: " + spec
    )
    assert order_types_path.exists(), (
        "SHARED-ORDER-STOP: broker/order_types.py absent. Spec: " + spec
    )
    body = sentinel_path.read_text(encoding="utf-8")
    assert "STOP" in body and "MARKET" in body, (
        "SHARED-ORDER-STOP: defensive stops must reference STOP MARKET. "
        "Spec: " + spec
    )
    # PR 6: order_types.py owns the reason→type mapping. Stop
    # reasons must map to STOP_MARKET.
    from broker.order_types import (
        order_type_for_reason,
        REASON_ALARM_A,
        REASON_ALARM_B,
        REASON_RATCHET,
        REASON_RUNNER_EXIT,
        ORDER_TYPE_STOP_MARKET,
    )
    assert order_type_for_reason(REASON_ALARM_A) == ORDER_TYPE_STOP_MARKET
    assert order_type_for_reason(REASON_ALARM_B) == ORDER_TYPE_STOP_MARKET
    assert order_type_for_reason(REASON_RATCHET) == ORDER_TYPE_STOP_MARKET
    assert order_type_for_reason(REASON_RUNNER_EXIT) == ORDER_TYPE_STOP_MARKET
