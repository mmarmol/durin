# Codex / ChatGPT OAuth Provider — Design

Date: 2026-06-06
Branch: `codex-oauth-provider`
Status: Approved design (pre-implementation)

## Goal

Let a user authenticate durin with their **ChatGPT account** (Plus/Pro/Business/Edu/Enterprise)
via OAuth and use the **Codex usage included in their plan** instead of a pay-per-token API key.
The provider already exists but is not wired into the user-facing surfaces.

Three objectives:

1. Configurable in the terminal onboarding wizard.
2. Configurable in the webui.
3. Updated list of supported Codex models.

## Background — current state in durin

- **Provider works**: `durin/providers/openai_codex_provider.py` calls
  `https://chatgpt.com/backend-api/codex/responses` (Responses API + SSE), sends the
  `chatgpt-account-id` header, reads tokens via `oauth_cli_kit.get_token()`.
- **OAuth login exists (CLI only)**: `durin oauth login openai-codex`
  ([commands.py:2511]) delegates to `oauth-cli-kit`, which implements **loopback PKCE only**
  (port 1455, `redirect_uri=http://localhost:1455/auth/callback`). No device-code flow.
- **`oauth-cli-kit` responsibilities we keep**: token storage (`FileTokenStorage`,
  `token_filename="codex.json"`), early refresh (`refresh_token` grant), file locking.
  `get_token()` reads `codex.json`, refreshes if needed, raises if absent — it never
  triggers an interactive login.
- **`OAuthToken` model**: `access`, `refresh`, `expires` (ms epoch), `account_id`.

### Gaps (== the three objectives)

1. Onboard: `openai_codex` is absent from `PROVIDER_CHOICES` and `DEFAULT_MODELS`
   (`durin/cli/onboard_wizard.py`).
2. WebUI: OAuth providers are explicitly **rejected** at
   `durin/channels/websocket.py:1296`; no UI to perform a login.
3. Models: default is the **deprecated** `gpt-5.1-codex`; no Codex model list exists.

## Comparison with reference implementations

| Aspect | openclaw (TS) | hermes (Python) | durin (today) | durin (target) |
|---|---|---|---|---|
| Flow | loopback PKCE + device-code | device-code | loopback (via kit) | loopback **and** device-code |
| client_id | `app_EMoamEEZ73f0CkXaXp7hrann` | same | same (kit) | same |
| account_id | JWT claim | JWT claim | from kit token | from kit token |
| `originator` | `openclaw` | **`codex_cli_rs`** | `durin` | **`codex_cli_rs`** |
| Models | static list | live discovery + static fallback | none / deprecated default | **live discovery + updated fallback** |
| Endpoint | `/backend-api/codex/responses` | same | same | same |

JWT account-id claim path (all impls): `payload["https://api.openai.com/auth"]["chatgpt_account_id"]`.

## Decisions (agreed)

- **Terminology**: this is OAuth **authorization** — durin obtains a Codex token to call the
  API on the user's behalf. It is unrelated to authenticating into durin's webui. UI wording
  is "Conectar con ChatGPT" / "Desconectar", not "login/logout".
- **Auth mechanism**: support both. Loopback PKCE for local CLI (reuse kit, nicer UX);
  device-code for webui and remote/headless CLI. Decided by **auto-detection with a manual override**.
- **WebUI**: always device-code (structural — the gateway cannot capture a loopback redirect
  that lands on the user's browser machine). WebUI surface = **Connect + status + Disconnect**.
- **Existing Codex CLI session**: do **not** silently adopt `~/.codex/auth.json`. Detect it and
  **ask** the user whether to use it or connect another account (disable the kit's silent
  `import_codex_cli`).
- **`originator`**: send `codex_cli_rs` (mimic the official Codex CLI) to avoid Cloudflare 403
  on non-residential IPs. Fixed value, not a config knob.
- **Models**: live discovery from `GET /backend-api/codex/models` is the source of truth; a
  static fallback (hermes-style) is used only when there is no token. No model knobs.
- **Default model**: `openai-codex/gpt-5.5` (replacing deprecated `gpt-5.1-codex`).

## Architecture

### 1. Device-code login module — `durin/providers/codex_device_auth.py` (new)

Ported from hermes (~150 LOC). Pure functions, no global state, usable from CLI (blocking)
and webui (start / poll split).

- `request_device_code() -> DeviceCodeChallenge`
  - POST `https://auth.openai.com/api/accounts/deviceauth/usercode`
  - body `{"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}`, header `originator: codex_cli_rs`
  - returns `user_code`, `verification_uri`, `device_auth_id`, `interval`, `expires_in`.
- `poll_once(device_auth_id, user_code) -> PollResult`
  - POST `https://auth.openai.com/api/accounts/deviceauth/token`; status pending/ok/error.
  - On success: exchange at `https://auth.openai.com/oauth/token`
    (`grant_type=authorization_code`, `redirect_uri=https://auth.openai.com/deviceauth/callback`,
    `code_verifier` from the device-auth response).
- `persist_token(token_response) -> OAuthToken`
  - extract `account_id` from the access-token JWT claim,
  - build `OAuthToken(access, refresh, expires_ms, account_id)`,
  - save via `FileTokenStorage(token_filename="codex.json").save(token)` — the **same file**
    the provider reads. Nothing else needs to know it came from device-code.
- `login_blocking(print_fn, open_browser=False)` — convenience wrapper for the CLI: request,
  display code + URL, poll until ok/expired.

The provider, refresh, and file-lock paths are **untouched** by this module.

### 2. Login strategy selector — `durin/utils/oauth.py` (extend)

- `should_use_device_code() -> bool`
  - True if `$SSH_CONNECTION`/`$SSH_TTY` set, or no GUI/browser available.
- CLI override: `--device` / `--loopback` flag on `durin oauth login` and an onboard prompt
  fallback. Order: explicit override > auto-detect > loopback default.
- WebUI: always device-code (does not call the selector).
- **Existing-session detection**: `existing_codex_session() -> CodexSessionInfo | None`
  inspects `~/.codex/auth.json` (official Codex CLI) and durin's own `codex.json`, returning
  the account email/plan if found. Both onboard and webui call this **before** starting a new
  flow and prompt the user to reuse it or connect another account. `FileTokenStorage` is
  constructed with `import_codex_cli=False` so nothing is adopted silently.

### 3. Provider — `durin/providers/openai_codex_provider.py` (minimal edits)

- `originator` `"durin"` → `"codex_cli_rs"`; `User-Agent` → Codex-CLI style
  (e.g. `codex_cli_rs/0.0.0 (durin)`).
- `default_model` `"openai-codex/gpt-5.1-codex"` → `"openai-codex/gpt-5.5"`.

### 4. Models — `durin/providers/codex_models.py` (new)

- `discover_codex_models(access_token) -> list[str]`
  - `GET https://chatgpt.com/backend-api/codex/models` (Bearer token, `originator` header);
    parse the `models` array; cache to a small JSON cache.
- Static fallback (updated, used when offline / no token):
  `gpt-5.5`, `gpt-5.5-pro`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`.
- Resolution order: live → cache → static fallback.
- Consumed by the onboard model picker and the webui `/api/models` endpoint for the
  `openai-codex` provider.

### 5. Onboard wizard — `durin/cli/onboard_wizard.py` (Objective 1)

- Add to `PROVIDER_CHOICES`:
  `("OpenAI Codex (ChatGPT Plus/Pro, OAuth)", "openai_codex", "gpt-5.5")`.
- Add `DEFAULT_MODELS["openai_codex"]` from the codex_models fallback.
- On selection: run the login strategy (loopback or device-code), no API-key prompt;
  confirm once a token is present; let the user pick a model.

### 6. WebUI — `durin/channels/websocket.py` + `webui/` (Objective 2)

Backend (device-code only):

- `GET  /api/oauth/codex/status` → `{ connected: bool, email?, plan?, source: "durin"|"codex-cli" }`.
  Reflects current `codex.json` and any detected `~/.codex/auth.json` (for the reuse prompt).
- `POST /api/oauth/codex/start` → `{ user_code, verification_uri, device_auth_id, interval, expires_in }`.
- `GET  /api/oauth/codex/poll?device_auth_id=...` → `{ status: pending|ok|error, ... }`;
  on `ok` the token is already persisted to `codex.json`.
- `POST /api/oauth/codex/disconnect` → deletes `codex.json` (+ `.lock`); returns updated status.
- The existing OAuth rejection at line 1296 stays for `/settings/provider/update`
  (still no api_key to set); the new routes are the OAuth path.

Frontend (`webui/`):

- In the Providers section, an "OpenAI Codex" entry showing connection **status**
  (connected account email/plan, or "no conectado").
- **"Conectar con ChatGPT"** → if an existing Codex session is detected, first ask to reuse it
  or connect another account; otherwise open an inline (non-native) modal showing `user_code`
  + `verification_uri`, polling `/poll` until `ok`. Model selector via `/api/models`.
- **"Desconectar"** → inline styled confirmation (no native `window.confirm`), then
  `POST /disconnect`.

### 7. Tests

- Device-code flow with mocked HTTP (usercode → poll pending → poll ok → exchange).
- `account_id` extraction from a sample JWT.
- Token persisted to / read back from `codex.json` round-trip.
- Model fallback when discovery fails; discovery parsing of a sample response.
- **Refresh preserves `account_id`** (see risk below).
- `should_use_device_code()` for SSH/headless vs local env matrices.
- `existing_codex_session()` detection (durin `codex.json` and `~/.codex/auth.json`), and that
  no silent import happens (`import_codex_cli=False`).
- Disconnect deletes `codex.json` and status flips to `connected: false`.

## Risks / verification items

- **Refresh dropping `account_id`**: `oauth-cli-kit._refresh_token` may not re-extract
  `account_id` from the new access token. If `get_token()` returns a refreshed token with
  `account_id=None`, the `chatgpt-account-id` header goes empty and requests may fail.
  Mitigation: a test that forces refresh and asserts `account_id` survives; if it does not,
  re-extract from the JWT inside the provider before building headers.
- **ToS / account risk**: mimicking `codex_cli_rs` and reusing the Codex CLI client_id is a
  ToS gray area; Google/OpenAI have banned accounts using third-party clients this way.
  Documented for the user; not gated in code.
- **`refresh_token_reused`**: if the same ChatGPT account also runs the official Codex CLI /
  IDE extension, refresh tokens can be invalidated across clients. Surface a clear relogin message.

## Out of scope (YAGNI)

- GitHub Copilot changes.
- Loopback in the webui.
- Making `originator` a config knob.
- Credential pooling / multi-account (hermes has it; not needed here).

## Files touched

New: `durin/providers/codex_device_auth.py`, `durin/providers/codex_models.py`,
webui modal component.
Edited: `durin/providers/openai_codex_provider.py`, `durin/utils/oauth.py`,
`durin/cli/onboard_wizard.py`, `durin/cli/commands.py` (login override flag),
`durin/channels/websocket.py`, webui Providers view, tests.
