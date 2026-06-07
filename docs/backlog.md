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

### Auto-instalar el extra de pip cuando se activa una feature

**Contexto**: las features opcionales viven en extras de pip (`durin-agent[web]`
= ddgs/search, `[slack]`, `[discord]`, `[mcp]`, `[memory]` = fastembed, `[local]`
= llama-cpp, `[oauth]`). Un `pipx install` base no los trae.

**Problema** (observado 2026-06-06): tras un install limpio (base), activar el
search provider `duckduckgo` falló en runtime con `No module named 'ddgs'`. Hoy
durin solo **avisa** (`ImportError → "pip install durin-agent[X]"`). No tiene
sentido que falle algo que el usuario **activó** (en config o en el onboarding).

**Propuesta** (diseño acordado 2026-06-06): helper `ensure_extra(extra)` que mapea
feature→extra+módulo (`duckduckgo→web/ddgs`, `slack→slack`, `discord→discord`,
`mcp→mcp`, `memory→memory/fastembed`, `local→local`, `oauth→oauth`) y, si el
módulo no importa + `install.auto_install_extras` (**default ON**), corre
`sys.executable -m pip install "durin-agent[<extra>]"` + reintenta. **Seguro**:
solo instala extras PROPIOS de durin (pinneados en pyproject), nunca arbitrarios;
funciona en pipx/pip/editable. Enganches:
1. **onboarding wizard** — al elegir provider/canal/feature.
2. **config activation** — al escribir el setting que la activa (ej.
   `web_search.provider`, habilitar un canal).
3. **red de seguridad runtime** — en los ~7 sitios de import lazy (ej.
   `web.py::_search_duckduckgo` `from ddgs import DDGS`): `ImportError` →
   `ensure_extra` + retry → **nunca falla lo activo**.

El (3) es el catch-all robusto (cubre "activo en config pero dep faltante"); (1)+(2)
lo hacen proactivo (instala al activar, no al primer uso). Toggle OFF para
offline/air-gapped → cae al aviso actual.

**Estado**: pendiente — diseño acordado; no construido (se priorizó cerrar el
rediseño de memoria + merge a main, 2026-06-06).

---

## §2 — Backlog (sin priorizar)

### P5 — Tracing de tool calls de memoria en la session viewer

**Contexto**: cuando el agente usa `memory_upsert_entity` / `memory_ingest` /
`memory_search` / `memory_drill` / `memory_forget` durante una sesión, hoy queda
en el log pero sin presentación visual integrada al historial de la sesión.

**Idea (Marcelo)**: en la session viewer (web especialmente, también
TUI si se puede):

- En cada turn que el agente invoque una memory tool, destacar
  visualmente cuál se procesó (ej: badge `📝 memory_upsert_entity` al lado del
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

**Estado**: pendiente — la pipeline entity-centric YA está implementada (Phase
1-7 + audit 2026-06) y la webui ya tiene una **vista de grafo** de memoria (nodos
por entidad + filtros por tipo + panel de detalle al click en `/api/memory/graph`
→ `graph.py::build_memory_graph`). Las "entity cards" (contact-book) serían una
presentación alternativa sobre esa misma data; ya no está bloqueado.

### P6 — Skills importadas: sin sandbox ni consentimiento de ejecución runtime

**Contexto**: §6.B importa skills a través del piso §8.C (fetch → cuarentena →
scan → gate → install). El gate es **install-time**: controla que la skill ENTRE.

**Problema**: una vez instalada (incluso una `dangerous` vía override), la skill
corre sus instrucciones/scripts con los permisos normales del agente. No hay
sandbox de ejecución ni consentimiento por-skill al correrla. El gate no confina
la ejecución.

**Qué hace el campo** (investigado 2026-06-03, hermes + openclaw):
- Ninguno sandboxea la ejecución de skills. Ambos gatean sólo install-time y
  ejecutan vía su capa genérica de aprobación de tools (`tools/approval.py` en
  hermes; `exec-approvals.ts` `ExecSecurity/ExecAsk` en openclaw).
- Hermes: los `install` specs de dependencias (brew/npm) **nunca se ejecutan** —
  quedan para el agente vía su terminal aprobado. `skills.inline_shell` (off por
  default) puede pre-ejecutar `` !`cmd` `` del body SIN aprobación.
- Openclaw: **auto-ejecuta** los install specs (hardening: `--ignore-scripts`,
  regex allowlist, bootstrap go/uv que puede correr brew/apt con sudo). Único
  consentimiento: el wizard de onboarding.

**Propuesta tentativa** (incremental, no el sandbox completo):
1. Near-term: cuando una skill declara `install` specs, **ofrecer correrlos con
   aprobación explícita** del user (punto medio entre hermes=nunca y
   openclaw=auto).
2. Apoyar la ejecución de scripts de skills en el gate de tools de durin.
3. Sandbox real (límites FS/red por skill) = v2 grande; medir necesidad.

**Estado**: **item #1 CONSTRUIDO (2026-06-04)** — `skill_install_deps` tool:
dry-run→confirm, configurable vía `skills.install_policy` (`never`|`approve`|`auto`),
corre los comandos por el **exec gate de durin (ExecTool)** estilo hermes (no
subprocess paralelo), sólo specs que el scanner §8.C no flaggeó, `download`/sudo
excluidos (privilegiados surfaceados como `needs_privileges`). Plan:
`docs/archive/skills-plans/2026-06-04-skill-install-deps-p6.md`. Pendientes: **#2**
(correr scripts bundleados de skills por el tool gate) y **#3** (sandbox FS/red
per-skill). durin sigue alineado con el campo (gate install-time + ahora exec gate).


### P7 — API REST del channel es GET-con-query (sin body POST) → valores sensibles en query

**Contexto**: la API REST co-ubicada en el channel websocket (skills, settings,
secrets, cron, config) se sirve sobre el parser del handshake
(`WsRequest = websockets.http11.Request`), que sólo lee request-line + headers.

**Problema**: por eso **todas las mutaciones van por GET con query params** —
incluyendo valores sensibles: el `source` (URL) de import de skills, los tokens
en `/api/settings/update` y `/api/secrets`, el `content` de skills. Quedan en
logs del server y en el historial del browser. Localhost + token lo mitiga, pero
GET para mutaciones con efectos/red no es correcto.

**Propuesta tentativa**: dar **soporte de body POST** a la capa HTTP del channel
(leer `Content-Length` bytes de la conexión tras parsear headers en
`_dispatch_http`) y migrar las rutas mutadoras/sensibles a POST con body. No es
local a skills — beneficia secrets/settings/skills por igual.

**Estado**: pendiente, no bloquea (localhost + token). Es cambio de plataforma,
no de skills; agendar como su propia tarea.


### DX — Sin separación dev/daily: todo el estado vive en `~/.durin` (falta `DURIN_HOME`)

**Contexto**: es común tener dos instalaciones del CLI a la vez — una de
desarrollo (editable, en el `.venv` del repo) y una "daily driver" (pipx en
`~/.local/bin`). Ambas resuelven su raíz de datos al **mismo** `~/.durin`.

**Problema**: no hay forma limpia de aislarlas. `--config` solo reubica el
`config.json` y `workspace` es configurable, pero el resto está hardcodeado a
`Path.home() / ".durin"`: `config.json` ([config/loader.py:68](../durin/config/loader.py#L68)),
`workspace` default, `history`, `bridge`, `sessions` ([config/paths.py](../durin/config/paths.py)),
raíz del daemon ([cli/gateway_daemon.py:56](../durin/cli/gateway_daemon.py#L56)),
media de canales (qq), `oauth/`, `secrets.json`, `tool-results`. Resultado: una
corrida de dev lee/escribe el estado de producción (memoria, cron, sesiones,
secrets) sin querer. Observado en vivo: levantar el gateway de dev usó el
`~/.durin/workspace` real (cron jobs `dream`/`memory_dream` reales).

**Propuesta tentativa**: introducir `DURIN_HOME` (env var, default `~/.durin`) y
centralizar la resolución de la raíz en **un** helper del que deriven todas las
rutas; reemplazar los `Path.home() / ".durin"` hardcodeados por ese helper. Así
dev corre con `DURIN_HOME=~/.durin_dev durin gateway …` y queda 100% separado de
`~/.durin` (daily). Cuidar: migración/compat (si `DURIN_HOME` no está seteado,
comportamiento idéntico al actual) y que `--config` siga funcionando como
override puntual del config.

**Estado**: pendiente — cambio acotado pero toca varias rutas; requiere test que
verifique que con `DURIN_HOME` seteado ningún path cae en `~/.durin`.

---

### P8 — Memory graph webui: modelo Obsidian (reading-first + local graph) — fases restantes

**Contexto**: rediseño del panel/grafo de la vista Memoria (`MemoryGraphView.tsx`),
informado por cómo funciona Obsidian (el grafo es un desvío de navegación, el
contenido es el hogar) y por un relevamiento de soluciones de memoria-para-agentes
(mem0, Letta, Zep/Graphiti, Cognee, GraphRAG, Khoj, Supermemory).

**Hallazgo estratégico** (del relevamiento): el flujo "click en nodo → leer el
contenido fuente + procedencia" es un nicho UX **sin dueño** (Supermemory lo tiene
pero cerrado/cloud; Zep lo tiene a nivel datos pero solo API; Khoj es markdown-native
pero sin grafo). durin (grafo + vault markdown + browsing de procedencia) está
posicionado para ser el único que lo hace bien. No reinventar el motor de retrieval
(Graphiti/Cognee ya fuertes); la ventaja de durin es el vault editable local-first +
la UX de inspección/procedencia.

**Ya hecho** (branch `worktree-phantom-graph-policy-a`): render MD del cuerpo;
content-primacy (panel ~58vw al seleccionar); local-graph (auto-foco al nodo activo);
landing en contenido renderizado al hacer click; procedencia con navegación a sesión.

**Pendiente — Fase 2.b (reading-first completo)**: reemplazar los 6 tabs pares de
entidad por una vista de lectura única: cuerpo renderizado = héroe; Propiedades +
Procedencia + Entradas + Sources + History + Archive como secciones plegables
(`<details>`), no tabs. Mantener tabs solo para sesiones. Conviene pasada en browser
(jerarquía visual, plegado por defecto).

**Pendiente — Fase 2.c (hover preview)**: popover al pasar sobre un nodo con el
contenido renderizado (fetch debounced del detalle), estilo Obsidian page-preview —
"survey sin navegar". Riesgo: fetch-por-hover + posicionamiento del popover; tunear en vivo.

**Pendiente — Fase 3 (clustering/comunidades)**: el grafo global es un hairball y
empeora al crecer. Patrón de GraphRAG/Zep: agrupar entidades en comunidades con
resúmenes (map-reduce), colapsar comunidades por defecto y expandir on-demand. Hacer
DESPUÉS de Fase 2.b/2.c, con lo aprendido de cómo se usa el local-graph (quizás el
local-graph reduce la necesidad del global, cambiando el diseño de clustering).

**Live review (2026-06-07, deployed to home + dogfooded vía Playwright):**

Ya hecho en esta ronda: single-click = peek compacto (no tapa los nodos) / **double-click = vista completa** (todo el espacio); MD del cuerpo renderizado + se limpian los marcadores `<!-- author [[...]] -->`; search filtra skills client-side + **dedup de fragments** (8 chunks idénticos → 1) + texto legible; guarda defensiva en `searchMatchSet` (un payload `{error}` ya no deja la app en blanco).

Pendiente (de la review en vivo):
- **History mezcla procedencia** → mover procedencia a su propia pestaña; History = solo commits git.
- **Search: click en FRAGMENT no hace nada** (solo `canonical` selecciona nodo) → definir destino (abrir la referencia/entry).
- **Header "N results"** muestra el total del backend, no los visibles tras filtro/dedup.
- **Bug backend**: `search_memory_api(scope="fact")` → `{"error":"invalid scope 'fact'"}` pese a que el schema declara el enum `["all","skill","fact"]`. Mismatch schema↔validación en `MemorySearchTool`; arreglar la validación o quitar `fact` del enum.
- Fase 2.b reading-first completo (tabs→secciones), 2.c hover-preview, Fase 3 clustering (sin cambios).

**Estado**: Fase 1 + core de Fase 2 + fixes de live-review implementados y desplegados en home; resto planeado arriba.

---

## Last updated: 2026-06-07 (added P8 memory-graph Obsidian-model redesign plan; Fase 1 + core Fase 2 shipped, 2.b/2.c/3 planned)
