---
name: mcp
description: Connect durin to external MCP servers — search the registry, install a hit (or add a server by URL/command), get credentials and OAuth sign-in right, and manage server lifecycle. Use when the user wants a new integration via MCP ("connect durin to Jira", "add an MCP server", "install the github MCP"), hands you an MCP endpoint or config, or an installed server needs auth, reconnecting, or removal.
metadata: {"durin":{"emoji":"🔌"}}
---

# MCP servers

An MCP server wires a new tool source into the agent. Two tools drive everything:
`mcp_search` (read-only discovery) and `mcp_manage` (gated writes). Never edit MCP
config files yourself.

## Flow — install from the registry

1. **Search.** `mcp_search(query="jira")` returns ranked hits, each with a `ref`.
   The default catalog is the verified tier behind a popularity floor; pass
   `include_all=true` to include community/unverified servers — tell the user
   when you widen the net. Search never installs.

2. **Pick with the user.** Several plausible hits → show them (name + ref) and ask
   via `ask_user_question`.

3. **Install through the gate.** `mcp_manage(action="install", ref=<ref>)`.
   Under the default `install_policy: approve` the first call returns a
   **dry-run preview** — review it with the user, then call again with
   `confirm="true"`. `never` refuses (relay that); `auto` proceeds directly.
   - `prefer` is `"remote"` by default; pass `"local"` for the stdio package
     (npx / uvx / docker) when the user wants it or no remote exists. A missing
     local runtime comes back as a `runtime_plan`: auto-installable commands run
     through the exec gate, otherwise relay the manual install to the user.

4. **Credentials are the human's, never yours.** You never supply, invent, or
   forward a credential value.
   - Token/API-key servers: `request_secret(name=..., service=...)` lets the user
     provide it into durin's secret store; server config then carries a
     `${secret:NAME}` reference, resolved only at spawn time. The web dashboard's
     MCP panel install form collects the same inputs.
   - OAuth servers: a result with `needs_oauth: true` (status `needs_auth`) means
     the user must sign in out of band — `durin mcp login <server>` in a terminal,
     or the sign-in button in the dashboard's MCP panel. Agent runs are headless:
     you never open a browser or touch the authorization code.

5. **Verify.** Read the install result's `status`. `connected` → the server's
   tools are registered as native tools; exercise one to prove the integration.
   `needs_auth` → step 4. `failed` → diagnose (runtime, URL, credentials), then
   `mcp_manage(action="reconnect", name=...)`.

## Adding a server that is in no registry

The user gives you an endpoint URL or a command instead of a registry ref:
`mcp_manage(action="add", name=<name>, config=<MCPServerConfig JSON>)` — same
dry-run → confirm gate. `action="update"` edits an existing server's config the
same way (e.g. raise a timeout, change the URL).

## Lifecycle

`mcp_manage(action=..., name=...)` with `remove`, `enable`, `disable`, or
`reconnect` — these are not gated. A 401 or OAuth error on a tool call mid-run
means the server needs re-auth: point the user at `durin mcp login <server>`
(or the dashboard sign-in) instead of retrying the call.

## Rules

- The install gate is a real security control: adding a server hands an external
  party a tool surface inside the agent, so an injected prompt must never slip one
  in silently. Surface every dry-run preview and refusal to the user verbatim;
  do not work around the gate.
- Prefer verified-tier hits; when the user picks a community server, make sure
  they know what they are trusting.
- Secrets are entered by the human (OAuth login, `request_secret`, or the
  dashboard form) and live in the secret store as `${secret:...}` references —
  never inline plaintext in a server config, never echoed into chat.
