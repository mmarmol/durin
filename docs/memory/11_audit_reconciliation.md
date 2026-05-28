---
title: Reconciliación auditoría doc ↔ código (2026-05-28)
version: 1.0
status: living document — se cierra item por item
last_updated: 2026-05-28
audience: humans + LLMs cerrando deuda doc/código
depends_on: docs/memory/00..10 (audited)
---

# Reconciliación auditoría — doc vs código

Este doc lista cada discrepancia encontrada entre `docs/memory/00..10` y el código real en `durin/`. Cada item incluye:

- **Doc dice** — cita verbatim + cita `file:line`
- **Código dice** — cita verbatim + cita `file:line`
- **Quién tiene razón** — evaluado con justificación
- **Acción propuesta** — fix code, fix doc, o ambos
- **Estado** — `pending` / `resolved` / `wontfix`

**Regla**: no asumir nada. Sólo lo verificado con `grep`/`read` directos sobre el código actual entra como "code dice".

**Orden**: critical (1-10), medium (11-22), low (23+). Resolvemos uno por uno en orden, comenzando por los que pueden romper UX del agente.

---

## CRITICAL — afectan UX del agente o operación

### A1 — `memory_ingest`: descripción promete API que el schema no implementa

**Doc dice** (`docs/memory/04_agent_tools.md:200-209`):

```json
{
  "source": "string (required, can be: file path, URL, or 'inline')",
  "content": "string (required if source='inline')",
  "title": "string (optional)",
  "entities": "array of <type>:<value> strings (optional)",
  "chunking": "auto | none (default: auto)"
}
```

**Código dice** (`durin/agent/tools/memory_ingest.py:42-47`):

```python
_PARAMETERS = tool_parameters_schema(
    path=StringSchema(
        "Absolute path (or workspace-relative path) to a markdown or "
        "plain-text file the user wants the agent to remember."
    ),
    required=["path"],
    ...
)
```

La **descripción canónica** sincronizada con `docs/memory/06_prompts_and_instructions.md` §3.3 (líneas 48-65 del mismo archivo) además publica `source`/`URL`/`"inline"`/`content` al LLM. El LLM entonces invoca `memory_ingest(source="https://...", content=...)` y falla con `unknown parameter`.

**Quién tiene razón**: ambiguo. La intención original (doc) es razonable — un tool de ingest debería aceptar URL e inline. La implementación se quedó corto. **El doc es la dirección correcta**; el código está incompleto.

**Acción**: extender el schema y la lógica de `memory_ingest.execute` para:
- `source` (req): file path | URL | "inline"
- `content` (opt): texto cuando `source="inline"`
- `title` (opt)
- `entities` (opt)
- `chunking` (opt, default auto)

Mantener compatibilidad con `path` (alias o paso de migración).

**Riesgo**: implementar URL fetch trae questions (timeouts, SSRF, content-type sniffing). Si se prefiere reducir scope: **alinear el doc al código** (sólo `path`) y dejar URL/inline como deferred. Decisión humana.

**Resolución (2026-05-28)**: Opción 2 — alinear el doc al código. Razón clave descubierta durante la decisión: **`web_fetch` ya existe** ([durin/agent/tools/web.py:454](durin/agent/tools/web.py#L454)) y ya hace URL → markdown con SSRF protection, Jina/readability extractors, image detection. La rama URL en `memory_ingest` no era una capability faltante sino una **duplicación pendiente**. Similar para "inline": `memory_store(class_name="corpus")` cubre el caso. Cambios:

- `_PARAMETERS["description"]` en [memory_ingest.py:48-68](durin/agent/tools/memory_ingest.py#L48-L68) reescrito para reflejar sólo `path` + dirigir al workflow correcto (`web_fetch` + `memory_store`).
- [docs/memory/04_agent_tools.md](docs/memory/04_agent_tools.md) §4.1, §4.2, §4.3 y §10 (status table) actualizados.
- [docs/memory/06_prompts_and_instructions.md](docs/memory/06_prompts_and_instructions.md) §3.3 sincronizada.
- [docs/memory/08_scope_and_discarded.md](docs/memory/08_scope_and_discarded.md) §2.8 nueva entry con la genealogía del error y la lección sobre sync tests.

**Lección sobre sync tests**: `test_tool_description_sync.py` valida igualdad de strings, no comportamiento. Pasó verde con el doc mintiéndole al LLM desde commit `572d5cf` (2026-05-28 09:28 +0200) hasta el fix `bce9092` (~1 hora después). El drift fue corto por suerte — el audit lo agarró la misma mañana, pero el test no lo habría detectado nunca. Fix general para tests de "sync" en futuro: ejercitar el comportamiento, no sólo comparar strings.

**Estado**: resolved (commit pendiente).

---

### A2 — `memory_store` parámetros divergen entre doc, código y descripción interna

**Doc dice** (`docs/memory/04_agent_tools.md:134-144`):

```json
{
  "headline": "string (required)",
  "body": "string (required)",
  "class_name": "stable | episodic (default: episodic)",
  "entities": "array of <type>:<value> strings (optional)",
  "summary": "string (optional, default: auto-generated)",
  "source_refs": "array of strings (optional)",
  "valid_from": "ISO date (optional)"
}
```

**Código dice** (`durin/agent/tools/memory_store.py:24-68`): parámetros = `content` (req), `class_name` (enum incluye `corpus`/`pending`), `headline` (opt, auto-gen), `summary` (opt), `source_refs` (opt), `entities` (opt), `force` (opt). **No existe `valid_from`. No existe `body` — se llama `content`.**

**Descripción canónica del propio código** (`memory_store.py:83`, sincronizada con doc 06 §3.2):
> *"Keep `headline` short and specific. `body` should be the full content; don't truncate."*

→ El propio tool habla de `body` en la descripción al LLM, pero el parámetro real es `content`. **El código es inconsistente consigo mismo.**

**Quién tiene razón**: parcialmente cada uno.
- `content` vs `body`: el código es más viejo, doc 04 propuso `body`. Renombrar el parámetro a `body` no rompe nada externo (los tools sólo se invocan vía schema), pero rompe tests internos y código que llama a `store_memory(content=...)`. **Mejor: actualizar doc 04 y descripción del tool a `content` para minimizar cambio** — el dato está, sólo el nombre difiere.
- `valid_from`: doc lo propone, código no lo tiene. No es accionable hoy (no se usa para temporal scoring porque decay no está cableado, ver A9). Defer hasta que decay opere — entonces sí tiene sentido.
- `force`: existe en código (skip-dedup), doc 04 no lo menciona. **Doc tiene razón en el sentido de "el agente nunca debería verlo"** — `force=true` es para humans/tools usando el tool programáticamente. Pero como está expuesto al LLM, debería documentarse o quitarse del schema y exponerse sólo via API interna.
- `class_name` enum: código incluye `corpus`/`pending`; doc dice "stable | episodic". El código es correcto — el LLM debería poder almacenar `corpus` (aunque normalmente lo hace `memory_ingest`) y `pending` (TODOs). **Doc desactualizado.**

**Acción**:
1. Doc 04 §3.1: renombrar `body` → `content`, ampliar enum `class_name`, agregar `force` (con caveat "rara vez relevante"), marcar `valid_from` como deferred.
2. Descripción del tool (`memory_store.py:83`): cambiar `"body"` → `"content"`.
3. Recurrir el sync test después.

**Resolución (2026-05-28)**: cinco discrepancias auditadas individualmente. Cambios shipped:

1. **`pending` removed from agent-facing enum** ([memory_store.py](../../durin/agent/tools/memory_store.py): nuevo `_AGENT_FACING_CLASSES = ("stable", "episodic", "corpus")` reemplaza `list(MEMORY_CLASSES)`). Razón verificada: `paths.py::walk_memory` + `indexer.py` + `file_watcher.py` todos excluyen `memory/pending/**`. Escribir ahí desde el LLM era data loss silencioso. Internal callers (compaction) siguen usando la función pura `store_memory`.

2. **`body` → `content` en doc 04 §3.1**. El campo persistido del `MemoryEntry` SÍ se llama `body` (declarado en doc 01 §3.3), pero el parámetro del tool y de la función pura siempre fueron `content`. Doc 04 v1 confundió los dos planos. Doc 04 v2 explicita la asimetría.

3. **`valid_from` NO se expone como param del tool**. Es campo real del `MemoryEntry` con uses downstream legítimos (hot_layer cursor compare, entity_ranker pre/post, sort de fragments). Default automático `date.today()`. **El consumer que necesita back-datear (LoCoMo bench) usa la función pura directamente** ([locomo_harness.py:227-233](../../scripts/benchmark/locomo_harness.py)), no el tool. 99% de los stores del LLM son "ahora" — exponer el knob agrega ruido al schema sin caso de uso real.

4. **`headline` queda optional**. Auto-gen [`store.py:106-109`](../../durin/memory/store.py) usa primeros ~10 words; razonable para LLM-generated content. Required agregaría latencia sin beneficio claro.

5. **`force` documentado** en doc 04 §3.1 con caveat ("rara vez relevante"). Existe en código desde commit `d34b337` para bypass del dedup near-duplicate check; doc 04 v1 lo omitió por oversight.

Cambios al canónico ([doc 06 §3.2](06_prompts_and_instructions.md)) reflejan los 5 puntos; `_PARAMETERS["description"]` sincronizada verbatim. Doc 04 §3.1/§3.2/§3.3/§9 (decision 5b) actualizados. Nueva entry [doc 08 §2.9](08_scope_and_discarded.md) con justificación completa + lecciones (enum-as-trap, param-vs-field, default-beats-knob).

**Lecciones nuevas**:
- *Enum values pueden ser trampas* — no mirror ciegamente un constants tuple a tool-facing enum sin verificar que TODO el sistema honra cada miembro.
- *Tool param name ≠ persisted field name* — cuando difieren, documentar AMBOS planos explícitamente.
- *Default behavior often beats new tool params* — antes de exponer un knob, preguntar quién lo necesita realmente; si es un internal pipeline, dejar la function pura como su path.

**Estado**: resolved (commit pendiente).

---

### A3 — `memory_search` `limit` documentado pero no expuesto

**Doc dice** (`docs/memory/04_agent_tools.md:42`):

```json
"limit": "integer (default: 10, max: 50)"
```

**Código dice** (`durin/agent/tools/memory_search.py:53-77`): `_PARAMETERS` sólo tiene `query`, `scope`, `level`, `keywords`. El límite está hardcoded a 10 en `memory_search.py:348`:

```python
pipeline_result = run_search_pipeline(
    self._workspace,
    query,
    keywords=keywords,
    vector_index=vi,
    limit=10,                # ← hardcoded
    ...
)
```

**Quién tiene razón**: **doc tiene razón**. Exponer `limit` al LLM es útil — algunas queries quieren top-3 (chat corto), otras quieren top-30 (auditoría). Hoy el LLM no tiene control.

**Acción**: agregar `limit: IntegerSchema(default=10, min=1, max=50)` al schema, pasar al `run_search_pipeline`.

**Resolución (2026-05-28)**: Opción A — exponer `limit`. A diferencia de A1 (URL duplicaba `web_fetch`) y A2 (varios knobs eran trampa), aquí **el pipeline ya soporta el parámetro** ([search_pipeline.py:71](../../durin/memory/search_pipeline.py#L71)), sólo faltaba propagarlo desde el tool. Doc 03 §1 y Doc 04 §2.1 ambos lo proponían — propuesta consistente, no invento aislado.

Cambios:
- Schema: `limit: IntegerSchema(10, minimum=1, maximum=50)` agregado a [memory_search.py](../../durin/agent/tools/memory_search.py).
- `execute()`: clamp defensivo `max(1, min(50, int(...)))` con fallback a 10 cuando la coerción falla.
- Llamada al pipeline: `run_search_pipeline(..., limit=limit, ...)` en vez del `limit=10` hardcoded.
- Doc 06 §3.1 canonical + descripción del tool: mención breve con guidance ("3-5 para chat-short, 20-30 para audit/investigative, hard cap 50").
- Test nuevo [test_memory_search_limit_param.py](../../tests/memory/test_memory_search_limit_param.py): 7 tests que **ejercitan el comportamiento**, cumpliendo la lección de [[feedback-sync-tests-exercise-behavior]]:
  - Schema declarado correctamente.
  - Default 10 cuando se omite.
  - `limit=5` recorta a 5.
  - `limit=30` permite más (con 25 entries seedeadas).
  - `limit=999` clamp a 50.
  - `limit=0` clamp a 1.
  - `limit="abc"` fallback a 10 (string-coerce graceful).

**Verificado pre-commit**:
- `IntegerSchema(value, description, minimum, maximum)` signature contra [schema.py:54-72](../../durin/agent/tools/schema.py#L54-L72).
- `tool_parameters_schema(...)` devuelve dict (no objeto), corregido el test después del primer falso intento — error tipo aplicación de [[feedback-verify-quantifiers]].
- Default 10 = comportamiento previo: **no breaking change**.

**Estado**: resolved (commit pendiente).

---

### A4 — LanceDB schema en doc 02 §3.1 ≠ columnas reales

**Doc dice** (`docs/memory/02_indexing.md:65-79`):

| Column | Type |
|---|---|
| `uri` | string PK |
| `path` | string |
| `type` | string (`entity`, `episodic`, `stable`, `corpus`, `session_summary`) |
| `entity_type` | string \| null |
| `entities` | list of strings |
| `vector` | fixed list of floats (**768**) |
| `mtime` | float |
| `headline` | string |
| `summary` | string |
| `valid_from` | string \| null |
| `indexed_at` | string |

Y *"**No `body` column.** Storing the body in LanceDB would double the index size for no retrieval benefit."*

**Código dice** (`durin/memory/vector_index.py:131-147`):

```python
record: dict[str, Any] = {
    "id": entity_ref,
    "class_name": "entity_page",
    "summary": summary,
    "headline": name,
    "vector": vec,
    "valid_from": "",
    "entities": [],
    "path": str(rel_path),
    # P2.5: full body for cold-tier reads without disk hits.
    "body": body or "",
}
```

Columnas reales: `id, class_name, summary, headline, vector, valid_from, entities, path, body`. Dim del vector: 384 (modelo default `paraphrase-multilingual-MiniLM-L12-v2` emite 384, no 768 — `vector_index.py:444` comenta migración "from 384-dim to 1024-dim").

**Quién tiene razón**: **código tiene razón**. P2.5 (commit `a266344`) agregó `body` deliberadamente como trade-off explícito (doblar tamaño del índice vs ahorrar N file reads para cold queries). El doc nunca se actualizó.

**Acción**: actualizar doc 02 §3.1:
- Renombrar `uri` → `id`, `type` → `class_name`. Sacar `entity_type`, `mtime`, `indexed_at` (no existen).
- Agregar `body` con la justificación P2.5 (doblar tamaño es aceptable porque ahorra disk hits cold-tier).
- Corregir dim 768 → 384.
- Actualizar §3.2 "Dim: 768" → "Dim: 384 (default; cambiar requiere full rebuild)".

**Resolución (2026-05-28)**: durante el análisis el usuario empujó con la pregunta clave — *"el doc real se mantiene en disco fue para no replicar toda la información; la fuente de verdad es el doc en disco no la base de datos"*. Verificación columna por columna mostró que `body` era el ÚNICO campo que duplicaba contenido sustancial del `.md` en LanceDB. P2.5 (commit `a266344`, 2026-05-28 09:10) había violado el principio arquitectónico original por una optimización de latencia (~5-10 ms ahorrados en disk reads de cold-tier) que NO era bottleneck — el LLM call downstream toma segundos.

**Decisión**: revertir P2.5 + alinear doc al schema real. Cambios al código:

- [vector_index.py:131-147](../../durin/memory/vector_index.py#L131-L147) (entity-page record) y [:360-377](../../durin/memory/vector_index.py#L360-L377) (entry record): remover el campo `body` del dict. Comentario explicativo apunta a doc 08 §2.10.
- [search_pipeline.py:294-298](../../durin/memory/search_pipeline.py#L294-L298): el cross-encoder rerank ya no usa `meta.get("body")`. Doc inline en la función explica que si CE quality requiere body en el futuro, la solución es un top-N disk fetch dentro del CE step, NO una columna en LanceDB.
- [search_pipeline.py:445-449](../../durin/memory/search_pipeline.py#L445-L449): `_resolve_meta` ya no threadea `body` desde vector hits.
- [sectioned_output.py:60](../../durin/memory/sectioned_output.py#L60): `SectionedHit.body` keep como field con default `""` (backward-compat; cold-tier callers caen a `_enrich_body`).
- [index_meta.py:47](../../durin/memory/index_meta.py#L47): `CURRENT_SCHEMA_VERSION` bumped 2 → 3 para forzar clean rebuild de tablas v2 existentes (que tendrán la columna `body` huérfana). El check `ensure_index_fresh` (P2.2) lo dispara automáticamente en el próximo `memory_search.execute`.
- [tests/memory/test_vector_index_no_body_column.py](../../tests/memory/test_vector_index_no_body_column.py): 2 tests nuevos que assertan el invariante post-A4 — si alguien re-introduce la columna, el test falla con mensaje específico apuntando a doc 08 §2.10.

Cambios al doc:

- [docs/memory/02_indexing.md §3.1](02_indexing.md): 8 columnas reales en la tabla del schema. Bloque dedicado explicando *"no body column — body lives on disk"* con justificación arquitectónica + referencia a doc 08 §2.10. Aclaración de la asimetría `id/class_name` (LanceDB) vs `uri/type` (FTS5).
- [docs/memory/02_indexing.md §3.2](02_indexing.md): dim corregida (default 384, no 768); listadas alternativas (e5-large 1024-dim, MiniLM-L6 384-dim).
- [docs/memory/02_indexing.md §3.3](02_indexing.md): `entity_page` en vez de `entity`; nota sobre session_summary que NO se emite hoy (delegada a A10).
- [docs/memory/02_indexing.md §5.1](02_indexing.md): nota sobre la asimetría con LanceDB + cómo FTS5 también honra el principio (indexa el `text` pero nunca lo devuelve).
- [docs/memory/02_indexing.md §11 status](02_indexing.md): fila vector index actualizada con el schema actual.
- [docs/memory/08_scope_and_discarded.md §2.10](08_scope_and_discarded.md): entry permanente con genealogía + 5 razones del revert + lección sobre optimización vs principio + lección sobre symmetry entre índices.

**Lecciones nuevas** (a guardar en memoria persistente):

- *"Una optimización que viola un principio arquitectónico debe justificarse con medición, no con intuición"* — P2.5 ahorraba ~10ms en una operación dominada por LLM latency de segundos.
- *"El fix para un consumer lento es local a ese consumer, no un schema change"* — si CE necesita más texto, optimizar CE; no agregar columnas a LanceDB que el 95% de las queries no usan.
- *"Symmetry entre componentes es feature"* — FTS5 y LanceDB siendo ambos "metadata + index, content en disk" hace el sistema más simple de razonar.

**Verificado pre-commit**: tests/memory/ 903 passed, 1 skipped (894 base + 7 A3 + 2 A4 invariante).

**Estado**: resolved (commit pendiente).

---

### A5 — `memory.dream.end` no emite los campos de costo que doc 08 §3 R3 necesita

**Doc dice** (`docs/memory/07_telemetry_and_observability.md:194-206`):

```
Already exists, augment with:
| entities_quarantined | int | NEW |
| llm_call_count | int |
| llm_input_tokens_total | int |
| llm_output_tokens_total | int |
| duration_ms | float |
```

**Código dice** (`durin/memory/dream_runner.py:337-354`):

```python
emit_tool_event(
    "memory.dream.end",
    {
        "trigger": trigger,
        "entity_filter": entity_filter or "",
        "entities_consolidated": consolidated,
        "entities_failed": failed,
        "duration_s": duration_s,    # ← seconds, not ms
    },
)
```

No emite `entities_quarantined` / `llm_call_count` / `llm_input_tokens_total` / `llm_output_tokens_total`. `duration_s` en segundos, no `duration_ms`.

Doc 08 §3 R3 (risk register) propone alarmar en `dream_llm_cost_per_day_usd > $5/día`. **Sin los token totals, esta alarma es inviable hoy.**

**Quién tiene razón**: doc tiene razón en intención (el costo dream es importante medir), pero la implementación requiere instrumentar los llm_invoke calls dentro de DreamConsolidator para capturar prompt/completion tokens. Eso es real work (no doc fix).

**Acción**: implementar acumulador de tokens en DreamRunner, pasar como kwargs a `_emit_end`. Renombrar `duration_s` → `duration_ms` (* 1000.0). Agregar `entities_quarantined` (ya existe el concepto en `_maybe_auto_absorb`).

**Resolución (2026-05-28)**: El `LLMInvoke` Protocol del dream era `Callable[..., str]` — descartaba el `response.usage` que litellm sí provee. Cambio arquitectónico local al consumer correcto (el dream namespace) — aplicando la lección de A4 [[feedback-optimization-vs-principle]]: el fix vive donde el consumer está, no como global state.

Cambios:

- **[durin/memory/dream.py](../../durin/memory/dream.py)**: nuevo `LLMResponse` dataclass (`text + prompt_tokens + completion_tokens`); `LLMInvoke` Protocol actualizado a devolver `LLMResponse`; `default_llm_invoke` extrae `response.usage` de litellm; `ConsolidationResult` gana `prompt_tokens`/`completion_tokens`/`llm_call_count`; `consolidate_entity` acumula tokens incluso a través de retries; `DreamError` gana `triggered_quarantine` flag.
- **[durin/memory/dream_quarantine.py](../../durin/memory/dream_quarantine.py)**: `record_failure` ahora devuelve `bool` — `True` cuando esa llamada disparó la 3ª strike → quarantine.
- **[durin/memory/dream.py::DreamConsolidator.apply](../../durin/memory/dream.py)**: capta el flag de `record_failure` y lo propaga en `raise DreamError(..., triggered_quarantine=triggered)`.
- **[durin/memory/dream_runner.py](../../durin/memory/dream_runner.py)**: nuevo `_ConsolidateTotals` dataclass (accumulador per-pass); `_consolidate()` devuelve los totals; `_emit_end()` payload con los 4 nuevos campos + `duration_ms`.
- **[durin/memory/absorb_judge.py](../../durin/memory/absorb_judge.py)**: extrae `.text` del response. NO acumula tokens en `dream.end` (el judge corre POST-dream y tiene su propia telemetría `memory.absorb.judged`).
- **[durin/telemetry/schema.py](../../durin/telemetry/schema.py)**: `MemoryDreamEndEvent` TypedDict actualizado — 4 nuevos campos, `duration_s` eliminado.
- **[tests/memory/test_dream_end_cost_telemetry.py](../../tests/memory/test_dream_end_cost_telemetry.py)** (nuevo, 4 tests): ejercita el comportamiento real:
  - `LLMResponse` → tokens en el payload de `dream.end`.
  - Legacy `str`-returning `llm_invoke` → tokens=0 (under-report safe-failure).
  - Multi-entity → tokens sumados correctamente.
  - Schema TypedDict tiene los campos requeridos + sacó `duration_s`.

**Backward-compat shim**: el call site en `dream.py:341` y `absorb_judge.py:144` aceptan TANTO `LLMResponse` como `str` (`isinstance` check). Esto permite que los ~15 tests existentes con mocks `lambda p,**kw: "raw"` sigan pasando sin churn mecánico — under-reportan tokens (0) pero el dream flow funciona.

**Doc 07 §6.2 actualizado**: tabla completa con los 9 campos, nota explícita de que el campo viejo `duration_s` se eliminó (no es additive), nota sobre safe-failure direction cuando el provider no surface `usage`.

**Doc 08 §3 R3 alarma**: ahora es computable. La fórmula es `dream_llm_cost_per_day_usd = sum(llm_input_tokens_total * input_rate + llm_output_tokens_total * output_rate)` sobre eventos `memory.dream.end` del día.

**Lecciones aplicadas**:
- [[feedback-optimization-vs-principle]]: el cambio es **local al consumer correcto** (dream namespace). `query_rewriter.LLMInvoke` queda intacto.
- [[feedback-sync-tests-exercise-behavior]]: el behavior test no compara sólo strings de doc, **emite eventos reales y verifica los valores**.
- [[feedback-verify-quantifiers]]: durante el desarrollo el test `test_dream_end_aggregates_tokens_across_multiple_entities` falló con assumption "slug in prompt matches unique entity" — falsa porque los prompts incluyen aliases cross-entity. Corregido con counter-based stub.

**Verificado pre-commit**: tests/memory/ 907 passed (903 baseline + 4 nuevos A5), 1 skipped (condition).

**Estado**: resolved (commit pendiente).

---

### A6 — `memory.health_check` payload mismatch

**Doc dice** (`docs/memory/07_telemetry_and_observability.md:314-327`):

```
| tick_id | UUID |
| triggered_by | scheduled | eager_post_failure |
| components | dict | {name: {"status": ok|degraded|critical, "details": str|null}}
| restorations_attempted | list[str] |
| restorations_succeeded | list[str] |
| duration_ms | float |
```

**Código dice** (`durin/memory/health_check.py:114-120`):

```python
payload: dict[str, Any] = {
    "status": status,
    "components": components,       # dict[str, str] plano, no nested
    "drift_count": drift_count,
}
if errors:
    payload["errors"] = errors
```

No tiene `tick_id`, `triggered_by`, `restorations_*`, `duration_ms`. `components` es `dict[str, str]` (status flat), no `dict[str, {"status", "details"}]`.

**Quién tiene razón**: pieza por pieza:
- `tick_id`: bueno para correlacionar logs cuando hay múltiples ticks por hora. **Razonable agregar**.
- `triggered_by`: hoy sólo hay scheduled (no hay eager-post-failure). Si nunca habrá eager, este campo es spec-only. **Defer hasta que eager exista o quitar del doc.**
- `components` nested vs flat: la versión nested permite incluir detalles (e.g. "lance probe: connection refused"). El código emite los detalles en un campo aparte `errors`. **Funcionalmente equivalente, pero shape distinto.** Es decisión de schema.
- `restorations_*`: el código tiene `_repair_drift` pero no emite agregados. Razonable agregar.
- `duration_ms`: trivial agregar (medir t0 al entrar `run_tick`).

**Acción opción A** (menor cambio): actualizar doc 07 §9.4 para describir el payload real. Agregar `duration_ms` (trivial). Dejar lo demás como "futuro".

**Acción opción B** (mayor cambio): agregar al código `tick_id` + `restorations_attempted/succeeded` + `duration_ms` y promover `components` a nested.

**Recomendación**: opción A. La estructura plana del código es más simple y los datos de detalles ya van por `errors`. El doc se ajusta a la realidad; cuando haya necesidad real de tick_id/eager se vuelve a evaluar.

**Resolución (2026-05-28) — Híbrida pragmática**: el análisis verificado mostró que **no hay consumers del evento en código hoy** (cero hits fuera del propio módulo emisor + tests), entonces "quién tiene razón" no es binario — es decisión de diseño anticipado. Resultado:

- **Agregado al código**: `tick_id` (uuid hex, 32 chars) + `duration_ms` (vía `time.perf_counter()`). Son estándar operacional: tick_id para correlación de logs entre ticks, duration_ms para diferenciar ticks rápidos vs lentos.
- **NO agregado**: `triggered_by` (sólo existe `scheduled` hoy; sería enum con un valor único), `components` nested (funcionalmente equivalente al flat + errors aparte; nested es churn sin beneficio), `restorations_attempted`/`succeeded` (`drift_count` + `errors` ya cubren la señal hoy; agregar cuando exista alarma operacional que lo necesite).

Cambios:
- [durin/memory/health_check.py](../../durin/memory/health_check.py): `import uuid` + `time` agregados. `run_tick()` genera `tick_id = uuid.uuid4().hex` y `t0 = time.perf_counter()` al entrar; el payload incluye ambos. ~5 LOC delta.
- [durin/telemetry/schema.py](../../durin/telemetry/schema.py): `MemoryHealthCheckEvent` TypedDict gana `tick_id` y `duration_ms`. Adicionales — pre-A6 fields siguen requeridos.
- [docs/memory/07_telemetry_and_observability.md §9.4](07_telemetry_and_observability.md): tabla reescrita con los 6 fields actuales + bloque "Shape decisions and what's deliberately NOT emitted" documentando por qué `triggered_by`/`nested components`/`restorations_*` quedaron fuera. **Ese bloque es lo que evita que esta decisión se vuelva a tomar al revés** (un futuro reader podría ver doc 07 §9.4 v1 y "implementar lo que el doc dice" sin saber el contexto).
- [tests/memory/test_health_check_a6_fields.py](../../tests/memory/test_health_check_a6_fields.py): 5 tests nuevos ejercitando behavior:
  - `tick_id` es exactamente 32-char hex (no 36-char dashed — catches `.hex` vs `str()` regression).
  - `duration_ms` es > 0 (catches segundos-en-vez-de-ms regression — el delta de `perf_counter()` en segundos es <1, multiplicado por 1000 es >0).
  - Ticks consecutivos producen tick_ids distintos (catches per-init vs per-tick generation regression).
  - TypedDict tiene los A6 fields **y** los pre-A6 fields (additive, no replace).
  - Pre-A6 fields siguen en el payload.

**Lecciones aplicadas**:
- [[feedback-verify-quantifiers]]: el test explícitamente verifica `len(tick_id) == 32` y que todos los caracteres sean hex. No asume "uuid es uuid".
- [[feedback-sync-tests-exercise-behavior]]: behavior tests, no sólo schema declarations.
- [[feedback-no-wait-and-measure]] invertido: NO agregar campos sin necesidad demostrada (`triggered_by`, `restorations_*`). Documentar la decisión para no volver a tomarla al revés.

**Verificado pre-commit**: tests/memory/ 912 passed (907 baseline + 5 nuevos A6), 1 skipped.

**Estado**: resolved (commit pendiente).

---

### A7 — `memory.health.critical` falta `manual_recovery_hint`

**Doc dice** (`docs/memory/07_telemetry_and_observability.md:338`):

```
| manual_recovery_hint | string | Suggested CLI: e.g., `durin reindex --target lancedb` |
```

**Código dice** (`durin/memory/health_check.py:227-238`):

```python
emit_tool_event(
    "memory.health.critical",
    {
        "component": component,
        "consecutive_failures": count,
        "last_error": error[:200],
    },
)
```

**Quién tiene razón**: doc tiene razón en valor (si vas a alertar, dar el comando de recovery ayuda). Implementación es trivial — mapping component → comando sugerido.

**Acción**: agregar dict de recovery hints en `health_check.py`:

```python
_RECOVERY_HINTS = {
    "fts5": "durin memory reindex --target fts",
    "lance": "durin memory reindex --target lance",
}
```

Y agregar al payload.

**Resolución (2026-05-28) — Opción A con anti-drift test**: el campo se agrega + test que protege contra drift entre los hints y el CLI real. Aplicando `feedback_verify_quantifiers`, el comando sugerido por el doc original (`durin reindex --target lancedb`) era **incorrecto** — el comando real es `durin memory reindex` (le faltaba el `memory`). Y el `--target` accepta `lancedb` (no `lance` que es el nombre del probe). Ambos errores en spec corregidos en la implementación.

Cambios:

- [durin/memory/health_check.py](../../durin/memory/health_check.py):
  * Nuevo `_RECOVERY_HINTS` dict — mapping probe-name → CLI command verbatim.
  * Nuevo `_RECOVERY_HINT_FALLBACK = "durin memory reindex --target all"` para componentes nuevos sin hint específico.
  * `_emit_critical()` payload incluye `manual_recovery_hint` (lookup con fallback).
- [durin/cli/memory_cmd.py](../../durin/cli/memory_cmd.py): la constante `("all", "fts", "lancedb")` extraída a `VALID_REINDEX_TARGETS` exportable. Permite que el test anti-drift compare contra una single-source-of-truth en vez de hardcodear strings.
- [durin/telemetry/schema.py](../../durin/telemetry/schema.py): `MemoryHealthCriticalEvent` gana `manual_recovery_hint: str`. Additive.
- [tests/memory/test_health_critical_a7_recovery_hint.py](../../tests/memory/test_health_critical_a7_recovery_hint.py) (nuevo, 6 tests):
  * Todos los probes conocidos (`fts`, `lance`) tienen hint.
  * Todos los hints empiezan con `durin memory reindex` (no `durin reindex` — protege contra re-introducir el spec-typo).
  * **Anti-drift core**: cada `--target X` en cada hint pasa la validación del CLI (importa `VALID_REINDEX_TARGETS`). Si alguien renombra un target sin actualizar `_RECOVERY_HINTS`, el test falla.
  * Emit path para componente conocido usa el hint específico.
  * Emit path para componente desconocido usa el fallback.
  * TypedDict declara el field + preserva pre-A7 fields.

- [docs/memory/07_telemetry_and_observability.md §9.5](07_telemetry_and_observability.md): reescrito con los 4 campos. Sección explica la traducción probe-name → CLI target (legacy drift `lance` vs `lancedb`) y referencia el anti-drift test. Corregido el comando equivocado de la spec v1.

**Lecciones aplicadas**:
- [[feedback-verify-quantifiers]]: verificar que el comando sugerido **realmente exista**. Doc 07 v1 decía `durin reindex` — comando inexistente (falta `memory`). El audit lo descubrió antes de implementar.
- [[feedback-sync-tests-exercise-behavior]]: el test no compara strings entre doc y código — verifica que el target sugerido **pase la validación del CLI**, ejercitando el contrato real.
- [[feedback-optimization-vs-principle]]: el fix es local al consumer (health_check + memory_cmd extract VALID_REINDEX_TARGETS). El consumer humano que lee logs es legítimo aunque no haya consumer software hoy.

**Verificado pre-commit**: tests/memory/ 918 passed (912 baseline + 6 nuevos A7), 1 skipped.

**Estado**: resolved (commit pendiente).

---

### A8 — `PushSink` es código muerto sin wiring

**Doc dice** (`docs/memory/07_telemetry_and_observability.md` §12.2 + `09_implementation_roadmap.md` P7.3): HTTPS push opt-in via `telemetry.push_url` + `telemetry.push_token`.

**Código dice**:
- `durin/telemetry/push.py:32` existe `PushSink` con tests pasando.
- `grep -rn "PushSink" durin/` (fuera del propio push.py): cero hits.
- `grep -rn "push_url|push_token" durin/config/`: cero hits.
- Ningún sink lo invoca; el config no tiene los campos; el agente nunca lo crea.

**Quién tiene razón**: ambos. El doc describe la feature correctamente. El código tiene la mitad (la clase). Falta el wiring: campos en `durin/config/schema.py::TelemetryConfig`, construcción en el sink registry, llamada `push.log(...)` desde el emit pipeline.

**Acción**:
1. Agregar a `durin/config/schema.py` (probablemente bajo `TelemetryConfig` o crear `TelemetryPushConfig`):
   - `push_url: str | None`
   - `push_token: str | None` (mejor leer del secret store)
   - `push_batch_size: int = 10`
2. En el sink registry (`durin/telemetry/sinks.py` o equivalente): si `push_url` configurado, instanciar `PushSink` y añadir al fan-out.
3. Test E2E: configurar URL fake (httpbin), verificar que un emit dispara HTTP request.

**Resolución (2026-05-28) — Opción A cableado end-to-end**: el primer análisis del audit propuso borrar PushSink ("no consumer"). El user corrigió: *"medir comportamiento es el propósito de la telemetría — si no hay consumo es porque todavía no lo publicamos a un dashboard/API, no porque no se necesite. Medir lo es todo."* Lección nueva guardada en memoria persistente: [[feedback-telemetry-is-first-class]] — pattern opuesto al de A4 (P2.5 revert).

Cambios:

- [durin/config/schema.py](../../durin/config/schema.py): `TelemetryPushConfig` + `TelemetryConfig` nuevos. `Config` gana `telemetry: TelemetryConfig`. El schema declara `token_secret_name` (referencia), NO el token; un test invariante (`test_config_schema_has_no_plaintext_token_field`) protege contra regresión.
- [durin/telemetry/logger.py](../../durin/telemetry/logger.py): `TelemetryLogger` gana `_extra_sinks` + `add_sink()`. `log()` escribe primero al JSONL (canonical source of truth) y luego itera los sinks adicionales — cada uno aislado en try/except para que un sink que falle no afecte el resto ni el JSONL.
- [durin/telemetry/wiring.py](../../durin/telemetry/wiring.py) (nuevo): `wire_push_sink()` que (a) verifica config válida, (b) resuelve el token via `get_secret_store().get(name)`, (c) construye `PushSink` + attach al logger, (d) loggea warnings claros si la config está incompleta o el secret falta. Todos los modos de falla terminan en "push disabled, JSONL keeps working".
- [durin/telemetry/__init__.py](../../durin/telemetry/__init__.py): `PushSink` exportado en `__all__` (ahora es API pública del paquete).
- [durin/agent/loop.py](../../durin/agent/loop.py): integrated — al crear el session_logger se intenta wire_push_sink; en el `finally` del cleanup se llama `push_sink.flush()` para no perder eventos del buffer parcial.
- [tests/telemetry/test_push_wiring.py](../../tests/telemetry/test_push_wiring.py) (nuevo, 9 tests):
  * Disabled-path: default → no sink. None config → no sink (no raise).
  * Misconfigured: url o secret_name vacío → graceful disable.
  * Secret missing: store no tiene el name → graceful disable + warning.
  * Happy path: el sink se attacha, el token RESUELTO viene del secret store (assert privacy invariant).
  * Fan-out: 3 events emitidos → 3 lines en JSONL + 3 pending en el push buffer.
  * Isolation: sink broken (raises) → JSONL sigue escribiendo correctamente.
  * Schema invariant: `TelemetryPushConfig` NO tiene field `token` plaintext — sólo `token_secret_name`. Catches a regression que pondría el token en config.json.

- [docs/memory/07_telemetry_and_observability.md §12.2](07_telemetry_and_observability.md): retention corregida (90 días, no 1 año). §12.3 nueva — descripción completa del push opt-in: config TOML, comando para el secret, privacy implications, behaviour (failure isolation, drain on shutdown, retry path).

**Lecciones aplicadas**:
- [[feedback-telemetry-is-first-class]] (nueva): medir comportamiento es el propósito, no requiere downstream consumer para justificar.
- [[feedback-verify-quantifiers]]: tests verifican el shape del Config schema (no asume; lee `model_fields`).
- [[feedback-sync-tests-exercise-behavior]]: el test no compara strings entre doc y código; ejercita los happy/unhappy paths del wiring real.
- Privacy by design: token via secret store (lección de cómo `ZHIPU_API_KEY` se maneja en A5), default OFF, warning explícito en doc 07 §12.3.

**Verificado pre-commit**: tests/memory/ + tests/telemetry/ 962 passed (953 baseline + 9 nuevos A8), 1 skipped.

**Estado**: resolved (commit pendiente).

---

### A9 — Temporal decay no aplicado al ranking

**Doc dice** (`docs/memory/00_overview.md:232`, fila 3b):
> **In MVP, enabled by default**, but only for observation-type docs. episodic (90d half-life) and session_summary (120d) decay.

**Doc dice** también (`docs/memory/03_search_pipeline.md` §10) — paso "STEP 6 — Temporal decay" entre cross-encoder y sectioning, "default enabled".

**Código dice** (`durin/memory/decay.py:14-18`, header literal):

```python
"""...
Phase 0 scope: the half-life table + the `half_life_for` resolver. The
ranking-time consumer (apply exponential decay to score) lands in a
later phase.
"""
```

`grep -n "decay|half_life" search_pipeline.py rrf_fusion.py entity_ranker.py` → **cero hits**. Nada consume el resolver.

**Quién tiene razón**: el código se autodocumenta correctamente (header explica que está pendiente). **Doc 00 §10 row 3b miente.** Doc 03 §10 promete "enabled by default" — falso.

**Acción opción A** (cumplir el doc): implementar consumer ranking-time. ~50 LOC: en `run_search_pipeline`, después de RRF y antes de entity rerank, multiplicar `score *= exp(-Δdays/half_life)` para hits con `half_life ≠ None`.

**Acción opción B** (alinear doc): marcar decay como deferred en doc 00 y doc 03, mover a `08_scope_and_discarded.md` como "deferred to post-MVP".

**Recomendación**: opción A es ~1h de trabajo y cierra una promesa explícita del doc. Hagamos A.

**Resolución (2026-05-28) — Opción A, class defaults only**: durante el análisis el user empujó con la pregunta clave: *"no asumas los defaults del doc, enumera todas las clases que se guardan y razoná por cada una"*. La enumeración (verificada contra `MEMORY_CLASSES` + el código real) llegó a la misma tabla que el doc original — pero ahora con el razonamiento explícito por clase grabado:

| Clase | Decae | Half-life | Razonamiento verificado |
|---|---|---|---|
| `entity_page` (alias `entity`) | No | null | `valid_from = ""` siempre para entity pages; el mtime es "última pasada Dream", no "edad del hecho" |
| `episodic` | Sí | 90d | Observaciones con timestamp intrínseco — la edad ES información del contenido |
| `stable` | No | null | El user/agente lo marcó explícitamente como durable; decaerlo contradice la decisión |
| `corpus` | No | null | `valid_from` es la fecha de INGEST, no del contenido — decaer castigaría "libros viejos en tu pipeline" |
| `session_summary` | Sí | 120d | Igual concepto que episodic pero cubre temas más amplios — pero inert hasta A10 (no se emite hoy) |
| `pending` | N/A | — | Walker lo excluye (A2) |

**Override per-entry NO se aplica en search pipeline**: el user confirmó que por clase alcanza. Verificación adicional mostró que **es spec sin uso real**: Dream nunca setea `evergreen` ni `decay_half_life`; el workspace actual no tiene entries con esos overrides; los templates de Dream no instruyen al LLM a emitirlos. El field queda en `MemoryEntry` schema preparado para futuro; el resolver `half_life_for` sigue exportándose para callers que lo necesiten (hot_layer, dream).

Cambios:

- [durin/memory/decay.py](../../durin/memory/decay.py): nueva función pura `apply_class_decay(score, class_name, valid_from_iso, now=None) -> (decayed, factor)`. `CLASS_HALF_LIFE_DEFAULTS` gana `entity_page` como alias de `entity` (FTS5 / LanceDB usan nombres distintos; ambos resuelven a null). Module header reescrito con la tabla razonada inline.
- [durin/config/schema.py](../../durin/config/schema.py): nuevo `MemoryTemporalDecayConfig(enabled: bool = True)`. `MemorySearchConfig` ahora tiene `temporal_decay`.
- [durin/memory/search_pipeline.py](../../durin/memory/search_pipeline.py): nuevo `_temporal_decay_step()` insertado después del cross-encoder y antes del sectioning. Reordena `fused` por decayed scores. `run_search_pipeline` gana `temporal_decay_enabled: bool = True`. `now` inyectable para tests deterministas.
- [durin/agent/tools/memory_search.py](../../durin/agent/tools/memory_search.py): lee `app_config.memory.search.temporal_decay.enabled` y lo threada al pipeline.
- [durin/telemetry/schema.py](../../durin/telemetry/schema.py): nuevo `MemoryRecallDecayEvent` TypedDict + registro en `EVENTS`.
- [tests/memory/test_decay_search_integration.py](../../tests/memory/test_decay_search_integration.py) (nuevo, 19 tests):
  * Unit: `apply_class_decay` por cada clase (decae / no decae) + edge cases (empty/malformed/future timestamp, unknown class).
  * Quantifier: `exp(-1) ≈ 0.368` para 1 half-life, `exp(-5) ≈ 0.0067` para 5 half-lives.
  * `entity` y `entity_page` ambos resuelven a no-decay (catches the FTS5 vs LanceDB naming).
  * Pipeline: hits viejos bajan al fondo, recientes suben; entity_page con valid_from antiguo NO mueve.
  * Telemetry: `memory.recall.decay` event con counts correctos.
  * Schema: TypedDict registrado, config default enabled=True.

- [docs/memory/03_search_pipeline.md §10.7](03_search_pipeline.md) (nuevo): describe qué shippeó A9 + la tabla razonada + scope (class only).
- [docs/memory/00_overview.md §10 row 3b](00_overview.md): de "promise" a "shipped".

**Lecciones aplicadas**:
- [[feedback-verify-quantifiers]] aplicado dos veces durante el desarrollo:
  1. Test inicial usó `_FIXED_NOW = datetime(... 12:00)` pero `valid_from="2026-05-28"` parsea a 00:00 — delta de 0.5 días, factor ≈ 0.9945 (no 1.0). Fix: `_FIXED_NOW = datetime(... 00:00)` para que los deltas sean exactos.
  2. Test del pipeline pasó `now=None` al `_temporal_decay_step` → wall-clock real diferente al `_FIXED_NOW` que esperaba el cálculo. Refactor para inyectar `now` desde tests.
- [[feedback-question-user-input]]: el primer plan copió los defaults del doc sin razonar. El user empujó "enumera y razoná por clase" — y la enumeración produjo el mismo resultado, pero con razonamiento verbatim guardado. La diferencia: futuros readers ven *por qué* corpus no decae, no sólo *que* no decae.
- [[feedback-sync-tests-exercise-behavior]]: tests no comparan strings del doc; ejercitan la función con valores numéricos verificados matemáticamente.

**Verificado pre-commit**: tests/memory/ 937 passed (918 baseline + 19 nuevos A9), 1 skipped.

**Estado**: resolved (commit pendiente).

---

### A10 — Doc 02 promete indexar session summaries; nada las indexa

**Doc dice** (`docs/memory/02_indexing.md:104`):

> *"`sessions/<id>/<id>.meta.json::derived._last_summary` (one row per session as `type=session_summary`)"*

Y §6.5 (yield rule): *"Also yields `sessions/<id>/<id>.meta.json` if a `_last_summary` is present"*.

**Código dice**:
- `durin/memory/paths.py:78-111` `walk_memory` itera **sólo** `*.md` bajo `memory/`. Nunca toca `sessions/`.
- `grep -rn "session_summary\|_last_summary" durin/memory/indexer.py durin/memory/vector_index.py` → cero hits relevantes (sólo aparece en metadata tables o como categoría de retorno, no como input).
- `CLASS_HALF_LIFE_DEFAULTS` lista `session_summary: 120` pero nada emite filas con ese tipo a Lance/FTS.

**Quién tiene razón**: doc 02 promete una capacidad que sería útil pero no existe. Si el dream consolidator escribiera summaries en `memory/sessions/<id>.md` (formato markdown), el walker las recogería; hoy viven en `sessions/<id>/<id>.meta.json` (JSON-derived) y nadie las propaga al índice.

**Acción opción A** (implementar): tras cerrar una sesión, escribir el last_summary como `memory/episodic/session-<id>.md` con class `session_summary`. Entonces el walker las ve.

**Acción opción B** (sacar del doc): borrar §6.5 yield y la fila `session_summary` de §3.3. Marcar como deferred.

**Recomendación**: opción A — las session summaries son retrieval-valiosas (resumen condensado de una conversación entera). ~30 LOC en el handler de session close. Pero requiere decidir dónde viven (`memory/<class>/` requiere una clase nueva o reusar `episodic`).

**Resolución (2026-05-28) — Opción A con single source of truth**: el user empujó con la pregunta clave: *"el last summary ahora va vivir en el archivo metadata de la session ademas de su entidad propia?"* — exactamente el pattern A4 (P2.5) que ya habíamos identificado como anti-pattern. La duplicación entre `<key>.meta.json::_last_summary` y `memory/session_summary/<key>.md` era replication, no fan-out. Solución: el `.md` es la **única** fuente de verdad; el JSON metadata deja de cargar `_last_summary` going forward.

Cambios:

- [durin/memory/session_summary_store.py](../../durin/memory/session_summary_store.py) (nuevo, ~155 LOC):
  * `SESSION_SUMMARY_CLASS = "session_summary"` constante.
  * `sanitize_session_key(key)` — mismo patrón que `TelemetryLogger`'s sanitiser: collapses non-word chars + dot runs (path-traversal safe), cap a 80 chars.
  * `session_summary_path(workspace, key)` — resuelve a `memory/session_summary/<sanitized>.md`.
  * `write_session_summary(workspace, key, text, last_active=None)` — escribe la entry via Pydantic-valid `MemoryEntry`. Empty/sentinel input → no write.
  * `get_session_summary(workspace, key) -> (text, last_active)` — read path. Never raises.
  * `delete_session_summary(workspace, key) -> bool` — borrado explícito.
- [durin/memory/paths.py](../../durin/memory/paths.py): `MEMORY_CLASSES` ahora incluye `"session_summary"` (5 valores). Walker recoge automáticamente.
- [durin/agent/memory.py::Consolidator._persist_last_summary](../../durin/agent/memory.py): refactorizado — escribe al `.md` via `write_session_summary` y **pop el legacy `_last_summary` del `session.metadata`** + save (migración one-shot por compaction). Aislado en try/except para que el flow del consolidator nunca rompa por un write fail.
- [durin/agent/memory.py::estimate_session_prompt_tokens](../../durin/agent/memory.py): lee del `.md` via `get_session_summary`; fallback al legacy `metadata["_last_summary"]` para sesiones pre-A10 que aún no compactaron.
- [durin/agent/loop.py::_format_pending_summary](../../durin/agent/loop.py): cambió de `@staticmethod` a método de instancia para acceder a `self.workspace`. Lee del `.md` primero; fallback al legacy metadata.
- [tests/memory/test_paths.py](../../tests/memory/test_paths.py): test del set canonical actualizado a 5 valores.
- 3 tests legacy actualizados para usar `get_session_summary` en vez de leer `session.metadata["_last_summary"]`: [test_consolidator.py:167](../../tests/agent/test_consolidator.py), [test_loop_consolidation_tokens.py:191](../../tests/agent/test_loop_consolidation_tokens.py), [test_d1_commands.py:187](../../tests/command/test_d1_commands.py).
- [tests/memory/test_session_summary_indexing.py](../../tests/memory/test_session_summary_indexing.py) (nuevo, 14 tests):
  * `session_summary` en `MEMORY_CLASSES` pero NO en `_AGENT_FACING_CLASSES` (agent never writes summaries directly).
  * `sanitize_session_key`: simple/colon/path-traversal/empty handled.
  * `write_session_summary` round-trip → texto idéntico.
  * Empty/sentinel input → no write.
  * Update overrides same path (id = sanitized key).
  * `delete_session_summary` removes md; second delete is no-op.
  * Persisted entry is Pydantic-valid (round-trips via `load_entry`).
  * Indexer's `_payload_for` asigna `class_name="session_summary"`.
  * A9 decay para `session_summary` resuelve a 120 días.

- [docs/memory/02_indexing.md §3.3](02_indexing.md): nueva §3.3.1 "Session summaries (audit A10)" explica el flow + la decisión de single source of truth + agent_facing_classes exclusion.

**Sesiones pre-A10**: tienen `_last_summary` en `metadata` JSON. La migración es **lazy**: en la próxima compaction de cada session, `_persist_last_summary` escribe el `.md` nuevo Y pop el legacy field del metadata. Si una session NUNCA se vuelve a compactar (e.g. user abandona), el legacy summary queda en su JSON — el `_format_pending_summary` lo lee como fallback. No data loss; sólo "no indexing" para esas sesiones huérfanas (aceptable; user nunca las va a usar).

**Lecciones aplicadas**:
- [[feedback-optimization-vs-principle]] (A4): el user identificó la replication antes de que yo la implementara. Cumple exactamente el pattern de A4.
- [[feedback-question-user-input]]: el user empujó "esto vive en dos lugares?" y la respuesta correcta era refactorizar el plan, no defender la duplicación.
- [[feedback-sync-tests-exercise-behavior]]: tests ejercitan round-trips reales, no schemas mockeados.

**Verificado pre-commit**: tests/memory/ + tests/agent/ + tests/command/ + tests/session/ + tests/telemetry/ 2302 passed (todos los tests pasan después de actualizar los 3 legacy + agregar 14 nuevos), 1 skipped.

**Estado**: resolved (commit pendiente).

---

### A11 — `MemoryFileWatcher` y `HealthChecker` shippeados pero no cableados al lifecycle

**Doc dice** (`docs/memory/10_remaining_work.md` P2.3 + P2.4 DoD):
- P2.3: *"Modificar `memory/entities/person/marcelo.md` con vim y, dentro de 5 segundos, el siguiente `memory_search` para 'marcelo' surface las palabras del edit."*
- P2.4: *"Cada 15 minutos (configurable), un job background... probe FTS + Lance."*

**Código dice**:
- `durin/memory/file_watcher.py::MemoryFileWatcher` existe + tests pasan.
- `durin/memory/health_check.py::HealthChecker` existe + tests pasan.
- `grep -rn "MemoryFileWatcher\|HealthChecker" durin/agent durin/cli durin/channels` → **cero hits**.

Ningún call site los arranca. `AgentLoop.start`, `durin agent` CLI, los channel adapters — ninguno los menciona.

**Quién tiene razón**: doc tiene razón sobre la **intención**; los DoDs propuestos requieren wiring que no existe.

**Acción**:
1. `durin/agent/loop.py::AgentLoop.start` — si `cfg.memory.enabled` y `cfg.memory.file_watcher.enabled` (nuevo flag), arrancar `MemoryFileWatcher` como background thread; detenerlo en `stop`.
2. Decisión: ¿el cron de health_check vive in-process (un thread daemon) o como cron externo? Doc 10 P2.4 sugiere in-process. Implementar `HealthCheckScheduler` que dispara `run_tick()` cada `cfg.memory.health_check.interval_seconds` (nuevo).
3. Agregar config keys.
4. Verify live: editar un .md con vim → memory_search ve el cambio.

**Riesgo**: file watchers en macOS/Linux/Docker tienen edge cases. `watchdog` ya está como dep.

**Resolución (2026-05-28) — Default ON ambos servicios + isolation**: aplicando `feedback_telemetry_is_first_class` (A8): el health check ES observability infrastructure — la razón por la que "no hay alertas" hoy es que no lo cableamos. **Default ON**. El file watcher es UX directo (vim edit → próximo search lo ve) — **también default ON**. Ambos opt-out vía config.

Cambios:

- [durin/config/schema.py](../../durin/config/schema.py): nuevos `MemoryFileWatcherConfig(enabled=True)` y `MemoryHealthCheckConfig(enabled=True, interval_seconds=900)`. `MemoryConfig` ahora tiene `file_watcher` y `health_check`.
- [durin/memory/health_check.py](../../durin/memory/health_check.py): nuevo `HealthCheckScheduler` — daemon thread que llama `run_tick()` cada N segundos. `wait(timeout)` en lugar de `sleep(N)` para que `stop()` sea responsivo (no espera el interval completo). Failure isolation: `run_tick()` exception logueada pero el thread sigue (siguiente tick fires).
- [durin/agent/loop.py::AgentLoop](../../durin/agent/loop.py):
  * Nuevos atributos `self._memory_file_watcher` y `self._memory_health_scheduler` inicializados al final de `__init__`.
  * Nuevo método `_start_memory_background_services()` — construye + start cada servicio si su flag de config está enabled. Cada uno aislado en try/except: si uno falla al arrancar, el otro sigue y `AgentLoop` arranca igual.
  * Nuevo método `_stop_memory_background_services()` — None-safe; llama `stop()` en cada uno y aísla los failures.
  * `AgentLoop.stop()` ahora invoca `_stop_memory_background_services()` antes de loguear.
- [tests/memory/test_a11_lifecycle_wiring.py](../../tests/memory/test_a11_lifecycle_wiring.py) (nuevo, 12 tests):
  * Config defaults: ambos enabled, interval=900.
  * `HealthCheckScheduler` ticks on start (primer tick inmediato).
  * `HealthCheckScheduler.stop()` responsivo aunque interval=3600.
  * `HealthCheckScheduler` aisla failures de `run_tick`: el thread sigue después de exception.
  * `app_config=None` → no servicios (mantiene tests existentes simples).
  * Default config → ambos arrancan.
  * Watcher disabled → sólo health corre.
  * Health disabled → sólo watcher corre.
  * `stop()` drena ambos cleanly.
  * Watcher startup failure aislada — health sigue funcionando.

- [docs/memory/02_indexing.md §6.3](02_indexing.md): nuevo bloque "Lifecycle (audit A11)" explica que el watcher arranca por default, failure isolation, y cómo deshabilitarlo.
- [docs/memory/07_telemetry_and_observability.md §9.4](07_telemetry_and_observability.md): nuevo bloque "Scheduling (audit A11)" explica `interval_seconds=900` default + "first tick immediate" + responsive shutdown.

**Test impact**: 2302 tests previos siguen pasando (los 2300+ que construyen `AgentLoop` lo hacen con `app_config=None`, lo cual skip la wiring por design). Sin breaking change para suite existente.

**Decisión arquitectónica clave**: `_start_memory_background_services` es defensivo end-to-end. Cada servicio puede fallar independientemente:
- Watchdog no instalado → watcher fail import → log warning → `_memory_file_watcher` queda None → resto del loop sigue.
- Health check thread no se puede crear → log warning → `_memory_health_scheduler` queda None → file watcher sigue.

Aplicando `feedback_telemetry_is_first_class`: telemetría (health_check emit) y observability infrastructure (file watcher reindex) NO requieren consumer downstream para justificar — son la fuente de los datos que después usaremos.

**Verificado pre-commit**: tests/memory/ + tests/agent/ + tests/command/ + tests/session/ + tests/telemetry/ 2314 passed (2302 baseline + 12 nuevos A11), 1 skipped.

**Estado**: resolved (commit pendiente).

---

## MEDIUM — drift sin romper UX directo

### B1 — `.description` property de los tools no está sincronizada ✅ RESOLVED con la canónica

**Doc dice** (`docs/memory/04_agent_tools.md:413-419` §8):

> *"The description in the tool registration MUST match the doc 06 §3.1-§3.4 text verbatim. Sync via `tests/memory/test_tool_description_sync.py`."*

**Código dice**:
- `_PARAMETERS["description"]` en cada tool **está** sincronizada (test pasa).
- Pero cada tool además tiene una `description` property distinta. Ejemplos:
  - `memory_search.py:165-178`: *"Search the agent's memory. Pass a short topical phrase..."* (texto comprimido, distinto al canónico).
  - `memory_store.py:121-127`: *"Persist a memory entry. Idempotent on (class, content)..."* (no menciona dedup vs Dream).
  - `memory_ingest.py:94-103`: *"Persist a markdown or plain-text file..."* (no menciona URLs, inline, etc.).
  - `memory_drill.py`: similar drift.

**Quién tiene razón**: doc tiene razón. **Dos descripciones distintas para el mismo tool es exactamente lo que el doc dice evitar.**

Hay que clarificar cuál se le presenta al LLM en runtime. Verificar `Tool` base class para saber si usa `_PARAMETERS["description"]` o `self.description`.

**Acción**:
1. Investigar cuál de las dos llega al LLM. Probablemente `_PARAMETERS["description"]` (lo que valida el sync test), pero si la property se usa en algún registro/CLI, debe alinearse.
2. Si la property es "human-readable short" y `_PARAMETERS` es "LLM canonical", documentar la distinción explícitamente y agregar test de invariante (e.g. property contiene un summary del canónico).
3. Si la property no se usa en ningún lado relevante, **borrarla** — código muerto que confunde.

**Resolución (2026-05-28) — Bug discovery + fix**: la investigación reveló que **el sync test de P6.3 estaba validando el campo equivocado**. `Tool.to_schema()` ([base.py:258](../../durin/agent/tools/base.py#L258)) emite `self.description` (la property corta) como `function.description` en el OpenAI function-calling spec — eso es lo que el LLM realmente lee para decidir invocar el tool. El `_PARAMETERS["description"]` que P6.3 sincronizó con doc 06 termina como `function.parameters.description` (descripción del schema del parameters object), que la mayoría de los LLMs ignora.

**Bug**: durante semanas el sync test pasó verde validando un campo que el LLM ignoraba mientras el campo que el LLM SÍ leía contenía texto corto no sincronizado con doc 06. Mismo patrón que A4 (mis propios commits previos validados parcialmente).

Cambios:

- [durin/agent/tools/memory_search.py:181](../../durin/agent/tools/memory_search.py#L181), [memory_store.py:140](../../durin/agent/tools/memory_store.py#L140), [memory_ingest.py:99](../../durin/agent/tools/memory_ingest.py#L99), [memory_drill.py:47](../../durin/agent/tools/memory_drill.py#L47): cada `.description` property ahora delega a `_PARAMETERS["description"]` (single source of truth — ambos fields resuelven al mismo string). El texto corto no canónico se eliminó. Comentario inline explica el flujo y referencia B1.
- [tests/memory/test_tool_description_sync.py](../../tests/memory/test_tool_description_sync.py): el test ahora instancia cada tool y lee `.description` property (en lugar de `_PARAMETERS["description"]`). Adicionalmente, nuevo test `test_description_property_is_what_to_schema_emits` verifica el invariante `to_schema()["function"]["description"] == tool.description` — anti-drift contra el caso "alguien cambia `to_schema()` para usar otro campo y el sync queda mirando el campo equivocado de nuevo".
- [docs/memory/06_prompts_and_instructions.md §3.5](06_prompts_and_instructions.md): reescrito — explica el contract `.description` → `function.description`, por qué el `_PARAMETERS["description"]` (que termina como `function.parameters.description`) se mantiene idéntico por defense-in-depth, y documenta el bug B1 que esta sección refleja.

**Lecciones aplicadas**:
- [[feedback-sync-tests-exercise-behavior]]: el sync test ahora ejercita el **contract real** (`to_schema()` output) no sólo string equality. El nuevo invariant test `test_description_property_is_what_to_schema_emits` es defensa específica contra "alguien refactorea `to_schema()` y el sync queda mirando el lugar equivocado".
- [[feedback-verify-quantifiers]]: durante la investigación verifiqué qué consumer real lee `self.description` (grep mostró `to_schema()` único). Sin ese check yo habría asumido que el sync test ya cubría lo correcto.
- [[feedback-optimization-vs-principle]] aplicado al pattern A4: P6.3 fue commit mío que sincronizó parcialmente — el fix es local al consumer correcto (la property que el LLM lee), no cambiar el contract global.

**Verificado pre-commit**: 5/5 tests del sync (4 contenido + 1 invariante); suite memoria 964 passing.

**Estado**: resolved (commit pendiente).

---

### B2 — Doc 99_phase_progress_review obsoleto ✅ RESOLVED

**Doc dice** (`docs/memory/99_phase_progress_review.md:5`): "4885 tests pasando".

**Doc dice** (§2 D4): "Phase 1.9 deferido (integración v2 pipeline en DreamConsolidator)... Próximo siguiente paso: Phase 1.9".

**Código dice**:
- `git log --oneline`: commit `6aafc3f` shipped Phase 1.9 (DreamConsolidator usa parse_dream_output + apply_dream_output).
- Test count actual (último commit `2e7097a` body): 4968 passing.

**Quién tiene razón**: código (commits dicen la verdad). Doc desactualizado.

**Acción**: actualizar `99_phase_progress_review.md` — marcar D4 resuelto, actualizar test count, mover §4 recomendaciones a estado "DONE".

**Estado**: pending

---

### B3 — Doc 10 marca como pending lo que está hecho ✅ RESOLVED

**Doc dice** (`docs/memory/10_remaining_work.md` líneas 24, P2.x, P3.x, P4.x, P5.x, P6.x, P7.x): muchos items sin ✅ DONE.

**Código dice** (git log):
- P2.2 ✅ commit `c3eff1e`
- P2.3 ✅ commit `d9a4d8e` (módulo existe; ver A11 sobre wiring)
- P2.4 ✅ commit `022d4b1` (módulo existe; ver A11)
- P2.5 ✅ commit `a266344`
- P3.3 ✅ commit `bc55686`
- P4.1-P4.3 ✅ commit `b3c50c6`
- P4.4 ✅ este turno
- P5.2-P5.6 ✅ commits `2e7097a`, `572d5cf`
- P6.1-P6.3 ✅ commit `572d5cf`
- P7.2-P7.3 ✅ commit `2e7097a`

Línea 24 dice "queda Phase 4 + Phase 8" — Phase 4 cerrado.

**Quién tiene razón**: código. Doc desactualizado.

**Acción**: pasar por doc 10 y marcar cada item con ✅ DONE + commit hash. Reescribir línea 24.

**Estado**: pending

---

### B4 — P5.5 implementado distinto al spec ✅ RESOLVED

**Doc dice** (`docs/memory/10_remaining_work.md` P5.5):
> *"Script `scripts/audit_tool_descriptions.py` extrae las descripciones... falla con diff específico si difieren. Wired en CI."*

**Código dice**:
- `ls scripts/audit_tool_descriptions.py` → no existe.
- `tests/memory/test_tool_description_sync.py` existe, 4 tests pasan, valida `_PARAMETERS["description"]` contra doc 06 §3.1-§3.4.
- No hay CI step nuevo en `.github/workflows/`.

**Quién tiene razón**: ambos válidos en intención. Test pytest cumple la misma función que el script + CI (pytest YA corre en CI), y es más estándar (no introduce un comando custom).

**Acción**: actualizar doc 10 P5.5 para reflejar que la implementación es pytest, no standalone script. Estado: ✅ DONE con desviación documentada.

**Estado**: pending

---

### B5 — Retention: 1 año en doc vs 90 días en código ✅ RESOLVED

**Doc dice** (`docs/memory/07_telemetry_and_observability.md` §12.2):
> *"old events compressed... kept 1 year, then deleted"*

**Código dice** (`durin/telemetry/retention.py:34-35`):

```python
COMPRESSION_AGE_DAYS: int = 30
DELETION_AGE_DAYS: int = 90
```

→ 30d para comprimir, 90d para borrar. Total 90 días, no 1 año.

**Quién tiene razón**: depende de uso real.
- Doc (1 año): conservador, útil para análisis longitudinal.
- Código (90d): minimiza disk usage. Razonable para single-user durin.

**Acción**: hacerlo configurable (`telemetry.retention.{compress_age_days, delete_age_days}` en config schema). Default actual (30/90) razonable; user puede subirlo a 365 si quiere análisis anual. Actualizar doc 07 §12.2 para describir los defaults reales + cómo extender.

**Estado**: pending

---

### B6 — Doc 03 §17 status table contradice §11 sobre MMR ✅ RESOLVED

**Doc dice** §11: "MMR — Removed from MVP".
**Doc dice** §17 status table: "MMR | Not implemented | New step, default enabled".

**Código dice**: `grep -rn "mmr\|MMR" durin/memory/` → cero hits en código de producción.

**Quién tiene razón**: §11 (removed). §17 quedó stale al actualizar §11.

**Acción**: corregir §17 — fila MMR debe decir "Removed from MVP".

**Estado**: pending

---

### B7 — Doc 05 §15 + doc 06 §10 status: "v1 page rewrites" ✅ RESOLVED

**Doc dice** (`docs/memory/05_dream_cold_path.md:201` y §15 status table): *"current code uses full-page rewrites"*.
**Doc dice** (`docs/memory/06_prompts_and_instructions.md` §10): *"templates/dream/consolidator.md: v1 (page + commit)"*.

**Código dice**:
- `durin/memory/dream.py` llama `parse_dream_output` + `apply_dream_output` (Phase 1.9 shipped en commit `6aafc3f`).
- `durin/templates/dream/` contiene `consolidator.md`, `rules.md`, `commit_format.md`, `json_patch_reference.md`, `examples/01..06_*.md`.
- `dream_prompt_builder.build_dream_prompt` arma el package.

**Quién tiene razón**: código. Doc desactualizado al no haberse pasado tras Phase 1.9.

**Acción**: borrar el callout de §15 doc 05 línea 201; actualizar status table; actualizar doc 06 §10 a "v2 (JSON Patch + body delta)".

**Estado**: pending

---

### B8 — Doc 03 §15 promete config keys que no existen ✅ RESOLVED

**Doc dice** (`docs/memory/03_search_pipeline.md` §15):

```
memory.search.vector_top_k
memory.search.lexical_top_k
memory.search.rrf_constant
memory.search.rrf_weights
memory.search.sectioning.max_per_source
memory.search.final_top_k
```

**Código dice** (`durin/config/schema.py:276-281`):

```python
class MemorySearchConfig(Base):
    cross_encoder: CrossEncoderConfig = Field(
        default_factory=CrossEncoderConfig,
    )
```

Sólo `cross_encoder`. Lo demás está hardcoded:
- `vector_top_k=50` (search_pipeline.py:347)
- `limit=10` (memory_search.py:348)
- RRF k=60 + weights (rrf_fusion.py:38-42)
- `DEFAULT_MAX_PER_SOURCE=3` (sectioned_output.py:38)

**Quién tiene razón**: depende del nivel de configurabilidad deseado. **Hoy, en single-user durin, hardcoded defaults razonables son OK** — exponer 6 knobs adicionales agrega complejidad sin necesidad clara.

**Acción opción A** (mínimo): actualizar doc 03 §15 para listar **sólo** los keys que existen (`memory.search.cross_encoder.*`) y agregar nota "los demás defaults están hardcoded; cambiar requiere PR".

**Acción opción B** (full config surface): exponer cada knob en schema.

**Recomendación**: A. La configurabilidad adicional es deferred hasta que alguien necesite ajustar (con datos). Marcar como "ergonomic deferral".

**Estado**: pending

---

### B9 — Eventos documentados que nunca se emiten ✅ RESOLVED (asymmetric)

**Doc dice**:
- `memory.silent_retrieval_miss` (doc 07 §4.6)
- `memory.search.failure` (doc 07 §8.1)

**Código dice**:
- `grep -rn "memory\.silent_retrieval_miss\|memory\.search\.failure" durin/` → cero hits.
- No están en `EVENTS` registry de `durin/telemetry/schema.py`.

**Quién tiene razón**: doc propone, código no implementa. **Cada evento es legítimo** — `silent_retrieval_miss` permitiría detectar "el usuario preguntó X, debía estar en memory, no surgió" (telemetría crítica para validar G3.b query rewriting). `memory.search.failure` permitiría alertas de degradación.

**Acción**:
- `memory.search.failure`: implementar en `search_pipeline.py` cuando un safe wrapper recupera (P5.2 ya tiene `recovered_from`); fácil. ~20 LOC.
- `memory.silent_retrieval_miss`: complejo — requiere LLM judge o user feedback. Defer; sacar del doc 07 §4.6 o marcar como "research item".

**Resolución (2026-05-28) — Asimétrica: failure implementado, silent_retrieval_miss discarded**: el user empujó con la pregunta clave sobre `silent_retrieval_miss`: *"como se puede detectar considerando multiples lenguajes de forma efectiva, no se me ocurre"*. La revisión honesta confirmó que 2 de las 3 heurísticas propuestas (negation tokens, correction patterns) son inherentemente English-shaped, y la 1 (substring overlap) genera demasiados falsos positivos. Sin un classifier LLM-based (que rompe el budget de telemetría), el evento no es viable para los workloads multi-lingual que durin sirve (LoCoMo seed usa CJK + español). Mover de "deferred" a **discarded** con la lección.

**`memory.search.failure` — IMPLEMENTADO**:

- [durin/telemetry/schema.py](../../durin/telemetry/schema.py): nuevo `MemoryRecallFailureEvent` TypedDict con shape recortado vs la spec v1 (sin `kind` enum ni `recoverable` bool — los wrappers no clasifican exceptions hoy; inventar esos fields sería data fabricada).
- [durin/memory/search_pipeline.py](../../durin/memory/search_pipeline.py): nuevo `_emit_search_failure()` que se invoca al final de `run_search_pipeline` cuando `recovery["sources"]` no está vacío. `degraded_to` derivado de los counts: `full` (solo grep falló y los otros cubrieron), `vector_only`, `lexical_only`, `grep_only`, o `none` (recovery_succeeded == False). Wrapped en try/except — un fallo de emit jamás rompe el search result.
- [tests/memory/test_search_failure_event.py](../../tests/memory/test_search_failure_event.py) (nuevo, 5 tests):
  * Clean run → no event.
  * Vector falla pero lexical produce hits → degraded_to=lexical_only.
  * Todas las sources fallan → recovery_succeeded=False, degraded_to=none.
  * TypedDict registrado en EVENTS + tiene los fields requeridos.
  * `emit_tool_event` raises → search result intacto (telemetry never breaks search).

**`memory.silent_retrieval_miss` — DISCARDED**:

- [docs/memory/07_telemetry_and_observability.md §4.6](07_telemetry_and_observability.md): reescrita — el evento ya no se emite, sección apunta a doc 08 §2.11 con la razón.
- [docs/memory/08_scope_and_discarded.md §2.11](08_scope_and_discarded.md) (nuevo): entry permanente con 4 razones del discard + 3 alternativas si en el futuro se necesita la señal + lesson general sobre "heuristic detectors with language-specific token lists are a red flag for any subsystem that has to serve multi-lingual workloads".

**Doc 07 §8.1** actualizada con shape real del payload + explicación de por qué se recortaron `kind` y `recoverable` vs spec v1.

**Lecciones aplicadas**:
- [[feedback-question-user-input]]: el user empujó "como se hace cross-lingual?" — sin ese push yo habría implementado las heurísticas como "deferred" pretending que el problema era de scheduling. La pregunta correcta no era "cuándo" sino "si tiene sentido siquiera".
- [[feedback-telemetry-is-first-class]]: aplica para `search.failure` (datos de degradation que el operator querría). NO aplica para `silent_retrieval_miss` con el approach propuesto — datos no confiables son peores que no datos (ruido > silencio).
- [[feedback-optimization-vs-principle]]: aplicado a la spec. El v1 de `silent_retrieval_miss` violaba el principio "must serve multi-lingual workloads"; defenderlo como "será deferred" hubiera repetido el pattern de A8 invertido (cablear something speculative cuyo costo de mantenimiento supera el valor).
- Nueva entry futura en memoria persistente: "heuristic detectors con language-specific token lists son red flag para multi-lingual systems".

**Verificado pre-commit**: 5/5 tests del search failure event; suite memoria 969+ passing.

**Estado**: resolved (commit pendiente).

---

### B10 — Eventos emitidos no documentados ✅ RESOLVED

**Código dice**:
- `memory.embedding.load` (`durin/memory/embedding.py:172`)
- `memory.embedding.embed` (`durin/memory/embedding.py:192`)
- `memory.hot_layer.failure` (`durin/memory/hot_layer.py:161`)

Los tres están en `EVENTS` registry y se emiten.

**Doc dice**: doc 07 §3 categoría tables no los lista.

**Quién tiene razón**: código (emite eventos legítimamente útiles). Doc incompleto.

**Acción**: agregar los 3 eventos a doc 07 con sus payload schemas.

**Estado**: pending

---

### B11 — Doc 06 §2 sólo menciona `## Memory` (incompleta) ✅ RESOLVED

**Doc dice** (`docs/memory/06_prompts_and_instructions.md` §2): reproduce sólo el bloque `## Memory` del identity.md.

**Código dice** (`durin/templates/agent/identity.md:35-46`): además del `## Memory`, existe `## Memory writing` que da guidance para escrituras (dedup, cuándo NO llamar memory_store).

**Quién tiene razón**: código (tiene contenido útil que el doc oculta).

**Acción**: actualizar doc 06 §2 para reproducir AMBAS secciones verbatim.

**Estado**: pending

---

### B12 — Cross-encoder model NO validado contra lista curada ✅ RESOLVED

**Doc dice** (`docs/memory/03_search_pipeline.md` §9.5): *"dropdown for picking the model from the curated list (jina-v2, bge-base, bge-v2-m3, qwen3-reranker-0.6b)"*.

**Código dice** (`durin/config/schema.py:266-273`):

```python
class CrossEncoderConfig(Base):
    enabled: bool = False
    model: str = "jinaai/jina-reranker-v2-base-multilingual"  # free string
    batch_size: int = 32
    top_n: int = 10
```

No hay validador, no hay enum. Un valor inválido (e.g. `model: "bogus"`) pasa el config y crashea al cargar.

**Quién tiene razón**: doc tiene razón en intención (lista curada). Pero hacer enum **estricto** rompe extensibilidad — un user que quiera probar un modelo nuevo no debería editar el schema.

**Acción opción A**: validador soft (lista de "known good", warn si no está, no falla). ~10 LOC.

**Acción opción B**: dejar como string libre, alinear doc a "models known to work" (no curated dropdown).

**Recomendación**: A. Warn-but-allow es el balance correcto. La webui ya filtra a los 4 conocidos; el config schema acepta otros pero loguea warning.

**Resolución (2026-05-28) — Opción C: validación dinámica, sin lista fija**: el user empujó con la observación clave: *"Los modelos antes de seleccionarlos y asignarlos deberian pasar un test. Los que durin ofrece en la instalacion no seran los unicos permitidos, el usuario deberia a la larga poder poner otro ya sea por ollama, o usando api de modelos que ya soportamos o customs. Pero no veo un listado fijo fuera el de la instalacion inicial."*

Eso descartó tanto la Opción A (soft validator con lista) como la Opción B (dejar libre + doc fix). La fix correcta es **probar el modelo live** antes de aceptar el valor — patrón del `check_model_ping` que ya existe para LLM models.

Cambios backend:

- [durin/memory/cross_encoder.py](../../durin/memory/cross_encoder.py): nuevo `test_model(model_id, *, loader=None) → dict` con shape `{status, message, model_id, duration_ms}`. Intenta `_load_default_scorer(model_id)` + score trivial. Maneja cuatro modos de falla: empty id, loader retorna None (no sentence_transformers o model not found), loader raises (network error, etc.), score raises (model loaded pero broken).
- [durin/channels/websocket.py](../../durin/channels/websocket.py): nuevo endpoint `GET /api/memory/cross-encoder/test?model=<id>` (`_handle_cross_encoder_test`). Async + `asyncio.to_thread` para que el load lento no bloquee el event loop del gateway.

Cambios webui:

- [webui/src/lib/api.ts](../../webui/src/lib/api.ts): nueva función `testCrossEncoderModel(token, model)` + `CrossEncoderTestResult` interface.
- [webui/src/components/settings/MemorySettings.tsx](../../webui/src/components/settings/MemorySettings.tsx): refactor del control "Reranker model". Antes: dropdown cerrado de 4 valores. Ahora: input free-form con HTML `<datalist>` para los 4 sugeridos + botón "Test" + área de status. El user puede tipear cualquier id; el botón Test invoca el endpoint nuevo; el resultado (ok verde / fail rojo con mensaje) se muestra inline.
- [webui/src/i18n/locales/en/common.json](../../webui/src/i18n/locales/en/common.json): strings actualizados — `crossEncoderModelPlaceholder` y `crossEncoderTest`; eliminé el namespace `crossEncoderModels` (labels per-model) ya que no hay lista cerrada.

Tests:

- [tests/memory/test_cross_encoder_model_validation.py](../../tests/memory/test_cross_encoder_model_validation.py) (nuevo, 8 tests): cubren los 4 modos de falla + happy path + invariante crítico:
  * `test_no_hardcoded_model_enum_in_config_schema`: asserts that `CrossEncoderConfig.model_fields["model"].annotation is str` (free-form). Si alguien re-introduce un `Literal[...]` o un `enum`, el test falla loudly — defensa contra regresión al pattern anti-user-extensibility.

Doc:

- [docs/memory/03_search_pipeline.md §9.5](03_search_pipeline.md): aclaración explícita: "The model set is open. The four entries below are bundled in the install as suggestions… but the config field accepts any sentence_transformers compatible id. Validation is dynamic via the Test button."

**Lecciones aplicadas**:
- [[feedback-question-user-input]]: sin tu push-back yo habría implementado la Opción A (soft validator con lista hardcoded), que era exactamente el anti-pattern user-restrictive del que me advertiste.
- [[feedback-sync-tests-exercise-behavior]]: el test del invariante `model_fields["model"].annotation is str` ejercita el contract del schema, no compara strings — defensa contra alguien convirtiendo el field a un Literal en el futuro.
- Pattern similar al de A8 / [[feedback-telemetry-is-first-class]]: la validación correcta no es "permitir o no según una lista" sino "ejercitar el comportamiento real" — load + score, igual que `check_model_ping` lo hace para LLM models.

**Verificado pre-commit**:
- Backend: 8/8 tests del helper + 2328 full suite (sin regresiones).
- Webui: `npx tsc --noEmit` clean, `npx vitest run` 142/142.

**Estado**: resolved (commit pendiente).

---

## LOW — cosmético / docs

### C1 — Doc 01 §4.3 referencia `STATEFUL_ATTRIBUTE_PATTERNS` que no existe ✅ RESOLVED

**Doc dice** (`docs/memory/01_data_and_entities.md` §4.3): *"The pattern set lives in code as a single source of truth (`STATEFUL_ATTRIBUTE_PATTERNS`)"*.

**Código dice**: `grep -rn "STATEFUL_ATTRIBUTE_PATTERNS" durin/` → cero hits.

**Quién tiene razón**: doc miente. La constante no existe. La lógica de "stateful attribute" probablemente está implícita en `entity_page.py::_validate`.

**Acción**: o crear la constante (extraer del código actual), o quitar la referencia del doc.

**Estado**: pending

---

### C2 — Doc 01 §4.4 "soft cap 50 / hard cap 200" entries-per-entity sin enforcement ✅ RESOLVED

**Doc dice** (`docs/memory/01_data_and_entities.md` §4.4): *"Per-entity cap — Soft cap = 50 (warn only), Hard cap = 200"*.

**Código dice**: `grep -rn "50\|200" durin/memory/dream.py durin/memory/entity_page.py | grep -iE "cap|limit"` → cero hits semánticamente relevantes.

**Quién tiene razón**: doc propone, código no enforca.

**Acción**: implementar el cap o sacar del doc. Recomendación: implementar el soft-cap (log warning cuando una entity tiene > 50 entries en su body). El hard cap es defensivo — defer hasta que ocurra.

**Estado**: pending

---

### C3 — Doc 01 §4.5 step 2 describe pinyin-with-tones, código usa unidecode directo ✅ RESOLVED

**Doc dice**: *"Transliterate non-Latin scripts to Latin (e.g., 马塞洛 → mǎsàiluò → masailuo)"*.

**Código dice** (`durin/memory/entities.py:153`): `unidecode(nfc)` directo. Para "马塞洛", `unidecode` produce `"Ma Sai Luo "` → `ma_sai_luo`.

**Quién tiene razón**: código (más simple y correcto). El intermedio pinyin-with-tones es ficción.

**Acción**: actualizar doc 01 §4.5 step 2: *"Transliterate non-Latin scripts to ASCII via unidecode (e.g., 马塞洛 → Ma Sai Luo → ma_sai_luo)"*.

**Estado**: pending

---

### C4 — Doc 05 §14 dice 5 triggers, §2 enumera 6 ✅ RESOLVED

**Doc dice** §14 row 1: "Five trigger types".
**Doc dice** §2: 6 triggers (`threshold`, `post_ingest_threshold`, `cron_daily`, `session_close`, `post_compaction`, `manual`).

**Código dice** — 6 triggers efectivamente cableados (verificado vía grep en commit `c3eff1e`).

**Quién tiene razón**: §2 + código.

**Acción**: corregir §14 a "Six trigger types".

**Estado**: pending

---

### C5 — Doc 05 §8.7 menciona verdict `unsure`; código usa `unclear` ✅ RESOLVED

**Doc dice** §8.7: *"flag uncertainty as `unsure` rather than confirm"*.
**Código dice** (`durin/memory/absorb_judge.py:73`): verdicts = `{"same", "different", "unclear"}`.

§8.4 del mismo doc 05 dice `unclear` correctamente.

**Quién tiene razón**: §8.4 + código.

**Acción**: corregir §8.7 a `unclear`.

**Estado**: pending

---

### C6 — Doc 07 §15 sub-totales obsoletos ✅ RESOLVED

**Doc dice** §15: "12 events in schema.py".
**Código dice** `durin/telemetry/schema.py:911-937` — 25 entradas memory.*.

**Doc dice** §15: "query truncation: Not enforced".
**Código dice** (`durin/agent/tools/_telemetry.py:29-33`) — sí enforzado vía `_truncate_freetext`.

**Quién tiene razón**: código (recuento actual).

**Acción**: actualizar §15 con counts y status reales.

**Estado**: pending

---

### C7 — Doc 02 §11 status table es stale completo ✅ RESOLVED

**Doc dice** §11 (status table): "FTS5 lexical index — Does not exist"; "File watcher — Manual rebuild only"; "Archive folder — Doesn't exist".

**Código dice**:
- `durin/memory/fts_index.py` existe + indexer usa.
- `MemoryFileWatcher` existe (aunque no cableado, ver A11).
- `archive/` walker existe (`durin/memory/archive.py`).

**Quién tiene razón**: código. Doc 02 §11 entera está obsoleta.

**Acción**: rehacer §11 desde cero reflejando estado actual.

**Estado**: pending

---

### C8 — Doc 03 §1 diagram tiene dos "Step 7" (header collision) ✅ RESOLVED

**Doc dice**: §11 "Step 7 — Removed (MMR deferred)"; §12 también titulada "STEP 7".

**Acción**: renumerar.

**Estado**: pending

---

### C9 — Doc 06 §3.5 menciona `memory_*.py::DESCRIPTION` constants que no existen ✅ RESOLVED

**Doc dice** §3.5: *"descriptions must match `memory_*.py::DESCRIPTION` constants"*.
**Código dice**: no hay `DESCRIPTION` constant en ningún tool. La canónica vive en `_PARAMETERS["description"]`.

**Quién tiene razón**: código.

**Acción**: corregir §3.5: *"matches `_PARAMETERS['description']` field"*.

**Estado**: pending

---

### C10 — Doc 04 §7.1 menciona webui surfaces — verificar ✅ RESOLVED

**Doc dice** §7.1: hay surfaces de webui "informational".

**Código dice**: webui Settings → Memory ahora existe (P4.4 este turno). Doc no lo refleja con detalle de los 3 controles añadidos.

**Acción**: actualizar §7.1 con los 3 controles del MemorySettings.tsx.

**Estado**: pending

---

## Items NO accionables (sólo registro)

### D1 — Doc 09 spec, sin claims de status
OK — referencia, no cambia.

### D2 — Doc 98 known_bugs.md
Sólo 1 entry (B1 absorption vector index), marcado Resolved 2026-05-27. Verificado vía `absorption.py:244-253`. OK.

### D3 — Doc 99 gaps_audit.md
Round 1-3 marcados resolved. Spot-checks confirman. OK.

---

## Resumen ejecutivo

| Bloque | Items | Naturaleza | Estado |
|---|---|---|---|
| Critical (A1-A11) | 11 | Afectan UX agente, operación, o medibilidad | ✅ Cerrados 2026-05-28 |
| Medium (B1-B12) | 12 | Drift sin romper UX directo | ✅ Cerrados 2026-05-28 |
| Low (C1-C10) | 10 | Cosmetic / docs | ✅ Closed 2026-05-28 |
| Not actionable (D1-D3) | 3 | OK as-is | ✅ Recorded 2026-05-28 |
| Second pass (E1-E38) | 38 | Drift discovered in re-audit | ✅ Closed 2026-05-28 |

**Total**: 36 items first pass + 38 items second pass = **74 items reconciled as of 2026-05-28**.

**Second-pass commit tally**:
- `42d0986` feat(memory): close E1-E9 second-pass audit (high impact — telemetry + embedding v2.a + EntityPage author + rebuild gap).
- `51b3579` feat(memory): close E10-E15 (doc 03 drift + cursor wiring regression + entities meta bug).
- `935e330` feat(memory): close E16-E20 (doc 04 shapes + EntityPage user-authored protection + walker contract).
- `2c8495b` docs(memory): close E21-E23 (doc 05/06 status drift).
- (TBD) docs(memory): close E24-E38 (cosmetic batch — status rows, numbering, CLI commands).

**Code regressions closed in the second pass**:
- E11: pre/post-cursor wiring lost in the v2 migration (commit c820447) — restored.
- E11 bonus: `_resolve_meta` was not propagating `entities` from vector_meta — fixed.
- E19: user_authored entity page protection was arch-unsupported — `EntityPage.author` + `_maybe_auto_absorb` check shipped.
- E5: documented dashboards (§10.3 perf, §216 capacity) were impossible to implement — `memory.index.write` extended with `duration_ms` + `trigger`.
- E9: v2.a (rendered_frontmatter in entity pages) + fix for the `rebuild_from_workspace` gap that wasn't walking entity pages.

**Lessons recorded in project memory during the second pass**:
- `feedback_telemetry_is_first_class`, `feedback_optimization_vs_principle`, `feedback_sync_tests_exercise_behavior`, `feedback_verify_quantifiers`, `feedback_heuristic_detectors_multilingual` (refreshed from the first pass).

**Resolution order (historical reference)**:
- First pass: A1 → A2 → A3 (the three tools — agent UX) → A11 (watcher+cron wiring) → A9 (decay) → A10 (session summaries) → A8 (push wiring) → A5+A6+A7 (telemetry payload) → A4 (LanceDB schema doc) → B/C/D in order.
- Second pass: E1-E9 high-impact (same flow of evidence → proposal → OK → implement → TDD → commit), then E10-E15 medium (doc 03), E16-E20 (doc 04 + EntityPage author), E21-E23 (doc 05/06 status), E24-E38 cosmetic batch.

**Maintenance**: items marked ✅ RESOLVED are the immutable decision log. Do not delete; when a fix is superseded by a later audit, append a "Superseded YYYY-MM-DD by X" note instead of rewriting the original record.

---

## SECOND PASS (E) — drift discovered in re-audit 2026-05-28

### E1 — `memory.recall` event payload no coincide con doc 07 §4.1 ✅ RESOLVED

**Doc 07 §4.1 (pre-E1)** listaba 10 campos: `query`, `keywords`, `scope`, `level`, `result_count`, `total_candidates`, `strategy`, `recovered_from`, `recovery_duration_ms`, `duration_ms`.

**Código `durin/agent/tools/memory_search.py` (pre-E1, líneas 454-462)** emitía solo 4: `query`, `scope`, `level`, `result_count`. El TypedDict `MemoryRecallEvent` solo aceptaba esos 4 + `iteration`/`session_key` auto-inyectados.

**Verificación**: grep `"memory.recall"` en todo el repo confirma una sola emisión (`memory_search.py:454`). Los 6 campos "faltantes" YA se computan localmente antes de la emisión (`strategy` en línea 441-448, `duration_ms` en 390, `pipeline_result.vector_count + lexical_count` para `total_candidates`, `keywords` es kwarg, `recovered_from` viene de `pipeline_result`).

**Decisión**: A8-style (telemetría es infra de primera clase) — expandir el payload, NO reducir el doc. Cero overhead nuevo: todos los valores ya estaban computados.

**Resolución**:
- TypedDict `MemoryRecallEvent` ampliado con `strategy`/`duration_ms`/`total_candidates` requeridos + `keywords`/`recovered_from`/`recovery_duration_ms` opcionales.
- Callsite construye dict y agrega recovery solo en runs degradados (espeja la forma de respuesta del tool).
- Tests TDD: 6 cases en `tests/memory/test_recall_event_payload_e1.py` (strategy+duration, total_candidates, keywords con/sin, recovery con/sin).
- Doc 07 §4.1 reescrita con columna `Required` para distinguir always-on vs degraded-only.

**Commit pendiente** (cierre del batch E1-E9).

### E2 — `memory.recall.lexical` field names doc vs code ✅ RESOLVED

**Doc 07 §4.3 (pre-E2)** listaba `query`, `tokenizer_used` con valores `unicode61|trigram|like_fallback`, `hit_count`, `duration_ms`.

**Código `durin/memory/lexical_search.py:124-133` + TypedDict `MemoryRecallLexicalEvent`**: emite `route` (con valores `unicode61|trigram|like_substring`), `query_chars`, `cjk_chars`, `hit_count`, `duration_ms`.

**Verificación**: el TypedDict en `schema.py:780-796` está bien estructurado y la emisión coincide; el doc nunca se actualizó cuando el campo se nombró `route` en lugar del placeholder original `tokenizer_used`. `like_substring` es el `LexicalRoute` enum value (no `like_fallback`).

**Decisión**: doc → code. El código es correcto y útil (route + query/cjk char counts dan dashboards de "cuántas queries cayeron al fallback CJK"). Reescribo §4.3.

**Resolución**: doc 07 §4.3 reescrita con los 5 campos reales + nota de por qué `query` no se duplica (ya está en `memory.recall`, join por `session_key+iteration`).

**Genealogía**: commit `792f1c6` (Phase 3 core) introdujo el evento con `route` desde la primera versión. El doc 07 §4.3 era spec aspiracional ("NEW event") nunca reconciliada. Cero consumers downstream (verificado por grep).

**Commit pendiente** (cierre del batch E1-E9).

### E3 — `memory.recall.rrf` field names doc vs code ✅ RESOLVED

**Doc 07 §4.5 pre-E3**: `sources_active` (list), `keyword_boost_applied` (bool), `dedup_count` (int), `duration_ms`.

**Código `durin/memory/rrf_fusion.py:148-158` + TypedDict `MemoryRecallRRFEvent`**: emite `vector_count`, `lexical_count`, `grep_count`, `fused_count`, `boosted`, `duration_ms`.

**Genealogía**: mismo commit `792f1c6` que E2. Doc spec aspiracional, impl divergió y doc nunca reconciliado.

**Consumers**: cero (grep en `durin/` confirma que solo el emitter y el TypedDict declaran estos campos; `memory_search.py` lee desde `SearchPipelineResult`, no del evento).

**Decisión**: doc → code (Opción A). Razones:
- Per-source counts son strictly más ricos que `sources_active` (derivable como `{s: count>0}`).
- `dedup_count` es derivable como `vector_count + lexical_count + grep_count − fused_count` (cantidad de pares (URI, source) que se mergearon en el RRF).
- `boosted` vs `keyword_boost_applied` es pure rename; el primero es más conciso.
- Cero código tocado.

**Resolución**: doc 07 §4.5 reescrita con los 6 campos reales + nota de derivación matemática para `sources_active` y `dedup_count`.

**Commit pendiente** (cierre del batch E1-E9).

### E4 — `memory.recall.decay` evento emitido sin entrada en doc 07 ✅ RESOLVED

**Doc 07 §4 (pre-E4)**: tabla de eventos recall lista 4.1-4.6 sin entrada para decay.

**Código**: `durin/memory/search_pipeline.py:594-601` emite `memory.recall.decay` con `hits_total`/`hits_decayed`/`avg_decay_factor`. TypedDict `MemoryRecallDecayEvent` declarado en `schema.py:859-880`.

**Genealogía**: A9 (audit primera pasada) introdujo el evento + TypedDict pero no agregó entrada documental.

**Decisión**: pure additive. `§4.6` (silent_retrieval_miss discarded) está referenciado desde doc 08 y doc 11 — NO renumerar; append como `§4.7`.

**Resolución**: doc 07 §4.7 agregada describiendo los 3 campos + nota sobre cómo el decay interactúa con classes no-decaying (factor=1.0) + pointer a doc 03 §10.3 para config.

**Commit pendiente** (cierre del batch E1-E9).

### E5 — `memory.index.write` payload mínimo vs dashboards documentados ✅ RESOLVED

**Doc 07 §9.1 (pre-E5)**: spec aspiracional con 5 campos: `uri`, `trigger`, `targets`, `duration_ms`, `embedding_skipped`.

**Código `durin/memory/indexer.py:212-218` (pre-E5)**: emitía solo `uri`, `op`, `index` (siempre `"fts"` en práctica).

**Evidencia de consumers documentados** (clave para decidir dirección):
- Doc 07 §10.3 define alert `index_write_p95_ms < 50ms (per row)` que requiere `duration_ms`.
- Doc 09 §216 declara mitigación de crecimiento del trigram table: "monitor via `memory.index.write` events" — necesita `trigger` para distinguir bursts.

**Genealogía**: commit `be75998` (Phase 2 core) introdujo el emisor con shape mínimo; el doc se escribió como spec aspiracional y nunca reconciliado.

**Decisión**: B-minimal (code → doc parcial). Agregar `duration_ms` + `trigger` al emisor; descartar `targets`/`embedding_skipped` como aspiracionales (LanceDB no escribe este evento; no hay mtime short-circuit).

**Trigger taxonomy revisada** (vs spec original):
- Descartados: `tool_write` (no hay callsites directos desde tools), `manual_rebuild` (esa ruta emite `.rebuild`, no `.write`).
- Reales: `watcher` (default, file_watcher), `dream_apply` (post-consolidación), `drift_repair` (health check).

**Resolución**:
- `_emit_write` ampliado con `trigger` (kw) + `duration_ms` (kw).
- `reindex_one_file` acepta `trigger="watcher"` default + mide duration en upsert y delete paths.
- Callsites: dream.py:666 pasa `dream_apply`; health_check.py:231 pasa `drift_repair`; file_watcher.py usa default.
- TypedDict `MemoryIndexWriteEvent` actualizado con los 2 campos como required.
- Doc 07 §9.1 reescrito con shape real + taxonomía de triggers + nota de descarte de `targets`/`embedding_skipped`.
- Tests TDD: 5 cases en `tests/memory/test_index_write_event_e5.py` (duration_ms, default trigger, dream_apply trigger, drift_repair trigger, delete op preserva campos).

**Commit pendiente** (cierre del batch E1-E9).

### E6 — Doc 07 §15 fila "Cost in dream.end" status stale ✅ RESOLVED

**Doc 07 §15 (pre-E6)**: fila "Cost in dream.end" decía estado actual = "Not present", v2 target = "Add `llm_input_tokens_total`, `llm_output_tokens_total`, optional `llm_cost_usd`".

**Realidad post-A5**: A5 (audit primera pasada, mismo doc) ya shippeó los 3 campos en `memory.dream.end`. La fila inmediatamente anterior ("Memory event registry") incluso reconoce "A5 added cost fields to `dream.end`".

**Decisión**: flip status row a "shipped" con A5 reference. `llm_cost_usd` se mantiene como out-of-scope con razón en §1.

**Resolución**: fila reescrita reflejando shipped + pointer a §6.2 y a E6.

**Commit pendiente** (cierre del batch E1-E9).

### E7 — Residuo de `silent_retrieval_miss` en docs post-discard ✅ RESOLVED

**Contexto**: §2.11 de doc 08 (audit B9, 2026-05-28) descartó el evento `memory.silent_retrieval_miss` y sus 3 heurísticas (substring overlap + English-shaped negation tokens + correction patterns) por no ser multi-lingual viables. El doc 07 §4.6 se reescribió pointing a §2.11. Pero quedaron 4 referencias residuales que aún citaban el evento descartado como activo.

**Residuos encontrados**:
1. `08_scope_and_discarded.md` §5 línea 349 — fila "§2.F eager pre-fetch" cita `memory.silent_retrieval_miss > 5%` como trigger.
2. `08_scope_and_discarded.md` §4.1 líneas 391-397 — sección "Trigger to revisit" describe el evento + 3 heurísticas como mecanismo activo.
3. `09_implementation_roadmap.md` §10.1 línea 352 — checklist Phase 7 lista `memory.silent_retrieval_miss` como event a implementar.
4. `99_gaps_audit.md` líneas 105 y 681 — historical decision records describen el evento como decisión activa sin nota de supersession.

**Decisión**: doc → doc consistency, respetando el discard en §2.11. Reemplazar trigger telemétrico por los alternativos que §2.11 explícitamente sugiere: explicit user feedback, bench failure cluster on LoCoMo/EverMemBench, offline LLM judge over traces (post-hoc, no per-turn).

**Resolución**:
- doc 08 §5: fila §2.F reescrita con 3 triggers language-agnostic.
- doc 08 §4.1: subsección "Trigger to revisit" reescrita; ya no describe el evento discarded como mecanismo activo.
- doc 09 §10.1: checklist Phase 7 ahora lista 13 events; `silent_retrieval_miss` removido con nota de discard; `recall.decay` añadido (A9).
- doc 99 historical records (líneas 105 + 681): append nota "Superseded 2026-05-28 (B9 + §2.11 + E7)" sin reescribir el record original.

**Commit pendiente** (cierre del batch E1-E9).

### E8 — Doc 03 §14.7 failure event schema stale vs B9 canonical ✅ RESOLVED

**Doc 03 §14.7 (pre-E8)**: JSON shape con 3 campos (`component` single-value enum, `kind` 6-enum, `degraded_to` 4-enum + null) + nota explícita "No `recovery_attempted` field".

**Doc 07 §8.1 (post-B9 canonical)** + código real (`search_pipeline.py:240-249`): 5 campos (`component` comma-joined string, `recovery_attempted` bool, `recovery_succeeded` bool, `recovery_duration_ms` float, `degraded_to` 5-enum `full|vector_only|lexical_only|grep_only|none`). No `kind` campo (B9 lo descartó).

**Divergencias específicas**:
1. `kind` listado en doc 03; descartado por B9 (wrappers catch generic Exception → emitir `kind` sería inventar data).
2. Doc 03 dice explícitamente "No `recovery_attempted` field"; código sí lo emite (`recovery_attempted: True` siempre — forward-compat marker).
3. `component` en doc 03 es single enum; código es comma-joined string (afectados pueden ser múltiples).
4. `degraded_to` en doc 03 incluye "no_rerank"/null; código usa "full"/"none".
5. Faltan en doc 03: `recovery_succeeded`, `recovery_duration_ms`.

**Genealogía**: doc 03 §14.7 es spec aspiracional pre-B9 nunca reconciliada. Doc 07 §8.1 fue el output de B9 con schema definitivo.

**Decisión**: doc 03 §14.7 → collapse a pointer al canonical en doc 07 §8.1 (DRY, evita re-drift). Mantener en doc 03 la nota histórica de campos `kind` + `recovery_attempted` descartados con la razón B9.

**Resolución**: doc 03 §14.7 reescrita como 2 párrafos: (1) "evento emitido — schema canonical en doc 07 §8.1", (2) nota de qué pidió la v1 spec y por qué B9 lo cortó.

**Commit pendiente** (cierre del batch E1-E9).

### E9 — Contradicción doc 02 sobre v1/v2 embedding text (ship v2.a, supersede v2.b) ✅ RESOLVED

**Doc 02 (pre-E9)**: §4.2 + §4.3 presentaban v2 como "target" activo; §10 filas 4+5 listaban v2 como decisión resuelta; §11 (post-C7) reportaba "v2 never shipped, entity-aware ranker cubre el caso". Triple contradicción.

**Sub-decisiones separadas tras evidencia**:
- **v2.a (rendered_frontmatter en entity pages)**: traduce `attributes` y `relations` a prosa en el embedding text. Cierra recall gap real en queries de tipo atributo ("X's email", "who is Y's spouse"). El entity-aware ranker NO cubre esto — el ranker re-ordena candidates dentro del top-50, pero la página tiene que entrar al top-50 vía centroide.
- **v2.b (entities_with_aliases en entries)**: expandiría URIs en el embedding text. El entity-aware ranker (A1) cubre exactamente este caso a query-time. v2.b es trabajo duplicado.

**Decisión (con user OK 2026-05-28)**: ship v2.a; supersede v2.b por A1.

**Gap pre-existente descubierto**: `rebuild_from_workspace` no walkeaba entity pages — solo `memory/<class>/*.md` entries. Post forced-rebuild (schema bump) los entity page rows desaparecían del índice hasta el próximo Dream/absorb. Fixed como parte de E9.

**Resolución**:
- `VectorIndex._render_frontmatter(attributes, relations)` nuevo helper: renderiza attributes con `_title_key`, skipa internal metadata (provenance, dream_processed_through, created_at, updated_at), stateful attributes renderean solo `current`, relations renderean `Type: target (since date)`.
- `_compose_entity_page_text` ampliado con `attributes`/`relations` kwargs (defaults None mantienen v1 behavior).
- `upsert_entity_page` plumbing nuevo de attributes/relations.
- Callsites: `dream.py:650-657` y `absorption.py:253-260` pasan `page.attributes` + `page.relations`.
- `rebuild_from_workspace` ahora walka `memory/entities/` además de `memory/<class>/` y construye records vía nuevo `_entity_page_record` helper.
- `CURRENT_SCHEMA_VERSION` bumped 3 → 4 (E9 — fuerza rebuild para realinear centroides).
- Doc 02 §4.2 marca v2.a shipped + nota de summary slot deferred; §4.3 marca v1 final + v2.b superseded por A1; §10 filas 4+5 actualizadas; §11 agrega fila de "Vector rebuild walks entity pages" como bug-fix.
- Stub en `tests/memory/test_auto_absorb_dispatcher.py:343-352` ampliado para aceptar las nuevas kwargs.
- Tests TDD: 7 cases en `tests/memory/test_entity_page_embedding_v2a_e9.py` (rendered_attributes/relations, ordering preserved, empty case, skip internal metadata, stateful current only, rebuild walks entity pages).

**Validación**: 995 tests pasan en tests/memory/ (1 skipped pre-existente).

**Commit pendiente** (cierre del batch E1-E9).

### E10 — Doc 03 §2.1 scope/level no son inputs de `run_search_pipeline` ✅ RESOLVED

**Doc 03 §2.1 (pre-E10)**: tabla de inputs lista `scope`/`level`/`limit` junto con `query`/`keywords`, presentando todos como inputs al "search pipeline".

**Código**: `run_search_pipeline(workspace, query, *, keywords, vector_index, limit, cross_encoder, cross_encoder_top_n, temporal_decay_enabled)` — NO acepta `scope` ni `level`. Estos se manejan en `MemorySearchTool` (memory_search.py:349 decide `vi=None` cuando `scope=undreamed`; línea 424 filtra hits post-pipeline; `level=cold` enriquece con body después).

**Decisión**: doc → reality. Agregar nota "Tool vs pipeline boundary" explicando que §2.1 lista los inputs del tool surface, y que el pipeline solo consume `query`/`keywords`/`vector_index`/`limit` directamente. Cero código tocado.

**Resolución**: doc 03 §2.1 ampliada con bloque "Tool vs pipeline boundary" describiendo cómo cada input se orquesta (scope/level alrededor del pipeline call; limit clamped a [1,50] en el tool; bodies enriched post-pipeline en cold-tier).

### E11 — Doc 03 §8.4 pre/post-cursor logic perdida en migración v2 ✅ RESOLVED

**Doc 03 §8.4**: describe el partitioning pre/post-cursor como conducta activa del entity-aware rerank.

**Código pre-E11**:
- `entity_ranker.rank_with_entities(cursors=...)` implementa la lógica correctamente (tests pasan).
- Helper `_load_cursors_from_entities_dir` (memory_search.py:31) cargaba cursors desde entity pages.
- v1 search path los cableaba con `cursors=cursors`.
- v2 search_pipeline `_entity_aware_rerank` NO los cableaba — llamaba `rank_with_entities` sin `cursors=`.
- Helper quedó huérfano post-migración.

**Genealogía**:
- Commit `b724fa8`: helper introducido y wireado en v1.
- Commit `1ea70ac` (Phase 2.5/3.5): nuevo `_entity_aware_rerank` en v2 pipeline SIN cursors desde día 1.
- Commit `c820447` (Phase 5 d1): migración v1 → v2 elimina la vieja función; helper queda huérfano en memory_search.py.

**Análisis de use cases** (con user, 2026-05-28):
- (a) Textura narrativa: usuario pide reconstrucción de eventos → drill por URI funciona.
- (b) Validación de evidence: agente cita fuente → drill al provenance URI funciona.
- (c) Evolución temporal: agente ve histórico → drill puntual funciona.

Esos 3 casos son drill-by-URI, NO búsquedas amplias. La duplicación canonical + N fragmentos pre-cursor en TODA query general es ruido sin valor.

**Decisión (con user OK, opción B)**: restaurar cursor wiring en `_entity_aware_rerank`. NO archivar agresivamente (opción C descartada — perdería recall por contenido raw).

**Bug adicional descubierto** durante TDD: `_resolve_meta` (search_pipeline.py:507) NO propagaba `entities` desde vector_meta. Resultado: entries no-entity_page llegaban a `rank_with_entities` con `entities=[]` → ningún overlap → ningún entry boosteado al entity-match list (solo el canonical page). Esto **enmascaraba** la regresión pre-cursor: sin entries en entity-match list, no había diferencia observable entre pre y post.

**Resolución**:
- Helper `_load_cursors_from_entities_dir` movido a `entity_ranker.py::load_cursors_from_entities_dir` (junto a su único consumer). Comment en memory_search.py:31 marca el move.
- `_entity_aware_rerank`: carga cursors después de `extract_query_entities` y los pasa a `rank_with_entities`.
- `_resolve_meta`: propaga `entities` desde vector_meta cuando está presente.
- Import huérfano `EntityPage` removido de memory_search.py.
- Tests TDD: 2 cases en `tests/memory/test_pipeline_cursor_wiring_e11.py` (pipeline excluye pre-cursor del boost; cursor loader devuelve dict correcto).

**Validación**: 997 tests pasan en tests/memory/ (995 base + 2 E11; 1 skipped pre-existente).

**Commit pendiente** (cierre del batch E10-E23).

### E16 — Doc 04 §2.2/§4.2/§5.2 return shapes vs código ✅ RESOLVED

**Doc 04 §2.2 (pre-E16)** memory_search return: listaba `type`, `path`, `score` (no existen), omitía `source`, `snippet`, `kind`, `class_name`, `entities`, top-level `strategy`, `ranking`. Claim "`recovered_from: null in normal operation`" era falso (omitted, no null).

**Realidad** (`Result.to_dict()` + `memory_search.py:454-480`):
- Per-result: `source`, `uri`, `headline`, `snippet`, `kind` siempre + `summary`/`body`/`class_name`/`valid_from`/`entities` condicionales + `rendered`.
- Top-level: `results`, `total`, `strategy`, `ranking` siempre + `recovered_from`/`recovery_duration_ms` solo on degraded.

**Doc 04 §4.2** memory_ingest: shape coincide pero `corpus_entry_id` no marcaba condicional.

**Doc 04 §5.2** memory_drill (E18): listaba `path`; código devuelve `{uri, content}` solamente.

**Decisión**: doc → reality. Reescribir §2.2 con tabla detallada (yes/condicional/never null), aclarar §4.2 `corpus_entry_id` opcional, eliminar `path` del §5.2.

### E17 — Doc 04 +12pp vs +3.9pp ✅ RESOLVED

**Contradicción interna**: §2.4 línea 149 dice "+3.9pp result"; línea 155 dice "+12pp on single-hop". Memoria `project_locomo_v2_prompts_result.md` registra **+3.9pp overall (60.8% → 64.7%)**; el "+12pp single-hop" no tiene fuente verificable.

**Decisión**: aplicar `feedback_verify_quantifiers` — no inventar números. Alinear ambas líneas al verificado.

### E18 — Doc 04 §5.2 memory_drill path ✅ RESOLVED

**Doc**: incluía `"path": "memory/entities/person/marcelo.md"`. **Código `memory_drill.py:71`**: `return {"uri": uri, "content": text}` — no `path`. Doc → reality.

### E19 — Doc 01 §4.6.1 wrong pointers + arch gap ✅ RESOLVED (B-full)

**Doc 01 §4.6.1 línea 480 (pre-E19)**: dos claims falsos:
1. "`dream.py::DreamConsolidator.apply()` filters out user_authored entries" → el único filter está en `cli/memory_cmd.py:150` (`_discover_pending_consolidations`).
2. "`dream_runner.py::_maybe_auto_absorb` skips entity pages where author: user_authored" → ningún check existía Y `EntityPage` no tenía campo `author`.

**Gap arquitectónico descubierto**: el doc prometía protección para entity pages, pero `EntityPage` no soportaba `author`. Auto-absorb fusionaría páginas hechas a mano por el usuario.

**Decisión (con user OK, B-full)**: cerrar el gap completo, no solo el doc:
- `EntityPage` gana campo `author: str = "user_authored"` (default safe).
- Round-trip de frontmatter (read lenient con fallback, emit solo cuando difiere del default).
- `dream.py:511` placeholder y `absorption.py:360` merge product setean `author="agent_created"`.
- `dream_runner.py::_maybe_auto_absorb` chequea ambas páginas y skipea con `reason="user_authored"`.
- Tests TDD: 3 cases en `tests/memory/test_auto_absorb_user_authored_e19.py` (canonical user-authored, absorbed user-authored, both agent-created proceeds).
- Stub helper `tests/memory/test_auto_absorb_dispatcher.py:_write_page` actualizado para pasar `author="agent_created"` por default (dispatcher tests simulan páginas Dream).
- Doc 01 §4.6.1 reescrita con pointers correctos + nota de E19.

**Validación**: 1000 tests pasan en tests/memory/ (997 + 3 nuevos E19; 1 skipped pre-existente).

### E20 — Doc 02 §6.5 walker contract bullet obsoleto post-A10 ✅ RESOLVED

**Doc 02 §6.5 línea 352 (pre-E20)**: "Also yields `sessions/<id>/<id>.meta.json` if a `_last_summary` is present".

**Realidad** (`walk_memory` en `paths.py:80-113`): solo emite `.md` files bajo `memory/`. A10 (audit primera pasada) movió el session summary de JSON sidecar a `memory/session_summary/<sanitized>.md`; el walker lo trata como cualquier otra class. No hay peek a `sessions/.../meta.json` en ningún lado.

**Decisión**: doc → reality. Eliminar el bullet stale y agregar nota explicando el cambio post-A10.

**Commit pendiente** (cierre del batch E16-E23).

### E21 — Doc 05 §15 status table 4 filas "Not implemented" shipped ✅ RESOLVED

**Doc 05 §15 (pre-E21)** marcaba como "Not implemented" / "Not explicit":
- Provenance tracking
- Archive of consumed episodic
- Git commits (Hybrid model)
- Failure quarantine

**Realidad** (Phase 1.9, commit `6aafc3f`): los 4 están shipped.
- `dream_patch_parser.py` + `dream_apply.py` colectan provenance por op.
- `dream_archive_consumed.py::archive_consumed_episodic` move a `memory/archive/episodic/`.
- `dream_commit_message.py` + `dream_git_history.py` implementan el hybrid model.
- `dream_quarantine.py` + frontmatter fields `dream_failure_count` / `dream_quarantine` + 3-strike logic.

**Decisión**: flip a "Shipped (Phase 1.9)" con pointer a módulo concreto en cada fila + audit E21 reference.

### E22 — Doc 05 §14 row 8 verdict vocab obsoleto ✅ RESOLVED

**Doc 05 §14 row 8 (pre-E22)**: "LLM-judged: merge / keep_separate / unsure".

**Código `absorb_judge.py:6,84`**: vocabulary real es `same | different | unclear`. Auto-merge solo cuando `verdict == "same"` AND `confidence ≥ threshold`.

**Genealogía**: posible holdover de spec original. Nunca matchó el enum real.

**Decisión**: doc → reality.

### E23 — Doc 06 §10 status rows identity + onboarding ✅ RESOLVED

**Doc 06 §10 (pre-E23)**:
- "identity.md Memory section | v2 shipped 2026-05-25 (+3.9pp) | Light revision per §2 | Minor wording polish" — la "light revision pending" no tenía scope concreto; el bench gain fue sobre lo que está en el template hoy.
- "Onboarding wizard text | Partial | Add §6 questions" — accurate, `onboard.py` (1169 LOC) no tiene grep hit para "memory".

**Plus**: doc 06 §2.2 también tenía "+12pp on single_hop" (mismo claim stale removido de doc 04 §2.4 en E17). Extendido el fix.

**Decisión**: identity row a fully shipped (drop "light revision pending"); onboarding row mantiene "Partial" pero con evidencia concreta (grep miss); +12pp removido también de doc 06 §2.2.

---

## THIRD PASS (F) — drift discovered in re-audit 2026-05-28

After closing E1-E38 and verifying the full test suite (5088/0 fail), the user asked for a third pass to validate that doc + code stayed consistent. Sub-agents found ~17 new items. Most are drift the second pass didn't reach.

### F1 — Doc 00 §189 `class_half_life_overrides` promise ✅ RESOLVED

**Doc 00 §189 (pre-F1)**: "Configurable via `memory.search.temporal_decay.class_half_life_overrides`."

**Code `durin/config/schema.py:276-291` (pre-F1)**: `MemoryTemporalDecayConfig` only had `enabled: bool`. The promised field **did not exist**. `resolve_class_half_life(class_name)` only consulted `CLASS_HALF_LIFE_DEFAULTS` without overrides.

**"Good system" analysis**:
- Real operators may need to tune: active workspace (90d → 30d), long-running multi-year workspace (90d → 365d), per-class enable/disable.
- The global toggle already exists but is too coarse — it does not allow "decay active but conservative".
- A9 wired all the infra; the override is a small toggle on top.

**Decision**: code → doc (ship the field). Rationale:
1. Reasonable, useful promise — not aspirational.
2. Low cost (~30 LOC + TDD), zero regression risk (default `{}` = no-op).
3. Closes drift by honouring the promise instead of retracting it.

**Resolution**:
- `MemoryTemporalDecayConfig.class_half_life_overrides: dict[str, int | None] = {}` added.
- `resolve_class_half_life(name, *, overrides=None)` extended with semantics: present-int → use, present-None → disable, absent → fall through to default.
- `apply_class_decay` accepts and forwards `overrides`.
- `_temporal_decay_step` accepts and propagates.
- `run_search_pipeline` accepts `temporal_decay_overrides`.
- `memory_search.execute` reads `cfg.memory.search.temporal_decay.class_half_life_overrides` and threads it through.
- TDD tests: 9 cases in `tests/memory/test_class_half_life_overrides_f1.py` (default + override-int + override-null + add decay to no-op class + unknown class + apply_class_decay threads + null disables + config field exists + end-to-end via memory_search).
- Doc 00 §189 updated: marks "audit F1 (2026-05-28)" + clarifies semantics (map class → days, `null` to disable).

**Commit pending** (F1-F11 batch close).

### F3 — Doc 03 §4.2 embedding dim 768 vs 384 ✅ RESOLVED

**Doc 03 §4.2 (pre-F3)** (line 214): `vector = MiniLM.embed(query)  # 768-dim`.

**Reality**: MiniLM-L12-v2 emits 384-dim (doc 02 §3.2 says so correctly; embedding.py confirms).

**Decision**: doc → reality, cosmetic fix.

**Resolution**: line 214 updated to `# 384-dim (audit F3, 2026-05-28)`.

**Commit pending** (F1-F11 batch close).

### F4 — Complete Phase 3 sectioned_output migration ✅ RESOLVED

**Context**: Phase 3 (commit 792f1c6, 2026-05-28) shipped `query_router`, `RRF`, `sectioned_output`, `lexical_executor` as infrastructure — but the callsite wiring in `memory_search` was left intact. Result: two parallel renderers (`Result.render_block` in search.py vs `sectioned_output._render_block`) with different formats. The Phase 3 intent (centralised rendering with section intros + active per-source cap + cross-section grouping) never landed in production.

**Why we hadn't advanced before**: the callsite migration was probably deferred to minimise risk of agent output change; the audit passes (E1-E38) checked field-level drift but did not trace the rendering codepath end-to-end. A system with two renderers for the same concept was technical debt that the second and third pass should have caught.

**Pre-F4 reality**:
- `memory_search.execute` called `r.render_block()` per Result (legacy path).
- `sectioned_output._render_block` emitted a basic marker (snippet only, no END close).
- Section intros never reached the LLM (the legacy path didn't have them).
- The per-source cap WAS applied in the pipeline (search_pipeline.py:182) but rendering was per-row, losing the cross-section grouping Phase 3 wanted.

**Decision**: full migration (user-explicit).

**Resolution**:
- `SectionedHit` extended with `summary` and `entities` (frozen dataclass, new defaults).
- `_render_block` enriched with: END marker (`=== END KIND ===`), body preference `summary > body > snippet`, entities tail (`Entities: ref, ref`) for non-canonical, `(canonical entity page)` hint when no ts.
- `_marker_for` now honours `ts=""` → format without `(ts ...)` suffix.
- `memory_search.execute` main path + archive path: convert Results into enriched SectionedHits, apply `apply_per_source_cap`, call `render_sectioned`. Response shape gains `sectioned_rendered` (string), loses per-row `rendered` (WebUI didn't consume it; the LLM reads the sectioned string).
- `Result.render_block` removed (it had 3 callsites, all in memory_search.py — all migrated).
- TDD tests: 7 new cases in `tests/memory/test_sectioned_migration_f4.py` (END markers, ts/no-ts canonical, summary preference, fallback body/snippet, entities tail, sectioned_rendered field, section intros).
- Tests migrated: `test_fragment_canonical_contract.py::TestRenderBlock` removed (3 cases) and `test_memory_search_tool_includes_rendered_blocks` → `test_memory_search_tool_emits_sectioned_rendered`.
- Doc 03 §12.1/§12.2/§12.4: marker table to the real format, body preference + entities tail documented, section intros mentioned, `max_per_source` config marked "not yet implemented" (gap for a future F).

**Full suite green**: 5107 passed, 16 skipped, 0 failed.

**Out of F4 scope (deferred)**:
- `hot_layer._render_canonical_block` (parallel renderer for eager pre-injection) — different use case (carries the full entity page structure), not in F4 scope.
- `memory.search.sectioning.max_per_source` config knob — the cap works but is hard-coded; lift to config later if an operator asks.

**Commit pending** (F1-F11 batch close).

### F5 — Doc 04 §2.2 return shape example stale ✅ RESOLVED

**Doc 04 §2.2 example (pre-F5)**: `valid_from: "2024-01-15"` for entity_page; `rendered` per-row field.

**Reality**: entity pages always write `valid_from = ""` (doc 03 §10.4 says so; vector_index.py:149,487 confirms). The `rendered` per-row field was removed in F4.

**Decision**: doc → reality, expand example to a 2-result shape to show the entity vs entry distinction; top-level fields table updated with `sectioned_rendered`.

**Resolution**: doc 04 §2.2 rewritten with a 2-result example, `rendered` row replaced by `sectioned_rendered`, `valid_from` row clarifies "Entity pages always `""`".

### F6 — Doc 05 §12 + doc 07 §6.4 kind enum aspirational ✅ RESOLVED

**Doc 05 §12.1-12.4 + doc 07 §6.4 (pre-F6)**: `kind=llm_call_failed | parse_failed | validation_failed | round_trip_failed`.

**Reality**: `DreamApplyFailureKind` enum shipped with values `validation | patch_runtime | round_trip | io`. Quarantine logic in `dream_quarantine.STRUCTURAL_FAILURE_KINDS = {VALIDATION, PATCH_RUNTIME, ROUND_TRIP}`. LLM call failures NEVER emit `memory.dream.entity_failed` (they bubble up upstream from the consolidator). The TypedDict docstring also claimed `parse_failed`/`llm_call_failed` which are never emitted.

**Decision**: doc → code. Document the 4 real values + clarify that LLM failures are ambient/upstream + fix the TypedDict docstring.

**Resolution**:
- Doc 05 §12.1: LLM call failure marked as upstream-of-apply (runner tally, not this event).
- Doc 05 §12.2: parse failure → patch_runtime (broader category covering parse + runtime errors).
- Doc 05 §12.3: `validation_failed` → `validation`.
- Doc 05 §12.4: `round_trip_failed` → `round_trip`.
- Doc 05 §12.4a: new — `io` failure category (disk write).
- Doc 05 §12.5: STRUCTURAL_FAILURE_KINDS set = `{validation, patch_runtime, round_trip}`; ambient = `io` + upstream LLM.
- Doc 05 §14 row 12 updated to the real enum.
- Doc 07 §6.4 rewritten: real fields (`entity_ref`, `trigger`, `kind`, `error_message`, `failure_count_now`, optional `quarantined_until`); structural vs ambient taxonomy explained; note discarding the aspirational kinds.
- `MemoryDreamEntityFailedEvent` TypedDict docstring corrected to reflect that only the 4 enum values are emitted.

### F7 — Dream prompt slots silently empty ✅ RESOLVED

**dream.py:731-734 (pre-F7)**: `existing_attribute_keys=()`, `existing_relation_types=()`, `existing_uris=()`, `recent_history=""` passed as empty. The original comment said "Phase 1 deliverables 9 and 10 will populate" — those never landed.

**Pre-F7 impact**: the Dream LLM ran schema-blind to the existing entity. If the page had `attributes: {e-mail: ...}` and the LLM proposed `attributes.email`, there was no hint to reuse. Schema drift was documented but invisible to the LLM.

**"Good system" analysis**:
- `existing_schema` (attributes + relations): prevents schema drift. Critical for long-term coherence.
- `recent_history`: lets the LLM see its own past decisions; avoids undoing them.
- `existing_uris`: prevents duplicate entity creation (same person registered as `person:marcelo` and `person:marcelo_marmol`).

**Decision**: wire 3 of 4 slots. Defer `existing_uris` (the producer is more complex: walk + sort by mtime + cap).

**Resolution**:
- `dream.py` top-level import of `format_recent_history`.
- `DreamConsolidator._build_prompt`: parses `EntityPage.from_text(current_page)` to extract `attributes.keys()` and `relations.type` set. Calls `format_recent_history(workspace, entity_ref)`. Failures swallowed with a warning log.
- TDD: 4 cases (attribute_keys populated vs `(none)`, relation_types populated vs `(none)`, format_recent_history called once, first-consolidation gracefully empty).
- Doc 05 §5.1 row `existing_schema` updated to "derived via EntityPage.from_text (F7)".
- Doc 05 §5.1 row `existing_uris` marked deferred.
- Doc 05 §5.1 row `recent_history` updates producer.
- Doc 06 §2 inline annotations on each affected slot.

### F8 — Doc 07 §6.5 `memory.dream.patch_applied` field names ✅ RESOLVED

**Pre-F8**: doc listed `entity_uri`, `op_count`, `body_delta_chars`, `commit_sha`, `cursor_advanced_to`. Only `body_delta_chars` matched the code.

**Reality** (`dream_apply._emit_apply_telemetry` + `MemoryDreamPatchAppliedEvent`): `entity_ref`, `trigger`, `ops_applied`, `sources_count`, `body_delta_chars`, `cursor_after`, `duration_ms`.

**Decision**: doc → code. The pre-F8 spec was aspirational with field names that never reached production. `commit_sha` is deliberately dropped (telemetry should not couple to git internals; dashboards join on `entity_ref + cursor_after`).

**Resolution**: doc 07 §6.5 rewritten with the 7 real fields + explicit note about `commit_sha` deferred-by-design.

### F10 — Doc 07 §9.2 `memory.index.rebuild` field names ✅ RESOLVED

**Pre-F10**: doc listed `entities_count`, `embedding_batches`, `duration_ms`, `prior_index_existed`.

**Reality** (`indexer._emit_rebuild` + `MemoryIndexRebuildEvent`): `target`, `indexed`, `errors`, `duration_ms`, optional `reason`.

**Decision**: doc → code.

**Resolution**: doc 07 §9.2 rewritten with the real shape + explanation per field. `target` clarifies that today it is always `"fts"` (future: `lancedb`, `all`).

### F11 — Doc 07 §9.3 `memory.index.staleness_detected` field names ✅ RESOLVED

**Pre-F11**: doc listed `uri`, `delta_seconds`, `action`.

**Reality** (`indexer._emit_staleness` + `MemoryIndexStalenessDetectedEvent`): `uri`, `reason` with values `missing_row | mtime_lag | row_for_missing_file`.

**Decision**: doc → code. `delta_seconds` and `action` were aspirational — the cron always re-derives (action single-valued = meaningless), and the time delta is implicit in the join with the corresponding `memory.index.write` event a few seconds later.

**Resolution**: doc 07 §9.3 rewritten + note discarding the 2 aspirational fields with rationale.

### F12 — `compose_embedding_text` single source of truth ✅ RESOLVED

**Doc 02 §4 (pre-F12)**: "**Single source of truth: `vector_index.py::compose_embedding_text(...)`**".

**Pre-F12 reality**: no such function existed. Two specialised composers:
- `_compose_entity_page_text(name, aliases, body, attributes?, relations?)` for EntityPage.
- `_embed_text(entry)` for MemoryEntry.

**Decision**: code → doc. Create the real public dispatcher that delegates to the correct specialist by type. The doc claim stops being aspirational.

**Resolution**:
- `VectorIndex.compose_embedding_text(item, ...)` added as a public `@classmethod`: routes EntityPage → `_compose_entity_page_text`, MemoryEntry → `_embed_text`, raises TypeError on unsupported input.
- The two specialists remain as implementation details (still accessible, not removed).
- Doc 02 §4 updated: the "Single source of truth" claim is now literally true.

### F13 — Doc 02 §11 schema_version 3 vs 4 ✅ RESOLVED

**Doc 02 §11 (pre-F13)**: `CURRENT_SCHEMA_VERSION (3 as of A4)`.

**Reality**: `index_meta.py:55` says `CURRENT_SCHEMA_VERSION = 4`. E9 bumped it when entity page composition gained `rendered_frontmatter`.

**Resolution**: doc 02 §11 row updated to `(4 as of audit E9 / F13 verification, 2026-05-28; bumped from 3 when entity-page composition gained rendered_frontmatter)`.

### F14 — Doc 03 §2.1 scope enum + grep coverage drift ✅ RESOLVED

**Doc 03 §2.1 (pre-F14)**: scope enum `dreamed|undreamed|all`; grep fallback note says "raw session/ingested" only.

**Reality**:
- F2 added `archive` to the enum.
- `_safe_grep_fallback` (search_pipeline.py:472-479) covers `memory/` + `sessions/` + `ingested/` to capture memory entries written outside the tool layer (tests, scripts).

**Resolution**: doc 03 §2.1 scope row updates the enum to `dreamed|undreamed|all|archive` + note that grep also covers `memory/` for entries-written-outside-tool.

### F15 — Doc 04 §5.3 memory_drill description divergence ✅ RESOLVED

**Doc 04 §5.3 (pre-F15)**: three paragraphs; "For related context (recent post-cursor observations mentioning this URI)..." + "This tool is read-only. It does not modify state.".

**Reality** (`memory_drill.py::_PARAMETERS["description"]`): two paragraphs; "This tool is read-only. For related context about an entity (recent observations, sessions mentioning it), use memory_search with the entity's name or URI as the query instead."

**Resolution**: doc 04 §5.3 rewritten verbatim from the shipped string + audit F15 note + clarifies canonical source.

### F16 — Doc 05 §6 step 9 `.md.bak` ordering ✅ RESOLVED

**Doc 05 §6 (pre-F16)**: step 8 = "Write to temp file + atomic rename"; step 9 = "Pre-write: copy the target to .md.bak". Step 9 happened AFTER the write — intra-doc contradiction (cannot be "pre-write" after the write).

**Reality** (`dream_apply.py:165-168`): the copy to `.md.bak` happens BEFORE any mutation. The doc's step 9 had inverted order.

**Resolution**: doc 05 §6 reordered: step 4 = copy `.md.bak` (pre-write); steps 5-9 = apply + render + validate + write; step 10 = round-trip check + restore from bak on failure; step 11 = delete bak + commit. Note references `dream_apply.py:165-168` for verifiability.

### F17 — `existing_uris` slot wired (Dream prompt) ✅ RESOLVED

**Pre-F17**: doc 06 §2 promised `{existing_uris}` recent-mtime ranked + 100-cap to prevent duplicate entity creation. `dream.py:733` passed `existing_uris=()`. F7 deferred wiring. The Dream LLM was creating duplicates (`person:marcelo_marmol` when `person:marcelo` already existed) without any workspace state signal.

**Decision**: implement the real producer.

**Resolution**:
- New module `durin/memory/entity_inventory.py` with `existing_uris_by_recent_mtime(workspace, *, cap=100)`.
- Walks `memory/entities/<type>/<slug>.md` excluding archive (top-level + legacy nested).
- Sorts by file mtime descending; default cap 100.
- `DreamConsolidator._build_prompt` replaces `existing_uris=()` with the producer call (try/except swallows failures → preserves dream resilience).
- TDD tests: 7 cases (empty workspace, collects URIs, recent-mtime sort, caps at 100, custom cap, excludes both archive variants, end-to-end via prompt builder).
- Doc 05 §5.1 + doc 06 §2 updated with producer reference.

### F18 — Doc 07 §6.1 trigger enum missing `post_ingest_threshold` ✅ RESOLVED

**Doc 07 §6.1 (pre-F18)**: trigger enum `threshold | cron_daily | post_compaction | session_close | manual`. §6.2 (dream.end) already included `post_ingest_threshold`.

**Reality**: `threshold_trigger.py:12-13` emits both in `memory.dream.start`.

**Resolution**: doc 07 §6.1 enum extended to `threshold | post_ingest_threshold | cron_daily | post_compaction | session_close | manual` + cross-ref to §6.2.

### F19 — Doc 07 alarm threshold contradiction ($1.50 vs $5/day) ✅ RESOLVED

**Pre-F19**: §10.2 "healthy range < $1.50 (alerting threshold)" vs §11 "Dream LLM cost > $5/day | error". Apparent contradiction.

**Cross-doc analysis**: doc 09 §11.1 target soak $0.25-$1.50/day; doc 09 §13 alerting $1.50; doc 08 §3 R3 alarm $5/day. The values describe a coherent two-tier alarm (warn $1.50, error $5).

**Decision**: reconcile §10.2 and §11 as explicit two-tier.

**Resolution**:
- §10.2 row updated to "target $0.25-$1.50/day; warn at $1.50 (F19), error at $5".
- §11 alerts table: new warn row `> $1.50/day`; existing error row `> $5/day` preserved.

### F20 — `iteration`/`session_key` auto-injection wired ✅ RESOLVED

**Doc 07 §4.1 (pre-F20)**: "auto-injected by `emit_tool_event`".

**Pre-F20 reality**: aspirational claim; code in `_telemetry.py` did not inject anything. Dashboards joining `memory.recall` to other events on `(session_key, iteration)` had no data to join on.

**Decision**: implement the real auto-injection.

**Resolution**:
- `TelemetryLogger.__init__(path, *, session_key="")` now accepts session_key.
- Properties `session_key`, `iteration` + `set_iteration(int)` method.
- `get_session_logger(session_key, ...)` passes session_key to the constructor.
- `emit_tool_event` reads `logger.session_key` and `logger.iteration` via `getattr` with default (test mocks without the attributes keep working), auto-injects if not already present in the payload. Caller-supplied values win (subagent override).
- New `AgentLoop._on_iteration(iteration)` callback: setattr `_current_iteration` + `current_telemetry().set_iteration(iteration)`. Replaces the previous lambda in the runner setup.
- TDD tests: 6 cases (session_key stamped, iteration starts 0, set_iteration updates, auto-inject, caller-supplied wins, no-logger no-crash).
- Doc 07 §4.1 row updated to flag F20 wired.

### F21 — Doc 03 §15 hardcoded knobs line refs stale ✅ RESOLVED

**Pre-F21**: `vector_top_k @ search_pipeline.py:347`, `lexical_top_k @ :362`, `rrf_constant @ rrf_fusion.py:38`. Real lines after refactors: `:444`, `:459`.

**Resolution**: table updated with verified line numbers OR symbols (`DEFAULT_K`, `DEFAULT_W_*`, `DEFAULT_MAX_PER_SOURCE`) that survive refactors.

### F22 — Doc 02 §4.2 `to_name_resolved` claim vs slug-only ✅ RESOLVED

**Pre-F22**: doc said relations render as `<type.title()>: <to_name_resolved>` with "name of the target entity if known".

**Reality** (`vector_index.py:231`): only strips the type prefix; does not resolve the name.

**Resolution**: row corrected; clarifies that the slug is used; alias-index resolution deferred until bench shows a recall gap on relation queries.

### F23 — Doc 02 §3.1 summary format ✅ RESOLVED

**Pre-F23**: doc said `name (also: alias1, alias2)`.

**Reality** (`vector_index.py:142, 516`): `name (alias1, alias2)` — no "also:".

**Resolution**: row corrected with the real format.

### F2 — `scope='archive'` + CLI archive commands ✅ RESOLVED (partial)

**Doc 01 §3.6 + doc 04 §11 (pre-F2)**: promised 3 recovery surfaces:
1. `memory_search(scope='archive')` walks `memory/archive/` on demand.
2. `durin archive show <uri>` reads an archived entry.
3. `durin archive list` enumerates the archive folder.

**Pre-F2 reality**:
1. `scope` enum was `["all", "dreamed", "undreamed"]`; `'archive'` rejected at `memory_search.py:315`.
2. CLI had 10 commands (`reindex`, `dream`, `history`, `show`, `diff`, `revert`, `expand`, `absorb`, `stats`, `absorb-suggest`); none archive-prefixed.
3. `durin memory expand <entity>` already covered the archive of a SINGLE entity; file access via `cat memory/archive/...` covered direct lookups.

**"Good system" analysis**:
- `scope='archive'` is the **highest-value** surface for an LLM-in-the-loop assistant: the agent can recover archived content without the operator doing manual grep. Covers the "find what you said 3 months ago about X" case.
- CLI commands are operator-debugging convenience; file access + `memory expand` already cover the minimum viable surface. Without a concrete case, they are speculative construction.

**Decision**: Option C (hybrid). Ship `scope='archive'`, defer both CLI commands.

**Resolution**:
- Enum extended to `["all", "dreamed", "undreamed", "archive"]`.
- `_run_archive_scope(query, limit)` added: walks `memory/archive/**`, parses YAML frontmatter via `split_frontmatter`, substring match over `headline+summary+name+aliases+body`. No decay, no rerank, no cross-encoder (recovery surface, not a hot path).
- Emits `memory.recall` event with `scope='archive'` + `strategy='archive'` so dashboards can tell them apart.
- TDD tests: 6 cases (`scope='archive'` accepted, finds archived episodic, finds archived entity, empty when no archive dir, does NOT include active memory, respects limit).
- Doc 01 §3.6 + §10 row 4 mark F2 shipped + clarify the CLI defer.
- Doc 04 §11 marks the CLI commands as deferred with strikethrough.
- Doc 08 §5 backlog: entry added with trigger ("concrete operator workflow") and the current workaround (`find` + `cat`).

**Commit pending** (F1-F11 batch close).

**Commit pending** (E16-E23 batch close).
