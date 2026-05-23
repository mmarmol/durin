# 18 — Plan entity-centric consolidado

> Consolidación completa post-research (docs 16, 16a/b/c, 17) y refinamiento
> por discusión iterada (preguntas 1-6). Captura el modelo de memoria que
> durin construye: principios sostenidos, decisiones tomadas, descartes
> explícitos, riesgos identificados, y lo que queda fuera de scope inicial.
>
> Este doc es el plan vigente. Los predecesores (doc 16 §3.4-3.7 con notas
> incrementales, doc 17 con verdictos) sirven como traza histórica.

---

## §1 — Principios sostenidos

Los principios que ordenan todas las decisiones del modelo:

1. **Amplio y podar**: empezar con set ancho de tipos sugeridos (no
   enforced); el "podar" significa **bajar peso en ranker**, no borrar
   contenido.
2. **Anti-frágil**: el sistema degrada suavemente si el dream falla o se
   retrasa. La materia prima (entries inmutables) siempre buscable
   independiente del estado de consolidaciones.
3. **Local-only**: memoria del user es archivo en disco bajo su control
   exclusivo. Sin servidor, sin sync por defecto, sin remoto.
4. **Cross-profession**: el agente es genérico (coders, marketing/ventas,
   estudiantes, daily-life). El modelo no es dev-only.
5. **Memoria como sustrato conectado**: todos los artefactos —
   sessions, meta events, episodic entries, entity pages, consolidaciones,
   archives — están enlazados via links markdown + metadata frontmatter +
   git history. Nada vive aislado.
6. **Retrieval como activación filtrada**: la búsqueda no es "encontrar X"
   sino "qué nodos del sustrato se activan dado este query". El ranker
   filtra; el sustrato completo siempre está disponible para drill-down.
7. **Drill-down first-class**: desde cualquier resultado de retrieval, el
   sistema (CLI, agent tool, futuro UI) puede expandir hacia sources
   originales, versiones previas con razonamiento, entidades relacionadas.
8. **Nada se borra**: no hay deletion automática por edad, popularidad o
   ranking. El movement a `archive/` ocurre solo por razón estructural
   (entidades absorbidas en otra canónica), no temporal.

---

## §2 — Marco general: tres flujos cooperando

```
                       ┌─────────────────────────────────┐
                       │  Modelo (turno del agente)      │
                       │  • Detecta aprendizaje          │
                       │  • Llama memory_store           │
                       │  • Stamp en meta:               │
                       │    type=memory_write            │
                       └────────────┬────────────────────┘
                                    │
                                    ▼
            ┌───────────────────────────────────────────┐
            │ Indexación pre-dream (siempre)            │
            │ • Vector: memory entries (Phase 2 ✓)      │
            │ • Vector: compaction summaries por sesión │
            │ • Vector: session-close summary           │
            │ • Grep: sessions/<key>.md + meta timeline │
            └────────────┬──────────────────────────────┘
                         │
                         ▼ (eventualmente, fuera de sesión)
       ┌─────────────────────────────────────────────────┐
       │ Dream — entity-centric, cross-session           │
       │ • Mira todas las memory entries acumuladas      │
       │ • Agrupa por entidad                            │
       │ • Consolida en entities/<type>/<value>.md       │
       │ • Marca contradicciones con valid_from/valid_to │
       │ • Fusiona aliases → /archive/                   │
       │ • Linkea consolidated_into / sources            │
       └─────────────────────────────────────────────────┘
```

---

## §3 — Descartes explícitos

Decisiones tomadas durante la conversación de NO seguir ciertos caminos:

| Camino descartado | Razón | Origen |
|---|---|---|
| `background_review` post-turn (propuesta C) | Modelo inline + indexación + dream cubren su rol sin costo extra | doc 15 (archivado tras descarte) |
| Dream session-centric | Reemplazado por entity-centric con consolidación cross-session | doc 16 §1.2 |
| Cursor global `dream/cursor.json` | Reemplazado por cursor per-sesión + per-entidad | doc 16 §1.2 |
| Scoring multi-factor estilo OpenClaw (frequency/relevance/diversity/recency/...) | Heurística "entidad acumuló N observaciones" es más simple bajo entity-centric | doc 16 §1.2 |
| **Lifecycle automático (aging/stale/archive por edad)** | "No borramos" + ranker filtra relevancia → no se necesita movement temporal | Pregunta 4 |
| **Claim-status enum estructurado en YAML** | Markdown libre + prose narrativa cubre las contradicciones; git history para auditar | Pregunta 2 |
| **Weight field explícito por claim** | Implícito vía señales del ranker; explícito solo si emerge necesidad | Pregunta 5 |
| **Borrar summaries antiguas del índice post-dream** | "No borramos" + links explícitos `consolidated_into` para deduplicar en ranker | Pregunta 6 |
| **Pre-paging / mega-hub mitigation upfront** | Defer hasta evidence concreta (>200 claims en una entidad) | doc 18 §10 R2 |
| **DB graph como almacén primario** | Pierde editability/git/grep; markdown puro alcanza con sidecars derivados | doc 17 §3 D1 |

---

## §4 — Schema de entidades

### Approach

Set amplio de **8 tipos sugeridos pero no enforced**. Anclado en
literatura cognitiva consolidada (Tulving tripartite, CoALA, Conway,
Rosch prototype theory) para cubrir cross-profession.

### Set sugerido

| Tipo | Mapeo Tulving | Cobertura cross-profession |
|---|---|---|
| `person` | Semantic | coworker, client, profesor, familia |
| `place` | Semantic | oficina, mercado, campus, casa |
| `project` | Semantic | software project, campaign, tesis, mudanza |
| `topic` | Semantic | embeddings, B2B funnels, ML, minimalismo |
| `event` | Episodic | outage, demo, examen, cumpleaños |
| `artifact` | Semantic | archivo, deck, textbook, pasaporte |
| `stance` | Semantic | preferencia, opinión, belief, posición |
| `practice` | Procedural | skill, rutina, método, hábito |

### Cosas que NO son tipos primarios

Emergen como derivadas, no entidades propias:

- **"aprendizaje"** → consolidación de `topic` o actualización de
  `practice` (reflection à la Generative Agents).
- **"error"** → `event` con valencia negativa, o `stance` corregido.
- **"decisión"** → `event` puntual con `stance` asociado.
- **`file`, `symbol`** → caen en `artifact` o se referencian desde
  frontmatter sin necesitar página propia.
- **`tool`** original → herramienta concreta = `artifact`; método de uso
  = `practice`.

### Vocabulario abierto

El schema permite `type` no listado. Si el dream LLM propone un tipo
nuevo recurrente, se agrega a la lista canónica. La distinción "tipo
reconocido vs tipo emergente" vive en código, no en schema.

### Estructura en disco

```
memory/
├── episodic/<id>.md                          ← entries crudas, inmutables
├── entities/
│   ├── person/
│   │   ├── marcelo.md                        ← canónica, indexada
│   │   └── marcelo/
│   │       └── archive/
│   │           └── marcelo-m.md              ← absorbido, de-indexado, navegable
│   ├── project/durin.md
│   ├── topic/embeddings.md
│   └── ...
├── sessions/<key>.md
├── stable/                                   ← sin cambio
├── corpus/                                   ← sin cambio
└── pending/                                  ← sin cambio
```

### Frontmatter mínimo

```yaml
type: person
name: Marcelo Marmol
aliases: [Marcelo, mmarmol@mxhero.com]
dream_processed_through: 4892
created_at: 2026-03-15T...
updated_at: 2026-05-23T...
```

Sub-paging futuro para mega-hub mitigation reutiliza la misma carpeta
`<slug>/` (ej: `marcelo/preferences.md`, `marcelo/projects.md`).

---

## §5 — Storage y versionado

### Markdown libre como cuerpo

El contenido de cada `entities/<type>/<slug>.md` es **prosa markdown
libre** generada por el dream LLM. NO se usa schema YAML para claims
estructurados — los hechos viven en secciones como `## Current state`,
`## History`, con texto natural.

### Git como substrato interno local-only

`memory/` es un repo git local. **El user no commitea ni edita en esta
fase**. durin gestiona los commits invisiblemente:

- Único autor: `durin-dream <dream@durin.local>` (y `durin-write` para
  paths raw).
- `git init memory/` al instalar / pasar wizard.
- **Estrictamente local**: durin no configura remote, no sugiere sync.

### Output del dream por consolidación

Cada consolidación produce dos artefactos en un commit:

1. Contenido nuevo del archivo de entidad.
2. Commit message LLM-generated con razonamiento + trailers estructurados.

```
Consolidate person:marcelo (rev 17)

3 observaciones nuevas integradas. La principal: el user reafirmó dos veces
en sesiones recientes que prefiere pytest sobre unittest. También aparece
un alias adicional verificado. Removí el claim "uses Python 2.7" porque
la evidencia acumulada lo contradice consistentemente.

Sources: episodic/2026-05-20-001.md, episodic/2026-05-21-003.md, episodic/2026-05-22-007.md
Entities-touched: person:marcelo
Entities-referenced: project:durin, topic:testing
Dream-session: 2026-05-23-001
Cursor-before: 4521
Cursor-after: 4892
```

### Lo que git provee gratis

- "qué mudó" → `git diff` exacto.
- "por qué" → cuerpo del commit message.
- "entidades usadas" → trailers `Entities-touched/Referenced`.
- Inmutabilidad de versiones pasadas, sin `supersedes`/`superseded_by`
  custom.
- Anti-fragilidad → `git revert` deshace.
- Drill-down "why" → `git log entities/.../slug.md` y `git show <ref>`.

### Archive subfolder para absorbidos

Cuando dream fusiona dos entidades (alias detection): el archivo
absorbido se mueve a `entities/<type>/<slug>/archive/`. Mantiene
frontmatter con `absorbed_into: ../../canonical.md`, `absorbed_at`,
`absorbed_reason`. La canónica linkea hacia el archive vía markdown
link.

El indexer (LanceDB + futuros sidecars) tiene regla "skip `**/archive/**`":
los archivos quedan en disco, en git, navegables, pero NO surfacen en
retrieval normal. Drill-down sí puede traerlos explícitamente.

### `.gitignore` recomendado

```
*.lance/
vectors/
.aliases.json
.usage.json
.usage/
.dream.lock
.locks/
```

### Comandos durin (wrappers de git)

- `durin memory history <entity>` → `git log` formateado.
- `durin memory diff <entity> <revs>` → `git diff` formateado.
- `durin memory revert <commit>` → deshace una consolidación mala.
- `durin memory expand <node>` (futuro) → drill-down traversa sources,
  versiones, entidades relacionadas.

---

## §6 — Consolidación y dream

### Modelo: dream como punto de compresión, no de consistencia

- **Entries episódicos** son first-class para retrieval desde el momento
  en que se escriben (independientes del dream).
- **Dream produce consolidaciones**: archivos de entidad regenerados que
  resumen N entries en menos tokens.
- Si el dream no corrió todavía sobre entries recientes, esos entries
  **siguen siendo buscables** como objetos independientes. La página
  consolidada y los entries post-cursor **coexisten** en los resultados
  de retrieval; el LLM reconcilia en read-time con timestamps y contexto.

### Protocolo único uniforme para conflictos (α)

Cuando dream encuentra contradicciones durante consolidación:

- Marca el hecho viejo con expresión narrativa (`previously...`, `until
  2026-03-15...`) en el cuerpo markdown.
- Agrega el nuevo (`since 2026-03-15...`, `now prefers...`).
- El tipo de entidad es **contexto** para el prompt del LLM, no
  hardcoded en código.

Ejemplo:
```markdown
## Preferences

Marcelo prefiere pytest sobre unittest desde marzo 2026.
Previamente (hasta marzo 2026) usaba unittest, cambio observado en
sesiones 42 y 47.
```

No hay claim-status enum YAML — la prosa expresa la temporalidad.

### Trigger del dream

No bloqueante del retrieval. Puede ser cualquier combinación:

- Session-end (idle timer, `/quit`, context compaction).
- Background idle timer cada X minutos.
- Threshold-based: cuando una entidad acumula N entries post-cursor.
- Batch nocturno tipo cron.

La elección se hace por **costo y UX**, no por correctness. Se itera
con telemetría.

### Compaction summaries: link explícito + drill-down

Cuando dream consolida learnings de un compaction summary en páginas
de entidad:

- El summary queda en disco e indexado (no se borra ni de-indexa).
- El summary recibe frontmatter `consolidated_into: [entities/person/marcelo.md, entities/topic/embeddings.md]`.
- Las entidades resultantes pueden incluir `sources` referenciando el
  summary y los episodic entries.
- El ranker puede usar este link para downweightar el summary cuando
  las entidades ya están en results (anti-redundancia) — pero por
  defecto ambos quedan accesibles.
- Drill-down traversa este link cuando el user quiere expandir.

### Anti-fragilidad

Si dream crashea, hangs, o no corre por días:

- Nada se rompe. Entries siguen siendo buscables.
- El sistema degrada **suavemente**: el contexto retrieved crece, pero
  las respuestas siguen siendo correctas.
- `git revert` deshace una consolidación mala sin afectar entries.

---

## §7 — Retrieval entity-aware

### Mínimo L1 light (no opcional)

Sin esto, los casos básicos (alias, identidad ambigua, contexto stale)
se rompen.

**Write-time:**

1. `aliases: [...]` en frontmatter de cada `entities/<type>/<slug>.md`.
2. `dream_processed_through: <msg_idx|timestamp>` cursor por entidad.
3. `entities: [type:slug, ...]` en frontmatter de cada entry episódica
   (requiere propuesta A del doc 14 — ver §12).
4. Aliases index sidecar (`memory/.aliases.json`):
   `alias_string → entity_slug`. Regenerable parseando frontmatters al
   boot.

**Read-time:**

5. Extracción de entidades del query: regex/string match contra aliases
   index. NO LLM call.
6. Boost a entries post-cursor con tag matcheado.
7. Demote a entries pre-cursor con tag matcheado (su info está
   consolidada en la página).
8. La página canónica surface naturalmente vía vector search.

### Multi-factor ranking

El score final de cada candidato combina:

- **Vector similarity** (existing).
- **Entity tag match** (boost si el tag aparece en query entities).
- **Cursor position** (boost post-cursor, demote pre-cursor consolidado).
- **Recency** (boost si reciente — desempata cuando otros factores
  son iguales).
- **Weight implícito** (derivado de señales: frecuencia de referencia,
  posición en página, telemetría de uso). No es field explícito.

Un peso alto antiguo puede ganarle a un peso bajo reciente. Recency
es desempate, no dominante.

### Drill-down como operación

Desde cualquier resultado, el sistema puede traversar:

- `sources: [...]` → entries originales que contribuyeron al consolidado.
- Git history → versiones previas + razonamiento de cada cambio
  (commit messages).
- `consolidated_into: [...]` → desde un summary, hacia las entidades
  que se derivaron.
- Markdown links + frontmatter relations → entidades relacionadas.
- Archive subfolders → entidades absorbidas y su contenido pre-merge.

Implementación inicial: comando `durin memory expand <node>` que
formatea estos enlaces. Visualización Obsidian-style queda como
futuro habilitado.

### Medición direccional incluida

Para que la decisión L2+ futura sea empírica:

9. **Telemetría de retrieval** (~50 LOC + tabla/JSONL). Por query:
   `query_text`, `entities_extracted`, `candidates_returned`,
   `candidates_with_matching_tag_NOT_returned`, `llm_actually_referenced`.
10. **Test de embedding de variaciones de nombre** (one-off, ~1 hora).
    Script que mida cosine similarity entre pares conocidos.

### L2+ diferido

Las técnicas siguientes NO se adoptan ciegamente:

- Synonymy edges via cosine > 0.9 (HippoRAG soft, sin DB graph).
- Cross-encoder reranking post-vector (Graphiti).
- PageRank / traversal multi-hop (requiere sidecar de grafo).
- Page-first intent detection.
- LLM-based entity extraction del query.
- Bi-temporal validity per claim (`valid_from`/`valid_to` YAML — se
  reemplazó con prosa).
- Weight explícito por claim.
- Lifecycle automático con thresholds.
- Sub-paging por scope (mega-hub mitigation).

### Disparadores para reabrir L2+

- Telemetría (#9) muestra el bucket "no retornado" creciendo o
  respuestas del LLM degradando.
- Decisión binding de validar contra benchmark público.
- Corpus a escala donde graph traversal tiene superficie (>10k
  entidades, queries multi-hop frecuentes).

---

## §8 — Lo que ya existe en el código (no se rehace)

Cosas que la propuesta entity-centric **no reemplaza**:

- **Propuesta D — vector embed enriquecido**: ya implementado en
  Phase 2. Composición `headline + summary + entities + body` dentro
  del budget de 1500 chars. Se mantiene tal cual.
- **`_MEMORY_AUTHOR` ContextVar**: existe en el código actual.
  Distingue `agent_created` (dream puede tocar) vs `user_authored`
  (intocable). Único sobreviviente conceptual de propuesta C.
- **Cuatro clases de memoria** (`stable/episodic/corpus/pending`):
  se mantienen como están. Entity-centric agrega `entities/` como
  capa derivada, no reemplaza.
- **LanceDB para vector index**: se mantiene. El indexer agrega
  páginas de entidad al mismo índice; eventual de-indexado de
  archive subfolders es configuración del indexer.

---

## §9 — Lo que queda fuera de scope inicial

Cerrado intencionalmente. **No descartado para siempre**; listado
explícitamente para que cualquier reapertura sea consciente y con
disparador claro.

| Item | Razón de diferir | Disparador para reabrir |
|---|---|---|
| Edición manual del user sobre páginas | Complica modelo de autoría sin valor demostrado | Demanda explícita de uso real |
| Sync entre máquinas via git remote | Fuera de scope "memoria local" | Si alguien pide, opt-in explícito fuera de durin |
| Sidecar de grafo (SQLite con edges) | Costo de implementación + sin dolor medido | Telemetría muestra queries multi-hop fallando |
| L2+ retrieval | Sin ganador claro en benchmarks publicados | Telemetría #9 degrada, o validación binding contra benchmark |
| Bi-temporal YAML per claim | Markdown prose cubre temporalidad; git provee history | Necesidad de queries "qué creíamos sobre X el día Y" |
| Weight field explícito | Implícito por señales del ranker | Si emerge un caso donde el ranker subestima algo importante |
| Cross-system identity automática | Agujero universal sin solución probada | Aliases manuales por ahora |
| Sub-paging para mega-hub | No es problema hasta >200 claims | Cualquier entidad supera N (≈200, empírico) |
| LLM-based entity extraction del query | Regex sobre aliases cubre ~80% | Telemetría muestra fallas en queries con menciones implícitas |
| `pinned` opt-out / `_MEMORY_AUTHOR` enforced en dream | Dream actual no toca contenido user-authored | Cuando dream maduro empiece a sobrescribir |
| Visualización Obsidian-style | Out of scope inicial | Si emerge utilidad de inspección/debugging del grafo |

---

## §10 — Riesgos

### R1 — HyperMem (SOTA LoCoMo) gana sin entity nodes

**Evidencia**: HyperMem 92.73% LoCoMo sin entity nodes; Mem0 production
admite que el delta vs full-context es 6 puntos a 14x menos costo.

**Implicación**: si la promesa de durin fuera "QA accuracy en
conversaciones largas", entity-centric no se justifica. La promesa
real es **coherencia operativa cross-sesión** sobre identidades
persistentes y editability humana del corpus de memoria — ejes que
LoCoMo no testea.

**Mitigación**: outcomes operativos verificables documentados (ver §11).

### R2 — Mega-hub en `person:user` y `project:durin`

**Evidencia**: GAAMA documenta entidades centrales con cientos de edges
volviéndose hubs ruidosos.

**Implicación**: en 3-12 meses, `person:marcelo` y `project:durin`
acumularán cientos/miles de claims.

**Mitigación diferida** (no crítica hasta evidence): sub-paging por
scope cuando una entidad cruza N claims (sugerido N≈200) usando la
misma carpeta `entities/<type>/<slug>/`. Compresión periódica en
dream con archive de claims viejos en `<slug>/archive/`.

### R3 — Costo del dream no medido

**Evidencia**: ningún sistema de la muestra tiene número de referencia
confiable para "consolidar una sesión".

**Mitigación**: medir antes de Phase X. Punto de partida sugerido:
**si una consolidación promedio cuesta > $0.10/sesión con Haiku,
reevaluar batch / modelo**.

### R4 — Cross-system identity sin solución universal

**Evidencia**: ningún sistema resuelve "email vs git author vs nombre
conversacional" automáticamente.

**Mitigación**: aceptar limitado inicial. Aliases declarados manualmente
(via onboarding o edición de página si emerge demanda).

### R5 — LLM-driven entity resolution puede equivocarse

**Evidencia**: Fountain City blog reporta casos donde el LLM decide mal
una unificación.

**Mitigación**: pipeline dedup en cascada — regla determinista primero
(exact match contra slug + aliases), LLM solo en zona gris.

---

## §11 — Outcomes operativos verificables

Cómo sabemos que el modelo funciona (defensa empírica del entity-centric):

- "Después de 10 sesiones tocando `project:durin`, una pregunta tipo
  '¿qué decisiones tomamos sobre embeddings?' debe encontrarse en
  `entities/project/durin.md` sin grep sobre todo `episodic/`."
- "Si user dice 'soy Marcelo' en sesión 1 y 'mmarmol@mxhero.com' en
  sesión 5, ambas deben caer en `entities/person/marcelo.md` sin
  intervención manual (cubierto por aliases array)."
- "Si 3 sesiones distintas mencionan un bug recurrente, debe aparecer
  una página `entities/event/<bug-id>.md` consolidada con causa + fix."
- "`durin memory history entities/person/marcelo.md` debe mostrar el
  diff exacto + razonamiento del dream para cada revision."
- "`durin memory expand entities/person/marcelo.md` debe listar los
  episodic entries fuente, las revisiones previas, y las entidades
  relacionadas."

Estos outcomes son testables. Si no se cumplen, el modelo no entrega
su promesa.

---

## §12 — Dependencias para implementación

| Dep | Qué es | Bloqueante de | Estado |
|---|---|---|---|
| **Propuesta A — typed entities** (doc 14) | `entities: [type:slug, ...]` en frontmatter de entries episódicas. Formato `type:value`, vocabulario abierto, validación de forma | §7 L1 light (sin esto no hay tag matching barato) | Documentada en doc 14, sin implementar |
| **Compaction summary indexing** | Vectorizar el resumen de compactación como entry indexable | §6 trigger del dream | Mencionado en doc 16, sin implementar |
| **`memory_write` meta event** | Telemetría/hook que dispara al escribir a memory | §7 medición direccional + §5 commit automático | Sin implementar |
| **Cursor `dream_processed_through_msg_idx`** | Estado per-session para tracking | §6 y §7 retrieval boost/demote | Sin implementar |
| **Idle-timer session-close summary** | Trigger natural fin de actividad | §6 trigger del dream | Sin implementar |
| **Git init + commit infrastructure** | `memory/.git/`, helpers para commits con trailers | §5 versionado | Sin implementar |
| **Entity page parser** | Lee frontmatter + secciones | §4 schema, §7 alias index | Sin implementar |
| **Dream prompt template** | Input al LLM: entries nuevos + página actual + instrucciones | §6 | Sin implementar |
| **`durin memory` commands** | history, diff, revert, expand | §5 + §7 drill-down | Sin implementar |

---

## §13 — Próximos pasos

No se decide orden en este doc. Menú:

- **Path A — Foundations primero**: propuesta A + git init + dream
  prompt template. Base que habilita el resto. Sin valor user-visible
  hasta cierre.
- **Path B — Vertical slice corta**: implementar end-to-end para una
  sola entidad (e.g., `person:marcelo`), atravesando A+§6+§7+§5.
  Demuestra el modelo completo en chico antes de escalar.
- **Path C — Solo telemetría + medición**: implementar #9 y #10 de §7
  ANTES de cualquier código del dream. Acumular datos sobre el uso
  actual para validar que L1 light era necesario.
- **Path D — Esperar más uso del estado actual**: no implementar nada
  entity-centric hasta tener N semanas más de uso con la memoria
  actual.

---

## Last updated: 2026-05-23 (post-discusión Q1-Q6 + visión sustrato conectado)
