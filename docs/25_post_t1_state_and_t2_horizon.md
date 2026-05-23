# 25 — Post-T1 state + T2 horizon

> Cierre de T1 (entity-centric memory shipped + wired + verified, mayo 2026) y
> horizon T2 sin compromiso de implementación. Este doc captura el estado
> verificado, los ítems candidatos a T2 con su origen archivado + razón de
> deferral citada, y el backlog ortogonal de UX para que la próxima decisión
> de qué construir se tome con evidencia.
>
> No es un plan ejecutable. Cuando se elija un ítem se abre doc 26 (o el que
> corresponda) con plan detallado por clusters de riesgo (mismo patrón que
> archived docs 23 + 24).
>
> **§2 fue reescrito 2026-05-23** verificando cada claim contra código
> (no contra la versión anterior del doc): T2.B descartado (ya
> implementado), T2.C re-scoped, T2.F y T2.G agregados desde archived doc
> 21 §3 + doc 22 N1. §2.E identificado como prerequisite estructural
> de los demás — **shipped el mismo día** vía `durin memory stats`.

---

## §1 — Estado verificado post-T1

### Lo que está construido + wireado + testeado

Phases 0-6 del doc 19 + wiring W1-W4 del doc 24 ya están ejecutados y
verificados. Resumen contra código:

| Capa | Componente | Archivo | Verificado |
|---|---|---|---|
| Entries | `MemoryEntry.entities` tipadas (`type:value`) | `durin/memory/schema.py` | Tests + live |
| Storage | Git substrate `memory/.git/` | `durin/utils/git_repo.py` | Tests |
| Pages | EntityPage parser (frontmatter abierto) | `durin/memory/entity_page.py` | Tests |
| Aliases | AliasIndex (rebuild-only, lazy) | `durin/memory/aliases_index.py` | Tests + E2E-3 |
| Dream | DreamConsolidator (pydantic + retry + context budget) | `durin/memory/dream.py` | Tests + live glm-5.1 |
| Dream trigger | `durin memory dream` CLI (manual) | `durin/cli/memory_cmd.py:170` | Tests + live |
| Vector index | `entities` field + entity_page rows | `durin/memory/vector_index.py` | Tests + E2E-2 |
| Retrieval | memory_search invoca ranker entity-aware (RRF) | `durin/agent/tools/memory_search.py` | Tests + E2E-1 |
| Drill-down | history, diff, show, revert, expand | `durin/cli/memory_cmd.py` | Tests |
| Absorption | EntityAbsorption + CLI `absorb` + `absorb-suggest` | `durin/memory/absorption.py`, `durin/cli/memory_cmd.py` | Tests + E2E-5 |
| Outcomes | 6 outcome tests (O1-O5 + anti-fragilidad) | `tests/integration/test_phase6_outcomes.py` | Pasando |

Suite total: 4365 tests pasando, 16 skipped.

### Lo que está fuera de scope inicial (doc 19 §14)

Explícito, no entra a T2 sin disparador:

- L2+ retrieval (graph traversal, cross-encoder, PageRank).
- Sub-paging para mega-hub (`person:user`, `project:durin`).
- Visualización Obsidian-style.
- User editing manual de entity pages.
- Sync remoto.
- Benchmark público (LoCoMo, EverMemBench).

---

## §2 — Ítems candidatos a T2 (verificados contra código)

Lista revisada el 2026-05-23 grep-validando cada claim. Los ítems
verificados se citan con su origen archivado y el motivo de deferral.
T2.B fue descartado (ya está implementado). T2.C fue re-scoped.

### §2.0 — Orden lógico

Dos lecturas posibles del horizon, no excluyentes:

**Foundational (complete-T1)** — todos shipped:

- §2.H Fragment/canonical retrieval contract — **SHIPPED** (2026-05-24).
  Cierra la promesa de doc 18 §6 ("LLM reconcilia con timestamps y
  contexto") en ambos paths de delivery (lazy `memory_search` y eager
  `hot_layer`).

**Automatización + features T2**:

- §2.E telemetry aggregation — **SHIPPED**.
- §2.C shared AliasIndex via ctx — **SHIPPED** (2026-05-24).
- §2.A.1 per-entity dispatcher — **SHIPPED** (4/4 triggers: cron_daily / post_compaction / session_close / threshold; β.1 + β.2 complete 2026-05-24).
- §2.A.2 cross-session learning — diferido (requiere doc 26).
- §2.D auto-absorb.
- §2.F eager-inject context block — parcialmente cubierto por §2.H
  hot_layer; eager-fetch on-demand de contenido completo (no solo
  hot layer) sigue pending.
- §2.G eviction/compresión results.

Con §2.H + §2.E + §2.C cerrados, queda §2.A.1 para terminar el flujo
end-to-end auto: dream se dispara solo, los resultados ya llegan al
LLM en formato canonical/fragment correcto.

### §2.A — Auto-trigger del DreamConsolidator (split por scope)

Doc 18 §6 lista 4 triggers posibles. Verificación contra código (2026-05-23
20:30): solo `cmd_dream` (`durin/cli/memory_cmd.py:241`) invoca
`DreamConsolidator`. El cron-scheduled `agent.dream.Dream` (system job
"dream" en `cli/commands.py:1559`) es el sistema **legacy** sobre
`MEMORY.md`/`SOUL.md`, NO el entity-centric.

Hay que separar dos cosas que se mezclaban en versiones previas de este
doc:

#### §2.A.1 — Per-entity dispatcher (SHIPPED 2026-05-24 — 4/4 triggers)

**β.1 (shipped 01:50)**: cron diario + DreamRunner core.
- `MemoryDreamConfig` (memory.dream.{enabled, cron, threshold_entries,
  post_compaction, on_session_close, model_override,
  min_seconds_between_runs}).
- `DreamRunner` con lock atómico (`memory/.dream.lock`, O_CREAT|O_EXCL,
  stale recovery a 10min) + throttle (`.dream.last_run` mtime).
- 3 telemetry events: `memory.dream.start` / `.end` / `.skipped`.
- Cron diario registrado como system job `memory_dream` (default
  `0 3 * * *`); dispatch via `asyncio.to_thread`.

**β.2 (shipped 01:30)**: 3 sub-daily triggers + manual route refactor.
- `cmd_dream` (CLI manual) refactored para usar `DreamRunner` —
  manual runs ahora comparten lock con auto-triggers
  (`min_seconds_between_runs=0` para no throttle al usuario).
- `MemoryStoreTool` agrega `_maybe_dispatch_threshold_dream()`:
  cuenta post-cursor entries por entidad después de cada write,
  spawns daemon thread con `DreamRunner.run(trigger="threshold",
  entity_filter=ref)` cuando crossea `threshold_entries`.
- `Consolidator.on_post_compaction: Callable[[str], None] | None`
  attribute. Fires después del loop cuando `last_summary` es truthy.
  Wired en `cli/commands.py` startup a spawn de daemon thread con
  `trigger="post_compaction"`.
- `AgentLoop.on_session_close: Callable[[str], None] | None`
  attribute. `cmd_new` (/new fresh session) invoca el hook. Wired
  en startup a spawn de daemon thread con `trigger="session_close"`.

**Tests**: 14 (β.1) + 11 (β.2) = **25 nuevos**. Suite: 4442 passing.
Smoke e2e (4 scenarios + 4 triggers) PASS.

**Triggers en producción**:
| Trigger | Source code | Config knob |
|---|---|---|
| `cron_daily` | `cli/commands.py` startup `register_system_job("memory_dream")` + `on_cron_job` dispatch | `memory.dream.cron` (default `0 3 * * *`) |
| `post_compaction` | `Consolidator.maybe_consolidate_by_tokens` → `on_post_compaction(session.key)` | `memory.dream.post_compaction` (default True) |
| `session_close` | `cmd_new` (/new) → `loop.on_session_close(ctx.key)` | `memory.dream.on_session_close` (default True) |
| `threshold` | `MemoryStoreTool._maybe_dispatch_threshold_dream` (post-write) | `memory.dream.threshold_entries` (default 5) |
| `manual` | `cmd_dream` (`durin memory dream`) | always works (no config gate) |

---

#### §2.A.1 — Diseño original (referencia histórica)

**Qué hace**: corre `consolidate_entity(ref, entries)` sobre entidades
con entries post-cursor pending. Es la **automatización** de lo que
hoy es manual via `durin memory dream`. NO produce aprendizajes cross-
sesión nuevos — solo procesa entries acumuladas en disco.

Triggers (doc 18 §6, no opcionales):

1. **Cron diario**: como OpenClaw (`0 7 * * *` o equivalente). Predecible.
2. **Post-compaction**: hook en `Consolidator.maybe_consolidate_by_tokens`
   — el contexto ya está siendo procesado, costo amortizado.
3. **Session-close**: idle timer + `/quit`. Como OpenClaude (turn-end
   hook con gates).
4. **Threshold per-entity**: cuando una entidad acumula N entries
   post-cursor. Como Hermes per-N-turns (pero a nivel entidad).

Todos comparten:
- Forked async (daemon thread o asyncio task) — nunca bloquea el main loop.
- Lock file `memory/.dream.lock` para prevenir runs concurrentes
  (ya gitignored por `EntityAbsorption`).
- Catch-up en next start si la máquina estaba off (patrón
  `HeartbeatService`).
- Telemetry: `memory.dream.start`, `memory.dream.end`,
  `memory.dream.skipped` (con razón).

**Costo estimado**: ~150 LOC (cron + lock + dispatch + 4 entry points)
+ ~250 LOC tests + ~20 LOC telemetry. Total ~420 LOC.

#### §2.A.2 — Daily cross-session learning (deferido)

**Qué hace** (lo que el cron diario debería SER eventualmente, no lo
que va a ser en MVP): un pass distinto que ve **múltiples entidades a
la vez** y busca patterns emergentes que no son visibles per-entidad:

- Cross-entity pattern detection ("Marcelo + project:X siempre juntos")
- Trend tracking ("user shifted preference A→B en 5 sesiones")
- Meta-learning ("session quality degrades en sesiones largas")
- Ontology evolution semántica ("topic:auth ≈ topic:authn → merge?")

**Estado verificado**: el prompt actual del consolidator
(`durin/templates/dream/consolidator.md:24`) le pide al LLM
"tomar N observaciones sobre **una sola entidad**". No hay path
cross-entity en código ni en prompt.

**Por qué se difiere**: requiere diseño nuevo — no es trivial.
Preguntas abiertas: ¿formato del meta-insight (¿nuevo tipo de page?
¿skill? ¿meta-entity?), ¿cómo se consume en retrieval, frecuencia,
costo en tokens. Doc 18 §6 lo menciona como "Detección de patterns
cross-day" en la sección "v1" del prompt como future work, no como
capability.

**Costo estimado**: ~400+ LOC + diseño previo en doc 26. **No entra
en el MVP de §2.A**.

### §2.H — Fragment/canonical retrieval contract (SHIPPED 2026-05-24 01:30)

**Estado**: `Result` lleva `class_name` / `valid_from` / `entities` / `kind`; `render_block()` produce `=== CANONICAL/FRAGMENT: ... ===` markers (mismo patrón que el `=== ARCHIVED SUMMARY ===` de compaction). El tool `memory_search` agrega `rendered` por result. `search_dreamed` también walkea `memory/entities/<type>/*.md` (no solo las 4 clases viejas), excluyendo `<slug>/archive/`. `hot_layer` agrega dos nuevas secciones — **Canonical pages** (top N entity pages) y **Recent fragments (post-cursor)** (episodic entries que tag entidades + post-cursor), ambos con markers.

**Tests** (21 nuevos en `tests/memory/test_fragment_canonical_contract.py`): cubre `Result.kind` para los 4 valores, `to_dict()` con/sin nuevos fields, `render_block()` header+footer, grep path surface de canonical+fragment, archive exclusion en grep y hot_layer, tool boundary include rendered, hot_layer canonical/fragment sections + pre-cursor filtering, cold-workspace graceful fallback.

**Suite**: 4417 passing (+21), 16 skipped. Live verify CLI OK.

**Commit**: ver bitácora 2026-05-24.

---

### §2.H — Diseño original (referencia histórica)

**Estado verificado** (2026-05-23 22:50): el patrón "memoria principal
+ fragmentos recientes marcados con timestamp + pointer al canonical"
**está diseñado en docs pero NO en el contrato de delivery al LLM**.

Doc 18 §6 línea 303 dice:

> "La página consolidada y los entries post-cursor coexisten en los
> resultados de retrieval; el LLM reconcilia en read-time con
> timestamps y contexto."

Y doc 18 §11 outcome A5:

> "Read-time reconciliation funciona: el LLM lee página consolidada +
> entries post-cursor coexistentes y reconcilia correctamente"

**Lo que falta en código** (los dos paths de delivery):

| Path | Cómo entrega | Gap |
|---|---|---|
| **Eager** (system prompt) | `read_hot_layer().render()` en `agent/context.py:200` | Lee de `memory/<class>/*.md` (las 4 clases viejas), NO de `memory/entities/<type>/<slug>.md`. Las páginas canónicas nuevas no aparecen en el system prompt. |
| **Lazy** (tool result) | `memory_search` → `Result.to_dict()` | Descarta `valid_from`, `entities`, `class_name`. Sin marker textual estilo `=== FRAGMENT (post-cursor, ts=...) ===` o `=== CANONICAL (rev N) ===`. El LLM tiene que inferir del URI prefix, brittle. |

Precedente: el patrón `=== ARCHIVED SUMMARY (source, last active TS,
N msgs condensed) ===` para compaction (bitácora 02 línea 417) muestra
que durin **ya sabe** hacer markers explícitos para distinguir
narrativa de real. Aplicar la misma idea a memory retrieval.

**Por qué importa**: sin §2.H, incluso después de que dream consolida
correctamente, **el LLM no puede usar la distinción canonical vs
fragment**. La promesa de doc 18 §6 ("read-time reconciliation") está
rota en la frontera de delivery — silent quality loss.

**Diseño necesario**:

1. **Lazy path**: extender `Result.to_dict()` a incluir
   `valid_from`, `entities`, `class_name`. Agregar markers textuales
   al output:
   - `class_name=="entity_page"` → `=== CANONICAL: <ref> (updated <date>) ===\n<body>\n=== END CANONICAL ===`
   - `class_name=="episodic"` → `=== FRAGMENT: <ref?>, ts=<valid_from> (post-cursor) ===\n<body>\n=== END FRAGMENT ===`

2. **Eager path**: `hot_layer` lee de `memory/entities/<type>/*.md`
   en lugar de (o junto a) las 4 clases viejas. Misma convención de
   markers.

3. **Documentar el contrato** en `docs/arch/memory.md` para que future
   tools (eager-inject §2.F, eviction §2.G) lo respeten.

**Costo estimado**: ~150 LOC (extender Result + marker rendering +
hot_layer source switch) + ~120 LOC tests + ~30 LOC doc update. Total
~300 LOC.

**Relación con otros items**:
- §2.F (eager-inject) construye sobre §2.H — eager-inject sin
  fragment/canonical contract reproduce el mismo gap a otra escala.
- §2.A.1 puede shipear sin §2.H, pero su valor compounding es menor:
  el dream produce páginas que el LLM no usa correctamente.

Considerar §2.H como **complete-T1**, no T2 — cierra un gap que doc 18
§6 prometía pero el wiring T1 (doc 24) no terminó.

### §2.D — Auto-absorb post-dream

**Estado verificado**: `dream.apply()` NO invoca `find_candidates` ni
`EntityAbsorption`. Solo `cli/memory_cmd.py:594` (cmd_absorb_suggest) y
`cli/memory_cmd.py:574` (cmd_absorb) lo usan.

**Origen**: archived doc 21 §3 T3.1 (tier 3, no T2) + reabierto en
archived doc 24 §7. archived doc 22 §1 A5 matizó:

> "OpenClaude (analog cercano) NO implementó absorption. Eligió
> prevención (prompt teaching) en lugar de cura (merge)."

**Mitigación que SÍ está en T1**: archived doc 22 §3 propuso "vector
similarity check al write-time en memory_store (patrón OpenClaw)" —
shipped como T1.7 en archived doc 23. Reduce duplicates antes de
crearlos, sin necesidad de auto-merge.

**Gate para abrir**: §2.E activo + >5 duplicates/mes observados en
producción (archived doc 21 §4).

**Diseño necesario**:
- Trigger: dentro del dream pass o async post-dream.
- LLM-judge: ¿estos dos refs son la misma entidad?
- Confirmation: silent merge | queue-for-review | no auto.

**Costo estimado**: alto. Toca path crítico + UX nueva.

### §2.E — Telemetry aggregation (SHIPPED 2026-05-23)

**Estado**: implementado. CLI `durin memory stats [--days N] [--json]`
en `durin/cli/memory_cmd.py`; aggregator en `durin/memory/stats.py`
(read-only). Schema fixes paralelos:

- `MemoryRecallVectorEvent` ahora declara los 5 fields que el emit site
  ya producía: `ranking`, `query_entities_count`, `reordered`,
  `top_1_id_before`, `top_1_id_after` (NotRequired para compat con
  eventos pre-W1).
- Nuevo `memory.store.blocked_near_duplicate` event registrado y
  emitido desde el dedup path de `memory_store.py` (T1.7) cuando el
  embedding cae bajo el threshold y bloquea el write — esto cierra el
  data gap del gate §2.D ("duplicates detectados").

**Métricas disponibles**:

Ground truth de disco (no event-derived):
- Episodic entries on disk + cuántas están tageadas (gate §2.A)
- Entity pages on disk + archived post-absorb

Eventos (con filtro `--days N`):
- Total recalls + split vector vs grep
- Vector entity-aware activations (gate §2.F)
- Vector reordered ratio (validación del ranker)
- Store writes + blocked-as-near-duplicate (gate §2.D)
- Ingest events + bytes total
- Embedding loads + cumulative duration

Output: rich tables agrupadas por sección + `--json` para downstream
scripting.

**Costo real**: ~280 LOC (vs estimación previa de ~80) — incluye
schema fixes, emit nuevo, aggregator + CLI + 13 unit tests + 3 CLI
tests. Verificado live contra 88 telemetry files reales.

**Gates ahora medibles**:

| Item | Gate | Cómo leer |
|---|---|---|
| §2.A auto-trigger dream | >50 entries tagged | `durin memory stats` → row "tagged with entities" |
| §2.D auto-absorb | >5 dup/mes | `durin memory stats --days 30` → row "blocked as near-duplicate" |
| §2.F eager-inject | tool-call freq | `durin memory stats --days 7` → "Total recalls" |
| §2.G eviction | corpus size | "Episodic entries on disk" + "Avg vector hits/call" |

### §2.F — Eager-inject context block (vs lazy tool-driven actual)

**Estado verificado**: durin hoy es lazy — el modelo debe invocar
`memory_search` explícitamente. Reference systems hacen lo opuesto:

- Hermes Agent: skill-doc retrieval automático al inicio del turn
- OpenClaw: memoria inyectada al system prompt
- OpenClaude: similar pattern

**Origen**: archived doc 22 §2 N1 (new finding). Patrón **3/3** en
reference systems revisados.

**Por qué se difirió**: cambio arquitectónico mayor. archived doc 21
no lo capturó como T1; emergió de doc 22 verification.

**Gate para abrir**: §2.E activo + métrica de "tool-call frequency"
muestra que el modelo no busca cuando debería (silent retrieval miss).

**Diseño necesario**:
- ¿ContextBuilder lee memoria por turn y la inyecta? ¿Cuánto cabe?
- ¿Eager-search heurística o LLM-driven?
- ¿Coexiste con `memory_search` como fallback o lo reemplaza?
- Persistencia: archived doc 22 §2 N4 sugiere SQLite si eager — pero
  esto agrega complejidad. Evaluar lazy-eager híbrido primero.

**Costo estimado**: alto. Toca ContextBuilder + posiblemente
nueva persistencia.

### §2.G — Eviction/compresión de memory_search results

**Estado verificado**: hoy `memory_search` retorna top_k=10 sin
compresión adicional. Sin telemetría, no sabemos si esto es problema.

**Origen**: archived doc 21 §3 T2.3.

**Por qué se difirió**: archived doc 21 §4 lo deja sujeto a "data real
lo justifica". Sin telemetría, no hay decisión.

**Gate para abrir**: §2.E activo + corpus >N entries (~500+) + métrica
de "results al modelo / results útiles" muestra ratio bajo.

**Diseño necesario** (solo si telemetría justifica):
- Threshold de distancia para cortar antes de top_k.
- Compresión de body en results (warm tier truncation).
- Re-rank con cross-encoder (L2 retrieval — fuera de scope hoy).

**Costo estimado**: bajo si solo es threshold + truncation. Alto si
implica L2.

### §2.C — Shared AliasIndex via ctx (SHIPPED 2026-05-24 00:15)

**Estado pre-fix**: 3 builders independientes en runtime
(`memory_search`, `DreamConsolidator`, `EntityAbsorption`), cada uno
con su `_get_alias_index()` que parseaba `memory/entities/<type>/*.md`
de cero. Sub-segundo per build pero redundante cuando >1 consumer corre
en el mismo proceso.

**Origen**: archived doc 24 W2 ("upgrade to shared via ctx is T2 if
perf needs"). Implementado en doc 25 como §2.C.

**Solución**: `durin/memory/aliases_cache.py` — process-wide singleton
keyed por `memory_root`:

- `get_shared_alias_index(memory_root) -> AliasIndex`: lazy build con
  double-checked locking. Workspace cold → empty index (no excepción).
- `invalidate_alias_index(memory_root)`: drop defensivo para edits
  out-of-band o tests.
- `_clear_all()` / `_cache_size()`: helpers de test.

**Propagación de mutaciones sin invalidación**: el contrato existente
de `AliasIndex.refresh_for()` / `remove()` muta el map in-place. Como
los 3 consumers ahora comparten la **misma instancia**, las
escrituras de uno se ven inmediatamente en los otros — no hay
necesidad de invalidate post-dream/post-absorb (la opción defensiva
está ahí por si futuras paths bypass el contrato).

**Wiring**: cada `_get_alias_index()` consulta el cache excepto si
recibió `alias_index=...` por constructor (inyección de tests).

**Tests**: 15 nuevos en `tests/memory/test_aliases_cache.py` cubren
sharing, propagación de refresh/remove, invalidate, concurrencia (8
threads race-build → 1 instancia), wiring de los 3 consumers. Suite
total: 4396 passing (+15).

**Commit**: ver bitácora 2026-05-24.

### §2.B — Identifier extraction (descartado — ya implementado)

**Por qué se descarta**:

- `EntityPage.identifying_strings()` (entity_page.py:198-247) recorre
  `extra` un nivel deep, incluyendo dicts → identifiers en frontmatter
  (`identifiers: {email: [...], slack: [...]}`) entran al índice.
- `AliasIndex.build()` (aliases_index.py:93) llama
  `page.identifying_strings()`.
- `_tokenize` (entity_ranker.py:121-134) preserva `@.-_+:/`, así que
  `mmarmol@mxhero.com` queda como token único.
- E2E test: `tests/memory/test_phase3_retrieval_e2e.py::test_alias_via_identifier_finds_page`.
- Unit test: `tests/memory/test_aliases_index.py::test_emergent_identifiers_indexed`.
- Dream prompt (`durin/templates/dream/consolidator.md:38`) instruye al
  LLM proactivamente: "ser proactivo extrayendo identifiers (emails,
  phones, slack IDs, github users, etc.)".

Cerrado.

---

## §3 — Backlog ortogonal (doc 20 — UX)

Items UX/UI capturados durante uso real, **no son evoluciones del
sistema de memoria** per se. Viven con su rationale completo (contexto,
problema, propuesta tentativa, estado) en **[20_pendings.md](20_pendings.md)**.

De los items actuales, **P4** (entity cards web) y **P5** (memory ops
trace en session viewer) son los más relacionados al sistema entity-
centric — ambos visualizan lo que ya existe en disco/JSONL, sin tocar
el motor. P5 además se beneficiaría del aggregator de §2.E si se
construye primero.

---

## §4 — Criterios para abrir T2

T2 no se abre por defecto. Para justificar un plan, debería darse
**al menos uno** de:

1. **Uso real revela bottleneck**: un par de semanas usando durin como
   daily-driver + dream manual + búsquedas reales muestra que el caso
   crítico es uno específico.
2. **Caso explícito de stakeholder**: un workflow concreto que falla hoy
   y T2.X lo resolvería.
3. **Métrica observable**: telemetría agregada (§2.E) muestra un patrón
   que un ítem T2 atacaría.

**El criterio 3 es el más defensible** y es el que las gates escritas
en archived doc 21 §4 asumen (>50 entries, >5 duplicates/mes, etc.).
Por eso §2.E es prerequisite estructural: sin agregación, los demás
items T2 se abren con asunción, no con evidencia.

Sin alguno de estos, **el orden por valor incremental es difícil de
defender** y el riesgo de over-engineering es real (doc 02 bitácora
lecciones).

---

## §5 — Próximos pasos (modo MVP completion)

Estrategia: completar las piezas necesarias para que el modelo
entity-centric funcione end-to-end antes de adoptar uso intensivo.
**No** modo "use it, measure, decide later" — modo "build the minimum
viable feature set, then adopt".

### Opción α — §2.H (SHIPPED 2026-05-24 01:30)

Contrato de delivery al LLM cerrado: `Result.kind` + `render_block` +
`rendered` en tool boundary + hot_layer canonical/fragment sections.
21 tests, suite 4417 passing.

### Opción β — §2.A.1 (SHIPPED 2026-05-24)

4/4 triggers en producción. β.1 = cron diario + DreamRunner core; β.2
= post-compaction + session-close + threshold-per-entity + refactor
manual CLI route through DreamRunner. 25 nuevos tests + smoke e2e
de los 4 triggers. Suite 4442 passing.

### Opción γ — §2.C (SHIPPED 2026-05-24)

Limpieza arquitectónica entregada como warmup antes de los items
mayores. Sin dependencias hacia α/β; los 3 consumers ahora comparten
una `AliasIndex` por workspace via `durin.memory.aliases_cache`.

### Otras combinaciones

- α + β en serie: cierra primero el contrato (§2.H), después
  automatiza dream (§2.A.1). Total ~720 LOC.

§2.D (auto-absorb), §2.F (eager-inject), §2.G (eviction) son más
caros y dependen al menos de α.

---

## Last updated: 2026-05-24 02:10 (§2.A.1 β.2 shipped — 4/4 triggers in production, 11 new tests, suite 4442 passing)
