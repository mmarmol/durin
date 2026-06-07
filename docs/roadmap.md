# Roadmap

> Forward plan después de la refutación empírica de la dirección "smart layer".
> Ver `bitacora.md` para qué se descartó y por qué.
> Para detalles históricos de items shipped, ver `git log` + `bitacora.md`.

---

## Current state (2026-05-30)

Durin es un Nanobot baseline + sistema completo de memoria entity-centric +
daily-driver lifecycle + capability bridges (vision/audio) + secrets +
web config parity + workspace vault-friendly.

**Memoria** (subsystem hardened post-H25→H30 + P9→P11):
- 5 memory tools (`memory_search`, `memory_upsert_entity`, `memory_ingest`, `memory_drill`, `memory_forget`) con FTS5 + LanceDB + grep + RRF + entity-aware ranker + cross-encoder rerank (`memory_store` quedó deshabilitado en el modelo entity-centric — facts van por `memory_upsert_entity`)
- Default embedding `intfloat/multilingual-e5-small` (~450 MB, 100+ langs, MIT)
- Default cross-encoder `BAAI/bge-reranker-base` (opt-in via wizard)
- Workspace Obsidian-compatible (`VAULT_README.md` + per-class `_INDEX.md`, wikilinks en `source_refs`/`related`, `.durin/index/` aislado)
- `HealthChecker` periódico + `durin doctor` checks: FTS/Lance/CE probes con auto-recovery
- Dream consolidator + entity-centric pages + auto-absorb opt-in
- LoCoMo bench ~75% (+18pp acumulado vs baseline pre-fix)
- 4500+ tests passing

**Acción derivada de Phase 1a-refuted (capture eficiencia sin router)**:
single generic-engineering SOUL en `durin/templates/SOUL.md`, auto-synced al
workspace en bootstrap (`sync_workspace_templates`), incluido en context via
`BOOTSTRAP_FILES`. Shipped.

Pendientes activos: `docs/backlog.md`.

---

## Direction: two horizons backed by industry evidence

### Horizon 1a — Role-based SOUL.md routing — REFUTED (2026-05-19)

**Status**: closed. V9e ran 107 exercises × 3 conditions (none / specific / generic_agent), `max_tokens=131072`, glm-5.1. Pass rates: 69.2% / 71.0% / 73.8% — gap of 4.6pp within the noise floor (±4.4pp for N=107). The 23 divergent exercises distribute uniformly across the 6 possible patterns (chi² = 1.78, df=5, p≫0.05), and sign-test per-condition gives p=0.41–0.68 — **statistically indistinguishable from random model variance**. Error types are nearly identical across conditions (28/30/25 AssertionError, 1/1/2 setup errors). Jaccard similarity of fail-sets is 0.57–0.61 — most failures are shared difficulty, not differentiation. See `bitacora.md` y `git log` para análisis V9e completo.

**Lo que sobrevive como efecto real**:
- **Token efficiency**: SOUL ≠ ∅ reduces median output tokens 3–5× y reasoning chars 2.84× vs no SOUL, a corrección idéntica. Robusto en V9d y V9e.
- El beneficio viene de **cualquier SOUL**, no de matching role-to-task. Un single generic engineering SOUL captura el efecto sin router.

**Por qué NO construimos el router**:
- Sin señal de corrección en el régimen donde nuestro modelo opera (frontier reasoning, 1M context)
- El patrón anecdótico "lyrics → none / structure → generic" (N=4/3/5 en casos divergentes) está dentro del ruido Bernoulli
- Un router agrega infraestructura (classifier LLM call, fragment library, integration) para un efecto que no se midió

**Lo que SÍ adoptamos**:
- Single generic-engineering SOUL como default (`durin/templates/SOUL.md`, shipped — ver Current state)

---

### Horizon 1b — Per-query dynamic context (Aider-style retrieval)

**Qué es**: context-specific information pulled from the workspace or prior conversation, packed into the user message or system prompt al inicio de un turn — no un SOUL.md fragment, sino *información relevante a esta query exacta*.

**Ejemplos en producción**:
- Aider's PageRank repo map (qué symbols/files son más relevantes a esta query)
- Cursor's @-references y codebase indexing
- Hermes Agent's skill-doc retrieval por task similarity
- Cualquier RAG layer en el agent boundary

**Hipótesis**:
Para tasks con codebase o knowledge base no-trivial, query-conditioned context retrieval mejora outcomes más allá de lo que un static SOUL puede proveer.

**Status actual**: convergencia parcial con Horizon 2 — el memory subsystem
actual ya hace retrieval por query, vía `memory_search` (FTS + vector + RRF +
rerank). El **skill-doc retrieval estilo Hermes ya está shipped** (Spec 2,
2026-06-03): los skills (`skills/<slug>/SKILL.md`) son una pseudo-clase de
memoria buscable — indexados en FTS + vector, devueltos por `memory_search`
como `kind="skill"` (procedimientos a seguir, no hechos a citar), drillables,
gated por `memory.index_skills`. Lo que falta para que esto sea Horizon 1b
completo es la pieza "codebase-aware" (PageRank-style sobre el repo del
usuario, no sobre la memoria). No priorizado hoy.

---

### Horizon 2 — Memory system — SHIPPED (entity-centric, Phases 0-6 + post-T1 cycle)

**Status**: implementado. La forma final difiere del diseño original
(el diseño original): no son "5 node types con milestone
promotion", sino entity-centric pages + classes (`stable` / `episodic` /
`corpus` / `pending` / `session_summary` / `entities`) + Dream consolidator
LLM-driven + auto-absorb opt-in. Ver `docs/architecture/memory/`.

**Evidence base que validó la dirección**:
- Hermes Agent skill loop (solve → document → reuse): **+40% speedup**, production-validated
- Aider's PageRank repo map: validated por adopción + benchmark results
- Reflexion (academic): episodic failure memory mejora recovery medible

**Refinamientos shipped post-Phase 6**:
- Skills como pseudo-clase de memoria buscable (Spec 2, 2026-06-03) — `memory_search` devuelve `kind="skill"`, indexado FTS + vector, keep-in-sync en cada mutación, gated por `memory.index_skills`. Cierra el "skill-doc retrieval estilo Hermes" de Horizon 1b.
- Hot working-set tier (Phase 5, 2026-06-03) — el bloque de skills del stable-anchor inyecta el working-set rankeado por uso (`skill_calls`: frecuentes-7d ∪ recientes, fill-to-budget), memoizado por sesión (prefix-cache-safe), gated por `agents.defaults.skills_hot_tier`. La cola larga queda en `memory_search`; un nudge en el prompt avisa de buscarla. Completa el retrieval híbrido caliente/frío de Spec 2 §2.2. (Sistema de skills completo as-built: `docs/architecture/skills/00_overview.md`.)
- Session-FTS + grep-verify + type prior (2026-06-09) — las sessions crudas se indexan en FTS5 por turno (`sessions/<key>.md#turn-N`, schema v6, reindex incremental en cada save); antes eran grep-only (w=0.3) y un turno con la mejor respuesta literal no podía ganarle a ninguna entry indexada. Fix de `ORDER BY rank` en FTS5 (el "ranking" léxico era orden de inserción). Grep-verify boost (doc 03 §7.4): hits de vector fuera del top-50 léxico que contienen literalmente la query recuperan la contribución léxica que el cutoff les negó (FTS MATCH per-uri, route-aware, language-neutral). Type prior (doc 03 §7.5): `session` ×0.85 — lo destilado lidera a evidencia comparable. Nota vs H26: el prior es estructural (calidad de fuente, estático), no pre-juzga recency ni intención de query; la magnitud queda sin tunear hasta medir.

**Refinamientos no perseguidos (por ahora)**:
- Aider PageRank-style relevance ranking para code-related milestones — no aplicable hoy (no usamos memoria sobre código)
- Reflexion-style explicit failure-pattern tracking — no priorizado

---

## What we are explicitly NOT doing

These have been tested o tienen razones fuertes en contra. Ver `bitacora.md`
para rationale completo.

- ❌ Posture vector (5-axis dynamic behavioral state)
- ❌ Plan tiers / phases / forced verification gate / cycle escalation
- ❌ Deliberation V3 (single-call multi-perspective en un modelo)
- ❌ Phase-aware temperatures
- ❌ Self-verification / self-review loops (same-model)
- ❌ Pre-completion Critic (sin genuinely different model)
- ❌ **Role-based SOUL.md router** (refuted V9e 2026-05-19) — sin correctness signal beyond noise; efficiency gain captured por single default SOUL sin routing
- ❌ Temporal decay en ranking de memory_search (removed H26 2026-05-30) — search debe ser faithful retrieval, no pre-juzgar lo que el LLM debe decidir

---

## Decision rules (carried over from bitácora lessons)

1. **No component sin empirical o industrial precedent.** "It seems like it should help" no alcanza.
2. **Mechanisms deben demostrablemente activarse en tests realistas.** Si el main code path nunca corre, el component es overhead.
3. **Distrust same-model self-verification.** Need ground truth (tests) o different models.
4. **Specificity > abstraction.** "Be cautious" no cambia comportamiento; concrete rules sí.
5. **3+ trials minimum** para cualquier quantitative claim.
6. **Test en regímenes donde el baseline puede fallar.** Ceiling-effect scenarios no prueban nada.
7. **Search es el producto** (lesson 2026-05-30): durin es memoria, search-misses son el bug primario, todo lo demás es detalle.
8. **Search faithful retrieval** (lesson 2026-05-30, H26): rankings deben venir de query-derived signals, no de heuristics que pre-juzgan lo que el LLM debe decidir.

---

## Last updated: 2026-06-03 (Spec 2 shipped: skills are a searchable memory pseudo-class + hot working-set tier — full hot/cold retrieval)
