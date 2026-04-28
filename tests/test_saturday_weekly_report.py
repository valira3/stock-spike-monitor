"""v5.8.4 \u2014 unit tests for scripts/saturday_weekly_report.py.

Fixtures under tests/fixtures/saturday_report/ exercise:
  - 1 winning allowed trade (AAPL ema_trail +$13)
  - 1 losing allowed trade (MSFT forensic_stop -$2.50)
  - 1 trade blocked by GEMINI_A (AAPL was FAIL on GEMINI_A -> blocked
    in that config), and another (MSFT) blocked by QQQ_ONLY/GEMINI_A.
  - 1 [SKIP] line per skipped candidate (TSLA, NVDA, GOOG)
  - 1 of each exit reason: ema_trail, forensic_stop, be_stop,
    velocity_fuse, eod, kill_switch.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "saturday_weekly_report.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "saturday_report"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "saturday_weekly_report",
        SCRIPT,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["saturday_weekly_report"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def swr():
    return _load_module()


@pytest.fixture(scope="module")
def parsed(swr):
    raw: list[dict] = []
    for day in ("2026-04-27", "2026-04-28"):
        raw.extend(swr.parse_jsonl_file(FIXTURES / f"day_{day}.jsonl"))
    events = swr.parse_records(raw)
    return events


def test_kv_parser_handles_gate_state_json(swr):
    line = (
        "[SKIP] ticker=NVDA reason=g1_failed ts=2026-04-27T14:30:00Z "
        'gate_state={"g1":false,"g3":true,"g4":true}'
    )
    kv = swr._kv_from(line)
    assert kv["ticker"] == "NVDA"
    assert kv["reason"] == "g1_failed"
    assert kv["gate_state"] == "__JSON__"


def test_parse_log_message_recognizes_all_event_types(swr):
    cases = [
        (
            "[V570-STRIKE] ticker=AAPL side=LONG ts=2026-04-27T13:31:00Z "
            "strike_num=1 is_first=True hod=425.10 lod=423.50 "
            "hod_break=True lod_break=False expansion_gate_pass=False",
            "V570_STRIKE",
        ),
        (
            "[V571-EXIT_PHASE] ticker=AAPL side=LONG entry_id=X "
            "from_phase=A to_phase=B trigger=foo current_stop=1.0 ts=now",
            "V571_EXIT_PHASE",
        ),
        (
            "[TRADE_CLOSED] ticker=AAPL side=LONG entry_id=X "
            "entry_ts=t entry_price=1.0 exit_ts=t2 exit_price=2.0 "
            "exit_reason=ema_trail qty=1 pnl_dollars=1.0 pnl_pct=0.01 "
            "hold_seconds=60 strike_num=1 daily_realized_pnl=1.0",
            "TRADE_CLOSED",
        ),
        (
            "[ENTRY] ticker=AAPL side=LONG entry_id=X entry_ts=t "
            "entry_price=1.0 qty=1 strike_num=1",
            "ENTRY",
        ),
        (
            "[V560-GATE] ticker=AAPL side=LONG ts=t ticker_price=1.0 "
            "ticker_avwap=1.0 index_price=1.0 index_avwap=1.0 "
            "or_high=1.0 or_low=1.0 g1=True g3=True g4=True pass=True "
            "reason=null",
            "V560_GATE",
        ),
        ("[SKIP] ticker=AAPL reason=g4_failed ts=t gate_state=null", "SKIP"),
        (
            "[V510-SHADOW][CFG=GEMINI_A][PCT=110/85] ticker=AAPL "
            "bucket=b stage=1 t_pct=80 qqq_pct=110 verdict=FAIL "
            "reason=blocked entry_decision=ENTER",
            "SHADOW_CFG",
        ),
    ]
    for line, expected_type in cases:
        ev = swr.parse_log_message(line)
        assert ev is not None, f"failed to parse: {line[:80]}"
        assert ev["type"] == expected_type


def test_parse_log_message_returns_none_for_unrelated(swr):
    assert swr.parse_log_message("info: app started ok") is None
    assert swr.parse_log_message("") is None


def test_aggregate_headline_numbers(swr, parsed):
    agg = swr.aggregate_week(parsed)
    h = agg["headline"]
    # Closes: AAPL +13.0, MSFT -2.5, AMD +10.0, META -1.0,
    #         NVDA -0.5, GOOG -10.0  -> sum 9.0
    assert h["total_closed"] == 6
    assert h["total_entries"] == 6
    assert h["actual_pnl"] == pytest.approx(9.0, abs=1e-6)
    # Wins: AAPL, AMD -> 2 wins out of 6
    assert h["win_rate"] == pytest.approx(2 / 6, abs=1e-6)


def test_per_config_table_sums(swr, parsed):
    agg = swr.aggregate_week(parsed)
    per = agg["per_config"]
    # TICKER+QQQ: every entry's shadow was PASS (or absent -> default
    # allow) -> 6 allowed, 0 blocked, allowed_pnl = total
    assert per["TICKER+QQQ"]["allowed"] == 6
    assert per["TICKER+QQQ"]["blocked"] == 0
    assert per["TICKER+QQQ"]["allowed_pnl"] == pytest.approx(9.0, abs=1e-6)
    # QQQ_ONLY: MSFT was FAIL there
    assert per["QQQ_ONLY"]["blocked"] == 1
    assert per["QQQ_ONLY"]["blocked_pnl"] == pytest.approx(-2.5, abs=1e-6)
    assert per["QQQ_ONLY"]["allowed_pnl"] == pytest.approx(11.5, abs=1e-6)
    # GEMINI_A: AAPL FAIL, MSFT FAIL, META FAIL -> 3 blocked
    assert per["GEMINI_A"]["blocked"] == 3
    assert per["GEMINI_A"]["blocked_pnl"] == pytest.approx(
        13.0 + (-2.5) + (-1.0),
        abs=1e-6,
    )
    # AMD PASS plus NVDA, GOOG (no shadow -> default allow) -> 3 allowed
    assert per["GEMINI_A"]["allowed"] == 3
    assert per["GEMINI_A"]["allowed_pnl"] == pytest.approx(
        10.0 + (-0.5) + (-10.0),
        abs=1e-6,
    )


def test_per_exit_reason_breakdown(swr, parsed):
    agg = swr.aggregate_week(parsed)
    per = agg["per_exit_reason"]
    expected = {
        "ema_trail": (1, 13.0),
        "forensic_stop": (1, -2.5),
        "be_stop": (1, 10.0),
        "velocity_fuse": (1, -1.0),
        "eod": (1, -0.5),
        "kill_switch": (1, -10.0),
    }
    for reason, (count, total) in expected.items():
        assert per[reason]["count"] == count, reason
        assert per[reason]["pnl_total"] == pytest.approx(total, abs=1e-6), reason


def test_skip_stats(swr, parsed):
    agg = swr.aggregate_week(parsed)
    s = agg["skip_stats"]
    # NVDA: g1_failed + g4_failed
    # GOOG: g1_failed
    # TSLA: g4_failed
    assert s["by_reason"] == {"g1_failed": 2, "g4_failed": 2}
    # top3 (only 2 reasons): both reported
    reasons = {item["reason"] for item in s["top3"]}
    assert reasons == {"g1_failed", "g4_failed"}
    g4 = next(it for it in s["top3"] if it["reason"] == "g4_failed")
    g4_tickers = {t["ticker"] for t in g4["top_tickers"]}
    assert g4_tickers == {"NVDA", "TSLA"}


def test_render_report_md_smoke(swr, parsed):
    agg = swr.aggregate_week(parsed)
    md = swr.render_report_md(dt.date(2026, 4, 27), agg, None)
    assert "Saturday weekly report" in md
    assert "## 1. Headline" in md
    assert "## 2. 4-config comparison" in md
    assert "## 3. Per-exit-reason" in md
    assert "## 4. Skipped-candidate" in md
    assert "## 5. Cumulative" in md
    assert "## 6. Anomalies" in md
    # Headline P&L printed
    assert "$9.00" in md


def test_cumulative_with_prior_week(swr, parsed, tmp_path):
    parent = tmp_path / "out"
    prior_dir = parent / "week_2026-04-20"
    prior_dir.mkdir(parents=True)
    (prior_dir / "report.json").write_text(
        json.dumps(
            {
                "week_start": "2026-04-20",
                "headline": {
                    "actual_pnl": 100.0,
                    "total_entries": 4,
                    "total_closed": 4,
                    "win_rate": 0.5,
                },
            }
        ),
        encoding="utf-8",
    )
    found = swr.find_prior_week_json(parent, dt.date(2026, 4, 27))
    assert found == prior_dir / "report.json"
    agg = swr.aggregate_week(parsed)
    cum = swr.build_cumulative(found, agg)
    assert cum is not None
    assert cum["cumulative_headline"]["actual_pnl"] == pytest.approx(
        109.0,
        abs=1e-6,
    )
    assert cum["cumulative_headline"]["total_closed"] == 10


def test_offline_cli_run(swr, tmp_path):
    out = tmp_path / "v57x"
    rc = swr.main(
        [
            "--week-start",
            "2026-04-27",
            "--out-dir",
            str(out),
            "--logs-dir",
            str(FIXTURES),
        ]
    )
    assert rc == 0
    week_dir = out / "week_2026-04-27"
    assert (week_dir / "report.md").exists()
    assert (week_dir / "report.json").exists()
    rj = json.loads((week_dir / "report.json").read_text(encoding="utf-8"))
    assert rj["week_start"] == "2026-04-27"
    assert rj["headline"]["total_closed"] == 6


def test_last_monday_helper(swr):
    # 2026-04-28 is a Tuesday -> last Monday is 2026-04-27
    assert swr._last_monday(dt.date(2026, 4, 28)) == dt.date(2026, 4, 27)
    # On a Monday, "most recent Monday before today" is the prior Mon.
    assert swr._last_monday(dt.date(2026, 4, 27)) == dt.date(2026, 4, 20)
    # Sunday -> the Monday 6 days back
    assert swr._last_monday(dt.date(2026, 5, 3)) == dt.date(2026, 4, 27)
