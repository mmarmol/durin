# Backlog

> Items observados durante el uso de durin (UX, features, deuda
> técnica) que NO bloquean el trabajo en curso pero merecen quedar
> registrados para no perderse. Cada item se anota con: contexto,
> problema, propuesta tentativa (si la hay), y estado.
>
> Cuando un item se cierra **se elimina del backlog** — el commit
> que lo cierra es el registro canónico (`git log` lo encuentra por
> mensaje). Items con avance parcial se actualizan in-place con el
> estado real. Items descartados explícitamente (decidimos no
> hacerlos) van a `bitacora.md` con el rationale. Documentos
> completos que quedaron superados (planes, propuestas) van a
> `archive/` con nota en `archive/README.md`.

---

## §1 — Pendientes activos

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
sesión 2026-05-25) — debe migrarse a `docs/architecture/` si se aprueba.

**Estado**: planeado, no priorizado. Esperando resultado bench v3.

---

### P11 — UI/CLI para gestionar cron jobs

**Contexto**: durin tiene cron jobs (heartbeat, dream cada 2h,
memory_dream daily, etc.) registrados internamente, y un tool `cron`
que el agente puede usar para crear/listar/borrar jobs desde el chat.
Pero NO hay:

- Subcomando `durin cron list/add/remove` en el CLI
- Panel en webui Settings para ver/editar cron jobs configurados

**Caso de uso**: user quiere ver qué jobs están corriendo, cuándo
ejecutaron por última vez, agregar/quitar uno sin pedirle al agente.

**Propuesta**:
- CLI: `durin cron list/add/remove/show <id>`
- Web: nuevo SettingsSection "Cron" con tabla (id, schedule, action,
  last_run, next_run) + botón "Add cron job"

**Estado**: detectado audit 2026-05-31. Funcionalidad existe en backend
(via tool), pero faltan superficies humanas para administrarlo.

---

### P12 — UI para gestionar entries de memoria individuales

**Contexto**: el Memory Graph view muestra entity_pages como nodos +
relaciones, pero NO permite ver/editar/borrar entries individuales de
`memory/{episodic,corpus,stable,...}/`. Hoy hay que usar
`durin memory show/forget/expand <uri>` desde CLI.

**Caso de uso**:
- "¿Qué memorias tengo sobre X?" (browse)
- "Esta memoria es vieja/incorrecta, bórrala" (forget)
- "Ver el contenido raw de este episodic" (read)

**Propuesta**:
- Backend ya tiene endpoints (`/api/memory/search` + `fetchMemoryEntity`)
- Frontend: nueva sección "Memoria" con list + filtros + click → drawer
  con contenido raw + botón forget
- Posible reusar el side-panel del MemoryGraph para mostrar el entry
  cuando el nodo es seleccionado (ya hay parte de esto)

**Estado**: detectado audit 2026-05-31. Más complejo que P10/P11.

---

## Last updated: 2026-05-31 (cleanup — P1/P2/P8 shipped; +P10 hardcoded strings; +P11 cron mgmt UI; +P12 memory entries UI; +pipx subprocess safety regression test)
