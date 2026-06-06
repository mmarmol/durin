# Phase 6 — Hot layer + principal (closes the loop)

> Builds on Phases 1-5. Branch `memory-redesign-phase1`.

**Goal:** Re-inject authored knowledge so the agent USES it. Resolve the
principal (who the user is) per-message and pin an always-injected context: the
principal's person entity + the `always_on` feedback entities (design
§2.10-2.12). USER.md / MEMORY.md dissolve into this dynamic composition.

**Built:** `durin/memory/principal.py`
- `resolve_principal(channel_id, *, owner, channel_map)` — per-message:
  channel → owner (config) → `person:anonymous`.
- `ensure_owner(workspace, owner_ref, *, name)` — cold-start: auto-create a
  dream-authored placeholder person entity if missing (idempotent).
- `mark_always_on(workspace, ref)` / `list_always_on(workspace)` — the
  always_on attribute (dream-owned, §2.11) + the scan that finds pinned
  feedback.
- `build_pinned_context(workspace, principal_ref)` — the always-injected layer:
  "Who you're talking to" (the person entity) + "Always-on guidance" (the
  always_on stance/practice entities), provenance markers stripped.

**Verified:**
- 4 unit tests: principal resolution order; cold-start owner (idempotent);
  mark/list always_on; pinned-context composition (principal + always_on,
  markers stripped).
- **LIVE (glm-5.1) — the loop closes end-to-end:** authored `person:marcelo`
  ("software architect, strict vegetarian") + an always_on `practice:spanish`
  ("respond in Spanish"). The pinned context was injected; asked in ENGLISH for
  a dinner idea, the agent replied **in Spanish** ("¡Hola Marcelo!…") and
  **vegetarian** ("100% vegetariana… tofu"), using both pinned facts. Authored
  knowledge → re-injected → used.

**Deferred (follow-on) — design §2.10/§6.2:**
- **Loop wiring:** call `build_pinned_context` from the agent context preload
  (alongside / replacing `read_hot_layer`'s static identity+headlines), so it
  runs on every real turn. Update `identity.md` ## Memory accordingly (§6.2).
- **Retrieval blend:** combine the pinned layer with retrieval-driven canonical
  blocks (the existing `read_hot_layer`) into one budgeted context.
- **Always-on pin budget** + "fresh feedback is always_on by convention until
  the dream rectifies" (§2.11).
- **`system:` entity** for the environment (paths/tools) as a second pinned
  principal-adjacent block.
- **Per-message principal switch** in multi-user channels (the resolution is
  ready; the loop must call it per message, not per session).
