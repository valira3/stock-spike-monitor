# Lessons

Project-specific gotchas. Reviewed at the start of every session via CLAUDE.md.
When something bites you twice, it goes here. Each entry is one or two lines
and has saved at least an hour of debugging.

Newest entries at the top. Lead with the rule. Follow with the why.

---

## 2026-05-20

- **`find /tmp/simulator_data -delete` will follow the `bars/` symlink and wipe the corpus.** The runner's `_ensure_data_root_layout` creates `<TG_DATA_ROOT>/bars` as a symlink to `data_pm_universe/`. `find -delete` traverses INTO that symlink during the depth-first walk and `unlink()`s every JSONL inside the target. On 2026-05-20 this deleted 258 of 343 corpus days; recovery via `tools/restore_corpus_from_cache.py` reconstructed them from the `.bt_cache/<date>/<ticker>.pkl` files. **Always use** `rm -rf /tmp/simulator_data && mkdir -p /tmp/simulator_data` (rm doesn't descend into symlinks for cleanup) **or** delete the `bars` symlink FIRST: `find /tmp/simulator_data -type l -delete; rm -rf /tmp/simulator_data; mkdir -p /tmp/simulator_data`.

- **Bar/corpus data lives on the Railway `/data` volume, never in git.** `data/20YY-MM-DD/`, `data/.cache_v2/`, `data/bars/`, `data/dynamic_universe/`, `data/tick-data/` are all gitignored. Before May 2026, ~6,600 corpus files (~2.5 GB) had been accidentally committed during research; they were untracked en masse. If you see new files under `data/2025-*/` or `data/2026-*/` in `git status`, do **not** commit them.

- **The `Dockerfile` has explicit per-file `COPY` lines and drifts out of sync when modules are deleted.** Any time you delete a top-level `.py` (e.g. `market_brief.py`, `volume_bucket.py`) or a package (`earnings_watcher/`), grep the Dockerfile for matching `COPY` lines and remove them — otherwise `docker-boot` CI fails with `failed to calculate checksum of ref ... not found`.

- **Python 3.9 trips on PEP 604 `X | None` syntax.** `python3 -c "import trade_genius"` raises `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` even though `run_ci.py` and prod (3.11) pass. When local smoke fails with that error, check Python version before debugging the code. Use `from __future__ import annotations` to make `X | None` safe under 3.9.

- **`_derive_current_main_note` regex must accept both `--` and the em-dash.** The CHANGELOG-heading parser in `trade_genius.py` lives behind `CURRENT_MAIN_NOTE` and was written for `--`; newer headings use a real em-dash. If `CURRENT_MAIN_NOTE` resolves to a stale version, check that line.

- **Two em-dash forms coexist in source.** Some legacy comments have the real `—` character; newer comments use the `—` escape literal (because CLAUDE.md forbids real em-dashes in `.py`). String-based search/replace must handle both. When `Edit` fails with "String to replace not found", `grep -n` to see which form the file uses.

- **`broker/positions.py` and `_v5104_maybe_fire_entry_2` are stubs now.** Both legacy entry/exit paths (Tiger Sentinel A/B/C + v5104 scale-in) are deleted but the public names survive as no-op stubs so back-compat imports in `broker/orders.py` + `trade_genius.py` + `broker/__init__.py` keep working. Do not delete the stubs without also rewriting the import chain.

- **Don't merge staging → main casually.** Staging carries 96+ commits ahead of main and accumulates research/replay work that should never reach production. Use the `promote_staging.py` pre-flight or hand-pick. Production reads from `main`; `staging` is a Railway environment with its own URL (`https://tradegenius-staging.up.railway.app`).

- **`bot_version.py` is release-managed; the `version-bump-check` workflow gates PRs to main on `BOT_VERSION` matching the top CHANGELOG heading.** Hand-editing it mid-session breaks that gate. `[skip-version]` in the PR title bypasses for docs/CI-only changes.

- **`scripts/run_ci.py` is the canonical preflight, not `bash scripts/preflight.sh`.** Both exist; the bash version is the older path. Use `python scripts/run_ci.py` (also runs on Windows/macOS/Linux) and add `--wide` for the top-level `tests/` suite.

- **`pytest tests/strategy/` is the fast lane (1,173 tests, ~6 s, no telegram dep). `pytest tests/` is the wide lane (needs `telegram`, `alpaca-py`, `lxml` + `.env.monitor`).** Don't expect tests under `tests/` (top level) to run in CI's fast lane.

- **`gh pr merge --admin` triggers Railway production redeploy.** Treat it as the deploy button for `main`. Confirm CI is green and post-deploy-smoke hasn't paged before using.

---

## Format guidance

- One line where possible. Two when the *why* matters.
- Lead with the rule. Follow with the reason.
- Use backticks for code. Plain text for everything else.
- Newest entries at the top.

## When to add an entry

- Something bit you twice.
- A wrapper swallowed an error and the next session has no way to know.
- A piece of legacy left a sharp edge that grep won't catch.
