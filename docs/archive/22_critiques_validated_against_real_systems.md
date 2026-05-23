# 22 — Críticas validadas contra implementaciones reales

> Doc 21 propuso un plan basado en mi análisis + glm-5.1 code-grounded.
> Aquí valido cada crítica contra 8 sistemas reales (Hermes, OpenClaw,
> OpenClaude, Cognee, Graphiti, Mem0, MemPalace, HippoRAG, A-Mem)
> usando agentes Explore con citas concretas a file:line.
>
> Cierra con plan ajustado: qué de doc 21 se confirma, qué se matiza,
> qué se agrega.

---

## §1 — Resultados de la verificación por crítica

### A1 — Tool description débil → modelo va a inventar formato

**Estado glm**: tool description larga + vocabulario abierto = modelo
producirá `Persona:Marcelo`, `marcelo` sin tipo, types inventados.

**Lo que hacen los reales:**

- **OpenClaw** (`memory-lancedb/index.ts:610-627`): no enseña al modelo
  el formato. **Auto-detecta category server-side via regex** después
  de que el modelo escribe el texto. El modelo se libera del schema;
  el sistema lo clasifica.
- **OpenClaude** (`memdir/memdir.ts:205-234` + memory system prompt
  línea 199-261): explicit few-shot examples + prohibición explícita
  de duplicates en el prompt + frontmatter validation by prompt
  guidance (NO code enforce).
- **Hermes** (`toolsets.py`): description corta tipo "Search and recall
  past conversations". Sin instrucciones de tagging.
- **Mem0** (`entity_extraction.py:123-358`): NO usa el modelo para
  formato. Usa **spaCy NLP** post-extraction para identificar entidades
  estructuralmente. Determinista, sin LLM en el path.

**Veredicto refinado**: glm tiene razón en que "modelo va a fallar" pero
la solución NO es "obligar al modelo via prompt" (ese juego se pierde
a escala). Las soluciones reales son:

1. **OpenClaw way**: deja al modelo escribir prosa; auto-detecta categoría
   en servidor con regex/heurística.
2. **Mem0 way**: spaCy NER post-extraction; modelo no maneja schema.
3. **Strict validation server-side** (lo que durin ya hace en `execute()`
   con `is_valid_entity_ref`): rechaza con error, modelo reescribe.

**Patrón dominante**: **NO confiar en el modelo para tagging estructurado**.
Sea por auto-detect (OpenClaw) o NER (Mem0) o validation+rechazo (durin
actual), el modelo no es la última línea de defensa.

**Acción ajustada para durin**: tool description corta + 4 ejemplos +
mantener `is_valid_entity_ref` strict en `execute()` (ya hecho). Además,
evaluar agregar fallback tipo OpenClaw: si el modelo pasa `entities=[]`
o no las pasa, regex-detect strings que parezcan entities en el content
del entry.

---

### A2 — Score normalization 1/(1+d) + boost multiplicativo distorsiona

**Estado glm**: distance LanceDB pueden ser 10-50; `1/(1+d)` los aplasta
a [0.02, 0.09]; boost 1.5x sobre 0.04 vs 0.05 es ruido. Recomienda RRF
(Reciprocal Rank Fusion).

**Lo que hacen los reales:**

- **Graphiti** (`search_utils.py:1780-1795`): **USA RRF explícito**:
  ```python
  scores[uuid] += 1 / (i + rank_const)  # 1/(rank+1)
  ```
  + `NODE_HYBRID_SEARCH_RRF` config (`search_config_recipes.py:156-160`).
- **Mem0** (`base.py:8`): post-reranking via cross-encoder o LLM reranker.
  Score normalization opaca (delega al reranker).
- **HippoRAG** (`rerank.py:108-131`): LLM-based filtering puro, sin scores
  numéricos.
- **Cognee, A-Mem**: vector puro top-K, sin reranking ni boost. Distance
  raw.
- **OpenClaw** (`memory-lancedb/index.ts:728-733`): score=cosine cruda
  inyectada al output: `[i] [category] text (score%)`. No multiplica.

**Veredicto: glm acertó 100%.**

- El patrón **score-multiplier** de durin (1.5x/1.4x/0.7x) **no aparece
  en ningún sistema clonado**. Es invención propietaria.
- **RRF es el estándar industria** (Graphiti lo usa explícito). Otros
  hacen reranking post-hoc o vector puro.
- **Las distances LanceDB no son lineales**; multiplicarlas rompe el
  invariante de comparabilidad.

**Acción ajustada para durin**: **reemplazar `entity_ranker.rank_with_entities`
por RRF.** Pipeline:

1. Vector search → ranking A (top-K por distance).
2. Alias_index lookup → ranking B (entities mencionadas en query).
3. Fusión RRF: `score[doc] += 1/(rank_A + k)` + `1/(rank_B + k)`.
4. Sort por score fusionado.

Esto **elimina** los constantes `BOOST_POST_CURSOR`, `BOOST_ENTITY_PAGE`,
`DEMOTE_PRE_CURSOR` — no hay que calibrarlos. El cursor sigue siendo útil
para FILTRAR (no incluir pre-cursor entries en ranking B), no para boost.

---

### A3 — alias_index JSON sidecar → drift state

**Estado glm**: si .md se edita fuera del tool, sidecar JSON queda stale.
Recomienda SQLite o rebuild-only.

**Lo que hacen los reales:**

- **Hermes Holographic** (per doc 16a): SQLite tabla `entities` con
  aliases CSV.
- **OpenClaw memory-wiki**: file-based con index regenerado al boot.
- **MemPalace**: EntityRegistry persistente (database).
- **Cognee, Graphiti**: graph DB nativo (Kuzu/Neo4j).

**Veredicto refinado**: glm tiene razón en que JSON sidecar tiene drift
risk. Pero **SQLite y rebuild-only son ambas válidas**; depende del
tamaño esperado.

- Para corpus durin (cientos de entities en single-user): **rebuild-only
  es suficiente y más simple** (`build()` sub-second per docstring).
- SQLite agrega overhead que solo justifica si las queries cross-entity
  son frecuentes (multi-hop, LIKE patterns).

**Acción ajustada para durin**: drop `save()/load()` paths, mantener
`build()` y `refresh_for()` para incremental in-memory. Boot llama
build, runtime mutaciones llaman refresh_for, no se persiste.

---

### A4 — Dream output parsing es frágil (markdown markers)

**Estado glm**: sin Pydantic validation, sin retry, sin context budget,
sin hallucination detection.

**Lo que hacen los reales:**

- **Graphiti** (`anthropic_client.py:191` + `test_anthropic_client.py:225-251`):
  **Pydantic structured output via tool calling + retry explícito on
  ValidationError**.
- **Cognee** (`llm_entity_extractor.py:15-20`):
  `acreate_structured_output(response_model=EntityList)` — schema
  enforced.
- **Mem0**: spaCy NLP determinista, sin LLM en path. No aplica.
- **MemPalace** (`test_llm_client.py:238-248`): OpenAI JSON mode.
- **OpenClaw** (`markdown.test.ts`): markdown frontmatter + YAML (mismo
  patrón que durin).

**Veredicto: glm acertó 3/4 (validation, retry, context budget). El cuarto
(hallucination detection) NINGÚN sistema lo hace** — es overhead que
nadie absorbió todavía.

**Nota importante sobre JSON-mode vs markdown**: la industria está
dividida 60-40. OpenAI/Anthropic ofrecen JSON-mode robusto desde 2024.
**glm/zhipu NO garantiza JSON-mode igual** — markdown markers para durin
es decisión técnica defensible. Pero hay que **fortalecer el parsing**:

- Pydantic validation del frontmatter parseado.
- Retry hasta N=3 con error feedback al modelo.
- Context budget: max 20-50 entries per consolidation call.
- Tamaño max page (e.g., 25KB hard cap).

**Acción ajustada para durin**:

1. `EntityPage` ya es dataclass; agregar validación strict tras parseo
   del LLM output (campos requeridos, tipos correctos).
2. `DreamConsolidator.consolidate_entity` retry hasta 3 veces con error
   passed back al prompt si parse falla.
3. Cap entries pasadas al prompt: si N>50, batch en grupos de 50.
4. Cap page_text size: si LLM devuelve >25KB, rechazar + retry.

---

### A5 — Absorption es over-engineering para single-user

**Estado glm**: "para single-user, si emerge duplicado borralo a mano".

**Lo que hacen los reales:**

- **Cognee, Graphiti, Mem0, MemPalace, OpenClaw**: NO tienen absorption
  estructurado. Dedup at write-time only (vector similarity 0.95 antes
  de persist, en OpenClaw).
- **OpenClaude** (closest analog a durin — CLI personal, file-based,
  single-user): NO tiene absorption. El user borra a mano si hay
  duplicates. El modelo se entrena a no duplicar via prompt
  ("Do not write duplicate memories. First check if there is an
  existing memory you can update before writing a new one.").

**Veredicto refinado**: el agente que investigó esto defendió que
"absorption es proporcional al use case de durin (multi-mes/año)". Pero
**OpenClaude tiene el MISMO use case** (CLI personal, multi-año) y NO
implementó absorption. Eligió prevención (prompt teaching) en lugar de
cura (merge).

**glm tiene razón parcialmente**: absorption sí es over-engineering
**si la prevención funciona**. Pero la prevención de OpenClaude se basa
en prompts, que ya vimos que el modelo no siempre respeta (A1).

**Patrón emergente más fuerte que ambos**: **OpenClaw "vector similarity
threshold check antes de persist"** (`memory-lancedb/index.ts:783-798`):
si una entry nueva tiene cosine similarity > 0.95 con una existente,
se descarta. Previene duplicados **sin necesitar absorption posterior**.

**Acción ajustada para durin**:

- **CUT auto-trigger de absorption**. Mantener el código (`absorption.py`,
  tests) — bajo costo de mantenimiento; **diferir su uso**.
- **AGREGAR**: vector similarity check al write-time en `memory_store`
  (patrón OpenClaw). Si el contenido nuevo similarity > threshold contra
  existing entries, evaluar si es duplicate / actualizar en vez de crear.
- **Exponer absorption solo via CLI** (`durin memory absorb A B`). User
  manual trigger. No proactivo.

---

## §2 — Hallazgos nuevos no contemplados en doc 21

### N1 — Eager-inject es el patrón dominante (durin actualmente lazy-only)

- **Hermes**: eager-inject via `<memory-context>` tags pre-API.
- **OpenClaw**: eager-inject top-3 via `prependContext`.
- **OpenClaude**: eager-inject MEMORY.md completo (max 200 lines).
- **durin**: lazy-only (modelo llama `memory_search` cuando quiere).

**Implicación**: 3/3 agents reales (los más comparables) usan eager-inject.
Durin's lazy approach es minoritario. Eager inject hace memoria SIEMPRE
disponible; lazy depende de que el modelo recuerde pedirla.

**Acción**: **T2 milestone — evaluar eager-inject de MEMORY.md-style
index** (top 5-10 entities + headlines). El modelo siempre ve qué
entities existen sin tener que adivinar para llamar `memory_search`.

### N2 — Auto-capture post-turn (Hermes/OpenClaw) vs tool-only (OpenClaude)

- **Hermes**: `sync_all(user_msg, response)` post-turn automático.
- **OpenClaw**: hook `agent_end` analiza messages con triggers regex.
- **OpenClaude**: NO auto-capture; solo Write tool del modelo.
- **durin**: tool-only (modelo llama `memory_store`).

**Implicación**: 2/3 sistemas hacen auto-capture. Durin podría reducir
dependencia del modelo agregando un hook post-turn que extracta
candidates a memoria (regex sobre user_msg + response).

**Acción**: **T2 milestone — auto-capture post-turn opcional** con
heurísticas simples (e.g., user dijo "remember that", o el modelo
emitió un tag tipo `<memory-worthy>...</memory-worthy>`).

### N3 — Vector similarity check pre-persist (OpenClaw pattern)

- **OpenClaw**: `memory-lancedb/index.ts:783-798` — threshold 0.95
  contra existing entries antes de insertar.

**Implicación**: previene duplicates en el write path **antes** de que
existan en el corpus. Reduce la necesidad de absorption posterior.

**Acción**: **T1.x — agregar a memory_store**: si embedding del content
nuevo tiene cosine > 0.95 con embedding de alguna entry existente
reciente, log warning o ofrecer update. No bloquea por default; permite
override.

### N4 — System prompt persistence en SQLite (Hermes pattern)

- **Hermes**: persiste system prompt completo en SQLite para Anthropic
  prefix cache warmth.

**Implicación**: si el system prompt incluye memoria eager-injected, su
estabilidad byte-exacta entre turnos es la diferencia entre paying
prefix cache discount vs paying full input tokens. Ahorro real de costo.

**Acción**: **T2 milestone — system prompt persistence**. Solo relevante
si vamos por eager-inject (N1).

### N5 — StreamingContextScrubber (Hermes pattern)

- **Hermes** (`memory_manager.py:62-200`): state machine para limpiar
  `<memory-context>` tags del streaming output en chunk boundaries.

**Implicación**: si vamos por eager-inject vía tags, ESTE pattern es
prerequisito. Sin él, los tags se filtran al output del modelo.

**Acción**: **T2 milestone — si T2 N1 (eager-inject)**, también T2 N5.

---

## §3 — Plan ajustado (delta vs doc 21)

### CONFIRMADO de doc 21 (T1 ship, sin cambio)

- T1.1 Tool description corta + strict validation ✓
- T1.4 alias_index rebuild-only (drop save/load) ✓
- T1.5 `durin memory dream` comando manual ✓

### REVISADO de doc 21

- T1.3 ranker: **reemplazar con RRF explícito** (no normalización + boost
  multiplicativo). Algoritmo concreto en §1 A2.
- T1.2 hook post-turn: **mantener lazy/tool-driven en T1**. Eager-inject
  + auto-capture mueven a T2 (necesitan más diseño y telemetría).

### NUEVO en este doc (no estaba en doc 21)

- **T1.6 — Pydantic validation + retry en dream parsing**: 3 attempts
  con error feedback al modelo si parse falla. Cap entries N=50,
  page size N=25KB.
- **T1.7 — Vector similarity 0.95 check en memory_store**: previene
  duplicates al write-time. Warning + ofrecer update si match.

### T2 (defer post-uso real)

- T2.N1 Eager-inject MEMORY.md style index
- T2.N2 Auto-capture post-turn con heurística
- T2.N3 System prompt persistence (si eager-inject activo)
- T2.N4 StreamingContextScrubber (si eager-inject activo)
- T2.1-2.3 originales (auto-trigger dream, vector index auto-upsert,
  eviction)

### CONFIRMADO cortar / diferir

- ❌ Score-multiplier ranker (reemplazado por RRF)
- ❌ alias_index save/load paths
- ⏸ Absorption auto-trigger (mantener código, exponer solo via CLI)

---

## §4 — Síntesis: glm vs realidad

| Crítica glm | Veredicto post-verificación |
|---|---|
| Tool description débil → modelo inventa | ✓ Confirmado, pero solución no es solo prompt — agregar vector-similarity pre-persist (OpenClaw) |
| RRF > score-multiplier | ✓ Confirmado 100%. Ningún sistema usa el patrón de durin |
| alias_index JSON → drift | ✓ Confirmado. Solución: rebuild-only (no necesita SQLite a esta escala) |
| Dream parsing frágil | ✓ Confirmado 3/4 puntos. Hallucination detection no la hace nadie |
| Absorption es over-engineering | ◐ Matizado. OpenClaude (analog cercano) no lo tiene; pero prevención prompt-based no es robusta. Cortar auto-trigger, mantener código manual |

**Conclusión**: glm acertó en 4/5 críticas. La quinta (absorption) es
matizable. Las dos nuevas direcciones que aportaron los agentes — eager-
inject y vector-similarity-at-write — refuerzan el camino "prevenir > curar".

---

## §5 — Lo que cambia en doc 19 plan de implementación

Doc 19 trazó Phases 0-6 y todas se ejecutaron. **Doc 21 es el plan
post-implementación de integración al loop**. **Doc 22 lo refina con
data empírica de comparación**.

El orden de implementación queda:

1. **T1.1** (strict validation tool) — ya en código, pero description
   debería acortarse + ejemplos few-shot.
2. **T1.3** (RRF) — refactor `entity_ranker.py` reemplazando boost
   multipliers por rank fusion.
3. **T1.4** (rebuild-only alias_index) — drop save/load.
4. **T1.5** (`durin memory dream` comando) — agregar al CLI memory_cmd.
5. **T1.6** (Pydantic + retry + budget en dream) — robustez del parsing.
6. **T1.7** (vector similarity write-time) — write-path dedup en
   `memory_store`.

Tras T1.x + uso real ≥2 semanas, evaluar T2.

---

## Last updated: 2026-05-23 (post-verification of doc 21 critiques)
