# Web config parity — review + action plan

> Status: review, 2026-05-22. Goal: configure all of durin from the
> web dashboard, no command line required.

## 1. Where the web stands today

The dashboard's Settings has exactly **two sections**
(`webui/src/components/settings/SettingsView.tsx`):

- **General** — theme, language (webui-local prefs), the AI
  provider + model picker, and read-only system info (config path,
  restart).
- **BYOK** — provider API key + base URL, and the web-search backend
  key.

The backend contract is a handful of bespoke endpoints in
`durin/channels/websocket.py`:

| endpoint | writes |
|---|---|
| `GET /api/settings` | model, provider, provider list, web-search |
| `POST /api/settings/update` | `agents.defaults.model` / `.provider` |
| `POST /api/settings/provider/update` | `providers.<n>.api_key` / `.api_base` |
| `POST /api/settings/web-search/update` | `tools.web.search.*` |

So the web can set roughly **four fields**. `durin`'s `Config` has ten
top-level sections and on the order of a hundred fields.

## 2. Gap analysis

| Config area | Web today | Missing |
|---|---|---|
| `agents.defaults` model/provider | ✓ General | — |
| `agents.defaults` tuning | ✗ | max_tokens, context window, temperature, reasoning_effort, timezone, bot name/icon, workspace, fallback_models, iteration/subagent caps, parallel_tool_calls, disabled_skills, dream, compaction ratios |
| `agents.aux_models` | ✗ | vision / audio aux models |
| `providers.*` | ⚠ BYOK | OAuth + local providers absent; **keys written as plaintext, bypassing the secret store** |
| **secrets** (the store) | ✗ | the entire `durin secret` subsystem |
| `memory` | ✗ | `enabled`, embedding model |
| `channels` | ✗ | telegram / slack / discord / … enable + credentials; websocket settings |
| `gateway` | ✗ | `daemon`, `webui_enabled`, websocket host/port/auth |
| `tools.web.search` | ✓ BYOK | — |
| `tools` (rest) | ✗ | exec sandbox / deny-allow patterns / timeouts, other tool configs |
| `api` (OpenAI-compat server) | ✗ | host, port, keys |
| `model_presets` | ✗ | named presets |
| `model_capabilities` | ✗ | per-model overrides |

Two findings beyond "missing UI":

- **The web leaks plaintext.** `POST /api/settings/provider/update`
  writes `providers.<n>.api_key` as a literal — undoing the secrets
  subsystem. A key set from the dashboard lands plaintext in
  `config.json.d/providers.json`.
- **Bespoke endpoints don't scale.** One endpoint per concern means
  ~20 more endpoints to cover the schema. Unsustainable.

## 3. Architecture

Stop adding per-concern endpoints. Add two generic surfaces, mirroring
the CLI that already works:

1. **Generic config API** — the web equivalent of `durin config`:
   - `GET /api/config` → the full merged config, secret-masked, plus
     the JSON schema (for form rendering).
   - `POST /api/config` → set a dotted key, validated against the
     schema before writing (same path as `durin config set`:
     `read_persisted_config` → `validate_dict` → `save_config`).
2. **Secrets API** — the web equivalent of `durin secret`:
   - `GET /api/secrets` → entries' metadata (name, service, account,
     scope, origin) — **never values**.
   - `POST /api/secrets` → create/update (value in the body, over the
     loopback+token channel; stored, never echoed).
   - `DELETE` (folded into a path, per the GET-only HTTP parser) and
     scope grant/revoke.

The webui renders schema-driven sections from `GET /api/config`, and
keeps the curated widgets (model picker, provider BYOK) on top for the
common path. Curated endpoints can stay as nice UX, but they delegate
to the same store/validation — and the BYOK one is fixed to write a
`${secret:}` reference, not plaintext.

Security: every config/secrets endpoint requires the API token (the
dashboard already mints one via `/webui/bootstrap`); the secrets API
never returns a value; writes go through schema validation so a bad
form submission can't corrupt the config.

## 4. Action plan

**Phase A — Secrets (the explicit ask).**
- `GET/POST /api/secrets` (+ scope grant/revoke, delete).
- New **Secrets** settings section: list (metadata only), add, edit
  scope, delete. Mirrors `durin secret`.
- Fix `POST /api/settings/provider/update`: store the key in the
  secret store and write a `${secret:}` reference — the dashboard
  stops leaking plaintext.

**Phase B — Generic config backbone.**
- `GET /api/config` (masked + schema) and `POST /api/config` (dotted
  set, validated).
- webui `api.ts` client + a generic settings-form renderer.

**Phase C — Schema-driven sections.** Fill the gaps using Phase B:
agent tuning, aux models, memory, channels, gateway, tools, presets.
Group them into Settings sections that match the CLI's mental model.

**Phase D — Curated polish.** Where a raw form is poor UX (aux model
pickers, channel enable + credential, embedding model choice), add
curated widgets on top of the generic API.

## 5. Test plan

- API: token enforced; `GET /api/secrets` never includes values;
  `POST /api/config` rejects schema-invalid input without writing;
  round-trips through the split layout.
- webui: settings sections render from `GET /api/config`; a save
  calls `POST` and reflects back; secrets section never shows a value.
- End-to-end: configure a provider from the dashboard → the key lands
  in `secrets.json` as a reference in config → `durin doctor` green.

## 7. Functionality the first pass missed

Re-reviewing for completeness — these are not plain config fields and
need their own handling:

- **OAuth provider setup.** `codex` / `copilot` (and other OAuth
  providers) can't be configured by writing a field — they need a
  device-code / browser login *flow*. The web needs an OAuth widget
  that drives the flow and reports status. Phase C must treat OAuth
  providers specially, not as a key field.
- **Web first-run onboarding.** Opening the dashboard against a fresh
  install (no config) should walk provider → key → model, the web
  equivalent of `durin onboard`. Today it just shows empty settings.
- **Channel login flows.** Some channels need more than a token field
  (QR pairing, OAuth). `durin channels login` exists; the web needs
  the equivalent interactive step, not just a credential field.
- **`requires_restart` per field.** Some changes hot-reload, some need
  a gateway restart. The payload already carries `requires_restart`;
  it must be computed per changed field and surfaced in the UI.

Adjacent surfaces — "manage durin from the web", not strictly config.
Flag as separate scope; pull in only if the user wants them:

- a **doctor / diagnostics** view (run the checks, show the table);
- **cron job** management (`~/.durin/cron/`, not part of `Config`);
- **extras install** (`pipx inject`) — runs a subprocess; feasible
  from the gateway but needs care, or stays a CLI-only action.

## 6. Open questions

- Restart semantics: which config changes hot-reload vs need a gateway
  restart. `_settings_payload` already returns `requires_restart`;
  extend it per-field.
- Whether to expose every field or curate a "safe subset" — some knobs
  (e.g. `max_tool_iterations`) are footguns. Lean toward exposing all
  under an "Advanced" disclosure rather than hiding them.

## 14. Settings IA — agreed plan (2026-05-22)

After A/B/C shipped, a design pass on the Settings area. Adding
sections reactively produced a muddle ("API Keys" bundled provider
keys with web search; the model area was split between General and
BYOK; no model catalog; no test action). Agreed structure:

| Section | Contents |
|---|---|
| **General** | The hub — most important. Interface (theme, language); the **model area**: default model + **vision** + **audio** aux models; a **searchable picker over configured models**; **test-before-accept** (ping the picked model, only commit if it answers); system info (config path, restart). |
| **Providers** | Per-provider API key + base URL; configured state; connection test. (The old BYOK "llm" pane.) |
| **Web search** | Search backend + key. (The old BYOK "web-search" pane.) |
| **Channels** | Enable Telegram/Slack/Discord + credential. |
| **Secrets** | The secret store. (Shipped — Phase A.) |
| **All settings** | The generic schema-driven form. (Shipped — Phase C.) |

The model area stays **in General** (not a separate "Models" section)
— it is the single most important thing a user configures.

### New backend needed

- `GET /api/models?provider=X` — models available for a provider, for
  the picker. Source: `durin/cli/models.py` (`get_all_models`,
  `get_model_suggestions`) + `providers/capabilities.py` + the wizard's
  `DEFAULT_MODELS` + the provider registry.
- `GET /api/model/test?model=&provider=` — round-trip test via
  `check_model_ping`. **Wrinkle**: `check_model_ping` calls
  `asyncio.run` internally, which fails inside the gateway's running
  event loop. Either run it in a thread executor from the handler, or
  refactor `check_model_ping` to expose an async core the handler can
  `await`.
- Aux models + channel enable need no new endpoint — `/api/config/set`
  (JSON object value) and `/api/channels` (shipped, §13) cover them.

### As-built before this rework

Committed and green (suite 4132): Phase A (Secrets), Phase B
(`/api/config`), Phase C ("All settings"), gateway graceful SIGTERM,
REST 401 re-bootstrap, BYOK→"API Keys" rename, `GET /api/channels`.
None of it is thrown away — Secrets and All settings stay as-is; "API
Keys" splits into Providers + Web search; the model area consolidates
into General.

### Implementation order

1. Split "API Keys" → **Providers** + **Web search** (rename + two nav
   sections; `ByokSettings` gains a `forcePane` prop).
2. **General** model area: vision/audio aux rows + the model test
   (backend `/api/model/test` first).
3. **General** searchable model picker (backend `/api/models`).
4. **Channels** section (UI over the shipped `/api/channels`).

Deploy once at the end; the user validates the visual design.

### Note — guided third-party setup (future)

The **Providers** and **Channels** sections are also the future home
of *guided* third-party configuration: OAuth / device-code login
flows, walking the user through obtaining an API key from a vendor's
site, channel QR pairing. Not in this pass — but the section layout
should leave room for a per-provider / per-channel guided step, not
just a flat credential field.
