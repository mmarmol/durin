# Changelog

User-facing changes per release, newest first. Each release also ships these
notes as a [GitHub Release](https://github.com/mmarmol/durin/releases).
Entries are curated at release time from the merged pull requests since the
previous tag — highlights first, then changes grouped by area.

## 0.9.1 — 2026-08-26

### Highlights

- **The loops migration no longer gives a standing pipeline an off switch.**
  A migrated multi-case loop — one with a `correlate` pattern and/or parallel
  concurrency, serving many tickets/cases at once — was getting a life
  condition with `achieved_when: "any_completed"`, which disables the whole
  automation after its first completed run: the pipeline would have switched
  itself off, silently, the first time it succeeded. Found by running a real
  ticket through a freshly migrated pipeline. Multi-case loops now migrate
  with no life at all (plus a warning naming the reason); single-case loops —
  the shape a life actually models — keep the mapping. If you migrated before
  this release and your automation carries a `correlate` trigger or parallel
  concurrency, remove its `life` block. (#563)

## 0.9.0 — 2026-08-26

### Highlights

- **Automations replace Loops.** Standing work got rebuilt from the ground up:
  an **automation** binds triggers — a schedule, a matching channel message, a
  webhook, or another automation finishing — to a workflow, then routes the
  outcome (delivery channel + how chatty to be) separately from its help lane
  (where approvals and questions land). An automation can carry a **life
  condition** — *"until invoice 4471 is paid"* — count its attempts, escalate
  when stuck, and switch its own triggers off the day the goal is reached.
  Existing loop definitions, runs, and queues **migrate automatically on first
  start**; the loops API, tool, and UI are gone. (#554, #555, #556, #557)

- **Workflows can pause for a real approval.** A node marked `approval: true`
  parks the run with a typed proposal; "approve", "reject", or free text
  ("make it shorter") resolve it — from the dashboard's inbox, chat, or a
  channel reply, with bilingual keywords. Send-with-receipt makes channel
  replies land back in the right thread (Slack status takeover, email
  thread-root digests). (#551)

- **You can see it, and you can stop it.** The new Automations section shows
  every definition, the live run with per-node progress, and a full history
  with each run's cause, delivery record, and approval verdict. A running run
  takes a graceful **Stop** (the current step finishes; a second click force
  stops); an operator stop is deliberately invisible — it never notifies, never
  chains, and never counts against the life condition. Answering a paused run
  no longer blocks until the workflow finishes — it returns immediately and
  resumes in the background. (#557, #558)

### Changes

**Automations**

- Trigger bindings: schedule / channel (filters + optional semantic match +
  `correlate` patterns) / webhook (`POST /api/v1/hooks/{hook}`) / chain, OR-ed
  per automation; chain cycles are rejected at save. (#554)
- Delivery vs help routing with `notify: always | failures_only | when_notable
  | never` (+ `silent_labels`); counterpart-tagged questions answer in the
  originating thread; a run fired from chat always reports back to that chat.
  (#554)
- Life conditions: `achieved_when` (`any_completed` or `label:<L>`),
  `max_attempts`, `on_stuck` (`notify` / `escalate_pause`); achieved
  automations disable themselves and remove their cron triggers. (#554, #556)
- HTTP stop for runs (graceful/`hard`), with the stop intent stamped on the
  record so a restart never relaunches an operator-stopped run; cancelled runs
  finalize `interrupted` — no delivery under any policy, no chains,
  streak-transparent. (#557, #559)
- Non-blocking answer on both the API and the `automations` chat tool; a
  failed background resume finalizes the run instead of stranding it. (#557)
- Boot migration converts loop definitions/runs/claims/queues; `automation:*`
  cron jobs are protected from toggle/run/remove; queued busy events keep
  their webhook cause. (#554, #557)
- The `cron` and `automations` tools now cross-reference each other, and
  `cron list` labels automation-owned rows — the agent picks the right tool
  for "what automations do I have?". (#557)

**Workflows**

- Approval pause (`approval: true` + `ask_kind`), terminal route labels
  (`final_route_label`), send receipts per channel, and service-path progress
  frames on the `runs:feed` websocket. (#551)

**WebUI**

- Automations section: definitions list with a "needs you" tray, five-group
  editor (triggers incl. webhook URL + secret reveal, delivery, help, life),
  detail view with live run card, stop/force-stop, one-click pause/resume,
  run history with cause/delivery/approval records, and drill-in to the
  workflow run. Inbox actions: approve / revise / reject. (#555, #557)
- The whole section ships in all nine languages; the legacy `loops` config
  section is hidden from the settings editor; the detail view stays live while
  runs are in flight. (#557, #558)
- Cron settings show automation triggers as read-only rows with an "open
  automation" link. (#555)

**Docs**

- The README's automations section carries its own mockup again, replacing the
  retired loops one; guide and internals docs follow the new subsystem. (#560)

## 0.8.2 — 2026-08-22

### Highlights

- **"What did this run cost?" is now one tool call.** `workflow_runs` gained a
  `cost` action: the per-run token table — every node with visits collapsed,
  subworkflow children included, reused nodes counted at zero — computed from
  the run's own telemetry. An empty result says so explicitly instead of
  rendering zeros. (#548)

- **A workflow node's model choice now behaves like the chat's.** Node and
  persona model references resolve through the same machinery as `/model`: a
  `"provider model"` pair works (it used to be passed raw and would have broken
  the call), a plain model name picks up its per-model config parameters —
  which now actually reach the node's calls — and personas can pin a
  `temperature` ("the reviewer runs cold"). Resolution is cached per run,
  falls back safely on a bad reference, and everything a node resolves enters
  its reuse identity, so changed parameters never serve stale work. Along the
  way: `/model` itself had silently dropped `top_p`/`top_k`/`repeat_penalty`
  from per-model config since forever — fixed for every caller. (#549)

### Changes

**Tools**

- `workflow_runs`: `cost` action (anchored file matching, midnight-spanning
  runs covered); `search` filters by date (`since`/`until`, end-of-day
  inclusive); correct TUI startup category. (#547, #548)
- `run_workflow` rejects `resume_run_id` + `work_key` together — a resumed run
  keeps the key it was started with. (#547)

**Workflows & config**

- Node/persona model refs resolve via presets; per-model generation params
  (temperature, sampling) apply to node calls; `PersonaConfig.temperature`
  (webui persona editor included); `repeat_penalty` joins the reuse params
  identity (existing artifacts re-run once and re-stamp). (#549)

## 0.8.1 — 2026-08-21

### Highlights

- **Identical work is no longer repeated.** Every artifact a workflow writes now
  carries its producer's identity — which node definition, which model, which
  configuration, fed by which input — and a node marked `reuse: "if-unchanged"`
  skips itself when all of it matches, serving the recorded artifact instead of
  re-paying the tokens. A new `work_key` gives runs a stable working folder to
  share (loops pass their ticket-correlation key automatically), runs sharing a
  key serialize instead of racing, a paused run resumes into its own folder, and
  six signals — definition, model, provider, params, input, content — guard
  against ever serving stale work. Measured motivation: a re-run of an
  already-solved support ticket burned 48 minutes and 1.45M tokens rebuilding
  artifacts that were sitting on disk. (#538, #539, #544, #545)

- **The engine stopped defeating its own prompt cache.** The forced verdict
  calls (deliver / route / re-entry) used to swap the tool list on the last
  call of every node, invalidating the provider cache and re-paying the whole
  conversation — ~14% of a pipeline run's fresh tokens. Verdict tools now ride
  the node's tool list from the first turn, a valid early delivery or route
  verdict is accepted on the spot, and the redundant end-of-turn call is
  skipped. (#540, #543)

- **The API stopped flying blind.** `/v1/chat/completions` reports the tokens a
  turn really consumed (streaming included), workflows launch directly via
  `POST /api/v1/workflows/{name}/runs` without paying an agent turn, launched
  runs can actually be cancelled — the stop flag now clears strictly before the
  terminal record is written — and an invalid `work_key` is rejected at the
  door instead of producing a ghost run. (#541, #543, #545)

- **The agent consults past executions instead of re-running them.** A new
  read-only `workflow_runs` tool searches and inspects prior runs — with each
  run's model and workflow version visible so the agent can judge whether old
  work is still trustworthy, and a standing instruction to say when an answer
  is built on prior work and how old it is. (#542)

### Changes

**Workflows**

- Artifact provenance: `work/.provenance.json` + producer fields on run
  manifests (`spec_hash`, `durin_version`, per-node `model`/`provider`/
  `node_hash`); legacy manifests unaffected, verified against production data.
  (#538)
- `reuse: "if-unchanged"`, validated fail-loud: incompatible combinations
  (`detached`, parallel branches, `context: "shared"`, `branches_from` without
  a declared pool) are parse-time errors with reasons. (#539, #543, #545)
- `work_key`: stable, collision-proof working folders; same-key serialization
  via cross-process lock; keyed folders idle for 30 days are swept at most
  daily, sparing parked runs. (#544, #545)
- The declared-artifacts contract evaluates what THIS run produced — leftovers
  in a shared folder neither appear as outputs nor satisfy the contract. (#545)
- Reused nodes are excluded from typical-duration estimates; run records carry
  `origin_run_id`. (#539)

**Providers & telemetry**

- `FallbackProvider` reports the real provider name in `provider.call` events
  and reuse identity. (#543)

**API**

- Real usage on `/v1` (sums over the turn's calls; final SSE chunk). Direct
  workflow launch endpoint (202 + run_id, `workflows:write`). Cancellation
  wired for service-launched runs. `work_key` validated at every edge. (#541,
  #543, #545)

**Tools**

- `workflow_runs` (search/show over past runs, plan-mode allowed); `run_workflow`
  gains `work_key`. (#542, #544)

## 0.8.0 — 2026-08-20

### Highlights

- **"Who spent the tokens?" is now answerable from telemetry alone.** Every LLM
  round-trip — the chat loop, workflow nodes, judges, memory and dream work, the
  vision and audio helpers — logs one `provider.call` event naming the provider,
  the model, the prompt/cached/completion token counts, the wall-clock duration
  and how the call ended. Workflow nodes were previously invisible: a run could
  burn hours of model time without leaving a single telemetry line — each node
  now writes its own telemetry file, tool calls and LLM round-trips included.
  Born from a real audit day where the provider's dashboard showed hundreds of
  millions of tokens and durin's telemetry could only account for a fraction of
  its own share. (#534)

- **Structured output stops failing with the wrong story.** A workflow node that
  had already done its work could still abort with `'ticket_id' is a required
  property` — when the truth was that the delivery payload got cut at the
  output-token limit and repaired into a partial object. The delivery call now
  runs with the model's real output budget (it was silently capped at a
  4096-token default), a truncation is named as a truncation and the retry is
  steered toward a more concise payload, and a provider failure aborts
  immediately with the provider's message instead of burning retries on a dead
  call. (#536)

### Changes

**Telemetry**

- New `provider.call` event emitted at the provider layer for every completed
  call through the retry wrappers, and from workflow nodes' direct calls and the
  `interpret_image`/`interpret_audio` helpers; the factory stamps each provider's
  config-registry name so events attribute to `zai_coding_plan`, not a class
  name. (#534)
- Workflow nodes bind their session logger for the whole node execution, so
  `workflow_<run>_<node>` telemetry files exist at all. (#534)

**Workflows**

- The forced-tool calls (route verdicts, re-entry assessments, structured-output
  delivery) ride `chat_with_retry`: transient transport failures retry, output
  budget resolves to the model's configured cap, and long generations keep the
  streaming transport alive. (#536)
- The structured-output delivery loop reads `finish_reason`: truncation and
  provider errors are reported as what they are, and a complete valid payload
  flagged at the limit is still accepted. (#536)

**Data**

- Weekly vendored model catalog and MCP floor refreshes. (#531, #532)

## 0.7.0 — 2026-08-15

### Highlights

- **Another agent can now talk to durin.** The gateway answers at
  `/v1/chat/completions` in the shape every OpenAI-compatible client already
  speaks, so anything that can point at a base URL — a coding agent, a script,
  another assistant — can hold a conversation with durin instead of just reading
  its API. It streams, it accepts file uploads, and it is closed by default:
  reaching it takes a token scoped to chat, issued by you. The old `durin serve`
  command is gone; the gateway is the one server. (#526)

- **Scanned documents work end to end now, not just in the happy case.** 0.6.0
  taught durin to read a scan; this release makes it survive contact with real
  documents. A book attached in chat used to vanish silently — the instruction to
  ingest it was composed and then discarded — and the gateway froze for as long as
  a conversion took. Both are fixed, along with the memory the OCR engine used to
  leave behind in the gateway forever: transcription now runs in a short-lived
  process that takes its ~1.4 GB with it when it exits. (#525)

- **A scan that cannot be read says so, and one you already added is not read
  twice.** A document whose every page comes back blank used to be published as an
  empty, unfindable Library entry; it now fails with a reason, and distinguishes
  blank paper from printed text this engine cannot decipher. Re-adding a document
  you already have returns what you had — instantly, no second transcription, no
  duplicate job — and a book you added while OCR was switched off upgrades itself
  the moment you turn OCR on. (#527)

- **A background transcription that failed can be retried, and picks up where it
  stopped.** Nothing could restart a failed job before: the work was simply lost.
  `tasks(action="retry")` revives it and resumes from the pages already
  transcribed — a book that failed at page four of forty re-does thirty-seven
  pages, not forty. Every failure message now names that recovery. (#527)

- **Nine more scripts, downloaded only if you choose one.** Cyrillic, Arabic,
  Greek, Korean, Devanagari, Thai, Tamil, Telugu and East-Slavic recognition are
  selectable under Settings → Documents. The built-in pack still covers Chinese,
  Japanese and Latin-script languages fully offline; picking another downloads its
  model once, on first use, into `~/.durin/models/ocr`, and every place that asks
  you to choose says what that costs. Your documents never leave the machine
  either way. (#527)

- **Stopping a workflow can now actually stop it.** A stop only ever took effect
  between steps, so an agent step in the middle of a model turn ran to the end no
  matter what you pressed. `force=true` — or simply pressing stop a second time —
  interrupts the turn itself, including inside nested sub-workflows, and a run
  that is winding down shows as `stopping` instead of pretending to be healthy.
  What a tool call already started still finishes on its own, and the interface
  says so rather than promising more. (#529)

### Changes

**API**

- New OpenAI-compatible surface on the gateway: `/v1/chat/completions` (streaming
  and multipart uploads), `/v1/models`, gated by the new `chat:write` scope and a
  configurable request timeout. Removed the separate `durin serve` command and its
  module, and the `[I] API Server` entry in the onboarding wizard. (#526)

**Documents**

- Attaching a scanned document in chat reaches the model with the filename, the
  path and what to do with it, on every channel; unreadable attachments are
  reported rather than dropped. (#525)
- Conversion no longer runs on the gateway's event loop, and deciding that a book
  belongs in a background job costs a probe instead of a full conversion —
  measured 18.8 s → 0.05 s on a 400-page scan. (#525)
- Background OCR is capped at one worker atomically, chains to the next queued
  book, and self-heals a worker killed outright within a minute. Waiting books are
  shown as `queued` rather than as running with a live clock. (#525)
- Per-page recognition scores are recorded and logged. They are deliberately not
  used to accept or reject a page: measured, the scores of confident nonsense
  overlap those of correct text from a noisy scan, and the docs say so. (#527)
- The transcribed text of a finished book no longer lives in the job database
  forever: it is deleted once the book is in your Library, while a failed job
  keeps its pages so a retry can resume, and terminal jobs are pruned after
  thirty days. (#527)
- The configuration guide documents the `documents` section; the install guide
  notes that slim Linux images need `libgl1` for the OCR extra. (#527)

**Workflows**

- Two-mode cancellation (graceful and hard), `stopping` as a visible state in the
  work panel, the composer strip and the `tasks` tool, and a force-stop that
  reaches nested runs, parallel branches, fan-out workers and detached launches.
  (#529)
- A run started after you changed the default model now uses the model you saved,
  instead of the one that was current when the gateway booted. (#504)

**Fixes**

- The extras auto-installer respects `install.auto_install_extras: false`. Three
  call sites had no configuration in hand and installed anyway, silently
  overriding the setting. Its refusal now also names the install commands durin
  actually documents, rather than a bare `pip install`. (#527)
- Liveness probing on Windows no longer risks killing the process it is checking:
  `os.kill(pid, 0)` is a console-control event there, and a known CPython
  fall-through turns it into a terminate. Both copies of the probe are now one
  Windows-safe implementation. (#527)

**Dependencies**

- Bump cryptography 49.0.0 → 50.0.0. (#513)
- Bump the python group: three updates across one directory. (#516)

## 0.6.0 — 2026-08-14

### Highlights

- **durin can read scanned documents.** A PDF with no text layer — a book someone
  scanned, a contract that came back from a photocopier, an archived file — used to
  extract to nothing and fail. durin now measures how much of a document it can
  actually read, page by page, and transcribes the pages it cannot with an OCR
  engine that runs on your machine. Nothing is uploaded and nothing is sent to a
  model in the cloud: the engine ships inside the package and reads the pixels
  locally. It is off until you turn it on, under **Settings → Documents**, which
  also tells you what it costs before you do. (#523)

- **A book no longer blocks the conversation.** Transcribing four hundred pages is
  minutes of work, and durin used to have nowhere to put work like that — a
  document was converted while you waited. Anything longer than a few pages now
  becomes a background job: the document is stored immediately, the transcription
  runs in its own process, and you watch it advance page by page in the work panel
  or by asking durin how it is going. When it finishes, the book is a searchable
  document in your Library like any other. (#523)

- **That work survives things going wrong.** Each page is saved the moment it is
  transcribed, so a job interrupted at page three hundred and eighty resumes at
  three hundred and eighty rather than starting over — and restarting durin picks
  up jobs that were running when it stopped instead of losing them. A document
  that yields nothing, an engine that is missing, a page that fails: each one now
  ends with a reason you can read rather than a job that quietly never finishes.
  (#523)

- **Partially scanned documents are handled as what they are.** A report with three
  photocopied inserts is not a scan, and a scan is not a document with gaps. durin
  tells them apart: it transcribes just the inserts, keeps the text it could
  already read, and when something cannot be read it says which pages are missing
  and what came before them, instead of handing the model a document with silent
  holes in it. (#523)

### Changes

**Documents**

- New `documents` configuration section: the OCR switch and page budget, plus two
  limits that used to be fixed in code — the largest attachment durin will read and
  how much of a document it inlines. An oversized attachment is now reported in the
  conversation instead of being dropped without a word. (#523)
- Local OCR ships as an optional `[ocr]` extra, installed on demand when you enable
  the setting. (#523)

**Jobs**

- New background-job registry, generic over the kind of work, with OCR as its first
  user. Jobs appear alongside sub-agents and workflow runs in the work panel and in
  the agent's `tasks` tool, and can be stopped from there. (#523)

**Dependencies**

- Bump pypdf 6.14.2 → 6.15.0. (#521)
- Bump h2 4.3.0 → 4.4.1. (#520)

## 0.5.5 — 2026-08-06

### Highlights

- **Every step of a workflow run can now be opened.** A run told you *that* a
  step ran, how long it took and whether it passed — never what it actually
  did. The only thread back to the work was a session key printed as text, to
  be looked up by hand on disk. Clicking a step now opens a panel: an agent
  step shows its whole conversation, every tool call with its arguments **and**
  its result, and the model's reasoning where the provider returned it, drawn
  with the same blocks the chat uses. A script step shows the command it ran,
  its exit code and both streams. A step that keeps no record of its own — a
  sub-workflow, a parallel group — says so and points at the runs or branches
  that do. The panel opens from the executions screen and from the chat's work
  strip, so watching a run does not mean leaving the conversation. (#519)

- **A step still working shows its work as it happens.** The panel does not
  wait for the step to finish: a step's conversation is saved after every
  round, so the transcript fills in round by round while you watch, and the
  same panel simply stops moving once the step ends — it is the run's record
  afterwards, not a second screen. Opened from the chat it also names what the
  step is doing right now, down to the tool and the file or command it is
  working on. (#519)

- **A script step that worked used to leave nothing behind.** Its output only
  travelled onward to the next step and its errors were discarded, so a script
  that behaved was unreadable after the fact and a broken one left a stub. Runs
  now record what each script ran and everything it printed — including when it
  is killed by a timeout or a cancellation, where a hung script has usually
  already printed the very lines that explain where it got stuck. Stored
  secrets are stripped from all of it, the command line included. How much is
  kept is yours to set (`workflow.script_log_max_chars`). (#519)

## 0.5.4 — 2026-08-05

### Highlights

- **A mistyped `${secret:NAME}` no longer ships as the credential.** Only the
  exact form is a reference, and anything that was not one came back untouched
  — right for a real literal, but it meant a near miss like `{{secret:X}}`,
  `$secret:X` or a lowercase name was handed downstream as if it were the
  secret itself. A Sentry MCP server was spawned with the literal string
  `{{secret:SENTRY_AUTH_TOKEN}}` as its token and failed inside the server with
  an opaque authentication error, nothing pointing back at the config; the
  hunt that followed went through wrapper scripts and process arguments and
  never reached the brace that caused it. A dangling reference already failed
  loudly, and a mistyped one is no more usable, so it now fails the same way —
  naming the correct spelling, and for an MCP server naming the exact `env` or
  header key. The same error covers a reference embedded in a longer string
  (`Bearer ${secret:TOKEN}`), which durin never interpolated. (#517)

## 0.5.3 — 2026-08-04

### Highlights

- **Slack shows what it is doing instead of going quiet.** The reaction emoji
  is set once when a message arrives and the text stream only begins when the
  model finally writes prose, so a turn spending minutes on tool calls — an
  ordinary ticket investigation runs eight or more in a row — was
  indistinguishable from a bot that had crashed. Reasoning would have covered
  the gap, but Slack was the channel that never implemented it, so it was
  discarded even with `showReasoning` on. There is now a status line that says
  what is happening, and it is a *single* message per turn: it is edited in
  place as the work moves and is then taken over by the answer itself, rather
  than leaving one post per tool call in a channel people read. The reply never
  gets painted over by a late update, and a status line is never left stranded
  above the answer it announced. Turn it on per channel with
  `channels.slack.sendToolHints` (still off by default, since a hint stream is
  unwanted on some surfaces). (#514)

### Channels

- A streamed Slack reply resolves its destination the way a non-streamed one
  already did. `send` turned a `#channel` name, an `@handle` or a user id into
  a real conversation and `send_delta` did not, so a stream aimed at anything
  but a concrete conversation id would have posted nowhere. Nothing was losing
  messages over it — a streamed reply answers an inbound event, whose id is
  already concrete — but the asymmetry was one caller away from doing so. (#514)

## 0.5.2 — 2026-08-04

### Highlights

- **A long answer in Slack no longer disappears.** Slack caps an edited
  message at 4 000 characters — an order of magnitude below the 40 000 a new
  message may carry — and durin sized every edit with the larger number. Any
  streamed reply past 4 000 therefore failed every edit it attempted, and
  failed the same way three times, at which point the reply was dropped: a
  streamed turn suppresses the complete copy that would otherwise have been
  the fallback, so what stayed in the thread was whichever half-written
  preview happened to fit. A reply that outgrows an edit now rolls into a
  fresh message and keeps going, and the segments left behind are converted
  to Slack's formatting as they are frozen rather than stranded in raw
  Markdown. (#507)
- **And it survives Slack moving that limit.** The 4 000 is a claim about
  someone else's API, which is exactly what went stale to cause the above. So
  it is now a starting point, not a fact: a payload rejected for size halves
  the working budget, warns, and is delivered at the smaller size instead of
  being dropped. Any other Slack error still surfaces untouched — a revoked
  token is not mistaken for a long message. (#508)
- **A chat is labelled by what was last said in it.** The list sorts and dates
  a row by its most recent activity but was labelling it with the *first*
  message in the conversation. On a key that lives for weeks — a Slack channel
  accumulates indefinitely — those are different conversations, so a thread
  active this morning sat under "Today" wearing a greeting from a week ago.
  As a side effect the list no longer reads message bodies at all, on an
  endpoint the sidebar re-hits whenever any session changes. (#509)
- **The chat list stops hiding every channel conversation.** The sidebar's
  channel filter defaulted to web-only and reset on every page load, so Slack,
  Telegram and Discord chats vanished each time the page was refreshed, with
  nothing on screen indicating the list was filtered. The choice is now
  remembered. (#509)

### Models

- A reasoning model no longer fails the connection test for thinking. The
  dashboard's "Probar" button and `durin doctor --ping-model` send a
  deliberately tiny output budget, and a reasoning model can spend all of it
  before reaching visible text — so a model that worked perfectly well once
  saved was reported as returning an empty response. The check now passes on
  content, tool calls *or* reasoning. It also stopped reading a failure as
  success: providers return API errors as ordinary response text rather than
  raising, so a stalled stream used to ping green. (#502)

### API

- New `POST /api/v1/channels/post` (scope `channels:write`): post through a
  running channel *and* record it in the session that conversation belongs to.
  Workflow script nodes run as subprocesses and cannot reach the channel
  layer, so automation posts straight to the platform and the conversation
  ends up existing only there — an automated investigation nobody replied to
  left no chat at all, and a human answering it started from a blank session
  rather than continuing the work already posted. It records under the key the
  channel itself derives, so a later reply continues that same session. Where
  a loop's internal *status* goes is unchanged: it is still never reported to
  the external party a channel origin identifies. (#510)

## 0.5.1 — 2026-07-28

### Highlights

- **Restarting the box no longer counted as a loop failing.** 0.5.0 taught a
  loop run killed with its process to say `interrupted` and relaunch what never
  started — but only when the process died abruptly. A graceful
  `systemctl restart` cancelled the run instead, and it was recorded `error`
  with no reason: it counted toward the stuck-loop streak, so a loop could
  escalate because the machine was restarted rather than because it kept
  failing, and nothing was relaunched, so whatever triggered it went unserved.
  A run cut short by the gateway going down is now left alone for the next
  start's sweep, which finalizes it with a reason, reports it, and relaunches
  it when no work had started. A deliberate stop is still an error. (#497)
- **A `${secret:NAME}` reference is resolved everywhere a credential is read.**
  0.4.9 and 0.5.0 fixed this one site at a time; this release closes the sweep.
  `durin status` presented the reference string instead of the secret when
  probing a live gateway, and the cloud speech-to-text and text-to-speech
  services built their providers straight from the raw config value — so a
  credential kept out of the config file, the documented way, silently failed
  to authenticate. (#495, #499)

### Secrets

- The config write guard also rejects the dashboard's own mask, not just the
  tool-result redaction marker. Every web surface that displays a credential
  was audited for whether it could round-trip a mask back into storage. (#498)

### Memory

- A canonical entity page is one result again. The three retrieval arms
  disagreed on how to address it, so a single page arrived as two documents —
  taking two of the caller's result slots and splitting its score between
  them. (#496)

## 0.5.0 — 2026-07-28

### Highlights

- **A loop run that died with the gateway told nobody.** Two chat-fired runs
  were killed by gateway restarts; both manifests were rewritten to `error`
  with no `finished_at`, no reason, no log line and no notification. The agent
  that fired them sat waiting on a turn it could no longer influence, and the
  operator never learned either. Every loop run that ends now reports an
  outcome, and it goes back to whoever asked for the run — the conversation
  that fired it — falling back to the loop's `operator_channel` when nothing
  fired it interactively. An outcome that reaches nobody logs a warning
  instead of vanishing. (#493)
- **`interrupted` replaces the lie that a killed run had failed.** A run killed
  with its process did not fail — it never produced a result, so calling it
  `error` claimed work was tried and misread the goal-streak counter that
  decides when a loop is stuck. Interrupted runs are now their own terminal
  state, transparent to that streak and excluded from the convergence and
  escalation rates. When no workflow had started, the loop fires a replacement
  and names it; when work was already in flight it stops and reports the
  workflow run id, because steps may already have posted somewhere external.
  (#493)
- **Firing a loop from chat no longer holds the turn hostage.** `loops` with
  `action="fire"` ran the whole workflow inside the tool call, so the agent
  could not answer, and a restart mid-run killed the awaited call with nothing
  left behind. It now returns the run id immediately and the outcome arrives
  as a follow-up. (#493)
- **A rotated secret kept its scope, but durin said otherwise.** An agent was
  told a freshly rotated credential had `scope=none` and no longer announced it
  as an available environment variable, because the confirmation was built from
  the write request instead of from what was actually stored. The agent stopped
  trusting a working token and asked for it to be replaced. (#492)

### Loops

- Crash recovery moved off the file-only sweep thread onto the runtime, which
  is the only place that can deliver an outcome and fire a replacement. The
  relaunch decision is the existence of the workflow's manifest, not a count of
  completed nodes: a node is recorded only when it finishes, so a run killed
  inside its first node shows no nodes while being the likeliest to have
  already acted. (#493)
- Outcomes are never delivered into a counterpart's channel thread. That lane
  carries workflow-authored prose only; internal status and exception text stay
  on the operator side. (#493)
- A standing destination only hears actionable outcomes; a run somebody
  explicitly asked for reports back whatever happened, including success. (#493)
- Answering a run parked since before a gateway restart no longer risks the
  sweep finalizing it as interrupted while it is still running. (#493)

### Gateway and channels

- A `${secret:NAME}` reference in the websocket channel's static admin token is
  resolved before comparison. The reference string itself used to be the
  accepted password while the real secret was refused. (#490)
- A config that points at a secret which is not in the store now fails loudly
  instead of taking the whole HTTP surface down quietly: the gateway used to
  start, log a warning, and leave nothing listening — no dashboard, no API.
  (#491)

### Skills

- Feedback on a built-in skill has an exit again, and a curation pass is no
  longer lost. A "reviewed" chip that no longer suppresses anything is
  recognised as stale. In the skills list, a skill's name no longer collapses
  to zero width when its badges are crowded. (#489)

### Doctor

- `durin doctor` stops reporting a missing extra that durin no longer ships.
  The installed-extras list is append-only history, so any retired extra became
  a permanent warning with a suggested fix that could not work. (#488)

### Web UI

- Loop runs can show the `interrupted` state, in all nine locales, and an
  interrupted run's explanatory note is no longer styled as a failure. A run
  fired from a conversation now names that origin instead of rendering a blank
  row. (#493)

### Documentation

- The `loops` skill no longer tells agents that errors reach the operator —
  they never did, and an agent believing it is part of what made the incident
  invisible. It now describes outcome delivery, the non-blocking `fire`
  contract, and `interrupted`. (#493)

## 0.4.9 — 2026-07-27

### Highlights

- **A Slack channel could authenticate with the word `«redacted»`.** Reading a
  config file through a tool masked every credential-keyed field, including the
  ones holding a `${secret:NAME}` reference — a pointer, not a credential. An
  agent that read the config and wrote it back persisted that view, so
  `channels.slack.bot_token` ended up literally storing the redaction marker
  and the channel failed with `invalid_auth` while the secret store was
  untouched. References are now printed as themselves, and the single config
  write path refuses to persist a redaction marker into a credential field at
  all. Editing a channel's config also cycles the running channel, so a saved
  change takes effect without a gateway restart. (#482)
- **Plan mode stopped rejecting plans for being written in Spanish.** A plan
  had to carry an English `## Verification` heading; a plan that ended in
  `## Verificación` was refused and had to be written out again — and the retry
  came back shorter. Success criteria are now a separate argument, so nothing
  about the prose is inspected and a plan in any language is accepted. (#475)
- **A proposed plan is no longer sent again on every turn.** On channels
  without native rendering — Telegram, Slack, Discord, WhatsApp, email, Matrix
  — the full plan text was re-published at the end of each turn until you
  approved it. Leaving plan mode with `/mode` never stopped it, because only
  `/build` cleared the pending plan. Delivery now happens once, a revised plan
  is sent again, and `/mode` closes the plan it leaves behind. (#478)

### Security

- `${secret:NAME}` references survive redaction, and `save_config` rejects a
  credential-keyed field whose value is a redaction marker, naming the offending
  paths. A literal credential sitting next to a reference is still masked. (#482)
- Whole-skill saves through `POST /api/v1/skills/{name}/save` stamp an explicit
  `Actor: user` git trailer. The actor used to be inferred from the commit
  subject, which held only because the default rationale contained the words
  "via web" — dream's curation filters user edits by actor precisely so it does
  not silently revert work you did by hand. (#481)

### Channels

- A config write to `channels.<name>.<field>` cycles the running channel, so a
  live channel no longer keeps serving the settings it was built with. Stopped
  channels stay stopped. (#482)

### Plan mode

- Verification criteria are a required `verification` argument rather than a
  heading matched in the plan's prose; the plan file is still one markdown
  document. (#475)
- A rejected `exit_plan_mode` no longer renders a plan card with an approve
  button in the web UI, or leaves Approve/Refine rows in the TUI. Failed tool
  calls fold into the activity trace instead of being presented as something
  that happened. (#475)
- The plan-mode prompt no longer asks the model to repeat the whole plan in its
  next message — the channel already renders it. (#475)
- Leaving plan mode via `/mode` records the plan as cancelled instead of
  leaving its event open forever. (#478)

### Gateway

- `durin gateway restart` works again. The fallback that resolves which
  executable to re-invoke returned a single string with spaces, used as the
  program name of a shell-less spawn, so it was exec'd as one literal path and
  always failed. It also named a module that is not runnable. The resolver now
  returns argv parts and looks beside the running interpreter before giving up,
  which covers `sudo -u durin`, whose reduced PATH hides `~/.local/bin`. (#486)

### Providers

- The `local` extra and its in-process llama.cpp provider are removed. The
  provider could not emit tool calls, so it could never run the agent; local
  models are served through Ollama, LM Studio or vLLM, which speak the OpenAI
  API including tools. **If you installed `durin-agent[local]`, drop that
  extra** — it no longer exists. (#483)

### Memory

- `memory/history.jsonl` is documented and enforced as a write-only archive of
  the consolidator's raw output. Its reader half — a cursor-driven queue whose
  consumer was replaced by the per-session dream cursor — is gone. Nothing about
  what is written changes. (#479)

### Internal

- Six helpers whose replacements were already in use are removed, along with a
  websocket todos snapshot for an event that was never built. (#480, #484)
- Weekly vendored refreshes of the MCP floor and the model catalog. (#476, #477)

## 0.4.8 — 2026-07-24

### Highlights

- **A confirmation the agent writes for itself is no longer an approval.**
  Four tools that add or rewrite executable state — MCP servers, skills,
  skill edits, dependency installs — decided whether they had permission by
  reading a `confirm` field out of their own call arguments. A value the model
  writes can never be evidence that a person agreed, and the tools carrying it
  are available to cron jobs, dreams and workflows, where there is nobody to
  ask at all. Permission now comes from the context the run happens in, which
  only the runtime can set. With no one reachable the action does not run: it
  is recorded, and `durin approvals` shows what is waiting. (#473)
- **Loop triggers can finally say where a message came from and who sent it.**
  Channel triggers matched on four fixed text fields, so "only in this room"
  and "only app notifications, not people" were both inexpressible; on Slack
  app posts never arrived at all. Filters are now an open map over facts every
  channel populates under the same names, plus a per-channel bag for what has
  no cross-channel meaning. (#472)

### Security

- Privileged actions are gated by the execution context (the runtime-minted
  session key plus a live consumer able to deliver an answer); an unrecognised
  context counts as autonomous. Requests that cannot run are staged under
  `<workspace>/.approvals/` and listed by `durin approvals`. A policy of `auto`
  stays exempt — that authority was granted in configuration, ahead of the run.
  (#473)
- Blocking questions no longer wait on contexts that can never answer: the
  non-interactive list missed dreams, workflows and the dream supervisor, so an
  `ask_user_question` inside a dream sat for five minutes waiting for a reply
  that could not arrive. (#473)

### Channels

- Slack app and bot posts reach loop triggers, and `group_policy: mention` no
  longer hides them from a trigger that asked for them. (#472)

## 0.4.7 — 2026-07-24

### Memory

- The refine verdict cache no longer reopens settled pairs when the
  always_on pass re-ranks its pinned set: the `always_on` attribute is
  curation state, not identity content, and is now excluded from the
  judgment fingerprint. Found in the live verification of 0.4.6 (8 of 35
  cached pairs reopened by 10 overnight flag flips). (#470)

## 0.4.6 — 2026-07-24

### Highlights

- **The nightly dream stops re-doing last night's work.** Full dreams were
  running 2.5–3.5 hours, 90% of it in the dedup judge — ~600 standing entity
  pairs re-sent to the LLM every night, 83% of them the exact pairs already
  ruled "different" the night before. Settled verdicts are now remembered
  (keyed by both pages' actual content and the judge's identity), so a pair
  only returns to the LLM when something about it changes. Expected effect:
  refine drops from hours to minutes and aux-model token spend falls ~90%.
  (#468)
- **Entity descriptions stop being overwritten by the last document
  processed.** Document seeding and session discovery wrote each source's
  "significance" sentence — why the entity matters *to that source* — as the
  entity's body, and newest-wins precedence meant central entities were
  rewritten on every pass (one organization page: 18 rewrites in 4 days,
  ending as whichever document happened to be processed last). Significance
  now only fills an empty body; described entities keep their description
  while still accruing relations, aliases, and sources. (#467)

### Memory

- New field-patch kind `body_if_absent`, used by the document-seeding and
  discovery passes; the learnings write keeps `body_replace` (there the body
  is the item's content). (#467)
- Verdict cache in `memory/.refine_verdicts.json`: plain Tier-1 "different"
  verdicts are memoized against a fingerprint of judgment-bearing fields
  (source/provenance accrual does not reopen a settled pair) plus the judge
  template and model; cache hits emit `memory.absorb.skipped` with reason
  `cached_verdict`, so the hit rate is visible in telemetry. Borderline
  verdicts are never cached and user tombstones remain a separate, permanent
  mechanism. (#468)

## 0.4.5 — 2026-07-24

### Highlights

- **Every editable surface now has history.** Workflow edits made in the
  dashboard — save, delete, duplicate, rename — now commit to the version
  store like the agent's own edits always did, each mutation under its own
  subject instead of one coarse sweep. Loops, previously the last surface
  with no history at all, get the same treatment: every change from the
  webui, the agent, or the CLI is reviewable and reversible. (#459, #460)
- **A pipeline node that runs out of budget gets a second chance.** A work
  node that exhausted its turn budget used to force a synthesis and die if
  that synthesis failed schema validation — whether the run survived was a
  coin flip on how the final turn happened to phrase itself. Nodes can now
  declare `max_reentries` with a `reentry_prompt`, and the exhaustion path
  is schema-aware: the forced synthesis is asked for the structured payload
  directly instead of prose that may or may not contain it. (#461)
- **The gateway hands retained memory back to the OS.** A heavy failed run
  could leave the process holding gigabytes of freed-but-unreturned glibc
  arena pages — indistinguishable from a leak in the old telemetry (a live
  diagnosis found 3.8GB resident with ~190MB actually live). The periodic
  memory tick now reports the allocator's live-vs-retained split and, past
  a threshold of retained freed memory, calls `malloc_trim(0)` and records
  what came back. (#465)

### Workflows

- Renaming a workflow follows into the loops that run it, and deleting one
  refuses while a loop or a sub-flow node still references it — the same
  dependency barrier skills gained for dreams, applied to the workflow
  registry. (#464)
- The registry directories are guarded against generic file writes: registry
  content changes go through the services that version them. (#459)

### Memory

- Dream passes refuse autonomous changes to skills a workflow depends on:
  fuse can no longer remove a referenced source skill, and restructure asks
  before rewriting prose a work node injects into its prompt. (#463)

### Security

- The skill scanner correlates environment access with network reach instead
  of flagging every `os.environ` mention: a script reading a bucket name with
  no way to send it anywhere is no longer `caution`, while one reading a
  credential and calling out keeps the flag. (#462)

### Observability

- `gateway.memory` telemetry and `GET /api/v1/diagnostics/memory` carry
  `malloc_system_mb` / `malloc_in_use_mb` / `malloc_free_mb` (0.0 off
  glibc), separating "objects are growing" from "the allocator retains
  freed pages"; a trim emits `gateway.memory.trimmed` with the observed
  RSS before and after. (#465)

## 0.4.4 — 2026-07-23

### Highlights

- **Screens stop resetting under you.** Every few minutes the dashboard would
  silently reload whatever you were looking at — lists refetched, the selected
  item snapped back to the first one, and unsaved edits in the workflow editor
  were discarded. The bootstrap token is re-minted ahead of expiry, and that
  rotation was flowing through the view tree, remounting every screen's data
  on each cycle. The token now lives in the API layer, so a rotation is
  invisible to the interface. (#457)
- **Secrets survive concurrent writers.** A `durin secret rm` racing the
  gateway storing an OAuth token could silently lose one of the two writes:
  saving a secret store re-wrote an in-memory snapshot taken outside the
  cross-process lock. Saving is now reachable only from the locked mutators,
  so the lost update is structurally impossible rather than merely avoided.
  (#456)

### WebUI

- The freshest bootstrap token is held in the HTTP layer and substituted on
  every authenticated call, instead of being passed down as view state; the
  proactive pre-expiry refresh and the 401 re-auth path both update it. (#457)

### Security

- `SecretStore.save()` is no longer public: the five mutators persist inside
  the cross-process lock, and the seven call sites that re-saved a stale
  snapshot afterwards are gone. (#456)

### Dependencies

- `pypdf` 6.13.3 → 6.14.2. (#455)

## 0.4.3 — 2026-07-23

### Highlights

- **The workflow canvas now speaks the full engine model.** Runtime-resolved
  branch candidates draw as dashed edges (no more floating disconnected
  nodes), data-flow edges from `inputs_from` render with their own toggle,
  and cards surface titles, runtime-aware parallel summaries, personas, and
  badges for schemas, files, skills, and secrets. Routing nodes can declare a
  candidate pool that the runtime is contractually held to. (#451)
- **Memory-view polish from live use.** Graph labels are now chosen by local
  density — one per spatial cell, evenly spaced at every zoom, and a sparse
  region labels everything — instead of a global cap that picked a
  seemingly arbitrary subset in uniform regions. An open entity panel now
  follows the ego focus: drilling to another entity carries the panel along
  instead of leaving it stuck on the previous one. (#452, #453)

### WebUI

- Memory view preference migrated to a versioned key: a stored graph default
  from before the list-first redesign resets to the list once; choices made
  after the migration (including the graph) persist. (#452)
- A search-result click still deliberately opens the entity panel
  (jump-to-node); hub and loose-node drills never open a closed panel; a
  manual selection inside an ego is never overridden. (#453)

## 0.4.2 — 2026-07-23

### Highlights

- **The memory section now leads with what you touched last, not a map.**
  Memoria opens on the recency-sorted list (then cards, then graph); each
  entity's panel gains a "Related" mini graph — its 1-hop neighborhood,
  clickable to hop entity-to-entity, with "View in graph" jumping into the
  exploratory graph already centered there. The standalone graph defaults to
  the ungrouped view with the most-connected entities emphasized, and
  grouping (by structure or by type) is an explicit choice. (#449)
- **Entity importance is honest now.** The overview used to weigh entities by
  a mention count that is always zero on current workspaces; importance is
  now relation degree plus log-damped distinct-session evidence, so hubs
  reflect what durin actually knows and works with — and high-churn
  operational entities can't drown the structure as session volume grows.
  Noise stays hidden by default behind verifiable structural filters
  (sessions, phantoms, no-connections), never an inferred "importance". (#449)
- **Skill curation closes its loop.** Reviewing a gated skill now adopts it —
  the verdict clears the gate permanently and each finding carries a per-file
  acknowledgement — and invalid quarantined skills get a deterministic repair
  path instead of silent expulsion; broken frontmatter no longer ejects a
  gated skill or strands its suggestions. (#446, #447, #448)

### Memory

- `GET /api/v1/memory/graph/overview` accepts `groupBy=community|type`;
  cluster drills follow the active dimension, caches are kept per dimension,
  and bubble thresholds fit real relation-graph density. (#449)
- Overview and drill payloads carry the degree/session score as node weight,
  so radii and label priority track the live signal with no client
  contract change. (#449)

### WebUI

- Memory view: list-by-recency default, reordered switcher (Table, Cards,
  Graph), Related mini ego-graph in the entity panel, group-by selector,
  disconnected-nodes filter with honest counts — strings in all nine
  locales. (#449)

### Workflows

- Workflows and node ids can be renamed from the editor, with references
  rewritten consistently. (#445)

### MCP

- Transient `invalid_grant` responses during the OAuth code exchange are
  retried instead of failing the connection outright. (#443)

## 0.4.1 — 2026-07-23

### Highlights

- **Builtin workflows now follow upgrades.** Bundled workflow seeds used to
  freeze at install day; now they reconcile on every start with provenance
  tracking: a seed you never edited updates itself (committed to the workflow
  version history, so it is revertible), and a seed you customized surfaces a
  reviewable suggestion — a banner on the Workflows screen with the diff and
  apply/dismiss, plus a `durin doctor` notice — never a silent overwrite.
  Existing installs are adopted in place: untouched copies become tracked,
  diverged ones ask once. (#441)
- **Workflow parallelism is now a global setting, split by branch kind.**
  Settings → Concurrency gains a "Workflow branches" group: LLM branches
  (default 2, provider-bound) and script branches (default 4 — cheap, and
  they run under their own lane so they never queue behind LLM branches). A
  node-level `max_concurrency` remains honored as a uniform override, but
  templates and the editor no longer set one — width is configuration, not
  authoring. (#441)
- **The Executions screen was rebuilt as a master-detail view.** Runs on the
  left, the selected run's per-node detail on the right, nested sub-workflow
  runs expand inline, and node output renders as markdown. (#439)
- **The memory graph got a two-layer view built for scale.** A server-side
  clustered overview (semantic-only structure with outlier hubs and a bounded
  payload) replaces the single force-directed canvas that capped out on real
  workspaces, and the client drills from overview into cluster and
  neighborhood views with semantic zoom and constant-size labels — verified
  at 30x today's graph size. (#440)

### Memory

- New `GET /api/v1/memory/graph/overview`: uncapped aggregation, deterministic
  communities, display caps with a drillable overflow bubble, tree-signature
  cache. Sessions and phantoms never shape the structure. (#440)

### Workflows

- Seeding metadata files (`.seeds.json`, suggestions, tombstones) are
  invisible to workflow listings and the improvement pass. (#441)
- New API surface for seed suggestions: list, apply, dismiss. (#441)

### WebUI

- Executions: master-detail split with nested sub-runs and markdown output. (#439)
- Concurrency settings: workflow branch caps, editable live (en/es). (#441)
- Workflows screen: builtin-update banner with per-item diff. (#441)

## 0.4.0 — 2026-07-22

### Highlights

- **The workflow engine grew up as a pipeline engine.** Six improvements, all
  sourced from a real four-stage support-ticket pipeline running in
  production: a sub-workflow child's terminal status now reaches the parent
  (a pipeline can no longer "complete" past a stage that never ran — a child
  that pauses for input pauses the whole run resumably, a failed child aborts
  it naming the stage) (#431); a parallel node can select its branches at
  RUN time from a routing script's output (`branches_from`), ending the
  one-static-block-per-combination workaround (#431); script nodes may run
  as parallel branches, so a deterministic fetch can overlap an LLM analysis
  instead of serializing behind it (#433); a work or script node can be
  `detached` — launched off the critical path for side effects like
  persisting to memory, joined before the run finishes, and unable to sink
  the run if it fails (#435); and an aborted run can RESUME at its failed
  node with the exact input it had — a transient API error at one node no
  longer costs the whole pipeline again (#436).
- **Nodes now have real I/O contracts.** `inputs_from` composes a node's
  input from the labeled outputs of named earlier nodes (plus the current
  edge), so a script chain between producer and consumer no longer needs
  courier files; `output_schema` makes a node deliver a schema-validated
  payload through a forced tool call — a malformed payload is retried
  immediately inside the node with the exact validation error instead of
  costing a full downstream loop-back — and `output_file` has the ENGINE
  write the validated JSON into the working folder, so the file cannot be
  malformed. The bundled seed workflows' fan-out list producers all declare
  schemas now, ending the prose-wrapped-JSON-array bug class at the source.
  (#437)
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
- **Context compaction actually fires now — and shows its work.** The
  compaction trigger, its measured savings, and the resulting context state
  are visible instead of silent; trimmed session files become append-only
  archive history that stays searchable in-session instead of disappearing.
  (#425, #426, #427)

### Changes

- **Workflows** — child status propagation + sub-workflow `duration_s` in the
  parent trace (#431); `branches_from` runtime-selected parallel branches
  (#431); script nodes as parallel branches, with per-branch `exit_code` in
  the trace (#433); choose/union branch forks copy the run's working folder
  only — never the durin workspace around it (#432); `detached: true`
  launch-and-continue nodes (#435); failure resume: aborted manifests store
  the failed node and the exact upstream it received, and `resume_run_id`
  retries that node alone (#436); `inputs_from`, `output_schema` (forced
  `deliver` tool, server-side jsonschema validation, in-node retry) and
  engine-written `output_file` (#437); `input_files` land under their
  original basename — documented everywhere agents read (#431).
- **Seed workflows** — the five fan-out list producers (research-to-answer,
  brainstorming, review-changes, writing-plans, build-specs) declare
  `output_schema` (#437).
- **Web UI (workflow editor)** — third parallel mode (runtime-selected
  branches), script nodes selectable as branches, `detached` toggle,
  `inputs_from` checklist, `output_schema` editor with parse-on-blur and an
  `output_file` field; run-visibility surfaces from #428 across chat, panel
  and executions.
- **Sessions** — compaction trigger reachable, measured, and visible (#425);
  index-rebase-safe park (#426); file-cap trims become append-only archive
  history, searchable in-session (#427).
- **Dependencies** — `jsonschema` promoted from transitive to explicit core
  dependency (#437); pillow 12.3.0 (#424), setuptools 83.0.0 (#434), CI
  actions group (#430).

### Fixes

- **Concurrent workflow runs can no longer prune a live run's working folder.**
  Run folders are pruned to the most recent `workflow.keep_runs`, but the cut
  was by folder age alone — a long-running node freezes its folder's age, so
  enough runs starting during it could delete a mid-flight run's files out
  from under it. Runs that are still executing (or paused waiting for input,
  which resume into the same folder) are now exempt from pruning and don't
  consume retention slots, matching the protection run manifests already had.
  (#429)

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
