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

### ~~P7 — Threshold trigger para `memory_ingest`~~ (DROPPED 2026-05-31)

**Decisión**: descartado, NO deferred. El material ingestado es
encontrable vía FTS + vector desde el momento del write; la consolidación
en página canónica espera al cron diario `memory_dream`. Aceptamos esa
latencia.

**Razón empírica** (bench LoCoMo): cargar 800+ docs en una sola pasada
hace que un trigger per-write — aunque gateado por threshold por entity —
explote en LLM calls inviables. Cualquier re-habilitación futura del
path debe traer un throttle explícito (token-floor estilo
`dream.min_tokens_to_run`, cron-batch separado, o per-session debounce),
no "medimos en prod".

**Lo que queda en el código**: el wiring sigue en
[memory_ingest.py:289+](../durin/agent/tools/memory_ingest.py#L289),
short-circuita porque `memory_ingest` no taggea entities. Está
documentado en el comment del propio call site para que un futuro
mantainer no lo "active" sin agregar el throttle. La librería compartida
`durin/memory/threshold_trigger.py` sigue viva — la usa `memory_store`
(write desde el agent, donde sí tiene sentido per-write).

**Cómo cerramos el "qué tan vieja está la consolidación"**: telemetría
de los dos dreams — legacy `dream` ahora emite
`memory.dream.legacy.{start,end,skipped}` (commit `f343ba8`); el
entity-centric `memory_dream` ya emitía `memory.dream.{start,end,skipped}`
desde `dream_runner`. Cuando esos eventos muestren latencias de consolidación
inaceptables para entities con corpus pendiente, ese sería el trigger
concreto para revivir esta idea — con throttle.

**No queda nada por hacer aquí**.

---

### ~~G3.b — LLM query rewriter para `memory_search`~~ (DROPPED 2026-05-31)

**Decisión**: descartado, NO deferred. El módulo
`durin/memory/query_rewriter.py` existía con tests pero **nunca fue
invocado** por `memory_search.execute()` — scaffolding muerto desde el
día 1 del shipping. Removido en commit anterior junto con el field huérfano
`aux_provider_handle` en `MemorySearchTool` y los comments `G3.b`.

**Razón**: el wiring requerido era invasivo, el lift estimado (4-6pp en
LoCoMo según el code-level diff vs mem0) no se midió, y mantener un
módulo dormant con tests pasando creaba falsa señal de "ya casi". El
multi-query a nivel agent (instrucción en el tool description para
emitir 2-3 calls con phrasings distintos) cubre parcialmente el caso de
uso sin acoplar el sistema a un rewriter LLM.

**Lo único que sobrevivió**: el field `aux_models.memory` en
`AuxModelsConfig`. Repurposed en commit `70912b4` para elegir el modelo
de los dreams (no del rewriter). Esa es su única función ahora.

**No queda nada por hacer aquí**.

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
