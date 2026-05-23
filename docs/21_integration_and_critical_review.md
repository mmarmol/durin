# 21 — Integración al loop + review crítico post-construcción

> Phase 0-6 produjeron infraestructura entity-centric completa y testeada
> (4335 tests). Cero piezas se invocan en `durin agent` real. Sin
> integración no hay valor.
>
> Este doc combina (a) mi análisis crítico, (b) review de glm-5.1
> grounded en código real, (c) cómo Hermes-agent y OpenClaw integran
> memoria en sus loops. Cierra con plan de integración con cortes
> explícitos y validación por etapas.

---

## §1 — Diagnóstico: construido vs invocado

| Componente | Código | Runtime real |
|---|---|---|
| `memory_store` con `entities:` param | ✓ | ❌ tool desc no fuerza al modelo |
| `DreamConsolidator` | ✓ | ❌ nada lo invoca |
| `AliasIndex` | ✓ | ❌ nunca se construye |
| `entity_ranker` | ✓ | ❌ `memory_search` no lo usa |
| `VectorIndex.upsert_entity_page` | ✓ | ❌ no hay pages porque dream no corre |
| `EntityAbsorption` | ✓ | ❌ nada detecta candidates |
| `durin memory` CLI | ✓ live | ⚠ workspace vacío |

**Sin integración: sistema dormido. Cuatro tests sintéticos pasan porque
construyen su propio mundo. El usuario real ve cero cambio en
comportamiento.**

---

## §2 — Findings críticos

### A. Findings de glm-5.1 (code-grounded review)

glm leyó código real (`memory_store.py`, `entity_ranker.py`,
`aliases_index.py`, `dream.py`) y devolvió crítica concreta:

**A1 — Tool prompting va a fallar en producción real.**
El modelo va a generar `Persona:Marcelo`, `person: Marcelo Marmol` (con
espacio), `marcelo` sin tipo, types inventados. La descripción actual
del parámetro `entities` (memory_store.py:46-56) es manual de
instrucciones disfrazado de schema. Acción: descripción corta + 4
ejemplos concretos + validación regex `[a-z][a-z0-9_]*:[a-z0-9_]+`
estricta en `execute()`. Sin esto el LanceDB se llena de basura
semántica en dos semanas.

**A2 — Score normalization `1/(1+d)` distorsiona topology.**
LanceDB L2 distances pueden estar en rango 10-50 para embeddings
normalizados pobres. Mapeados via `1/(1+d)` quedan en [0.02, 0.09] —
rango minúsculo donde boost 1.5× sobre 0.05 vs 0.04 es irrelevante
para ordering. Asimetría boost/demote (1.5x vs 0.7x) sesga el sistema.
**Alternativa estándar industria: Reciprocal Rank Fusion (RRF)**.
Convierte distances a ranks, fusiona señales por posición, es insensible
a la escala de distances.

**A3 — alias_index sidecar JSON: drift garantizado.**
`save()`/`load()` sin checksum ni mtime validation. Si un `.md` se edita
fuera del tool (vim, git merge, CI), sidecar queda stale; `lookup()`
devuelve refs inexistentes. Para corpus chico (cientos de aliases),
**la solución es rebuild al boot siempre** (`build()` ya existe, es
sub-second). Eliminar `load/save` paths.

**A4 — Dream parsing es frágil. Faltan 3 safety nets:**
- Validación structurada del page_text: si el LLM injecta `===PAGE===`
  dentro del contenido (no improbable), el regex se rompe.
- Hallucination detection: no se verifica que entities-touched del
  output sea subset de entities en input.
- Context budget: no hay límite en `entries` pasados al prompt. 500
  entries → context overflow.
- Tamaño max del page_text no validado: LLM verbose genera 50KB.

**A5 — Si glm cortara mañana**: `dream.py` entero. Reemplazar por stub
que escribe template fijo (sin LLM call). Cortar boost del ranker.
Dejar solo: `memory_store` core + `aliases_index` rebuild-only +
retrieval por vector distance plain. Eso shippa. Validar en uso real
antes de re-agregar complejidad.

### B. Patrones observados en agents reales

Análisis de `~/git_personal/hermes-agent/` y `~/git_personal/openclaw/`:

**Hermes (Python) — patrón "post-turn sync automático":**

- **WRITE**: hook `agent._memory_manager.sync_all(user_msg, response)` al
  final del turno (`conversation_loop.py:2003-2007`). El modelo NO
  llama tool — la sincronización es automática post-turn.
- **READ**: `prefetch_all()` UNA vez al inicio del turno
  (`conversation_loop.py:580`); resultado se cachea para todo el tool
  loop. El contexto se inyecta en el user message vía
  `build_memory_context_block()` (`:754`) wrapped en
  `<memory-context>...</memory-context>` tags.
- **System prompt persistido en SQLite** para reutilización de prefix
  cache (warmth de tokens).
- Sin consolidation/dream LLM-driven visible.

**OpenClaw (TypeScript) — patrón "tool-driven lazy":**

- **WRITE**: model-initiated tool calls (`memory_recall`, etc.). No hay
  sync automático. El modelo decide qué guardar.
- **READ**: lazy via tools. Sin prefetch.
- Plugin-style activation per session.

**Patrones que durin NO contempló:**

1. **System prompt persistence en SQLite** para prefix cache warmth.
   Crítico para ahorrar tokens en cada turno.
2. **Prefetch caching una sola vez por turno**. Sin esto cada tool call
   golpea el vector DB.
3. **Streaming context scrubber** — limpia `<memory-context>` tags en
   chunk boundaries sin romper streaming.
4. **Eager-inject via tags vs lazy tool-driven**. Hermes opta por
   eager: el modelo SIEMPRE tiene memoria visible. OpenClaw opta por
   lazy: el modelo PIDE cuando necesita.

### C. Mi análisis transversal

**Tres riesgos que sumo:**

**C1 — Cold start letal.** Sin entity pages, alias_index está vacío;
`extract_query_entities` devuelve []; el ranker pasa todo through sin
boost; el sistema es vector search plano con overhead extra. **Hasta
que dream corra ≥1 vez, ningún componente nuevo aporta.**

**C2 — Eviction missing** (glm también lo marcó como crítico):
`memory_search` puede devolver 10 entries + 2 pages = 10-15k tokens
solo de memoria inyectada. Sin compresión / re-ranking por relevancia
fina, el contexto del agente principal se degrada por sobrecarga, no
por imprecisión.

**C3 — Vocabulary drift confirmado por glm**: el modelo va a inventar
slugs inconsistentes sin enforcement. Si pasa al inicio (cold start) y
seguimos guardando entries con `entities` malformados, el dataset queda
contaminado de origen. Esto refuerza A1: **validar y rechazar en
write-time es bloqueante**.

---

## §3 — Plan de integración con cortes explícitos

Estrategia: **ship lo simple, ver fallar lo complejo en uso real, luego
medir y mejorar con datos**. No al revés.

### Tier 1 — MVP integración (lo que ship sí o sí)

**T1.1 — Tool description corta + validación strict.**
- `memory_store.py:46-56`: reescribir descripción a 2 líneas + 4
  ejemplos (`person:marcelo`, `project:durin`, `topic:embeddings`,
  `event:bug-X`).
- `execute()` rechaza con error explícito si algún entity no matchea
  `^[a-z][a-z0-9_]*:[a-z0-9_]+$` (forma estricta). El modelo recibe
  el error y reescribe.
- **Sin esto el dataset se contamina.**

**T1.2 — Hook post-turn al estilo Hermes** (no tool-driven).
- En `agent/loop.py` después del turno completo: hook que decide si la
  respuesta del modelo tuvo aprendizaje memoriable (heurística simple:
  ¿el usuario marcó como importante, o el modelo lo flageó vía un
  tag de output?). Si sí, escribir memory entry con entities tagged.
- Patrón Hermes (`sync_all`) — automático, no depende de que el modelo
  recuerde llamar tool.
- Alternativa más simple v1: mantener tool-driven (`memory_store`)
  pero asegurar T1.1 (strict validation).

**T1.3 — `memory_search` integra el ranker (sin boost).**
- Reemplazar el ranker `1/(1+d)` + boost multiplicativo por **RRF**:
  - top-K de vector search → un ranking
  - top-K de alias_index lookup (case-insensitive) → otro ranking
  - fusión por posición, no por score numérico
- Aplica solo cuando alias_index tiene contenido (>0 entities). Si
  está vacío, fallback a vector ranking puro.
- **Cortar el boost multiplicativo entero** (per glm A2).

**T1.4 — alias_index rebuild-only.**
- Borrar el código `save()`/`load()` del sidecar JSON.
- `build()` se llama una vez al boot del agente, mantiene en memoria.
- Tras cada write de entity page (en producción, después de dream o
  edición manual), `refresh_for()` actualiza en memoria.
- Si el agent muere y reinicia, rebuild es sub-second. Aceptable.
- **Cortar `aliases_index.save/load`.**

**T1.5 — `durin memory dream` como comando manual** (no auto-trigger).
- El comando ya existe? No, hay que agregarlo: lee entries de
  `memory/episodic/` con tags, agrupa por entidad, invoca consolidator.
- v1 no auto-trigger. El usuario corre `durin memory dream` cuando
  quiere. Si no corre nunca, las entries siguen siendo searchables
  (anti-fragility de doc 18 §3.4).
- **Esto valida el dream con uso real antes de meterlo en hot path.**

### Tier 2 — Solo después de T1 + telemetría de 2 semanas

**T2.1 — Auto-trigger del dream** (session-end o threshold).
**T2.2 — Vector index entity pages auto-upsert tras dream apply** (ya
está en código, solo falta pasar `vector_index` al `DreamConsolidator`
en producción).
**T2.3 — Eviction / compresión de results** antes de inyectar al
context del agente principal.

### Tier 3 — Cortar / diferir indefinidamente

**T3.1 — Absorption (Phase 5 entera).** Per glm A5: over-engineering
para single-user. Si emergen duplicados, borrarlos a mano. Reabrir
solo si telemetría muestra >5 duplicados/mes en uso real.

**T3.2 — Multi-factor boost matemático.** Reemplazado por RRF en T1.3.

**T3.3 — Dream con LLM rico** sin las safety nets (A4). Diferir hasta
agregar: validation Pydantic del frontmatter, hallucination check
(diff entities pre/post), context budget per consolidation (max N
entries), tamaño máximo de page.

**T3.4 — `EntityAbsorption.find_candidates` y CLI command.** Forma
parte de T3.1.

### Resumen — qué se shippa, qué se corta

```
SHIP (T1):
  ✓ strict entity validation en memory_store
  ✓ RRF en memory_search
  ✓ alias_index rebuild-only
  ✓ durin memory dream manual command
  ✓ tool description tight con ejemplos

CORTAR (T3):
  ✗ EntityAbsorption (durin/memory/absorption.py)
  ✗ Multi-factor boost ranker (1.5/1.4/0.7)
  ✗ alias_index save/load paths
  ✗ Dream sin safety nets

DEFER (T2):
  ⏸ Auto-trigger dream
  ⏸ Vector index auto-upsert
  ⏸ Eviction / compression
  ⏸ Telemetría retrieval (Phase 0.2)
```

---

## §4 — Validación de cada Tier

**T1 ship criteria:**

- 2 semanas de uso real del autor con MVP integrado.
- Telemetría minimalista: cuántas `memory_store` calls hicieron entities
  válidos vs rechazados. Si >20% rechazados, el prompt necesita más
  trabajo.
- Manual smoke: tras 50 entries, correr `durin memory dream` sobre 1
  entidad real. Verificar output a mano.

**T2 trigger:**

- T1 telemetría muestra >50 entries acumuladas con tags.
- Manual dreams funcionan bien (output coherente, 0 hallucinations
  detectadas en sample de 10).

**T3 reapertura:**

- Solo si data real lo justifica (>5 duplicates/mes para absorption,
  o degradación measurable de retrieval para boost matemático).

---

## §5 — Cosas que doc 18/19 no cubrieron y son ahora obvias

1. **System prompt persistence (Hermes pattern)** — durin debería cachear
  el system prompt en SQLite para prefix cache warmth. No es entity-
  centric pero impacta cost/latency directamente.
2. **Prefetch caching** — una vez por turno, no por tool call.
3. **Eager inject vs lazy tool** — durin actualmente lazy (memory_search
   tool). Hermes eager. Para MVP T1, mantener lazy (menos disrupción).
   Evaluar eager en T2.
4. **Eviction** (C2 / glm-omisión-crítica) — no diseñamos cómo no
   inundar el context. Asunción implícita: el top-K de search es
   pequeño. Pero entity_page + 5 entries pueden ser 10K tokens. Necesita
   un cap explícito.

---

## §6 — Próximo paso

Implementar T1.1 (tool description tight + strict validation). Es el
prerequisito de todo lo demás: sin entities consistentes, la
infraestructura completa es pintura sobre una pared que se desmorona.

Una vez T1.1 esté shippado y testeado en runtime real (1-2 semanas), reevaluar
qué de T1.2-T1.5 vale construir, y qué de T3 efectivamente se queda
cortado.

---

## Last updated: 2026-05-23 (post Phase 0-6 critical review)
