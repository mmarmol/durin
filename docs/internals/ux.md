# UX subsystem — unified I/O layer

## 1 Purpose

The UX subsystem is the unified I/O layer that sits between humans and the
agent core. It covers:

- **Three interactive surfaces** (CLI, TUI, WebUI) plus **broadcast channels**
  (Slack, Telegram, Matrix, WhatsApp, and others) that all funnel input into
  the same `MessageBus` and receive output from it.
- **Config and secrets**: the split-file config layout under
  `~/.durin/config.json.d/` and the separate secret store at
  `~/.durin/secrets.json` (plaintext, mode 0600), each accessed through
  atomic primitives to prevent concurrent-process corruption.
- **Design system**: the token-driven palette that keeps CLI, TUI, and WebUI
  visually consistent.
- **Lifecycle commands**: the operator-facing tools for onboarding, configuring,
  upgrading, and uninstalling durin.

## 2 Mental model

**Three interactive surfaces + channels, one bus.** Every surface — interactive
CLI, Textual TUI, WebUI over WebSocket, and external channels like Slack or
Telegram — publishes `InboundMessage` objects into the `MessageBus` and
consumes `OutboundMessage` objects from it. `AgentLoop` and `AgentRunner`
process messages without any awareness of which surface sent them. Agent
behavior is uniform; only rendering differs.

**Config and secrets are separate concerns.** Config is a split-file layout
on disk (`~/.durin/config.json.d/*.json`), validated and loaded through
`load_config`, written through `save_config` / `mutate_config` under a
cross-process lock. Secrets live in a distinct plaintext-0600 file
(`~/.durin/secrets.json`), never in the config tree, and are resolved lazily
at the point of use via `resolve_secret`. Both stores use atomic write
primitives to prevent partial reads on concurrent access.

**Design tokens and lifecycle commands are orthogonal to the agent.** The
two-axis token system (palette × mode) governs the visual surfaces but has no
effect on agent behavior. Lifecycle commands (`durin onboard`, `durin config`,
`durin upgrade`, etc.) operate on the installation state, not on live sessions.

## 3 Diagram

```mermaid
flowchart TD
    subgraph surfaces["User-facing surfaces"]
        CLI["Interactive CLI\n(prompt_toolkit)"]
        TUI["Textual TUI\n(durin agent --tui)"]
        WEB["WebUI\n(WebSocket channel)"]
        EXT["External channels\n(Slack / Telegram / Matrix / etc.)"]
    end

    subgraph bus_core["Shared core"]
        BUS["MessageBus\n(inbound / outbound queues)"]
        LOOP["AgentLoop\n(CommandRouter + state machine)"]
        RUN["AgentRunner\n(LLM iterations + tools)"]
    end

    subgraph orthogonal["Orthogonal subsystems"]
        CFG["Config system\nload_config / mutate_config / save_config\n~/.durin/config.json.d/"]
        SEC["SecretStore\n~/.durin/secrets.json (0600)\nresolve_secret / SecretRedactor"]
        DES["Design system\ndesign/tokens.css + durin/cli/theme.py\npalette x mode (ithildin/forge/mithril)"]
        LIF["Lifecycle commands\nonboard / config / upgrade / uninstall / doctor"]
        PAI["Pairing store\n~/.durin/pairing.json\ngenerate_code / approve_code / revoke"]
    end

    CLI -->|"InboundMessage\n(_wants_stream=True)"| BUS
    TUI -->|"InboundMessage\n(_wants_stream=True)"| BUS
    WEB -->|InboundMessage| BUS
    EXT -->|InboundMessage| BUS

    BUS --> LOOP --> RUN
    RUN -->|OutboundMessage| BUS

    BUS -->|metadata flags route rendering| CLI
    BUS -->|metadata flags route rendering| TUI
    BUS -->|metadata flags route rendering| WEB
    BUS -->|fallback text| EXT

    CFG -.->|"load / mutate / save"| LOOP
    SEC -.->|"resolve_secret at use\nSecretRedactor on tool results"| RUN
    PAI -.->|"is_approved gate on inbound"| EXT
    DES -.->|"palette + mode"| CLI
    DES -.->|"palette + mode"| TUI
    DES -.->|"palette + mode"| WEB
```

## 4 How it works

### Input pipeline

User input at any surface is sanitized and published as an `InboundMessage`:

1. **Surrogate sanitization** (CLI/TUI on Windows): lone surrogate code points
   from console input are repaired or replaced by `_sanitize_surrogates` before
   anything else sees the text.
2. **Drag-and-drop pre-processing** (CLI/TUI): `durin/cli/dragdrop.py` scans
   the input for absolute file paths. Image and audio files (`.png`, `.jpg`,
   `.gif`, `.webp`, `.bmp`, `.svg`, `.mp3`, `.wav`, `.m4a`, `.flac`, `.ogg`,
   `.opus`) are copied to `<workspace>/.media/<sha>.<ext>` (idempotent by
   content hash) and surfaced via `InboundMessage.media`. Document paths (text,
   Markdown, PDF) are left in the text so the agent's `read_file` tool can
   resolve them directly. Dragged audio may be transcribed to text before the
   message is published; on success the audio path is dropped from `media`.
3. **Bus publish**: the CLI and TUI set `metadata={"_wants_stream": True}` so
   the loop wires up streaming callbacks for that turn.

### Command routing

`AgentLoop` calls `CommandRouter.is_priority` first. Priority commands (`/stop`,
`/restart`, `/status`) are dispatched outside the session lock — they can
interrupt an active turn. All other slash commands match exact or longest-prefix
tiers inside the lock; non-commands enter the agent path.

**Slash-command registry.** `BUILTIN_COMMAND_SPECS` (`durin/command/builtin.py`)
is the single source of truth for every built-in slash command: its handler,
help text, and the surfaces it is listed on. The WebUI palette, the TUI
autocomplete, `/help` output, and channel command menus (e.g. Telegram's
bot command list) all derive their listings from this registry instead of
maintaining their own copies. Each spec declares which surfaces list it
(`webui`, `tui`, `channels`); a command can be dispatchable everywhere while
being listed on only some surfaces — for example a TUI-only shortcut stays
usable if typed elsewhere, it just doesn't appear in other surfaces' menus.
Commands marked `admin` stay fully functional but are never listed on any
surface. `GET /api/v1/commands` serves the registry scoped to a surface
(the WebUI calls it with the `webui` surface, so its palette only shows
webui-listed commands).

### Agent execution

`AgentRunner` iterates LLM calls and tool batches. During execution:

- **Streaming**: text delta chunks are published as `OutboundMessage` with
  `metadata["_stream_delta"] = True`; the final chunk carries `_stream_end`.
- **Tool events**: structured `tool_events` frames (start / end / result) flow
  to every subscriber; rich channels hoist certain tool payloads as first-class
  widgets.
- **Special interactive tools** (`ask_user_question`, `request_secret`,
  `exit_plan_mode`, `todo_write`) follow a payload-canonical contract: the tool
  *arguments* carry all display content; the tool *result* is model-directed
  bookkeeping. Rich channels (`RICH_PAYLOAD_CHANNELS = {"websocket", "cli"}`)
  render the structured payload directly (question panel with option chips,
  masked secret prompt, plan card, todo checklist). All other channels receive
  a serialized plain-text fallback at turn end from
  `AgentLoop._maybe_publish_interaction_fallback`. In the TUI the `exit_plan_mode`
  bubble additionally renders inline `Approve` and `Refine` action rows; selecting
  Approve publishes the `/build` command without requiring the user to type it.
- **Blocking ask_user**: when `agents.defaults.ask_user_blocking` is true
  (the default), `ask_user_question` awaits the user's next plain-text reply
  *inside the same turn* via the `durin/agent/pending_answers.py` future
  registry. The loop's inbound consumer resolves the future; the answer returns
  as the tool result and the model continues without a turn boundary. On answer
  timeout, media reply, absent loop consumer, or non-interactive session
  (`cron:`/`system:` prefixes), the tool degrades to yield semantics: it
  returns early and the next user message carries the answer.
- **Secret redaction**: `SecretRedactor` processes every tool result before it
  reaches the model or is spilled to disk. Two layers: value-based (exact stored
  secret values become `«redacted:NAME»`) and pattern-based (credential-shaped
  strings — vendor prefixes `sk-`/`ghp_`/`AKIA`, JWTs, PEM blocks,
  `KEY=value` fields — become `«redacted»`).

### Outbound routing

Metadata flags on `OutboundMessage` route behavior at each surface:

| Flag | Effect |
|---|---|
| `_stream_delta` | Append text to the active assistant bubble / stream buffer |
| `_stream_end` | Close / finalize the current streaming bubble |
| `_streamed` | End-of-turn marker; no visible side-effect |
| `_switch_chat_id` | In-place session switch; `run_interactive` / `DurinApp` updates `cli_chat_id` |
| `_pairing_code` | Channel-level pairing code delivery (external channels only) |
| `_progress` | Progress note; not rendered as a full message in WebSocket channel |
| `_turn_end` | Carries latency and goal-state; triggers WS `turn_end` frame |
| `render_as="text"` | Render as a plain system bubble instead of an assistant bubble |

### WebUI message rendering

**Math.** Inline `$…$` and display `$$…$$` expressions are rendered via KaTeX.
Each rendered formula exposes two per-formula actions: *Copy LaTeX* (puts the raw
LaTeX source on the clipboard) and *Copy for Word* (puts MathML on the clipboard
for paste-as-editable-equation into Word or Google Docs). The composer toolbar
contains a visual equation editor (MathLive) that lets the user build a formula
with point-and-click; confirming inserts a `$…$` delimited expression into the
draft, and a live preview updates as the user types `$…$` or `$$…$$` markers
directly in the composer.

**Attachments.** The composer accepts image, audio, and document attachments
(the accepted MIME set lives in `ThreadComposer` and mirrors the WebSocket
channel's server-side whitelist). Images ride inline as base64 vision content;
documents (PDF, Office, EPUB, …) are decoded to a file on the gateway — the
original extension is preserved so extraction dispatches correctly — and their
text is extracted and folded into the message by `extract_documents`
(`durin/utils/document.py`) for the agent to read. The marker carries the file's
**saved on-disk path** (`[File: <name> — saved on disk at <path>]`), not just the
name, so when the user asks to *remember* an attached document the agent can
`memory_ingest("<path>")` it directly instead of asking for a path it doesn't
have.

**Rich fenced blocks.** Code blocks tagged `html`, `svg`, `mermaid`, or
`vega-lite` render inline as a `RichBlock`. Each block shows a header strip with
a code⇄preview toggle, an expand button, and a copy button. The
preview is the default view; the toggle reveals the raw source. The expand button
opens a full-screen dialog so large diagrams or charts fill the viewport. HTML and
SVG previews run in a sandboxed iframe with `allow-scripts` and a
`Content-Security-Policy` that blocks all network requests, so embedded content
cannot load external resources. Mermaid diagrams and Vega-Lite charts are rendered
by bundled lazy-loaded modules so they do not add to the initial bundle.

The agent-side contract lives in the system prompt
(`durin/templates/agent/rich_output.md`, injected by the context builder's stable
layer): it advertises the four fence languages and the sandbox constraints, and
explicitly steers the agent away from the paths that bypass rendering — leaving
the content only as a workspace file path, and drawing diagrams as ASCII art in
plain code blocks.

`.html` message attachments render inline through the same sandboxed preview as
a fenced `html` block (the agent often delivers a full-page mockup as a file
attachment rather than a fence), with the download link preserved as a caption.
The attachment's display kind is inferred from the filename on both paths: in
the client for live frames, and in the transcript replay builder for reloads.

**Render stability.** Rich blocks must never remount while the user watches —
a remount reloads the sandboxed iframe (visible flash) and resets an open
code⇄preview toggle. Three contracts uphold this: replay-minted message ids
are deterministic (two replays of the same transcript produce identical ids,
so the post-turn canonical refetch reconciles by React key instead of
re-keying rows); a streamed reply keeps its first render identity via a
client-only `renderKey` when the consolidated final frame and the canonical
replay row later re-identify it (the final frame merges into the streamed row
rather than appending a duplicate); and the markdown renderer's
component-override map is memoized, because each entry's function identity is
the React element type for every fence — a fresh map per render would remount
every rich block in the message on any re-render.

### Work-visibility surfaces (WebUI)

Three surfaces make background work visible inside a chat without navigating
away from it.

**Goal banner.** A pinned strip at the top of the chat thread renders the
session's `goal_state` — the agent-maintained description of what it is trying
to accomplish for this turn. The banner is persistent: it stays visible while
the agent runs and is cleared when the turn ends with no active goal. It draws
from the `_turn_end` frame's `goal_state` field.

**Work panel.** A collapsible side panel docked to the right of the chat thread,
toggled from a button in the chat header (next to the theme toggle). A badge on
the toggle button lights up while any work is active. The panel lists in-progress
work items — workflow nodes with nested parallel branches and sub-agent steps —
and a collapsible "Finished" fold below (collapsed by default). Each item is a
`WorkItem` fetched from `GET /api/v1/tasks?session=<key>`. Sub-agent and workflow
detail is shown in the panel, not inline in the thread; the chat itself shows only
a compact **work chip** (running or done) that opens the panel when clicked.
Provider-retry status renders inside the run strip, not as a separate banner.
The panel is durable across a page reload: finished items are reconstructed from
session lineage and workflow run manifests.

**Work chip.** A compact inline indicator in the message thread shows that
background work is running or has finished. Clicking it opens the work panel.
Per-node detail and sub-agent steps are not expanded inline — they live in the
panel.

**Work strip.** The panel's collapsed representation: a slim status line docked
directly above the composer, rendered only while the panel is closed and there
is something to report (opening the panel replaces it, so the two never show at
once). It reads the active work in priority order — any item waiting on user
input renders a warn-tinted "needs your response" line with a Respond
affordance; otherwise running work renders a neutral line with the item's label
(a count when several run at once); and when the last active item ends the strip
flashes a finished/failed line for a few seconds before disappearing. The whole
strip is a single button that opens the work panel, and the wrapper is a
`role="status"` live region so state changes are announced by screen readers.
Unlike the inline work chip, it does not scroll away with the transcript.

### Work-visibility surfaces (TUI)

The TUI sidebar (toggled with **Ctrl+B**) exposes a WORK section alongside the
existing Todos, Files, and MCP tabs.

**Sidebar WORK section.** When work becomes active in a turn — a running
workflow or a spawned sub-agent — the sidebar auto-opens and switches to the
WORK tab. The section renders each active workflow as a node tree, including
nested parallel branches, with per-node status (running / done / failed /
needs_input). Active sub-agents appear as peer entries alongside workflow
nodes. The section is fed from `workflow_progress` and `subagent_result`
events on the outbound bus; no polling is involved.

**Paused workflows (needs_input).** The terminal `workflow_progress` frame
carries the run-level status and, for a `needs_input` run, the questions as a
capped `detail` field. The TUI keeps a paused run in the active list — glyph
`?`, "waiting" count in the WORK header, first question line under the item —
and additionally raises a warning toast plus a system note in chat carrying the
questions. The user answers in chat and the agent resumes the run
(`run_workflow` with `resume_run_id`); the sidebar entry is a signal, not an
input surface, matching the webui's "the agent owns resume" design.

**Live turn diagnostics (footer).** While a turn is in flight the footer shows
a ticking elapsed clock (1s interval, only active during the turn) instead of
the previous turn's latency, and the header status dot fills in. Provider
retry/backoff status (`_retry_wait` outbound frames) renders as a footer badge
— attempt count, retry limit (`∞` for persistent mode) and seconds until the
next try, or a "giving up" marker on the final attempt. The badge clears as
soon as reasoning/content/tool events flow again or the turn ends.

**Goal banner.** A sticky strip above the chat renders the session's
active goal, drawn from the `goal_state` blob carried on turn-end frames and
on the dedicated goal-state sync push (`_goal_state_sync`). The banner is
hidden when there is no active goal.

### Config flow

`load_config()` detects the layout (split directory or legacy monolith) and
returns a validated `Config`. On a legacy monolith it auto-migrates once:
splits per-topic files into `~/.durin/config.json.d/`, backs up the original
as `config.json.legacy`, and rewrites `config.json` as a one-line marker
`{"_layout": "split"}`. The section set is derived from the serialized config
(every non-default top-level key) so newly added sections are never silently
dropped.

`save_config()` writes only non-default fields (`exclude_defaults=True`) back
to the split directory. `mutate_config()` performs an atomic
load-modify-save under `cross_process_lock` so concurrent processes serialize.

### Secrets flow

`SecretStore` loads and persists `~/.durin/secrets.json` (mode 0600) under
`cross_process_lock`. Config consumers hold `${secret:NAME}` references;
`resolve_secret()` turns them into plaintext at the point of use — the value
never re-enters the `Config` object, logs, or telemetry. The `ExecTool`
injects execution-scoped secrets into subprocess environments via
`collect_for(consumer)` so scripts access credentials without the agent ever
seeing the values. After any in-process write the store is reloaded and the
redactor is rebuilt so the next tool result immediately picks up the new secret.

### Pairing flow

External channels gate unrecognized DM senders. When a new sender contacts a
channel, `BaseChannel._handle_message` checks `is_approved(channel, sender_id)`.
If the sender is not in `pairing.json` and there is no `allowFrom` match, the
channel generates a time-limited pairing code (`generate_code`, 10-minute TTL)
and sends it back as a formatted message. The account owner then issues one of
these commands to approve or manage access:

| Subcommand | Effect |
|---|---|
| `/pairing list` | Show pending codes with their expiry |
| `/pairing approve <code>` | Approve the sender; adds them to `pairing.json` |
| `/pairing deny <code>` | Discard a pending code without approving |
| `/pairing revoke <user_id>` | Remove an approved sender from the current channel |
| `/pairing revoke <channel> <user_id>` | Remove an approved sender from a specific channel |

These subcommands are handled by `handle_pairing_command` in `durin/pairing/store.py`.
The store uses a module-level `threading.Lock` plus `cross_process_lock` so
operations are safe from both async channel handlers and sync CLI contexts.

## 5 Key types and entry points

| Symbol | File | Role |
|---|---|---|
| `MessageBus` | `durin/bus/queue.py` | Two `asyncio.Queue`s (inbound / outbound); pure async decoupler between surfaces and agent |
| `InboundMessage` / `OutboundMessage` | `durin/bus/events.py` | Message envelope: `channel`, `chat_id`, `content`, `media`, `metadata` (routing flags); `InboundMessage.session_key` property derives the session key |
| `AgentLoop` | `durin/agent/loop.py` | Per-turn state machine and command dispatcher; polls `bus.inbound`, routes to `CommandRouter` or agent; publishes `bus.outbound` |
| `AgentRunner` | `durin/agent/runner.py` | LLM iteration core: tool batches, streaming, redaction, context governance |
| `CommandRouter` | `durin/command/router.py` | Three-tier dispatch (priority / exact / longest-prefix); `is_priority()` gates lock-free commands |
| `BuiltinCommandSpec` / `cmd_*` | `durin/command/builtin.py` | Slash-command metadata (`BUILTIN_COMMAND_SPECS` tuple) and async handlers for `/new`, `/stop`, `/restart`, `/status`, `/usage`, `/retry`, `/model`, `/persona`, `/effort`, `/history`, `/goal`, `/help`, `/plan`, `/build`, `/mode`, `/sessions`, `/resume`, `/compact`, `/copy`, `/name`, `/hotkeys`, `/memory`, `/skills`, `/remember`, `/forget`, `/sources`, `/audit`, `/why`, `/version`, `/pairing` |
| `DurinApp` | `durin/cli/tui/app.py` | Textual TUI app; `on_mount` spawns `agent_loop.run()` and `_consume_outbound`; maps metadata flags to widget operations |
| `run_interactive` | `durin/cli/commands.py` | Interactive CLI loop: `PromptSession`, surrogate-sanitize, drag-drop pre-process, bus publish, `_consume_outbound` render |
| `Config` | `durin/config/schema.py` | Pydantic `BaseSettings` root: `agents`, `providers`, `channels`, `tools`, `memory`, `gateway`, `api`, `telemetry`, `appearance`, `model_presets`, `skills`, etc. |
| `load_config` / `save_config` / `mutate_config` | `durin/config/loader.py` | Config I/O: layout-transparent (split or legacy monolith); atomic cross-process write; auto-migration to split on first use |
| `SecretStore` | `durin/security/secrets.py` | Plaintext-0600 JSON store; `SecretEntry` fields: `value`, `service`, `account`, `description`, `scope`, `origin`, `created_at` (`name` is the map key, not a model field) |
| `resolve_secret` / `SecretRedactor` | `durin/security/secrets.py` | `resolve_secret()` dereferences `${secret:NAME}` at use; `SecretRedactor` applies value-based + pattern-based redaction on tool results |
| `handle_pairing_command` | `durin/pairing/store.py` | Pure function executing `/pairing` subcommands (list / approve / deny / revoke); `generate_code` / `approve_code` / `revoke` manage `~/.durin/pairing.json` under `threading.Lock` + `cross_process_lock` |
| `ask_user_question` / `request_secret` / `exit_plan_mode` / `todo_write` | `durin/agent/tools/ask_user.py`, `durin/agent/tools/secrets.py`, `durin/agent/tools/plan_mode.py`, `durin/agent/tools/todos.py` | Interactive tools; payload-canonical contract (arguments carry display content); rich channels render widgets, dumb channels get serialized fallback |
| `pending_answers` | `durin/agent/pending_answers.py` | Per-session `asyncio.Future` registry for blocking `ask_user_question`; `can_block()` gates in-turn blocking by checking consumer activity and session prefix |
| `RICH_PAYLOAD_CHANNELS` | `durin/agent/user_payloads.py` | Set of channel names that render structured tool payloads natively: `{"websocket", "cli"}` |
| `theme.py` / `tokens.css` | `durin/cli/theme.py` / `design/tokens.css` | Six Textual themes (ithildin/forge/mithril × light/dark) mirroring the CSS token values; a test pins the two together so they cannot drift |
| `process_dragged_paths` | `durin/cli/dragdrop.py` | Scans input for absolute file paths; copies media to `<workspace>/.media/<sha>.<ext>`; returns `(cleaned_text, media_list)` |

## 6 Configuration and surfaces

### Launch surfaces

| Surface | How to start |
|---|---|
| Interactive CLI | `durin agent` |
| Textual TUI | `durin agent --tui` |
| WebUI (gateway) | `durin gateway` — serves WebSocket channel + SPA on `gateway.host:gateway.port` |

### WebUI features

The WebUI (served by `durin gateway` on port `gateway.port`, default 18790)
provides browser-based interaction over WebSocket.

**Composer toolbar.** Above the text input, two pill buttons control the model
and agent:
- **Agent pill** (left, icon + "mode · persona"): Opens a popover with two
  sections. The mode section lists available agent modes (build, plan, explore, read);
  selecting one publishes `/mode <name>`. The persona section lists all configured
  personas (each with name and optional description); selecting one publishes
  `/persona <name>`. Personas lazy-load on first popover open.
- **Model pill** (right, icon + model name + "effort" badge): Opens a popover
  with two sections. The model section lists configured model presets and provides
  a custom provider/model entry; selecting one publishes `/model <ref>`. The effort
  section lists reasoning levels (Default / Off / High / Max); selecting one
  publishes `/effort <level>`. The current effort level (if any) is extracted from
  the active model preset's suffix (e.g. `default:high` → `high`).

**Sending while the agent works.** A plain message sent mid-turn is deferred
server-side: it enters the conversation when the current work finishes its
response, and the message row shows a "queued" chip until then (driven by the
`message_queued` / `queued_consumed` WebSocket acks). The composer's steer
button (compass icon, visible while streaming) sends the text flagged as a
steer instead: it injects into the running turn as mid-work guidance. Stop
remains the hard interrupt.

**Empty-state landing.** When a chat is first opened, a centered greeting is
shown above the composer. There are no suggestion chips or canned prompts.

**Edit affordance.** The last user message carries a dim edit hint; clicking the
message reloads its text into the composer input for revision. This is a refill
only — it does not truncate history or automatically resend.

### TUI features


The Textual TUI (`durin agent --tui`) provides richer interaction affordances
than the interactive CLI.

**Keybindings**

| Binding | Action |
|---|---|
| Ctrl+B | Toggle the sidebar (Todos / Files / MCP / WORK tabs) |
| Ctrl+Shift+P | Open the persona picker modal |

**Persona picker.** The Ctrl+Shift+P modal lists all configured personas, showing
each persona's name, soul, and model. The currently active persona is marked.
Selecting a persona publishes `/persona <name>` into the session.

**Steer (Ctrl+G).** Sends the current input as a steer — injected into the
running turn as mid-work guidance. A plain Enter send while the agent works is
deferred until the turn finishes its response, and a toast confirms it was
queued.

**Footer.** The footer displays the wall-clock duration of the last completed
turn and the active agent mode.

**Edit affordance.** Each user message carries a dim edit hint; clicking the
message reloads its text into the composer input for revision. This is a refill
only — it does not truncate history or automatically resend.

**Empty-thread landing.** When the thread is empty, the chat area shows only
the durin logo/banner — no suggestion chips or canned prompts. A
scroll-to-bottom control appears when the user has scrolled up from the latest
message.

**Lazy older history.** Opening a session loads only the newest page of its
transcript; scrolling near the top fetches the previous page and prepends it
without moving the visible content. A "Loading earlier messages…" indicator
shows while a page is in flight, and once the start of history is reached the
top of the thread renders a "Beginning of conversation" label instead.

### Key config keys

**Channels**

| Key | Default | Effect |
|---|---|---|
| `channels.send_progress` | `true` | Stream agent text progress to the channel |
| `channels.send_tool_hints` | `false` | Stream tool-call hint messages to the channel |
| `channels.show_reasoning` | `true` | Surface model reasoning when the channel implements it |
| `channels.send_max_retries` | `3` | Max outbound delivery attempts (initial send included) |
| `channels.transcription_provider` | `"groq"` | Voice transcription backend (`"groq"` or `"openai"`) |
| `channels.transcription_language` | `null` | Optional ISO-639-1 hint for audio transcription |

**Appearance**

| Key | Default | Effect |
|---|---|---|
| `appearance.palette` | `"ithildin"` | Color palette: `ithildin` / `forge` / `mithril` |
| `appearance.mode` | `"auto"` | Light/dark mode: `auto` (detects `COLORFGBG` or browser `prefers-color-scheme`) / `light` / `dark` |

**Agent interaction**

| Key | Default | Effect |
|---|---|---|
| `agents.defaults.ask_user_blocking` | `true` | Enable in-turn blocking ask_user (awaits answer inside the same turn without a turn boundary) |
| `agents.defaults.ask_user_answer_timeout_s` | `300` | Seconds before blocking ask_user falls back to yield semantics |

**Gateway**

| Key | Default | Effect |
|---|---|---|
| `gateway.host` | `"127.0.0.1"` | Bind address for the gateway server |
| `gateway.port` | `18790` | Port for the gateway server |
| `gateway.daemon` | `false` | Run gateway detached (PID file + log file) |
| `gateway.webui_enabled` | `true` | Auto-enable WebSocket channel so the embedded WebUI is served |

**Other**

| Key | Default | Effect |
|---|---|---|
| `model_presets` | `{}` | Named `ModelPresetConfig` entries; `/model <preset>` switches at runtime |
| `agents.defaults.unified_session` | `false` | Share one session across all channels |

### Filesystem paths

| Path | Contents |
|---|---|
| `~/.durin/config.json` | One-line marker `{"_layout": "split"}` after migration |
| `~/.durin/config.json.d/*.json` | Per-topic config files (one per non-default top-level section) |
| `~/.durin/config.json.legacy` | Backup of the pre-split monolith (written once, not updated) |
| `~/.durin/secrets.json` | Plaintext secret store, mode 0600 |
| `~/.durin/pairing.json` | Approved senders + pending pairing codes per channel |
| `<workspace>/.media/` | Workspace-local copies of dragged/dropped media files |

## 7 Curated rationale

**One bus for all surfaces.** Every surface routing through the same
`MessageBus` means a new channel (say, a Discord adapter) gets the full agent
behavior for free — slash-command routing, interactive tool payloads, streaming,
session management — without touching agent code. The only surface-specific work
is rendering.

**Payload-canonical interactive tools.** Encoding the display content in tool
*arguments* rather than having the model re-present it in prose creates a
reliable rendering contract. The channel sees the structured payload and renders
it as a native widget; the model never gets a chance to misstate the question or
reformat the plan. For channels that cannot render widgets, the serialized
fallback reproduces the same content from the stored session metadata rather
than re-asking the model.

**Config split-layout.** Keeping one JSON file per top-level section means a
diff to `channels.json` does not touch `providers.json`. The section set is
derived from the serialized config at write time so newly added top-level fields
are never silently dropped by a hardcoded list.

**Secrets isolated from config.** A `${secret:NAME}` reference in config is
inert on disk and inert in the `Config` object. The value only materializes at
the moment a provider or tool calls `resolve_secret()`, and it goes straight to
the consumer without touching any persisted path. This means accidentally
committing or sharing `config.json` leaks only a reference, not the credential.

**Pairing as a channel-level gate.** Unrecognized DMs never reach the agent.
`BaseChannel._handle_message` checks `is_approved` before publishing to the bus
and sends a pairing code reply instead of an error — so the channel appears
responsive to the new user and the owner can approve them at leisure.
