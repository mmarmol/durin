# 26 — Memory graph view (webui, Obsidian-style)

> Visualización del entity-centric memory + drill-down completo a la
> información persistida. Diseñado como vista alternativa a las
> sessions, accesible desde el sidebar. Read-only sobre disco — no
> escribe ni invoca LLM (la búsqueda sí usa la pipeline LLM-aware vía
> `MemorySearchTool`, pero es read-only sobre el índice ya construido).
>
> Predecesor: doc 20 §2 P4 ("UI de gestión de entidades — entity
> cards") + P5 ("Tracing de tool calls de memoria en session
> viewer"). Esta vista cumple ambos parcialmente y agrega lo que el
> user pidió: drill-down a body/history/sources/archive + búsqueda
> espejo de lo que ve el LLM + popup sobre las edges para ver evidencia.

---

## §1 — Qué hace (estado actual)

### Vista principal

Layout single-pane con:

- **Header**: counts (nodes / edges / phantom / truncated), search box,
  focus toggle, refresh.
- **Canvas central**: grafo force-directed (vanilla, sin deps —
  ~50 LOC de repulsión + spring + centering en `MemoryGraphView.tsx`).
- **Side panel derecho** (cuando hay nodo seleccionado): tabs Info /
  Body / History / Sources / Archive.
- **Search panel izquierdo** (cuando hay query): lista de results con
  marker `canonical` vs `fragment`.
- **Edge popup** (cuando se clickea una edge): lista de entries que
  co-mencionan los dos refs.
- **Legend bottom-left**: palette por type + indicador phantom.

### Datos visualizados

**Nodos** = una página de entidad por archivo bajo
`memory/entities/<type>/<slug>.md` (archive subfolders excluidos —
absorbed pages son de-indexed por diseño doc 25 §2.D). Atributos:

- `id`: ref `<type>:<slug>`
- `type`, `name`, `aliases`
- `weight`: cuántas entries episódicas referencian este ref (proxy de
  importancia / centralidad)
- `phantom: true` cuando un ref aparece en entries pero no tiene página
  consolidada todavía (signal visual de "corre dream")

**Edges** = co-ocurrencia en entries episódicas. Cada entry que tagea
≥2 entidades contribuye `+1` a cada par. Peso del edge = count total.
Grosor de la línea visual escala con `log(weight)`.

### Drill-down (side panel tabs)

- **Info**: type, aliases, identifiers (email/slack/github/etc del
  frontmatter), last dreamed cursor, entries referencing.
- **Body**: el cuerpo markdown completo de la página (lo que el dream
  consolidó — la "current state" + secciones).
- **History**: lista de git commits que tocaron esta página, con
  subject + body + trailers (incl. `Judge-Confidence` cuando es auto-
  absorb). Cada commit expandible para ver reasoning + trailers
  completos. Badge `auto` cuando viene de §2.D dispatcher.
- **Sources**: entries episódicas post-cursor que referencian esta
  entidad — las que el dream todavía no consolidó. Body de cada entry
  expandible. Esto materializa el "raw evidence behind the
  consolidated content" del contrato §2.H.
- **Archive**: páginas absorbidas que viven en
  `entities/<type>/<canonical>/archive/<absorbed>.md`. Cada item muestra
  slug, name, absorbed_at, absorbed_reason. (Evidencia de §2.D
  auto-merges.)

### Búsqueda

El search box dispara `GET /api/memory/search` que invoca el mismo
`MemorySearchTool` que usa el LLM — vector retrieval + entity-aware
reranking RRF + grep fallback. La respuesta carga el panel izquierdo
con resultados que muestran:

- Badge `canonical` (verde) vs `fragment` (ámbar) — doc 25 §2.H kind.
- Headline + snippet + timestamp.
- Click en canonical → selecciona el nodo en el grafo.

El grafo dimea los nodos que NO matchean el search (composable con
focus mode).

### Focus mode (1-hop)

Botón Focus en el side panel: muestra solo la entidad seleccionada
+ sus vecinos directos (1-hop). El resto del grafo queda dimeado.
Stackable con search (dimming es AND, no OR).

### Edge inspection

Click en cualquier línea de edge → popup junto al click con la lista
de entries que co-mencionan los dos refs. Permite responder "por qué
están conectados estos dos?".

### Refresh

Manual — el builder camina disco en cada call. Sin auto-poll para no
sobrecargar workspaces grandes.

---

## §2 — Endpoints backend

Todos en `durin/channels/websocket.py` con autenticación via
`_check_api_token`. Lógica en `durin/memory/graph.py` y
`durin/memory/graph_api.py` (puros, JSON-serializables).

| Path | Función | Shape |
|---|---|---|
| `GET /api/memory/graph` | `build_memory_graph(workspace)` | `{nodes, edges, stats}` |
| `GET /api/memory/entity/<ref>` | `get_entity_detail(workspace, ref)` | `{ref, page, history, archive, entries}` o 404 |
| `GET /api/memory/search?q=&scope=&level=` | `search_memory_api(workspace, q, scope, level)` | igual que `memory_search` tool: `{results, total, strategy, ranking}` |
| `GET /api/memory/edge/<src>/<tgt>` | `get_edge_detail(workspace, a, b)` | `{source, target, total, entries}` |

Caps defensivos: graph cap 500 nodes / 2000 edges. Entity entries cap
50. Edge entries cap 50. Truncation se reporta en `stats.truncated_*`.

---

## §3 — Decisiones de diseño

### D1 — Co-ocurrencia en entries como única señal de edge (v1)

Alternativas consideradas:
- (a) Links explícitos `[other-ref]` en el body de la página → no los
  emite el consolidator prompt actual; no hay data.
- (b) Absorption history (archived → canonical) → otra clase de
  relación; mostrar como sub-categoría conceptual confunde el grafo.
- (c) Same-session co-occurrence (entries en mismo session.jsonl) →
  ruidoso, mezcla temas.

**Decisión**: (a) co-ocurrencia en entries. Es la señal más limpia
de "estas dos entidades aparecen juntas en el mismo evento mental".
Future evolutions documentadas como comments en `graph.py`.

### D2 — Force-directed vanilla en lugar de dep nueva

Alternativas:
- `react-force-graph`, `react-force-graph-2d`, `cytoscape`,
  `vis-network`, `d3-force` + render manual.

**Decisión**: ~50 LOC propios (repulsión Coulomb O(N²) + spring por
edge + centering + annealing). Para ≤200 nodes (cap doc 25 §1) es
fluido sin work web-worker. Dep zero. Si crece a >500 nodes
realistas → switch a `d3-force` o variante WebGL.

### D3 — Phantom nodes son first-class

Entities que aparecen en entries pero no tienen página → renderizadas
con borde discontinuo. **No las escondemos**: son la señal visible de
"hay coverage pendiente, corré dream". Visualizar el gap > hide el
gap.

### D4 — Search espeja `memory_search` tool

El user pidió "replicar el sistema de búsquedas como hace el LLM con
tool". Implementado vía wrapper directo a `MemorySearchTool.execute()`
en `search_memory_api`. La response carga el mismo `kind` /
`rendered` / `class_name` / `valid_from` / `entities` que ve el LLM
(doc 25 §2.H contrato). El badge `canonical|fragment` en la UI usa
exactamente la misma semántica.

### D5 — Markers visuales coherentes con doc 25 §2.H

`kind=canonical` → badge verde, `kind=fragment` → badge ámbar.
History tab badge `auto` (ámbar) para commits con `Reason: auto`
trailer (§2.D). Mantiene la línea: el usuario humano ve lo mismo que
el LLM ve.

### D6 — 1-hop focus + search dimming componen multiplicativamente

`isHighlighted(id) = (not focus or id ∈ focus_neighbours) AND (not
search or id ∈ search_matches)`. Si activás focus en Marcelo Y
buscás "pytest", solo se iluminan los vecinos de Marcelo que también
matchearon el search.

### D7 — Read-only end-to-end

No hay UI para editar páginas, mergear, dreamear. La motivación es
visibility primero: el user ve qué pasa, después decide qué CLI
correr. Editing UI es §2.D-adjacent y va por separado (P4 en doc 20
escala más allá del grafo).

### D8 — Lazy fetch del detail

El graph endpoint trae solo nodes + edges (compact). Click en nodo
dispara fetch del detail completo. Click en edge dispara fetch del
edge detail. Asume baja interacción concurrente — para uso
single-user con sub-segundo per fetch, OK.

---

## §4 — Por qué este orden de prioridades

Cuando el user dijo "haz, sorprendeme luego ajusto", la prioridad fue:

1. **Vista útil from-day-one**: aún sin entender la mecánica entera
   del entity-centric, el grafo + drill-down expone el modelo mental
   completo (entidades + relaciones + evidencia).
2. **Reuso del trabajo previo**: el contrato §2.H + el judge reasoning
   trailer + el archive folder fueron pensados para que un visualizador
   les diera vida. Esta vista lo materializa.
3. **Cero deps nuevas**: webui ya carga muchas libs; agregar
   `react-force-graph` (290 KB) o `cytoscape` (550 KB) por una vista
   secundaria no compensa.
4. **Búsqueda fiel al LLM**: el user explícitamente quiere ver lo que
   el LLM ve. La reutilización de `MemorySearchTool` garantiza que
   el badge `canonical|fragment` que ve el humano coincide exactamente
   con lo que recibe el modelo.

---

## §5 — Lo que falta / próximas mejoras

### M1 — Edit-from-graph (P4 doc 20 expansion)

Hoy click no permite editar. Acciones potenciales:
- Botón "Trigger dream for this entity" → POST que dispara `DreamRunner.run(entity_filter=ref)`.
- Botón "Absorb into…" → flujo guiado que llama a `EntityAbsorption.absorb()`.
- Inline rename de aliases / identifiers en la página.

**Bloqueador**: estos son writes; necesitan UX de undo/confirm bien
diseñada. Vale como doc separado cuando se priorice.

### M2 — Time slider

Filtrar nodes/edges por ventana temporal usando `valid_from` de las
entries que los soportan. Implica fetch incremental o pre-cómputo
por bucket de tiempo. Útil para preguntar "qué entidades aparecieron
juntas en mayo?".

### M3 — Memory ops trace en session viewer (P5 doc 20)

Cuando el agente invoca `memory_store` / `memory_search` /
`memory_dream` / `memory_expand` durante una sesión, destacar
visualmente en el historial del chat + drill-down al detail de
memoria invocada. Linkable a esta vista (click en una memoria →
abre su entity card).

**Cómo se conecta**: el endpoint `/api/memory/entity/<ref>` ya
existe. El session viewer solo necesita: (a) parsear tool calls del
JSONL, (b) badge inline en el turn que lo invocó, (c) link al detail.

### M4 — Edges de absorption history

Mostrar archived → canonical como un tipo de edge distinto
(direccional, dashed, color separado). Útil para visualizar la
evolución de identidades. Requiere parsear `archive/*.md`
frontmatter `absorbed_into` durante el graph build.

### M5 — Cluster detection / community layout

Para corpora grandes, usar algoritmo Louvain o similar para identificar
comunidades de entidades fuertemente conectadas. Colorear por
comunidad además del type. Defer hasta que tengamos workspaces con
>100 entidades reales.

### M6 — Heatmap mode

Vista alternativa: heatmap N×N de co-occurrence en lugar de
force-directed. Cuando el grafo se vuelve hairball, la matriz revela
la estructura mejor.

### M7 — Búsqueda más rica

- Filter by type checkbox row.
- Filter por phantom-only.
- Saved searches.
- Búsqueda combinada "marcelo AND project" con sintaxis.

### M8 — Performance hardening

Si el cap de 500 nodos se alcanza con regularidad:
- Pre-compute layout server-side y cachear.
- WebWorker para el tick loop.
- LOD: deshabilitar labels cuando hay >200 nodos visibles, mostrar
  solo top-N por weight.

### M9 — A11y

Hoy la interacción es exclusivamente pointer (drag, click). Agregar:
- Keyboard navigation (arrow keys, Tab, Enter).
- ARIA live region para narrar selección.
- Reduced-motion respeta para deshabilitar el annealing.

### M10 — Persistir layout

Hoy cada refresh re-deal del seed circular. Persistir posiciones por
ref en localStorage → estabilidad visual entre sessions.

---

## §6 — Tests

`tests/memory/test_graph_builder.py` (10): empty workspace, single
page, weight/edge cálculos, phantom detection, archive exclusion,
truncation, sort order, types sorted.

`tests/memory/test_graph_api.py` (12): entity detail (missing,
minimal, identifiers promotion, post-cursor filter, archive
inclusion, bad ref), search (empty, grep path, kind marker), edge
detail (no co-occurrence, with co-occurrence, limit respect).

Suite total 4479 passing (+22 vs §2.D close).

---

## §7 — Componentes shipped

| Archivo | LOC | Función |
|---|---|---|
| `durin/memory/graph.py` | ~140 | `build_memory_graph` — nodes + edges |
| `durin/memory/graph_api.py` | ~250 | `get_entity_detail`, `search_memory_api`, `get_edge_detail` |
| `durin/channels/websocket.py` | ~95 | 4 handlers + 3 route registrations |
| `webui/src/lib/api.ts` | ~120 | types + 4 fetchers |
| `webui/src/hooks/useMemoryGraph.ts` | ~50 | manual-refresh fetch |
| `webui/src/components/MemoryGraphView.tsx` | ~720 | canvas + side panel tabs + search + edge popup + focus |
| `webui/src/components/Sidebar.tsx` | +25 | "Memory graph" button |
| `webui/src/App.tsx` | +15 | ShellView union + render branch |
| `tests/memory/test_graph_builder.py` | ~180 | 10 tests |
| `tests/memory/test_graph_api.py` | ~180 | 12 tests |

Total ~1775 LOC source + 360 LOC tests.

---

## §8 — Lo que **no** hace (defer explícito)

- No edita memoria (M1).
- No dispara dream desde el grafo (M1).
- No visualiza absorption history como edges (M4).
- No tiene time slider (M2).
- No usa WebGL ni cluster detection (M5, M8).
- No es accesible vía teclado (M9).
- No persiste layout (M10).
- No integra con session viewer todavía (M3 — endpoint listo, falta wiring).

Cada uno está documentado arriba en §5 con razón y nivel de
prioridad. La regla: visualización primero, edición segundo, perf
tercero — bajo carga real veremos qué se mueve.

---

## Last updated: 2026-05-24 (initial design + ship)
