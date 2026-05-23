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

---

## §3 — Resueltos

(Vacío por ahora — items se mueven acá con fecha + commit al cerrarse.)

---

## Last updated: 2026-05-23 (creado durante Phase 0 de implementación entity-centric)
