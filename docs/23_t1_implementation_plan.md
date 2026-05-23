# 23 — T1.x Implementation Plan (detallado, por clusters de riesgo)

> Plan de implementación de los 6 items T1.x consolidados en doc 21
> (post-construction integration plan) + doc 22 (post-verification
> contra 8 sistemas reales). Orden por **riesgo y dependencia**, no
> por número T1.x.
>
> Cada cluster: build → test integration → run live → commit.
> glm peer review se hace UNA vez sobre este plan completo antes de
> ejecutar (no per-change).

---

## §0 — Items T1.x consolidados (recap)

De doc 22 §3:

| # | Item | Origen | Riesgo |
|---|---|---|---|
| T1.1 | Tool desc corta + 4 ejemplos + strict validation | doc 21 (glm A1) + doc 22 A1 matiz | Bajo (mecánico) |
| T1.3 | RRF reemplaza score-multiplier en entity_ranker | doc 21 (glm A2) + doc 22 A2 confirmado 100% | Medio (refactor interface) |
| T1.4 | Drop alias_index save/load, rebuild-only | doc 21 (glm A3) + doc 22 A3 confirmado | Bajo (mecánico) |
| T1.5 | `durin memory dream` comando manual | doc 21 T1.5 (no controvertido) | Bajo (aditivo) |
| T1.6 | Pydantic + retry + context budget en dream | doc 21 (glm A4) + doc 22 A4 3/4 confirmado | Alto (toca dream LLM path) |
| T1.7 | Vector similarity dedup pre-persist | doc 22 N3 nuevo (OpenClaw pattern) | Alto (write path crítico) |

---

## §1 — Cluster A: Mecánicos (T1.1 + T1.4)

**Justificación de orden**: baja superficie de cambio, alto valor inmediato,
tests existentes cubren. Hacer primero para construir confianza.

### A.1 — T1.1: Tool description corta + strict validation refinement

**Estado actual**: `durin/agent/tools/memory_store.py:46-56`

```python
entities=ArraySchema(
    StringSchema("entity reference in '<type>:<value>' form"),
    description=(
        "Optional list of typed entity references this memory mentions. "
        "Each item MUST follow the form '<type>:<value>' where type is "
        "lowercase [a-z][a-z0-9_]* and value is non-empty. Suggested "
        "types (open vocabulary — new types welcome when content "
        "demands): person, place, project, topic, event, artifact, "
        "stance, practice. Examples: 'person:marcelo', "
        "'project:durin', 'topic:embeddings', 'artifact:settings.py'."
    ),
),
```

**Problema**: 11 líneas de description. El modelo va a perder atención
después de la línea 3. Per glm A1 + doc 22: el modelo va a inventar
formato (`Persona:Marcelo`, `marcelo` sin tipo). El strict validation
en `store.py:62-72` ya rechaza con `StoreError` (bueno), pero el modelo
pierde 1 turno reescribiendo.

**Cambio propuesto** (memory_store.py:46-56):

```python
entities=ArraySchema(
    StringSchema("entity reference"),
    description=(
        "Optional list, format '<type>:<slug>'. Examples: "
        "person:marcelo, project:durin, topic:embeddings, event:bug-X. "
        "Use lowercase slugs. Types are open vocabulary "
        "(person/place/project/topic/event/artifact/stance/practice "
        "are common but not exhaustive)."
    ),
),
```

**Cambios concretos**:
- Description de 11 líneas → 4 líneas.
- Quita "MUST follow the form" (el modelo no procesa requirements bien
  en prompts largos; los ejemplos enseñan mejor).
- Cambia "value" por "slug" (clarifica que debe ser lowercase
  identifier, no texto libre con espacios).
- Quita "suggested types — new types welcome" — implícito en "are
  common but not exhaustive". Más conciso.

**Validation** ya está strict en `durin/memory/store.py:62-72` con
`is_valid_entity_ref`. **No cambia.**

**Tests**:
- `tests/agent/tools/test_memory_store.py` — verificar que la nueva
  description sigue presente y aceptable.
- `tests/memory/test_entities.py` — sin cambios (validation no cambió).
- No requiere tests nuevos.

**Breaking changes**: ninguno. Description es prompt-level, no afecta
API.

**Live verification (post-cluster A)**:
- Correr `durin agent --message "I prefer pytest and work on durin"`
- Verificar que el modelo llama `memory_store(entities=[...])` con
  formato correcto (`person:marcelo`, `project:durin`, `topic:pytest`
  o equivalente). Si lo hace mal, iterar el prompt.

**Costo estimado**: 30 min implementación + 15 min verificación live.

### A.2 — T1.4: Drop alias_index save/load, rebuild-only

**Estado actual**: `durin/memory/aliases_index.py:92-124`

Métodos `load()` y `save()` que persisten/leen `memory/.aliases.json`.
Usados por:
- `dream.py:242`: `idx.save()` tras consolidación
- `dream.py:378`: `idx.load()` en `_get_alias_index()` (con fallback a
  `build()`)
- `absorption.py:226`: `idx.save()` tras absorción
- `absorption.py:248`: `idx.load()` en `_get_alias_index()`

**Problema**: per glm A3 + doc 22 A3: el sidecar JSON queda stale si
algún `.md` se edita fuera del tool (vim, git merge externo). Para el
tamaño esperado (cientos de entities), rebuild es sub-second
(per docstring de `build()`).

**Cambio propuesto**:

1. **Eliminar** métodos `load()` y `save()` de `aliases_index.py`.
2. **Eliminar** método `sidecar_path()` (ya no relevante).
3. **Reemplazar** las invocaciones en `dream.py:242` y `absorption.py:226`:
   - **Antes**: `idx.save()` tras refresh
   - **Después**: nada (refresh queda en memoria; la próxima vez que
     el proceso arranca, rebuild from disk lo recompone)
4. **Reemplazar** las invocaciones en `dream.py:378` y `absorption.py:248`:
   - **Antes**: `if not self._alias_index.load(): self._alias_index.build()`
   - **Después**: `self._alias_index.build()` (always)
5. **Eliminar** `.aliases.json` del `.gitignore` que GitRepo aplica
   (ya no se genera; no rompe nada pero conviene limpiar).

**Tests**:
- `tests/memory/test_aliases_index.py`:
  - Eliminar `TestPersistence` clase entera (3 tests).
  - Las clases `TestBuild`, `TestIncremental`, `TestLookup` quedan
    sin cambio.

**Breaking changes**:
- La API pública de `AliasIndex` cambia (sin `load()`, `save()`,
  `sidecar_path()`). Importadores externos romperían — verificar
  con grep que solo los archivos del proyecto importan.

**Validación cruzada**:
```bash
grep -rn "alias_index.save\|alias_index.load\|sidecar_path\|aliases\.json" \
  durin/ tests/ scripts/
```
Confirma que solo `dream.py`, `absorption.py`, los tests, y referencias
en docs cambian.

**Live verification**:
- No requiere live test (los tests integration ya cubren). Suite
  existente debería pasar tras el cambio.

**Costo estimado**: 45 min implementación + 15 min tests.

### Resumen Cluster A

- 2 archivos production código tocados (memory_store.py, aliases_index.py).
- 1 archivo de tests modificado (test_aliases_index.py — eliminar
  TestPersistence).
- ~80 LOC borradas, ~30 LOC modificadas.
- 0 nuevos tests requeridos (las clases existentes cubren).
- Riesgo: bajo. Si live verification de T1.1 muestra que el modelo
  sigue mal, iterar prompt sin tocar arquitectura.

---

## §2 — Cluster B: Algorítmico (T1.3)

**Justificación de orden**: cambio interno con interface estable hacia
afuera (la función `rank_with_entities` se llama desde un solo lugar
todavía — `tests/integration/test_phase3_retrieval_e2e.py`). Refactor
contenido en `entity_ranker.py`. Tests detallados validan el comportamiento.

### B.1 — T1.3: RRF reemplaza score-multiplier

**Estado actual**: `durin/memory/entity_ranker.py:46-50` define las
constantes `BOOST_POST_CURSOR=1.5`, `BOOST_ENTITY_PAGE=1.4`,
`DEMOTE_PRE_CURSOR=0.7`. La función `rank_with_entities` aplica estas
multiplicativamente sobre `normalized = 1/(1+distance)`.

**Problema** (glm A2 + doc 22 A2 confirmado): el patrón **no existe** en
ningún sistema clonado. Graphiti usa RRF (`1/(rank+k)`). Distances de
LanceDB pueden ser 10-50 (depende del modelo de embeddings y query),
mapearlos via `1/(1+d)` los aplasta a [0.02, 0.09], y multiplicaciones
1.5x sobre ese rango son ruido para ordering.

**RRF (Reciprocal Rank Fusion) — algoritmo**:

Dado N listas rankeadas (cada una ordena documentos best-first),
para cada documento que aparece en cualquier lista:

```
score[doc] = sum over each list L:  1 / (rank_in_L + k)
```

Donde `k` es una constante (típicamente 60 — Cormack et al. 2009).
Documentos en múltiples listas se acumulan. Sort descending por
score → ranking final.

**Diseño para durin**:

Tres "listas" potenciales:

1. **Vector search ranking** (siempre presente): la lista de
   `vector_index.search(query)` ya ordenada por distance ascendente.
2. **Entity match ranking** (cuando hay query_entities): construir
   una lista priorizada de candidatos que matchean por entity tag:
   - Entity pages cuya `id` == una query entity → top
   - Memory entries con `entities` overlap, ordenadas por `created_at`
     descendente (frescos primero)
3. **(Futuro)** BM25 ranking si se activa Phase 2c — diferido.

Boost de "entity page id == query entity" se vuelve **estar en el top
de la lista entity-match**, no un multiplicador.

Demote de "pre-cursor" se vuelve **no incluir en lista entity-match**
(la consolidada ya tiene esa info; mostrarla duplica).

**Pseudo-código de la nueva `rank_with_entities`**:

```python
RRF_K = 60  # standard from Cormack et al.

def rank_with_entities(
    candidates: list[dict],
    *,
    query_entities: list[str],
    cursors: dict[str, str | None] | None = None,
    score_field: str = "_distance",
    higher_is_better: bool = False,
) -> list[RankedCandidate]:
    """Multi-signal ranking via Reciprocal Rank Fusion (RRF).
    
    Compose two rankings:
    - Vector-similarity rank from `candidates` ordered by score_field.
    - Entity-match rank: entity_pages whose id ∈ query_entities first,
      then post-cursor memory entries tagged with any query entity,
      ordered by recency. Pre-cursor tagged entries excluded (their
      info lives in the page).
    
    Final score per doc = sum of 1/(rank_in_list + k) across lists.
    """
    cursors = cursors or {}
    query_entity_set = set(query_entities)
    
    # Sort candidates by base score to get vector-rank.
    def base_sort_key(c):
        v = float(c.get(score_field, 0.0))
        return v if not higher_is_better else -v
    by_vector = sorted(candidates, key=base_sort_key)
    
    # Build entity-match list (only when query has entities).
    entity_rank_list: list[dict] = []
    if query_entity_set:
        # Pages first
        pages_for_query = [
            c for c in candidates
            if c.get("class_name") == "entity_page"
            and c.get("id") in query_entity_set
        ]
        # Tagged entries post-cursor
        tagged_post = []
        for c in candidates:
            if c.get("class_name") == "entity_page":
                continue
            recs = c.get("entities", []) or []
            if isinstance(recs, str):
                recs = [e.strip() for e in recs.split(",") if e.strip()]
            overlap = [e for e in recs if e in query_entity_set]
            if not overlap:
                continue
            entry_ts = c.get("valid_from") or c.get("created_at") or ""
            is_pre = any(
                isinstance(cursors.get(e), str)
                and isinstance(entry_ts, str)
                and entry_ts != ""
                and entry_ts <= cursors[e]
                for e in overlap
            )
            if not is_pre:
                tagged_post.append((entry_ts, c))
        # Sort post-cursor by recency (newest first)
        tagged_post.sort(key=lambda t: t[0], reverse=True)
        entity_rank_list = pages_for_query + [t[1] for t in tagged_post]
    
    # Identify each candidate's id for RRF accumulation.
    def doc_id(c) -> str:
        return c.get("id", id(c))  # fallback
    
    scores: dict[str, float] = {}
    signals: dict[str, list[str]] = {}
    
    for rank, c in enumerate(by_vector):
        scores[doc_id(c)] = scores.get(doc_id(c), 0.0) + 1.0 / (rank + RRF_K)
    
    for rank, c in enumerate(entity_rank_list):
        scores[doc_id(c)] = scores.get(doc_id(c), 0.0) + 1.0 / (rank + RRF_K)
        signals.setdefault(doc_id(c), []).append(
            f"entity_match_rank:{rank}"
        )
    
    # Build output
    ranked = []
    for c in candidates:
        did = doc_id(c)
        ranked.append(RankedCandidate(
            record=c,
            base_score=float(c.get(score_field, 0.0)),
            adjusted_score=scores.get(did, 0.0),
            signals=signals.get(did, []),
        ))
    ranked.sort(key=lambda r: r.adjusted_score, reverse=True)
    return ranked
```

**Changes**:
- Eliminar constantes `BOOST_POST_CURSOR`, `BOOST_ENTITY_PAGE`,
  `DEMOTE_PRE_CURSOR` del módulo.
- Eliminar `__all__` references a esas constantes.
- Nueva constante `RRF_K = 60`.
- Reescribir `rank_with_entities` con el algoritmo arriba.
- Mantener la signature pública estable (mismos params; mismo
  `RankedCandidate` dataclass).
- Comments + docstring actualizados.

**Tests** (`tests/memory/test_entity_ranker.py`):

Cambios necesarios:
- `test_no_query_entities_preserves_order`: ✓ debería seguir pasando.
- `test_entity_page_for_query_entity_boosted`: cambiar assertion sobre
  signals (`"entity_page:..."` → `"entity_match_rank:0"`).
- `test_memory_entry_with_matching_tag_post_cursor_boosted`: el
  comportamiento se mantiene (post-cursor entry sube vs base). Cambia
  signal name.
- `test_memory_entry_pre_cursor_demoted`: el pre-cursor NO entra a
  entity_rank_list → solo recibe el vector score. Tests siguen
  asserción "pre cae al fondo" pero hay que verificar.
- `test_combined_realistic_mix`: revisar el ordenamiento esperado.
- `test_higher_is_better_score_handled`: ✓ flag sigue funcionando.
- `test_no_cursor_defaults_to_boost`: cambiar a "no cursor → entrada
  entra al entity_rank_list como post-cursor". Mismo efecto. Signal
  name cambia.

**Tests nuevos** a agregar:
- `test_rrf_two_lists_fuse_correctly`: doc en ambas listas score acumulado.
- `test_rrf_only_in_vector_list`: doc solo en vector → score = 1/(rank+k).
- `test_rrf_with_k_60_typical_distribution`: distribution check para
  catch regressions de la constante.

**Test de integración** (`tests/integration/test_phase3_retrieval_e2e.py`):
- Ajustar asserts sobre signals (string match).
- Verificar que page surface en top 3 cuando query menciona entidad
  (criterio principal del test) sigue cumpliendose.

**Breaking changes**:
- API pública de `rank_with_entities` igual. `RankedCandidate` igual.
- Constantes públicas `BOOST_*` y `DEMOTE_*` desaparecen. Búsqueda:
  ```bash
  grep -rn "BOOST_POST_CURSOR\|BOOST_ENTITY_PAGE\|DEMOTE_PRE_CURSOR" \
    durin/ tests/
  ```
  Si solo aparecen en `entity_ranker.py` + sus tests, OK.

**Live verification** (post-cluster B):
- Setup: workspace con 2-3 entity pages + 5+ memory entries con tags.
- Correr una query que mencione una entidad.
- Inspeccionar visualmente top-5 results: ¿la página sale arriba?
- Es difícil hacer test live sin un corpus real. Aceptable: tests
  unit + integration son suficientes para Cluster B.

**Costo estimado**: 2-3 hours implementación + tests.

---

## §3 — Cluster C: Write/Parse path (T1.7 + T1.6)

**Justificación de orden**: alto riesgo (toca write path crítico y
dream LLM path). Hacer al final. Antes de C, A y B han ejercitado los
patrones de tests + el comportamiento del modelo en live.

### C.1 — T1.7: Vector similarity check pre-persist (OpenClaw pattern)

**Estado actual**: `durin/agent/tools/memory_store.py:171-178` upserta
al vector index DESPUÉS de escribir el .md. No hay check de duplicate
contenido similar.

**Patrón OpenClaw** (`memory-lancedb/index.ts:783-798`): antes de
persistir, busca top-1 nearest neighbor del contenido nuevo. Si
similarity > threshold (e.g. 0.95), descarta o ofrece update.

**Problema que resuelve**: previene duplicates al write-time, en vez de
absorberlos después (que sí decidimos no usar auto-trigger).

**Cambio propuesto** (`memory_store.py:execute`):

```python
async def execute(self, **kwargs: Any) -> Any:
    # ... validation existente ...
    
    # NEW: pre-persist similarity check.
    # Only when vector index available + memory enabled.
    vi = self._get_vector_index()
    if vi is not None:
        try:
            # Quick top-1 search; if cosine similarity to existing entry
            # exceeds threshold, return advisory error so the model can
            # decide whether to update or skip.
            hits = vi.search(content, top_k=1)
            if hits and hits[0].get("_distance", 1.0) < self._DEDUP_DISTANCE_THRESHOLD:
                near = hits[0]
                return {
                    "warning": "near-duplicate",
                    "nearest_id": near["id"],
                    "nearest_headline": near.get("headline", ""),
                    "nearest_distance": near["_distance"],
                    "hint": "Consider updating existing entry instead of creating a new one.",
                }
        except Exception as exc:
            logger.warning("dedup similarity check failed: %s", exc)
            # Don't fail the write; just skip the dedup.
    
    # ... write path existente ...
```

Add to class:

```python
# LanceDB L2 distance below which we consider the new content a
# near-duplicate. Calibrated conservatively — set high (small distance)
# to avoid false positives that block legitimate writes.
_DEDUP_DISTANCE_THRESHOLD = 0.05  # ~0.95 cosine sim with normalized vectors
```

**Decisión de UX**: ¿retornar warning (modelo decide) o silenciar (skip
write)? Per OpenClaw: discard. Pero para durin, lazy aproach es mejor:
**retornar warning con info del near-match, NO bloquear**. El modelo
puede entonces:
- Llamar de nuevo con argumento explícito tipo `force=True` (si añadimos).
- O decidir no escribir.
- O llamar a un hipotético `memory_update(id=..., content=...)` en el
  futuro.

Para v1 mantenemos simple: warning + no escribir si threshold cruzado.
El modelo recibe warning, puede ajustar.

**Refinamiento**: el threshold 0.05 puede ser muy estricto. Mejor empezar
**0.1** (cosine ~0.90) para conservador. Iterar si genera falsos
positivos.

**Tests** (nuevos en `tests/agent/tools/test_memory_store.py`):
- `test_dedup_warning_on_near_match`: write content A. write content
  A again. Segundo write devuelve warning con `nearest_id`.
- `test_dedup_allows_distinct_content`: write content A, write content
  B (no related). Ambos persisten.
- `test_dedup_skip_when_index_disabled`: memory.enabled=False → no
  check, ambos persisten.
- `test_dedup_failure_does_not_block_write`: mock vector index that
  raises → warning logged, write proceeds.

**Breaking changes**: el tool puede devolver `warning` en vez de
`result` en casos de near-match. Callers deberían chequear ambos
shapes. Documentar en docstring del tool.

**Live verification** (crítica):
- Escribir "I prefer pytest" 2 veces seguidas.
- Verificar: segundo write devuelve warning.
- Verificar logs no muestran corrupción del store.

**Costo estimado**: 1-2 hours.

### C.2 — T1.6: Pydantic + retry + context budget en dream

**Estado actual**: `durin/memory/dream.py:171-187`:

```python
def consolidate_entity(
    self,
    entity_ref: str,
    entries: list[EntryRef],
) -> ConsolidationResult:
    if not entries:
        raise DreamError(...)
    if ":" not in entity_ref:
        raise DreamError(...)
    
    current_page = self._read_existing_page(entity_ref)
    prompt = self._build_prompt(entity_ref, entries, current_page)
    raw = self._llm_invoke(prompt, model=self.model)
    return self._parse_response(raw)
```

Y `_parse_response` (línea 332+):
```python
@staticmethod
def _parse_response(raw: str) -> ConsolidationResult:
    # ... strip fence ...
    match = _SECTION_PAGE.search(stripped)
    if not match:
        raise DreamError(
            "LLM response missing ===PAGE=== / ===COMMIT=== markers"
        )
    # ...
```

**Problemas** (glm A4 + doc 22 A4 confirmado 3/4):
1. Sin retry: una sola parse failure → raise.
2. Sin Pydantic-like validation del page parseado.
3. Sin context budget: 500 entries pasarían al prompt sin cap.
4. (Doc 22) Hallucination detection — nadie lo hace, defer.

**Cambios propuestos**:

**C.2.a — Context budget**:

```python
# Class attribute on DreamConsolidator
MAX_ENTRIES_PER_CALL = 50  # cap per LLM call; batch beyond this

def consolidate_entity(
    self,
    entity_ref: str,
    entries: list[EntryRef],
) -> ConsolidationResult:
    if not entries:
        raise DreamError(...)
    if ":" not in entity_ref:
        raise DreamError(...)
    
    # Apply context budget — too many entries blow LLM context window.
    # For initial implementation: just cap and warn; later we can
    # batch in groups and consolidate iteratively.
    if len(entries) > self.MAX_ENTRIES_PER_CALL:
        logger.warning(
            "dream consolidate: capping entries %d -> %d for %s "
            "(remaining will be picked up next consolidation)",
            len(entries), self.MAX_ENTRIES_PER_CALL, entity_ref,
        )
        entries = entries[-self.MAX_ENTRIES_PER_CALL:]  # take newest
    
    # ... rest unchanged ...
```

**C.2.b — Retry on parse failure**:

```python
MAX_RETRIES = 3

def consolidate_entity(self, ...):
    # ... budget + setup ...
    
    last_error: str | None = None
    for attempt in range(self.MAX_RETRIES):
        prompt = self._build_prompt(entity_ref, entries, current_page)
        if last_error is not None:
            prompt += (
                f"\n\nNote: previous attempt failed with error: "
                f"{last_error}\nPlease produce a strictly-formatted "
                f"response with ===PAGE=== and ===COMMIT=== markers."
            )
        raw = self._llm_invoke(prompt, model=self.model)
        try:
            return self._parse_response(raw)
        except DreamError as exc:
            last_error = str(exc)
            logger.warning(
                "dream consolidate attempt %d failed: %s",
                attempt + 1, exc,
            )
    raise DreamError(
        f"dream failed after {self.MAX_RETRIES} attempts: {last_error}"
    )
```

**C.2.c — Validation strict del page parseado**:

Tras parsear ===PAGE===, validar via `EntityPage.from_text()`:

```python
@staticmethod
def _parse_response(raw: str) -> ConsolidationResult:
    # ... existing fence strip + regex match ...
    
    page_text = match.group(1).strip() + "\n"
    commit_text = match.group(2).strip()
    
    # NEW: validate page parses cleanly. EntityPage.from_text returns
    # None on bad frontmatter / missing required fields.
    from durin.memory.entity_page import EntityPage
    parsed_page = EntityPage.from_text(page_text)
    if parsed_page is None:
        raise DreamError(
            "LLM produced page text that does not parse as a valid "
            "EntityPage (missing required frontmatter or malformed YAML)"
        )
    
    # Optional: size cap on page_text (per glm — 25KB hard cap).
    if len(page_text) > _PAGE_MAX_BYTES:
        raise DreamError(
            f"LLM page text exceeds {_PAGE_MAX_BYTES} bytes "
            f"({len(page_text)} bytes); refusing to commit"
        )
    
    # ... rest: split commit, return ConsolidationResult ...
```

Add module constants:

```python
_PAGE_MAX_BYTES = 25 * 1024  # 25KB hard cap
```

**Tests** (nuevos en `tests/memory/test_dream.py`):
- `test_consolidate_caps_entries_at_max`: stub LLM gets called with at most MAX_ENTRIES.
- `test_consolidate_retries_on_parse_failure`: first stub returns malformed → second valid → success.
- `test_consolidate_fails_after_max_retries`: all stubs return malformed → DreamError raised mentioning attempt count.
- `test_parse_rejects_unparseable_page`: stub returns ===PAGE=== with malformed YAML → DreamError.
- `test_parse_rejects_oversized_page`: stub returns >25KB page → DreamError.

**Breaking changes**: `consolidate_entity` puede tardar Nx más (3 LLM
calls en el peor caso). Aceptable: dream es async, no UX-blocking.

**Live verification** (crítica):
- Setup: workspace con 60+ entries para person:marcelo.
- Correr `durin memory dream` (post T1.5).
- Verificar:
  - Log muestra que se capó a 50.
  - Output coherente.
  - Si forzamos un fallo de parse (stub LLM), retries.

**Costo estimado**: 2-3 hours.

### Resumen Cluster C

- 2 archivos prod tocados (memory_store.py, dream.py).
- 2 archivos tests modificados/expandidos.
- Riesgo alto: write path + dream path son críticos. Test integration
  imprescindible antes de live.

---

## §4 — Cluster D: CLI (T1.5)

**Justificación de orden**: aditivo, no rompe nada. Pequeño pero útil.
Hacer último porque depende de que dream funcione (que se valida en
Cluster C).

### D.1 — T1.5: `durin memory dream` comando manual

**Estado actual**: `durin/cli/memory_cmd.py` tiene `history`, `show`,
`diff`, `revert`, `expand`. **No tiene `dream`**.

**Diseño**:

```python
@memory_app.command("dream")
def cmd_dream(
    entity: str = typer.Argument(
        None,
        help="Specific entity (e.g., person:marcelo) to consolidate. "
             "If omitted, consolidates all entities with pending entries.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be consolidated without writing.",
    ),
    max_entries: int = typer.Option(
        50,
        "--max-entries",
        help="Cap entries per entity per call (default 50).",
    ),
) -> None:
    """Manually trigger memory consolidation (dream pass).
    
    Reads memory/episodic entries with entity tags, groups by entity,
    invokes the dream LLM consolidator, and writes/updates entity pages.
    """
    workspace = _workspace_root()
    memory_root = workspace / "memory"
    
    if not (memory_root / "episodic").exists():
        console.print("[yellow]No episodic memory yet — nothing to dream.[/yellow]")
        return
    
    # Discover entities with pending entries
    from durin.memory.dream import DreamConsolidator, EntryRef
    from durin.memory.storage import load_entry
    
    # Group entries by entity. For each entity, find entries newer than
    # the page's dream_processed_through cursor.
    pending = _discover_pending_consolidations(memory_root, entity_filter=entity)
    
    if not pending:
        console.print("[green]No pending consolidations.[/green]")
        return
    
    for entity_ref, entry_refs in pending.items():
        console.print(f"\n[bold]{entity_ref}[/bold]: {len(entry_refs)} entries")
        if dry_run:
            for er in entry_refs[:3]:
                console.print(f"  - {er.id}: {er.text[:80]}")
            if len(entry_refs) > 3:
                console.print(f"  ... +{len(entry_refs)-3} more")
            continue
        
        consolidator = DreamConsolidator(
            workspace=workspace,
            model=...,  # from config
        )
        try:
            result = consolidator.consolidate_entity(entity_ref, entry_refs)
            sha = consolidator.apply(entity_ref, result)
            if sha:
                console.print(f"  [green]✓[/green] Consolidated → {sha[:8]}")
            else:
                console.print(f"  [dim]= No changes[/dim]")
        except Exception as exc:
            console.print(f"  [red]✗[/red] Failed: {exc}")


def _discover_pending_consolidations(
    memory_root: Path,
    entity_filter: str | None = None,
) -> dict[str, list[EntryRef]]:
    """Walk memory/episodic, group entries by entity, filter by cursor."""
    from durin.memory.entity_page import EntityPage
    from durin.memory.storage import load_entry
    
    pending: dict[str, list[EntryRef]] = {}
    
    episodic_dir = memory_root / "episodic"
    if not episodic_dir.exists():
        return pending
    
    # Load existing pages to know cursors
    cursors: dict[str, str | int] = {}
    pages_dir = memory_root / "entities"
    if pages_dir.exists():
        for page_path in pages_dir.rglob("*.md"):
            if "/archive/" in str(page_path):
                continue
            page = EntityPage.from_file(page_path)
            if page is None:
                continue
            slug = EntityPage.slug_from_path(page_path)
            ref = f"{page.type}:{slug}"
            if page.dream_processed_through is not None:
                cursors[ref] = page.dream_processed_through
    
    # Walk episodic entries, group by entity, filter by cursor
    for entry_path in episodic_dir.glob("*.md"):
        entry = load_entry(entry_path)
        for ent_ref in entry.entities:
            if entity_filter and ent_ref != entity_filter:
                continue
            cursor = cursors.get(ent_ref)
            ts = entry.valid_from.isoformat() if entry.valid_from else ""
            # Skip if pre-cursor
            if cursor is not None and isinstance(cursor, str) and ts <= cursor:
                continue
            pending.setdefault(ent_ref, []).append(
                EntryRef(
                    id=entry.id,
                    timestamp=ts,
                    text=entry.body,
                    entities=entry.entities,
                )
            )
    
    return pending
```

**Tests** (en `tests/cli/test_memory_cmd.py`):
- `test_dream_no_pending_returns_clean`: empty workspace → "No pending".
- `test_dream_consolidates_pending_entity`: seed entries with tags →
  command runs → page created.
- `test_dream_dry_run_does_not_write`: --dry-run → no files written.
- `test_dream_filters_by_entity`: --entity person:marcelo → only that
  one consolidated.

**Breaking changes**: ninguno (comando nuevo).

**Live verification** (final del Cluster D, también es validation
end-to-end de Cluster C):
- Setup: `durin agent --message "Marcelo prefiere pytest"` 3-4 veces
  con variantes.
- Correr `durin memory dream`.
- Inspeccionar `memory/entities/person/marcelo.md` resultante.
- Correr `durin memory expand person:marcelo`.

**Costo estimado**: 2-3 hours (incluye `_discover_pending_consolidations`).

---

## §5 — Live verification matrix por cluster

| Cluster | Live test concreto | Pass criteria |
|---|---|---|
| A | `durin agent` real + ver que llama memory_store con format correcto | ≥80% calls válidos |
| B | (no live; tests + integration suficiente) | tests pasan |
| C | escribir contenido similar 2 veces; verificar warning. correr dream con >50 entries; verificar cap | warning emitido, log de cap |
| D | flujo end-to-end: agent → memory_store ×N → memory dream → memory expand | page generada, expand surface sources |

---

## §6 — Riesgos identificados + mitigación

| Riesgo | Probabilidad | Mitigación |
|---|---|---|
| RRF k=60 produce ordering peor que score-multiplier para corpus chico | Media | Tests integration validan. Si peor, refinar k o split queries vector-only vs hybrid |
| Dedup threshold 0.1 demasiado estricto → falsos positivos | Media | Calibrar conservador inicialmente. Hacer configurable. Telemetría futura |
| Cap entries=50 trunca info importante en dream | Baja | "Take newest" preserva info reciente. Iteración con prompt si mejora |
| Retry 3x infla costo LLM | Baja-Media | Cap explícito. Logging para detectar si pasa frecuente |
| Drop save/load rompe imports externos | Muy baja | Grep confirma solo el proyecto importa |
| Tool description corta → modelo aún se equivoca | Media | Iterar prompt si telemetría real lo muestra |

---

## §7 — Orden de ejecución concreto

```
1. Cluster A  ← bajo riesgo, alta confianza
   - A.1 T1.1 tool description shrink
   - A.2 T1.4 alias_index drop save/load
   - run tests/ → commit + push
   - live verify A.1
   
2. Cluster B  ← refactor algoritmico
   - B.1 T1.3 RRF replacement
   - run tests/memory/test_entity_ranker.py
   - run tests/integration/
   - commit + push
   
3. Cluster C  ← write path crítico (riesgo alto)
   - C.1 T1.7 vector similarity dedup
   - C.2 T1.6 dream Pydantic + retry + budget
   - run tests/memory/test_dream.py + test_memory_store.py
   - live verify C.1 (dup write) + C.2 (dream con muchos entries)
   - commit + push
   
4. Cluster D  ← additive (CLI)
   - D.1 T1.5 durin memory dream
   - tests/cli/test_memory_cmd.py
   - live verify end-to-end: agent → store ×N → dream → expand
   - commit + push
```

Cada cluster es un commit (o pareja de commits si conviene).

---

## §8 — Pre-implementación: glm peer review checklist

Antes de empezar Cluster A, pedirle a glm que revise este plan con
contexto pleno y mire por:

1. **Correctness algoritmico del RRF** (B.1): ¿el k=60 es razonable
   para corpus chico? ¿La construcción de entity_rank_list es la
   correcta? ¿Falta algún edge case?
2. **Coherencia del dedup threshold** (C.1): ¿0.1 LanceDB distance es
   razonable para "cosine ~0.90 sim"? ¿O hay error de cálculo?
3. **Retry logic** (C.2): ¿pasarle el error al modelo en el siguiente
   intento es correcto, o lo distrae? ¿Tres intentos es overkill?
4. **Context budget** (C.2): "take newest 50" — ¿es la heurística
   correcta? ¿O debería priorizar por importance?
5. **Discover pending logic** (D.1): ¿la comparación `ts <= cursor` es
   correcta cuando ts/cursor son ISO strings de fechas distintas
   precisión?
6. **Orden de clusters**: ¿el orden A→B→C→D es defendible, o glm vería
   un orden mejor?
7. **¿Algo importante que omití?**

---

## §9 — glm peer review findings + fixes aplicados

glm-5.1 revisó el plan completo con doc 21+22+23 + código actual.
Findings con fixes:

### Bloqueantes (corregidos antes de implementar)

**G1 — Error de cálculo L2² → cosine (afecta C.1)**

Mi plan decía: `_DEDUP_DISTANCE_THRESHOLD = 0.10  # cosine ~0.90`.

**Realidad**: LanceDB con métrica L2 devuelve `L2² = 2(1 - cos)` para
vectores unitarios. Entonces:

| L2² | cosine real |
|---|---|
| 0.05 | 0.975 |
| 0.10 | **0.95** ← matchea OpenClaw |
| 0.20 | 0.90 |

**Fix aplicado**: threshold 0.10 sigue razonable (cosine 0.95) pero
documentar fórmula y verificar que fastembed produce vectores unitarios
antes de calibrar. Add:

```python
# LanceDB L2 distance: for unit-normalized vectors,
# L2² = 2(1 - cosine_similarity).
# Therefore distance ≈ 0.10 ↔ cosine ≈ 0.95.
# Source: doc 23 §9 G1 (peer review).
# Fastembed paraphrase-multilingual-MiniLM-L12-v2 produces unit vectors
# (validated experimentally; see scripts/test_embedding_name_variations.py).
_DEDUP_DISTANCE_THRESHOLD = 0.10  # ≈ cosine 0.95
```

**G2 — Cursor drift en dream batch (afecta C.2)**

Mi plan: si `entries=80` pendientes y `MAX_ENTRIES_PER_CALL=50`, paso 50
al LLM. Si el LLM setea `Cursor-after:` al timestamp de la última entry
del batch (entry 50), las entries 51-80 se vuelven pre-cursor en el
próximo discover → se pierden silenciosamente.

**Fix aplicado**: tras parseo exitoso del LLM response,
**forzar programáticamente** el `dream_processed_through` al timestamp
de la última entry del **batch pasado al LLM** (no del corpus total).
Esto es un invariante de seguridad, no sugerencia al modelo.

```python
def apply(self, entity_ref: str, result: ConsolidationResult,
          *, batch_last_ts: str | None = None) -> str | None:
    # ... write page logic ...
    
    # SAFETY INVARIANT (G2): force cursor to last-entry-of-batch,
    # ignoring whatever the LLM put in Cursor-after trailer. This
    # prevents silent data loss when N entries > MAX_ENTRIES_PER_CALL.
    if batch_last_ts is not None:
        page = EntityPage.from_text(result.page_text)
        if page is not None and page.dream_processed_through != batch_last_ts:
            page.dream_processed_through = batch_last_ts
            result.page_text = page.to_markdown()
    
    # ... rest ...
```

Y `consolidate_entity` pasa `batch_last_ts = entries[-1].timestamp` cuando
hizo cap.

### Serios (corregidos)

**G3 — ISO string comparison rota (afecta D.1)**

Mi plan: `if cursor is not None and isinstance(cursor, str) and ts <= cursor`.

**Bug**: `"2024-01-15T10:30:00" <= "2024-01-15"` → False
(`T` > "" lexicográficamente). Entry no se filtra; se procesa de nuevo.

**Fix aplicado**: parsear ambos a datetime, comparar numéricamente.

```python
from datetime import datetime

def _is_pre_cursor(entry_ts: str, cursor: Any) -> bool:
    if not entry_ts or cursor is None:
        return False
    try:
        et = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
        if isinstance(cursor, (int, float)):
            return False  # msg_idx cursor — not comparable to ts
        ct = datetime.fromisoformat(str(cursor).replace("Z", "+00:00"))
        return et <= ct
    except (ValueError, TypeError):
        return False  # malformed → don't filter
```

Reemplaza la lógica inline en `entity_ranker.rank_with_entities` y
`memory_cmd._discover_pending_consolidations`.

**G4 — `doc_id` fallback frágil (afecta B.1)**

Mi plan: `return c.get("id", id(c))` como fallback.

**Bug**: 2 candidatos sin id colapsan en mismas key del scores dict, sus
scores se suman incorrectamente.

**Fix aplicado**: requerir `id` obligatorio, fail fast.

```python
def _require_id(c: dict) -> str:
    did = c.get("id")
    if not did or not isinstance(did, str):
        raise ValueError(f"candidate missing required 'id': {c.keys()}")
    return did
```

**G5 — Double embedding cost en dedup (afecta C.1)**

Mi plan: `vi.search(content)` luego `vi.upsert(entry)`. Cada uno hace
embedding del content. **Doble cómputo por write**.

**Fix aplicado**: cachear embedding.

```python
async def execute(self, **kwargs):
    # ... validation ...
    
    vi = self._get_vector_index()
    cached_vec = None
    if vi is not None:
        try:
            # Compute embedding once
            cached_vec = vi._provider.embed([content])[0]
            # Search by vector (not text) to reuse
            hits = vi._search_by_vector(cached_vec, top_k=1)
            if hits and hits[0].get("_distance", 1.0) < self._DEDUP_DISTANCE_THRESHOLD:
                return {"warning": "near-duplicate", ...}
        except Exception as exc:
            logger.warning(...)
            cached_vec = None  # fallback to recompute on upsert
    
    # ... write path; pass cached_vec to upsert if available ...
```

Requiere agregar `VectorIndex._search_by_vector(vec, top_k)` y
`VectorIndex.upsert_with_vector(entry, ..., precomputed_vector)` (o
similar interfaz). Refactor menor.

**G6 — UX contradicción en C.1 (decidir bloquear vs warn)**

Texto del plan decía "no bloquear" pero código retornaba early sin
write. **Inconsistente.**

**Fix aplicado — decisión explícita**: **bloquear por default; ofrecer
override**. Razón: para single-user CLI, double-write silencioso es
peor que un warning explícito que el modelo puede manejar.

```python
async def execute(self, **kwargs):
    # ... dedup check ...
    if hits and hits[0]["_distance"] < threshold:
        return {
            "warning": "near-duplicate",
            "nearest_id": near["id"],
            "nearest_distance": near["_distance"],
            "hint": "Identical content already stored. Pass force=true to override.",
        }
    
    # If kwargs.get("force") is True, skip dedup entirely
    force = bool(kwargs.get("force", False))
    if force:
        logger.info("memory_store: force=true skipping dedup")
```

Agregar `force` al schema del tool con default False.

**G7 — Quality metric omitido en dream**

Mi plan validaba estructura pero no contenido. Una consolidación que
borra todos los facts pasa validación.

**Fix aplicado**: sanity check de longitud relativa.

```python
# In _parse_response, after EntityPage.from_text validates:
if current_page is not None:
    old_body_len = len((current_page_parsed.body or "").strip())
    new_body_len = len((parsed_page.body or "").strip())
    if old_body_len > 200 and new_body_len < old_body_len * 0.5:
        raise DreamError(
            f"consolidated page body shrank from {old_body_len} to "
            f"{new_body_len} chars (>50% loss). Refusing to commit "
            "(possible LLM hallucination/info-loss)."
        )
```

**G8 — `_INLINE_TEMPLATE_FALLBACK` falta `===END===` marker**

Mi código actual en `dream.py` tiene `_INLINE_TEMPLATE_FALLBACK` que
NO incluye instrucciones de cerrar con `===END===`. Pero el regex
`_SECTION_PAGE` lo espera (`(?:\n===END===|\Z)`). El regex hace `\Z`
fallback así que técnicamente funciona, pero el output queda menos
predecible.

**Fix aplicado**: agregar `===END===` literal a `_INLINE_TEMPLATE_FALLBACK`
para alinear con el template canónico de `consolidator.md`.

### Menores (anotados, no bloquean)

**G9 — RRF list-length normalization**

Vector list es typically 50-100 items; entity list 1-5 items. RRF
acumula `1/(rank+60)` para cada — entity contribuye proporcionalmente
poco. **Es deliberado** (entity es "nudge" no "override"), pero
**documentar explícitamente** en docstring de `rank_with_entities`.

**G10 — Retry con temperature constante repite errores**

Con `temperature=0.1`, mismo prompt + mismo modelo → output muy similar
3 veces. **Fix recomendado** (no bloqueante): bumpear temperature
gradualmente.

```python
for attempt in range(self.MAX_RETRIES):
    temp = 0.1 + attempt * 0.15  # 0.1, 0.25, 0.4
    raw = self._llm_invoke(prompt, model=self.model, temperature=temp)
```

Esto requiere agregar `temperature` al `LLMInvoke` protocol o pasarlo
opcionalmente. Lo dejamos como nice-to-have para v1; observamos en
telemetría si retries fallan repetidamente.

**G11 — MAX_ENTRIES_PER_CALL=50 justificación**

Estimación: 50 entries × ~100 tokens (texto comprimido) = ~5000 tokens
de input. Con prompt overhead (~2000 tokens) + página actual (~1500
tokens) = ~8500 tokens input. Bien dentro del context window de 32K+
de la mayoría de modelos. Documentar en docstring.

**G12 — Concurrencia race en dedup**

Single-agent single-thread: irrelevante. Documentar en docstring que
parallel tool calls del modelo podrían producir doble write. Phase 2+
si emerge problema.

**G13 — Páginas no se ordenan entre sí en pages_for_query**

Si query menciona 3 entidades, las 3 pages reciben mismo rank inicial.
Documentar en docstring de `rank_with_entities`.

**G14 — Tests de dream pueden llamar idx.save()**

Tras eliminar save/load (A.2), verificar que ningún test fixture queda
referencing esos métodos. Grep + cleanup en CI.

### Acciones consolidadas

Antes de Cluster A (mecánico):
- ✓ Doc 23 actualizado con findings G1-G14
- ✓ Calibración threshold C.1 documentada (G1)
- ✓ Cursor force-set diseñado para C.2 (G2)

Durante Cluster B (RRF):
- Implementar `_require_id` (G4)
- Documentar list-length normalization (G9)

Durante Cluster C (write path):
- Implementar cached embedding (G5)
- Implementar force=True override (G6)
- Implementar sanity check body shrink (G7)
- Fix `_INLINE_TEMPLATE_FALLBACK` (G8)
- Implementar cursor force-set (G2)
- Justificar MAX_ENTRIES_PER_CALL con números (G11)

Durante Cluster D (CLI):
- Usar datetime parse para cursor compare (G3)

Optional:
- Bumpear temperature en retries (G10)

---

## Last updated: 2026-05-23 (post glm peer review with fixes)
