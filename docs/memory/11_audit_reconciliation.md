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

**Lección sobre sync tests**: `test_tool_description_sync.py` valida igualdad de strings, no comportamiento. Pasó verde con el doc mintiéndole al LLM por una semana. Fix general para tests de "sync" en futuro: ejercitar el comportamiento, no sólo comparar strings.

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

**Estado**: pending

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

**Estado**: pending

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

**Estado**: pending

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

**Estado**: pending

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

**Estado**: pending

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

**Estado**: pending

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

**Estado**: pending

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

**Estado**: pending

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

**Estado**: pending

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

**Estado**: pending

---

## MEDIUM — drift sin romper UX directo

### B1 — `.description` property de los tools no está sincronizada con la canónica

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

**Estado**: pending

---

### B2 — Doc 99_phase_progress_review obsoleto

**Doc dice** (`docs/memory/99_phase_progress_review.md:5`): "4885 tests pasando".

**Doc dice** (§2 D4): "Phase 1.9 deferido (integración v2 pipeline en DreamConsolidator)... Próximo siguiente paso: Phase 1.9".

**Código dice**:
- `git log --oneline`: commit `6aafc3f` shipped Phase 1.9 (DreamConsolidator usa parse_dream_output + apply_dream_output).
- Test count actual (último commit `2e7097a` body): 4968 passing.

**Quién tiene razón**: código (commits dicen la verdad). Doc desactualizado.

**Acción**: actualizar `99_phase_progress_review.md` — marcar D4 resuelto, actualizar test count, mover §4 recomendaciones a estado "DONE".

**Estado**: pending

---

### B3 — Doc 10 marca como pending lo que está hecho

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

### B4 — P5.5 implementado distinto al spec

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

### B5 — Retention: 1 año en doc vs 90 días en código

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

### B6 — Doc 03 §17 status table contradice §11 sobre MMR

**Doc dice** §11: "MMR — Removed from MVP".
**Doc dice** §17 status table: "MMR | Not implemented | New step, default enabled".

**Código dice**: `grep -rn "mmr\|MMR" durin/memory/` → cero hits en código de producción.

**Quién tiene razón**: §11 (removed). §17 quedó stale al actualizar §11.

**Acción**: corregir §17 — fila MMR debe decir "Removed from MVP".

**Estado**: pending

---

### B7 — Doc 05 §15 + doc 06 §10 status: "v1 page rewrites"

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

### B8 — Doc 03 §15 promete config keys que no existen

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

### B9 — Eventos documentados que nunca se emiten

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

**Estado**: pending

---

### B10 — Eventos emitidos no documentados

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

### B11 — Doc 06 §2 sólo menciona `## Memory` (incompleta)

**Doc dice** (`docs/memory/06_prompts_and_instructions.md` §2): reproduce sólo el bloque `## Memory` del identity.md.

**Código dice** (`durin/templates/agent/identity.md:35-46`): además del `## Memory`, existe `## Memory writing` que da guidance para escrituras (dedup, cuándo NO llamar memory_store).

**Quién tiene razón**: código (tiene contenido útil que el doc oculta).

**Acción**: actualizar doc 06 §2 para reproducir AMBAS secciones verbatim.

**Estado**: pending

---

### B12 — Cross-encoder model NO validado contra lista curada

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

**Estado**: pending

---

## LOW — cosmético / docs

### C1 — Doc 01 §4.3 referencia `STATEFUL_ATTRIBUTE_PATTERNS` que no existe

**Doc dice** (`docs/memory/01_data_and_entities.md` §4.3): *"The pattern set lives in code as a single source of truth (`STATEFUL_ATTRIBUTE_PATTERNS`)"*.

**Código dice**: `grep -rn "STATEFUL_ATTRIBUTE_PATTERNS" durin/` → cero hits.

**Quién tiene razón**: doc miente. La constante no existe. La lógica de "stateful attribute" probablemente está implícita en `entity_page.py::_validate`.

**Acción**: o crear la constante (extraer del código actual), o quitar la referencia del doc.

**Estado**: pending

---

### C2 — Doc 01 §4.4 "soft cap 50 / hard cap 200" entries-per-entity sin enforcement

**Doc dice** (`docs/memory/01_data_and_entities.md` §4.4): *"Per-entity cap — Soft cap = 50 (warn only), Hard cap = 200"*.

**Código dice**: `grep -rn "50\|200" durin/memory/dream.py durin/memory/entity_page.py | grep -iE "cap|limit"` → cero hits semánticamente relevantes.

**Quién tiene razón**: doc propone, código no enforca.

**Acción**: implementar el cap o sacar del doc. Recomendación: implementar el soft-cap (log warning cuando una entity tiene > 50 entries en su body). El hard cap es defensivo — defer hasta que ocurra.

**Estado**: pending

---

### C3 — Doc 01 §4.5 step 2 describe pinyin-with-tones, código usa unidecode directo

**Doc dice**: *"Transliterate non-Latin scripts to Latin (e.g., 马塞洛 → mǎsàiluò → masailuo)"*.

**Código dice** (`durin/memory/entities.py:153`): `unidecode(nfc)` directo. Para "马塞洛", `unidecode` produce `"Ma Sai Luo "` → `ma_sai_luo`.

**Quién tiene razón**: código (más simple y correcto). El intermedio pinyin-with-tones es ficción.

**Acción**: actualizar doc 01 §4.5 step 2: *"Transliterate non-Latin scripts to ASCII via unidecode (e.g., 马塞洛 → Ma Sai Luo → ma_sai_luo)"*.

**Estado**: pending

---

### C4 — Doc 05 §14 dice 5 triggers, §2 enumera 6

**Doc dice** §14 row 1: "Five trigger types".
**Doc dice** §2: 6 triggers (`threshold`, `post_ingest_threshold`, `cron_daily`, `session_close`, `post_compaction`, `manual`).

**Código dice** — 6 triggers efectivamente cableados (verificado vía grep en commit `c3eff1e`).

**Quién tiene razón**: §2 + código.

**Acción**: corregir §14 a "Six trigger types".

**Estado**: pending

---

### C5 — Doc 05 §8.7 menciona verdict `unsure`; código usa `unclear`

**Doc dice** §8.7: *"flag uncertainty as `unsure` rather than confirm"*.
**Código dice** (`durin/memory/absorb_judge.py:73`): verdicts = `{"same", "different", "unclear"}`.

§8.4 del mismo doc 05 dice `unclear` correctamente.

**Quién tiene razón**: §8.4 + código.

**Acción**: corregir §8.7 a `unclear`.

**Estado**: pending

---

### C6 — Doc 07 §15 sub-totales obsoletos

**Doc dice** §15: "12 events in schema.py".
**Código dice** `durin/telemetry/schema.py:911-937` — 25 entradas memory.*.

**Doc dice** §15: "query truncation: Not enforced".
**Código dice** (`durin/agent/tools/_telemetry.py:29-33`) — sí enforzado vía `_truncate_freetext`.

**Quién tiene razón**: código (recuento actual).

**Acción**: actualizar §15 con counts y status reales.

**Estado**: pending

---

### C7 — Doc 02 §11 status table es stale completo

**Doc dice** §11 (status table): "FTS5 lexical index — Does not exist"; "File watcher — Manual rebuild only"; "Archive folder — Doesn't exist".

**Código dice**:
- `durin/memory/fts_index.py` existe + indexer usa.
- `MemoryFileWatcher` existe (aunque no cableado, ver A11).
- `archive/` walker existe (`durin/memory/archive.py`).

**Quién tiene razón**: código. Doc 02 §11 entera está obsoleta.

**Acción**: rehacer §11 desde cero reflejando estado actual.

**Estado**: pending

---

### C8 — Doc 03 §1 diagram tiene dos "Step 7" (header collision)

**Doc dice**: §11 "Step 7 — Removed (MMR deferred)"; §12 también titulada "STEP 7".

**Acción**: renumerar.

**Estado**: pending

---

### C9 — Doc 06 §3.5 menciona `memory_*.py::DESCRIPTION` constants que no existen

**Doc dice** §3.5: *"descriptions must match `memory_*.py::DESCRIPTION` constants"*.
**Código dice**: no hay `DESCRIPTION` constant en ningún tool. La canónica vive en `_PARAMETERS["description"]`.

**Quién tiene razón**: código.

**Acción**: corregir §3.5: *"matches `_PARAMETERS['description']` field"*.

**Estado**: pending

---

### C10 — Doc 04 §7.1 menciona webui surfaces — verificar

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

| Bloque | Items | Naturaleza |
|---|---|---|
| Critical (A1-A11) | 11 | Afectan UX agente, operación, o medibilidad |
| Medium (B1-B12) | 12 | Drift sin romper UX directo |
| Low (C1-C10) | 10 | Cosmético / docs |
| No accionable (D1-D3) | 3 | OK como están |

**Total**: 36 items.

**Orden de resolución sugerido**: A1 → A2 → A3 (los tres tools — UX agente) → A11 (wiring watcher+cron — operación) → A9 (decay) → A10 (session summaries) → A8 (push wiring) → A5+A6+A7 (telemetría payload) → A4 (LanceDB schema doc) → resto en orden.

**Mantenimiento**: a medida que se resuelven items, marcar **resolved** + breve nota de la decisión + commit hash. No borrar items resueltos — sirven como decisions log.
