# 20 — Pendings observados durante uso

> Bitácora viva de items de UX / features que aparecen mientras uso durin
> pero que NO bloquean el trabajo actual (entity-centric memory, doc 18 y
> doc 19). Cada item se anota con: contexto, problema, propuesta tentativa
> (si la hay), y estado.
>
> Cuando un item se aborda, se mueve a "Resueltos" al final con la fecha
> y el commit/PR de referencia.

---

## §1 — Pendientes activos

### P1 — Edición de campos password/key en la web (secrets system)

**Contexto**: web settings, sección de providers o channels que usan el
sistema KEY/PASSWORD privado (`${secret:...}` refs).

**Problema**: cuando se edita un campo password que tiene un secret ref
(`${secret:openai_api_key}` o similar), el editor muestra **la variable
literal** al editar. No queda claro qué hace si el user:
- Escribe encima → ¿reemplaza el valor del secret o crea un nuevo secret?
- Borra el contenido → ¿deja vacío o desconecta el ref?
- Confirma sin cambios → ¿re-escribe lo mismo o no-op?

El comportamiento actual probablemente es correcto en código pero la UI
no comunica intención.

**Propuesta tentativa**:

- Mostrar dos representaciones distintas:
  - Si el campo es un secret ref: badge tipo `🔒 secret:openai_api_key`
    (no editable directamente). Click para "rotar" o "desconectar".
  - Si es plaintext: input normal.
- Botón "Edit secret value" abre un dialog separado que aclara: "Esto
  reemplazará el valor almacenado para `openai_api_key`. Todas las refs
  a `${secret:openai_api_key}` lo usarán."
- Botón "Disconnect secret" reemplaza el ref por input plaintext.
- Onboarding wizard ya tiene patrones para esto — alinear con esa UX.

**Estado**: pendiente.

---

### P2 — Edición de nombres de sesiones

**Contexto**: web (y posiblemente TUI). Cada sesión tiene un nombre
auto-generado (timestamp o title-inferred del primer mensaje).

**Problema**: no se puede renombrar la sesión. Al acumular muchas, se
vuelve difícil distinguir cuál era cuál.

**Propuesta tentativa**:

- Web: click en el nombre de la sesión en el sidebar → inline edit (similar
  a renombrar archivos en file explorer).
- Endpoint: `PATCH /api/sessions/<key>` con `{title: "new name"}`.
- Persistir el override en `<key>.meta.json` (campo `display_title`); si
  no existe, fallback al auto-generated.
- TUI: comando `/rename <new name>` para la sesión actual, o `/sessions
  rename <key> <new name>`.

**Estado**: pendiente.

---

### P3 — Comando para cambiar modelo es precario — autocompletion progresivo

**Contexto**: TUI/web, comando `/model` (o equivalente) para cambiar el
modelo activo.

**Problema**: la UX actual no autocompleta progresivamente. El user tiene
que conocer la sintaxis exacta (`<provider>:<model>` o similar) o
recordar los modelos disponibles.

**Propuesta tentativa**:

Autocompletion en cascada:

1. Tipear `/model` + space → mostrar opciones:
   - Modelos configurados actualmente (los que están en `agents.defaults`,
     `auxModels`, etc. — los conocidos).
   - Lista de providers disponibles.
2. Si el user elige un **modelo configurado** → set y done.
3. Si elige un **provider** (ej: `openai`, `anthropic`, `zhipu`) →
   próximo step muestra todos los modelos disponibles del provider
   (catalog refresh per `refresh_model_capabilities.py` o cache).
4. Si el provider está sin configurar → ofrece configurar primero
   (link/comando para setup).

Implementación posible:

- Reusar la slash command palette infrastructure de la web (el slash
  picker que recién arreglamos).
- En CLI/TUI: rich autocompletion via prompt_toolkit (ya está en deps).
- Backend: endpoint `/api/models?provider=<x>` ya existe (commit
  `de4143e feat(web): GET /api/models + /api/model/capabilities`).

**Estado**: pendiente — requiere proposal más detallada antes de implementar.

---

## §2 — Backlog (sin priorizar)

### P5 — Tracing de tool calls de memoria en la session viewer

**Contexto**: cuando el agente usa `memory_store` / `memory_search` /
`memory_dream` / `memory_expand` durante una sesión, hoy queda en el
log pero sin presentación visual integrada al historial de la sesión.

**Idea (Marcelo)**: en la session viewer (web especialmente, también
TUI si se puede):

- En cada turn que el agente invoque una memory tool, destacar
  visualmente cuál se procesó (ej: badge `📝 memory_store` al lado del
  turno, o highlighted background).
- Click en el badge → expande para mostrar la memoria exacta que
  escribió/leyó, los argumentos, y el resultado (entry id, results
  retornados, etc.).
- En la misma vista, listar el resto de calls a memoria de la sesión
  para ver el flujo completo: "esta sesión hizo 3 stores, 5 searches,
  1 dream".
- En web: el visualizador de memorias integrado (P4) puede linkearse
  desde acá — click en una memoria → abre su entry card.

**Casos de uso**:
- Debugging: entender por qué el agente respondió X — ¿qué memorias
  recuperó?
- Auditing: revisar que el agente está taggeando entities bien.
- Aprendizaje: ver qué patterns de uso emergen tras N sesiones.

**Propuesta tentativa para web**:
- Backend: el log JSONL ya tiene los tool calls. Agregar endpoint
  `/api/sessions/<key>/memory-ops` que devuelva los memory_*
  invocations parseados.
- Frontend: badge inline en cada turn + side panel "Memory operations"
  con timeline. Click expande a JSON pretty + link al entry/page.

**Propuesta tentativa para TUI**:
- Más constrained por superficie. Quizás un comando `/memory-ops` que
  liste las operaciones de la sesión actual con drill-down (similar
  al `durin memory expand`).

**Estado**: idea, sin priorizar. Probablemente post-P4 (entity cards
UI) porque comparte infrastructure del visualizador.

### P4 — UI de gestión de entidades (entity cards)

**Contexto**: doc 18 §4 (post-Phase-2 entity-centric memory). Cuando
el dream genere páginas `entities/<type>/<slug>.md` con frontmatter
estructurado (aliases, identifiers, etc.), una UI futura puede
renderizar "entity cards" tipo contact book.

**Caso de uso**: user abre la web → ve listado de personas con cards
mostrando email/phone/slack ID; click → ve la página completa (current
state + history + sources).

**Propuesta tentativa**:

- Nueva sección en webui `/entities` que liste pages bajo
  `entities/<type>/`.
- Card render binding al frontmatter:
  - `name` + `aliases`
  - Campos emergentes (`identifiers`) renderizados como pills/badges
  - Link "View page" abre el markdown rendered
  - Search box que busca contra aliases + identifiers (alias_index ya
    expuesto via endpoint)
- Search global: query `mmarmol@mxhero.com` debería surface marcelo
  card.

**Estado**: pendiente, post-Phase 5 (cuando la pipeline entity-centric
esté implementada).

### P6 — Índice keyword escalable como fallback de `memory_search` (FTS5 / inverted index)

**Contexto**: el path de grep en `durin/memory/search.py` (`search_dreamed`,
`search_undreamed`) hace walk + `load_entry` parse YAML por cada archivo
en `memory/<class>/*.md` por cada query. Para N pequeño (bench LoCoMo,
sesiones cortas) es instantáneo. A escala de daily use con miles de
entradas episódicas / sesiones, O(N) walk + parse por query se vuelve
caro.

Hoy ya tenemos dos protecciones contra el escalado:
- **Vector index** (`durin/memory/vector_index.py`) — O(log N) via
  LanceDB. Es el path principal cuando `memory.enabled=True`.
- **Dream consolidation** — fusiona episódicos en entity_pages
  canónicos, reduciendo el N que llega al grep.

**Problema**: si vector está apagado, falla, o no encuentra (queries
naturales tipo "Calvin Japan stay" matcheando contra substring literal
del query completo → 0 results), `memory_search` cae a grep, que no
escala. El fallback no es robusto a largo plazo.

**Trade-off explorado (24 May 2026)**: vimos tres patrones en sistemas
de referencia:
- **Hermes** → SQLite FTS5 + BM25 con tokenización AND por default + doc
  explícita al LLM ("multi-word=AND, OR para broader, quoted para exact,
  prefix*"). Es la opción más madura para keyword search escalable.
- **Pi** → no tiene memoria long-term; delega a `grep` del filesystem.
- **OpenClaude** → LLM-as-judge pre-inyecta memorias por turn; no usa
  tool de search.

**Propuesta tentativa**:

- Agregar índice FTS5 (SQLite) sobre `memory/<class>/*.md` que se
  actualiza al `store_memory` y al dream consolidation.
- Schema mínimo: `(entry_id, class_name, headline, summary, body,
  entities_concat, valid_from)` con FTS5 sobre `headline + summary +
  body + entities_concat`.
- Reemplazar el path grep actual en `search_dreamed` por query FTS5
  cuando el índice existe (fallback al walk actual si está roto o
  vacío).
- Actualizar `memory_search` tool description para enseñar la sintaxis
  Hermes-style cuando el path activo sea FTS5.

**Cuándo elegir esta opción**: cuando veamos en producción que vector
deja huecos sistemáticos (queries que el LLM hace y vector no responde
bien) Y el N de archivos es grande. Hasta entonces, el path vector +
fallback substring actual es suficiente — esta es una optimización de
escala, no un fix funcional.

**Riesgo de hacerlo antes de tiempo**: dos índices que mantener
(LanceDB + SQLite FTS5) con su propia lógica de sincronización, race
conditions en concurrent writes, dependencia adicional (SQLite es
stdlib pero los wrappers de FTS5 requieren cuidado en triggers).

**Estado**: backlog, no priorizado. Activar cuando: (a) el bench muestre
queries reales donde vector falla sistemáticamente, o (b) reportes de
slowdown en `memory_search` con workspaces grandes.

### P8 — Bench-100 fail audit (2026-05-30) — 13 bugs reales identificados

**Contexto**: bench-100 proporcional post-H26 (decay removal) → 68/100 oficial. Análisis QA-por-QA de los 30 fails reveló que ~17 son ruido del dataset (gold mal etiquetado, judge demasiado estricto, infra timeouts) y **~13 son fails reales del sistema**. Score "fair" estimado: ~85%.

**Distribución de los 13 fails reales**:

| Tipo | Cantidad | QAs |
|---|---|---|
| Retrieval miss (memoria no surfacea fact que SÍ existe) | 8 | conv-3-q169 (Tilly), conv-3-q91 (dragons), conv-9-q142 (party), conv-4-q40 (NC/TN), conv-5-q19 (cook treats), conv-9-q54 (documentaries), conv-8-q44 (paint subjects), conv-7-q113 (comfort) |
| Synthesis fail (memoria OK, agente enumera/elige mal) | 4 | conv-3-q113 (fantasy AND sci-fi), conv-3-q116 (movies vs games), conv-2-q19 (windshield), conv-6-q2 (VR Club) |
| Adversarial hallucination | 1 | conv-0-q188 (Caroline hike fabricated) |

**Patrones identificados**:

1. **Event_summary / observation entries no se rankean alto**: 4 de los 8 retrieval miss son facts presentes en `event_summary[events_session_N]` u `observation[session_N]` — entradas curator-derivadas. El bench las siembra pero las queries del agente no las priorizan. **Hipótesis**: el embedding upgrade a `multilingual-e5-small` (H27, commit d6a6e16) puede ayudar porque está retrieval-tuned y los summaries son frases cortas y abstractas. **Validación**: bench-100 post-H27 corriendo ahora — comparar el delta en este bucket específico.

2. **Synthesis fail = el LLM no enumera todas las opciones cuando el corpus tiene varios matches**: conv-3-q113 ("fantasy AND sci-fi" → agente drop "fantasy"), conv-3-q116 (corpus tiene ambos pero agente picked uno arbitrariamente). El identity.md tiene "enumerate all" pero el LLM lo ignora bajo glm-5.1. **Soluciones a explorar**:
   - (a) Reforzar el bullet en identity.md con ejemplo concreto
   - (b) Cross-encoder rerank funcionando (actualmente OFF — sentence-transformers no instalado, ver H25). Cross-encoder podría surfacear mejor entries del mismo topic
   - (c) Probar el LLM judge re-evaluando esos casos con prompt más liberal — confirmar que la respuesta SÍ contiene el fact aunque incompleta

3. **Adversarial hallucination es difícil**: conv-0-q188 es el único de 23 adversarials. 22/23 = 96% accuracy en adversarial es realmente bueno. La respuesta es fabricación parcial sobre evidencia tangencial — el agente confundió "incident" (sí hubo) con "setback" (no encaja).

**Acciones derivadas**:

- [ ] **Acción 1 (en curso)**: bench-100 post-H27 corriendo. Comparar score + per-category vs bench-100 post-H26. Si retrieval miss bucket baja → e5-small validado.
- [ ] **Acción 2**: instalar `sentence-transformers` localmente (no lo agregamos a deps por footprint — pero podemos hacer un bench experimental con CE activo para medir delta).
- [ ] **Acción 3**: si bench-100 post-H27 no cierra los synthesis fails, reforzar enumeration rule en identity.md (variante stronger del bullet "Combine facts across hits — enumerate every distinct item before answering").
- [ ] **Acción 4 (out of scope inmediato)**: re-judge con prompt más liberal para validar empíricamente que los 5 "judge over-strict" son realmente over-strict. No bug del sistema; ayuda interpretación del bench.

**Dataset issues identificados (informativo, no actionable)**:

- conv-3-q39: gold "attended" pero corpus dice "hosted"
- conv-3-q43: gold "four months" pero fechas dan ~2.8 meses
- conv-1-q56: gold "dancing together" pero corpus dice "rollercoaster" literal
- conv-4-q55: gold "May 2023" pero corpus dice "just under a year as of Dec 2023" (~Jan 2023)
- conv-7-q35: gold "24 Feb" vs agente "25 Feb" — off by one, necesita verificar quién acierta
- conv-2-q64: gold pide titles específicos que el corpus no tiene

LoCoMo tiene ~10-12% de gold-noise (consistente con reportes de mem0, A-Mem, otros). No actionable de nuestro lado.

**Referencias**:

- Run dir: `bench-results/locomo/2026-05-30_094628_087ee40c/`
- Análisis full: `/tmp/fail_analysis.txt` (regenerable con script en el reporte)
- Commits relacionados: H25 (65d3a74), H26 (087ee40), H27 (d6a6e16)

---

### P7 — Threshold trigger para `memory_ingest` (simetría con `memory_store`)

**Contexto**: `memory_store` (durin/agent/tools/memory_store.py:282-363) ya
dispara `DreamRunner` en daemon thread cuando una entity acumula
≥ `threshold_entries` post-cursor entries — patrón shipped 2026-05-24
(§2.A.1 β.2). `memory_ingest` **no** tiene el equivalente: un usuario que
sube docs no dispara consolidación intermedia, solo el cron diario o un
post-compaction/session-close hook eventual.

**Lo que cierra**: el "gap del ingest". Si el usuario carga 5 docs sobre
Caroline y Caroline ya tenía 8 episodic + 4 corpus pendientes, ese es el
momento natural de consolidar. Hoy se espera al cron diario o a que un
`memory_store` posterior cruce el threshold por su cuenta.

**SOTA reference (verificado 2026-05-25)**: ni hermes ni openclaw hacen
esto — ambos son "dumb pipes" para writes (hermes mirror a SQLite
fact-store sin merge, openclaw search-then-decide pre-write pero nada
post-write). Durin ya está por delante en este eje (3 de 4 trigger
points wireados); esto completa el 4to.

**Diseño** (post-crítica glm-5.1 2026-05-25):

- Nuevo módulo `durin/memory/threshold_trigger.py` con helper compartido
  `maybe_dispatch_threshold_dream(workspace, entities, dream_config,
  vector_index, source_trigger)`.
- Refactor de `memory_store::_maybe_dispatch_threshold_dream` para
  llamar al helper (preserva telemetry `trigger="threshold"` para
  retrocompat).
- `memory_ingest` acepta `dream_config` en constructor +
  `create(ctx)`; llama al helper con `source_trigger="post_ingest_threshold"`
  después de crear el corpus entry.
- **El conteo del threshold cuenta episodic + corpus** (no solo
  episodic). Razón: el ingest crea entries en `memory/corpus/`, y como
  SEÑAL de "user activo sobre esta entity" el corpus debe contar
  aunque Dream solo consolide episodic. Helper `_count_pending_for_trigger`
  walkea ambos directorios.
- **Sin dedup window global** (era over-engineering con race condition
  y memory leak detectados por glm). Burst protection 100% delegada a
  `DreamRunner` (lock + `min_seconds_between_runs=300s` + stale-lock
  recovery). 20 threads spawnados que ven lock y mueren rápido es
  cheap.
- Tests: unitarios del helper + test de contención `test_concurrent_dispatches_serialize_via_lock`
  (5 threads simultáneos → ≤1 Dream pass ejecutado).

**Costo**: ~215 LOC (75 código + 130 tests + 10 docs). ~3-4h dev.
Riesgo: bajo (patrón ya validado en `memory_store`).

**Beneficio**:
- Daily-driver: respuestas mejores post-ingest (canonical en vez de
  fragments raw)
- Cierra gap arquitectónico real
- Activar `threshold_entries > 0` en bench LoCoMo podría sumar
  +3-8pp en single_hop (especulativo, requiere medir)

**Cuándo elegir**: post-bench v3. Priorizar si v3 no muestra mejora
grande por otro lado (top_k=20 + rank visible). Si v3 sube >+2pp,
Step 2 doc 28 (source-priority weighting en vector index) es mejor
ROI para single_hop.

**Plan completo**: ver `/tmp/plan_ingest_threshold.md` (notas de
sesión 2026-05-25) — debe migrarse a `docs/arch/` si se aprueba.

**Estado**: planeado, no priorizado. Esperando resultado bench v3.

---

## §3 — Resueltos

(Vacío por ahora — items se mueven acá con fecha + commit al cerrarse.)

---

## Last updated: 2026-05-30 (P8 added — bench-100 fail audit, 13 real bugs identified, H27 bench validation in progress)
