# Auto-install feature extras on activation — design

**Status:** APPROVED (2026-06-06) · **Scope:** all optional extras, phased.

## Problem

Optional features live behind pip extras (`durin-agent[web]` = ddgs/search,
`[cross-encoder]` = sentence-transformers/torch, `[slack]`, `[discord]`, `[mcp]`,
`[memory]` = fastembed, `[local]` = llama-cpp, `[oauth]`). A base install ships
without them. Today, when a user **activates** a feature whose extra is missing, it
**fails at runtime** with `ImportError → "pip install durin-agent[X]"` — sending the
user to a console. Observed twice in one session (ddgs for search, sentence-
transformers for the reranker "Probar"). Activating something intentionally should
not fail; it should install it — **frictionless**, with a notice.

## Decisions (approved)

- **Install UX — context-specific** (refined 2026-06-06):
  - **Webui enable / "Probar":** a **confirmation dialog** before installing — shows
    the extra + its **download size**, plus (when `needs_restart`) a **restart
    checkbox** in the same dialog. On confirm → install with progress, then restart if
    the box was checked. Not silent: deliberately enabling a (possibly ~1GB) feature
    warrants the heads-up.
  - **Onboarding wizard:** when a feature is selected, indicate "installs X (~Y MB
    download)"; install the selected features' extras during setup.
  - **Reinstall / fresh install:** auto — re-installing what the base already had.
  - **Agent / chat (runtime):** `ensure_extra` fires on the `ImportError`, installs
    with a log/progress notice; restart handled by the ask below.
- **Restart handling:** context-aware, only for extras that need it. Webui: the
  restart checkbox in the install dialog (or an inline "¿Reiniciar?" right after).
  Agent: the `ask_user_question` tool if available, else a plain question in the
  reply; if restart can't be executed (no permission), notify. Where in-process state
  can be reset (e.g. the cross-encoder's cached-OFF flag), do that and skip the
  restart entirely.
- **Gate:** `install.auto_install_extras` default **ON** — the capability is enabled;
  the confirm UX above is the user's control point. OFF → fall back to today's
  "install durin-agent[X]" message (offline / air-gapped / locked-down).

## Architecture

A central backend helper `ensure_extra(feature)` + a single registry mapping
`feature → (extra, probe-module, needs_restart, label)`. Three call-sites trigger
it; two surfaces (webui, agent) render progress + the restart ask.

## Components

1. **Extra registry** — `durin/extras.py` (new). One source of truth:
   | feature | extra | probe module | needs_restart |
   |---|---|---|---|
   | `web_search` | web | `ddgs` | no (lazy import) |
   | `cross_encoder` | cross-encoder | `sentence_transformers` | yes* (loader caches OFF — *resettable, see below) |
   | `mcp` | mcp | `mcp` | yes (loaded at startup) |
   | `slack` | slack | `slack_sdk` | yes (channel starts at boot) |
   | `discord` | discord | `discord` | yes (channel starts at boot) |
   | `memory_vector` | memory | `fastembed` | yes (embedding warms at boot) |
   | `local_models` | local | `llama_cpp` | yes (model loads at boot) |
   | `oauth` | oauth | `oauth_cli_kit` | no (on-demand) |
   (`needs_restart` is confirmed per-extra at wiring time by where the dep loads.)
   Each entry also carries an **`approx_size`** (rough download estimate, e.g.
   cross-encoder ~1 GB / torch, web ~few MB) shown in the confirm dialog + onboarding.

2. **`ensure_extra(feature) -> EnsureResult`** — `durin/extras.py`:
   - Probe the module. Present → `{status: "present"}`.
   - Else, if `install.auto_install_extras` (default **on**): resolve the extra's
     package specs from durin-agent's own installed metadata
     (`importlib.metadata.requires("durin-agent")`, filtered by the
     `; extra == "<extra>"` marker — no duplication of pyproject pins) and install
     them. **Install mechanism (detected):** `python -m pip install <specs>` if pip
     is importable in the env; else `uv pip install --python <sys.executable>
     <specs>` (pipx venvs have no pip but uv is present); else a clear failure.
   - Re-probe; return `{status: "installed"|"failed", needs_restart, message}`.
   - **Safety:** only extras in the registry are installable; package specs come from
     durin-agent's own metadata — never an arbitrary, caller-supplied package.

3. **Webui surface** — endpoints: `GET /api/extras/status?feature=…` (returns
   `{present, extra, approx_size, needs_restart}`) and `POST /api/extras/ensure
   {feature, restart?}` (installs, returns progress/result, restarts if requested).
   On a toggle / "Probar" / provider-select where the probe is missing, the UI opens a
   **confirm dialog** — "Esto instalará `<extra>` (~Y MB)" + a **restart checkbox**
   when `needs_restart` — then on confirm calls `ensure`, shows progress, and restarts
   if the box was checked. No console.

4. **Agent surface** — at the existing runtime `ImportError` sites (cross_encoder
   loader, web search, etc.): call `ensure_extra`; if installed and `needs_restart`,
   use `ask_user_question` ("¿Reiniciar para activar X?") when available, else ask in
   the reply; if restart can't be executed (no permission), notify. If no restart
   needed, retry the operation and continue.

5. **Config gate** — `install.auto_install_extras: bool = True`. Off → fall back to
   today's "install durin-agent[X]" message (offline / air-gapped / locked-down).

6. **Restart optimization** — for `cross_encoder`, reset the module-level cached-OFF
   flag after install so the next rerank loads the freshly-installed model **without**
   a restart. Restart-ask remains only for boot-initialized features (channels, mcp,
   memory warmup, local).

## Error handling

- Install fails (network, build error like torch) → `{status: "failed", message}`;
  the surface shows the real error + the manual `pip install` fallback. Never crashes
  the gateway/agent.
- `auto_install_extras` off → no install; the current ImportError message stands.
- Neither pip nor uv available → clear message ("no installer found; run …").

## Testing

- `ensure_extra`: module present → no-op; missing + gate-on → install invoked with
  the metadata-resolved specs (subprocess mocked); install fails → `failed` result;
  gate-off → no install. Registry shape test (every feature maps to a real extra).
- Install-mechanism detection: pip-path vs uv-path selection (mocked).
- Webui endpoint: missing extra → calls ensure_extra → returns progress/result
  (installer mocked).
- Agent fallback: ImportError site → ensure_extra → retry/ask path (mocked).
- cross-encoder cache-reset: after a (mocked) install, the loader retries instead of
  staying OFF.

## Phasing (all extras, by phase)

- **Phase 1 (this build):** `durin/extras.py` (registry + `ensure_extra` + mechanism
  detection + cache-reset hook) · config gate `install.auto_install_extras` · wire
  the two already hit — **cross_encoder** (webui toggle/Probar + the runtime loader)
  and **web_search** (the search provider) — across webui + runtime · webui
  `/api/extras/ensure` + restart prompt · tests.
- **Phase 2:** wire the rest — `slack`, `discord`, `mcp`, `memory_vector`,
  `local_models`, `oauth` — at their activation points + the onboarding wizard
  (install selected features' extras during setup).

Each phase ships working, tested software on its own.
