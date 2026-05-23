# 25 — Post-T1 state + T2 horizon

> Cierre de T1 (entity-centric memory shipped + wired + verified, mayo 2026) y
> horizon T2 sin compromiso de implementación. Este doc captura el estado
> verificado, los ítems explícitamente diferidos durante T1, y el backlog
> ortogonal de UX para que la próxima decisión de qué construir se tome
> con la evidencia delante.
>
> No es un plan ejecutable. Cuando se elija un ítem se abre doc 26 (o el que
> corresponda) con plan detallado por clusters de riesgo (mismo patrón que
> docs 23 + 24).

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

## §2 — Ítems diferidos a T2 durante T1

Capturados durante T1 con razón explícita de por qué no entraron. Esta es
la lista que un eventual plan T2 puede priorizar.

### T2.A — Auto-trigger del DreamConsolidator post-write

**Contexto**: hoy el dream corre solo manualmente vía `durin memory dream`.
Doc 18 §6 prevé un trigger automático (post-session, post-N-entries,
threshold-based).

**Por qué se difirió** (doc 24 §7): auto-trigger sin LLM-judge + confirm
flow tiene riesgo silencioso (consolida cuando no debe → drift). Decisión
en T1: "v1 = manual command, NO auto-absorb".

**Diseño necesario antes de implementar**:
- Disparador (post-N-entries, post-time, post-session-close).
- LLM-judge: ¿esta entrada deserves consolidation now? (opcional).
- Confirmation flow: ¿auto o silent? Para qué clases de entries.
- Telemetría: cost del dream automático (cost-per-day).

**Costo estimado**: medio. ~80 LOC + plan de validación contra corpus
durin real (no sintético).

### T2.B — Identifier-based extraction en query

**Contexto**: `extract_query_entities` busca por alias (nombre, abreviación).
Si query trae `mmarmol@mxhero.com`, no resuelve a `person:marcelo` por
identifier — porque el matching es contra `aliases:`, no contra
`identifiers:` del frontmatter.

**Por qué se difirió**: no apareció como blocker en doc 19 §3.1 ni en
T1.x. La extracción por alias cubre el caso dominante (queries naturales
mencionan nombres, no IDs).

**Diseño necesario**:
- ¿AliasIndex extiende a identifier (un solo índice case-insensitive con
  alias + identifier merged)? ¿O índice separado?
- Regex/heuristic vs LLM-judge para detectar identifiers en query.
- Test cases: emails, slack IDs, github handles, phone, etc.

**Costo estimado**: bajo si el approach es extender AliasIndex (~30 LOC +
tests). Alto si requiere LLM-judge.

### T2.C — Shared AliasIndex via ctx

**Contexto**: hoy `memory_search` construye su propio AliasIndex (lazy,
sub-segundo), y `memory_store` construye otro independiente. Ambos
parsean el mismo set de entity pages.

**Por qué se difirió** (doc 24 W2): "different responsibility; upgrade
to shared via ctx is T2 if perf needs". No es bloqueante porque el build
es sub-segundo para corpora <100 pages.

**Diseño necesario**:
- ¿ctx-scoped (per agent run) o process-scoped (singleton)?
- Invalidación cuando entity pages cambian (post-dream, post-absorb).
- ¿Refresh lazy o eager?

**Costo estimado**: bajo. ~40 LOC + invalidation hooks.

### T2.D — Auto-absorb post-dream

**Contexto**: hoy `durin memory absorb` es manual. El dream detecta
candidatos vía `find_candidates()` pero no actúa.

**Por qué se difirió** (doc 24 §7): "auto-absorb async tras CADA dream
tiene riesgo silencioso". Necesita LLM-judge + confirmation.

**Diseño necesario**:
- Trigger: dentro del dream pass o async post-dream.
- LLM-judge: ¿estos dos refs son la misma entidad? (con context).
- Confirmation: ¿silent merge, queue-for-user-review, o no auto?

**Costo estimado**: alto. Toca el path crítico del dream + UX nueva.

### T2.E — Persistir telemetría agregada para tuning

**Contexto**: hoy `memory.recall.vector` event lleva `ranking`,
`query_entities_count`, `reordered`, `top_1_id_before/after`. Se
emite a JSONL pero no se agrega.

**Por qué se difirió**: agregación es post-hoc, no operativa. Sirve
solo si vamos a tunear el ranker.

**Diseño necesario** (solo si se va a tunear):
- Dashboard local sobre los JSONL existentes.
- Trigger de re-tune (frecuencia de `reordered=true`, etc.).

**Costo estimado**: bajo si se hace como query CLI sobre JSONL. Alto
si se necesita UI.

---

## §3 — Backlog ortogonal (doc 20 — UX)

Capturado durante uso real, no son evoluciones del sistema de memoria
per se. Listado completo y rationale en [20_pendings.md](20_pendings.md):

| ID | Tema | Donde |
|---|---|---|
| P1 | Edición campos password/key en web (secrets) | web settings |
| P2 | Edición de nombres de sesiones | web + TUI |
| P3 | Comando `/model` con autocompletion progresivo | TUI + web |
| P4 | UI de gestión de entidades (entity cards) | web |
| P5 | Tracing de tool calls de memoria en session viewer | web + TUI |

P4 y P5 son los más relacionados al sistema de memoria nuevo — surface
visual sobre lo que ya existe en disco.

---

## §4 — Criterios para abrir T2

T2 no se abre por defecto. Para justificar un plan, debería darse
**al menos uno** de:

1. **Uso real revela bottleneck**: un par de semanas usando durin como
   daily-driver + dream manual + búsquedas reales muestra que el caso
   crítico es uno específico (e.g. "queries por email no resuelven" →
   T2.B sube de prioridad).
2. **Caso explícito de stakeholder**: un workflow concreto que falla hoy
   y T2.X lo resolvería.
3. **Métrica observable**: telemetría agregada (T2.E primero) muestra
   un patrón que un ítem T2 atacaría.

Sin alguno de estos, **el orden por valor incremental es difícil de
defender** y el riesgo de over-engineering es real (doc 02 bitácora
lecciones).

---

## §5 — Próximos pasos sugeridos (no decididos)

Tres caminos plausibles, en orden de costo creciente:

### Opción 1: Daily-driving + medir

- Mantener T1 como está.
- Usar durin con memory.enabled en uso real.
- Pasar dream manual cuando convenga.
- Capturar nuevos pendings en doc 20.
- Re-evaluar en 2-4 semanas si algún ítem T2 emerge como obvio.

### Opción 2: UX ítem específico de doc 20

P4 (entity cards web) o P5 (memory ops session viewer) — ambos visualizan
lo que ya existe en disco/JSONL, sin tocar el sistema de memoria.

### Opción 3: T2 plan detallado

Solo si §4 criterios se cumplen. Doc 26 con clusters de riesgo (mismo
patrón que 23/24).

---

## Last updated: 2026-05-23 (post T1 wiring + e2e tests doc 24 closure)
