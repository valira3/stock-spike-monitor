"""Canonical sweep subprocess environment + pre-flight smoke check.

All sweep runners MUST use build_sweep_env() to construct subprocess env.
All sweep runners MUST call preflight_smoke() before fanning out the day grid.

Background: three consecutive Wave 2 sweep failures (v6.9.0\u20136.9.2) were
caused by silent failure modes that a 10-second smoke check would have
caught immediately:

  v6.9.0 \u2014 cache regression (no smoke check; silent perf failure)
  v6.9.1 \u2014 /data permission errors (workers reported FAIL pnl=? for hundreds
             of days before anyone noticed)
  v6.9.2 \u2014 FMP_API_KEY hard-fail at import (empty raw/*.json files; no abort)

This module is the single enforcement point so every future sweep runner
gets the same hermetic environment and mandatory pre-flight gate.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required env keys injected into every sweep subprocess.
# These guard the three failure modes above:
#   SSM_SMOKE_TEST  -- disables Telegram/scheduler so trade_genius imports cleanly
#   TELEGRAM_BOT_TOKEN -- satisfies the token-present check without hitting API
#   FMP_API_KEY     -- prevents hard-fail at import when key is absent (v6.9.2 mode)
# ---------------------------------------------------------------------------

REQUIRED_ENV: dict[str, str] = {
    "SSM_SMOKE_TEST": "1",
    "TELEGRAM_BOT_TOKEN": "000:fake",
    "FMP_API_KEY": "sweep_dummy_key",
}


def build_sweep_env(
    *,
    isolate_dir: Path,
    tg_data_root: Path,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct a hermetic subprocess env for a single replay invocation.

    Steps applied in order (last writer wins):

    1. Start from a full copy of ``os.environ`` so the subprocess inherits
       PATH, PYTHONPATH, and any other ambient keys the host process needs.
    2. Overwrite the ``REQUIRED_ENV`` keys unconditionally \u2014 this is the guard
       that prevented the v6.9.2 FMP_API_KEY hard-fail.
    3. Derive ``PAPER_STATE_PATH`` and ``PAPER_LOG_PATH`` under ``isolate_dir``
       so concurrent sweep workers never share paper-state files (the v6.9.1
       isolation fix).
    4. Set ``TG_DATA_ROOT`` to ``tg_data_root`` (must exist; raises if not).
    5. Layer ``extra`` on top last so per-variant flag overrides (e.g.
       ``STOP_PCT=0.015``) win over everything else.

    Parameters
    ----------
    isolate_dir:
        Per-variant working directory.  Must exist; ValueError is raised if
        it does not so callers get a loud error at setup time rather than a
        silent ``FileNotFoundError`` inside the subprocess.
    tg_data_root:
        Root of the bars/or/forensics tree.  Must exist; ValueError raised
        otherwise.
    extra:
        Optional dict of additional env overrides applied last.

    Returns
    -------
    dict[str, str]
        Complete env mapping ready for ``subprocess.run(env=...)``.

    Raises
    ------
    ValueError
        If ``isolate_dir`` or ``tg_data_root`` do not exist.
    """
    if not isolate_dir.exists():
        raise ValueError(
            f"build_sweep_env: isolate_dir does not exist: {isolate_dir}"
        )
    if not tg_data_root.exists():
        raise ValueError(
            f"build_sweep_env: tg_data_root does not exist: {tg_data_root}"
        )

    env: dict[str, str] = dict(os.environ)

    # Step 2 \u2014 overwrite required guard keys
    env.update(REQUIRED_ENV)

    # Step 3 \u2014 per-isolate paper state paths
    env["PAPER_STATE_PATH"] = str(isolate_dir / "paper_state.json")
    env["PAPER_LOG_PATH"] = str(isolate_dir / "paper_trade.log")

    # Step 4 -- data root + all derived path vars so every subsystem
    # that reads an env var (STATE_DB_PATH, BAR_ARCHIVE_BASE, etc.) lands
    # under the correct writable tree without touching /data.
    # v6.9.4: derived paths are set here once; subsystem defaults then
    # pick them up automatically via os.environ.get().
    env["TG_DATA_ROOT"] = str(tg_data_root)

    _derived: dict[str, Path] = {
        "STATE_DB_PATH":        tg_data_root / "state.db",
        "BAR_ARCHIVE_BASE":     tg_data_root / "bars",
        "UNIVERSE_GUARD_PATH":  tg_data_root / "tickers.json",
        "INGEST_AUDIT_DB_PATH": tg_data_root / "ingest_audit.db",
        "VOLUME_PROFILE_DIR":   tg_data_root / "volume_profiles",
        "OR_DIR":               tg_data_root / "or",
        "FORENSICS_DIR":        tg_data_root / "forensics",
        "TRADE_LOG_PATH":       tg_data_root / "trade_log.jsonl",
        "SSM_BAR_CACHE_DIR":    tg_data_root / "bar_cache",
    }
    for key, dpath in _derived.items():
        env[key] = str(dpath)
        # mkdir for directory-type paths; file-type paths get their
        # parent directory created so the file can be written on first use.
        if dpath.suffix == "":
            dpath.mkdir(parents=True, exist_ok=True)
        else:
            dpath.parent.mkdir(parents=True, exist_ok=True)

    # Step 5 -- per-variant overrides
    if extra:
        env.update(extra)

    return env


def preflight_smoke(
    *,
    workdir: Path,
    bars_dir: Path,
    sample_date: str,
    env: dict[str, str],
    timeout_sec: int = 60,
) -> None:
    """Run replay_v511_full on ONE day. Abort sweep if it fails.

    Runs ``python -m backtest.replay_v511_full`` against ``sample_date``
    using the provided hermetic ``env``.  Any of the following conditions
    cause an immediate ``RuntimeError`` so the sweep runner can abort before
    fanning out the full day grid:

    * Subprocess returncode != 0
    * Output JSON file missing or empty (v6.9.2 symptom: empty raw/*.json)
    * Output JSON ``summary`` field missing or has no ``entries`` / ``exits``
      keys (v6.9.0 symptom: structurally malformed output)
    * Stderr contains ``'Traceback'`` (uncaught Python exception)
    * Stderr contains ``'Permission denied'`` (v6.9.1 symptom: /data access)

    On failure the first 50 lines of stderr are logged at ERROR level so
    the on-call engineer has immediate forensic context without needing to
    dig through worker logs.

    Parameters
    ----------
    workdir:
        Directory where the smoke-check output JSON is written
        (``smoke_check_output.json``).
    bars_dir:
        Path passed to ``--bars-dir``.
    sample_date:
        YYYY-MM-DD string for the single-day replay.
    env:
        Env mapping from ``build_sweep_env()``.
    timeout_sec:
        Subprocess wall-clock limit (default 60 s).

    Raises
    ------
    RuntimeError
        On any of the failure conditions listed above.
    """
    output_path = workdir / "smoke_check_output.json"
    cmd = [
        "python", "-m", "backtest.replay_v511_full",
        "--date", sample_date,
        "--bars-dir", str(bars_dir),
        "--output", str(output_path),
    ]

    logger.info("preflight_smoke: running single-day replay for %s", sample_date)

    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )

    stderr_text: str = result.stderr or ""
    stderr_lines = stderr_text.splitlines()

    def _fail(reason: str) -> None:
        excerpt = "\n".join(stderr_lines[:50])
        logger.error(
            "preflight_smoke FAILED (%s). First 50 lines of stderr:\n%s",
            reason,
            excerpt,
        )
        raise RuntimeError(
            f"preflight_smoke failed: {reason}. "
            f"stderr excerpt:\n{excerpt}"
        )

    # Gate 1: exit code
    if result.returncode != 0:
        _fail(f"subprocess exited with returncode={result.returncode}")

    # Gate 2: stderr poison strings (check before JSON so we get useful context)
    if "Traceback" in stderr_text:
        _fail("stderr contains 'Traceback' (uncaught exception)")
    if "Permission denied" in stderr_text:
        _fail("stderr contains 'Permission denied' (/data access error)")

    # Gate 3: output file present and non-empty
    if not output_path.exists():
        _fail(f"output JSON not found at {output_path}")
    raw = output_path.read_text().strip()
    if not raw:
        _fail(f"output JSON is empty at {output_path}")

    # Gate 4: structural validity of output summary
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(f"output JSON parse error: {exc}")
        return  # unreachable; satisfies type-checker

    summary = data.get("summary")
    if summary is None:
        _fail("output JSON missing 'summary' field")
    if "entries" not in summary:
        _fail("output JSON summary missing 'entries' key")
    if "exits" not in summary:
        _fail("output JSON summary missing 'exits' key")

    logger.info(
        "preflight_smoke PASSED for %s (entries=%s exits=%s)",
        sample_date,
        summary.get("entries"),
        summary.get("exits"),
    )
