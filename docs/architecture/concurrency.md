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
