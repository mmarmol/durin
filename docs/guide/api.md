# durin's API

The gateway serves an HTTP API under `/api/v1`: the surface the web dashboard
uses to manage sessions, memory, skills, schedules, workflows and settings. It
also lets any program **chat** in the dashboard's conversations: send durin a
message, watch the turn as it runs (text, reasoning, tool calls), catch up
after a disconnect, and stop it.

It is served on the websocket channel's host and port (`channels.websocket`,
default `127.0.0.1:8765` — the dashboard's address), not on `gateway.port`,
which only answers `/health`.

The API exists only while the websocket channel runs. The gateway turns that
channel on by itself while the dashboard is enabled (`gateway.webui_enabled`,
the default), unless `channels.websocket.enabled` is explicitly `false`; if you
switched the dashboard off, set `channels.websocket.enabled` to `true`.

Start the gateway first:

```bash
durin gateway start
```

The full route list, with request and response schemas, is the OpenAPI
contract at `contract/openapi-v1.json` in the repository, generated from the
service route table. The event stream, `GET /api/v1/health`, the webhook
ingress (`POST /api/v1/hooks/{hook}`) and the MCP OAuth callback are mounted
outside that table, so the contract does not list them. This guide covers what
the contract cannot: chatting, the event stream, and how a client should use
them.

Looking to plug in a client that already speaks OpenAI? Use the
[OpenAI-compatible API](openai-api.md) instead: one request per message,
answered with text only (no tool or progress events).

## Tokens

Every `/api/v1` route requires a bearer token (`Authorization: Bearer <token>`),
except `GET /api/v1/health`, the webhook ingress (which checks its own secret
header) and the MCP OAuth callback. Issue one with only the scopes the program
needs:

```bash
durin auth token issue --scopes chat:write,sessions:read --label my-app
```

| Scope | For chat, it allows |
|---|---|
| `chat:write` | sending messages and stopping a turn |
| `sessions:read` | watching the event stream and reading conversation history |

`durin auth token issue --help` lists every scope. The plaintext token is
printed once; `durin auth token list` and `durin auth token revoke <id>`
manage them. Add `--ttl <seconds>` for a token that expires on its own.

## Chat

### Conversations

API conversations **are** dashboard conversations. A conversation's key is
`websocket:<id>`, where `<id>` is 1–64 characters of letters, digits, `_`, `-`
or `:`. Pick any new id to start one — the first message creates it — or reuse
the key of an existing dashboard chat (`GET /api/v1/sessions` lists them). The
conversation shows up in the dashboard's sidebar, can be continued from either
side, and can be watched live from both; messages sent through the API are
labelled "Sent through the API".

Only dashboard conversations accept messages from the API. Other channels'
sessions (`slack:…`, `telegram:…`) can be read; a token with `channels:write`
can also post into their conversation as durin with `POST /api/v1/channels/post`
(the post is recorded in that session unless `record` is `false`; no turn
runs).

### Send a message

```bash
curl -X POST http://127.0.0.1:8765/api/v1/sessions/websocket:report-42/messages \
  -H "Authorization: Bearer $DURIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content": "summarize my open tickets"}'
```

Answers `202` at once — the turn runs on the server, detached from the request:

```json
{"key": "websocket:report-42", "client_msg_id": "6f1c…"}
```

| Field | Meaning |
|---|---|
| `content` | the message text (may be empty when `media` is attached) |
| `media` | optional images, documents, audio or video: `[{"data_url": "data:image/png;base64,…", "name": "chart.png"}]`; a refused attachment answers `422` with the cause in `details.reason`. Each file is saved under a short unique prefix plus its `name`, so the agent can tell your files apart. Attached audio is transcribed into the message text. With `transcription.mode: off` the clip stays attached for the model; a clip that is not transcribed otherwise (transcription disabled or failed) stays attached and is named in the text |
| `steer` | `true` to inject the message into the turn already running instead of queueing it |
| `client_msg_id` | your id for the message (≤ 64 characters); minted and returned when you omit it |

If a turn is already running and the message is not a `steer`, it waits
(`message_queued`) and the same turn takes it after answering
(`queued_consumed` lists your `client_msg_id`); to find the turn that answers
it, match on that, not on `turn_end.client_msg_id`. Errors are
`application/problem+json`: `401` no token, `403` missing scope or a sender
outside `channels.websocket.allow_from` (a token sends as `api:<token id>`),
`413` body over `channels.websocket.max_message_bytes`, `422` invalid key or
message.

### Watch the conversation

```bash
curl -N http://127.0.0.1:8765/api/v1/sessions/websocket:report-42/events \
  -H "Authorization: Bearer $DURIN_TOKEN"
```

A server-sent event stream of everything that happens in the conversation,
from whoever drives it. It lives as long as you hold it, or until the gateway
stops or restarts, which ends it; reattach then (see
[Client patterns](#client-patterns)). While nothing happens, a `: keepalive`
comment arrives every 15 seconds.

### Stop a turn

```bash
curl -X POST http://127.0.0.1:8765/api/v1/sessions/websocket:report-42/stop \
  -H "Authorization: Bearer $DURIN_TOKEN"
```

Answers `{"stopped": <tasks cancelled>}`; watchers see `turn_end` with
`outcome: "stopped"`. Disconnecting from the stream never stops a turn — this
route is how you do. It also stops a turn of the
[OpenAI-compatible API](openai-api.md): use the key `api:<session_id>`.

### Send and stream in one call

For a script that wants the answer to one message, add
`Accept: text/event-stream` to the send request (the token needs both
`chat:write` and `sessions:read`). The response is the event stream, and it
ends by itself after the `turn_end` of the turn that answers your message. If
the connection drops, the turn still finishes; catch up from the history. A
gateway stop or restart ends the stream early, without that `turn_end`; the
gateway answers the message once it is running again, so reattach and catch
up from the history.
Commands (`/status`, `/stop`, …) are refused in this form (`422`) — they answer
without a turn; send them with the plain form.

## Events

Each event is `event: <name>` plus a JSON `data:` line that repeats the name in
its `event` field. They are the frames the dashboard receives over its
WebSocket for the conversation, without the voice-mode `voice_*` frames. Like
every dashboard connection, the stream also gets the gateway-wide
`runtime_model_updated`, `dream_progress` and `concurrency_snapshot` frames.

| Event | Meaning |
|---|---|
| `user` | a message sent to the conversation (by you, another client or the dashboard): `text`, `client_msg_id`, `origin: "api"` when a token sent it |
| `delta` / `stream_end` | the reply's text as it is generated (`text`, `stream_id`); `stream_end` closes a segment |
| `message` | a complete reply (`text`, `id`); with `kind: "tool_hint"` a tool call starting, with `kind: "progress"` a progress notice or a tool call finishing |
| `reasoning_delta` / `reasoning_end` | the model's reasoning, only when the channel's `show_reasoning` is on |
| `turn_end` | the turn is over — every turn sends exactly one: `outcome` is `completed`, `stopped` or `failed`; `client_msg_id` names the message that opened it |
| `goal_status` | `status: "running"` (with `started_at`) or `"idle"` |
| `goal_state` | the conversation's live state in `goal_state`: whether a sustained goal is `active` (with its `objective`), plus the agent `mode` when not the default, and the `pending_question` or `pending_approval` a turn waits on |
| `message_queued` / `queued_consumed` | a message is waiting behind a running turn (`client_msg_id`) / the turn took it (`client_msg_ids`) |
| `api_status` | the model provider is being retried (`status.kind: "retry_wait"`), or durin stopped retrying it (`giving_up`, `exhausted_persistent`; `status.final: true`) |
| `session_updated` | the conversation's title or metadata changed |
| `lagged` | your stream fell too far behind and was closed (see below) |

Tool activity rides in `tool_events` on those `message` events: one entry per
tool call with `phase` (`start` on a `tool_hint`, then `end` or `error` on a
`progress`; a long call such as a sub-agent or a workflow also sends `running`
updates), `call_id`, `name`, `arguments`, and `result` or `error` once it
finishes. The same `call_id` arrives more than once: keep the latest entry per
`call_id`, and ignore names and phases you don't recognise.

**Ignore events you don't recognise** — new ones may be added.

## Client patterns

**Open the stream before you send.** Events that happen before you subscribe
are not replayed, so subscribe first, then send (or use the one-call form,
which does both in the right order).

**Reattach after a disconnect.** Reopen `events`, then read what you missed
from the history: `GET /api/v1/sessions/{key}/webui-thread` (the conversation
as the dashboard shows it, newest page first; for the page before, pass the
response's `data.prevCursor` as `?before=`, and a `null` cursor means you have
reached the start) or `GET /api/v1/sessions/{key}/messages` (the raw
session). On reopen, a turn still in flight announces itself with
`goal_status: running`. A turn's output is recorded even while nobody is
watching.

**Treat streamed text as a preview.** When the turn ends, the history holds
the authoritative reply; re-read it if your stream dropped text (see
`lagged`).

**Keep reading.** Each stream buffers up to 8 MiB for a slow reader. Past
that, text fragments are dropped first; if a state event (a tool call, a
`turn_end`) still does not fit, the stream ends with `lagged` — reattach and
catch up from the history. A slow reader never slows the turn or other
watchers.

**Answer the agent's questions.** When durin needs input it calls the
`ask_user_question` tool: a `tool_events` entry with `phase: "start"`,
`name: "ask_user_question"` and the question in `arguments`. Answer by sending
a plain message; it goes straight into the waiting turn (`queued_consumed`
confirms it). While a stream or a dashboard tab watches the conversation, the
turn waits up to `agents.defaults.ask_user_answer_timeout_s`; with nobody
watching it stops waiting after 30 seconds. Past that the turn ends with the
question pending, and your next message answers it in a new turn — so keep a
stream open while a turn may ask.

**Queue or steer.** A message sent while a turn runs waits until that turn has
answered, and the same turn then takes it (`queued_consumed` lists your
`client_msg_id`) — match on that, not on `turn_end.client_msg_id`. With
`steer: true` it is injected into the running turn as guidance instead.

**Browsers.** The gateway sends no CORS headers, so a web page can call it only
from the gateway's own origin or through a proxy. `EventSource` cannot send an
`Authorization` header; read the stream with `fetch` and a stream reader (or an
SSE library that supports headers).

## What a token cannot do

A message sent with a token comes from a program, not from the person at the
dashboard. A turn it opens or joins therefore does **not** carry the authority
to approve privileged actions: installing an MCP server, or importing, editing
or installing dependencies for a skill. A request that would need a person's
approval is recorded instead of run, and it waits on the dashboard's Pending
page and in `durin approvals` for a person to act on; what
`install_policy: auto`, the skills judge or a clean scan already allows still
runs. The approval decision route (`POST /api/v1/approvals/{id}/decision`)
refuses API tokens: only the dashboard session decides a request there. The
domain write routes are a separate matter: they act with the operator
authority their scope grants, without an approval request — a token with
`mcp:write` can add an MCP server, and one with `skills:write` can install a
quarantined skill through the skills routes. Such a change is recorded as the
token's (an operator's), never as the person's. A shell command that would
need the person's approval is refused. A message sent with a token never
answers an approval request the conversation is waiting on, even "yes": only
the person can. Asking the person (or program) a question and waiting for the
answer still works.

## A Python example

Send a message and follow its turn with `httpx`:

```python
import json
import httpx

BASE = "http://127.0.0.1:8765/api/v1/sessions/websocket:report-42"
HEADERS = {"Authorization": f"Bearer {DURIN_TOKEN}", "Accept": "text/event-stream"}

with httpx.stream("POST", f"{BASE}/messages", headers=HEADERS,
                  json={"content": "summarize my open tickets"}, timeout=None) as r:
    event = None
    for line in r.iter_lines():
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            frame = json.loads(line[len("data: "):])
            if event == "delta":
                print(frame["text"], end="", flush=True)
            elif event == "message" and frame.get("tool_events"):
                for tool in frame["tool_events"]:
                    print(f"\n[{tool['phase']}] {tool['name']}")
            elif event == "turn_end":
                print(f"\n({frame.get('outcome')})")
```

## Reaching it from another machine

The API listens where the dashboard does. To reach it remotely, bind the
websocket channel to an interface (`channels.websocket.host`), put it behind a
reverse proxy with HTTPS, and keep tokens scoped and revocable. For streams,
the proxy must not buffer responses: durin sends `X-Accel-Buffering: no`, which
nginx honours; with any other proxy, turn response buffering off. Allow
long-lived responses; the 15-second keepalives keep idle streams open. See
[Channels](channels.md#web--dashboard-websocket) for `channels.websocket`,
including `token` and `token_issue_secret` (binding `0.0.0.0` or `::` requires
one of them), and [Configuration](configuration.md#gateway) for
`gateway.public_url`.
