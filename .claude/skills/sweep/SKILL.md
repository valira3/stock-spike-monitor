---
name: sweep
description: Run a multi-day or full-year backtest of a new theory via the existing lever-sweep GHA workflow + tools/orb_backtest.py + docs/research/r{N}_*.py chain. Do NOT build parallel infrastructure — this skill documents the production research path.
---

# GHA-driven backtest via lever-sweep

When the operator asks for a multi-day or full-year backtest of a new theory, **do not build parallel infrastructure**.

## The chain

1. **Corpus lives on `data-extensions/rth-expand` branch.** Bar files at `data/<YYYY-MM-DD>/<TICKER>.jsonl`. Backfill missing dates by dropping a JSON trigger under `.github/rth-trigger/` (e.g. `fill-2026-05-12.json`) — the `pull-rth-bars.yml` workflow auto-fires on push.

2. **Add new theory env vars to `tools/orb_backtest.py`.** Pattern:
   - Field on `ORBConfig` with default 0/False (off)
   - `_envf`/`_envs` parse in `from_env`
   - Behavior in the simulate loop
   - Surface in per-day diagnostics
   - Keep changes minimal and tested in `tests/strategy/test_orb_backtest_v18_rules.py` (or a new file)

3. **Write a sweep script** under `docs/research/r<N>_<theme>.py` mirroring `r2`-`r5`. Theories are a list of `(vid, env_overrides)` tuples layered on `BASE` (which encodes the v12-winning config). Evaluate each theory on full-year + quarterly slices to catch in-sample fits.

4. **Dispatch the sweep via `Lever Sweep`** workflow (Actions tab → "Lever Sweep" → "Run workflow"). The `variants` input takes the JSON tuple from `python3 docs/research/r<N>_<theme>.py --print-variants`. Results commit to the `sweep-results` branch under `sweeps/run-<id>/<vid>/`.

5. **Retrieve results via MCP**:
   ```
   mcp__github__get_file_contents(
     owner="valira3", repo="stock-spike-monitor",
     path="sweeps/run-<id>/<vid>/summary.json",
     ref="sweep-results"
   )
   ```

## Anti-pattern

Building a new `tools/corpus_backtest.py` or new `corpus-backtest.yml` workflow. The `lever-sweep.yml` + `tools/lever_sweep_runner.py` + `tools/orb_backtest.py` chain is the production research path.

## Where falsified theories live

`docs/pl_optimization_final_report_v<N>.md` (currently v13). Each round's `r{N}_*.py` script also has a top-of-file docstring listing falsified ideas. **Check there before re-running a dead theory.**

## Common deviations and when they're appropriate

- **Single-day quick check**: run `tools/orb_backtest.py` locally on `data-extensions/rth-expand`. No sweep needed.
- **Live-engine-replay-specific rule** (e.g. rules that depend on multi-fire same-side re-entries): the classical `orb_backtest.py` will show 0 effect. Switch to `tools/orb_replay_day.py`-based custom replay — see the `replay` skill.
- **Per-portfolio (Val/Gene) analysis**: the lever-sweep harness runs Main-only. For per-portfolio differences, use the live-replay skill.
