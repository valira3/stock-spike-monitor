"""v9.1.25 -- unified_monitor per-tag sampling tests.

Covers the regression that turned today's (2026-05-13) [V910-EOD]
silent failure into a 6-hour outage: the pre-v9.1.25 monitor took a
flat `forensic[-200:]` slice, so V917 cutoff-reject spam (30
lines/cycle x N cycles) buried every other tag within minutes. The
new _sample_per_tag bucketed-quota path keeps each tag visible.

Asserts:
  1. Per-tag cap is enforced (no bucket exceeds max_per_tag).
  2. Spammy tags do not push the V910 bucket out of the result.
  3. Total cap respected.
  4. Order within a bucket is preserved (chronological).
  5. _classify_tag picks the most-specific prefix first (V910- wins
     over the V9 catch-all).
  6. Unknown tags land in _other.
"""

from tools.unified_monitor import (
    RAILWAY_MAX_PER_TAG,
    RAILWAY_MAX_TOTAL,
    _classify_tag,
    _sample_per_tag,
)


def _row(msg: str, ts: int = 0) -> dict:
    return {"message": msg, "ts": ts}


class TestClassifyTag:
    def test_v910_wins_over_v9_catchall(self):
        # The catch-all "[V9" appears in the same prefix; the more
        # specific "[V910-" must win because it appears earlier in the
        # tuple order. This is the bug-class guard for the v9.1.25
        # ship: any future addition to RAILWAY_TAG_BUCKETS must keep
        # specific prefixes BEFORE catch-alls.
        assert _classify_tag("[V910-EOD-ENTRY] foo") == "[V910-"

    def test_v900_picked_for_mbr_reject(self):
        assert _classify_tag("[V900-MBR-REJECT] gap=X") == "[V900-"

    def test_v917_picked_for_cutoff(self):
        assert _classify_tag("[V917-TIME-CUTOFF-REJECT] AAPL") == "[V917-"

    def test_traceback_classified_when_no_tag(self):
        assert _classify_tag("Traceback (most recent call last):") == "Traceback"

    def test_error_classified(self):
        assert _classify_tag("ERROR: something broke") == "ERROR"

    def test_unknown_lands_in_other(self):
        assert _classify_tag("nothing-here") == "_other"


class TestSamplePerTag:
    def test_per_tag_cap_enforced(self):
        # 50 V917 rows + 3 V910 rows. Default cap = 20 per tag.
        rows = [_row("[V917-TIME-CUTOFF-REJECT] AAPL", i) for i in range(50)]
        rows.extend(_row("[V910-EOD-ENTRY] tick", 100 + i) for i in range(3))
        sampled, summary = _sample_per_tag(rows)
        v917 = [r for r in sampled if "[V917-" in r["message"]]
        v910 = [r for r in sampled if "[V910-" in r["message"]]
        assert len(v917) == RAILWAY_MAX_PER_TAG
        assert len(v910) == 3   # all of them survive
        # Pre-sampling counts visible to operator.
        assert summary["[V917-"] == 50
        assert summary["[V910-"] == 3

    def test_spammy_tag_does_not_evict_quiet_tag(self):
        """Today's failure mode: V917 spam pushed V910 out of the
        flat -200 slice. Under per-tag sampling the V910 rows MUST
        survive regardless of V917 volume.
        """
        rows = []
        # 1000 V917 rows interleaved with 5 V910 rows. The V910 rows
        # arrive at timestamps 100 / 200 / 300 / 400 / 500.
        for i in range(1000):
            rows.append(_row("[V917-TIME-CUTOFF-REJECT] X", i))
            if i in (100, 200, 300, 400, 500):
                rows.append(_row(f"[V910-EOD-ENTRY] cycle={i}", i))
        sampled, _ = _sample_per_tag(rows)
        v910 = [r for r in sampled if "[V910-" in r["message"]]
        assert len(v910) == 5, "V910 lines must NOT be evicted by V917 spam"

    def test_total_cap_respected(self):
        # Many tags x many lines should not exceed max_total.
        rows = []
        for tag_prefix in ("[V910-", "[V900-", "[V917-", "[V79-ORB-",
                           "[V10-FIRE]", "[V834-", "[V83-"):
            for i in range(100):
                rows.append(_row(f"{tag_prefix}foo {i}", i))
        sampled, _ = _sample_per_tag(rows, max_per_tag=80, max_total=200)
        assert len(sampled) <= 200

    def test_chronological_order_within_bucket(self):
        # Bucket order is preserved -- the LAST max_per_tag rows in
        # each bucket survive (most recent). Within a bucket the
        # surviving rows are in input order.
        rows = [_row("[V917-TIME-CUTOFF-REJECT] foo", i) for i in range(30)]
        sampled, _ = _sample_per_tag(rows, max_per_tag=5)
        v917_ts = [r["ts"] for r in sampled if "[V917-" in r["message"]]
        # Last 5 rows = ts 25..29 in order.
        assert v917_ts == [25, 26, 27, 28, 29]

    def test_unknown_rows_kept_in_other_bucket(self):
        rows = [_row("nothing-known", i) for i in range(3)]
        sampled, summary = _sample_per_tag(rows)
        assert len(sampled) == 3
        assert summary["_other"] == 3

    def test_defaults_match_module_constants(self):
        # Caller can omit args -- they should match the constants.
        rows = [_row("[V917-X]", i) for i in range(RAILWAY_MAX_PER_TAG + 10)]
        sampled, _ = _sample_per_tag(rows)
        v917 = [r for r in sampled if "[V917-" in r["message"]]
        assert len(v917) == RAILWAY_MAX_PER_TAG

    def test_handles_text_alias_field(self):
        # railway_log_tail rows sometimes carry .text instead of
        # .message; the classifier must read both.
        rows = [{"text": "[V910-EOD-ENTRY] alias"}]
        sampled, summary = _sample_per_tag(rows)
        assert len(sampled) == 1
        assert summary["[V910-"] == 1

    def test_global_cap_default(self):
        # Construct enough total rows to exceed RAILWAY_MAX_TOTAL even
        # after per-tag capping (20/tag x ~11 tags = 220 -- still
        # under 500). Add many _other rows to push past 500.
        rows = []
        for tag_prefix in ("[V910-", "[V900-", "[V917-", "[V79-ORB-",
                           "[V10-FIRE]", "[V834-", "[V83-",
                           "Traceback", "ERROR"):
            for i in range(50):
                rows.append(_row(f"{tag_prefix}foo {i}", i))
        # 600 unknown rows -> _other bucket gets 600 (no per-tag cap
        # on _other under current design; global cap should still
        # enforce <=500 sampled).
        for i in range(600):
            rows.append(_row(f"plain-line-{i}", i))
        sampled, _ = _sample_per_tag(rows)
        assert len(sampled) <= RAILWAY_MAX_TOTAL
