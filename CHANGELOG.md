# Changelog

User-facing changes per release, newest first. Each release also ships these
notes as a [GitHub Release](https://github.com/mmarmol/durin/releases).
Entries are curated at release time from the merged pull requests since the
previous tag — highlights first, then changes grouped by area.

## Unreleased

### Highlights

- **A running workflow shows its work, live.** The chat's work strip and
  in-thread pill, the web UI's work panel, the Runs (executions) view, and
  the terminal UI all now name the node currently active, how long it has
  been running, which round of tool use it is on, and what it is doing right
  now (which tool, on what file or query) — the same picture on every
  surface. Once a workflow has completed runs to learn from, the executions
  view also shows each node's typical duration next to how long this pass
  actually took, which files a node produced, and how long a whole run of
  this workflow usually takes. (#428)
- **A run's progress is never lost.** Reload the page mid-run and the active
  node, its elapsed time, and its round are exactly where you left them. If
  the gateway itself restarts partway through a node, the rounds that node
  had already completed are preserved in its session instead of vanishing
  with the process. (#428)

## 0.3.4 — 2026-07-21

### Highlights

- **GLM stops answering the same question twice, and the provider path heals
  itself.** On a multi-step tool turn the assistant's own narration was being
  stripped from what the model saw on the next step, so models that narrate
  every step — GLM in particular — re-emitted the same acknowledgment over and
  over. Content now rides alongside tool calls the way the OpenAI standard
  intends. The same change hardens the OpenAI-compatible path: lone surrogate
  characters that reasoning models occasionally emit are scrubbed before they
  can crash the request, overloaded endpoints (Z.AI Coding Plan's "service
  temporarily overloaded") back off progressively instead of hammering, and a
  new reactive recovery strips a parameter an endpoint rejects and retries once
  — so a new model whose endpoint drops support self-heals without a
  hand-maintained list. (#422)
- **Skill authoring is now a governed boundary.** Authoring a skill goes through
  a draft → publish path with a registry write-lock and provenance instead of
  writing files straight into `skills/`. Agent-authored skills are attributed
  and no longer indistinguishable from an unverified external drop. (#419)
- **A calmer, more legible web dashboard.** The redundant top goal banner is
  gone; interactive tool blocks are toned to durin's palette; rich fenced-block
  previews (mermaid, vega-lite, sandboxed html/svg) get a real zoom inspector, a
  download, and hardened error handling instead of leaking a raw parse-error
  graphic; mermaid diagrams follow the durin theme; and the ask-user answer
  field auto-grows like the composer. (#411, #414, #415, #416, #417)
- **The agent can read its own changelog.** `CHANGELOG.md` now ships inside the
  installed package, and `durin changelog` prints the running version's entry
  (or `--all`, or a named version) so a running agent — or you — can check what
  changed. (#418)

### Changes

- **Providers** — assistant content is kept alongside `tool_calls`; lone UTF-16
  surrogates are scrubbed before the request is encoded; DeepSeek thinking-mode
  `reasoning_content` is padded with a space (V4 Pro rejects the empty string);
  overloaded endpoints use a wider retry backoff; a reactive strip-on-error
  recovery drops a rejected request parameter and retries once. (#422)
- **Skills** — governed authoring: draft → publish, registry lock, provenance,
  attributed agent-authored backstop. (#419)
- **CLI** — `durin changelog [--all | <version>]`, with `CHANGELOG.md` bundled
  in the installed package. (#418)
- **Web UI** — removed the top goal banner (#411); calmer interactive tool
  blocks (#414); rich-preview zoom / download / error hardening (#415);
  auto-growing ask-user field (#416); theme-aware mermaid diagrams (#417).
- **Tools** — PDFs are read via `pypdf` rather than the undeclared `pymupdf`.
  (#412)
- **Model data** — weekly automated refresh of the vendored model catalog
  (community sources + NVIDIA id ground truth). (#413)
- **Project** — a structured bug-report issue form that blocks blank issues
  (#420); dropped a dead docs pointer from the release workflow (#421).

## 0.3.3 — 2026-07-19

### Highlights

- **Background workers can finally see, read, and remember.** Sub-agents and
  workflow work nodes now get the image/audio interpretation bridges (when
  aux models are configured), document→markdown conversion, memory writes
  (entity upsert, document ingest, lineage reads), notebook editing, and a
  bounded `sleep` for polling external jobs. All of these were main-agent-only
  — mostly by omission, not decision. What stays out is now an explicit,
  commented policy: no user asking, no channel sends, no nested spawn or
  workflow runs, no cron/loop creation, no destructive memory ops, no skill
  self-modification. (#408)
- **The tool surface is now legible — to you and to the agent.** The `spawn`
  tool tells the delegating agent exactly what a sub-agent can and cannot do;
  the workflows skill spells out what `tools: "default"` contains; the
  workflow editor's mode dropdown lists your custom modes (previously
  hardcoded — custom modes, the mechanism for per-node tool allowlists, were
  unselectable); and the modes editor badges which tools are
  background-capable, so you can see which allowlist entries will actually
  reach a node or sub-agent. (#408, #409)
- **Workflow saves now warn about silent tool-surface surprises.** Saving a
  workflow (from the agent tools or the web editor) returns advisory warnings
  for a node `mode` that isn't a registered mode — at run time a typo silently
  falls back to `build`, i.e. FULL access — and for mode-allowlist entries
  that can never load in a work node. The save still succeeds; the surprise
  doesn't wait until run time. (#409)

### Changes

- **Agent core:** read-only additions (bridges, document conversion, entity
  and lineage reads, `sleep`) join the `plan`/`explore`/`read` mode
  allowlists, so a read-only verify node can inspect a screenshot without
  gaining write access; `sleep`'s "allowed in every mode" contract is now
  actually true. Aux bridge construction moved to a shared module
  (`durin/agent/aux_bridges.py`) — handles rebuild per spawn (hot-reload
  friendly) and are cached per workflow run.
- **API:** `GET /api/v1/tools` entries carry `background`; workflow save
  responses carry `warnings` (contract + typed client regenerated).
- **WebUI:** mode badges and save-warning notices localized across all nine
  languages.

## 0.3.2 — 2026-07-19

### Highlights

- **One embedding model for the whole system.** durin now runs a standing
  embedding service — a gateway-supervised loopback server holding a single
  warm model copy that every process shares (gateway, dream worker, TUI).
  Before, each process loaded its own ~0.5-1GB copy, and two coexisted during
  every dream. The service caches results by content hash, so re-indexing
  unchanged text costs zero compute — a big win on small servers. If the
  service isn't reachable, embedding quietly falls back to the previous
  per-process behavior: nothing ever stops working. (#406)
- **Voice engines no longer sit in memory unused.** Startup now only verifies
  the speech models are downloaded (~1.2GB of STT+TTS engines used to load
  into every gateway at boot — including headless servers that never speak).
  The engines load on first use and unload after 15 minutes idle
  (configurable; `0` keeps them resident for latency-sensitive setups). (#406)

### Changes

- **Memory:** `memory.embedding.isolation` gains `"service"` (the new
  default) with knobs `service_port` and `service_max_rss_mb`; the gateway
  supervises the server (respawn with backoff, RSS-cap restart, clean
  teardown). New telemetry: `memory.embedding.service_fallback`.
- **Voice:** new knobs `tts.idle_unload_s` and `transcription.idle_unload_s`
  (default 900); first-install model downloads are verified at boot and
  recorded under `~/.durin/voice-verified/`.

## 0.3.1 — 2026-07-19

### Highlights

- **The memory dream can no longer take down the host.** A production incident
  traced a full-box freeze to the dream's discovery pass feeding an entire
  session transcript into full-text search as a "query" — ~800MB of allocations
  per call, per session. Search queries are now hard-bounded at the router (any
  caller, any size), discovery passes a compact recent window, and the fatal
  input now costs ~3MB. (#402)
- **Runaway dreams die alone, not with the machine.** The dream worker runs in
  its own process group under an RSS watchdog that terminates the whole tree
  above a configurable cap, and reactive dreams skip spawning while system
  memory is tight — a killed or skipped dream simply retries on the next
  trigger. Every pass now reports its memory footprint, and the worker keeps
  its own rotating log so a long run is auditable instead of a black box. (#402)
- **No more ghost "running" workflows.** Run manifests record which process
  owns them; at boot and every few minutes, runs whose owner died are marked
  crashed immediately — no more six-hour grace during which the UI showed a
  live timer for a run killed by a restart. Poking a ghost with `tasks
  status/stop` repairs it on the spot with an honest answer. (#402)
- **Work strip above the composer:** background work (sub-agents and workflow
  runs) is visible at a glance while you chat, with live per-node progress.
  (#401)

### Changes

- **Memory:** vector-index maintenance in the nightly dream — the LanceDB
  table is compacted verify-or-rollback (one production table had accreted
  2,929 versions; maintenance shrank it 294MB → 2.6MB with search verified
  intact, rebuilding from current rows when the underlying library corrupts
  the vector read path). A search hit pointing at a missing entity page no
  longer aborts the pass; losing embedding-pool isolation now emits telemetry.
- **Workflows/loops:** ownership-based crash reconciliation (see highlights)
  applies to loop runs too, so a `single`-concurrency loop can't stay jammed
  behind a stale manifest.
- **Observability:** the gateway emits a periodic `gateway.memory` footprint
  event and serves `GET /api/v1/diagnostics/memory` on demand (RSS, child
  processes, threads, gc, host headroom); telemetry events emitted from
  background threads are no longer silently dropped.
- **Config:** new knobs `memory.dream.max_rss_mb` (worker-tree RSS cap;
  0 = automatic) and `memory.dream.min_available_mb` (reactive-dream
  memory floor; 0 = disabled).

## 0.3.0 — 2026-07-18

### Highlights

- **Script nodes can authenticate:** a workflow script node declares the stored
  secrets it needs (`"secrets": ["ZENDESK_API_TOKEN"]`) and they arrive as
  environment variables — so an authenticated `curl` stays a zero-token,
  instant script step instead of becoming a full agent turn. Injection requires
  the secret's `exec` scope grant, unresolvable names abort the run pre-flight
  naming the node, and script output is redacted against the secret store so a
  leaked credential can never persist into sessions or run records. (#399)
- **Workflows declare what they produce:** the output descriptor accepts an
  `artifacts` list — the files the run promises to leave in its working folder.
  Every node sees the contract while working, and promised files missing after
  completion are reported as a warning in the result, the manifest, and
  `tasks(status)`, so a composed pipeline learns the gap immediately instead of
  failing confusingly downstream. (#399)
- **No more sleep+status babysitting:** background workflow results were always
  push-delivered as a follow-up message, but the tool guidance taught the agent
  to poll with sleep+status loops — blocking the chat for minutes. The guidance
  now teaches the real contract (report the run, end the turn, the follow-up
  wakes you), and a deterministic backstop makes `sleep` remind the agent about
  running push-delivered work at wake time, correcting a polling loop on its
  first iteration. (#399)
- **Mid-run visibility for workflow runs:** the run manifest records the shared
  working folder from the first write plus per-node durations, and
  `tasks(status)` renders the folder path, each node's latest-pass duration,
  and a listing of the folder's current files — a live window onto a run's
  artifacts while it executes. (#399)

### Changes

- **Workflows:** script-node `secrets` field in the visual editor; declared
  artifacts editable on the Output canvas object (one `path | description` per
  line); secret-resolution errors point the agent at the `workflows` skill;
  `run_workflow`'s description now names multi-way `cases` routing and the
  `__needs_input__` terminal. (#399)
- **Skills:** the `workflows` skill teaches the background waiting contract,
  script-node secrets, and the declared-artifacts contract across its overview,
  authoring schema, and patterns. (#399)
- **Web UI:** scalable type-filter popover for the memory Entities toolbar.
  (#398)
- **CLI:** `durin status` counts entities, Library documents, and fragments
  separately. (#397)

## 0.2.0 — 2026-07-18

First stable release. Highlights: the memory Entities view family (graph,
cards, table) with Obsidian-style gestures and camera controls, MCP OAuth
tokens surviving gateway restarts, and session-entity graph edges drawn from
page provenance. Full pull-request list:
[v0.2.0 release notes](https://github.com/mmarmol/durin/releases/tag/v0.2.0).
