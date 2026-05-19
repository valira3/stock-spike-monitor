# Lock ordering reference

This file lists every `threading.Lock` / `threading.RLock` declared in
production code (everything outside `tests/`, `scripts/`, `tools/`, and
`synthetic_harness/`). The aim is **two things only**:

1. A maintainer adding a new lock can see the existing inventory and pick a
   non-colliding name + scope.
2. Anyone introducing a code path that acquires two locks together can check
   the "may-acquire-while-holding" table for an ordering conflict.

There is no automated deadlock detector. `tests/strategy/test_lock_order_doc.py`
is the lightweight enforcement: every code-declared lock must appear in this
file, and any lock listed here that no longer exists in code must be removed.

## Inventory (production locks only)

| # | Lock | File:line | Purpose | Scope | Type |
|---|---|---|---|---|---|
| 1 | `_init_lock` | `persistence.py:52` | One-shot SQLite schema init | module | Lock |
| 2 | `_paper_save_lock` | `paper_state.py:44` | Serialize `paper_state*.json` writes | module | Lock |
| 3 | `_dir_lock` | `lifecycle_logger.py:92` | Lifecycle log directory create | instance | Lock |
| 4 | `_locks_guard` | `lifecycle_logger.py:94` | Guards the per-position lock dict | instance | Lock |
| 5 | `_locks[position_id]` | `lifecycle_logger.py:117` | Per-open-position lifecycle log | per-position | RLock |
| 6 | `_meta_lock` | `lifecycle_logger.py:99` | Meta-cache for lifecycle JSON | instance | Lock |
| 7 | `_default_logger_lock` | `lifecycle_logger.py:431` | Singleton lazy init | module | Lock |
| 8 | `_audit_lock` | `ingest/audit.py:82` | Multi-statement audit SQLite ops | module | Lock |
| 9 | `_init_lock` (audit) | `ingest/audit.py:84` | One-shot audit schema init | module | Lock |
| 10 | `_sla_lock` | `ingest/sla.py:78` | SLA table mutations | module | RLock |
| 11 | `_lock` (algo_plus) | `ingest/algo_plus.py:205` | Stream subscription state | instance | Lock |
| 12 | `_ingest_stats_lock` | `ingest/algo_plus.py:676` | Ingest-stats counters | module | Lock |
| 13 | `_system_test_lock` | `trade_genius.py:5616` | One-shot system-test trigger | module | Lock |
| 15 | `_lock` (volume_profile) | `volume_profile.py:634` | Volume baseline state | instance | Lock |
| 16 | `_bootstrap_lock` | `orb/live_runtime.py:88` | OrbEngine atomic build/swap | module | RLock |
| 17 | `_sizes_lock` | `orb/live_runtime.py:89` | Per-portfolio size dict | module | RLock |
| 18 | `_activity_lock` | `orb/live_runtime.py:101` | `_recent_activity` deque | module | RLock |
| 19 | `_rollback_lock` | `orb/live_runtime.py:1150` | Rollback-cooldown bookkeeping | module | RLock |
| 20 | `_lock` (RiskBook) | `orb/risk_book.py:77` | Per-portfolio risk admission | per-portfolio | RLock |
| 21 | `_lock` (RiskBookRegistry) | `orb/risk_book.py:499` | Registry mutations | instance | RLock |
| 22 | `_lock` (earnings_refresh) | `orb/earnings_refresh.py:71` | v10.0.1 refresh state | module | Lock |
| 23 | `_lock` (scanner_state) | `orb/scanner_state.py:20` | v10.0.0 scanner result | module | Lock |
| 24 | `_signal_listeners_lock` | `orb/signal_bus.py:35` | v10.0.1 bus listener list | module | Lock |
| 24b | `_trade_log_lock` (carved) | `orb/trade_log.py:50` | v10.0.1 trade-log JSONL append (carved from trade_genius.py) | module | Lock |
| 25 | `_cache_lock` | `engine/extended_universe.py:51` | Earnings-universe cache | module | Lock |
| 26 | `_LOCK` (v770_flags) | `engine/v770_flags.py:60` | v770 flag cache | module | Lock |
| 27 | `_gate_lock` | `engine/ingest_gate.py:67` | Ingest-gate state | module | Lock |
| 28 | `_snapshot_cache_lock` | `dashboard_server.py:1619` | /api/state response cache | module | Lock |
| 29 | `_login_attempts_lock` | `dashboard_server.py:2759` | Brute-force login tracker | module | Lock |
| 30 | `_executor_cache_lock` | `dashboard_server.py:3228` | Per-executor cache | module | Lock |
| 31 | `_indices_cache_lock` | `dashboard_server.py:3230` | Indices cache | module | Lock |

## May-acquire-while-holding (the only edges that exist as of v10.0.1)

The table below is the entire set of multi-lock acquisitions in production code.
If you are adding a new code path that acquires two locks, add a row.

| Holder | Acquires while holding | Why | File:line |
|---|---|---|---|
| `_bootstrap_lock` | (nothing) | bootstrap is the outermost lock; never call into other locked code while holding it | `orb/live_runtime.py:347-382` |
| `_signal_listeners_lock` | (nothing) | snapshot the list under the lock, release before iterating | `orb/signal_bus.py:107-110, 132-133` |
| `_locks_guard` | `_locks[position_id]` | install a new per-position RLock under the guard, then acquire it | `lifecycle_logger.py:113-119` |
| `_paper_save_lock` | (nothing) | serializes file writes only; never calls other locked code | `paper_state.py:44+` |
| RiskBook `_lock` | (nothing) | admission decisions are entirely local to the book | `orb/risk_book.py:77+` |

**There are no cycles in this graph as of v10.0.1.** A new edge from any
"acquires while holding" row back to the holder column would deadlock under
contention. The smoke test in `tests/strategy/test_lock_order_doc.py` does not
detect cycles automatically; that requires runtime instrumentation. The test
only enforces that every code-declared lock is documented here.

## Conventions for new locks

- **Default to module scope** unless the lock is per-portfolio / per-position.
- **Default to `Lock`** unless reentrant call-back from inside a locked region is
  expected, in which case use `RLock`.
- **Acquire-then-snapshot-then-release**, NOT acquire-and-iterate. The signal
  bus pattern (`with lock: listeners = list(_signal_listeners)`) is the
  canonical example.
- **Never call into a different module's locked code while holding a lock.**
  If you must, document the edge in the table above.
- **Add a row to this file** in the same PR that adds the lock. The doc-test
  fails CI otherwise.
