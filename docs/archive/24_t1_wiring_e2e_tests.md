# 24 — E2E tests para wiring gaps de T1.x

> Tras T1.x (clusters A-D) los unit tests pasan (4355) pero verificación
> con grep reveló **4 wiring gaps en runtime**: piezas que existen pero
> no se invocan desde el agent loop. Este doc diseña los e2e tests que
> ejercitan **el path completo** (agent → tool → componente), define
> los cambios productivos mínimos para que pasen, y se somete a self-
> review antes de glm.

---

## §1 — Findings verificados (grep + read, no asunciones)

| # | Afirmación | Evidencia | Estado |
|---|---|---|---|
| W1 | `memory_search` no invoca `entity_ranker` | `grep "rank_with_entities\|extract_query_entities" durin/` fuera de módulo: 0 hits en código productivo; único match es docstring en `memory_cmd.py:83`. `memory_search.py:122-152` solo usa `VectorIndex.search()` raw + grep fallback | Verificado ✓ |
| W2 | `AliasIndex` no se construye al boot del agent | `grep "AliasIndex" durin/agent/`: 0 hits. Las menciones en `cli/memory_cmd.py` son solo dentro de `_discover_pending_consolidations` (path del dream, no del retrieval) | Verificado ✓ |
| W3 | `cmd_dream` no pasa `vector_index` a `DreamConsolidator` | `memory_cmd.py`: `consolidator = DreamConsolidator(workspace=workspace)` — sin `vector_index=...`. Entity pages no entran a LanceDB en producción | Verificado ✓ |
| W4 | `EntityAbsorption` no expuesto en CLI | `grep "EntityAbsorption\|absorption" durin/cli/`: 2 hits, ambos en docstrings del comando `expand`. No hay `durin memory absorb` command | Verificado ✓ |

**Implicación**: gran parte del valor de T1.x sigue siendo teórico hasta cerrar
estos gaps. Cerrarlos es **trabajo de horas, no semanas** — son ~30-60 líneas
de wiring por gap.

---

## §2 — Cambios productivos requeridos

Para cada wiring gap, el cambio de código mínimo:

### W1 — `memory_search` integra entity_ranker

**Archivo**: `durin/agent/tools/memory_search.py`

**Cambio**:
1. Agregar atributo `_alias_index: AliasIndex | None` (lazy-init).
2. En `execute()`, antes de retornar, hacer:
   ```python
   from durin.memory.entity_ranker import extract_query_entities, rank_with_entities
   ai = self._get_alias_index()  # new lazy helper
   if ai is not None and ai.size() > 0:
       qe = extract_query_entities(query, ai)
       if qe:
           # convert Result objects back to dict for ranker
           # OR rank vector_rows directly before they become Results
           ranked = rank_with_entities(
               vector_rows, query_entities=qe,
               cursors=_load_cursors(ai),
               score_field="_distance",
               higher_is_better=False,
           )
           # reorder vector_rows by ranked.adjusted_score
   ```
3. `_load_cursors(alias_index)` helper: para cada entity_ref conocido,
   lee `dream_processed_through` del page on disk. Caché in-memory.
4. `_get_alias_index()` helper similar a `_get_vector_index()`:
   construye + `build()` una vez. Lo cachea.

**Sutilezas**:
- El ranker espera `_distance` field y `id`. Ya está en los rows de
  LanceDB (W3 cuando se cierra agregará `entity_page` class_name).
- Mientras W3 no esté cerrado, no hay entity_pages en el índice, así
  que el ranker reordena solo por cursor + vector. Es OK incremental.

### W2 — Boot-time alias_index para queries

**Archivos**:
- `durin/agent/tools/memory_search.py` (mismo lazy del W1 cubre esto)
- Considerar: en `agent/loop.py`, hacer warmup async como ya hace con
  embeddings (`_warmup_memory_embedding`). Análogo:
  `_warmup_alias_index()`.

**Cambio mínimo (W1 covers it)**: el lazy en `memory_search` resuelve
si el agent ejecuta `memory_search` antes de que el alias_index tenga
datos — caso normal del cold start.

**Mejora opcional** (no estrictamente necesaria): warmup en boot del
agent loop. Bajo costo (build sub-second) — vale la pena.

### W3 — `cmd_dream` pasa vector_index

**Archivo**: `durin/cli/memory_cmd.py:cmd_dream`

**Cambio**:
```python
# Build a VectorIndex if memory is enabled (same as memory_store does).
from durin.memory.vector_index import VectorIndex, vector_index_available
vi: VectorIndex | None = None
if cfg.memory.enabled and vector_index_available():
    try:
        from durin.memory.embedding import FastembedProvider
        provider = FastembedProvider(model=cfg.memory.embedding.model)
        vi = VectorIndex(workspace, provider)
    except Exception as exc:
        console.print(f"[yellow]vector index unavailable: {exc}[/yellow]")

consolidator = DreamConsolidator(workspace=workspace, vector_index=vi)
```

Pequeño. Idempotente. Si memory.enabled=False, sigue funcionando
(las pages no entran al índice, pero el dream igual escribe markdown).

### W4 — Decisión `EntityAbsorption`

Tres caminos:

- **(a)** Borrar `durin/memory/absorption.py` + tests. Limpieza.
- **(b)** Mantener código actual (status quo: importable, no CLI).
- **(c)** Exponer `durin memory absorb <canonical> <absorbed>` + opt
  `durin memory absorb --suggest` que liste candidates.

**Mi voto**: **(c)**. Razones:
- Código + tests ya están pagos (~290 LOC + 200 LOC tests).
- C.1 dedup pre-persist previene la mayoría de duplicates, pero NO
  todos (e.g. dos entries por separado que el LLM tageó con slugs
  distintos pero refieren a la misma entidad → emergen como 2 pages).
- Comando manual es bajo riesgo: user explícito invoca.
- Cubre futuro auto-trigger (T2) sin re-escribir.

Costo de (c): ~30 LOC CLI + 3 tests CLI.

---

## §3 — E2E tests específicos

Los siguientes tests ejercitan **el path completo**, no unidades. Cada
uno fallaría hoy (ese es el punto). Pasan cuando los wiring gaps se
cierren.

### E2E-1: `memory_search` aplica entity-aware ranking

**Setup** (sintético, sin LLM real):
- Workspace con vector_index habilitado (real LanceDB en tmp_path).
- 1 entity page `entities/person/marcelo.md` (built via `EntityPage.save`,
  + `vector_index.upsert_entity_page` directamente para tener el page
  en el índice independiente de W3).
- 3 entries memoria sobre `person:marcelo` con timestamps mixtos vs
  el page's `dream_processed_through`.
- 2 entries no-tagged como ruido.

**Action**:
```python
tool = MemorySearchTool(workspace=tmp_path, embedding_model=...)
out = await tool.execute(query="what does Marcelo prefer", scope="dreamed")
```

**Asserts**:
- `out["results"]` no vacío.
- Top result tiene `uri` que apunta al entity page (o id == `person:marcelo`).
- `out["strategy"]` incluye "entity" o equivalente — verificable
  via telemetry event `memory.recall.entity` que vamos a agregar.

**Si falla hoy**: top result es la entry vector-closest, ignorando
la page. Que es exactamente lo que la verificación predice.

### E2E-2: `cmd_dream` upsertea entity page al vector index

**Setup**:
- Workspace con memory.enabled (necesita stub del fastembed real o
  un fake provider per `tests/memory/test_vector_index.py`).
- 3 episodic entries con tag `person:marcelo`.

**Action**:
```python
# Run cmd_dream via CliRunner with stub LLM
with patch("durin.memory.dream.default_llm_invoke") as mock_llm:
    mock_llm.return_value = _well_formed_consolidation_response("person:marcelo")
    result = runner.invoke(memory_app, ["dream"])
```

**Asserts**:
- Entity page on disk: `entities/person/marcelo.md` exists.
- **Vector index contains the page**: `VectorIndex.search("Marcelo")`
  returns a row with `class_name == "entity_page"` and `id == "person:marcelo"`.
- `result.exit_code == 0`.

**Si falla hoy**: page on disk OK, pero `vector_index.search` no
incluye la page. La consecuencia directa de W3.

### E2E-3: Cold-start alias_index rebuild en `memory_search`

**Setup**:
- Workspace con 2 entity pages on disk (built directly).
- **No `.aliases.json` sidecar** (T1.4 no save/load).

**Action**:
```python
# Fresh tool instance — should rebuild alias_index lazily.
tool = MemorySearchTool(workspace=tmp_path, embedding_model=...)
out = await tool.execute(query="ask Marcelo")
```

**Asserts**:
- Tool internamente reconstruye alias_index al primer call.
- Si el query menciona alias, el ranker se invoca.
- Cold-start works (no persistencia required).

### E2E-4: End-to-end real LLM (gated by `DURIN_E2E_DREAM=1`)

**Setup**:
- Real glm-5.1 via z.ai endpoint.
- 5 entries tageadas about `person:marcelo` (similar a live verify de Cluster D).

**Action**:
```python
# Seed entries
# Run durin memory dream  (real LLM)
# Run memory_search query that references Marcelo
```

**Asserts**:
- Dream produces page.
- Page enters vector index.
- memory_search query about Marcelo surfaces:
  - The page in top result.
  - Post-cursor entries ranked above non-related entries.
  - Pre-cursor entries (none here since first consolidation) — N/A.

Este test es el "smoke real" — usa todas las piezas conectadas con LLM real.

### E2E-5: `durin memory absorb` (solo si W4(c))

**Setup**: 2 entity pages con aliases overlapping.

**Action**: `durin memory absorb person:marcelo person:marcelo-m`.

**Asserts**:
- Canonical page contains merged aliases.
- Absorbed file in `marcelo/archive/marcelo-m.md`.
- Alias_index updated.
- Vector index: absorbed page row removed.
- Git commit created.

### E2E-6: Smoke completo del agent loop con memory.enabled

**Setup**: workspace fresco, memory.enabled=true.

**Action**:
```bash
durin agent --new --message "Marcelo prefers pytest. Project: durin." 
durin agent --new --message "What does Marcelo prefer?"
```

**Asserts**:
- Primer mensaje: agente llama `memory_store` con entities válidas
  (verified Cluster A).
- Segundo mensaje: agente llama `memory_search`, y la entry del
  primer mensaje sale en results (no requires dream para esto).

Este test es **manual o scripted vía subprocess**, no unit-level.

---

## §4 — Self-review (cosas que adopto antes de pasar a glm)

Mirando esto antes de comprometerme:

### Adopciones

**A1**: la decisión sobre W4 (EntityAbsorption) la voy a tomar en
este doc, no defer. **Voto: (c) exponer `durin memory absorb`**. Costo
bajo, retornos claros.

**A2**: el test E2E-1 asume que `memory_search` reordena ya con el
ranker. Pero el ranker espera `_distance` field y `id`. `memory_search`
hoy convierte vector_rows a `Result` objects. **Decisión**: hacer el
ranking ANTES de la conversión a Result. El ranker opera sobre los
rows raw de LanceDB.

**A3**: cursors dict del ranker espera `{entity_ref: cursor_value}`.
Para popularlo, hay que leer cada entity page del disco y sacar
`dream_processed_through`. Esto se puede hacer una vez por tool
invocación (cacheado). NO requiere otro sidecar.

**A4**: E2E-1 setup hace `vector_index.upsert_entity_page` directamente
para independizarlo de W3. Esto es bueno — los tests siguen siendo
ortogonales.

**A5**: agregar telemetry event `memory.recall.entity_aware` con
`{query_entities, applied_ranking, top_k_signal_summary}`. Esto sirve
para futuras decisiones T2 (Phase 0.2 dijimos que diferíamos) y para
debug.

### Cosas que noto al diseñar

**Issue 1 — E2E-1 con vector_index real requiere fastembed**. Es la
única forma de tener un `_distance` real. Alternativa: usar el
`_CharProvider` o `_DeterministicProvider` que ya creamos en
`test_phase3_retrieval_e2e.py` o `test_vector_index.py`. **Decisión**:
reutilizar el fake provider, no fastembed real, para test rápido.

**Issue 2 — `memory_search` `_get_vector_index` y `_get_alias_index`
no comparten state**. Si el agent loop tiene 2 tools (`memory_store`
y `memory_search`), cada uno construye sus propios objetos. **Decisión
ahora**: aceptarlo. Es trabajo extra (2 builds al boot), pero el
overhead es <100ms total. Optimization futura.

**Issue 3 — E2E-6 requiere `durin agent` subprocess + LLM real**.
Lento (cada test = 1 turno). Pero es el único que verifica el path
completo agent → tool. Si lo hacemos, marcar como `@pytest.mark.live`
similar a `DURIN_E2E_DREAM`.

**Issue 4 — Alias-index rebuild costo**: si el corpus es chico
(<100 entities) es sub-second. Si crece a 1000+, el lazy en
`memory_search` pagaría ese costo en el primer query del turno. **No
es problema hoy. Anotar como futuro (T2.N3 system prompt cache nos
salva si llegamos ahí).**

### Cosas que omití en draft y agrego

- **A6**: el `memory_search` retorno actual tiene `strategy` field
  ("grep"/"vector"/"hybrid"). Voy a agregar **"entity_aware"** o
  componer como "hybrid+entity". El nombre lo veré con glm.

---

## §5 — Plan ejecutivo (orden y costo)

Tras este self-review, el orden propuesto:

| # | Acción | Costo |
|---|---|---|
| 1 | Codear W3 (cmd_dream pasa vector_index) | 15 min |
| 2 | Codear W1+W2 (memory_search integra ranker + alias) | 2-3 h |
| 3 | Codear W4 (durin memory absorb command) | 30 min |
| 4 | E2E-1, E2E-2, E2E-3, E2E-5 (ranker, vector, cold start, absorb) | 2-3 h |
| 5 | E2E-4 (real LLM) | 30 min + costo LLM |
| 6 | E2E-6 (subprocess agent — opcional, manual) | 30 min |
| 7 | glm peer review de doc + diff + tests verde | 30 min wait |
| **Total** | | **~6-8 h** |

---

## §6 — Wiring comparison: durin vs Hermes vs OpenClaude

Antes de seguir, Explore agent dirigida específicamente a estos 4 gaps
en los repos clonados. Output crudo: **Hermes y OpenClaude no tienen
subsistemas entity-centric**. Hermes memory delega a providers
externos (Honcho/Mem0/Supermemory); OpenClaude memory es un editor de
archivos markdown con índice MEMORY.md sin vector search.

**Implicación**: no hay patrón "wiring de alias_index + ranker" en
Hermes/OpenClaude para copiar. La comparación arquitectónica del
doc 22 (eager-inject, post-turn sync) sigue siendo válida, pero
NO aplica a los 4 wiring gaps específicos.

Recomendaciones del Explore agent y mi crítica honesta (sin asumir
verdad — aplicando feedback-verify-assumptions):

### Sobre W2 — eager build + shared via context

**Agent dice**: eager build al boot + compartido entre tools vía
context. **Sub-second cost, evita rebuilds redundantes**.

**Mi pushback**: "shared via context" requiere plumbing — durin no
tiene un context object obvio que pase a tools. Opciones:
- (i) Global singleton: anti-patrón, hard to test.
- (ii) Inyectar en ctx que se pasa a `tool.create(ctx)`: requiere
  cambio en agent loop init.
- (iii) Cada tool construye su propio AliasIndex al primer call: simple.

**Verificación con grep**:
- `grep "_warmup_memory_embedding" durin/agent/loop.py`: existe
  (`agent/loop.py:587`). Patrón establecido de "warm async at boot".
- `grep "tool.create" durin/agent/`: tools se construyen via
  `cls.create(ctx)` pattern (ej memory_store.py:99). El `ctx`
  expone `ctx.config` y `ctx.workspace`. Agregar `ctx.alias_index`
  es posible pero acopla.

**Mi decisión**: para v1, **(iii) cada tool construye al primer call**.
Razones:
- Cost real: build sub-second según docstring de AliasIndex.build().
- Cada tool tiene su instance pero comparten el disk (rebuilds desde
  los mismos .md files); no hay state divergence.
- Si emerge problema de performance, optimizo con T2 (compartir via
  ctx). YAGNI por ahora.

Doc 24 se mantiene (W2 cubierto por lazy en W1 fix).

### Sobre W3 — `vector_index` required vs optional

**Agent dice**: hacer `vector_index` obligatorio en `DreamConsolidator`
constructor signature, eliminar default None.

**Mi pushback**: breaking change a tests que hoy crean
`DreamConsolidator(workspace=..., llm_invoke=stub)`. La require-by-
type implica refactor de ~10 test fixtures.

**Verificación con grep**:
- `grep "DreamConsolidator(" tests/`: 11 test instantiations.
  Mayoría: `DreamConsolidator(workspace=tmp_path, llm_invoke=...)`.
  Requerirlas obligatorias añade boilerplate sin valor.

**Mi decisión**: **mantener optional con default None**. Lo importante
es **siempre pasarlo en producción**. Doc 24 §2.W3 ya lo dice.
Agrego nota en docstring del constructor:
`"vector_index: opcional para tests; pasarlo SIEMPRE en producción
para que las entity pages entren al índice (sin él, retrieval no
las encuentra)"`.

### Sobre W4 — auto-absorb async post-dream

**Agent dice**: auto-absorb async post-dream + manual fallback.

**Mi pushback fuerte**: auto-absorb async tras CADA dream tiene
riesgo silencioso. Si dos entities son aliases similares pero NO la
misma persona (e.g. dos colegas con mismo nombre), el merge sin
confirmación pierde info irrevocablemente (técnicamente recoverable
via git revert, pero error silencioso).

**Verificación**: en doc 18 §10 R6, dijimos explícitamente
"absorption es elegida por user OR auto con confirmación". Hoy
EntityAbsorption.absorb() es no-confirm. Para auto, requeriría:
- LLM-judge para validar merge antes de proceder.
- Confidence threshold por number of shared aliases.
- Notification al user post-merge para rollback.

Esto es trabajo grande, mucho más que "30 LOC CLI". Anti-pattern
para v1.

**Mi decisión**: **mantener mi voto original — opción (c) solo CLI
manual** (`durin memory absorb`). Auto-trigger es T2 con diseño
explícito (LLM-judge, confirmation flow). Documentar en doc 24 §2.W4
que **W4(c) v1 = manual command, NO auto-absorb**.

### Adopciones reales tras agent review

| Gap | Doc 24 original | Tras agent review | Cambio |
|---|---|---|---|
| W1 | memory_search integra ranker post-vector | Igual | Sin cambio |
| W2 | Lazy en memory_search | Igual (agent sugirió eager+shared, evaluado, rechazado YAGNI) | Sin cambio + nota |
| W3 | Optional vector_index, cmd_dream pasa | Igual (agent sugirió required, evaluado, rechazado breaking) | Sin cambio + nota |
| W4 | Manual CLI command | Igual (agent sugirió auto-async, evaluado, rechazado por riesgo silencioso) | Sin cambio + nota |

**Conclusión de la wiring comparison**: las 4 decisiones del doc 24
sobreviven la crítica. La comparación NO me reveló patrones nuevos
load-bearing — porque Hermes/OpenClaude no tienen entity-centric.
Pero el ejercicio valió: forzó re-evaluación crítica de las
decisiones implícitas, y la nota en doc 24 §6 documenta el por qué
de cada una vs alternativas.

---

## §7 — glm peer review findings + ajustes aplicados

glm-5.1 revisó doc 24 + código real. Aplicando `feedback-verify-
assumptions`, cada finding fue chequeado contra código antes de
aceptarlo.

### Bloqueantes (verificados, ajustes obligatorios)

**B1 — VectorIndex no persiste `entities` field**

`durin/memory/vector_index.py:_record_with_vector` (líneas 355-363)
construye record con solo `{id, class_name, summary, headline,
vector, valid_from, path}`. **NO incluye `entities`**.

Consecuencia: `rank_with_entities` busca `c.get("entities", [])` y
siempre obtiene `[]`. **La parte de W1 "boost post-cursor por entity
tag match" queda inoperante** porque no hay tags en los rows.

**Fix obligatorio**:

1. Modificar `_record_with_vector` para incluir `entities` field:
   ```python
   return {
       "id": entry.id,
       "class_name": class_name,
       "summary": entry.summary,
       "headline": entry.headline,
       "vector": vector,
       "valid_from": entry.valid_from.isoformat() if entry.valid_from else "",
       "entities": list(entry.entities),  # NEW: needed for ranker boost
       "path": str(rel_path),
   }
   ```

2. **Migration path**: LanceDB tables existentes sin la columna van a
   romper en read. Opciones:
   - (i) `VectorIndex.rebuild_from_workspace()` se invoca al detectar
     schema mismatch.
   - (ii) Lenient `_record_with_vector` lee con `row.get("entities", [])`
     en search results — ya hace eso implícitamente en
     `_vector_row_to_result` que ignora campos extra.
   - Decisión: **(i) rebuild en upgrade path**. Agregar detection de
     "missing column" en `_guard_dim_match` o helper similar.

3. **Para upsert_entity_page** (entity pages): también debe escribir
   `entities` field — esta vez vacío o solo el self-ref. Coherencia.

**Costo**: +1h sobre la estimación. Migration handling es lo más
delicado.

**B2 — Pseudo-snippet W1 incompleto**

El snippet en doc 24 §2.W1 termina en `# reorder vector_rows by
ranked.adjusted_score` sin el código real. Implementador podría
malinterpretar.

**Snippet completo correcto**:

```python
# In memory_search.execute(), after vi.search() but before
# converting to Result objects:
from durin.memory.entity_ranker import (
    extract_query_entities, rank_with_entities,
)

vector_rows = vi.search(query, top_k=10)  # list[dict]

ai = self._get_alias_index()
if ai is not None and ai.size() > 0:
    query_entities = extract_query_entities(query, ai)
    if query_entities:
        cursors = _load_cursors_from_entities_dir(
            self._workspace / "memory", query_entities,
        )
        ranked = rank_with_entities(
            vector_rows,
            query_entities=query_entities,
            cursors=cursors,
            score_field="_distance",
            higher_is_better=False,
        )
        # Reorder: extract record from each RankedCandidate.
        vector_rows = [rc.record for rc in ranked]

# Now convert (reordered) vector_rows to Result objects.
vector_results = [_vector_row_to_result(row) for row in vector_rows]
```

### Serios (no bloqueantes pero atender)

**S1 — `strategy` field backward-compat**

`memory_search.execute()` retorna `strategy: "grep"|"vector"|"hybrid"`.
Agregar "entity_aware" rompe callers que hacen pattern match exacto.

**Fix**: agregar campo **separado** `ranking: "default" | "entity_aware"`.
Backward-compatible.

```python
return {
    "results": [...],
    "total": len(results),
    "strategy": strategy,         # unchanged: grep|vector|hybrid
    "ranking": ranking_applied,   # NEW: default|entity_aware
}
```

**S2 — Telemetry overlap**

`memory_search.py:129-138` ya emite `memory.recall.vector`. Agregar
`memory.recall.entity_aware` separado es duplicación.

**Fix**: extender el evento existente con campos:
```python
emit_tool_event("memory.recall.vector", {
    ...existing...,
    "entity_aware": ranking_applied == "entity_aware",
    "query_entities_count": len(query_entities) if query_entities else 0,
    "reordered_top_k": <delta visible>,
})
```

**S3 — Helper signature `_load_cursors`**

Doc 24 W1 pseudo-snippet llamaba `_load_cursors(ai)` pasando AliasIndex.
**AliasIndex NO tiene cursors** — están en EntityPage frontmatter.

**Fix**: renombrar a `_load_cursors_from_entities_dir(memory_root, query_entities)`.
Itera entity refs, lee cada page, extrae `dream_processed_through`.
Cachea in-memory.

```python
def _load_cursors_from_entities_dir(
    memory_root: Path,
    entity_refs: list[str],
) -> dict[str, Any]:
    cursors: dict[str, Any] = {}
    for ref in entity_refs:
        type_, slug = ref.split(":", 1)
        page_path = memory_root / "entities" / type_ / f"{slug}.md"
        if not page_path.exists():
            continue
        try:
            page = EntityPage.from_file(page_path)
        except Exception:
            continue
        if page and page.dream_processed_through is not None:
            cursors[ref] = page.dream_processed_through
    return cursors
```

### Menores (anotados)

**M1 — alias_index build en hot path** (glm 7b):
sub-second para corpus < 100 pages. Para >200, podría bloquear primer
query (~1-2s). Aceptable hoy. **Anotar como límite conocido**, no
bloquear.

**M2 — telemetría delta** (glm 3 extended):
glm sugirió capturar "delta antes/después" del ranker. Útil para
Phase 0.2. Lo agrego al evento (S2): `top_1_id_before` y `top_1_id_after`.
Sub-100 bytes de overhead, mucho insight.

### Lo que glm CONFIRMÓ (no requiere fix)

- Orden W3 → W1+W2 → W4 correcto (W1 sin W3 es incrementalmente
  útil — boost por tag funciona independiente del entity_page in
  index).
- Instancias separadas AliasIndex entre tools: aceptable (no hay
  divergencia de state porque LanceDB comparte y AliasIndex relee
  los mismos .md).
- E2E-2 antes que E2E-1 correcto.
- W4(c) — manual CLI absorb — defensible. Auto-async tiene riesgo
  silencioso.
- E2E-1 bypass via `upsert_entity_page` directo es legítimo
  testing isolation.

### Resumen acciones

| # | Acción | Impacto en plan |
|---|---|---|
| B1 | VectorIndex incluye `entities` field + rebuild path | +1h, modifica vector_index.py + cmd_dream o boot debe rebuild si schema viejo |
| B2 | Snippet W1 completo en doc 24 | sin costo (doc fix) |
| S1 | Separar `ranking` field del `strategy` field | +5 LOC |
| S2 | Extender evento `memory.recall.vector` en vez de duplicar | +5 LOC |
| S3 | Renombrar helper `_load_cursors_from_entities_dir` | sin costo (rename) |
| M1 | Anotar límite sub-second build | docstring nota |
| M2 | Telemetría delta top_1 before/after | +5 LOC |

**Total cambios al plan**: +1.5h sobre estimación original (6-8h → 7.5-9.5h).

Las 4 decisiones macro del §6 (lazy, optional vector_index, manual
absorb, orden de fixes) **siguen válidas**.

### Verificación cruzada: glm dijo, yo verifiqué

| Finding glm | Mi verificación | Conclusión |
|---|---|---|
| B1 entities no en row | grep `_record_with_vector`: confirmado | Aceptado |
| B2 snippet incompleto | Re-leí doc 24 §2.W1: confirmado | Aceptado |
| S1 strategy backward-compat | `grep "strategy" durin/`: callers harían match | Aceptado |
| S2 telemetry overlap | `grep "memory.recall.vector"`: ya emitido | Aceptado |
| S3 _load_cursors signature | AliasIndex no tiene cursors (verified) | Aceptado |
| 7b I/O hot path | Docstring dice sub-second; lo medí no | Anotado, no bloqueante |

glm acertó 6/6 verificables. **No tomar como gospel** pero esta vez
acertó. Voy a internalizar: validar B1 a fondo antes de codear es lo
crítico.

---

## §8 — Open questions para glm

Cuando este doc pase a glm, le pido enfoque en:

1. **¿El orden de los wiring fixes es correcto?** ¿Hay dependencia
   oculta? (e.g., podría W1 sin W3 producir comportamiento confuso
   porque entity_page no está en el índice?)

2. **Ranker antes vs después de Result conversion**: ¿integrar el
   ranker SOBRE los raw rows de LanceDB es la decisión correcta?
   ¿O mejor envolver los Result objects?

3. **Telemetry shape**: ¿`memory.recall.entity_aware` con
   `{query_entities, top_k_signal_summary}` es útil? ¿Algo más?

4. **AliasIndex y VectorIndex no comparten state entre tools**:
   ¿problema real o aceptable hoy?

5. **Si los e2e fallan parcialmente (e.g., E2E-1 funciona pero
   E2E-3 cold-start no)**, ¿cuál priorizar?

6. **EntityAbsorption (W4)**: ¿(a) borrar, (b) status quo, (c) exponer?
   ¿Mi voto (c) es defensible o glm vería un caso para (a)?

7. **¿Algo crítico que omití?**

---

## Last updated: 2026-05-23 (pre-glm review, post self-review)
