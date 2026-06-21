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
The execution window (after load, before execute) is therefore not serialised
at the file level. To stop the post-execution save from clobbering external
edits made during that window, `_on_timer` now **reloads `jobs.json` under the
final lock and re-applies only the run-state deltas** (`last_status`,
`last_error`, `last_run_at_ms`, `run_history`, `next_run_at_ms`, `updated_at_ms`,
plus one-shot disable/removal) onto the freshly-loaded store, matched by job id;
an executed job removed externally during the window is not resurrected. So a
concurrent `add_job`/`remove_job`/`enable_job`/`update_job` on *other* jobs
survives. (See "Residual ledger" for the bounded same-job-schedule-edit case.)

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

## Memory git-worktree lock (sub-hazard A + B)

`_commit_dirty_as_user` and `_fast_forward_working_tree` in
`durin/memory/memory_writer.py` both mutate the git working tree and
`.git/index` via dulwich porcelain. Under concurrent durin processes
(gateway, TUI, cron) sharing one `DURIN_HOME` these mutations could
interleave; dulwich's `_transition_to_file` is non-atomic
(unlink-then-recreate).

Both methods acquire `cross_process_lock(<memory_git_root>/.git-worktree)`
around the dulwich call. Because `cross_process_lock` is reentrant
per-thread, the common call sequence
`_commit_dirty_as_user → (CAS) → _fast_forward_working_tree` re-enters
the lock safely on the same thread.

### Sub-hazard A — concurrent commit + reset corrupt .git/index

The lock serializes `add+commit` and `reset --hard` so they cannot
interleave and leave `.git/index` in a torn state.

### Sub-hazard B — reset absent-file window {#reset-absent-window}

`reset --hard` (`dulwich porcelain.reset`) internally calls
`_transition_to_file` which unlinks the file and then recreates it. During
this window a concurrent reader that calls `path.is_file()` observes
`False` and may permanently prune the FTS / vector row for a file that is
still valid.

**Fix:** The two prune paths that act on an absent-file observation both
acquire the same `.git-worktree` lock before committing to a prune:

- `durin/memory/indexer.py` `reindex_one_file`: when `not md_path.is_file()`
  in the watcher, the code acquires `.git-worktree` and re-checks
  `md_path.is_file()`.  If present on re-check, the reset completed and the
  prune is skipped.  If still absent, the deletion is genuine.
- `durin/memory/vector_index.py` `prune_orphan_rows`: the initial scan
  collects rows whose `path` column resolves to an absent file, then acquires
  `.git-worktree` and re-checks each candidate.  Only still-absent rows are
  deleted.

**Lock ordering:** `.git-worktree` is the **outermost** memory lock.  The
prune paths take `.git-worktree` THEN perform the FTS/LanceDB row deletes —
which take no cross-process lock of their own (SQLite WAL + `busy_timeout` for
FTS; plain row ops for LanceDB), so they cannot create an opposite-order edge.
No path takes an FTS/Lance lock and then `.git-worktree`, so there is no
opposite-order acquisition and no deadlock risk.  The mutation path takes
`.git-worktree` then dulwich only (no FTS/Lance held during the reset).

The canonical lock-path helper is `git_worktree_lock_path(memory_git_root)`
in `memory_writer.py`; all three sites (two mutation, two prune) use this
function so the path cannot drift.

## Out-of-turn / direct savers (resolved)

The three savers that previously wrote a session file without holding the turn
lease — and could therefore clobber a concurrent turn's whole-file write — now
acquire `session_turn_lease(<session_path>)` and **reload under the lease before
saving** (so they write fresh disk state, never a stale in-memory copy):

- **HTTP rename** (`durin/service/sessions.py`) — acquires the lease with a
  short `timeout=30.0`; on `TimeoutError` it raises `ConflictError` ("session
  is busy, rename not applied") with no partial write, rather than clobbering.
- **`process_direct`** (`durin/agent/loop.py`) — the cron / heartbeat / HTTP-API
  / SDK / CLI direct-message entrypoint. Wrapped in the lease (default 600s).
  On `TimeoutError` it returns the same `_SESSION_BUSY_NOTICE` that `_dispatch`
  publishes (so API/SDK/CLI callers get a meaningful busy message, not an empty
  response) and processes nothing — a cron/heartbeat fire simply retries next
  schedule; no user turn is half-written or duplicated.
- **Webui background title generation** (`durin/utils/webui_titles.py`, scheduled
  from `_schedule_background`) — the LLM title call runs **outside** any lease;
  only the subsequent reload + set-title + `save()` is taken under the lease, so
  the next turn is never blocked behind a multi-second model call.

The lock-ordering invariant is preserved: each saver takes `<key>.turn.lock`
(worker thread) then `save()`'s `<key>.jsonl.lock` (event loop) — the same order
as a normal turn.

## Other resolved residuals

- **`SecretStore` scope edits** (`durin/security/secrets.py`) — `grant`/`revoke`
  previously did an unlocked `load → set_scope → save`. The whole
  read-compute-write now runs inside `cross_process_lock(self._path)` via
  `grant_consumer_locked`/`revoke_consumer_locked` (and `set_scope_locked` for a
  pre-computed full scope), mirroring `put`/`remove`. The in-memory `set_scope`
  remains for non-persisting callers (documented as such).
- **`edit_file`** (`durin/agent/tools/filesystem.py`) — the read→edit→write
  sequence was a silent lost-update window. It now captures the read content
  hash and **re-hashes immediately before `atomic_write_bytes`**, aborting with a
  clear "file changed on disk — re-read and retry" error if the on-disk content
  changed in the window (optimistic CAS; no `.lock` siblings in the user
  workspace). `write_file` is an unconditional overwrite (documented
  last-writer-wins) and is intentionally unchanged.
- **Stream-delta coalescing** (`durin/channels/manager.py`) — consecutive
  same-`(channel, chat_id)` outbound deltas are now coalesced **only when their
  `_stream_id` matches**, so two concurrent streams sharing a chat (Telegram
  forum topics) no longer bleed text across edit bubbles.

## Residual ledger (accepted)

These are deliberately not fixed; each carries a written failure mode.

1. **No TTL on the held turn lease — not built.** *Failure mode:* an
   alive-but-stuck process holds `<key>.turn.lock` until it exits or crashes
   (the OS releases the flock on crash); meanwhile a new turn on that session
   gets a retriable "session busy in another window" message after the 600s
   acquire timeout — **not data loss**. *Why not a TTL/steal:* a steal-after-TTL
   table (hermes `compression_locks` style) would let a waiter seize the lease
   from a slow-but-alive holder and re-introduce the very clobber the lease
   prevents — a turn is not idempotent the way compression is. Revisit only if
   alive-but-stuck holds (not crashes) become common.

2. **`oauth/<provider>.json` token writes — external boundary.** Owned by the
   `oauth-cli-kit` `FileTokenStorage`; durin performs no read-modify-write of
   these files, so there is nothing durin-side to lock. *Failure mode:* two
   processes refreshing the same provider token concurrently → last-writer-wins,
   but both write the same refreshed token, so it is non-destructive and rare
   (refresh fires ~once per token lifetime). Not a durin residual.

3. **Sessions Phase-B append-only — measured defer.** After Phase B (mid-turn
   checkpoints → sidecar), a turn-end `save()` still does one whole-file `.jsonl`
   rewrite + one `.md` regen. *Failure mode:* none for correctness — purely
   write amplification of ~one bounded rewrite per turn (≤~2.5 MB ceiling).
   Finishing append-only would save ~2× on that single write (marginal) while
   touching core session truth (consolidation boundary + partial-append crash
   recovery). Risk > gain at current scale; revisit only if profiling shows the
   `.jsonl` rewrite is a measured turn-latency cost.

4. **Cron same-job schedule edit during the execution window.** If an external
   writer changes the *schedule* of the *same* job that is currently executing,
   the post-execution reload-merge re-applies that job's run-state delta —
   including a `next_run_at_ms` computed from the *old* schedule. *Failure mode:*
   the job fires once on the old cadence, then self-heals on the next execution
   (which recomputes `next_run` from the current schedule). Bounded,
   single-cycle, self-healing; edits to *other* jobs are unaffected.

5. **Benign / dormant audit items (verified non-issues).**
   - **#13 tool-results bucket** — stale-content reuse only on the `tool_{idx}`
     positional fallback (real providers emit unique ids); cleanup only removes
     already-stale peer buckets. Recoverable, low-probability.
   - **#14 `_subs` snapshot / #19 `ToolRegistry`** — the mutators have no `await`
     between read and mutate, so they are atomic under single-thread asyncio;
     only a transient snapshot-staleness across an `await` remains (a missed
     in-flight streaming frame / a one-turn incomplete MCP tool set), self-healing
     on the next frame / registry rebuild.
   - **#16 deletion tombstone** — the lockless `.deleted.json` RMW is real, but no
     live delete-issuing path exists (CLI `forget` and agent `memory_forget`
     refuse entity pages; the only tombstone writer is single-writer). *Latent
     trigger:* the instant a concurrent delete-issuing path is wired, serialize
     the RMW under flock and re-check `is_deleted` inside `write_entity`'s CAS.

(Audit items **#10**, **#12**, and **#18** are now fixed — see "Stream-delta
coalescing", "`edit_file`", and "Memory git-worktree lock" above.)

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

`SubagentManager._run_subagent` (`durin/agent/subagent.py`) applies the same
fix: it captures `self.runner.provider` immediately before building the
`AgentRunSpec` and passes it as `provider=`.  `SubagentManager.set_provider`
mutates `self.runner.provider` on each session's `/model` swap; without this
capture, a concurrent swap could change the provider mid-flight inside a
background subagent turn.

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
