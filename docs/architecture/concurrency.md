# Concurrency architecture

## Governing invariant

Session files (`.jsonl`, `.meta.json`, `.md`) are the source of truth.
SQLite (`fts.sqlite`) and LanceDB are derived indexes only and are never
authoritative for session content. This is a deliberate divergence from
hermes-agent, which moved sessions into SQLite.

Rationale: session files are bounded (`FILE_MAX_MESSAGES=2000`, ~2.5 MB
ceiling), so there is no scale forcing-function. Doc-native storage
preserves grep/cat/git/recover operability and one-truth conceptual
integrity.

## Problem fixed (Phase A)

Durin runs as multiple OS processes over a single shared `DURIN_HOME`:
the gateway daemon, `durin agent --tui` (with its own in-process
`AgentLoop`), cron, and heartbeat. Previously each process held a
never-invalidated in-memory `SessionManager` cache and `save()` did a
lockless whole-file rewrite. This caused cross-process clobber and
split-brain: the TUI and webui could show divergent session chains for
the same key.

## Mechanism

### `durin/utils/file_lock.py` — `cross_process_lock(target, *, timeout)`

Reentrant (thread-local guard) cross-process advisory `flock` on
`<target>.lock`. Extracted and generalized from
`durin/channels/msteams.py:_refs_file_lock`. Falls back to a
`threading.Lock` on platforms without `fcntl`/`msvcrt`. The reentrancy
guard is per-thread; it does **not** cross `asyncio.to_thread`
boundaries.

### `durin/session/manager.py` — `reload(key)` and `save()`

`reload(key)` drops the in-memory cache entry and re-reads from disk
(load-per-turn semantics). `save()` wraps its entire write unit —
`.jsonl` append, `.meta.json` update, `.md` regen, FTS reindex — in
`cross_process_lock(session_path)`, serializing all whole-file writes
across processes.

### `durin/session/turn_lease.py` — `session_turn_lease(session_path, *, timeout=600)`

Async context manager that acquires the cross-process lock for the whole
RESTORE..SAVE span of one turn. The blocking `flock` is acquired off the
event loop via `asyncio.to_thread` so the loop is not blocked during
contention. The lock is held on `<key>.turn.lock` (a file distinct from
save()'s `<key>.jsonl.lock`; see lock-ordering below). Wired in
`durin/agent/loop.py` `_dispatch` after the in-process `asyncio.Lock`
(fast path). On acquire-timeout it publishes a clear "session busy in
another window" message rather than dropping the turn silently.

### `durin/config/loader.py` — `save_config` and `mutate_config`

`save_config` wraps its entire write — the multi-file split-layout set plus
stale-file unlink — in `cross_process_lock(config_path)`.  This serializes
concurrent writers and makes the split-layout write atomic as a SET relative
to other lock holders, so a reader under the lock never sees a torn
cross-section state.

`mutate_config(mutator)` is the lost-update-safe read-modify-write entry
point: it acquires the lock, reloads the config from disk, calls *mutator*,
saves, and returns the updated config.  Because `cross_process_lock` is
reentrant (thread-local guard), the inner `save_config` re-taking the lock
is safe.

**Residual:** a direct `load_config() → edit → save_config()` not routed
through `mutate_config` remains last-writer-wins across processes.  This
matches hermes and is an accepted trade-off; callers are migrated
opportunistically.

### `durin/cron/service.py` — `CronService` mutators

`CronService` keeps `jobs.json` consistent across processes (gateway
scheduler + `durin cron` CLI) via `self._lock = FileLock(<cron_dir>.lock)`.

Every mutator that performs a `_load_store → mutate → _save_store`
read-modify-write now wraps that sequence in `with self._lock:`.  Without
the lock, two concurrent adders both load the old snapshot, each append one
job, and the second writer clobbers the first — producing a lost-update.

`filelock.FileLock` is reentrant *within a process* when the same instance
re-acquires (lock count increments).  `_append_action` and `_merge_action`
already use `with self._lock:`; a mutator that calls them while holding the
lock does not deadlock.

**Cross-instance caveat:** two *different* `FileLock` objects at the same
path in the same thread will deadlock (filelock raises `RuntimeError`).
`_on_timer` avoids this by releasing the lock before calling `_execute_job`:
an `on_job` callback that creates a second `CronService` and calls a mutator
on it would otherwise deadlock.  The execution window (after load, before
save) is therefore not fully serialised at the file level, but the
before-execution load and the after-execution save each acquire the lock
independently, so the net `jobs.json` state remains consistent.

**Read-only path:** `list_jobs`, `get_job`, and `status` call `_load_store`
without the lock.  They are last-reader-wins, which is acceptable for
display/scheduling reads.

### `durin/cli/gateway_daemon.py` — `acquire_gateway_singleton()`

Holds `flock(LOCK_EX|LOCK_NB)` on `DURIN_HOME/gateway.lock`. The OS
releases the lock automatically if the gateway crashes, replacing the
previous PID-file + `os.kill(pid, 0)` TOCTOU check. `gateway.pid` is
kept for human-readable status only.

## Lock-ordering invariant

Within a single turn the acquisition order is fixed and must not be
reversed:

1. In-process `asyncio.Lock` (fast path — no syscall, no I/O)
2. Turn lease on `<key>.turn.lock` — acquired on a worker thread via
   `asyncio.to_thread`
3. *(turn body executes)*
4. `save()` lock on `<key>.jsonl.lock` — acquired on the event-loop
   thread inside `save()`

The turn lease (step 2) and the save lock (step 4) use **separate lock
files** by design. The thread-local reentrancy guard in
`cross_process_lock` does not cross the `asyncio.to_thread` boundary:
if both used the same file, `save()` — running on the event-loop thread
— would deadlock against the turn lease still held by the worker thread.
Because the three flock files are always distinct and always acquired in
the same order, there is no opposite-order acquisition and no deadlock
risk.

## Phase-A residual ledger

The following known limitations are deferred to the
background/direct-saver serialization phase:

1. **No TTL on the held turn lease.** A hung (alive-but-stuck) process
   holds `<key>.turn.lock` until it exits. The OS releases on crash. A
   TTL advisory-lock table is a later-phase addition.

2. **Out-of-turn / background savers bypass the turn lease.** They
   acquire only `save()`'s `<key>.jsonl.lock`, not `<key>.turn.lock`,
   so they can still clobber a concurrent turn's whole-file write.
   Affected paths:
   - HTTP rename in `durin/service/sessions.py`
   - Cron / heartbeat `process_direct` saves
   - Webui background title-generation save
     (`durin/agent/loop.py` `_schedule_background` →
     `durin/utils/webui_titles.py`)

   Closing these requires background/direct-saver serialization work not
   yet scheduled.

3. **`SecretStore.set_scope()` is an unlocked in-memory mutator.**
   (`durin/security/secrets.py`) It does not write to disk itself; a caller
   doing `load → set_scope → save()` is an unlocked read-modify-write.
   Migrate to a locked path opportunistically.

4. **`oauth/<provider>.json` token writes are not durin-locked.**
   These files are owned by the external `oauth-cli-kit` library
   (`FileTokenStorage`) and are intentionally outside durin's locking
   domain — out of durin's control.
