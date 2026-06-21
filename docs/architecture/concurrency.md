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

**Cross-instance caveat:** a second `FileLock` *instance* on the same path
acquired on the same thread does NOT reentrantly succeed — a blocking acquire
deadlocks (hangs); this is why `_on_timer` releases `self._lock` before
executing jobs (a job callback may build a second `CronService` instance).
The execution window (after load, before save) is therefore not fully
serialised at the file level, but the before-execution load and the
after-execution save each acquire the lock independently, so the net
`jobs.json` state remains consistent.

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

3. **CronService execution-window external writes.** `CronService` `_on_timer`
   releases `self._lock` before executing jobs, then saves run-state afterward
   WITHOUT reloading — an external write to `jobs.json` during the execution
   window is overwritten by the post-execution save (a bounded, pre-existing
   last-writer-wins residual; close by reload-merging run-state under the
   final lock).

4. **`SecretStore.set_scope()` is an unlocked in-memory mutator.**
   (`durin/security/secrets.py`) It does not write to disk itself; a caller
   doing `load → set_scope → save()` is an unlocked read-modify-write.
   Migrate to a locked path opportunistically.

5. **`oauth/<provider>.json` token writes are not durin-locked.**
   These files are owned by the external `oauth-cli-kit` library
   (`FileTokenStorage`) and are intentionally outside durin's locking
   domain — out of durin's control.

## SQLite helpers (FTS5 / derived indexes)

`durin/utils/sqlite_util.py` provides two primitives used wherever durin opens a
SQLite database that may have concurrent cross-process writers (currently the FTS5
index at `<workspace>/.durin/index/fts.sqlite`):

**`connect(path, *, read_only=False, busy_timeout_ms=5000)`**

Opens *path* with `check_same_thread=False` and `isolation_level=None`.  For
read-write connections it sets WAL journal mode (with a DELETE-journal fallback for
NFS/SMB/FUSE filesystems that reject WAL locking), `PRAGMA busy_timeout`, and
`PRAGMA synchronous=NORMAL`.  When `read_only=True` the database is opened via a
`file:?mode=ro` URI — no write lock is ever taken and the WAL/journal/busy
pragmas are skipped (a read-only connection cannot set them and does not need to).

**`execute_write(conn, fn, *, attempts=15)`**

Runs `fn(conn)` inside `BEGIN IMMEDIATE … COMMIT`.  `BEGIN IMMEDIATE` acquires the
write lock at transaction start, so no other writer can interleave.  On
`SQLITE_BUSY` / "locked" errors it rolls back, sleeps 20–150 ms with random jitter,
and retries up to *attempts* times.  Any other `OperationalError` is re-raised
immediately.

Together these two primitives ensure that concurrent cross-process writers (gateway,
TUI `AgentLoop`, cron, heartbeat) do not drop FTS5 rows due to unretried
`SQLITE_BUSY` errors.

## Per-turn provider snapshot (hazard #8)

The gateway runs a single shared `AgentRunner` instance.
`AgentLoop._apply_provider_snapshot` (`durin/agent/loop.py`) calls
`runner.provider = new_provider` on each session's `/model` swap or
per-turn provider refresh.  Because multiple sessions share the same runner,
a concurrent swap can mutate `self.provider` while another session's turn is
mid-flight inside `run()` — causing that in-flight turn to call the wrong
provider (wrong model, auth, or endpoint), producing "model not found" errors
or routing responses to the wrong backend.

**Fix:** `AgentRunSpec` carries an optional `provider: LLMProvider | None`
field (per-turn snapshot).  `AgentRunner.run()` resolves
`provider = spec.provider or self.provider` once at entry and passes that
local reference into every method that makes a model call
(`_request_model`, `_request_finalization_retry`, `_mid_turn_precheck`,
`_snip_history`).  `self.provider` is unchanged — it remains the shared
default and the fallback for callers that do not set `spec.provider`.

`AgentLoop` sets `spec.provider` in `_dispatch` immediately after
`_apply_provider_snapshot` so the snapshot is always the provider that was
active at the moment the turn was dispatched.

## AliasIndex in-process staleness (hazard #17)

`AliasIndex` is built lazily once per process via
`aliases_cache.get_shared_alias_index(memory_root)` and then mutated
incrementally.  All three runtime consumers (memory search, refine pass,
entity absorption) share the same in-memory instance.

**Hazard:** `write_entity` commits a new or updated entity page to git
but previously did NOT call `AliasIndex.refresh_for` on the shared
instance — a freshly written entity was invisible to entity-aware ranking
until the process restarted.

**Fix (in-process):** `memory_writer._refresh_alias_index` is called
after every successful `write_entity` CAS commit.  It calls
`AliasIndex.refresh_for(page, slug)` (incremental — not a full rebuild)
on the already-cached index.  The guard `_cache.get(memory_root) is None`
skips the call when the index has not been built yet in this process, so
entity writes before the first search query impose no build cost.

**Cross-process divergence** (a second process' AliasIndex missing the
write) is a known accepted residual: cross-process consumers rebuild the
index on process start from the git-committed pages, so the divergence
self-heals on restart.  No cross-process lock or invalidation is needed
for this path.
