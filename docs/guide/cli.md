# CLI & in-session commands

A command cheatsheet for driving durin day-to-day. For installing, configuring,
and where durin keeps its state on disk, see [install.md](install.md).

## Lifecycle

| Need | Command |
|---|---|
| First-time setup | `durin onboard` (re-runnable — keeps what's configured) |
| Snapshot of what's configured | `durin status` |
| Diagnose what's wrong | `durin doctor` (`--fix` safe auto-fixes, `--ping-model` real round-trip) |
| Show config | `durin config show` |
| Change one key | `durin config set agents.defaults.model glm-5.1` |
| Edit config in `$EDITOR` | `durin config edit` |
| Pull the latest build | `durin upgrade` |
| See what changed | `durin changelog` (`--all` for full history; `durin changelog <version>` for one) |
| Remove durin + data | `durin uninstall --purge` |

`status` = a factual snapshot; `doctor` = health checks with fixes.

## Day-to-day

```bash
durin agent              # rich TUI (default)
durin agent -m "hola"    # one-shot
durin gateway start      # background daemon: webui dashboard + channels + cron
durin gateway status     # is it running? where's the dashboard?
durin gateway stop
```

The gateway also serves an HTTP API: native chat on `/api/v1` (send, watch a
turn live, stop — see [durin's API](api.md)) and the OpenAI-compatible
`/v1/chat/completions` that remote agents and OpenAI clients talk to — see
[OpenAI-compatible API](openai-api.md). Callers authenticate with API tokens:

```bash
durin auth token issue --scopes chat:write --label my-app   # plaintext printed once
durin auth token list                                       # metadata only
durin auth token revoke <token-id>
```

The browser dashboard is served by `durin gateway` when
`config.gateway.webui_enabled` is true (default). `durin gateway status`
prints the URL. `durin status` also shows a `Dashboard` row (the same URL —
`gateway.public_url` when set, otherwise the websocket channel's host:port)
and, when the websocket channel has a login credential configured, a
`Web token` row for pasting into the webui login form — the effective value
the login gate accepts (`token_issue_secret` when set, otherwise `token`).

## Secrets & memory

```bash
durin secret set NAME --service SVC   # store a secret (API keys, channel tokens)
durin secret set NAME                 # rotate an existing secret's value (metadata kept)
durin secret list                 # list stored secrets, values masked
durin secret show NAME --reveal   # print the actual value
durin memory show <entity>        # inspect an entity page (e.g. person:marcelo)
durin memory history <entity>     # its git history; `diff` / `revert` to inspect or undo
durin memory rename <entity> <new-slug>  # give it a clearer key; every reference follows
durin memory dream                # run a memory consolidation (dream) pass now
durin memory stats                # recall / telemetry summary
durin memory forget <uri>         # delete one memory entry
```

`durin workflow recommendations` / `durin workflow apply <name> <id>` review and
apply workflow self-improvement suggestions; `durin mcp search|install|status`
manage MCP servers. Append `--help` to any group for its full command list.

## Approvals

When the agent wants to add or change an MCP server, install or edit a skill,
or install a skill's dependencies, and your settings require a person's
approval for it, you are asked in the chat. When nobody can be asked (a cron
job, dream, workflow or sub-agent, or a turn driven by an API token), or you
did not answer in the chat, the request is recorded instead of run, and these
commands are where it waits. A shell command that needs approval never waits
here: in a chat you are asked, and elsewhere it is refused.

```bash
durin approvals                # list pending records (--all for resolved too)
durin approvals approve <id>   # approve and run it — needs a real terminal
durin approvals reject <id>    # reject it — needs a real terminal
durin approvals discard <id>   # delete the record without deciding it
```

`approve` and `reject` refuse to run without a real terminal, and the agent's
shell tool never runs them, so the agent cannot approve its own request. Each
decided record keeps who decided it: you on the command line, a click or reply
in the chat, or the skills judge. A request recorded by an earlier durin
version is listed as `legacy:mcp` or `legacy:skills` and can only be discarded;
ask the agent again if it is still needed.

A pending request expires 14 days after it was filed and can no longer be
decided; ask the agent again if it is still wanted. A resolved record (decided
or expired) is kept for 30 days, then pruned. Expiry and pruning happen when
you decide a request, when you list them, and once when the gateway starts.

## Inside the TUI

- `/sessions` — modal picker over saved sessions (Esc to cancel)
- `/model` or `Ctrl+L` — modal picker over configured presets
- `/memory list|show|search|drill` — inspect the agent's memory
- `/remember <fact>` / `/forget <id>` — author memory directly
- `/compact [hint]` / `/copy` / `/name <name>` — session ergonomics
- Drag-and-drop a file path into the input to attach it
- Attach or record audio — transcribed to text locally before reaching the
  agent (`[stt]`/`[voice]` extras; see [install.md](install.md)).
  Default engine: Parakeet TDT v3 (~30× real-time on CPU, 25 European
  languages). Use `sensevoice` for Chinese/Japanese/Korean, or configure a
  cloud provider (Groq/OpenAI). In the TUI: drag an audio file or `/voice`
  to record. In the webui: attach a clip or use the 🎙 mic button.
- `@<prefix>` — fuzzy-complete a workspace file
- `!cmd` / `!!cmd` — shell shortcut (publishes / silent)
- `Alt+Enter` — newline; `Enter` — submit; `Esc` — cancel turn (also with `unified_session` on)
