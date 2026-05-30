# 14 — Entidades tipadas en memoria

> Mayo 2026. Propuesta de diseño para tipar las entidades que hoy
> circulan como `list[str]` plano en `MemoryEntry.entities`, en
> `<key>.meta.json::derived.tags.entities` y en
> `ingested/<id>/meta.json::derived.entities`. El documento analiza
> cómo lo resuelven los sistemas que ya estudiamos (OpenClaw,
> Hermes con sus 8 providers), confronta una propuesta tentativa
> de 9 tipos contra esa evidencia y cierra con una lista
> recomendada y el plan de encaje en el código actual de Phase 1 +
> Phase 2.
>
> **Nota de supersession (2026-05-23)**:
>
> - §3.1 Formato (`type:value`) y §3.2 Validación (forma, no
>   vocabulario) **siguen vigentes** y son la mecánica que se
>   implementa.
> - §3.3 Tipos recomendados (lista de 9 prescriptivos) **fue
>   superseded por doc 18 §4**: el set sugerido pasó a 8 tipos
>   amplios cross-profession (`person, place, project, topic, event,
>   artifact, stance, practice`), con vocabulario abierto + tipos
>   sugeridos (no enforced). Doc 14 §3.3 queda como traza histórica
>   del proceso.
> - §3.4 Mecanismo de extensión: la frase "Tipos fuera del set
>   recomendado son legales pero indeseables" se invierte — bajo
>   doc 18, tipos fuera del set son **bienvenidos** y emergen
>   naturalmente (Phase 0.3 lo confirmó: el LLM extendió con `agent:`,
>   `org:` sin pedirlo).
> - §3.5 Backward compatibility, §3.6 Helpers, §4 Encaje con código
>   **siguen vigentes** y guían la implementación de Phase 1.1.

---

## §1 — El problema y por qué importa ahora

Hoy, `durin/memory/schema.py:38` define:

```python
entities: list[str] = Field(default_factory=list)
```

El campo aparece replicado en tres sitios independientes con el mismo
shape:

- Frontmatter de `memory/<class>/<id>.md` — escrito por `store_memory`
  (`durin/memory/store.py:43`).
- Tags derivadas del consolidator en `sessions/<key>.meta.json::derived.tags.entities`
  — pobladas por `parse_consolidator_response`
  (`durin/memory/consolidator_tags.py:62`).
- Sección `derived` de cada artefacto ingerido en `ingested/<id>/meta.json`
  (`durin/memory/ingestion.py:74`).

El hot layer las agrega y las imprime como CSV plano al final de la
sección estable del prompt:

```python
# durin/memory/hot_layer.py:50
csv = ", ".join(self.entities)
parts.append(f"## Memory: Known Entities\n\n{csv}")
```

Y `search.py` matchea entidades por substring case-insensitive
contra el query plano (`durin/memory/search.py:139`).

**Qué se pierde**: `marcelo`, `durin`, `durin/agent/loop.py`,
`docs/bitacora.md`, `glm-5.1`, `phase 2`, `release v0.1.0a7`,
`autocompact`, `posture vector` son todos strings idénticos desde el
punto de vista del schema. No hay forma de filtrar "dame las
personas mencionadas" ni de saber si `marcelo` es una persona o un
nombre de archivo. Tampoco hay forma de promover el grafo a tablas
relacionales sin un paso de clasificación retroactiva sobre el corpus.

**Por qué ahora**:

1. **Phase 3 (KG) ya está nominalmente en el plan**
   (`docs/08_memory_phase2_proposal.md:626`): "SQLite KG (entities +
   triples with `valid_from` and `source_ref`)". Esa tabla quiere
   `entity(id, type, name)`, no `entity(id, name)`. Si llegamos sin
   tipo, la migración es: leer N markdowns, mandarlos al LLM, pedir
   "clasifica cada string" — costoso, ruidoso, y la respuesta
   tampoco es perfectamente reproducible.

2. **El consolidator y el dream son los productores naturales**.
   El consolidator ya invoca al modelo y le pide YAML estructurado
   (`durin/templates/agent/consolidator_archive.md:15-17`). Cuesta
   nada extender el prompt para que emita `type:value`. El dream
   (Phase 3) hará lo mismo sobre `ingested/<id>/`. Si el formato lo
   acordamos antes de que el dream exista, no hay deuda técnica.

3. **El hot layer está perdiendo señal**. Hoy imprime una
   CSV alfabética sin contexto. Con tipos podríamos imprimir secciones
   (`Personas: marcelo`, `Proyectos activos: durin, hermes-agent`) o,
   más útilmente, dejar fuera tipos voluminosos y poco informativos
   (`file:durin/agent/tools/_telemetry.py` no necesita estar siempre
   en el prompt — la consulta lo encuentra cuando hace falta).

**Tradeoff explícito que el usuario marcó**: "quedar estacionarios"
vs "costo de migración retroactiva". El planteo es real: cada vez
que añadimos opinionatedness al schema dejamos un compromiso que
hay que mantener. La línea que recomienda este documento es:

- **El formato es estricto** (`type:value`, regex de forma).
- **El vocabulario de tipos es abierto** (no `Literal[...]`,
  solo un set _recomendado_ documentado).
- **El productor canónico es el LLM**, no un clasificador
  determinista. Si la propuesta resulta equivocada, lo único que
  cambia es el prompt del consolidator y la lista de ejemplos en
  el doc. No hay tabla SQL ni enum compilado que rehacer.

Esa estructura mantiene la opcionalidad: si en seis meses descubrimos
que necesitamos `incident` y `event` separados (o, al revés, que
sobra `decision`), el cambio es local al prompt + un grep sobre
`memory/`. No exige una migración global.

---

## §2 — Cómo lo resuelven los sistemas que estudiamos

### 2.1 OpenClaw — memory-lancedb (categoría como enum cerrado a la entrada)

Definición en `/Users/marcelo/git_personal/openclaw/extensions/memory-lancedb/config.ts:23-24`:

```ts
export const MEMORY_CATEGORIES = ["preference", "fact", "decision", "entity", "other"] as const;
export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];
```

El schema de LanceDB tiene `category: MemoryCategory` como columna
plana en cada fila de `memories`
(`/Users/marcelo/git_personal/openclaw/extensions/memory-lancedb/index.ts:40,235-245`).
No hay sub-tipos ni jerarquía: cada fila lleva exactamente una
categoría dentro del enum cerrado.

**Cómo se asigna**:

- El tool `memory_store` (`index.ts:753-768`) acepta `category?: MemoryCategory`
  con `default = "other"`. El modelo puede pasarla explícitamente.
- En auto-capture (`index.ts:1101`), llama `detectCategory(text)`
  para asignarla automáticamente. La función
  (`index.ts:610-627`) es un cascade de regex multilingües:

  ```ts
  if (/prefer|radši|like|love|hate|want|喜欢|偏好/i.test(lower)) return "preference";
  if (/decided|will use|决定|これから/i.test(lower)) return "decision";
  if (/\+\d{10,}|@[\w.-]+\.\w+|is called/i.test(lower)) return "entity";
  if (/is|are|has|have|je/i.test(lower)) return "fact";
  return "other";
  ```

  Esto es exactamente la heurística determinista que vamos a evitar
  abajo — funciona para frases en lenguaje natural ("I prefer", "we
  decided") pero rompe contra nombres propios mezclados.

**Cómo se usa para retrieval**:

- En `memory_recall` (`index.ts:728-733`), la categoría se imprime
  como prefijo decorativo en la salida del tool al modelo:

  ```ts
  `${i + 1}. [${r.entry.category}] ${r.entry.text} (${(r.score * 100).toFixed(0)}%)`
  ```

  No filtra el ranking. El score sigue siendo similitud vectorial pura.
- El `formatRelevantMemoriesContext` (`index.ts:553-556`) que se
  inyecta en el prompt también imprime `[category]` como prefijo.

**Cómo se promueve en dreaming**: aquí lo importante es que la
**categoría no participa del scoring**. El dream (en
`memory-core/src/dreaming.ts`) puntúa por frequency / relevance /
diversity / recency / consolidation / conceptual (ver
`docs/08_memory_phase2_proposal.md:955-967`). La categoría se
arrastra por ser una columna del registro, no porque influencie la
promoción. En `memory-core/src/concept-vocabulary.ts` existe un
mecanismo paralelo de "concept tags" derivado de tokens y vocabulary
(`deriveConceptTags`, línea 399), totalmente _no tipado_ —
strings normalizados con stop-words filtradas. Eso es lo que el
dream sí usa para clustering.

**Lección para durin**:

- Lo bueno: tener categoría como columna en cada registro es
  barato, no impone overhead de retrieval y sirve para filtrar
  ergonómicamente (`list_facts(category="preference")` en el sister
  store).
- Lo malo: **el enum cerrado** (`preference|fact|decision|entity|other`)
  es la decisión que nos quita opcionalidad. Si descubrimos que
  queremos diferenciar `person` de `tool`, hay migración. El `detectCategory`
  por regex es heurística superficial — no escala más allá de
  inglés conversacional y se rompe con frases mixtas.
- **Adoptar**: la idea de **una categoría plana por entrada**
  como decoración del retrieval.
- **Descartar**: enum cerrado y `detectCategory` determinista.
  Reemplazar por vocabulario abierto + producción LLM.

Nota adicional: OpenClaw mezcla _category de la memoria_ (de la
entrada en su conjunto) con _entidad como categoría_ (la opción
`"entity"` significa "esta memoria habla de una entidad"). Esa
ambigüedad muestra el costo de no separar **tipo del item** de
**tipo de las entidades dentro del item** — un item puede ser una
`decision` que menciona `person:marcelo`. En durin son dos campos
distintos: la `class` de la memoria (`stable | episodic | corpus |
pending`, `durin/memory/paths.py:33`) ya existe, y este doc trata
solo del campo `entities`. No confundirlos.

### 2.2 OpenClaw — memory-core (concept tags como vocabulario abierto)

`/Users/marcelo/git_personal/openclaw/extensions/memory-core/src/concept-vocabulary.ts`
expone `deriveConceptTags({path, snippet, limit})` que devuelve
`string[]` plano (línea 399). El proceso (líneas 412-422):

1. Recolecta `collectGlossaryMatches`, `collectCompoundTokens`,
   `collectSegmentTokens` sobre `${basename(path)} ${snippet}`.
2. Normaliza con lowercase, filtra contra `LANGUAGE_STOP_WORDS`
   (líneas 15-110) que incluyen inglés, español, francés, etc.
3. Tope `MAX_CONCEPT_TAGS = 8` (línea 4).

Es exactamente el patrón "tags planas" sin tipado. Lo usan para
clustering en el dream:

```ts
// dreaming.ts referencia concept-vocabulary
summarizeConceptTagScriptCoverage(conceptTagsByEntry)
```

para detectar si el corpus es latino, CJK o mixto.

**Lección para durin**: este es el caso donde **no tipar** es la
respuesta correcta — son tokens derivados léxicamente para
agrupación, no entidades nombradas con cardinalidad estable. El
campo `topics: list[str]` que ya tenemos en
`<key>.meta.json::derived.tags.topics` (consolidator_tags.py:63) es
exactamente este patrón y debe quedarse plano. **Lo que tiparemos es
solamente `entities`**, no `topics`.

### 2.3 Hermes — Holographic (SQLite con `entity_type` declarado pero nunca asignado)

Schema en
`/Users/marcelo/git_personal/hermes-agent/plugins/memory/holographic/store.py:30-46`:

```sql
CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

La columna `entity_type` existe — pero el path de write
(`_resolve_entity`, líneas 433-461) **nunca la setea**: cuando crea
una entidad nueva (línea 457-458) hace `INSERT INTO entities (name)
VALUES (?)` y deja `entity_type` con su default `'unknown'`.
Tampoco hay un update path que lo cambie.

El `_extract_entities` (líneas 398-431) es 4 reglas regex:

1. Frases capitalizadas multi-palabra (`John Doe`).
2. Términos entre comillas dobles (`"Python"`).
3. Términos entre comillas simples (`'pytest'`).
4. Patrón AKA (`Guido aka BDFL` → dos entidades).

Y luego `_resolve_entity` busca por nombre o por aliases.

Por separado, **las facts** sí tienen `category TEXT DEFAULT 'general'`
(línea 20) y un índice `idx_facts_category` (línea 45). El schema
del tool expone un enum cerrado de categorías de fact
(`__init__.py:66`):

```python
"category": {"type": "string", "enum": ["user_pref", "project", "tool", "general"]}
```

**Lección para durin**: Holographic muestra el anti-patrón explícito.
Declarar `entity_type` "por si acaso" y no llenarlo nunca produce
una columna `'unknown'` para el 100% de las filas — peor que no
tener la columna, porque sugiere capacidad que no se ejerce. Si
tipamos, **lo tipamos en el productor** (consolidator + dream), no
solo en el schema. Esto refuerza la dirección que este doc
recomienda: el LLM emite el tipo en el mismo paso en el que extrae
la entidad. Sin productor, no hay tipo.

### 2.4 Hermes — OpenViking (enum de "categoría de memoria" abierto al modelo)

`/Users/marcelo/git_personal/hermes-agent/plugins/memory/openviking/__init__.py:285-289`:

```python
"category": {
    "type": "string",
    "enum": ["preference", "entity", "event", "case", "pattern"],
    "description": "Memory category (default: auto-detected).",
}
```

Cinco categorías. La extracción se delega al servicio remoto
OpenViking que "automatically extracts 6 categories of memories:
profile, preferences, entities, events, cases, and patterns"
(líneas 589-590, también en el docstring `on_session_end`).

Note la diferencia con LanceDB:

- LanceDB: 5 categorías a nivel `MemoryEntry` (un texto, una categoría).
- OpenViking: 5 categorías nominalmente, pero el modelo conceptual
  remoto es más rico (6 con "profile"). El cliente Hermes solo expone
  un subset al tool.

En el `_tool_remember` (línea 856-878) la categoría es un _hint_
para el extractor remoto:

```python
text = f"[Remember — {category}] {content}"
```

Se inyecta como prefijo textual al mensaje que se manda a
OpenViking. El backend remoto re-clasifica internamente.

**Lección para durin**:

- El patrón "categoría es un hint textual en el contenido, no un
  campo dedicado" es atractivo por simplicidad, pero pierde la
  capacidad de filtrar/contar. No lo recomendamos.
- Cinco categorías a nivel item (memoria) es comparable a las 4
  classes que durin ya tiene (`stable|episodic|corpus|pending`).
  Confirma que el espacio "tipo de item" es pequeño y discreto. El
  que estamos tipando es distinto: cada item tiene N entidades, y
  cada entidad tiene un tipo.

### 2.5 Hermes — Hindsight, Mem0, Supermemory, RetainDB, ByteRover (delegan a backend remoto)

- **Hindsight** (`plugins/memory/hindsight/__init__.py:241-260`):
  el tool `hindsight_retain` acepta `content`, `context` y `tags`,
  pero ningún campo "type" para entidades. El servidor remoto
  "automatically extracts structured facts, resolves entities, and
  indexes for retrieval" (línea 244-245). La tipificación —
  si existe — es interna al servicio Hindsight.
- **Mem0** (`plugins/memory/mem0/__init__.py:374-388`):
  `metadata: dict` libre. Sin enum.
- **Supermemory** (`plugins/memory/supermemory/__init__.py:135-163`):
  `content`, `query`, sin categoría. Server-side.
- **RetainDB** (`plugins/memory/retaindb/__init__.py:471-528`):
  el cliente solo manda `content + entity_context: str` libre como
  prefijo de extracción. Sin enum visible.
- **ByteRover** (`plugins/memory/byterover/__init__.py:126-139`):
  delega al CLI `brv` que opera sobre un árbol jerárquico de
  contexto. No expone tipos.

**Lección para durin**: 5 de los 8 providers de Hermes _no tipan
entidades_ en su interfaz. Tipar es **menos común** en producción
de lo que el doc 08 sugiere. Los que sí tipan (LanceDB, OpenViking,
Holographic) o tipan el item (no la entidad), o declaran la
columna y no la usan.

Eso refuerza dos puntos:

1. Tipar entidades no es un patrón consensuado de la industria.
2. Los que más se acercan a un grafo (Holographic) lo hacen con
   `name` + `aliases` pero sin tipo significativo.

Si tipamos, lo haremos porque la trayectoria a Phase 3 KG lo justifica
internamente, no porque sea estándar.

### 2.6 Hermes — agent/curator.py + background_review.py

Lecturas con `grep -n "entity\|category\|kind"`:

- `/Users/marcelo/git_personal/hermes-agent/agent/curator.py` — sin
  matches en esos términos.
- `/Users/marcelo/git_personal/hermes-agent/agent/background_review.py` —
  un único match en la línea 302 ("provenance metadata for external
  memory-provider mirrors"), sin tipado de entidades.

El bg-review fork decide _qué archivo de skill/memory_ tocar; no
clasifica entidades. Esa decisión queda implícita en el contenido
markdown que el modelo escribe. Esto confirma: en Hermes, **toda
la inteligencia de tipado** vive en el provider de memory, no en
el loop de auto-mejora.

**Lección para durin**: cuando lleguemos al equivalente (Phase 3
dream + curator), no carguemos al curator con clasificar entidades
del corpus. El productor canónico (consolidator + dream) ya las
emitió tipadas. El curator solo gestiona ciclo de vida.

### 2.7 Tabla resumen — quién tipa qué

| Sistema | Tipa entidades nombradas | Tipa el item de memoria | Vocabulario | Productor del tipo |
|---|---|---|---|---|
| OpenClaw memory-lancedb | No | Sí (enum cerrado 5) | `preference|fact|decision|entity|other` | LLM (opcional) + regex (auto) |
| OpenClaw memory-core (concept tags) | No tipa, tokens planos | n/a | abierto | léxico (vocab + stop words) |
| Hermes Holographic | Declarado, nunca asignado | Sí (enum cerrado 4) | facts: `user_pref|project|tool|general`; entities: `'unknown'` siempre | regex superficial |
| Hermes OpenViking | No (categoría aplica al item) | Sí (enum cerrado 5) | `preference|entity|event|case|pattern` | servidor remoto |
| Hermes Hindsight | Backend opaco | No | n/a | servidor remoto |
| Hermes Mem0 | Metadata libre | No | n/a | servidor remoto |
| Hermes Supermemory | Backend opaco | No | n/a | servidor remoto |
| Hermes RetainDB | Backend opaco | No | n/a | servidor remoto |
| Hermes ByteRover | Backend opaco | No | n/a | CLI externa |
| durin (hoy) | No | Sí (`stable|episodic|corpus|pending`) | n/a | n/a |

Conclusión cruzada: el espacio "tipo del item de memoria" es bien
estudiado (3 sistemas con enums cerrados de 4–5 valores). El
espacio "tipo de la entidad dentro del item" es **terreno mucho
menos pisado** — Holographic lo intentó y no lo llenó; nadie más
lo expone.

Ese hueco es una señal de dos cosas:

- Tipar entidades a la entrada es difícil sin un productor
  confiable. (En 2026, ese productor _existe_: es el LLM.)
- El valor solo aparece si hay un consumidor que sepa qué hacer
  con el tipo. (Para durin, ese consumidor es Phase 3 KG.)

---

## §3 — Diseño propuesto

### 3.1 Formato

Cada elemento de `entities` es un string con la forma:

```
<type>:<value>
```

Donde:

- `<type>` está en lowercase, solo `[a-z][a-z0-9_]*`, longitud ≥ 1.
- `:` es el separador.
- `<value>` es texto arbitrario no vacío (puede contener `:`,
  espacios, puntuación). El primer `:` separa; el resto pertenece
  al valor.

Ejemplos válidos:

```
person:marcelo
project:durin
file:durin/agent/loop.py
symbol:MemoryEntry
topic:autocompaction
decision:dropped-posture-vector
incident:webui-crash-2026-05-15
tool:memory_store
event:release-v0.1.0a7
model:glm-5.1
```

Ejemplos inválidos (no cumplen forma):

```
marcelo                    # falta type:
:value                     # type vacío
Person:Marcelo             # type no es lowercase
person :marcelo            # espacio en type
123type:value              # type no empieza con letra
my-type:value              # guión no permitido en type (sí en value)
```

### 3.2 Validación

Una sola función pura, llamada desde tres puntos (store, ingest,
consolidator-tags):

```
^[a-z][a-z0-9_]*:[^\s].*$
```

Reglas explícitas:

- Si un string no matchea, **se rechaza con error** (no se
  silenciosamente normaliza, no se rellena con `unknown:`). En el
  consolidator-tags el rechazo es lenient — el parser ya
  silencia errores YAML (`durin/memory/consolidator_tags.py:55-57`)
  y debe seguir haciéndolo: una entry con `entities` malformadas se
  descarta la lista, no rompe el turn.
- En `memory_store` (write path explícito del agent), el rechazo
  es estricto — devuelve `{"error": "invalid entity format: ..."}`.
  El modelo recibe el mensaje y reescribe.
- En `store_memory` directo (path Python), `StoreError`.

**Por qué solo validar forma, no vocabulario**: si validamos
vocabulario tenemos un enum cerrado. Es exactamente lo que queremos
evitar. La validación de forma cubre el costo real (no perder la
estructura) sin imponer opcionalidad cero.

### 3.3 Tipos recomendados (set inicial)

Justificación tipo-por-tipo basada en §2 + bitácora de durin.
El set inicial debe ser pequeño — el LLM lo memoriza del prompt.
Cada tipo que añadimos al recommended set tiene costo en el
prompt del consolidator.

| Tipo | Justificación de inclusión |
|---|---|
| `person` | Cubre el caso canónico que el doc 08 §0c.5 ya usa: `usuario:marcelo`. Universal en todos los sistemas estudiados (LanceDB lo cubre con su `entity`; OpenViking con `profile`/`entity`). |
| `project` | El doc 08 también lo ejemplifica: `proyecto:durin`. La bitácora habla de `durin`, `hermes-agent`, `openclaw` constantemente — son entidades de primera clase del corpus. |
| `file` | `docs/bitacora.md:411` es un ejemplo típico de source_ref que aparece como entidad. Distinto de `symbol` porque el grano es el archivo entero, no la función. |
| `symbol` | El bitácora menciona `Consolidator.archive`, `MemoryEntry`, `format_summary_block` (líneas 417, 707) — funciones/clases con identidad estable. Distinto de `file` por granularidad. |
| `topic` | Tema conceptual recurrente que no es ni código ni persona: `autocompact`, `posture vector`, `plan tier`, `phase 2`. El doc 08 separa `topics: list[str]` plano en meta.json, pero el _contexto del topic_ puede aparecer también como entidad nombrada en el frontmatter de una memoria. |
| `decision` | Patrón muy común en bitácora (`Discarded: Posture Vector`, `Discarded: Plan System`). Importante distinguirlo de `event` porque una decisión tiene contraparte de "qué se descartó/adoptó" — Phase 3 KG querrá un edge `(decision, supersedes, decision)`. |
| `incident` | Bugs o fallos con identidad temporal: `webui-crash-2026-05-15`, `nfs-wal-fallback`. La bitácora líneas 652+ documenta varios. Phase 3 KG querrá `(incident, in, file)`. |
| `tool` | Cada tool de durin (`memory_store`, `memory_search`, `read_file`) es una entidad nombrada con identidad estable y aparece en `tools/` directory. Phase 3 puede querer `(tool, used_in, decision)`. |
| `event` | Ciclo de vida con timestamp: releases (`release-v0.1.0a7`), pivots (`pivot-cognitive-to-context-orchestration`). La bitácora documenta varios eventos discretos con `valid_from` que ya está en MemoryEntry. |

**Confrontación con el set tentativo de 9**: el set arriba **es el
mismo** que los 9 propuestos en conversación. Tras el análisis de
§2 no encontré evidencia para añadir o quitar uno. Notas:

- `model:glm-5.1` cabe semánticamente en `tool` (un modelo es la
  herramienta del runtime); no proponemos un tipo `model` separado
  inicialmente. Si la cardinalidad de modelos referenciados se
  vuelve alta (no parece), revisitar.
- `release` cabe en `event`. No proponemos tipo separado.
- `commit` o `pr` específicos del workflow git — la bitácora los
  menciona pero el grano de identidad es bajo. Cabrían en `event`
  si surgen. No los anclamos en el set inicial.
- Eliminar `incident`: tentación de fusionar con `event`. Mi
  recomendación es **mantenerlos separados**: un incident tiene
  contraparte "qué se rompió + qué lo arregló", un event no
  necesariamente. Phase 3 los modelará distinto.

### 3.4 Mecanismo de extensión

Tipos fuera del set recomendado son **legales pero indeseables**.
El sistema:

- Acepta `meeting:standup-2026-05-23` aunque `meeting` no esté
  en el set recomendado — pasa el regex de forma.
- Pero no aparece en la lista de "tipos sugeridos" del prompt del
  consolidator.

Para promover un tipo nuevo a "recomendado" basta:

1. Editar este doc.
2. Editar el prompt del consolidator
   (`durin/templates/agent/consolidator_archive.md`) para
   añadirlo a la lista de tipos sugeridos.
3. Opcionalmente, regenerar (o dejar que el dream regenere) el
   corpus existente — pero no es obligatorio. Las entidades viejas
   con tipo antiguo siguen siendo legales.

Eso resuelve el "quedar estacionarios" sin pagarlo: el coste de
introducir un tipo nuevo es ~3 líneas de prompt. Si el tipo nuevo
no rinde (no aparece en las salidas del modelo), retroceder
también es ~3 líneas.

### 3.5 Backward compatibility con entries existentes sin tipo

El corpus actual (Phase 1 + 2 ya aterrizadas) tiene entradas con
strings planos. Tres reglas:

1. **Lectura**: `_entry_matches` (`search.py:139`) y el hot layer
   (`hot_layer.py:126`) aceptan strings de ambas formas. Un string
   sin `:` se trata como entidad sin tipo conocido. No se rechaza
   en lectura.
2. **Escritura**: a partir del cambio, todos los writes nuevos
   deben usar el formato tipado. El consolidator regenerará
   entradas con tipo a partir de las que va consolidando.
3. **Migración pasiva**: no migramos retroactivamente el corpus
   en un solo script. Cuando una entrada vieja sea consolidada
   o consultada por dream (Phase 3), el dream emite la versión
   tipada y reemplaza. Ergo: en sistemas con uso real, el corpus
   se _tipifica naturalmente_ en semanas. En sistemas fríos, las
   entries sin tipo conviven indefinidamente sin romper nada.

Ese "no hay flag day" es el motivo por el que recomendamos un
formato lenient en lectura. Cualquier otra decisión (rechazo en
lectura, migración con script) costaría más operativamente que el
beneficio que produce.

### 3.6 Helpers que añadiremos al schema

Pequeña superficie de Python para no duplicar lógica:

```
durin/memory/entities.py  (nuevo módulo, ~50 líneas)
  ENTITY_RE: compiled regex
  RECOMMENDED_TYPES: tuple[str, ...] (los 9)
  split_entity(s: str) -> tuple[str, str] | None
    # ("person", "marcelo") o None si malformada
  validate_entity(s: str) -> bool
  filter_by_type(items: list[str], type_name: str) -> list[str]
```

No se exporta nada más. La validación de pydantic se hace con un
`@field_validator` sobre `MemoryEntry.entities` que llama a
`validate_entity` por cada elemento. En modo strict (toolcall) se
rechaza; en modo lenient (read), pydantic ya hace `extra='forbid'`
pero la entrada no se valida en lectura — el flujo es:
`split_frontmatter → load_entry`, y `load_entry` puede aplicar la
política de no rechazar entries existentes si lo decidimos
construyendo con `model_validate` en modo no-estricto. Detalle de
implementación lo cierra el PR.

---

## §4 — Encaje con el código actual de durin

Lista archivo por archivo. **Contrato actual / contrato propuesto / por qué**.

### `durin/memory/schema.py`

- **Antes**: `entities: list[str]` sin validación.
- **Después**: mismo tipo (no cambia firma), validador a nivel
  campo que llama a `entities.validate_entity` por cada elemento.
  Modo: strict por defecto (writes), lenient opt-in (reads de
  corpus legacy).
- **Por qué**: queremos detectar entries malformadas en el write
  path sin romper compatibilidad con entries viejas en read path.

### `durin/memory/entities.py` (nuevo)

- **Antes**: no existe.
- **Después**: ~50 líneas. Regex, validator, helpers.
- **Por qué**: un solo sitio para la verdad del formato. Sin este
  módulo, la lógica se duplica en consolidator-tags + store +
  ingest.

### `durin/memory/store.py`

- **Antes**: acepta `entities: list[str] | None = None` sin
  validar (línea 43).
- **Después**: valida cada elemento contra `validate_entity`. Si
  falla, raise `StoreError("invalid entity: '<value>' — expected
  '<type>:<value>'")`.
- **Por qué**: el agent que llama `memory_store` recibe el error y
  puede reintentar con el formato correcto.

### `durin/agent/tools/memory_store.py`

- **Antes**: la docstring del parámetro `entities` dice "Optional
  list of named entities this memory references"
  (línea 47-49). No menciona formato.
- **Después**: la docstring describe el formato (`type:value`) con
  3-5 ejemplos cortos. El modelo lo lee y emite formato
  correcto desde la primera llamada.
- **Por qué**: el prompt del tool es el único canal para
  comunicarle al modelo la convención.

### `durin/memory/consolidator_tags.py`

- **Antes**: `parse_consolidator_response` ya devuelve
  `{"entities": [...], "topics": [...]}` (línea 62) con coerción
  lenient a string. No valida formato.
- **Después**: tras coercion, filtra entidades malformadas con un
  `validate_entity`. Logs un warn por entidad descartada. La
  estructura del retorno no cambia.
- **Por qué**: el consolidator es un productor canónico. Si el
  modelo no respeta el formato, queremos quedarnos solo con las
  entries válidas — no rechazar el tag block completo y perder
  los `topics` también.

### `durin/templates/agent/consolidator_archive.md`

- **Antes**: instruye `entities: list of named entities ... use
  the names exactly as they appeared` (líneas 16-17).
  Ejemplo en línea 23: `entities: [marcelo, durin, cache-layer]`.
- **Después**: el prompt cambia a:
  - Descripción: "list of named entities, each formatted as
    `type:value` (lowercase type, no spaces)".
  - Recomienda los 9 tipos del set inicial con una línea por tipo.
  - Ejemplo actualizado a `entities: [person:marcelo,
    project:durin, decision:dropped-cache-layer]`.
- **Por qué**: el prompt es la única vía de producción. Si no
  cambia el prompt, el modelo sigue emitiendo strings planos.

### `durin/memory/hot_layer.py`

- **Antes**: agrega entidades como CSV ordenada alfabéticamente
  (líneas 110-128, 50-51).
- **Después** (cambio mínimo):
  - Sigue ordenando, sigue mostrando como CSV.
  - Lectura usa `split_entity` para extraer el `value` cuando hay
    tipo (display: `marcelo`, no `person:marcelo`) en la primera
    iteración. Mantiene el `type:value` interno en
    `HotLayer.entities` por si un consumidor downstream lo
    necesita.
- **Después** (cambio expansivo, opcional para una v2):
  - Particiona por tipo. Imprime secciones:
    - `Personas: marcelo, sergio`
    - `Proyectos: durin, hermes-agent`
    - `Decisiones: dropped-posture-vector, adopted-permission-modes`
  - Aplica budget por tipo (people primero, files al final).
- **Por qué**: empezar con el cambio mínimo evita acoplar el
  refactor de display al cambio de schema. La v2 (secciones) es un
  follow-up evaluable empíricamente con el corpus ya tipado.

### `durin/memory/search.py`

- **Antes**: `_entry_matches` y `_tag_match` matchean substring en
  el string crudo (líneas 139, 213-216).
- **Después**:
  - Si el needle es de la forma `type:value`, hace match exacto
    sobre `(type, value)`. Esto permite `memory_search(query=
    "person:marcelo")` con semántica precisa.
  - Si el needle es plano (sin `:`), se conserva el comportamiento
    actual: substring contra `value` (el `type` no contamina
    matches falsos).
- **Por qué**: las entidades tipadas habilitan queries más
  ergonómicas; no romper queries planas para no forzar al modelo
  a aprender la sintaxis exacta el día que actualizamos.

### `durin/memory/ingestion.py`

- **Antes**: inicializa `derived.entities: []` y `derived.relations:
  []` vacíos (línea 74).
- **Después**: sin cambio inmediato. Cuando Phase 3 (dream) llene
  esos campos, lo hará en formato tipado por contrato.
- **Por qué**: la ingestion no produce entidades hoy; solo
  esqueleta el meta.json. Cambiar nada hoy, contrato documentado
  para el dream.

### `durin/memory/vector_index.py`

- **Antes**: el record tiene `id, class_name, summary, headline,
  vector, valid_from, path` (línea 5). Las entidades no se indexan
  en LanceDB (solo en markdown).
- **Después**: sin cambio. La sugerencia futura (Phase 3) sería
  añadir una columna `entity_types: list[str]` o un join con la
  tabla del KG, pero está fuera del scope de esta propuesta.
- **Por qué**: el vector index responde a búsqueda semántica
  por embedding; tipos de entidad son filtros estructurados que
  caben mejor en SQLite.

---

## §5 — Implicaciones para Phase 3 (KG)

`docs/08_memory_phase2_proposal.md:626` dice: "SQLite KG (entities
+ triples with `valid_from` and `source_ref`). Tool `kg_query(entity,
as_of=None)`. Schema migration from memory entry frontmatter".

Con el set tipado de §3.3, el schema natural es:

```sql
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    value       TEXT NOT NULL,
    first_seen  TEXT NOT NULL,        -- ISO date
    UNIQUE (type, value)
);
CREATE INDEX idx_entities_type ON entities(type);
CREATE INDEX idx_entities_value ON entities(value);

CREATE TABLE triples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id  INTEGER NOT NULL REFERENCES entities(id),
    relation    TEXT NOT NULL,
    object_id   INTEGER NOT NULL REFERENCES entities(id),
    valid_from  TEXT NOT NULL,
    source_ref  TEXT,                  -- markdown link to memory entry
    UNIQUE (subject_id, relation, object_id, valid_from)
);
CREATE INDEX idx_triples_subject ON triples(subject_id);
CREATE INDEX idx_triples_object ON triples(object_id);
```

Migración desde markdown:

```
for each memory/<class>/<id>.md:
  for each entity in entry.entities:
    if entity is "type:value":
      INSERT OR IGNORE INTO entities (type, value, first_seen)
        VALUES (type, value, entry.valid_from);
```

Esto es **lineal en el tamaño del corpus, una sola pasada, sin
LLM**. Si las entidades vienen sin tipo, hay que mandarlas al
modelo y pedir clasificación retroactiva — exactamente la deuda
que esta propuesta evita.

**Mapping tipo → uso típico en triples**:

| Subject type | Relation común | Object type |
|---|---|---|
| `person` | `prefers`, `uses`, `is_member_of` | `topic`, `tool`, `project` |
| `project` | `depends_on`, `has_milestone`, `uses` | `project`, `event`, `tool` |
| `file` | `defines`, `imports` | `symbol`, `file` |
| `symbol` | `defined_in`, `referenced_by` | `file`, `symbol` |
| `decision` | `supersedes`, `affects`, `originated_in` | `decision`, `file`/`tool`, `event` |
| `incident` | `affects`, `resolved_by` | `file`, `decision` |
| `event` | `marks_version_of`, `follows` | `project`, `event` |
| `tool` | `replaces`, `used_in` | `tool`, `decision` |
| `topic` | `discussed_in`, `relates_to` | `event`, `topic` |

Las relaciones se mantienen abiertas (TEXT, no enum). Lo mismo que
con los tipos: el LLM en Phase 3 dream las produce con el contexto
del momento. El doc 03 (memory_design.md:416) ya menciona
"SQLite with explicit relationships" — esto encaja directamente.

**Validación del set por mapping**: si los 9 tipos generan un
grafo razonable (las columnas Subject/Object se llenan con
combinaciones plausibles, sin "el tipo X solo aparece como ente
aislado"), el set es internamente coherente. La tabla arriba sugiere
que sí: cada tipo participa de al menos una relación natural.

---

## §6 — Lista cerrada de tipos recomendados (TL;DR)

| Tipo | Cardinalidad esperada | Fuente típica | Ejemplo | Mapping KG |
|---|---|---|---|---|
| `person` | baja (decenas) | usuario, contactos | `person:marcelo` | nodo central, alta conectividad outgoing |
| `project` | baja (unidades-decenas) | repos, productos | `project:durin` | nodo central, hub de `depends_on` / `has_milestone` |
| `file` | media (cientos-miles) | source_refs, ingested paths | `file:durin/memory/schema.py` | hojas del grafo de código |
| `symbol` | alta (miles) | funciones, clases | `symbol:MemoryEntry` | atado a `file` por `defined_in` |
| `topic` | media (cientos) | temas conceptuales recurrentes | `topic:autocompact` | hub de `discussed_in` |
| `decision` | media (decenas-cientos) | bitácora, ADRs | `decision:dropped-posture-vector` | encadenado por `supersedes` |
| `incident` | baja (decenas) | bugs, crashes con identidad | `incident:webui-crash-2026-05-15` | atado a `file` o `tool` por `affects` |
| `tool` | baja (decenas) | herramientas del agent, modelos | `tool:memory_store` | atado a `decision` por `used_in` |
| `event` | media (decenas-cientos) | releases, pivots, milestones | `event:release-v0.1.0a7` | atado a `project` por `marks_version_of` |

Cardinalidad esperada se usa para:

- Decidir budget por tipo en el hot layer (v2): `person` y
  `project` casi siempre caben enteros; `file` y `symbol` se
  recortan al top-K.
- Estimar índices en Phase 3: la tabla `entities` no necesita
  particionar por tipo aún (cardinalidades manejables); en futuro
  un `entities_by_type_<type>` puede ayudar si `file` crece a
  10⁵+.

---

## §7 — Lo que esta propuesta NO hace

- **No introduce un enum cerrado**. Tipos fuera del set
  recomendado son aceptados si pasan el regex de forma.
- **No clasifica entidades retroactivamente**. El corpus existente
  sigue funcionando con strings planos en lectura; se migra
  pasivamente cuando el dream toca una entry.
- **No añade un campo `relations` a `MemoryEntry`**. Las
  relaciones viven en Phase 3 KG, no en el frontmatter de cada
  entry. La estructura `headline | summary | source_refs | related |
  entities` se mantiene sin nuevos campos.
- **No tipa `topics`**. El campo `topics` en
  `<key>.meta.json::derived.tags.topics` queda como `list[str]`
  plano. Eso replica el patrón "concept tags" de OpenClaw
  memory-core (§2.2) — son tokens de clustering, no nombres
  propios.
- **No clasifica con heurísticas regex**. No replicamos
  `detectCategory` de OpenClaw (§2.1) ni la extracción regex de
  Holographic (§2.3). El productor es el LLM en el consolidator y,
  en Phase 3, en el dream. Si el LLM falla, la entry queda con
  entidades vacías — no caemos al fallback determinista.
- **No define las relaciones del KG**. Phase 3 los abre con TEXT
  libre; este doc solo nombra ejemplos de mapping para validar
  que el set de tipos es coherente.
- **No afecta el campo `entities` de pasos de subagent**.
  `docs/08_memory_phase2_proposal.md:707` menciona `entities`
  dentro de un Step node hipotético; eso es discusión de Phase 5+
  y fuera del scope.
- **No añade un tool nuevo**. `memory_store` cambia su descripción
  de parámetros pero no su firma. Si en Phase 3 emerge un
  `kg_query` que filtra por tipo, ese sí es un tool nuevo
  (cubierto por doc 08 §0c.9).
- **No promueve el campo `category` de OpenClaw a durin**. Nuestro
  equivalente al `category` de LanceDB es la `class` de memoria
  (`stable|episodic|corpus|pending`) que ya existe en
  `durin/memory/paths.py:33`. El tipado de entidades es un eje
  ortogonal.
- **No automatiza la promoción de tipos `unknown` a tipos
  conocidos**. Si el modelo emite `meeting:standup-2026-05-23` y
  decidimos más tarde que queremos un tipo `meeting`, se promueve
  añadiendo `meeting` al doc + prompt. El corpus existente con
  `meeting:` ya queda compatible — eso es lo bueno de no haber
  cerrado el enum.

---

## Resumen ejecutivo (1 párrafo)

El campo `entities: list[str]` en `MemoryEntry` y en los dos
`meta.json` derivados pasa a ser `list[str]` con elementos
obligatoriamente formados como `type:value` (regex de forma,
vocabulario abierto). Se añade un módulo `durin/memory/entities.py`
con el validador y un set recomendado de 9 tipos (`person`,
`project`, `file`, `symbol`, `topic`, `decision`, `incident`,
`tool`, `event`). El prompt del consolidator se extiende para
emitir el formato; el resto del código solo lee + valida. No hay
flag-day: las entries existentes sin tipo siguen funcionando en
lectura, y se migran pasivamente cuando el dream las consolida.
Phase 3 KG mapea directo a `entities(id, type, value)` + `triples`
sin clasificación retroactiva — ahí está el ahorro real.
