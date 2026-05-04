"""Static-source test for v6.10.1: ingest feed enum fix.

Verifies that _run_ws_ingest in ingest/algo_plus.py uses DataFeed enum
(DataFeed.SIP or DataFeed.IEX) instead of the bare string feed="sip".
Does not require alpaca-py to be installed in CI.
"""

import pathlib
import re


_ALGO_PLUS = pathlib.Path(__file__).parent.parent / "ingest" / "algo_plus.py"


def _extract_run_ws_ingest(source: str) -> str:
    """Return the text of the _run_ws_ingest method body."""
    idx = source.find("def _run_ws_ingest(")
    assert idx != -1, "_run_ws_ingest not found in ingest/algo_plus.py"
    return source[idx:]


def test_datafeed_enum_used_not_string():
    """_run_ws_ingest must pass a DataFeed enum to StockDataStream, not a raw string."""
    source = _ALGO_PLUS.read_text(encoding="utf-8")
    body = _extract_run_ws_ingest(source)

    # Must import DataFeed inside the method
    assert "from alpaca.data.enums import DataFeed" in body, (
        "DataFeed import missing from _run_ws_ingest; enum must be imported before use"
    )

    # Must use DataFeed enum member (SIP or IEX as fallback)
    has_enum = bool(
        re.search(r"feed\s*=\s*DataFeed\.(SIP|IEX)", body)
    )
    assert has_enum, (
        "_run_ws_ingest must pass feed=DataFeed.SIP (or DataFeed.IEX) to StockDataStream"
    )

    # Must NOT contain the bare string literals that caused the crash
    assert 'feed="sip"' not in body, (
        'Bare string feed="sip" still present in _run_ws_ingest; replace with DataFeed.SIP'
    )
    assert "feed='sip'" not in body, (
        "Bare string feed='sip' still present in _run_ws_ingest; replace with DataFeed.SIP"
    )
