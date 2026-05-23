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
> de los demás.

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

### §2.0 — Orden lógico (gate central)

**T2.E es prerequisite estructural** de §2.A, §2.D y §2.F. Las gates
escritas en archived doc 21 §4 son métricas observables — sin
agregación, no podemos justificar abrir los otros:

> "T1 telemetría muestra >50 entries acumuladas con tags. Manual dreams
> funcionan bien (output coherente, 0 hallucinations detectadas en
> sample de 10)."  — archived doc 21 §4

Si se va a abrir T2, empezar por §2.E.

### §2.A — Auto-trigger del DreamConsolidator post-write

**Estado verificado**: `grep -rn "DreamConsolidator(" durin/ --include="*.py"`
devuelve solo `durin/cli/memory_cmd.py:241` (cmd_dream). Sin cron, sin
hook, sin trigger automático. El `agent.dream.Dream` cron-scheduled es
el sistema legacy sobre MEMORY.md, no es entity-centric.

**Origen**: archived doc 21 §3 T2.1.

**Por qué se difirió** (archived doc 21 §2 C1):

> "Cold start letal. Sin entity pages, alias_index está vacío... Hasta
> que dream corra ≥1 vez, ningún componente nuevo aporta."

Ship manual primero, ver fallar lo complejo en uso real, luego medir.

**Gate para abrir**: §2.E activo + >50 entries acumuladas tageadas +
sample de 10 manual dreams sin hallucination.

**Diseño necesario**:
- Disparador: post-N-entries por entidad / post-time / post-session-close.
- ¿LLM-judge antes de consolidar? Probablemente no para v1 — primero
  manual confirm via CLI con queue de pending.
- Telemetría: cost-per-day del dream automático.

**Costo estimado**: medio. ~80 LOC + plan de validación contra corpus
durin real (no sintético).

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

### §2.E — Telemetry aggregation (PREREQUISITE)

**Estado verificado**: `memory.recall.vector` se emite en
`agent/tools/memory_search.py:223` con `{ranking, query_entities_count,
reordered, top_1_id_before, top_1_id_after, duration_ms, hit_count}`.
Nada consume esos JSONL — `grep -rn "memory.recall" durin/cli/` vacío.

**Origen**: archived doc 17 §3.5 ítems 9-10 (Medición direccional) +
archived doc 21 §4 (T2 gate criteria).

**Por qué hoy**: las gates de §2.A, §2.D, §2.F son métricas observables.
Sin agregación, abrir esos items es un acto de fe, no de evidencia.

**Diseño necesario**:
- CLI: `durin memory stats [--days N]` sobre `~/.cache/durin/telemetry/`.
- Métricas mínimas:
  - Entries acumuladas tageadas (gate §2.A)
  - Duplicates detectados (gate §2.D)
  - Frecuencia de tool-call memory_search vs eager-inject candidate (gate §2.F)
  - `reordered` ratio (validación del ranker)
- Output: tabla rich + opcional JSON para downstream.

**Costo estimado**: bajo (~80 LOC). Es read-only, no toca el write path.

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

### §2.C — Shared AliasIndex via ctx (re-scoped)

**Estado verificado**: `grep -n "AliasIndex" durin/`:

| Consumer | Construcción | Línea |
|---|---|---|
| `memory_search` | `_get_alias_index()` lazy | tools/memory_search.py:156 |
| `DreamConsolidator` | `_get_alias_index()` | memory/dream.py:507 |
| `EntityAbsorption` | `_get_alias_index()` | memory/absorption.py:247 |

3 builders independientes en runtime (no 2 como afirmaba la versión
previa de este doc — `memory_store` NO usa AliasIndex). El build es
sub-segundo per docstring de aliases_index.py:24.

**Origen**: archived doc 24 W2 ("upgrade to shared via ctx is T2 if
perf needs"). Pero la afirmación de doc 24 sobre "memory_store has its
own" era incorrecta — pasa lo mismo con doc 25 v1.

**Por qué se difirió**: no es bloqueante. Build sub-segundo + los 3
consumers raramente corren en el mismo `durin agent` run (search es
normal usage, dream + absorb son comandos CLI dedicados).

**Gate para abrir**: §2.E activo + métrica muestra que el mismo
proceso construye AliasIndex >1 vez (e.g. agent que invoca search +
algún flow que también lance dream/absorb).

**Costo estimado**: bajo (~40 LOC + invalidation hooks post-dream/absorb).

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

## §5 — Próximos pasos sugeridos (no decididos)

Tres caminos plausibles, en orden de costo creciente:

### Opción 1: Daily-driving sin instrumentar

- Mantener T1 como está.
- Usar durin con memory.enabled en uso real.
- Pasar dream manual cuando convenga.
- Capturar nuevos pendings en doc 20.
- **Limitación**: sin §2.E, las gates de §2.A / §2.D / §2.F siguen
  siendo "feeling-based" cuando se quiera abrir T2.

### Opción 2: §2.E primero — Telemetry aggregation

- Costo bajo (~80 LOC, read-only).
- Destraba decisiones empíricas sobre §2.A, §2.D, §2.F, §2.G.
- Output: `durin memory stats` con métricas concretas.
- **Recomendado si** el plan es escalar usage o eventualmente
  abrir otro T2 con evidencia.

### Opción 3: UX ítem específico de doc 20

P4 (entity cards web) o P5 (memory ops session viewer) — ambos
visualizan lo que ya existe en disco/JSONL, sin tocar el sistema de
memoria. P5 además se beneficia indirectamente de §2.E (compartiría
el aggregator).

### Opción 4: T2 plan detallado de algún item específico

Solo si §4 criterios 1 o 2 se cumplen para ese item concreto. Doc 26
con clusters de riesgo (mismo patrón que archived 23/24).

---

## Last updated: 2026-05-23 (post T1 wiring + e2e tests doc 24 closure)
