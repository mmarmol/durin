# OpenAI-compatible API

The gateway serves an OpenAI-compatible API at `/v1`. Any OpenAI client — the
official SDKs, `curl`, another agent, an app that already speaks the protocol —
can talk to durin by pointing its `base_url` at the gateway and using a durin
token as its `api_key`.

This is the surface to use when **another agent** should be able to ask durin
for things: durin keeps the conversation, runs its own tools, and answers.

Start the gateway first — the API lives inside it, there is no separate server
process:

```bash
durin gateway start
```

## Mint a token

The endpoint always requires a bearer token; there is no anonymous access, not
even from localhost. Issue one scoped to `chat:write` — the permission to hold
a conversation, and nothing else (it cannot read or administer sessions,
config, secrets, or skills):

```bash
durin auth token issue --scopes chat:write --label my-agent
```

The plaintext token is printed once and never stored — copy it now. Add
`--ttl 604800` (seconds) for a token that expires on its own. To see or remove
tokens later:

```bash
durin auth token list
```

```bash
durin auth token revoke <token-id>
```

## The contract

The API is OpenAI-shaped but **session-oriented**, and that difference matters:

- **Exactly one user message per request.** durin keeps the conversation
  server-side. Do not resend history — a request with more than one message is
  rejected with 400.
- **`session_id` picks the conversation.** Send it as a top-level field; the
  turn lands in the durin session `api:<session_id>`, which persists across
  requests and across gateway restarts, and shows up in the web dashboard like
  any other chat. Omit it and everything shares one `api:default` session.
- **`model` is optional.** If sent, it must match the id from `GET /v1/models`.
- **Client-defined tools are rejected.** durin runs its own tools inside the
  turn; a request carrying `tools`, `tool_choice`, `functions`, or
  `function_call` gets a 400 rather than silently ignoring them.
- **Images** travel as base64 `data:` URLs in `image_url` content parts. Remote
  image URLs are rejected — the gateway does not fetch URLs on a caller's
  behalf.
- **Other files** go through `multipart/form-data` with the fields `message`,
  `session_id`, and one or more `files` parts.

### curl

```bash
curl http://127.0.0.1:18790/v1/chat/completions -H "Authorization: Bearer $DURIN_TOKEN" -H "Content-Type: application/json" -d '{"messages":[{"role":"user","content":"summarize my open tickets"}],"session_id":"agent-billing"}'
```

### Python (official OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:18790/v1", api_key=DURIN_TOKEN)

reply = client.chat.completions.create(
    model="durin",
    messages=[{"role": "user", "content": "summarize my open tickets"}],
    extra_body={"session_id": "agent-billing"},
    timeout=180,
)
print(reply.choices[0].message.content)
```

`extra_body` is how the OpenAI SDK passes `session_id` through; a plain HTTP
client just puts it in the JSON body.

## Streaming

Set `stream: true` for server-sent events in the standard
`chat.completion.chunk` format. Use it for anything long: the stream stays open
for the **whole turn**, including the time durin spends running tools, so the
connection keeps producing data instead of looking dead to impatient proxies
and clients.

What the stream carries is the assistant's text as it is generated. Tool runs
happen server-side inside the turn and are not emitted as separate events — the
narration durin writes between tool calls arrives as ordinary content.

A successful stream ends with a `finish_reason: "stop"` chunk followed by
`data: [DONE]`. If the turn fails mid-stream, the last frame is an
`{"error": ...}` object and `[DONE]` is **not** sent, so a client can tell a
truncated stream from a complete one.

## Timeouts

Each request is capped by `gateway.api_request_timeout` (default `120.0`
seconds). Raise it for tool-heavy work:

```json
{
  "gateway": {
    "apiRequestTimeout": 300
  }
}
```

A non-streaming request that exceeds the cap answers `504`. Give the client a
timeout comfortably above the server's.

Requests to the same `session_id` are processed one at a time — a second call
waits for the first to finish. Use distinct session ids for genuinely
independent conversations.

## Reaching it from another machine

The API rides on the gateway's own bind and shares its `channels.websocket`
host/port, so exposing it is a gateway question, not an API-specific one. Two
things to get right:

1. **Terminate TLS in front of it.** Put a reverse proxy (Caddy, nginx,
   Traefik) in front and let it hold the certificate; the token travels in the
   `Authorization` header and should never cross a network in the clear. A
   tailnet or VPN works equally well.
2. **Bind beyond loopback deliberately.** The default `127.0.0.1` accepts only
   local callers. Change it only behind a proxy, firewall, or private network —
   the token is the only gate on the API itself.

## Prompt for the consuming agent

Paste this into the other agent's instructions, filling in the two values:

````markdown
# Talking to durin

You can reach **durin**, a persistent agent with its own tools and memory,
over an OpenAI-compatible HTTP API.

## Configuration
- `DURIN_BASE_URL` — e.g. `https://durin.example.com/v1`
- `DURIN_TOKEN` — a durin token with the `chat:write` scope; send it as the
  `api_key` / `Authorization: Bearer` value.
- `SESSION_ID` — a stable name for your conversation thread with durin, e.g.
  `agent-billing`. Reuse the same one across requests.

## Protocol — one critical difference from a normal OpenAI endpoint
- Send **exactly one user message per request**. Never resend conversation
  history: durin stores it server-side under your `session_id`. More than one
  message is rejected with HTTP 400.
- Always include `"session_id"` as a top-level JSON field. It keeps continuity
  across requests and restarts. Omitting it drops you into a shared default
  session used by everyone.
- Do not declare your own `tools`/`functions` — durin runs its own tools
  server-side and rejects the request.

Request shape:

```json
{
  "messages": [{"role": "user", "content": "<your single message>"}],
  "session_id": "<SESSION_ID>",
  "stream": false
}
```

## Operational notes
- durin may run tools before answering; a turn can take a few minutes. Set your
  client timeout to at least 180 s and prefer `"stream": true` for long tasks.
- HTTP 504 means durin exceeded its own timeout — retry once or simplify.
- Requests on one `session_id` are queued and run one at a time. Use different
  session ids for independent threads.
- Treat durin as a capable teammate: state the goal and the context, not
  micro-steps. It remembers earlier turns in your session, so you can refer
  back to previous requests and their results.
````
