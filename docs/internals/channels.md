# Channels & Message Bus

## 1. Purpose

The channels subsystem is durin's input/output layer. It translates
platform-specific events (Telegram messages, Discord DMs, WebSocket frames,
emails, Slack events, and others) into a neutral `InboundMessage` that the
agent loop can process, and routes `OutboundMessage` responses back to the
originating platform without the agent ever knowing which platform it is
talking to.

Three concerns are handled here that belong nowhere else:

- **Plugin discovery** — new channel adapters can be added as Python packages
  and are loaded automatically at startup.
- **Permission and pairing** — inbound authorization is enforced once,
  centrally, at the message-bus ingress gate; unapproved DM senders are handed
  a time-limited pairing code; the owner approves or denies via `/pairing`.
- **Streaming and deduplication** — the outbound dispatcher coalesces stream
  deltas, gates progress and reasoning messages per channel capability, and
  retries on transient delivery failures, all in one place.

## 2. Mental model

**Decoupled bidirectional broker.** Two `asyncio.Queue` objects in
`MessageBus` are the only coupling between channels and the agent loop. Each
channel adapter runs its own async listener loop and pushes `InboundMessage`
objects onto `bus.inbound`; the agent loop consumes them and pushes
`OutboundMessage` objects onto `bus.outbound`. The outbound dispatcher
(`ChannelManager._dispatch_outbound`) consumes from `bus.outbound` and calls
the appropriate channel's `send` / `send_delta` methods. Neither side blocks
the other; the queues absorb timing differences.

**Plugin discovery with built-in priority.** `discover_all()` in
`durin/channels/registry.py` first scans the `durin.channels` package with
`pkgutil` to find built-in adapters, then loads external adapters registered
via the Python `entry_points` group `durin.channels`. Built-in adapters always
win if a plugin registers the same name. `ChannelManager._init_channels`
iterates the discovered classes, checks `config.channels.<name>.enabled`, and
instantiates the ones that are on.

**Inbound authorization and pairing.** Authorization is enforced once,
centrally, at the message-bus ingress via `MessageBus.publish_inbound`. When
`ChannelManager` starts it installs `_authorize_inbound` on the bus via
`bus.set_inbound_authorizer`. Every call to `publish_inbound` — from any
channel or internal publisher — runs through this gate before the message is
enqueued. Channels are pure transport and MUST NOT re-implement authorization
in their handlers.

The gate applies three rules in order: (1) messages whose channel name is not
in `ChannelManager.channels` (cli, cron, subagent, TUI — internal origins) are
always trusted; (2) for registered chat channels, `channel.is_allowed(sender_id)`
is called — star wildcard, allowlist, or pairing-store approval; (3) if denied
and the message is a DM (`is_dm=True`), a pairing code is generated via
`generate_code` and sent back to the sender; if denied and the message is a
group message, it is silently dropped.

`BaseChannel.is_allowed(sender_id)` checks three layers: a wildcard (`"*"`) in
`allow_from`, an exact match in the `allow_from` list, and a lookup in the
pairing store (`is_approved(channel, sender_id)`). Channels set `is_dm` on the
`InboundMessage` they publish so the gate can distinguish private from group
context. The pairing store is a small JSON file at `<durin_home>/pairing.json`
guarded by a `threading.Lock` plus `cross_process_lock` for cross-process
safety (see `durin/pairing/store.py`). The owner runs `/pairing approve <code>`
to move a sender from pending to approved.

## 3. Diagram

### Full message flow

```mermaid
sequenceDiagram
    participant P as Platform<br/>(Telegram / Discord / etc.)
    participant CH as Channel adapter<br/>(BaseChannel subclass)
    participant BUS as MessageBus<br/>publish_inbound → gate → inbound queue
    participant GATE as ChannelManager<br/>_authorize_inbound
    participant LOOP as AgentLoop
    participant MGR as ChannelManager<br/>(_dispatch_outbound)

    Note over CH: start() — long-running listener

    P->>CH: platform event (message / update)
    CH->>CH: _handle_message(sender_id, chat_id, content, is_dm=…)
    CH->>BUS: publish_inbound(InboundMessage)
    BUS->>GATE: _authorize_inbound(msg)
    alt channel unknown (cli/cron/subagent/TUI)
        GATE-->>BUS: True (trusted — internal origin)
    else channel.is_allowed(sender_id)
        GATE-->>BUS: True
    else not allowed + is_dm
        GATE->>CH: send(OutboundMessage with pairing code)
        Note over GATE: pairing code stored in pairing.json (TTL 600s)
        GATE-->>BUS: False (dropped)
    else not allowed + group
        Note over GATE: silently dropped
        GATE-->>BUS: False (dropped)
    end
    BUS-->>LOOP: consume_inbound() (allowed messages only)
    LOOP->>LOOP: process turn (state machine)
    LOOP->>BUS: publish_outbound(OutboundMessage)
    BUS-->>MGR: consume_outbound()
    alt _reasoning_delta / _reasoning_end / _reasoning
        MGR->>MGR: check channel.show_reasoning
        MGR->>CH: send_reasoning_delta / send_reasoning_end
    else _progress message
        MGR->>MGR: _should_send_progress (send_progress / send_tool_hints)
        MGR->>CH: send(msg) if allowed
    else _retry_wait
        MGR->>CH: send(msg) only for websocket channel
    else _stream_delta
        MGR->>MGR: _coalesce_stream_deltas (same channel+chat_id+_stream_id)
        MGR->>CH: send_delta(chat_id, content, metadata)
    else final response
        MGR->>MGR: _should_suppress_outbound (SHA1 dedup by origin_message_id)
        MGR->>CH: send(OutboundMessage)
    end
    CH->>P: platform-specific delivery (with retry backoff)
```

### Pairing approval sub-flow

```mermaid
sequenceDiagram
    participant U as Unknown DM sender
    participant CH as Channel adapter
    participant GATE as ChannelManager._authorize_inbound
    participant PS as pairing.json store
    participant O as Owner

    U->>CH: DM message
    CH->>GATE: publish_inbound(InboundMessage, is_dm=True)
    GATE->>PS: generate_code(channel, sender_id) — TTL 600s
    GATE->>CH: send(OutboundMessage with pairing code)
    CH->>U: "Your pairing code is ABCD-EFGH …"
    U->>O: (shares code out of band)
    O->>CH: /pairing approve ABCD-EFGH
    CH->>PS: approve_code("ABCD-EFGH")
    PS-->>CH: (channel, sender_id) moved to approved set
    Note over U,GATE: Next DM from U passes channel.is_allowed()
```

### Plugin discovery and startup

```mermaid
flowchart TD
    A[gateway start] --> B[ChannelManager.__init__]
    B --> C[_init_channels]
    C --> D["discover_all()<br/>durin/channels/registry.py"]
    D --> E["pkgutil.iter_modules(durin.channels)<br/>built-in adapters"]
    D --> F["entry_points(group='durin.channels')<br/>external plugins"]
    E --> G{built-in wins on name clash}
    F --> G
    G --> H[for each discovered class]
    H --> I{config.channels.name.enabled?}
    I -- No --> J[skip]
    I -- Yes --> K["_resolve_section_secrets(section)<br/>expand \${secret:} refs"]
    K --> L[cls(config, bus, **kwargs)]
    L --> M[inject TranscriptionService]
    M --> N[set send_progress / send_tool_hints / show_reasoning]
    N --> O[channels dict]
    O --> P[start_all: asyncio tasks per channel<br/>+ _dispatch_outbound task]
```

## 4. How it works

### Startup

`MessageBus()` is constructed once per gateway process. `ChannelManager` is
constructed with the bus and the full `Config`. In `_init_channels` it calls
`discover_all()`, which combines built-in adapters found by
`pkgutil.iter_modules` with external adapters from `entry_points`. For each
enabled channel it resolves `${secret:}` credential references with
`_resolve_section_secrets` (so plaintext never lives in the shared config
object), constructs the channel, injects the shared `TranscriptionService`
(built once from `config.transcription`), and copies the global boolean
overrides (`send_progress`, `send_tool_hints`, `show_reasoning`). Per-channel
values in config can override the global defaults.

Once `start_all` is called, a dedicated asyncio task is created for
`_dispatch_outbound`, and one task per channel runs under a supervision loop
(`ChannelManager._start_channel`) that calls `channel.start()`.

**Channel crash supervision.** An exception raised out of `channel.start()` is
treated as a transient crash: the supervisor restarts the channel with a
capped exponential backoff, and the backoff resets once the channel has run
stably for a while. A clean return from `channel.start()`, by contrast, ends
supervision for that channel — it signals either a deliberate stop or a fatal
configuration error that a restart loop cannot fix and would only hammer the
platform with reconnect attempts. Fatal-configuration examples include a
rejected bot token, missing privileged intents, or another local process
already holding durin's per-token advisory lock — an advisory file lock that
prevents two gateways from double-replying on the same bot token; the second
starter refuses cleanly rather than raising.

**Liveness.** Discord also runs a REST-based liveness probe alongside the
gateway connection; when the probe detects a zombie socket the heartbeat
cannot see, it force-closes the connection so the crash supervisor above
reconnects it.

### Optional extras and channel availability

Some built-in channels depend on a third-party SDK that is not installed by
default (Slack, Discord, Matrix today). Matrix guards the SDK import in a
`try`/`except ImportError` block and sets a module-level availability flag
(`MATRIX_AVAILABLE`) rather than letting the import fail. Discord instead
probes with `importlib.util.find_spec` before importing, setting
`DISCORD_AVAILABLE` and only running `import discord` when the probe
succeeds. Either way, both modules stay importable regardless of whether
their extra is installed, so `discover_channel_names()`
(`durin/channels/registry.py`) can enumerate them with zero imports. Slack
currently does **not** guard its import — `slack.py` hard-imports `slack_sdk`
at module scope — so when the `slack` extra is missing, the module itself
fails to import and Slack disappears from `discover_all()` and
every surface built on it (webui channel list, onboarding, config docs). The
auto-install step described below covers the common case where Slack is
enabled, since it installs the extra before the module is imported for that
process; it does not help an operator who wants to merely see Slack listed
as available-but-disabled without enabling it first.

`GET /api/v1/channels` (`durin/service/config.py`) reports this per channel as
two extra fields: `available` (whether the channel's dependency currently
imports) and `install_extra` (the pip extra to install when it does not, or
`null` when the channel is available). Both are derived by looking the
channel name up in the extras `REGISTRY` (`durin/extras.py`) and probing its
declared module.

At gateway start, `ChannelManager._ensure_channel_extras()`
(`durin/channels/manager.py`) auto-installs the missing dependency for any
*enabled* channel: it intersects the extras `REGISTRY` with the channel names
`discover_channel_names()` reports, and for each enabled channel whose probe
module is not importable, calls `ensure_or_note` (`durin/extras.py`) to
install the corresponding pip extra (subject to
`config.install.auto_install_extras`). A freshly-installed SDK needs a
gateway restart to take effect, since the channel module's import already ran
for the current process.

### Inbound path

Every channel's `start()` implementation runs a platform-specific event loop
(polling, webhook, or persistent connection). When an event arrives, the
channel calls `self._handle_message(sender_id, chat_id, content, is_dm=…)`.

`_handle_message` is purely a transport helper: it attaches the
`"_wants_stream": True` flag when the channel supports streaming
(`config.streaming=True` AND the subclass overrides `send_delta`), constructs
an `InboundMessage`, and calls `bus.publish_inbound`. Authorization is not
performed in the channel.

`MessageBus.publish_inbound` runs the installed inbound-authorizer gate before
enqueuing the message. `ChannelManager` installs `_authorize_inbound` as the
gate at construction time. `publish_inbound` is the only path that enqueues to
`bus.inbound`, so any message that reaches the bus is gated and the gate cannot
be bypassed once a message is published.

The channel contract is to be **pure transport**: publish unconditionally with
`is_dm` set and let the gate authorize — a channel should NOT re-implement
`is_allowed`/pairing in its handlers. **Telegram, Slack, Discord, and
WhatsApp are the reference implementations of this contract.** Several other
channels still pre-filter with their own `is_allowed` check and early-`return`
in their handlers (legacy behaviour, unchanged here — those messages never
reach the gate); migrating them to pure transport so they route through the
central gate is a follow-up. New channels should follow the
Telegram/Slack/Discord/WhatsApp model.

Pure transport applies to *sender authorization* only — the gate owns who is
allowed to talk to the bot. Channel-policy filters that decide *which
messages within an already-authorized conversation* get a response, such as
mention gating in group chats or an allowed-channels list, are unrelated to
authorization and stay channel-local. On top of the shared contract, Discord's
adapter applies two inbound normalizations of its own: it deduplicates
messages replayed by the gateway on reconnect, and it reassembles a long
message the Discord client split client-side before re-publishing it as one
`InboundMessage`.

The agent loop (`AgentLoop.run()`) consumes from `bus.inbound`. The
`InboundMessage.session_key` property returns `session_key_override` when set,
or `"{channel}:{chat_id}"` otherwise. This lets thread-scoped sessions (for
example, Slack threads) share a distinct session from the channel-level one.

### Outbound path

After completing a turn, the agent loop publishes one or more `OutboundMessage`
objects to `bus.outbound`. The `_dispatch_outbound` loop in `ChannelManager`
consumes these and applies a layered gating and transformation pipeline:

1. **Reasoning routing** — messages with `_reasoning_delta`, `_reasoning_end`,
   or `_reasoning` in metadata are routed only when `channel.show_reasoning`
   is true, and only channels that override `send_reasoning_delta` /
   `send_reasoning_end` render them (the default is a no-op).

2. **Progress gating** — `_progress` messages are passed through only when
   `channel.send_progress` is true. `_progress` messages with `_tool_hint=True`
   also check `send_tool_hints` (unless the channel renders structured tool
   payloads natively, in which case the gate is bypassed).

3. **Retry-wait gating** — `_retry_wait` messages are only delivered on the
   `websocket` channel (the webui shows a visible indicator; CLI/TUI users rely
   on the running indicator).

4. **Stream delta coalescing** — consecutive `_stream_delta` messages for the
   same `(channel, chat_id, _stream_id)` are merged into a single call via
   `_coalesce_stream_deltas`. This reduces API calls when the LLM generates
   faster than the platform can process. Coalescing stops at a stream-end
   marker or at the first message belonging to a different stream or target.

5. **Duplicate suppression** — for non-streaming final responses, the dispatcher
   computes a whitespace-normalized SHA1 fingerprint of the content and checks
   it against `_origin_reply_fingerprints` keyed by `(channel, chat_id,
   origin_message_id)`. Identical content for the same origin message is
   suppressed. Progress and streaming messages are exempt from this check.

6. **Delivery** — `_send_with_retry` dispatches to `_send_once`, which calls
   the appropriate channel method (`send_reasoning_end`, `send_reasoning_delta`,
   `send_delta`, or `send`). On failure it retries up to
   `config.channels.send_max_retries` times with exponential backoff delays of
   1s, 2s, and 4s.

### Channel adapter contract

Every channel is a subclass of `BaseChannel` with three abstract methods:

- `start()` — long-running async listener; pushes inbound messages to the bus
  via `_handle_message` (which calls `bus.publish_inbound`). Channels publish
  unconditionally and set `is_dm=True` when the message arrived in a private
  chat so the central gate can distinguish DM from group context.
- `stop()` — graceful shutdown.
- `send(msg: OutboundMessage)` — deliver a final response; raise on failure so
  the manager retries.

Adapters may transform content before delivery. Discord's `send` converts GFM
tables to bullet lists before chunking, since Discord's client does not render
markdown tables, and posts to forum and media channels as a new thread instead
of a plain message, since those channel types reject plain sends.

Two optional streaming methods default to no-ops: `send_delta(chat_id, delta,
metadata)` and `send_reasoning_delta(chat_id, delta, metadata)`. The
`supports_streaming` property returns `True` only when both `config.streaming`
is true and the subclass actually overrides `send_delta` (checked by identity
against `BaseChannel.send_delta`).

`is_allowed(sender_id)` is a policy method called by the central gate — NOT by
the channel itself. It checks: `"*"` in `allow_from`, exact match in
`allow_from`, and `is_approved(channel, sender_id)` from the pairing store.
Channels MUST NOT call `is_allowed` in their own handlers.

A channel registers its default configuration via the class method
`default_config()` which the onboarding flow uses to seed `config.json`.

### Audio transcription contract

When a channel transcribes incoming audio at the channel level, it must pass the
transcript text to the agent and **drop the audio path from `media`**. The raw
path is forwarded only when transcription fails, as a fallback for the
`interpret_audio` tool. This mirrors the TUI drag-drop path
(`durin/cli/dragdrop.py::transcribe_dragged_audio`) and the WhatsApp adapter
(`durin/channels/whatsapp.py`), and matches the agent loop's `audio_mode="auto"`
behaviour, which silently skips audio paths in `media`. Passing both the
transcript text and the raw path causes the model to invent a file path and call
`interpret_audio` on it — a hallucination the contract prevents.

The transcript is handed over as the **bare user message**, not wrapped in a
`[transcription: …]` marker. A marker reads to the model as a transcript of a
separate audio file it should open, so it narrates that it cannot access the
audio; the bare text reads as what the user said.

WhatsApp is the reference implementation of this contract. Channels that
transcribe locally (currently Telegram, Discord, Matrix, Feishu, and Weixin)
apply the same idiom: on transcription success, return an empty `media` list
and the transcript as the bare user message text; on failure, return the audio
path so the `interpret_audio` tool remains a usable fallback.

### WhatsApp bridge transport

Unlike every other channel, WhatsApp's `start()` does not talk to the platform
directly: `WhatsAppChannel` (`durin/channels/whatsapp.py`) supervises a
separate Go process — the bridge (`bridge/`) — that implements the WhatsApp
Web multi-device protocol via the [whatsmeow](https://github.com/tulir/whatsmeow)
library, and speaks to it over a loopback-only WebSocket
(`bridge_url`, default `ws://localhost:3001`).

**Transport and auth.** The bridge's WS server accepts exactly one client. The
first frame on a new connection must be an `auth` command carrying a shared
token (constant-time compared); anything else closes the socket. The token is
either the configured `bridge_token` or one generated on first use and
persisted at `<durin_home>/whatsapp-auth/bridge-token`, and is also passed to
the bridge subprocess as the `BRIDGE_TOKEN` environment variable. A new client
authenticating (e.g. after `WhatsAppChannel` reconnects) closes any previous
connection — newest client wins.

**v2 frame protocol.** Frame shapes are defined in `bridge/frames.go`; the
two families are:

| Python → bridge (`Command`) | Purpose | Key fields |
|---|---|---|
| `auth` | authenticate the socket (must be first frame) | `token` |
| `send` | send a text message | `to`, `text`, `id`, `reply_to` (quoted reply), `reply_to_participant` (quoted message's participant JID; required for a valid group quote, see below) |
| `send_media` | send an image/video/audio/document | `to`, `filePath`, `mimetype`, `fileName`, `id` |
| `typing` | presence hint; fire-and-forget, no ack | `to`, `state` (`composing`/`paused`) |

| Bridge → Python | Purpose | Key fields |
|---|---|---|
| `ack` | result of a `send`/`send_media` command | `id`, `ok`, `error` |
| `message` | inbound WhatsApp message | `pn`, `sender`, `content`, `id`, `isGroup`, `wasMentioned`, `media`, `timestamp`, `voice`, `quoted` |
| `status` | connection status, pushed on connect and on whatsmeow connect/disconnect events | `status` (`connected`/`disconnected`), `version` |
| `qr` | QR code payload (`qr` login mode) | `code` |
| `error` | protocol or delivery error | `error` |

`WhatsAppChannel.send` awaits the matching `ack` (with a timeout) before
returning, so the channel manager's retry-on-failure policy applies the same
way it does for every other channel's `send`. On the bridge side, inbound
whatsmeow events are handed off to a buffered outbound queue consumed by a
dedicated goroutine that writes to the WS connection, so a slow or stalled
Python client can never block whatsmeow's own event dispatch.

**Quoted replies and LID identities.** A WhatsApp quote's `ContextInfo.Participant`
must be the replied-to message's sender JID, never the chat JID — for a group
that's a participant JID, not the group JID. `WhatsAppChannel` tracks this by
caching each inbound `message_id → participant JID` (bounded, same
capacity/eviction as the dedup cache) and sending it back as
`reply_to_participant` when replying. If the mapping is unknown for a group
reply, the bridge sends a plain (unquoted) message rather than emit a quote
with a wrong `Participant`; DM replies always know their participant (the
peer JID) and are unaffected. Separately, `wasMentioned` detection compares
the message's `MentionedJID` list against *both* of the device's identities —
its phone JID and its LID (`store.Device.LID`) — because groups on WhatsApp's
LID rollout carry LID JIDs in `MentionedJID`, not phone JIDs. The Python side's
own phone/LID classification (`_handle_bridge_message`) parses the JID's
server suffix (`s.whatsapp.net` = phone, `lid` or `lid.whatsapp.net` = LID)
rather than substring-matching, since whatsmeow emits the bare `@lid` form.

**Supervision and the exit-code contract.** `WhatsAppChannel.start()` resolves
the bridge binary and hands it to `BridgeSupervisor`
(`durin/channels/whatsapp_bridge.py`), which spawns
`<binary> serve --port <port> --auth-dir <dir> --media-dir <dir>` with
`BRIDGE_TOKEN` set, and restarts it on crash with jittered exponential
backoff (starting at 2s, capped at 30s, reset after a stable run) — the same
backoff applies when the WebSocket connection between `WhatsAppChannel` and
the bridge drops, whether via an error or a clean close, so two gateways
racing for the same bridge don't tight-loop reconnecting against each other.
The bridge uses its exit code to distinguish a crash from a pairing or
configuration problem: `2` is a usage/config error (bad flags or a missing
`BRIDGE_TOKEN`); `3` means `serve` was invoked with no paired session; `4`
means whatsmeow's `LoggedOut` event fired during a run (the user unlinked the
device from their phone). `BridgeSupervisor` does **not** restart on any of
`2`, `3`, or `4` — restarting can't fix a bad config or a dead session — but
only `3`/`4` set `needs_login` (a config error isn't a pairing problem, and
`WhatsAppChannel.start()` uses `needs_login` to decide whether to stop the
channel cleanly rather than leave it for the channel-manager crash supervisor
(see "Channel crash supervision" above) to keep resurrecting). Re-pairing is
`durin channels login whatsapp`, which runs the bridge in `qr` mode in the
foreground and prints a scannable QR to the terminal.

Inbound media is written under `--media-dir` using a filename derived from
the message's stanza ID — attacker-controlled, since it arrives in the
inbound message itself — so the bridge sanitizes it to a safe charset
(`[A-Za-z0-9._-]`, substituting `_` otherwise; a random name if nothing
survives) and verifies the resulting path stays inside the media dir before
writing, refusing the write otherwise. `WhatsAppChannel` re-checks the same
containment on its side (`media` paths in the inbound frame must resolve
under `get_media_dir("whatsapp")`) as defense in depth against a compromised
or misbehaving bridge.

**Binary distribution.** The bridge ships as a prebuilt platform binary, not
Go source, and is **versioned and released independently of the durin
package** — it changes rarely, so a durin release that doesn't touch it never
rebuilds, re-publishes, or invalidates a user's cached binary. The version is
`BRIDGE_VERSION` in `durin/channels/whatsapp_bridge.py`; its own workflow
(`.github/workflows/bridge-release.yml`, triggered only by the
`whatsapp-bridge-v<version>` tag) cross-compiles each supported OS/arch and
attaches `durin-whatsapp-bridge-<os>-<arch>` plus a `checksums.txt` to a
dedicated GitHub Release. The committed pin file
`durin/channels/bridge_checksums.json` records the sha256 of each asset and
ships inside the wheel as ordinary package data; the release workflow rebuilds
the binaries reproducibly (pinned toolchain, `-trimpath -buildvcs=false`) and
fails if they don't match the committed pin, so what the wheel verifies always
matches what was published. `scripts/gen-bridge-checksums.sh` regenerates the
pin from source. `ensure_bridge_binary()` resolves the binary at first use:
reuse a cached copy under `<durin_home>/bridge/<BRIDGE_VERSION>/` if present,
otherwise download the asset from the `whatsapp-bridge-v<BRIDGE_VERSION>`
release and verify its sha256 against the bundled pin before making it
executable — a mismatch raises rather than running an unverified binary. A
source install with no matching pin falls back to `go build` from `bridge/`
with a local Go toolchain. `durin doctor` includes a `whatsapp bridge` check
that reports whether the binary is cached (never fails the run — the binary
self-installs on login or first start).

**Pairing.** WhatsApp follows the same pure-transport pairing model described
above: it publishes unconditionally with `is_dm` set and lets the central
gate authorize, generating pairing codes for unapproved DM senders exactly
like Telegram and Slack.

### Email conversation threading

The email channel (`durin/channels/email.py`) resolves each inbound message to
a thread and, depending on `threading_mode`, uses that resolution to scope the
session — mirroring the Slack thread-scoped session precedent
(`InboundMessage.session_key_override`, see "Inbound path" above). With
`threading_mode="thread"`, `_resolve_thread` computes a thread digest and sets
`session_key_override` to `email:{sender}:{digest}`; with
`threading_mode="sender"`, no override is set and `session_key` falls back to
`email:{sender}`, as before. The thread store (below) is updated identically
in both modes, so toggling `threading_mode` is a lossless config change.

**Thread resolution order** (`_resolve_thread`): the thread root is, in order,
(1) the first entry in the inbound message's `References` header, falling
back to `In-Reply-To` when `References` is absent; (2) a secondary index
keyed by the Outlook Thread-Index conversation prefix plus the normalized
subject is consulted whenever step (1) found nothing, *or* found a root the
thread store doesn't recognize (Exchange/Outlook rewrote or dropped
`References` on an internal hop) — a hit there replaces the resolved root; a
miss keeps the step-(1) root as-is when one was found, rather than
discarding it, since a References-derived root the store hasn't seen yet is
still better thread identity than starting a new thread; (3) if no root was
found by either step, a new thread rooted at the mail's own `Message-ID`, or
at a synthetic root derived from the IMAP UID when even `Message-ID` is
missing, so header-less mail is never folded into an unrelated thread. The
resolved root is hashed (`thread_digest`, from `durin/channels/email_threads.py`)
into the short digest used in the session key and the thread store. The
digest is a 16-hex-char SHA-256 prefix; a collision would require two
distinct roots to hash identically within the store's entry cap, a
probability negligible at this scale, so collision handling is deliberately
not implemented.

**Thread store** (`durin/channels/email_threads.py::ThreadStore`): a single
JSON file at `~/.durin/email/threads.json`, with the gateway's email channel
instance as its only writer, written atomically (temp file + rename). It is
pruned both at load and once a day: entries idle beyond the configured
max age are dropped, and if the store still exceeds the entry cap the
oldest-by-last-seen entries are evicted. A corrupt or unreadable file is
treated as an empty store rather than raised — replies degrade to unstitched
(no `In-Reply-To`/`References`), since the store only carries header-stitching
state; conversation content lives in durin sessions and is never affected.

**Outbound stitching invariant**: `send()` resolves the thread entry for a
reply from, in order, an explicit digest carried in the outbound metadata, the
latest known thread for the recipient address, or no entry (a fresh mail with
no reply context). Durin always generates its own `Message-ID` for the
outbound mail and, when replying within a known thread, records it via
`record_outbound` as the thread's new last link. When a thread entry
exists, the outbound `References` header is the stored chain with the
resolved `In-Reply-To` value appended if it isn't already the last entry —
`References` always ends with `In-Reply-To`. `Thread-Index` and
`Thread-Topic` are re-emitted verbatim when the store carries them, so
Outlook/Exchange clients keep grouping the conversation. The reply body is
sent as `multipart/alternative`: the raw markdown as `text/plain`, plus HTML
rendered from it; a render failure logs a warning and falls back to
plain-only.

After a successful SMTP send, `_append_to_sent` makes a best-effort copy of
the message into the mailbox's Sent folder over IMAP. The Sent folder is
auto-detected via `LIST` for the mailbox flagged `\Sent` (falling back to a
literal `"Sent"` if detection fails), and a failed detection is not cached, so
it is retried on the next send rather than giving up permanently.

**Inbound RFC 3834 guard**: any inbound message whose `Auto-Submitted` header
is present and not equal to `"no"` — bounces, out-of-office replies, vacation
autoresponders, and other automated mail — is dropped before it reaches the
agent loop, regardless of `threading_mode`.

### Pairing store

The pairing store (`durin/pairing/store.py`) is a small JSON file at
`<durin_home>/pairing.json` with two top-level sections: `"approved"` (channel
→ set of sender IDs) and `"pending"` (code → `{channel, sender_id,
expires_at}`). All mutations use `threading.Lock` plus `cross_process_lock`
over the file path for cross-process safety. Expired pending entries are
collected lazily by `_gc_pending` on every read-modify-write; there is no
background worker. `approve_code` moves an entry from pending to approved;
`deny_code` removes it; `revoke` removes an approved sender.

## 5. Key types and entry points

| Symbol | File | Role |
|---|---|---|
| `BaseChannel` | `durin/channels/base.py` | Abstract adapter base. Owns `_handle_message` (publishes to bus; no auth), `is_allowed` (policy called by the gate, not the channel), `supports_streaming`, `send`, `send_delta`, `send_reasoning_delta`, `send_reasoning_end`, `transcribe_audio`. |
| `ChannelManager` | `durin/channels/manager.py` | Lifecycle and dispatch coordinator. `_init_channels` discovers and instantiates. `_dispatch_outbound` applies gating, coalescing, dedup, and retry. `start_all` / `stop_all` manage the asyncio task tree. |
| `MessageBus` | `durin/bus/queue.py` | Two `asyncio.Queue` objects (`inbound`, `outbound`). `publish_inbound` runs the installed inbound-authorizer gate before enqueuing; `set_inbound_authorizer` wires the gate. |
| `InboundMessage` | `durin/bus/events.py` | Channel-to-loop event: `channel`, `sender_id`, `chat_id`, `content`, `media`, `metadata`, `session_key_override`. `session_key` property returns override or `"channel:chat_id"`. |
| `OutboundMessage` | `durin/bus/events.py` | Loop-to-channel event: `channel`, `chat_id`, `content`, `reply_to`, `media`, `metadata`, `buttons`. Metadata carries routing and flag keys such as `_progress`, `_stream_delta`, `_reasoning_delta`, `_retry_wait`. |
| `discover_all` | `durin/channels/registry.py` | Returns merged dict of built-in (pkgutil scan) + external (entry_points) channel classes. Built-ins shadow plugins of the same name. |
| `generate_code` | `durin/pairing/store.py` | Creates a pairing code (`ABCD-EFGH` format, 8 chars) for an unapproved DM sender and writes it to `pairing.json` with a TTL. |
| `approve_code` | `durin/pairing/store.py` | Moves a pending code to the approved set; returns `(channel, sender_id)` or `None` if expired or absent. |
| `is_approved` | `durin/pairing/store.py` | Read-only check: is `sender_id` in the approved set for `channel`? |
| `handle_pairing_command` | `durin/pairing/store.py` | Pure function dispatching `/pairing list|approve|deny|revoke` subcommands. Used by both CLI and `CommandRouter`. |
| `format_pairing_reply` | `durin/pairing/store.py` | Returns the user-facing string sent to an unapproved DM sender containing their pairing code. |
| `TelegramChannel` | `durin/channels/telegram.py` | Telegram adapter using `python-telegram-bot`. Implements streaming via in-place message edits. |
| `SlackChannel` | `durin/channels/slack.py` | Slack Socket Mode adapter. Implements streaming via in-place `chat.update` edits, thread-scoped sessions, quoted/forwarded-content extraction, and mentioned-thread auto-follow. |
| `WebSocketChannel` | `durin/channels/websocket.py` | WebSocket server channel that also hosts the embedded webui SPA. Handles token issuance, `websocket_requires_token`, and session/cron integration. |
| `EmailChannel` | `durin/channels/email.py` | IMAP+SMTP adapter. Polls for new messages and sends replies. |
| `WhatsAppChannel` | `durin/channels/whatsapp.py` | Supervises the Go bridge process (`bridge/`, whatsmeow) and speaks the v2 frame protocol to it over a loopback WebSocket. See "WhatsApp bridge transport" above. |
| `BridgeSupervisor` | `durin/channels/whatsapp_bridge.py` | Spawns and restarts the WhatsApp bridge binary; resolves/downloads/verifies it via `ensure_bridge_binary`. |

## 6. Configuration and surfaces

### Global channel settings (`channels.*`)

| Config key | Type | Default | Effect |
|---|---|---|---|
| `channels.send_progress` | bool | `true` | Deliver intermediate progress messages to channels. Per-channel override allowed. |
| `channels.send_tool_hints` | bool | `false` | Deliver tool-call hint messages (e.g. `read_file("…")`). Per-channel override allowed. |
| `channels.show_reasoning` | bool | `true` | Route model reasoning content to channels that implement the reasoning primitives. Per-channel override allowed. |
| `channels.send_max_retries` | int | `3` | Total delivery attempts (includes initial send). Range 0–10. |
| `channels.transcription_provider` | str | `"groq"` | Voice transcription backend (`"groq"` or `"openai"`). Overridden by the `transcription.*` section for local/HTTP modes. |
| `channels.transcription_language` | str or null | `null` | ISO-639-1 language hint for audio transcription (e.g. `"en"`). |

### Per-channel settings

Each channel section is stored as an extra field on `ChannelsConfig`. Common
keys accepted by most adapters:

| Key | Effect |
|---|---|
| `enabled` | Boolean gate; channel is only instantiated when `true`. |
| `allow_from` | List of allowed sender IDs, or `["*"]` for open access. Also accepted as `allowFrom`. |
| `streaming` | Enable streaming output (requires that the channel implements `send_delta`). |
| `token` | API token or bot secret for the channel backend. Supports `${secret:<name>}` references. |

Channel-specific extensions:

- **Telegram**: no additional permission fields beyond `allow_from`.
- **Discord**: `allow_channels` — list of Discord channel IDs allowed to
  trigger the bot (empty means all; a thread also matches its parent channel,
  so allowing a forum covers its posts). `group_policy` (`mention`/`open`),
  the read-receipt and working emojis with their delay, and `proxy*`. Sender
  authorization uses the standard `allow_from` + pairing flow via the central
  ingress gate. Gateway intents are **not configurable**: they are derived from
  the events the adapter handles — guilds, guild messages, direct messages and
  message content — because durin implements no member, presence, voice or
  reaction-event handler. A legacy `intents` key is ignored with a warning
  rather than silently dropped.
- **Slack**: `dm_enabled` (routing toggle for DMs), `group_policy`
  (`open`/`mention`/`allowlist`), `group_allow_from` (channel IDs for the
  allowlist policy), `open_channels` (rooms that reply to every message under
  any policy), and `thread_auto_follow` (answer follow-ups in a mentioned
  thread without re-mention). Sender authorization uses the standard
  `allow_from` + pairing flow via the central ingress gate.
- **Persona per channel** (any adapter): a `persona` key on the channel
  section sets the default persona for sessions born there; a
  `chat_personas` map (chat id → persona name) refines it per conversation.
  Resolution lives in `durin.personas.resolve` with precedence: cron
  override > session pick > chat map > channel default > global default.
- **Email**: `imap_host`, `imap_port`, `imap_username`, `imap_password`,
  `imap_mailbox`, `imap_use_ssl`, `smtp_host`, `smtp_port`, `smtp_username`,
  `smtp_password`, `smtp_use_tls`, `smtp_use_ssl`, `auto_reply_enabled`,
  `threading_mode` (`"thread"` | `"sender"`; see "Email conversation
  threading" above).
- **WhatsApp**: `bridge_url` (loopback WS URL for the Go bridge, default
  `ws://localhost:3001`), `bridge_token` (shared auth secret for the bridge
  socket; auto-generated and persisted under
  `<durin_home>/whatsapp-auth/bridge-token` when left empty), `group_policy`
  (`open`/`mention`). Sender authorization uses the standard `allow_from` +
  pairing flow via the central ingress gate (see "WhatsApp bridge transport"
  above).
- **WebSocket** (webui): `host`, `port`, `path`, `token`,
  `token_issue_secret`, `websocket_requires_token` (bool; default `true`),
  `allow_from` (default `["*"]`).

### Plugin channel registration

External channels are registered via the Python `entry_points` group
`durin.channels`. The entry point name becomes the channel's config key. A
plugin that declares:

```toml
[project.entry-points."durin.channels"]
mychannel = "mypkg.channels.mychannel:MyChannel"
```

will be discovered at startup and can be enabled with
`channels.mychannel.enabled = true` in `config.json`.

### CLI / TUI / webui surfaces

- **`/pairing list`** — show pending pairing requests with TTL remaining.
- **`/pairing approve <code>`** — approve an unapproved sender.
- **`/pairing deny <code>`** — reject and discard a pairing code.
- **`/pairing revoke <user_id>`** — remove an approved sender from the current
  channel; `revoke <channel> <user_id>` targets a specific channel.
- **Webui** — the WebSocket channel hosts the embedded single-page app at the
  configured `host:port/path`. Authentication is handled via `token` or
  `token_issue_secret` (reverse-proxy path).

### Dashboard channel services

A channel renders a typed, grouped settings form when its class overrides
`config_model()`; a field reaches that form only if it carries a `group` in
`json_schema_extra` or is marked `secret`. Channels that expose no fields fall
back to a single credential box.

Telegram, Slack and Discord additionally have a service module
(`durin/service/channels_<name>.py`) backing a guided panel. Each registers in
**both** `durin/service/catalog.py` and `durin/service/wiring.py` — a service
present in only one is served to tooling and silently 405s on the live gateway,
and a test freezes that invariant. All three expose a token `test` plus the
pairing quartet (list / approve / deny / revoke) over the channel-agnostic
pairing store. Slack adds workspace-channel listing and joining; Discord adds
guild-and-channel listing and a least-privilege OAuth invite URL. The Discord
service is REST-only against the configured token, so its panel keeps working
while the channel is stopped, and its `test` reports whether the bot may read
message content as `enabled | limited | disabled | unknown` — `limited` is the
normal state of an unverified app, which works until it passes 100 guilds.

Adding or changing a route means regenerating the contract
(`PYTHONPATH=<worktree> python scripts/gen_openapi.py`, then
`cd webui && bun run gen:api-types`); running the generator without
`PYTHONPATH` from a worktree imports the installed package and silently emits a
stale contract.

### Viewing non-websocket sessions in the webui

The webui sidebar lists sessions from all channels (Telegram, CLI, subagent,
etc.). Historically, clicking one returned a 404 because the rich per-session
JSONL transcript used by the thread endpoint is written only by the websocket
channel (`append_transcript_object` in `durin/channels/websocket.py`).

The thread endpoint (`GET /api/v1/sessions/{key}/webui-thread`) now falls back
to the **universal session history** when no JSONL exists: it reads the
OpenAI-format messages via `SessionManager.read_session_file` and converts them
to the webui `UIMessage` shape with `session_messages_to_ui_messages`
(`durin/utils/webui_transcript.py`). The payload carries `"readOnly": true` so
the frontend knows the session cannot be continued from the webui (the webui
would spawn a new websocket session, not resume the original channel). The
`ThreadShell` component disables the composer and shows a banner when the active
session's `channel` is not `"websocket"`.

The converter is a pure function (no I/O): media signing reuses the channel's
existing `_augment_transcript_user_media` callback, which HMAC-signs file paths
using the same per-process `_media_secret` as websocket sessions.

## 7. Curated rationale

**Why two queues instead of direct calls?** The agent loop processes one turn
at a time per session (holding a per-session asyncio lock). Direct calls from a
channel to the loop would either block the channel's listener or require the
loop to expose a thread-safe API. The queue pair inverts the dependency: both
sides push and pull at their own pace, and neither knows the other's
implementation. This also makes it straightforward to add new channels without
touching the loop code.

**Why do built-in channels shadow external plugins?** This prevents a
third-party package from accidentally (or maliciously) overriding a built-in
adapter by registering the same name. Operators who want to replace a built-in
adapter must fork the package, not just install a plugin.

**Why is duplicate suppression keyed to `origin_message_id`?** The same
response can be published more than once if a session has multiple active
consumers (for example, the agent pushes an `OutboundMessage` and a background
task also triggers a response for the same original message). Fingerprinting by
`(channel, chat_id, origin_message_id)` limits suppression to the scope of a
single inbound message and avoids silently dropping a legitimately different
response sent later in the same chat.

**Why is pairing a JSON file rather than a database table?** The pairing store
is designed for private-assistant scale: a handful of channels and a small
number of users. A JSON file under `cross_process_lock` is sufficient, requires
no migration, and survives gateway restarts without a daemon. Operators who need
LDAP or SSO-style access control implement a custom channel adapter with a
custom `is_allowed` policy; the central gate calls it uniformly.

**Why is authorization enforced at the bus rather than in each channel?**
Putting the gate at `MessageBus.publish_inbound` gives a single enforcement
point that runs for every message a channel publishes — a pure-transport channel
cannot publish an unauthorized message past it, even if it gets its own DM
detection wrong, and the pairing logic lives in one place instead of being
re-implemented per channel. (A channel that still pre-filters with its own
`is_allowed` and early-returns short-circuits before publishing, so it never
reaches the gate; that is the legacy pattern the pure-transport migration
removes — Telegram first, then Slack and Discord.)

**Why does stream coalescing key on `_stream_id` and not just `(channel,
chat_id)`?** Channels like Telegram forum topics or Discord threads can have
multiple independent streams open on the same `chat_id` simultaneously. Without
the `_stream_id` guard, deltas from stream B would be merged into the batch for
stream A, corrupting both messages.
