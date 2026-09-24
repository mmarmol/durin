# durin's API

The gateway serves an HTTP API under `/api/v1`: the same surface the web
dashboard uses to manage sessions, memory, skills, schedules, workflows and
settings — and to **chat**. Any program can send durin a message, watch the
turn as it runs (text, reasoning, tool calls), catch up after a disconnect,
and stop it.

It is served on the websocket channel's host and port (`channels.websocket`,
default `127.0.0.1:8765` — the dashboard's address), not on `gateway.port`,
which only answers `/health`. Start the gateway first:

```bash
durin gateway start
```

The full route list, with request and response schemas, is the OpenAPI
contract at `contract/openapi-v1.json` in the repository, generated from the
code. This guide covers what the contract cannot: chatting, the event stream,
and how a client should use them.

Looking to plug in a client that already speaks OpenAI? Use the
[OpenAI-compatible API](openai-api.md) instead: a single request per message,
text only.

## Tokens

Every `/api/v1` route requires a bearer token (`Authorization: Bearer <token>`).
Issue one with only the scopes the program needs:

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
sessions (`slack:…`, `telegram:…`) can be read but not written to.

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
| `media` | optional images/documents: `[{"data_url": "data:image/png;base64,…", "name": "chart.png"}]` |
| `steer` | `true` to inject the message into the turn already running instead of queueing it |
| `client_msg_id` | your id for the message (≤ 64 characters); minted and returned when you omit it |

If a turn is already running, the message waits and enters the conversation
when it finishes (`message_queued`, then `queued_consumed`), unless it is a
`steer`. Errors are `application/problem+json`: `401` no token, `403` missing
scope or a sender outside `channels.websocket.allowFrom`, `413` body over
`channels.websocket.max_message_bytes`, `422` invalid key or message.

### Watch the conversation

```bash
curl -N http://127.0.0.1:8765/api/v1/sessions/websocket:report-42/events \
  -H "Authorization: Bearer $DURIN_TOKEN"
```

A server-sent event stream of everything that happens in the conversation,
from whoever drives it. It lives as long as you hold it; while nothing
happens, a `: keepalive` comment arrives every 15 seconds.

### Stop a turn

```bash
curl -X POST http://127.0.0.1:8765/api/v1/sessions/websocket:report-42/stop \
  -H "Authorization: Bearer $DURIN_TOKEN"
```

Answers `{"stopped": <tasks cancelled>}`; watchers see `turn_end` with
`outcome: "stopped"`. Disconnecting from the stream never stops a turn — this
route is how you do.

### Send and stream in one call

For a script that wants the answer to one message, add
`Accept: text/event-stream` to the send request (the token needs both
`chat:write` and `sessions:read`). The response is the event stream, and it
ends by itself after the `turn_end` of the turn that answers your message. If
the connection drops, the turn still finishes; catch up from the history.
Commands (`/status`, `/stop`, …) are refused in this form (`422`) — they answer
without a turn; send them with the plain form.

## Events

Each event is `event: <name>` plus a JSON `data:` line that repeats the name in
its `event` field. They are exactly the frames the dashboard receives over its
WebSocket.

| Event | Meaning |
|---|---|
| `user` | a message sent to the conversation (by you, another client or the dashboard): `text`, `client_msg_id`, `origin: "api"` when a token sent it |
| `delta` / `stream_end` | the reply's text as it is generated (`text`, `stream_id`); `stream_end` closes a segment |
| `message` | a complete reply (`text`, `id`); with `kind: "tool_hint"` a tool call starting, with `kind: "progress"` a progress notice or a tool call finishing |
| `reasoning_delta` / `reasoning_end` | the model's reasoning, only when the channel's `show_reasoning` is on |
| `turn_end` | the turn is over — every turn sends exactly one: `outcome` is `completed`, `stopped` or `failed`; `client_msg_id` names the message that opened it |
| `goal_status` | `status: "running"` (with `started_at`) or `"idle"` |
| `goal_state` | the state of an active goal |
| `message_queued` / `queued_consumed` | your message is waiting behind a running turn / the turn took it (`client_msg_ids`) |
| `api_status` | the model provider is being retried |
| `session_updated` | the conversation's title or metadata changed |
| `lagged` | your stream fell too far behind and was closed (see below) |

Tool activity rides in `tool_events` on those `message` events: one entry per
tool call with `phase` (`start` on a `tool_hint`, then `end` or `error` on a
`progress`), `call_id`, `name`, `arguments`, and `result` or `error` once it
finishes. Match a call's start and end by `call_id`.

**Ignore events you don't recognise** — new ones may be added.

## Client patterns

**Open the stream before you send.** Events that happen before you subscribe
are not replayed, so subscribe first, then send (or use the one-call form,
which does both in the right order).

**Reattach after a disconnect.** Reopen `events`, then read what you missed
from the history: `GET /api/v1/sessions/{key}/webui-thread` (the conversation
as the dashboard shows it, paged newest-first with `?before=<cursor>`) or
`GET /api/v1/sessions/{key}/messages` (the raw session). On reopen, a turn
still in flight announces itself with `goal_status: running`. A turn's output
is recorded even while nobody is watching.

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
confirms it).

**Queue or steer.** A message sent while a turn runs waits for it to finish;
with `steer: true` it is injected into the running turn as guidance instead.

**Browsers.** `EventSource` cannot send an `Authorization` header; read the
stream with `fetch` and a stream reader (or an SSE library that supports
headers).

## What a token cannot do

A message sent with a token comes from a program, not from the person at the
dashboard. A turn it opens or joins therefore does **not** carry the authority
to approve privileged actions: installing an MCP server, or importing, editing
or installing dependencies for a skill. durin records such a request instead of
running it, and it waits in `durin approvals` for a person to act on. Asking
the person (or program) a question and waiting for the answer still works.

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
turn proxy response buffering off (durin sends `X-Accel-Buffering: no` for
nginx) and allow long-lived responses; the 15-second keepalives keep idle
streams open. See [Configuration](configuration.md) for `channels.websocket`,
`token_issue_secret` and `gateway.public_url`.
