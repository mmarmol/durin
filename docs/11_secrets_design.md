# Secrets subsystem — design

> Status: approved design, implementation starting 2026-05-22.
> Scope of first delivery: Phase 1 + Phase 2. Phase 3 deferred.

## 1. Problem

Today every API key / token is stored **as plaintext, inline** in
`~/.durin/config.json.d/providers.json` (and channel/search sections).
Consequences:

- `durin config show`, a screenshot, a backup, an accidental paste of
  the config — all leak live credentials.
- When the agent runs a script via `exec` that needs a credential,
  there is no path to provide it without the value passing through the
  model context or the chat.
- Skills that need credentials have no mechanism at all.

The goal is **isolation, not encryption**. A 600-mode file is not
meaningfully more secure than today's config — both are user-readable
plaintext in `$HOME`, and the agent process needs the value in clear to
function anyway. What actually moves the needle:

1. The secret value lives **only** in the store and in the `env` of the
   specific subprocess that needs it — never in `config.*`, never in
   the model context, never in chat.
2. Config files become safe to share / diff / screenshot.
3. The agent can use a credential without ever receiving its value.

## 2. Prior art (validated 2026-05-22)

- **hermes** — secrets in a separate `~/.hermes/.env`, plus a static
  curated registry (`OPTIONAL_ENV_VARS`): each known var has
  `description`, `category` (`provider`/`tool`/`messaging`/`skill`),
  and `tools` (which tools it enables). Plus `redact_secrets` — scrubs
  values from tool output before the model/user see them. Good
  classification, but **static and curated** — only covers known vars.
  Also has "credential pools": multiple credentials per provider with
  rotation.
- **openclaw** — config holds a `SecretRef {source, provider, id}`,
  never the value; sources are `env` / `file` / `exec`. A target
  registry knows every config path that holds a secret; an
  exec-resolution-policy gates whether `exec`-sourced secrets resolve
  in a given context. Rich indirection, **no semantic "purpose"**.
- **pi-agent** — `resolveConfigValue`: a config value is a literal, an
  env var name, or `!<command>`. Minimal, no store, no classification.

None does cross-skill reuse, per-agent scoping, or runtime
classification of an agent-requested secret. We take hermes's
"classify by purpose" idea and make it dynamic, plus openclaw's
"reference, not value" indirection.

## 3. Model

Two orthogonal axes — kept separate on purpose:

- **`service`** — *what the secret is* (classification). Used to
  discover and reuse. **Not unique**: many secrets may share a service
  (work vs personal Atlassian token; several keys for one provider).
- **`scope`** — *who may use it* (authorization / isolation).

A secret is identified by a unique **`name`**. Config and skills always
reference a secret by `name` (unambiguous). `service` is for discovery
and the agent-request flow only.

### 3.1 Store

`~/.durin/secrets.json`, mode `0600`, **outside** `config.json.d/` so
the config tree stays shareable.

```json
{
  "_version": 1,
  "secrets": {
    "ATLASSIAN_WORK": {
      "service": "atlassian",
      "account": "work",
      "description": "Jira/Confluence API token (work)",
      "value": "…",
      "scope": ["exec", "skill:*"],
      "origin": "user",
      "created_at": "2026-05-22T12:00:00Z"
    },
    "OPENAI_MAIN": {
      "service": "provider:openai",
      "account": "main",
      "description": "OpenAI API key",
      "value": "…",
      "scope": ["provider:openai"],
      "origin": "migration",
      "created_at": "…"
    }
  }
}
```

Entry fields:

| field | required | meaning |
|---|---|---|
| `name` (map key) | yes | unique id; `^[A-Z][A-Z0-9_]*$` (env-var-safe) |
| `value` | yes | the secret |
| `service` | yes | classification tag, non-unique |
| `account` | no | distinguisher within a `service` |
| `description` | no | human text |
| `scope` | no | consumer tags (see 3.3); empty = config-reference only |
| `origin` | yes | `user` / `wizard` / `migration` / `agent` |
| `created_at` | yes | ISO-8601 |

### 3.2 References in config

A config string field that held a secret now holds a **reference**:

```
providers.openai.apiKey = "${secret:OPENAI_MAIN}"
```

Grammar: the **entire field value** is `${secret:<NAME>}` with `NAME`
matching `[A-Z][A-Z0-9_]*`. No partial interpolation inside text — the
field either *is* a reference or is a literal. Partial interpolation
invites leaks and ambiguity.

`durin config show` prints the `${secret:…}` literal, never the value.

### 3.3 Scope — consumer tags

`scope` is a list of consumer tags. A secret only flows to a consumer
whose tag is present:

| tag | consumer |
|---|---|
| `provider:<name>` | resolved into that provider's client config |
| `web-search` | the web-search tool's backend key |
| `channel:<name>` | a channel's credential |
| `exec` | injected into the `env` of `exec`/shell subprocesses |
| `skill:<name>` / `skill:*` | injected when that skill (or any) runs |
| `agent:<id>` / `agent:*` | reserved for multi-agent (see 6) |

Empty `scope` = the secret is resolvable **only** through an explicit
`${secret:NAME}` config reference and never auto-injected anywhere.

## 4. Resolution

### 4.1 Config-field references — lazy

The config loader does **not** substitute references. `Config` keeps
`"${secret:OPENAI_MAIN}"` in memory, so `model_dump()`, telemetry, and
logs never carry the value. The consumer resolves at the point of use:

```python
key = secret_store.resolve(cfg.providers.openai.api_key,
                           consumer="provider:openai")
```

`store.resolve(value)`:
- not a reference → return `value` unchanged (literal still allowed).
- reference → look up `NAME`; if missing → raise `SecretNotFoundError`;
  else return the value.

Config-field resolution does **not** check `scope`: a `${secret:NAME}`
reference written into config *is* the authorization for that field.
`scope` gates *auto-injection* (the `exec` env, Phase 2) and the agent
flow (Phase 3) — `store.collect_for(consumer)` returns the secrets a
given consumer may auto-receive.

Resolution sites in Phase 1: `providers/factory.py`, the web-search
tool, channel setup, `memory.embedding`.

### 4.2 Discovery by service — the agent flow (Phase 3)

`need_secret(service, account=None, reason=…)`:
- 0 matches → `request_secret` flow (prompt the **user**, classify).
- 1 match → use it if `scope` authorizes the caller.
- >1 matches → the agent may pass `account`; else durin asks the user
  which one for this task.

The agent never receives the value in any branch.

## 5. Redaction (Phase 2)

A `SecretRedactor` holds the set of current secret values. `redact(text)`
replaces each value with `«secret:NAME»`. Applied to:

- every tool result, before it enters the model context;
- agent output shown to the user;
- log lines (a loguru patcher).

Values shorter than 8 characters are not redacted (avoids scrubbing
innocuous substrings). Built from the store at agent startup.

## 6. Scoped `exec` injection (Phase 2)

When the agent runs the `exec`/shell tool, durin builds the subprocess
`env`: inherited env **plus** `env[NAME] = value` for every store
secret whose `scope` includes `exec` (and `skill:<current>` when a
skill is running). The agent issues the command but never sees the
values — the script reads `os.environ["NAME"]`.

Multi-agent (`agent:` tags) is in the data model but **not enforced**
until durin's multi-agent surface needs it — enforcing it now would be
designing for a hypothetical.

## 7. CLI — `durin secret`

| command | behaviour |
|---|---|
| `durin secret set NAME --service S [--account A] [--description D] [--scope exec,skill:*]` | hidden value prompt → store |
| `durin secret list` | table: name, service, account, scope, masked value — never the value |
| `durin secret show NAME` | metadata only; `--reveal` prints the value with a warning |
| `durin secret rm NAME` | delete |
| `durin secret grant NAME --to <consumer>` / `revoke --from` | edit `scope` |

## 8. Migration

One-shot, on config load, idempotent. Detect plaintext secrets in known
slots — `providers.<n>.api_key`, `tools.web.search.api_key`,
`memory.embedding.api_key`, channel tokens — that are non-empty and not
already a `${secret:}` reference. For each:

1. Back up the config first (`loader.backup_config`).
2. Create a store entry: `name` derived (`OPENAI_MAIN`, `TELEGRAM_BOT`,
   …), `service` derived (`provider:<n>`, `web-search`, `channel:<n>`),
   `scope` set to the matching consumer, `origin: "migration"`.
3. Replace the config field with `${secret:NAME}`.
4. Write `secrets.json` with mode `0600`.

## 9. Wizard integration

The onboard wizard's API-key / token prompts write the value to the
store (creating the entry with the right `service` + `scope`) and put a
`${secret:NAME}` reference in the config — never the plaintext.

## 10. Phasing

- **Phase 1** — store module, `${secret:}` grammar + lazy resolution,
  migration, `durin secret` CLI, wizard, resolution wired into provider
  factory / web-search / channels. Removes plaintext from config.
- **Phase 2** — redaction; scoped `exec` injection. Closes the
  leak-to-model and makes `exec` credentials work.
- **Phase 3** — `need_secret` / `request_secret` agent tools.

## 11. Test plan

- store: round-trip; file mode is `0600`; rejects bad `name`.
- reference grammar: parse / reject; whole-field only.
- resolution: literal passthrough; missing → `SecretNotFoundError`;
  unauthorized consumer → `SecretScopeError`.
- migration: plaintext → reference + store entry; idempotent on re-run;
  config backed up.
- CLI: `set`/`list`/`rm`/`grant`/`revoke`; `list` never prints values.
- redaction: values replaced; <8-char values left alone.
- exec injection: `exec`-scoped secrets in subprocess env; others absent.

## 12. Open questions

- Dangling reference (config points to a deleted secret): resolution
  raises; `durin doctor` gets a check for dangling refs.
- `secrets.json` backup/sync is the user's responsibility — out of
  scope.
