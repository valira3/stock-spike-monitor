"""v9.0.0 -- prior-day SPY regime-skip gate tests.

Covers:
  * day_gates.evaluate_day applies the SPY regime gate after VIX
  * fail-open vs fail-closed on missing SPY data
  * threshold sign semantics (negative bps = "drop deeper than")
  * orb_spy_loader walks bar archive backward to find two prior closes
  * orb_spy_loader falls back to CSV when bar archive is missing
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from orb import day_gates


# ----- 1. evaluate_day with SPY gate ----------------------------------


def _gate_cfg(*, spy_thr=-40.0, fail_closed=False, vix_thr=0.0):
    return day_gates.DayGateConfig(
        skip_vix_above=vix_thr,
        fail_closed_on_missing_vix=False,
        skip_earnings_window=False,
        skip_gap_above_pct=0.0,
        ticker_side_blocklist={},
        skip_prior_spy_ret_lt_bps=spy_thr,
        fail_closed_on_missing_spy=fail_closed,
    )


class TestEvaluateDaySpyGate:
    def test_disabled_when_threshold_zero(self):
        cfg = _gate_cfg(spy_thr=0.0)
        result = day_gates.evaluate_day(
            cfg,
            date_iso="2026-01-15",
            vix_close_d1=None,
            tickers=["AAPL"],
            ticker_open_today={},
            ticker_prev_close={},
            spy_prior_ret_bps=-200.0,  # very negative but gate is off
        )
        assert result.block_day is False

    def test_blocks_when_return_below_threshold(self):
        cfg = _gate_cfg(spy_thr=-40.0)
        result = day_gates.evaluate_day(
            cfg,
            date_iso="2026-01-15",
            vix_close_d1=None,
            tickers=["AAPL"],
            ticker_open_today={},
            ticker_prev_close={},
            spy_prior_ret_bps=-75.0,
        )
        assert result.block_day is True
        assert "spy_regime_low" in result.block_reason
        assert result.spy_d1_ret_bps == -75.0
        assert result.spy_threshold_bps == -40.0

    def test_passes_when_return_above_threshold(self):
        cfg = _gate_cfg(spy_thr=-40.0)
        result = day_gates.evaluate_day(
            cfg,
            date_iso="2026-01-15",
            vix_close_d1=None,
            tickers=["AAPL"],
            ticker_open_today={},
            ticker_prev_close={},
            spy_prior_ret_bps=20.0,
        )
        assert result.block_day is False

    def test_passes_when_at_threshold(self):
        # Exact threshold = NOT below -> pass.
        cfg = _gate_cfg(spy_thr=-40.0)
        result = day_gates.evaluate_day(
            cfg,
            date_iso="2026-01-15",
            vix_close_d1=None,
            tickers=["AAPL"],
            ticker_open_today={},
            ticker_prev_close={},
            spy_prior_ret_bps=-40.0,
        )
        assert result.block_day is False

    def test_fail_open_on_missing_data(self):
        cfg = _gate_cfg(spy_thr=-40.0, fail_closed=False)
        result = day_gates.evaluate_day(
            cfg,
            date_iso="2026-01-15",
            vix_close_d1=None,
            tickers=["AAPL"],
            ticker_open_today={},
            ticker_prev_close={},
            spy_prior_ret_bps=None,
        )
        assert result.block_day is False
        assert result.spy_d1_ret_bps is None

    def test_fail_closed_on_missing_data(self):
        cfg = _gate_cfg(spy_thr=-40.0, fail_closed=True)
        result = day_gates.evaluate_day(
            cfg,
            date_iso="2026-01-15",
            vix_close_d1=None,
            tickers=["AAPL"],
            ticker_open_today={},
            ticker_prev_close={},
            spy_prior_ret_bps=None,
        )
        assert result.block_day is True
        assert result.block_reason == "missing_spy"

    def test_vix_gate_runs_before_spy(self):
        # VIX too high -> blocks early; SPY gate never evaluated.
        cfg = _gate_cfg(spy_thr=-40.0, vix_thr=20.0)
        result = day_gates.evaluate_day(
            cfg,
            date_iso="2026-01-15",
            vix_close_d1=25.0,  # > 20 threshold
            tickers=["AAPL"],
            ticker_open_today={},
            ticker_prev_close={},
            spy_prior_ret_bps=None,  # would fail-open if it ran
        )
        assert result.block_day is True
        assert "vix_high" in result.block_reason


# ----- 2. orb_spy_loader: bar archive walk-back -----------------------


from tools import orb_spy_loader


def _write_spy_jsonl(path: Path, *, close: float, bucket: str = "1559"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": "2026-01-15T20:59:00Z",
                    "et_bucket": bucket,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "total_volume": 1000,
                }
            )
            + "\n"
        )


class TestSpyLoaderBarArchive:
    def test_returns_none_when_no_data(self, tmp_path):
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path),
            csv_path=str(tmp_path / "missing.csv"),
        )
        assert ret is None

    def test_two_prior_days_compute_return(self, tmp_path):
        # D-1 close = 460.0, D-2 close = 458.0 -> 200bps / 458 * 10000 = 43.7
        _write_spy_jsonl(tmp_path / "2026-05-12" / "SPY.jsonl", close=460.0)
        _write_spy_jsonl(tmp_path / "2026-05-09" / "SPY.jsonl", close=458.0)
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path),
            csv_path=str(tmp_path / "missing.csv"),
        )
        assert ret is not None
        expected = (460.0 - 458.0) / 458.0 * 10000.0
        assert abs(ret - expected) < 0.1

    def test_negative_return(self, tmp_path):
        _write_spy_jsonl(tmp_path / "2026-05-12" / "SPY.jsonl", close=455.0)
        _write_spy_jsonl(tmp_path / "2026-05-09" / "SPY.jsonl", close=460.0)
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path),
            csv_path=str(tmp_path / "missing.csv"),
        )
        assert ret is not None
        assert ret < 0

    def test_skips_non_rth_buckets(self, tmp_path):
        # bucket "1700" is post-RTH; should be ignored. Last RTH bar
        # is the only one written here at 1559.
        d1 = tmp_path / "2026-05-12" / "SPY.jsonl"
        d1.parent.mkdir(parents=True, exist_ok=True)
        with d1.open("w") as fh:
            fh.write(
                json.dumps(
                    {
                        "et_bucket": "1559",
                        "close": 460.0,
                        "high": 460.0,
                        "low": 460.0,
                        "open": 460.0,
                        "total_volume": 1,
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "et_bucket": "1700",
                        "close": 999.0,
                        "high": 999.0,
                        "low": 999.0,
                        "open": 999.0,
                        "total_volume": 1,
                    }
                )
                + "\n"
            )
        _write_spy_jsonl(tmp_path / "2026-05-09" / "SPY.jsonl", close=458.0)
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path),
            csv_path=str(tmp_path / "missing.csv"),
        )
        assert ret is not None
        # Last RTH close should be 460 (not 999).
        expected = (460.0 - 458.0) / 458.0 * 10000.0
        assert abs(ret - expected) < 0.1


# ----- 3. orb_spy_loader: CSV fallback --------------------------------


class TestSpyLoaderCsvFallback:
    def test_csv_when_bar_archive_missing(self, tmp_path):
        csv = tmp_path / "spy.csv"
        csv.write_text("DATE,CLOSE\n2026-05-12,460.0\n2026-05-09,458.0\n")
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path / "nonexistent_bars"),
            csv_path=str(csv),
        )
        assert ret is not None
        expected = (460.0 - 458.0) / 458.0 * 10000.0
        assert abs(ret - expected) < 0.1

    def test_returns_none_with_only_one_prior_close(self, tmp_path):
        csv = tmp_path / "spy.csv"
        csv.write_text("DATE,CLOSE\n2026-05-12,460.0\n")
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path / "nonexistent_bars"),
            csv_path=str(csv),
        )
        assert ret is None


# ----- 4. v9.1.3 orb_spy_loader: Alpaca REST third-tier fallback ------


class _FakeBar:
    def __init__(self, ts_iso: str, close: float):
        self.timestamp = datetime.strptime(ts_iso, "%Y-%m-%d")
        self.close = close


class _FakeResp:
    def __init__(self, bars):
        self.data = {"SPY": bars}


class TestSpyLoaderAlpacaFallback:
    def setup_method(self):
        # Per-test cache reset so prior tests don't leak results.
        orb_spy_loader._alpaca_cache.clear()

    def _install_fake_alpaca(self, monkeypatch, bars, *, raise_exc=None):
        """Patch the Alpaca SDK imports inside
        _prior_two_closes_from_alpaca so the test never touches the
        network. The function does a `from alpaca.data.historical
        import StockHistoricalDataClient` inside its body, so we
        replace the call site by stubbing the module attribute the
        function pulls in.
        """
        import sys
        import types

        hist = types.ModuleType("alpaca.data.historical")
        reqs = types.ModuleType("alpaca.data.requests")
        tf = types.ModuleType("alpaca.data.timeframe")

        class _Client:
            def __init__(self, k, s):
                pass

            def get_stock_bars(self, req):
                if raise_exc is not None:
                    raise raise_exc
                return _FakeResp(bars)

        class _Req:
            def __init__(self, **kw):
                self.kw = kw

        class _TF:
            Day = "1Day"

        hist.StockHistoricalDataClient = _Client
        reqs.StockBarsRequest = _Req
        tf.TimeFrame = _TF
        monkeypatch.setitem(sys.modules, "alpaca.data.historical", hist)
        monkeypatch.setitem(sys.modules, "alpaca.data.requests", reqs)
        monkeypatch.setitem(sys.modules, "alpaca.data.timeframe", tf)

    def test_alpaca_used_when_bar_archive_and_csv_missing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "pk_fake")
        monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "sk_fake")
        bars = [
            _FakeBar("2026-05-09", 458.0),
            _FakeBar("2026-05-12", 460.0),
        ]
        self._install_fake_alpaca(monkeypatch, bars)
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path / "nonexistent_bars"),
            csv_path=str(tmp_path / "nonexistent.csv"),
        )
        assert ret is not None
        expected = (460.0 - 458.0) / 458.0 * 10000.0
        assert abs(ret - expected) < 0.1

    def test_alpaca_not_called_when_bar_archive_succeeds(
        self, tmp_path, monkeypatch
    ):
        # If the bar archive can answer, the function must NEVER touch
        # Alpaca. Install a fake that would raise to prove no call.
        _write_spy_jsonl(tmp_path / "2026-05-12" / "SPY.jsonl", close=460.0)
        _write_spy_jsonl(tmp_path / "2026-05-09" / "SPY.jsonl", close=458.0)
        monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "pk_fake")
        monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "sk_fake")
        self._install_fake_alpaca(
            monkeypatch, [], raise_exc=RuntimeError("should not be called"),
        )
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path),
            csv_path=str(tmp_path / "missing.csv"),
        )
        assert ret is not None  # came from bar archive, not Alpaca

    def test_alpaca_returns_none_when_credentials_missing(
        self, tmp_path, monkeypatch
    ):
        # Make sure neither pool var is set.
        for v in (
            "VAL_ALPACA_PAPER_KEY",
            "VAL_ALPACA_PAPER_SECRET",
            "GENE_ALPACA_PAPER_KEY",
            "GENE_ALPACA_PAPER_SECRET",
        ):
            monkeypatch.delenv(v, raising=False)
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path / "nonexistent_bars"),
            csv_path=str(tmp_path / "nonexistent.csv"),
        )
        assert ret is None  # fail-open contract preserved

    def test_alpaca_returns_none_on_rest_failure(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "pk_fake")
        monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "sk_fake")
        self._install_fake_alpaca(
            monkeypatch, [], raise_exc=RuntimeError("503 service unavailable"),
        )
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path / "nonexistent_bars"),
            csv_path=str(tmp_path / "nonexistent.csv"),
        )
        assert ret is None  # fail-open contract preserved

    def test_alpaca_drops_same_day_bar_lookahead_guard(
        self, tmp_path, monkeypatch
    ):
        # The function asks Alpaca for bars up to D-1, but if the API
        # mis-returns a D bar we must drop it (rule #7b look-ahead).
        monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "pk_fake")
        monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "sk_fake")
        bars = [
            _FakeBar("2026-05-09", 458.0),
            _FakeBar("2026-05-12", 460.0),
            _FakeBar("2026-05-13", 999.0),  # same as decision_date
        ]
        self._install_fake_alpaca(monkeypatch, bars)
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path / "nonexistent_bars"),
            csv_path=str(tmp_path / "nonexistent.csv"),
        )
        assert ret is not None
        expected = (460.0 - 458.0) / 458.0 * 10000.0
        assert abs(ret - expected) < 0.1  # 999 must be ignored

    def test_alpaca_returns_none_with_only_one_close(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "pk_fake")
        monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "sk_fake")
        bars = [_FakeBar("2026-05-12", 460.0)]
        self._install_fake_alpaca(monkeypatch, bars)
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path / "nonexistent_bars"),
            csv_path=str(tmp_path / "nonexistent.csv"),
        )
        assert ret is None

    def test_alpaca_cache_avoids_second_call(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VAL_ALPACA_PAPER_KEY", "pk_fake")
        monkeypatch.setenv("VAL_ALPACA_PAPER_SECRET", "sk_fake")
        bars = [
            _FakeBar("2026-05-09", 458.0),
            _FakeBar("2026-05-12", 460.0),
        ]
        call_count = {"n": 0}

        import sys
        import types

        hist = types.ModuleType("alpaca.data.historical")
        reqs = types.ModuleType("alpaca.data.requests")
        tf = types.ModuleType("alpaca.data.timeframe")

        class _Client:
            def __init__(self, k, s):
                pass

            def get_stock_bars(self, req):
                call_count["n"] += 1
                return _FakeResp(bars)

        class _Req:
            def __init__(self, **kw):
                pass

        class _TF:
            Day = "1Day"

        hist.StockHistoricalDataClient = _Client
        reqs.StockBarsRequest = _Req
        tf.TimeFrame = _TF
        monkeypatch.setitem(sys.modules, "alpaca.data.historical", hist)
        monkeypatch.setitem(sys.modules, "alpaca.data.requests", reqs)
        monkeypatch.setitem(sys.modules, "alpaca.data.timeframe", tf)

        for _ in range(3):
            ret = orb_spy_loader.prior_spy_return_bps(
                "2026-05-13",
                bar_archive_root=str(tmp_path / "nonexistent_bars"),
                csv_path=str(tmp_path / "nonexistent.csv"),
            )
            assert ret is not None
        assert call_count["n"] == 1, "cache must collapse 3 calls -> 1 REST"

    def test_gene_credentials_used_when_val_missing(
        self, tmp_path, monkeypatch
    ):
        # Pool fallback: GENE key should win when VAL is unset.
        monkeypatch.delenv("VAL_ALPACA_PAPER_KEY", raising=False)
        monkeypatch.delenv("VAL_ALPACA_PAPER_SECRET", raising=False)
        monkeypatch.setenv("GENE_ALPACA_PAPER_KEY", "pk_gene")
        monkeypatch.setenv("GENE_ALPACA_PAPER_SECRET", "sk_gene")
        bars = [
            _FakeBar("2026-05-09", 458.0),
            _FakeBar("2026-05-12", 460.0),
        ]
        self._install_fake_alpaca(monkeypatch, bars)
        ret = orb_spy_loader.prior_spy_return_bps(
            "2026-05-13",
            bar_archive_root=str(tmp_path / "nonexistent_bars"),
            csv_path=str(tmp_path / "nonexistent.csv"),
        )
        assert ret is not None
