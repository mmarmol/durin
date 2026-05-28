# Phase 0 → Phase 7 progress + decisions for review

**Autoría:** Claude (sesión autónoma 2026-05-28).
**Branch:** `memory/phase-0-foundations`.
**Estado:** 4885 tests pasando, 16 skipped, 0 fallando. Webui build limpio.

Este documento es para que revises mañana. Lista de:
1. Qué se completó.
2. Decisiones tomadas sin consultar (con justificación).
3. Trabajo deferido (con motivo + scope estimado).
4. Próximo siguiente paso recomendado.

---

## 1. Fases completas

### Phase 0 — Foundations (100%)
- `walk_memory` / `walk_class` walker centralizado, todos los callers migrados.
- `archive_episodic` / `archive_entity` helpers, layout top-level `memory/archive/<class>/`.
- `slugify_name` + `resolve_slug_collision` (NFC + unidecode + truncate 64).
- `EntityPage` v2: `attributes`, `relations`, `provenance` como campos tipados.
- `MemoryEntry`: `decay_half_life`, `evergreen` + `decay.py` con `half_life_for` y `CLASS_HALF_LIFE_DEFAULTS`.
- `index_meta.py`: `<workspace>/.durin/index/meta.json` con `schema_version=2` + `embedding_model_id` + atomic save.

### Phase 1 — Dream v2 (módulos d1-d12 todos shippeados como unidades aisladas)
- `consolidator.md` reescrito al spec v2 + 6 examples (`01_new_entity` … `06_no_changes`).
- `rules.md`, `commit_format.md`, `json_patch_reference.md`.
- `dream_patch_parser.py` — split `===PATCH=== / ===BODY_DELTA=== / ===COMMIT=== / ===END===` con `json_repair`.
- `dream_prompt_builder.py` — assembler del paquete con substitución de slots + truncation a 100 URIs.
- `dream_apply.py` — apply pipeline con jsonpatch, `.md.bak` rollback, telemetría
  `memory.dream.patch_applied` / `memory.dream.entity_failed`.
- `dream_archive_consumed.py` — mueve los episódicos consumidos a archive + delete LanceDB row.
- `dream_quarantine.py` — counter de fallos estructurales + cuarentena de 7 días.
- `dream_commit_message.py` — finaliza el commit con trailers canónicos.
- `dream_git_history.py` — formatter para el slot `{recent_history}`.
- `user_authored` filter en `_discover_pending_consolidations` + 5 test files actualizados con `_agent_created_scope` autouse.
- `absorb_judge` template + parser locked vía test contract.
- `onboard_memory.prompt_enable_auto_absorb` con texto Q6.3 verbatim del spec.

### Phase 1.5 — Hot layer (100% via worktree agent paralelo)
- `hot_layer.py` budgets / markers / cursor logic verificado contra spec.
- v2 rendering: `attributes` y `relations` como prose dentro del bloque CANONICAL.
- Per-section failure handling + `memory.hot_layer.failure` telemetría.

### Phase 2 — Indexing v2 (core, falta watcher + cron + tool hooks)
- `fts_index.py` — sqlite FTS5 con dual table (`memory_fts` unicode61 + `memory_fts_trigram`) + `fts_meta` bookkeeping.
- `indexer.py` — `rebuild_fts_index` (bulk) + `reindex_one_file` (incremental) + `detect_index_staleness` (drift).
- `durin memory reindex [--target fts|lancedb|all]` CLI.
- 3 telemetría: `memory.index.write`, `memory.index.rebuild`, `memory.index.staleness_detected`.

### Phase 3 — Search pipeline v2 (core, falta entity-aware rerank wiring + cross-encoder)
- `query_router.py` — CJK detection + NFC + routing a `UNICODE61` / `TRIGRAM` / `LIKE_SUBSTRING`.
- `rrf_fusion.py` — RRF k=60 cross-source + dynamic boost (`w_lexical = 2.5` cuando `keywords_provided`).
- `lexical_search.py` — ejecutor que conecta router → FTS index con quoting + emit.
- `sectioned_output.py` — render con markers `=== CANONICAL / FRAGMENT / SESSION / INGESTED ===` + per-source cap (default 3 corpus chunks/ingest_id).
- `search_pipeline.py` — orquestador end-to-end con graceful degradation.
- 2 telemetría: `memory.recall.lexical`, `memory.recall.rrf`.

### Phase 5 — Tools v2 (parcial: solo `keywords` param)
- `memory_search` ahora acepta `keywords` (descripción match doc 04 §2.3 + doc 06 §3.1).

### Phase 6 — Prompts v2 (parcial: identity.md)
- `identity.md::Memory` section migrado al texto v2 verbatim de doc 06 §2.
- `tests/memory/test_identity_memory_section.py` locks spec-anchor phrases against drift.

### Phase 7 — Telemetry v2 (parcial: privacy truncation)
- `emit_tool_event` trunca free-text fields (`query` / `text` / `snippet` / `content` / `needle`) a 200 chars per doc 07 §13.

---

## 2. Decisiones tomadas sin consultar

> Si crees que alguna está mal, dimelo y la revierto.

### D1 — absorb_judge vocabulary (Phase 1 d11)
**Conflicto:** doc 06 §5 spec decía `merge | keep_separate | unsure`. El código (`absorb_judge.py:_VALID_VERDICTS`) usa `same | different | unclear`. La template `absorb_judge.md` también usa `same/different/unclear`.

**Decisión:** Actualicé doc 06 §5 + doc 05 §8.4-8.6 para que coincidan con el código.

**Justificación:** El vocabulario `same/different/unclear` es **identity-judgement**, no action-prescription. Separación más limpia: LLM juzga identidad; runner mapea verdict+confidence a la acción (merge / log / defer). Cambiar el código + la template + retrain del LLM era el costo mayor, vs editar el doc que ya advertía "current implementation is solid; this doc doesn't redefine it".

**Test que lockea:** `tests/memory/test_absorb_judge_template_contract.py`.

### D2 — `user_authored` filter rompió 23 tests existentes (Phase 1 d9)
**Causa:** El filter (correcto, per spec doc 01 §4.6.1) hace skip de entradas con `author=user_authored`. El default de `MemoryEntry.author` es `user_authored` (apropiado para edits manuales humanos). Tests existentes creaban entries con el default y esperaban que Dream las viera.

**Decisión:** Añadí autouse fixture `_agent_created_scope` en 6 test modules (test_dream_runner, test_dream_triggers_beta2, test_threshold_trigger, test_threshold_trigger_e2e, test_t1_wiring_e2e, test_memory_cmd) que envuelve los test bodies en `author_scope("agent_created")`.

**Justificación:** En producción, los tool calls del agente corren bajo `author_scope("agent_created")`. Los tests modelaban observaciones del agente; el wrap es semánticamente correcto. La alternativa (cambiar el default de `MemoryEntry.author` a `agent_created`) habría roto la protección que el spec quiere.

### D3 — Phase 1.5 lanzado en worktree aislado
Inicialmente lancé el agente Phase 1.5 en el workspace principal. Luego identifiqué que ambos tocaríamos `durin/telemetry/schema.py` y lo maté + relancé con `isolation: "worktree"`. El branch (`memory-phase-1.5-hot-layer`) fue mergeado limpiamente, worktree eliminado, branch borrado.

### D4 — Phase 1.9 deferido (integración v2 pipeline en DreamConsolidator)
**Estado actual:** Todos los módulos v2 (parser, builder, applier, archive, quarantine, commit, git_history) están construidos y testeados en aislamiento. El `consolidate_entity` viejo todavía usa el parser `===PAGE===` legacy y el flujo full-page-rewrite.

**Decisión:** No hice la integración wholesale.

**Motivo:** El refactor toca `DreamConsolidator.consolidate_entity` + `apply` + migración de ~12 test stubs que retornan formato `===PAGE===`. Estimo 200-400 LOC + migración cuidadosa de tests. Lo dejé como **Phase 1.9** para que tú decidas si lo hacemos juntos en una sesión enfocada o por chunks.

**Riesgo:** Mientras esto no esté wired, el `consolidator.md` v2 se le pasa al LLM PERO el response se intenta parsear como v1 → todos los Dream runs reales fallarían (el stub LLM en tests retorna v1 format, así que la suite no captura este bug). **Recomiendo no correr Dream pass real hasta que esté wired**.

### D5 — Scope de Phase 2/3 deliberadamente acotado
Phase 2 watchdog file watcher, health-check cron, tool re-index hooks, y schema-version startup check NO están implementados. Phase 3 entity-aware rerank no está wired downstream del orquestador.

**Motivo:** Tiempo + contexto + el camino crítico (search pipeline working) ya está cubierto por los core modules. Los items deferidos son operacionales / opt-in.

### D6 — `keywords` param añadido pero no aún wired al search path
El parameter `keywords` está expuesto en `memory_search`, parseado en `execute`, pero **NO se pasa al lexical search** (porque el lexical search del memory_search actual va por grep + LanceDB, no por `run_search_pipeline`). Cuando se migre el tool a `run_search_pipeline`, el thread completo conecta automáticamente vía `keywords_provided=True` en `fuse_rrf`.

---

## 3. Trabajo pendiente (con scope estimado)

### Phase 1.9 — Wire v2 pipeline en DreamConsolidator
- Refactor `consolidate_entity` para parsear con `parse_dream_output`.
- Refactor `apply` para usar `apply_dream_output` + `archive_consumed_episodic` + `record_failure`/`clear_failures` + `finalize_commit_message`.
- Migrar `_well_formed_response()` y ~12 dream tests a v2 format.
- Escala: ~200-400 LOC + careful test migration.

### Phase 2 — restantes
- File watcher (`watchdog`) — operational, manual edits son raros hoy. ~100 LOC.
- Health-check cron — operational, restaura índices corruptos cada 15min. ~150 LOC.
- Re-index-on-write hooks en `memory_store` / `memory_ingest` / Dream apply — necesario para que el FTS se mantenga sin correr `durin reindex`. ~30 LOC + tests.
- Schema-version mismatch check en startup. Tiene infraestructura (`index_meta.py`); falta hook. ~20 LOC.
- LanceDB body column extension (doc 02 §5.1) — opcional, para search level=cold sin disk reads. ~50 LOC.

### Phase 3 — restantes
- Entity-aware rerank wiring downstream del orquestador (el módulo `entity_ranker.py` existe; solo falta llamada). ~30 LOC.
- Intent router para patrones email/URL/ID. ~50 LOC.
- Grep fallback wired al orquestador. ~30 LOC.

### Phase 4 — Cross-encoder (todo)
- Module `cross_encoder.py` con lazy load.
- Integración en `search_pipeline.py` step 5.
- Config `memory.search.cross_encoder.*`.
- Webui dashboard panel.
- ~200-300 LOC.

### Phase 5 — Tools v2 restantes
- Wire `memory_search` to `run_search_pipeline` (replace old grep+vector path or coexist con flag).
- `memory_search` `recovered_from` + `recovery_duration_ms` fields.
- `memory_ingest` recursive character splitter.
- `memory_drill` remove `include_context` flag.
- Tool description audit script.
- ~150 LOC.

### Phase 6 — restantes
- 3 onboarding questions (memory enable, cross-encoder, aux model) — solo el Q6.3 (auto-absorb) está listo.
- Tool description constants per doc 06 §3.
- Audit script comparing strings in code vs doc 06.

### Phase 7 — restantes
- Retention (log rotation a 30 días + gzip).
- HTTPS push opt-in.
- 11 telemetría restantes per roadmap §10.1 (la mayoría ya emitidas; falta wire algunos sites).

### Phase 8 — Validation
- LoCoMo bench run con v2 pipeline.
- 200 hand-coded adversarial QAs (50 c/u en coder, sales, support, personal-assistant).
- Soak test 7 días.
- Documentation pass.

---

## 4. Próximo siguiente paso recomendado

**Phase 1.9 — Wire v2 pipeline en DreamConsolidator**. Es el cierre crítico de Phase 1 + bloquea Phase 8 validation (no podemos benchear el flow nuevo si el wiring no está). Estimado 1-2 sesiones de trabajo enfocado.

Después: **Phase 5 d1** (wire memory_search a `run_search_pipeline`) — desbloquea Phase 8 y empieza a entregar el FTS+RRF al agente real.

---

## 5. Estado del repo

```
Branch: memory/phase-0-foundations
Commits since main: ~15
Tests: 4885 passing, 16 skipped, 0 fail
Webui: builds clean
```

Last commits (newest first):

- `77e8cfc` feat(memory): memory_search exposes `keywords` parameter (Phase 5 d1 partial)
- `f2c?????` feat(memory): identity.md v2 + telemetry privacy truncation (Phase 6 + Phase 7)
- `f???????` feat(memory): Phase 3 orchestrator — search_pipeline ties FTS + RRF + sectioning
- `?` feat(memory): Phase 3 core — query router, RRF, sectioned output, lexical executor
- `?` feat(memory): FTS5 dual index + indexer + reindex CLI (Phase 2 core)
- `?` feat(memory): absorb_judge template lock + onboarding Q6.3 (Phase 1 d11+d12)
- `1ec05db` feat(memory): Dream v2 archive/quarantine/commit/telemetry (Phase 1 d5-d10)
- `?` feat(memory): Dream apply pipeline w/ jsonpatch + rollback (Phase 1 d4)
- `f64ec75` feat(memory): Dream v2 prompt package + parser + builder (Phase 1 d1-d3)
- `?` Phase 1.5 merge — hot layer v2 via worktree agent
- `b13fdd4` feat(memory): entity_page v2 schema + decay/evergreen + index meta.json
- `782fc47` refactor(memory): walker swap + archive rename + slug normalization
- `9a8bab0` refactor(memory): absorption uses top-level archive via archive_entity()
- `980e36c` feat(memory): archive helpers + bug tracker (Phase 0 deliverable 5)
- `be8fe01` feat(memory): shared workspace walker (Phase 0 deliverable 1)

Cuando vuelvas mañana, `git log --oneline memory/phase-0-foundations` te da el detalle.
