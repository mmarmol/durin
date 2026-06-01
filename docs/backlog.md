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

### Perf — `count_pending_for_trigger` hace full-walk del corpus por cada write

**Contexto**: `durin/memory/threshold_trigger.py`. `maybe_dispatch_threshold_dream`
(disparado desde `memory_store`/`memory_ingest` tras cada write con
entities) llama a `count_pending_for_trigger(workspace)` en
[threshold_trigger.py:161](../durin/memory/threshold_trigger.py#L161),
**antes** del check de threshold. Corre en **cada** write que pasa el
gate (`dream.enabled=True` + `threshold_entries>0`, ambos default → es
frecuente).

**Problema**: la función computa counts de **todas** las entidades pero
el call-site sólo usa `counts.get(ref)` para `ref in entities` (los 1-3
recién escritos). El costo es O(todo el corpus + todo episodic + todas
las entity pages) por write:
- Parte 1 — `_discover_pending_consolidations` ([memory_cmd.py:103](../durin/cli/memory_cmd.py#L103)):
  carga **todas** las entity pages (`pages_dir.rglob("*.md")` para los
  cursors) + camina **todo** episodic (`episodic_dir.glob("*.md")`,
  `load_entry` por archivo).
- Parte 2 — corpus walk ([threshold_trigger.py](../durin/memory/threshold_trigger.py)):
  `walk_class(workspace, "corpus")` + `load_entry` de **cada** archivo.

En vault típico ("cientos de entidades") es sub-segundo pero no gratis;
en vault grande es latencia per-write notable. Es la misma clase de
overhead per-write que motivó dejar dormant el threshold de ingest (ver
[[project_ingest_dormant_rationale]] / nota memoria).

**⚠️ Lo que NO funciona (verificado 2026-06-01, no repetir)**: el QA
report sugirió "pasar `entity_filter`" como fix one-liner. **Es
ineficaz, incluso contraproducente.** El parámetro `entity_filter` de
ambas funciones se aplica *después* de cargar cada archivo (el `if
entity_filter and ref != entity_filter: continue` está dentro del loop,
tras `load_entry`), así que **no evita leer ningún archivo** — sólo
achica el dict devuelto. Y como `count_pending_for_trigger` toma un solo
`entity_filter` (no un set), usarlo con N entities obligaría a llamarlo N
veces → **N full-walks → estrictamente peor**.

**Propuesta tentativa (cambio estructural, no one-liner)**:
- Un índice incremental de pending-counts por entidad (mantenido en el
  write path) en vez de full-walk por trigger. El write ya sabe qué
  entities tocó; podría incrementar un contador persistente y el trigger
  sólo leería ese contador para los `entities` del write actual.
- Alternativa parcial y más acotada: hacer que `_discover_pending_consolidations`
  cargue sólo las entity pages de `entities` (no todas) cuando recibe un
  filtro — pero ojo: esa función la **comparte Dream** para sus cursores,
  así que tocarla afecta su semántica; requiere tests de Dream además de
  los del trigger.
- Cualquier diseño debe preservar la semántica de cursor (parte 1 reusa
  el helper de Dream a propósito, para que el trigger "vea" exactamente
  lo que Dream consolidaría) y la señal de "hotness" del corpus (parte 2;
  Dream NO consolida corpus, pero un user activo dropeando docs sobre una
  entidad es señal de que está hot).

**Referencias**: ítem P1 #10 del QA review en
[docs/qa/code-review-2026-06-01.md](qa/code-review-2026-06-01.md).

**Estado**: pendiente — perf pass estructural. No bloquea; el costo es
tolerable en vaults chicos. Priorizar si/ cuando un vault grande muestre
latencia per-write medible (medir antes de diseñar).


---

## Last updated: 2026-06-01 (QA review P1: agregado item perf de threshold_trigger full-walk)
