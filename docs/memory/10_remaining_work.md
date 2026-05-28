---
title: Remaining work — accionable post-Phase-1.9
version: 1.0
status: living document
last_updated: 2026-05-28
audience: humans and LLMs picking up the work
depends_on: 09_implementation_roadmap.md (spec); 99_phase_progress_review.md (decisions log)
---

# Remaining work (post-Phase-1.9)

Este documento es el **listado granular de lo que queda** después del estado actual (commit `c820447`). El plan original (`09_implementation_roadmap.md`) sigue siendo la referencia de **specs**, pero usa una granularidad de "deliverables" que ocultó scope y bugs latentes en la sesión autónoma. Este doc aplica el formato discutido en D5+D6:

| Campo | Significado |
|---|---|
| **TYPE** | 🟢 module · 🟡 refactor · 🔴 integration · 🟣 test-migration · 📄 docs |
| **DoD** | Observable, no "implementado" sino "X funciona end-to-end via Y" |
| **Refs** | Archivos + líneas o módulos concretos a tocar |
| **LOC** | Estimación honesta (no "small/medium/large") |
| **Risk** | Lo que puede romper / bugs latentes potenciales |

**Convención de orden por fase**: los items se listan en orden de **dependencia**, no de prioridad. Si X depende de Y, X viene después.

**Estado al cierre del segundo-pass audit (2026-05-28)**: ~5096 tests collected; tests/memory/ 1000 passed + 1 skipped (pre-existing). El sistema v2 anda end-to-end. Phase 4 (cross-encoder) shipped (P4.1-P4.4); Phase 8 (validation con LoCoMo bench) sigue pending. Audit E35/E36 (2026-05-28) actualizó este header — el conteo "4888 tests" y la frase "queda Phase 4 + Phase 8" se rezagaron a partir de los commits de la jornada audit.

> **Audit refresh 2026-05-28** (audit B3): la mayor parte de los items P2-P7 listados sin ✅ DONE abajo **se cerraron durante el día 2026-05-28** vía los commits del audit A1-A11. El estado vigente y razonado por item está en [`11_audit_reconciliation.md`](11_audit_reconciliation.md) (sección de cada A*). Resumen rápido por phase:
>
> - **Phase 2**: P2.2 ✅ (commit `c3eff1e`), P2.3 ✅ módulo + cableado en `989d33e` (A11), P2.4 ✅ módulo + cableado en `989d33e` (A11), **P2.5 reverted** en `7a835f8` (audit A4 — viola principio "filesystem es source of truth").
> - **Phase 3**: P3.3 ✅ commit `bc55686`.
> - **Phase 4**: P4.1/P4.2/P4.3 ✅ commit `b3c50c6`. P4.4 ✅ commit `11d9f96`.
> - **Phase 5**: P5.1 ✅, P5.2 ✅ commit `2e7097a`, P5.3 ✅ commit `572d5cf`, P5.4 ✅ era no-op (verificado en B4 audit), P5.5 ✅ pero **shipped como pytest sync test** en `tests/memory/test_tool_description_sync.py` en lugar del script `scripts/audit_tool_descriptions.py` que el plan original proponía — divergencia documentada en B4, mismo objetivo cumplido vía CI test, P5.6 ✅ commit `2e7097a`.
> - **Phase 6**: P6.1/P6.2/P6.3 ✅ commit `572d5cf`. P6.4 ya estaba ✅.
> - **Phase 7**: P7.1 ya estaba ✅. P7.2 ✅ commit `2e7097a`. P7.3 (PushSink) ✅ wired end-to-end en `b822b75` (A8) con secret store + config + tests.
> - **Phase 8**: pendiente (validación con bench LoCoMo etc.).
>
> Los items individuales abajo conservan su descripción para histórico; las secciones con ✅ DONE in-line están up-to-date. Cuando hay divergencia entre el plan original y lo que efectivamente se shippeó, el doc 11 documenta la razón.

---

## Phase 2 — Indexing v2 (4 items pendientes)

### P2.1 — Re-index-on-write hooks ✅ DONE (commit `1ea70ac`)
Documentado aquí para histórico. Hooks en `memory_store.execute`, `memory_ingest.execute`, `DreamConsolidator.apply`.

### P2.2 — Schema-version startup check ✅ DONE (commit `c3eff1e`)

- **TYPE**: 🔴 integration
- **DoD**: Cuando `<workspace>/.durin/index/meta.json::schema_version != CURRENT_SCHEMA_VERSION` o el archivo no existe, la próxima llamada a `MemorySearchTool.execute` (o equivalente) dispara `rebuild_fts_index` + `VectorIndex.rebuild_from_workspace` automáticamente y emite `memory.index.rebuild` con `reason="schema_mismatch"`.
- **Refs**:
  - Lectura: `durin/memory/index_meta.py::load_index_meta` (ya existe).
  - Comparación: `durin/memory/index_meta.py::CURRENT_SCHEMA_VERSION` (= 2).
  - Hook point: `durin/agent/tools/memory_search.py::MemorySearchTool.execute` línea ~256 (antes de llamar `run_search_pipeline`) — chequear meta + rebuild si stale.
  - Alternativa más limpia: hook en `MemorySearchTool.__init__` para hacerlo una vez por proceso.
- **LOC**: ~30 (helper `_ensure_index_fresh()` + call sites + 1 test).
- **Risk**: si el rebuild tarda (>10s para workspaces grandes), bloquea el primer `memory_search` post-update. Mitigación: emitir progress log + considerar lock para que dos procesos no rebuild-en a la vez.
- **Test**: setear `meta.json` con `schema_version=1`, llamar `memory_search`, assert rebuild corrió + meta actualizado.

### P2.3 — Watchdog file watcher ✅ DONE (module: `d9a4d8e`; wired in `AgentLoop` in `989d33e` / audit A11)

- **TYPE**: 🟢 module (~120 LOC)
- **DoD**: Modificar `memory/entities/person/marcelo.md` con vim y, dentro de 5 segundos, el siguiente `memory_search` para "marcelo" surface las palabras del edit. Adicionalmente: el commit en `memory/.git/` queda con `author: user`.
- **Refs**:
  - Nuevo módulo: `durin/memory/file_watcher.py`.
  - Dependency: `watchdog` (pip install). Añadir a `pyproject.toml`.
  - Watch path: `<workspace>/memory/` excluyendo `archive/` y `pending/`.
  - En cada evento mtime → `reindex_one_file(workspace, path)` + git commit con `author=user`.
  - Lifecycle: arrancar en `durin/agent/loop.py::AgentLoop.start` o en CLI `durin agent`.
- **LOC**: ~120 (watcher + lifecycle hook + 4 tests).
- **Risk**: watchdog en macOS usa FSEvents (built-in); en Linux usa inotify (fine); en Docker / network FS puede fallar — `watchdog` falla a polling automáticamente pero hay que verificar. Doc 02 §6.3 confirma esta mitigación.
- **Test**: `tmp_path` + tocar archivo + esperar evento + assert FTS index lo ve.

### P2.4 — Health-check cron ✅ DONE (module: `022d4b1`; scheduler + lifecycle wiring in `989d33e` / audit A11)

- **TYPE**: 🟢 module (~150 LOC, per spec §5.1)
- **DoD**: Cada 15 minutos (configurable), un job background:
  1. Llama `detect_index_staleness(workspace)` y para cada drift loguea + dispara `reindex_one_file`.
  2. Probe LanceDB connect (best-effort): si falla, log + queue rebuild.
  3. Emite `memory.health_check` event con `{components: {fts: "ok", lance: "degraded", ...}, drift_count: N}`.
  4. Después de 3 fallos consecutivos en el mismo componente en 1h, emite `memory.health.critical` y pausa el componente.
- **Refs**:
  - Nuevo: `durin/memory/health_check.py`.
  - Reusar `detect_index_staleness` (ya existe en `indexer.py`).
  - Scheduler: `croniter` (ya en deps) + un thread daemon o asyncio task.
  - Telemetry: 2 nuevos events en `durin/telemetry/schema.py` (`memory.health_check`, `memory.health.critical`).
  - Config: nuevo `memory.health_check.{enabled, interval_seconds}` en `durin/config/schema.py`.
- **LOC**: ~150 (module ~80 + 2 telemetry events ~30 + config ~10 + tests ~30).
- **Risk**: cron en proceso del agente vs cron separado. Doc 02 sugiere in-process (un thread). Eso significa que `durin agent` debe correr para que el health-check ocurra — apagar agente = no probe.
- **Test**: simular fallo de LanceDB con monkeypatch + verificar emit + verificar pause después de 3 fallos.

### P2.5 — LanceDB body column extension ❌ REVERTED (audit A4, commit `7a835f8`) — violated "filesystem is source of truth"

- **TYPE**: 🟡 refactor (~50 LOC)
- **DoD**: `level="cold"` queries devuelven el body sin tocar disco. `VectorIndex.rebuild_from_workspace` popula el nuevo column. Tabla existente se migra (rebuild forzado on schema_version bump — ver P2.2).
- **Refs**:
  - Schema en `durin/memory/vector_index.py::_record_for` y `_record_for_entity_page`.
  - Add column `body: str` al record dict.
  - En `memory_search._sectioned_to_result` para level=cold: leer `body` del vector_meta en vez de hacer `_enrich_body` que lee disco.
- **LOC**: ~50 (schema change + reads + test).
- **Risk**: tabla existente requiere rebuild (no se puede ALTER en LanceDB sin recrear). Mitigación: bump `CURRENT_SCHEMA_VERSION` y dejar que P2.2 lo dispare.
- **Test**: storar un entry + reindex + query level=cold + assert body presente sin haber leído el .md.

---

## Phase 3 — Search pipeline v2 (1 item pendiente)

### P3.1 — Entity-aware rerank wiring ✅ DONE (commit `1ea70ac`)
### P3.2 — Grep fallback wired ✅ DONE (commit `1ea70ac`)
### P3.3 — Intent router pattern detection ✅ DONE (commit `bc55686`)

- **TYPE**: 🟢 module (~80 LOC)
- **DoD**: Query "mmarmol@mxhero.com" (email pattern) → search_pipeline detecta el pattern + boost lexical weight a 2.5 incluso si el agente NO pasó `keywords`. Lo mismo para queries que parecen URLs (`https://...`), UUIDs, file paths.
- **Refs**:
  - Extender `durin/memory/query_router.py::decide_lexical_route` para devolver además `auto_keywords: str | None`.
  - Patterns: email regex, URL regex, UUID regex, file path regex (todos en una constante `_IDENTIFIER_PATTERNS`).
  - En `search_pipeline.run_search_pipeline`: si `auto_keywords` y `keywords is None`, treat como `keywords_provided=True`.
- **LOC**: ~80 (router extension + 6 tests para cada pattern + integration).
- **Risk**: false positives (e.g. "v1.2.3" matches version pattern but the user wants semantic). Mitigación: solo patterns con identificadores claros (email/URL/UUID/path); no version strings, no números sueltos.
- **Test**: query "find marcelo@mxhero.com" → routing decision tiene `auto_keywords="marcelo@mxhero.com"`. Query "marcelo en spain" → `auto_keywords=None`.

---

## Phase 4 — Cross-encoder opt-in (todo) — total ~250 LOC

### P4.1 — Cross-encoder runner module ✅ DONE (commit `b3c50c6`)

- **TYPE**: 🟢 module (~100 LOC)
- **DoD**: `CrossEncoderReranker(model_id).score(query, [doc_text, ...]) -> [scores]`. Lazy-loads model on first call. Batches inputs of N=32. Graceful degradation: model load failure → log + return None (caller skips).
- **Refs**:
  - Nuevo: `durin/memory/cross_encoder.py`.
  - Dependency: `sentence-transformers` o `FlagEmbedding` (verify). Optional dep via `durin[cross-encoder]` extra.
  - Default model: `jinaai/jina-reranker-v2-base-multilingual` per doc 03 §9.1.
- **LOC**: ~100 (module + lazy load + batching + 5 tests).
- **Risk**: model download ~1.1GB en primera invocación. Mitigación: progress log + considerar pre-download en onboarding (P6.1).
- **Test**: mock model with stub `score(query, docs) → [random]`; verify batching de 32; verify failure handling.

### P4.2 — Integration en search_pipeline step 5 ✅ DONE (commit `b3c50c6`)

- **TYPE**: 🔴 integration (~30 LOC)
- **DoD**: Cuando `memory.search.cross_encoder.enabled=true` y `pipeline_result.hits` no-empty, `run_search_pipeline` invoca el reranker con (query, [hit.snippet+body for hit in top 50]) y reordena. Hits ranked > 10 dropped. Emit `memory.recall.rerank`.
- **Refs**:
  - Hook point: `durin/memory/search_pipeline.py::run_search_pipeline` después de `_entity_aware_rerank`, antes de `apply_per_source_cap`.
  - Config: `memory.search.cross_encoder.{enabled, model, batch_size}` en `durin/config/schema.py`.
  - Telemetry: nuevo event `memory.recall.rerank` en schema.
- **LOC**: ~30 + 3 tests.
- **Risk**: latency. Mitigación: spec dice OFF por default; user explícito.
- **Test**: con cross_encoder enabled + stub model, verifica re-ranking aplicado.

### P4.3 — Onboarding question (doc 06 §6.2) ✅ DONE (commit `b3c50c6`)

- **TYPE**: 🟢 module (~30 LOC)
- **DoD**: `durin/cli/onboard_memory.py::prompt_enable_cross_encoder(current: bool) -> dict` con texto verbatim del doc 06 §6.2. Devuelve `{"enabled": bool, "model": str}`.
- **Refs**:
  - Patrón existente: `prompt_enable_auto_absorb` en `onboard_memory.py`.
- **LOC**: ~30 + 4 tests.

### P4.4 — Web dashboard memory settings panel ✅ DONE

- **TYPE**: 🟡 refactor webui (~250 LOC componente + ~50 i18n)
- **DoD**: Sección "Memory" agregada al nav de Settings con tres bloques observables:
  1. Cross-encoder toggle (`memory.search.cross_encoder.enabled`) + dropdown de 4 modelos curados.
  2. Number input para `memory.dream.threshold_entries` (commit on Enter o botón Save).
  3. Read-only summary de los defaults de `CLASS_HALF_LIFE_DEFAULTS` (no son configurables; viven en código).
- **Refs**:
  - Nuevo: `webui/src/components/settings/MemorySettings.tsx`.
  - Wiring nav: `webui/src/components/settings/SettingsView.tsx` (SETTINGS_NAV_ITEMS + render branch).
  - i18n: `webui/src/i18n/locales/en/common.json` (memory namespace completo); `es/common.json` (sólo nav.memory).
  - Backend: reusa `/api/config` y `/api/config/set` existentes en `durin/channels/websocket.py`.
- **Verificación**: `npx tsc --noEmit` pasa; `npx vitest run` 142 tests pasan; `npm run build` produce dist (vite build 1.89s).
- **Test manual pendiente**: `npm run dev` + abrir Settings → Memory, togglear cross-encoder, cambiar modelo, ajustar threshold, verificar que `~/.durin/config.json` cambia.
- **Risk asumido**: el dropdown del cross-encoder se desactiva cuando el toggle está OFF — UX deliberadamente conservador para evitar configurar un modelo que no se va a usar.

---

## Phase 5 — Tools v2 (4 items pendientes, ~180 LOC)

### P5.1 — `memory_search` keywords ✅ DONE (commit `c820447` / D6)
### P5.2 — `memory_search` `recovered_from` + `recovery_duration_ms` fields ✅ DONE (commit `2e7097a`)

- **TYPE**: 🟡 refactor (~40 LOC)
- **DoD**: Cuando `run_search_pipeline` activa un recovery (e.g. lance index recreado on the fly), el dict de respuesta del tool incluye `"recovered_from": ["lance"]` y `"recovery_duration_ms": <float>`. Cuando no hay recovery, los campos no aparecen.
- **Refs**:
  - `SearchPipelineResult` necesita carry `recovered_from: list[str]` y `recovery_duration_ms: float`.
  - `run_search_pipeline` los populariza basándose en si los safe wrappers tuvieron que fallar+recuperar.
  - `memory_search._sectioned_to_result` los pasa al dict final.
- **LOC**: ~40 + 2 tests.
- **Depende de**: nothing — puede ir antes o después de P2.4 (health-check).

### P5.3 — `memory_ingest` recursive character splitter ✅ DONE (commit `572d5cf`)

- **TYPE**: 🟡 refactor (~60 LOC)
- **DoD**: `memory_ingest` con un PDF de 50 páginas genera N chunks de ~1500 chars con ~200 chars de overlap, prefiriendo cortes en paragraph > line > sentence > word > char. Verificable via test: feed text de 10000 chars, verify chunk count + overlaps + boundary preference.
- **Refs**:
  - Reemplazar logic actual en `durin/agent/tools/memory_ingest.py` (línea ~150-200, sección `_chunk_content` o similar).
  - Patrón: LangChain RecursiveCharacterTextSplitter (referencia conceptual, NO copiar).
- **LOC**: ~60 (splitter ~40 + tests ~20).
- **Risk**: cambios al splitter pueden mover chunks existentes en producción → re-ingest necesario. Mitigación: solo aplica a nuevos ingests; los corpus existentes no se re-procesan automáticamente.
- **Test**: text de 10k chars con párrafos definidos; verify cuts en paragraph boundaries; verify overlap.

### P5.4 — `memory_drill` remove `include_context` flag ✅ DONE (no-op — verified the flag never existed; B4 audit)

- **TYPE**: 🟡 refactor (~10 LOC)
- **DoD**: La descripción del tool en doc 06 §3.4 no menciona `include_context`. El tool en `durin/agent/tools/memory_drill.py` lo tiene; eliminar del schema + execute signature.
- **Refs**:
  - `durin/agent/tools/memory_drill.py` — encontrar `include_context` en `_PARAMETERS` y `execute`.
- **LOC**: ~10 (delete code + update test mocks).
- **Risk**: tests que pasan `include_context` → fail. Update them.

### P5.5 — Tool description audit script ✅ DONE differently (B4 divergence: shipped as `tests/memory/test_tool_description_sync.py` instead of `scripts/audit_tool_descriptions.py` — same outcome via CI test, simpler integration)

- **TYPE**: 🟢 module + CI (~50 LOC)
- **DoD**: Script `scripts/audit_tool_descriptions.py` extrae las descripciones de los 4 tools de memoria + el bloque Memory de `identity.md`; compara contra los strings canónicos en doc 06 §3 y §2; falla con diff específico si difieren. Wired en CI.
- **Refs**:
  - Nuevo: `scripts/audit_tool_descriptions.py`.
  - Parsing: doc 06 markdown — extraer code blocks bajo `## 3.1`, `## 3.2`, etc.
  - Comparar con `<tool>._PARAMETERS.description` y `<tool>.description` properties.
  - Test que ejerce el script + CI step en `.github/workflows/`.
- **LOC**: ~50 (script + 1 test + 1 CI step).
- **Risk**: spec drift inevitable — la spec evoluciona. Mitigación: el script muestra DIFF preciso, el dev decide actualizar spec o código.

### P5.6 — Re-test los 3 skipped en commit `c820447` ✅ DONE (commit `2e7097a`)

- **TYPE**: 🟣 test-migration (~50 LOC)
- **DoD**: Los 3 tests skipped en `test_phase2_smoke.py` y `test_t1_wiring_e2e.py` (mencionados en commit message) se re-escriben contra la v2 surface: assertions sobre resultados (qué hits surge primero) en vez de detalles internos (qué función se llamó).
- **Refs**:
  - `tests/memory/test_phase2_smoke.py::test_recall_vector_telemetry_fires` — verificar payload v2 con `hit_count` real.
  - `tests/memory/test_phase2_smoke.py::test_vector_recall_does_not_regress_against_grep` — comparar v2 strategy contra grep-only.
  - `tests/memory/test_t1_wiring_e2e.py::test_e2e1_memory_search_invokes_entity_aware_ranker` — verify telemetry `ranking="entity_aware"` post-search.
- **LOC**: ~50 (3 tests rewritten).
- **Risk**: requires running fastembed+lancedb locally (no en CI). Mitigación: marca explícita `@pytest.mark.local_only`.

---

## Phase 6 — Prompts v2 (4 items pendientes, ~100 LOC)

### P6.1 — Onboarding wizard: memory subsystem enable ✅ DONE (commit `572d5cf`)

- **TYPE**: 🟢 module (~30 LOC)
- **DoD**: `prompt_enable_memory_subsystem(current: bool) -> bool` con texto verbatim doc 06 §6.1. Wired en `durin onboard` flow.
- **Refs**:
  - `durin/cli/onboard_memory.py` (ya existe, añadir función).
  - Wizard wiring: `durin/cli/onboard.py::run_onboard` — añadir step "Memory" antes del config provider.

### P6.2 — Onboarding: aux model for memory (doc 06 §6.4) ✅ DONE (commit `572d5cf`)

- **TYPE**: 🟢 module (~30 LOC)
- **DoD**: `prompt_memory_aux_model(current_agent_model: str, current: str | None) -> str` ofrece "same / specify / skip". Setea `config.aux_models.memory`.
- **Refs**:
  - `durin/cli/onboard_memory.py`.

### P6.3 — Tool description constants per doc 06 §3 ✅ DONE (commit `572d5cf`)

- **TYPE**: 🟡 refactor + 📄 docs sync (~50 LOC)
- **DoD**: Las descripciones de `memory_search`, `memory_store`, `memory_ingest`, `memory_drill` en código coinciden VERBATIM con doc 06 §3.1-§3.4. La verificación es automática vía P5.5.
- **Refs**:
  - `durin/agent/tools/memory_search.py::_PARAMETERS`.
  - `durin/agent/tools/memory_store.py::_PARAMETERS`.
  - `durin/agent/tools/memory_ingest.py::_PARAMETERS`.
  - `durin/agent/tools/memory_drill.py::_PARAMETERS`.
- **LOC**: ~50 (text updates en los 4 tools).
- **Risk**: el spec puede tener wording que sea peor para el LLM en práctica. Mitigación: si bench muestra regresión (-5pp en LoCoMo), revertir + ajustar spec.

### P6.4 — `identity.md` ✅ DONE (commit `2bdafec`)

---

## Phase 7 — Telemetry v2 (3 items pendientes)

### P7.1 — Privacy truncation ✅ DONE (commit `2bdafec`)
### P7.2 — Retention / log rotation ✅ DONE (commit `2e7097a`)

- **TYPE**: 🟢 module (~80 LOC)
- **DoD**: Telemetry JSONL files > 30 days old se comprimen a `.jsonl.gz`. Archives > 90 days se borran. Job corre diariamente vía health-check cron (P2.4).
- **Refs**:
  - Nuevo: `durin/telemetry/retention.py`.
  - Walk `~/.cache/durin/telemetry/*.jsonl` (per `durin/memory/stats.py::DEFAULT_TELEMETRY_DIR`).
  - Hook en P2.4 health-check tick.
- **LOC**: ~80 (module + tests).

### P7.3 — HTTPS push opt-in ✅ DONE (PushSink: `2e7097a`; end-to-end wiring incl. secret store + config + tests: `b822b75` / audit A8)

- **TYPE**: 🟢 module (~120 LOC)
- **DoD**: Cuando `telemetry.push_url` está set, eventos se envían además al endpoint via POST batches (cada 10 eventos o cada 60s). Authentication via `telemetry.push_token`.
- **Refs**:
  - Nuevo: `durin/telemetry/push.py` (httpx async client).
  - Config: `telemetry.push_url`, `telemetry.push_token`, `telemetry.push_batch_size`.
- **LOC**: ~120 + 5 tests.
- **Risk**: privacy — verify P7.1 truncation already applied antes de push.

---

## Phase 8 — Validation (todo) — wall-clock heavy

### P8.1 — LoCoMo bench run con v2 pipeline

- **TYPE**: 🔴 integration + 📄 report
- **DoD**: Correr `scripts/benchmark/locomo_run.py` (existe) con per_category=25 → resultado documentado en `docs/28_locomo_results_and_sota_gap.md`. Bar: ≥ 64.7% (v2 baseline previa) sin cross-encoder; ≥ 70% con cross-encoder.
- **Refs**:
  - Script: `scripts/benchmark/locomo_run.py`.
  - Resultados: append en doc 28.
- **Wall-clock**: ~90 min per run.
- **Risk**: regresión vs 64.7%. Mitigación: bench failure_breakdown per category (doc 28 §4) localiza qué se rompió.

### P8.2 — Adversarial generalist sets (4 dominios)

- **TYPE**: 📄 docs + tests (~300 QAs)
- **DoD**: 4 archivos JSON de 50 QAs c/u en `bench-results/adversarial/`: coder, sales, support, personal-assistant. Bar: ≥ 50% por dominio.
- **Refs**:
  - Nuevo: `bench-results/adversarial/{coder,sales,support,assistant}.json` (50 QAs each).
  - Runner: extender `locomo_run.py` o crear `adversarial_run.py`.
- **Wall-clock**: alto (writing 200 QAs + running them).
- **Risk**: 50 QAs de calidad por dominio toma horas. Considerar generación asistida por LLM (oracle answers humano-verificadas).

### P8.3 — Soak test 7 días

- **TYPE**: 🔴 integration (script + observación)
- **DoD**: Script que simula daily user activity por 7 días (cron) + verifica: Dream cost en $0.25-$1.50/día, no quarantines injustificados, index growth tracks workspace size, no silent retrieval misses.
- **Refs**:
  - Nuevo: `scripts/soak/run_soak.sh` + `scripts/soak/analyze.py`.
  - Métricas: lee de `~/.cache/durin/telemetry/*.jsonl`.
- **Wall-clock**: 7 días reales.

### P8.4 — Documentation lint pass

- **TYPE**: 📄 docs
- **DoD**: `grep -rn "(pending)" docs/memory/` no devuelve nada. Todas las decisiones marcadas con resolución. Discrepancias spec↔code detectadas por P5.5 fixed.
- **LOC**: ~varies (depende de cuánta deuda doc hay).

---

## Resumen y secuenciación recomendada

**Bloque A — refinements + safety nets (~250 LOC)**:
- P2.2 schema-version check (chico, alto valor)
- P5.4 memory_drill cleanup (10 LOC)
- P5.6 re-habilitar los 3 tests skipped (50 LOC)
- P6.3 tool descriptions sync (50 LOC)
- P5.5 audit script (50 LOC)

**Bloque B — Phase 4 cross-encoder (~250 LOC)**:
- P4.1 module → P4.2 integration → P4.3 onboarding → P4.4 webui

**Bloque C — operacional (~350 LOC)**:
- P2.3 watcher + P2.4 cron + P7.2 retention + P7.3 push

**Bloque D — validation (wall-clock pesado)**:
- P8.1 bench → P8.2 adversarial → P8.3 soak → P8.4 docs lint

**Recomendado**: Bloque A primero (chico, cierra deudas), después B (mayor unlock), después C (operational hardening), después D (validation final).

---

## Cómo este formato evita los problemas de la sesión autónoma

| Problema previo | Cómo lo previene este formato |
|---|---|
| "Ship cores, defer integration" | Cada item tiene **TYPE explícito** (module vs integration). No se puede hacer pasar integration por module. |
| Bullets de tamaño aparente uniforme | **LOC estimado** muestra disparidad real. P5.4 (10 LOC) vs P2.4 (150 LOC) son obviamente distintos. |
| DoD vaga ("implementado") | **DoD observable** ("file editado con vim aparece en próximo memory_search"). Imposible auto-engañarse. |
| Sin trazabilidad spec→código | **Refs** apuntan a archivos+líneas. La traducción spec→trabajo es explícita. |
| Bugs latentes empaquetados como "operational" | **Risk** explicita qué puede romperse. Si no está, el item no está bien analizado. |

**Mantenimiento**: actualizar este doc cuando se cierre cada item. Cada commit relevante debería referenciar el item ID (P2.2, P4.1, etc.) en el body del commit message.
