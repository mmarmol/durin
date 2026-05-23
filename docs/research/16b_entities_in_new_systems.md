# 16b — Modelo de entidades en sistemas open-source recientes

> Investigación complementaria al doc 16 (memoria entity-centric). Cubre los
> sistemas open-source que no estaban clonados: Cognee, Graphiti, Mem0,
> A-Mem, HippoRAG y MemPalace. El objetivo es contrastar la propuesta de
> durin (10 tipos consolidables/referenciables, dream entity-centric) contra
> implementaciones reales que ya están en producción o cerca, antes de
> cerrar Q1-Q4.
>
> NO conclusiones definitivas — el doc 17 hace la síntesis con los outputs
> de los tres agentes. Este documento solo aporta evidencia.

---

## §1 — Sistemas clonados / no encontrados

| Sistema | URL | Commit SHA | Estado |
|---|---|---|---|
| Cognee | https://github.com/topoteretes/cognee | `0187fd8a477c5c88bdd50d840bfff8b023de059b` | clonado en `~/git_personal/cognee/` |
| Graphiti | https://github.com/getzep/graphiti | `34f56e65e0fe2096132c8d16f3a1a4ac9300a5f6` | clonado en `~/git_personal/graphiti/` |
| Mem0 | https://github.com/mem0ai/mem0 | `16a7702d09dd48a9dfbb530a0fa2a51511c7bf26` | clonado en `~/git_personal/mem0/` |
| A-Mem | https://github.com/agiresearch/A-mem | `ceffb860f0712bbae97b184d440df62bc910ca8d` | clonado en `~/git_personal/A-mem/` |
| HippoRAG | https://github.com/OSU-NLP-Group/HippoRAG | `d437bfb1805278b81e20c82357ed3f7d90f14901` | clonado en `~/git_personal/HippoRAG/` |
| MemPalace | https://github.com/mempalace/mempalace | `e5bbc5f9ef51649351d551ce921855dcab27474e` | clonado en `~/git_personal/mempalace/` |

Notas:
- `MemoryPalace` no fue el match del listado: el sistema vivo y mantenido es
  **MemPalace** (Milla Jovovich + Ben Sigman, 2026). Hay también
  `jeffpierce/memory-palace` (MCP server) y `MemoriLabs/Memori`, pero
  MemPalace es el que mejor cubre la categoría "memoria espacial" del listado
  original. Sustitución limpia.
- Todos los demás repos cargaron sin problema, en un solo intento (salvo
  HippoRAG que requirió un reclone — primer intento dejó un `.git` vacío,
  segundo intento funcionó).
- Cognee tiene también `mem0-plugin` (en `~/git_personal/mem0/mem0-plugin/`)
  pero no lo exploré como sistema independiente — Mem0 es lo principal.

---

## §2 — Por sistema

### 2.1 Cognee (`~/git_personal/cognee/`)

**Arquitectura general**

Cognee es una librería Python que convierte documentos en un knowledge graph
heterogéneo y permite búsqueda sobre triplets. Pipeline canónico:
`add → cognify → search`. El "extraer entidades" pasa por un LLM call por
chunk que produce un `KnowledgeGraph` con `nodes` (entidades) + `edges`
(relaciones); ambos se materializan como `DataPoint`s en un graph DB
(Neo4j/Kuzu/NetworkX) y se indexan en un vector DB.

El concepto fundacional es `DataPoint` (clase abstracta en
`cognee/infrastructure/engine/models/DataPoint.py`). Cualquier cosa
guardable extiende `DataPoint`: `Entity`, `EntityType`, `Event`, `Tool`,
`Skill`, `NodeSet`, etc. Cada subclase declara qué campos son `Embeddable`
(van al vector store) y cuáles son `Dedup` (forman parte del UUID
determinístico).

**Q1 — Modelo de identidad**

Identidad por **UUID5 derivado de identity_fields normalizados**. Si una
subclase de `DataPoint` declara `identity_fields` en `metadata`, su ID se
genera deterministicamente; sino, cae a `uuid4` aleatorio.

`cognee/infrastructure/engine/models/DataPoint.py:104-131`:

```python
@classmethod
def _generate_identity_id(
    cls, identity_fields: list[str], data: dict, class_name: str
) -> UUID | None:
    parts = []
    for field_name in identity_fields:
        ...
        if isinstance(value, str):
            value = value.lower().replace(" ", "_").replace("'", "")
        ...
        parts.append(value)
    joined = "|".join(parts)
    identity_string = f"{class_name}:{joined}"
    return uuid5(NAMESPACE_OID, identity_string)
```

Para nodos extraídos por LLM (vía `KnowledgeGraph`), la normalización
canónica vive en `cognee/infrastructure/engine/utils/generate_node_id.py:4-5`:

```python
def generate_node_id(node_id: str) -> UUID:
    return uuid5(NAMESPACE_OID, node_id.lower().replace(" ", "_").replace("'", ""))
```

Es decir: identidad = `uuid5(NAMESPACE_OID, name.lower().replace(" ","_").replace("'",""))`.
`"Durin"` y `"durin"` colisionan al mismo UUID. `"durin agent"` y
`"durin-agent"` NO colisionan (porque el guion se mantiene).

Hay una capa adicional de ontología: si el llamador configura un
`RDFLibOntologyResolver`, Cognee intenta mapear el nombre extraído al "closest
class" de la ontología y reusa ese ID en su lugar
(`expand_with_nodes_and_edges.py:115-128`).

**Q2 — Granularidad**

Vocabulario **abierto, pero estructurado en dos niveles**. El LLM extrae un
`KnowledgeGraph` cuyo schema (`cognee/shared/data_models.py:45-69`) es:

```python
class Node(BaseModel):
    """Node in a knowledge graph."""
    id: str
    name: str = ""
    type: str       # <-- libre, el LLM decide
    description: str
```

El campo `type` es un `str` plano — no hay enum. Lo que sí pasa es que cada
`type` único se materializa como un nodo `EntityType` de igual nombre, con
relación `is_a` desde el `Entity` al `EntityType`
(`expand_with_nodes_and_edges.py:131-208`).

Aparte del set extraído por LLM, Cognee tiene tipos "first-class" hardcoded
como subclases de `DataPoint`:
- `Entity` (`Entity.py:7-12`)
- `EntityType` (`EntityType.py:6-10`)
- `Event` (`Event.py:8-16`) — con `at: Timestamp`, `during: Interval`,
  `location`
- `Tool` (`Tool.py:12-36`)
- `Skill` (`Skill.py` — no leí su detalle, pero existe)
- `Interval` / `Timestamp` (entidades temporales)
- `ColumnValue` / `TableRow` / `TableType` (datos tabulares)
- `Triplet` (sujeto-relación-objeto materializado)
- `NodeSet` (etiqueta arbitraria, `node_set.py:4-7`)

Esto da una pista útil: Cognee separa **tipos de entidad del modelo de datos**
(que SÍ son enum cerrado: `Entity`, `Event`, `Tool`, `Skill`, etc.) de los
**subtipos semánticos de Entity** (que SÍ son vocabulario abierto vía
`Entity.is_a = EntityType("person") / EntityType("project") / ...`).

Es exactamente el patrón "tipos consolidables vs referenciables" de la
propuesta durin §3, pero con la diferencia importante de que **Cognee tiene
ya un `Event` y un `Tool` tipados como modelo propio** — no como subtipo de
`Entity`.

**Q3 — Evolución / conflictos**

**Cognee NO resuelve conflictos semánticos.** Lo más cerca que llega es
deduplicar por ID idéntico (`deduplicate_nodes_and_edges.py:1-21`):

```python
def deduplicate_nodes_and_edges(nodes: list[DataPoint], edges: list[dict]):
    added_entities = {}
    final_nodes = []
    final_edges = []
    for node in nodes:
        if str(node.id) not in added_entities:
            final_nodes.append(node)
            added_entities[str(node.id)] = True
    ...
```

Y deduplicar edges contra edges existentes en el grafo
(`retrieve_existing_edges.py:42-89`). Si dos LLM calls producen
`Entity(name="Marcelo", description="usa pytest")` y
`Entity(name="Marcelo", description="usa unittest")`, **el segundo INSERT
OR REPLACE pisa al primero** (porque tienen igual UUID5). No hay merge ni
detección de contradicción.

Sí hay un módulo de "temporal awareness"
(`cognee/tasks/temporal_graph/`) que extrae eventos con timestamps
explícitos, pero opera sobre `Event` (que tiene su propio `at: Timestamp`),
no sobre relaciones genéricas entre entidades. Es decir: Cognee modela
*cuándo pasó algo* (Event con timestamp) pero NO *cuándo fue cierto algo*
(relación con valid_from/valid_to). Esto contrasta con Graphiti que sí lo
hace.

`cognee/tasks/temporal_graph/models.py:9-55` muestra el data model: cada
Event tiene `time_from: Timestamp` + `time_to: Timestamp`, pero los Triplets
(`EntityAttribute`) que conectan entidades no tienen temporalidad.

**Q4 — Lifecycle**

**Cognee NO tiene archivado por entidad.** Lo que sí tiene es un sistema de
**feedback weights** (`cognee/tasks/memify/apply_feedback_weights.py:43-247`)
que ajusta un score `feedback_weight: float ∈ [0,1]` por nodo y edge según
ratings del usuario sobre QAs:

```python
def stream_update_weight(previous_weight: float, normalized_rating: float, alpha: float) -> float:
    """Streaming update with clipping to [0, 1]."""
    ...
    updated = float(previous_weight) + alpha * (normalized_rating - float(previous_weight))
    final_score = max(0.0, min(1.0, float(updated)))
    return round(final_score, FEEDBACK_WEIGHT_DECIMALS)
```

Este weight afecta retrieval pero no causa deletion. Para borrar
explícitamente, hay una API de `prune` (no la cito porque no la leí en
detalle) que blanquea el grafo entero, no por entidad. Cognee tampoco hace
"compactar páginas de entidad" porque no tiene páginas: las entidades viven
solo como nodos en el grafo.

**Q5 — Retrieval entity-aware**

**Sí, entity-aware via triplet search.** El retriever default es
`GraphCompletionRetriever`
(`cognee/modules/retrieval/graph_completion_retriever.py:33-100+`). El flujo:

1. Vector search sobre nombres de entidades + facts de edges.
2. Para cada match, expandir a triplets (`source → edge → target`).
3. Generar respuesta con el LLM usando el contexto de triplets.

El retriever acepta filtros `node_type` y `node_name`
(`graph_completion_retriever.py:48-49`) — se puede pedir explícitamente
"buscame neighbors de la entidad 'marcelo'".

**Q6 — Costo operacional**

Cognify es **offline (background) por diseño**, pero per-chunk: cada chunk
hace 1 LLM call para extraer el `KnowledgeGraph`. Para una sesión con N
chunks de ~1k tokens cada uno: N llamadas. Con `temporal_cognify=True`
agrega otra capa de extracción (Event/Entity por chunk separado).

La consolidación NO ocurre — no hay un "agente que mira el grafo entero y
fusiona". Lo más cerca es `add_synonymy_edges` (mismo nombre que HippoRAG)
pero no lo encontré central en Cognee; el dedup oficial es por UUID.

**Lección directa para durin**

- **Adoptar**: el patrón `uuid5(NAMESPACE_OID, normalized_name)` para
  identidad determinística. Es el truco más simple posible y resuelve la
  unificación case-insensitive sin estado.
- **Adoptar**: separar `EntityType` como nodo first-class (no solo string).
  Permite preguntas como "dame todos los `topic`" sin escanear todo.
- **Descartar**: ausencia total de manejo de conflictos. Para durin, dream
  específicamente existe para hacer eso. Cognee deja gap aquí.
- **Tomar como advertencia**: Cognee acumula entidades sin pruning. Si
  durin quiere lifecycle real, debe diseñarlo desde el día 1 — Cognee
  muestra que retrofitearlo es difícil cuando las entidades viven en un
  graph DB compartido sin namespace.

---

### 2.2 Graphiti (`~/git_personal/graphiti/`)

**Arquitectura general**

Graphiti es un knowledge graph temporal específicamente diseñado para
agentes con memoria. Storage en Neo4j / FalkorDB / Kuzu / Neptune. La unidad
de input es un `EpisodicNode` (un mensaje o evento con timestamp); el
sistema extrae `EntityNode`s y `EntityEdge`s con `valid_at` / `invalid_at`
explícitos. Donde Cognee es genérico, Graphiti está obsesionado con el
problema "estos dos facts se contradicen, ¿cuál es más reciente?".

**Q1 — Modelo de identidad**

Identidad por **UUID4 inicial + resolución multi-etapa** que mapea el UUID
recién creado al UUID canónico ya existente. El flujo de dedup tiene tres
pasos (`graphiti_core/utils/maintenance/dedup_helpers.py:220-279`):

1. **Exact match normalizado**: lowercase + collapse whitespace
   (`_normalize_string_exact`, líneas 39-42). Si hay 1 candidate con el
   mismo nombre normalizado, fusiona inmediatamente.
2. **Fuzzy match via MinHash + LSH + Jaccard ≥ 0.9**: para nombres con
   suficiente entropía (`_NAME_ENTROPY_THRESHOLD = 1.5`,
   `_FUZZY_JACCARD_THRESHOLD = 0.9`, líneas 31-36). Esto detecta
   variaciones tipo `"Durin Agent"` vs `"durin_agent"` vs `"Durin-Agent"`.
3. **Escalación a LLM**: si los dos primeros pasos no resuelven, manda al
   LLM la lista de candidatos + la entidad nueva y le pide elegir
   (`graphiti_core/utils/maintenance/node_operations.py:467-628`,
   particularmente el prompt en `prompts/dedupe_nodes.py:53-100`).

Notable: hay una guard llamada "entropy gate" — para nombres muy cortos o
de baja entropía (ej. "AI", "ML", "Sam"), no aplica fuzzy matching porque
es demasiado prone to false positives, sino que escala directo al LLM
(`dedup_helpers.py:79-85`).

Además hay una primitive `_promote_resolved_node`
(`dedup_helpers.py:170-189`) que **promueve etiquetas** cuando una nueva
extracción agrega un label específico a una entidad genérica preexistente.
Si la entidad canónica tiene solo `["Entity"]` y el extracted tiene
`["Entity", "Person"]`, se promueve a `["Entity", "Person"]`. Esto es
el equivalente Graphiti de "agregar tipo retroactivamente sin renombrar".

**Q2 — Granularidad**

`EntityNode` tiene un campo **`labels: list[str]`** (multi-label, no
single-type). Ver `nodes.py:97`:

```python
class Node(BaseModel, ABC):
    uuid: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(description='name of the node')
    group_id: str = Field(description='partition of the graph')
    labels: list[str] = Field(default_factory=list)
```

Y la subclase EntityNode (`nodes.py:499-504`):

```python
class EntityNode(Node):
    name_embedding: list[float] | None = Field(default=None, ...)
    summary: str = Field(description='regional summary of surrounding edges', default_factory=str)
    attributes: dict[str, Any] = Field(
        default={}, description='Additional attributes of the node. Dependent on node labels'
    )
```

Los `labels` son **vocabulario abierto** pero el llamador puede pasar
`entity_types: dict[str, type[BaseModel]]` para forzar schema validation
sobre `attributes` cuando el label coincide con uno de los tipos
declarados (`node_operations.py:484`). Es el patrón "open vocabulary +
optional schema per label".

Tipos hardcoded como subclases de Node:
- `EpisodicNode` (mensaje crudo, con `valid_at`)
- `EntityNode` (entidad)
- `CommunityNode` (cluster de entidades, generado por Leiden + LLM summary)
- `SagaNode` (resumen long-form de muchos episodes)

**Q3 — Evolución / conflictos**

**El módulo más sofisticado de los seis estudiados.** Las relaciones
(`EntityEdge`) tienen 4 campos temporales explícitos (`edges.py:271-282`):

```python
class EntityEdge(Edge):
    name: str = Field(description='name of the edge, relation name')
    fact: str = Field(description='fact representing the edge and nodes that it connects')
    fact_embedding: list[float] | None = ...
    episodes: list[str] = Field(default=[], ...)
    expired_at: datetime | None = Field(default=None, description='datetime of when the node was invalidated')
    valid_at: datetime | None = Field(default=None, description='datetime of when the fact became true')
    invalid_at: datetime | None = Field(default=None, description='datetime of when the fact stopped being true')
    reference_time: datetime | None = Field(default=None, ...)
```

Semántica precisa:
- `valid_at` — desde cuándo el hecho es cierto en el mundo
- `invalid_at` — hasta cuándo el hecho fue cierto en el mundo
- `expired_at` — cuándo en sistema (DB-time) decidimos que ya no era cierto
- `reference_time` — timestamp del episodio que extrajo este edge

La función `resolve_extracted_edge`
(`utils/maintenance/edge_operations.py:623-847`) corre por cada edge nuevo:

1. Busca edges existentes con mismo source/target.
2. Manda al LLM la lista de candidatos como "EXISTING FACTS" + "INVALIDATION
   CANDIDATES" y le pide ambos resultados: cuáles son duplicates, cuáles
   son contradicciones (`prompt_library.dedupe_edges.resolve_edge`,
   `edge_operations.py:726-732`).
3. El LLM responde dos listas: `duplicate_facts` y `contradicted_facts`.
4. Las contradicciones se procesan en `resolve_edge_contradictions`
   (`edge_operations.py:540-573`):

```python
for edge in invalidation_candidates:
    edge_invalid_at_utc = ensure_utc(edge.invalid_at)
    resolved_edge_valid_at_utc = ensure_utc(resolved_edge.valid_at)
    edge_valid_at_utc = ensure_utc(edge.valid_at)
    resolved_edge_invalid_at_utc = ensure_utc(resolved_edge.invalid_at)

    if (
        edge_invalid_at_utc is not None
        and resolved_edge_valid_at_utc is not None
        and edge_invalid_at_utc <= resolved_edge_valid_at_utc
    ) or (
        edge_valid_at_utc is not None
        and resolved_edge_invalid_at_utc is not None
        and resolved_edge_invalid_at_utc <= edge_valid_at_utc
    ):
        continue
    elif (
        edge_valid_at_utc is not None
        and resolved_edge_valid_at_utc is not None
        and edge_valid_at_utc < resolved_edge_valid_at_utc
    ):
        edge.invalid_at = resolved_edge.valid_at
        edge.expired_at = edge.expired_at if edge.expired_at is not None else utc_now()
        invalidated_edges.append(edge)
```

En español: si el edge existente era válido desde antes del edge nuevo,
y el LLM marcó contradicción, entonces el viejo se cierra (`invalid_at =
resolved_edge.valid_at`) en lugar de borrarse. Si el nuevo es más antiguo
que algún invalidation_candidate, es el nuevo el que se expira
(`edge_operations.py:820-839`).

**El edge nunca se borra — solo se cierra con timestamps.** La policy es
estrictamente "append, never delete". Esto contrasta con Mem0 (que sí
borra) y A-Mem (que sí actualiza in-place).

`_extract_edge_timestamps` (`edge_operations.py:576-621`) es una llamada LLM
separada que extrae `valid_at` / `invalid_at` del texto del fact + el
reference_time del episodio. Es una llamada adicional por cada edge nuevo
que no traiga timestamps ya.

**Q4 — Lifecycle**

Lifecycle de **nodos**: no hay archivo/decay; existe `delete()` per-UUID
(`nodes.py:111-167`) y `delete_by_group_id` (`nodes.py:177-234`) para
borrar particiones enteras. Pero ninguna policy automática.

Lifecycle de **edges**: como explicado en Q3, los edges se cierran via
`invalid_at` + `expired_at` en lugar de borrarse. Eso es el lifecycle real
en Graphiti — el edge sigue en el grafo, pero queda fuera de queries
"current state" porque su `invalid_at` está en el pasado.

Hay un nodo nuevo `SagaNode` (`nodes.py:867-895`) que es summary long-form
de muchos episodios — un mecanismo de compresión opcional. Tiene
`last_summarized_episode_valid_at` para tracking incremental.

**Q5 — Retrieval entity-aware**

Sí, completamente. El search module (`graphiti_core/search/search.py`,
solo vi grep) tiene retrievers que combinan:
- BM25 sobre `fact` de edges
- Vector similarity sobre embeddings de nombres / facts
- BFS desde nodos seed
- Re-ranking opcional con cross-encoder

El query soporta `as_of: datetime` para filtrar edges activos en ese
momento (estilo bitemporal DB).

**Q6 — Costo operacional**

Por episode nuevo el costo es alto:
- 1 LLM call para extraer entities + edges (`combined_extraction.py`)
- 1 LLM call para extraer timestamps si no vinieron en el primer pass
  (`extract_timestamps`)
- 1 LLM call para dedupe de nodos (si fuzzy falló)
- 1 LLM call para resolve_edge (dedupe + invalidation por cada edge nuevo)
- Opcionalmente: 1 LLM call para extract_attributes si el edge type
  declara schema

Para una conversación con 20 episodes, fácilmente 60-80 LLM calls de
extracción. Por eso se recomienda Haiku/3.5 turbo para esta capa. **Es
inline (no diferido)** — cada `add_episode` corre el pipeline completo.

**Lección directa para durin**

- **Adoptar TODO el modelo temporal** (`valid_at`/`invalid_at`/`expired_at`)
  para relaciones entre entidades. Es la pieza más conceptualmente fuerte
  que vi en los seis sistemas. Resuelve Q3 (conflictos) sin perder
  información — el grafo nunca olvida, solo cierra ventanas.
- **Adoptar el patrón multi-etapa de dedup**: exact normalized → fuzzy
  MinHash/Jaccard → LLM escalation. Es CPU-cheap en el caso común
  (mismo nombre exacto = sin LLM). El entropy gate es importante: no
  fuzzy match strings cortos.
- **Considerar adoptar**: vocabulario abierto vía `labels: list[str]` con
  schema opcional por label vía `entity_types: dict[str, type[BaseModel]]`.
  Da flexibilidad sin perder estructura. Es más rico que el set cerrado de
  10 tipos del doc 16 §3.
- **Descartar para fase 1**: la complejidad de extract_edge_timestamps y
  resolve_edge_contradictions per-edge. Es operacionalmente caro y solo
  se justifica si el dream tiene tiempo offline para correrlo. Si durin
  hace dream offline, vale la pena. Si quiere hacerlo inline, NO.

---

### 2.3 Mem0 (`~/git_personal/mem0/`)

**Arquitectura general**

Mem0 está optimizado para "personalización a escala": memoria por
`user_id`, fact extraction agresiva, dedup por hash + LLM. La unidad de
storage es la **memoria individual** (un fact en formato natural language),
no el grafo de entidades. La versión actual del repo NO incluye el módulo
graph (que vive en el producto cloud) — solo vector + lemmatized BM25
opcionalmente.

Pipeline `add`:
1. Vector search por las últimas 10 memorias relevantes del user.
2. LLM call extrae memorias nuevas del input.
3. Hash dedup contra existentes.
4. Insert en vector store con timestamps.
5. Linking de entidades extraídas vía spaCy (no LLM).

**Q1 — Modelo de identidad**

**Mem0 no tiene "entidades" como first-class.** Las "memorias" tienen
identidad por `uuid4()` aleatorio. Las entidades que sí extrae son:

`mem0/utils/entity_extraction.py:123-144`:

```python
def extract_entities(text: str) -> List[Tuple[str, str]]:
    """Extract named entities, quoted text, and noun compounds from text.
    ...
    Returns:
        Deduplicated list of (entity_type, entity_text) tuples.
        Entity types: PROPER, QUOTED, COMPOUND, NOUN.
    """
    from mem0.utils.spacy_models import get_nlp_full
    nlp = get_nlp_full()
    if nlp is None:
        return []
    doc = nlp(text)
    return _extract_entities_from_doc(doc)
```

Identidad de cada entidad extraída = `entity_text.strip().lower()` como
key, ver `mem0/memory/main.py:875`:

```python
key = entity_text.strip().lower()
if key in global_entities:
    global_entities[key][2].add(memory_id)
else:
    global_entities[key] = [entity_type, entity_text, {memory_id}]
```

Es decir: la unificación es **case-insensitive string equality**. No hay
fuzzy match, no hay LLM, no hay alias.

**Q2 — Granularidad**

Tipos de entidad: **enum cerrado de 4** sacado de spaCy NER + heurísticas
(`entity_extraction.py:347`):

```python
type_pri = {"PROPER": 0, "COMPOUND": 1, "QUOTED": 2, "NOUN": 3, "VERB": 4}
```

- `PROPER` — secuencia de palabras capitalizadas (names, places, brands)
- `QUOTED` — texto entre comillas (titles, terms)
- `COMPOUND` — frase nominal compuesta
- `NOUN` — sustantivo individual (fallback)

**No es semántico** — es lexical. `"Marcelo"` y `"durin"` ambos quedan
como `PROPER`. `"machine learning"` queda como `COMPOUND`. Esto es muy
inferior a Cognee/Graphiti para entity modeling, pero es CHEAP
(spaCy local, no LLM).

Hay un set extensivo de **palabras prohibidas** (`_GENERIC_HEADS`,
`_CIRCUMSTANTIAL_MODS`, `_NON_SPECIFIC_ADJ`, etc., líneas 26-80) para
filtrar candidatas pobres ("thing", "stuff", "place", "thing"). Útil como
referencia operativa.

Notar: **`"place"` está hardcoded en `_GENERIC_HEADS`** (línea 30) — Mem0
considera `place` como genérico y lo descarta. Esto es directamente
relevante para el doc 16 §3.3, que pregunta "¿vale la pena `place` como
tipo?". Mem0 explícitamente dice que NO.

**Q3 — Evolución / conflictos**

Mem0 maneja conflictos vía **prompt LLM que decide ADD/UPDATE/DELETE/NONE**.
El prompt está en `mem0/configs/prompts.py:176-324` (DEFAULT_UPDATE_MEMORY_PROMPT).
Extracto crítico:

```
3. **Delete**: If the retrieved facts contain information that contradicts
   the information present in the memory, then you have to delete it.
   ...
   - Old Memory: "Loves cheese pizza"
   - Retrieved facts: ["Dislikes cheese pizza"]
   - New Memory: ... "event" : "DELETE"
```

Decisión: **override + delete del viejo**. No hay tracking temporal — la
preferencia anterior se pierde. Esto es muy distinto a Graphiti, que
preserva con `invalid_at`.

Hay un mecanismo paralelo más nuevo (`ADDITIVE_EXTRACTION_PROMPT`,
`prompts.py:468+`) que es "ADD only — never delete or update, just dedup
by hash". Es la dirección que toma el repo actual (`memory/main.py:725`
elige este sistema por default).

**Q4 — Lifecycle**

Lifecycle por **hash dedup pre-insert** (`memory/main.py:786-803`):

```python
existing_hashes = set()
for mem in existing_results:
    h = mem.payload.get("hash") if hasattr(mem, "payload") and mem.payload else None
    if h:
        existing_hashes.add(h)

records = []
seen_hashes = set()
for mem in extracted_memories:
    text = mem.get("text")
    ...
    mem_hash = hashlib.md5(text.encode()).hexdigest()
    if mem_hash in existing_hashes or mem_hash in seen_hashes:
        logger.debug(f"Skipping duplicate memory (hash match): {text[:50]}")
        continue
```

Cualquier memoria con MD5 idéntico al de una existente se descarta. No hay
archivado por antigüedad o relevancia. No hay decay de scores. No hay
"forget".

**Q5 — Retrieval entity-aware**

**Parcialmente.** El search default es vector similarity puro sobre el
contenido de las memorias (`memory/main.py:708-714`). Las entidades
extraídas se persisten en un sidecar dict (mapeando `entity_key →
memory_ids`) pero el código que vi no las usa en el path de retrieval —
están ahí más para analytics que para query.

**Q6 — Costo operacional**

Per `add()`: 1 LLM call (extract + dedup decision combinado) + 1 vector
search + N inserts. Es **inline**. spaCy se carga lazy. La eficiencia de
Mem0 viene de batchear extracción + embedding (`memory/main.py:769-784`,
batch embed).

**Lección directa para durin**

- **Adoptar el ADDITIVE_EXTRACTION_PROMPT mental model**: extrae todo lo
  memorable, no intentes hacer UPDATE/DELETE inline. Que el dream (offline)
  se ocupe de consolidar. Mem0 mismo se está moviendo en esta dirección
  (deprecating el viejo UPDATE_MEMORY_PROMPT).
- **Adoptar las listas de filter words** (`_GENERIC_HEADS`,
  `_NON_SPECIFIC_ADJ`) como referencia operativa para qué NO extraer como
  entidad. Particularmente: **Mem0 trata `"place"` como noun genérico**.
- **Descartar**: el modelo de identidad por `entity_text.strip().lower()`.
  Es demasiado primitivo — `"Durin"` ≠ `"durin agent"` ≠ `"durin-agent"`
  serían 3 entidades separadas, repitiendo el problema del doc 16 §2.2.
- **Considerar**: no tener entidades como first-class. Mem0 demuestra que
  puede funcionar bien para use cases simples (preferencias por user_id).
  Pero el doc 16 ya cerró que durin SÍ las quiere — Mem0 es punto de
  referencia para "qué pasa si NO las tenés".

---

### 2.4 A-Mem (`~/git_personal/A-mem/`)

**Arquitectura general**

A-Mem es el sistema más conceptualmente "zettelkasten": cada memoria es
una **nota** (`MemoryNote`) con keywords + context + tags + links. No hay
entidades en absoluto. La evolución pasa cuando una nota nueva entra al
sistema y LLM decide si fortalecer links con vecinos vectoriales o
actualizar sus contextos.

La unidad básica es `MemoryNote`
(`agentic_memory/memory_system.py:24-81`). Todo se persiste en ChromaDB
(vector) + dict en memoria.

**Q1 — Modelo de identidad**

**UUID4 aleatorio sin normalización alguna** (`memory_system.py:65`):

```python
self.id = id or str(uuid.uuid4())
```

A-Mem es radicalmente lo opuesto a Cognee/Graphiti: no intenta unificar
entidades porque NO TIENE entidades. Lo que une "Marcelo escribió X" y
"Marcelo escribió Y" es la similitud vectorial al hacer retrieval — no un
ID compartido.

**Q2 — Granularidad**

**Cero tipos de entidad.** Cada nota tiene:
- `content: str` — el texto crudo
- `keywords: list[str]` — extraídos por LLM
- `context: str` — una sola sentencia summarizing the topic
- `tags: list[str]` — clasificación libre por LLM
- `category: str` — clasificación broad (default "Uncategorized")
- `links: list[str]` — IDs de notas relacionadas

Ver `memory_system.py:159-200` para el prompt de `analyze_content` que
produce keywords/context/tags. Es **vocabulario completamente abierto** —
ni siquiera hay un tipo de entidad implícito. Las "tags" son lo más cerca
y son strings libres.

**Q3 — Evolución / conflictos**

A-Mem tiene un mecanismo único: **evolution prompt cuando una nota nueva
entra**. Cuando se crea una nota:

`memory_system.py:233-264`:

```python
def add_note(self, content: str, time: str = None, **kwargs) -> str:
    ...
    note = MemoryNote(content=content, **kwargs)
    evo_label, note = self.process_memory(note)
    self.memories[note.id] = note
    ...
    if evo_label == True:
        self.evo_cnt += 1
        if self.evo_cnt % self.evo_threshold == 0:
            self.consolidate_memories()
```

`process_memory` (`memory_system.py:590-727`) hace:
1. Vector search por las 5 notas más similares (vecinos).
2. LLM call con el evolution prompt (`memory_system.py:127-157`).
3. LLM responde con `should_evolve`, `actions` (subset de
   `[strengthen, update_neighbor]`), `suggested_connections`,
   `tags_to_update`, `new_context_neighborhood`, `new_tags_neighborhood`.
4. Si `strengthen`: agregar links de la nota nueva a los suggested
   neighbors, actualizar sus tags.
5. Si `update_neighbor`: **actualizar EN PLACE los context y tags de las
   notas vecinas** según lo que decidió el LLM.

Es decir: A-Mem hace dream INLINE per-write. No hay valid_at — hay
**override directo de context/tags de notas vecinas**. Si una nota dice
"Marcelo usa pytest" y luego entra "Marcelo usa unittest", el LLM puede
decidir update_neighbor y reescribir el context de la primera, perdiendo
la info.

A-Mem NO maneja conflictos lógicos — los considera "evolución natural" y
override.

**Q4 — Lifecycle**

A-Mem hace **consolidación periódica** cuando `evo_cnt % evo_threshold ==
0` (default `evo_threshold = 100`, ver `memory_system.py:97`):

`memory_system.py:266-286`:

```python
def consolidate_memories(self):
    """Consolidate memories: update retriever with new documents"""
    # Reset ChromaDB collection
    self.retriever = ChromaRetriever(collection_name="memories", model_name=self.model_name)

    # Re-add all memory documents with their complete metadata
    for memory in self.memories.values():
        metadata = { ... }
        self.retriever.add_document(memory.content, metadata, memory.id)
```

Esto NO archiva ni borra — solo re-indexa todo (probablemente para reflejar
los updates de context/tags hechos por process_memory). Es una operación
de mantenimiento, no de lifecycle real.

No hay delete automático. No hay decay. No hay archivo. La función
`delete()` (`memory_system.py:398-413`) existe pero solo se llama
manualmente.

**Q5 — Retrieval entity-aware**

**No. Retrieval es puro vector similarity sobre el content de las notas**
(`memory_system.py:432-450`). Las notas vecinas se incluyen via
`links` (`find_related_memories_raw`, líneas 315-344), pero los links se
crearon por similitud vectorial — no por entidad compartida.

**Q6 — Costo operacional**

Per nota nueva: 1 LLM call para `analyze_content` + 1 LLM call para
`process_memory` (decisión de evolution). Si el LLM dice
`update_neighbor`, se actualizan las K=5 notas vecinas en memoria (sin
LLM extra, ese trabajo ya viene en la respuesta).

Es **completamente inline** — cada `add_note` puede mutar memorias
vecinas. Sin offline pass.

**Lección directa para durin**

- **Adoptar como anti-pattern**: el modelo "update_neighbor IN PLACE
  durante el write" produce pérdida de información sin trazabilidad. El
  doc 16 §2.2 ya señalaba esto como riesgo ("dream reemplaza 'Marcelo usa
  pytest' con 'Marcelo usa unittest'"). A-Mem materializa exactamente ese
  riesgo. Durin DEBE preservar histórico (estilo Graphiti) o no hacer este
  tipo de updates a entries previos.
- **Adoptar conceptualmente**: la idea de "evolution score" por nota
  (cuántas veces fue tocada por process_memory) es un buen proxy de
  "qué entidad merece página propia" — si una nota se está conectando
  mucho a vecinos, probablemente es hub.
- **Descartar**: la ausencia de entity modeling. El doc 16 ya descartó el
  modelo session-centric/note-centric a favor de entity-centric. A-Mem
  confirma que el modelo note-centric escala mal a queries por entidad
  ("dame todo lo de Marcelo" requiere similarity search, no graph lookup).
- **Confronto con doc 16 §3**: el set de 10 tipos del doc 16 no aparece
  en A-Mem en absoluto — A-Mem niega que los tipos importen. Esto es
  evidencia de que la decisión "tener tipos" es divisiva en la literatura
  open-source, no un consenso.

---

### 2.5 HippoRAG (`~/git_personal/HippoRAG/`)

**Arquitectura general**

HippoRAG (NeurIPS 2024, OSU NLP Group) propone un retrieval system
inspirado en cómo el hipocampo humano indexa memorias: construir un
knowledge graph **denso** (todos los pares de entidades coappearing) y
hacer retrieval por Personalized PageRank desde nodos seed extraídos del
query. La pieza de entity modeling es deliberadamente liviana — el peso
conceptual está en el algoritmo de retrieval, no en la ontología.

**Q1 — Modelo de identidad**

Identidad por **string lowercase del nombre de la entidad**. Las entidades
son `phrase_nodes` (strings) en un igraph. La normalización es trivial,
basta con el matching exacto.

Para la "unificación" usa **synonymy edges** computadas por
KNN-vectorial entre todos los nodos, threshold de cosine similarity
(`global_config.synonymy_edge_sim_threshold`, default 0.8 según docs).

`HippoRAG.py:838-882`:

```python
self.entity_id_to_row = self.entity_embedding_store.get_all_id_to_rows()
entity_node_keys = list(self.entity_id_to_row.keys())
...
entity_embs = self.entity_embedding_store.get_embeddings(entity_node_keys)

# Here we build synonymy edges only between newly inserted phrase nodes and all phrase nodes
query_node_key2knn_node_keys = retrieve_knn(query_ids=entity_node_keys, ...)

num_synonym_triple = 0
synonym_candidates = []

for node_key in tqdm(query_node_key2knn_node_keys.keys(), ...):
    synonyms = []
    entity = self.entity_id_to_row[node_key]["content"]
    if len(re.sub('[^A-Za-z0-9]', '', entity)) > 2:
        nns = query_node_key2knn_node_keys[node_key]
        num_nns = 0
        for nn, score in zip(nns[0], nns[1]):
            if score < self.global_config.synonymy_edge_sim_threshold or num_nns > 100:
                break
            nn_phrase = self.entity_id_to_row[nn]["content"]
            if nn != node_key and nn_phrase != '':
                sim_edge = (node_key, nn)
                synonyms.append((nn, score))
                num_synonym_triple += 1
                self.node_to_node_stats[sim_edge] = score
                num_nns += 1
```

**Crítico**: HippoRAG NO fusiona nodos. Crea un edge "synonymy" entre
ellos. `"Durin"` y `"durin"` quedan como dos nodos distintos en el grafo,
conectados por un edge sintético con peso = cosine similarity. PageRank
luego se encarga de propagar relevancia entre ellos.

Es el opuesto exacto del enfoque de Cognee/Graphiti (que fusionan a un solo
nodo): HippoRAG asume que **mantener duplicados explícitos + linkearlos**
es mejor que **fusionar y perder información**.

**Q2 — Granularidad**

**Cero tipos**. Las entidades son extraídas por un NER prompt LLM
(`prompts/templates/ner.py:1-22`):

```python
ner_system = """Your task is to extract named entities from the given paragraph.
Respond with a JSON list of entities.
"""
```

El output es **un array de strings**, sin labels. Ver el one_shot output:

```python
one_shot_ner_output = """{"named_entities":
    ["Radio City", "India", "3 July 2001", "Hindi", "English", "May 2008", "PlanetRadiocity.com"]
}
"""
```

Personal name, country, date, language — todos como entidad sin
distinción.

Las relaciones (triples) se extraen con un prompt separado
(`triple_extraction.py:4-50`) que también devuelve strings libres como
predicado:

```python
ner_conditioned_re_system = """Your task is to construct an RDF (Resource Description Framework) graph
from the given passages and named entity lists.
Respond with a JSON list of triples, with each triple representing a relationship in the RDF graph.
```

El predicado es free-form string ("located in", "is", "started on",
"plays songs in"). HippoRAG NO normaliza el vocabulario de predicados
tampoco.

**Q3 — Evolución / conflictos**

**No los maneja.** HippoRAG es un sistema de indexación, no de memoria
con escritura iterativa. Su `index(docs)` (`HippoRAG.py:218`) procesa una
vez. No hay update semántico. Si volvés a indexar el mismo documento, se
duplica.

Lo más cerca que tiene es el `delete` (`HippoRAG.py:329`) que borra
triples — y al borrar triples puede dejar entidades huérfanas en el grafo
(no las purga). No es manejo de conflicto, es solo borrado.

**Q4 — Lifecycle**

**No tiene lifecycle de entidades**. El grafo crece monotónicamente. No
hay archive, decay, threshold ni nada similar. El único mantenimiento es
recomputar synonymy edges cuando se agregan nodos nuevos (`add_synonymy_edges`
opera incremental — solo computa para los nuevos contra todos).

**Q5 — Retrieval entity-aware**

**Sí, fundamentalmente entity-aware**. Es el punto fuerte de HippoRAG. El
flujo de retrieval:

1. NER sobre la query para extraer entidades seed (`prompts/templates/ner_query.py`).
2. Match cada seed a nodos del grafo via embedding similarity.
3. Construir un reset_prob vector poniendo masa en esos nodos seed.
4. Correr Personalized PageRank sobre el grafo (incluye synonymy edges
   ponderadas por cosine sim).
5. Los `passage_node`s (documentos originales) reciben la masa de
   PageRank por proximidad gráfica a las entities seed.

`HippoRAG.py:1596-1610`:

```python
if damping is None: damping = 0.5
reset_prob = np.where(np.isnan(reset_prob) | (reset_prob < 0), 0, reset_prob)
pagerank_scores = self.graph.personalized_pagerank(
    vertices=range(len(self.node_name_to_vertex_idx)),
    damping=damping,
    directed=False,
    weights='weight',
    reset=reset_prob,
    implementation='prpack'
)
doc_scores = np.array([pagerank_scores[idx] for idx in self.passage_node_idxs])
sorted_doc_ids = np.argsort(doc_scores)[::-1]
```

Es decir: HippoRAG **explota explícitamente la estructura del grafo de
entidades para retrieval**. Cognee también lo hace pero con triplet search;
HippoRAG con PPR. El resto (Mem0, A-Mem) ignoran la estructura.

**Q6 — Costo operacional**

Index pass: 2 LLM calls por chunk (NER + triple extraction). PPR es CPU
cheap (igraph optimizado). Synonymy edges: 1 KNN search por nodo nuevo
contra todos los nodos (O(n²) batch).

**Es offline (background) por diseño**. No hay path inline. Esto es
consistente con el modelo del doc 16 (dream offline).

**Lección directa para durin**

- **Adoptar conceptualmente**: la idea "synonymy edges en vez de merge"
  es válida para casos donde no estás 100% seguro de que dos entidades
  son la misma. En vez de tomar la decisión ahora (riesgo de pérdida), las
  linkás con un score y dejás que el retrieval propague relevance entre
  ellas. Es una alternativa intermedia entre Cognee (fusión hard) y A-Mem
  (sin entity model). Para durin podría ser un mecanismo de "alias soft"
  cuando la confianza no alcanza para slug merge.
- **Descartar**: vocabulario completamente abierto sin tipos. HippoRAG
  es un sistema de **retrieval**, no de personal memory para un agente.
  Sus entidades pueden ser fechas, números, languages — todas mezcladas.
  Eso no aplica para durin donde el modelo necesita preguntar "ahora dame
  el contexto de `person:marcelo`".
- **Adoptar como cifra de referencia**: PageRank threshold 0.5 damping y
  similarity threshold 0.8 para synonymy edges. Son valores que el paper
  validó empíricamente sobre MuSiQue / HotpotQA.
- **Confronto con doc 16 §3**: HippoRAG no distingue `person` de `place`
  de `topic`. La granularidad de tipos no aparece en el paper como
  factor. Esto sugiere que para retrieval-only, no importan los tipos;
  importa la estructura del grafo. Para durin (consumido por LLM, no por
  un sistema de QA) sí importa porque el modelo necesita tipo para
  navegar.

---

### 2.6 MemPalace (`~/git_personal/mempalace/`)

**Arquitectura general**

MemPalace es el sistema más nuevo y peculiar de los seis. Local-first,
verbatim-storage, MCP-native. La metáfora central es física: **wings**
(grandes proyectos / personas), **rooms** (subtopics dentro de un wing),
**drawers** (registros verbatim crudos), **closets** (índices compactos
con topics + entidades + quotes que apuntan a drawers). Las **hallways**
conectan entidades dentro de un wing por co-occurrence. Encima hay un
knowledge graph SQLite con triples temporales.

Es el sistema que más se parece a lo que durin propone (combinación de
storage local + entidades tipadas + grafo temporal).

**Q1 — Modelo de identidad**

Identidad por **string normalizado** (`knowledge_graph.py:219-220`):

```python
def _entity_id(self, name: str) -> str:
    return name.lower().replace(" ", "_").replace("'", "")
```

Es **idéntica a la de Cognee** (lowercase + underscore + drop apostrophe),
pero MemPalace la usa como ID directo (string), no como input a uuid5.

A nivel de aplicación, MemPalace tiene un **EntityRegistry persistente**
(`entity_registry.py:299+`) que mapea nombres a clasificaciones, sourced
desde tres lugares en orden de prioridad:

1. **Onboarding** (`seed()`, líneas 396-444): user declares "estas son mis
   personas y proyectos" — `confidence=1.0`, source=`"onboarding"`. Soporta
   `aliases` dict para mapear `"Max" → "Maxwell"`.
2. **Learned** (inferido de history con high confidence).
3. **Researched** (Wikipedia API call para palabras desconocidas;
   privacy-flagged, opt-in con `allow_network=True`).

El lookup (`entity_registry.py:448+`) busca: exact match → alias match →
wiki cache → wikipedia API si está habilitado.

Hay un mecanismo especial llamado **ambiguous_flags**
(`entity_registry.py:438-442`) — si el nombre coincide con una "common
English word" (lista hardcoded en `COMMON_ENGLISH_WORDS`, líneas 32-87,
incluye `"ever"`, `"grace"`, `"will"`, `"max"`, `"may"`, etc.), se marca
como ambigua y requiere **context disambiguation** vía regex patterns
sobre la frase circundante (`PERSON_CONTEXT_PATTERNS` líneas 90-111,
`CONCEPT_CONTEXT_PATTERNS` líneas 114-125).

Esto es el patrón más sofisticado que vi para resolver el clásico problema
"Marcelo dijo: 'Will lo va a hacer'" donde `Will` puede ser nombre o verbo
modal. Las patterns chequean cosas como `r"\b{name}\s+said\b"` (señal de
person) vs `r"\bwould\s+{name}\b"` (señal de modal verb).

**Q2 — Granularidad**

Vocabulario **semi-cerrado**, 4 tipos hardcoded (`entity_registry.py:472,
482, 496`):

- `person`
- `project`
- `concept`
- `place` (vía wiki inferencia, `PLACE_INDICATOR_PHRASES` líneas 161-174)

Plus un `unknown` default (`knowledge_graph.py:224`):

```python
def add_entity(self, name: str, entity_type: str = "unknown", properties: dict = None):
    """Add or update an entity node."""
```

El `entity_type` en la table SQL es `TEXT DEFAULT 'unknown'`
(`knowledge_graph.py:147-153`). Es decir: el schema es abierto a nivel SQL
(podés pasar cualquier string), pero la app capa solo emite 4-5 valores.

El entity_detector va más simple (`entity_detector.py:344+`): clasifica
candidatos en **3 buckets**: `person`, `project`, `uncertain`. Y luego el
user confirma en CLI:

`entity_detector.py:666-670`:

```python
kind = input(f"  Is '{name}' a (p)erson or p(r)oject? ").strip().lower()
if kind == "p":
    ...
elif kind == "r":
```

Es decir: **MemPalace tiene un wizard interactivo de onboarding** que le
pregunta al user qué es cada entidad detectada. Esto es muy diferente del
LLM-decides de Graphiti/Cognee y del NER ciego de Mem0.

**Q3 — Evolución / conflictos**

MemPalace tiene un **KG temporal** dedicado (`knowledge_graph.py:155-170`):

```python
CREATE TABLE IF NOT EXISTS triples (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    confidence REAL DEFAULT 1.0,
    source_closet TEXT,
    source_file TEXT,
    source_drawer_id TEXT,
    adapter_name TEXT,
    extracted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    ...
);
```

`valid_from` + `valid_to` por triple. Es el mismo patrón que Graphiti
pero más simple (solo dos campos en lugar de cuatro).

El conflicto se resuelve manualmente vía `invalidate()`
(`knowledge_graph.py:328-358`):

```python
def invalidate(self, subject: str, predicate: str, obj: str, ended: str = None):
    """Mark a relationship as no longer valid (set valid_to date/time)."""
    ...
    with self._lock:
        conn = self._conn()
        with conn:
            rows = conn.execute(
                "SELECT id, valid_from FROM triples "
                "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                (sub_id, pred, obj_id),
            ).fetchall()

            for row in rows:
                valid_from = row["valid_from"]
                if valid_from is not None and _temporal_end_key(ended) < _temporal_start_key(valid_from):
                    raise ValueError(
                        f"valid_to={ended!r} is before valid_from={valid_from!r}; "
                        "an inverted interval would be invisible to every KG query"
                    )

            conn.execute(
                "UPDATE triples SET valid_to=? "
                "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                (ended, sub_id, pred, obj_id),
            )
```

Es decir: `invalidate()` solo UPDATE — nunca DELETE. Append-only.
Idéntica filosofía a Graphiti.

Sin embargo, **MemPalace NO detecta contradicciones automáticamente**.
Hay que llamar `invalidate()` explícitamente. El KG solo registra hechos —
es el adapter / extractor el que tiene que decir "este predicado nuevo
contradice al anterior, marcalo".

Pero los drawers son **storage verbatim** — no se borran ni se actualizan.
El conflicto se materializa en el KG (que es índice derivado), no en los
drawers (que son la verdad).

**Q4 — Lifecycle**

- **Drawers**: verbatim, nunca se borran salvo dedup explícito
  (`dedup.py:79-100`). El dedup detecta drawers casi-idénticos del mismo
  source_file (cosine distance < 0.15 default) y se queda con el más largo.
- **Closets**: regenerables. Se purgan via `purge_file_closets`
  (`palace.py:298-306`) cada vez que se re-mina un source file.
- **Triples KG**: append-only, se cierran via `invalid_at` pero nunca se
  borran.
- **Entity registry**: persistente JSON, mantenido manualmente vía
  onboarding y inferencia.
- **Hallways**: regenerables — se computan from drawer co-occurrence.

Esta separación es interesante: **datos crudos (drawers) son
inmutables; índices (closets, hallways, KG, registry) son regenerables o
append-only**. Es el mismo principio que tu propuesta del doc 16 para
indexación pre-dream + dream consolida.

**Q5 — Retrieval entity-aware**

Sí, parcialmente. El retrieval default es vector search sobre closets
(que ya están compactados con entity tags). Hay un `searcher.py` que
también filtra por wing/room. Y hay `knowledge_graph.query_entity()`
(`knowledge_graph.py:362+`) para queries entity-first con `as_of` filter
temporal.

Es decir: el path "give me everything about Marcelo" se sirve directo del
KG, no de vector search.

**Q6 — Costo operacional**

MemPalace es **completamente local** salvo opt-in a wiki/LLM. Los closets
default son regex-based (cero LLM calls). El path LLM
(`closet_llm.py:62+`) es opcional para regenerar closets con mejor
indexación cuando el usuario lo permite.

El entity_detector hace 0 LLM calls — todo via regex sobre prose files.

Esto significa que **MemPalace puede operar a costo $0** (modulo
embeddings locales de ChromaDB). Y la consolidación es offline (background)
por design.

**Lección directa para durin**

- **Adoptar el patrón de `EntityRegistry` con tres fuentes priorizadas**:
  onboarding > learned > researched. Particularmente el onboarding interactivo
  es lo que más se acerca a "Marcelo (architect) cuenta a durin quién es
  quién" — es modelo familiar para Marcelo (ya vio onboarding en
  install-wizard durin).
- **Adoptar el patrón ambiguous_flags + context patterns** para nombres
  que coinciden con palabras comunes. `"Will"`, `"May"`, `"Mark"` son
  ejemplos reales que romperán la unificación basada en string en el caso
  durin (si Marcelo conoce a "Will"). Las regex patterns de MemPalace son
  copiables casi tal cual.
- **Adoptar `valid_from`/`valid_to` simple (no el cuádruple de Graphiti)
  como punto de partida**: dos campos, append-only via UPDATE, validación
  que valid_to > valid_from. Es el mínimo viable de Q3.
- **Adoptar la regla "datos crudos inmutables, índices regenerables"**:
  drawers vs closets es exactamente el patrón episodic vs entities/. Si
  los drawers no se borran, MemPalace gana la garantía de que ningún dream
  pierde info; durin con `memory/episodic/` debería respetar lo mismo.
- **Confronto con doc 16 §3.1**: MemPalace tiene 4 tipos (`person`,
  `project`, `concept`, `place`) — el doc 16 propone 6 consolidables
  (`person`, `project`, `place`, `topic`, `incident`, `tool`). MemPalace
  no separa `topic` de `concept` (son el mismo), no tiene `incident` ni
  `tool` como tipos. Esto es evidencia de que `incident` y `tool` son
  decisión durin-específica (no convencional). Vale la pena cuestionarlas.
- **Confronto con doc 16 §3.3**: MemPalace **SÍ usa `place` como tipo
  first-class** vía wiki indicator phrases. Es contrapunto al "Mem0 trata
  place como noun genérico". El argumento de MemPalace es que un place
  tiene historia propia (uno vive ahí, lo visita, lo asocia con eventos)
  — exactamente el argumento de doc 16 §3.1. Evidencia A-FAVOR del tipo
  place.
- **Confronto con set de 10 tipos**: MemPalace NO tiene `file`, `symbol`,
  `decision`, `event` como tipos referenciables. Solo entidades de "primer
  ciudadano" (las 4) y todo lo demás vive en drawers como texto. Esto
  cuestiona la utilidad del split "consolidable vs referenciable" del doc
  16 — si los referenciables solo viven como etiquetas, ¿realmente
  necesitamos un vocabulario tipado para ellos, o basta con extraerlos
  como entities pero no consolidarlos?

---

## §3 — Tabla síntesis cross-sistema

| Sistema | Tipo de identidad | Vocabulario | Conflictos | Lifecycle | Inline/offline | Adopta? |
|---|---|---|---|---|---|---|
| **Cognee** | `uuid5(name.lower().replace(' ','_').replace("'",""))` | Abierto (LLM-decided `type:str`) + tipos hardcoded de DataPoint (Entity, Event, Tool, Skill) | NO — INSERT OR REPLACE por UUID | Solo feedback_weight; sin archive | Offline (cognify es batch) | Sí — el patrón UUID5 + EntityType separado |
| **Graphiti** | UUID4 → resolved via exact-norm → MinHash/Jaccard (≥0.9) → LLM | Abierto (`labels: list[str]`) + schema opcional por label | LLM resuelve duplicate vs contradiction; edges con `valid_at/invalid_at/expired_at`; append-only | Edges se cierran (no se borran); nodes deletables a mano | Inline (per episode) | Sí — TODO el modelo temporal de edges |
| **Mem0** | `entity_text.strip().lower()` (sin fuzzy) | Cerrado 4 tipos lexicales spaCy: PROPER/QUOTED/COMPOUND/NOUN | LLM decide ADD/UPDATE/DELETE/NONE; en mode aditivo solo hash dedup | Hash MD5 dedup pre-insert; sin archive | Inline (per add) | Solo el ADDITIVE_EXTRACTION pattern + la lista de stopwords |
| **A-Mem** | `uuid4()` aleatorio, sin normalización | Cero tipos (notas con keywords/context/tags abiertos) | Override IN PLACE via update_neighbor durante write | Re-index periódico cada 100 writes; sin archive | Inline (process_memory por write) | Anti-pattern: NO replicar update_neighbor |
| **HippoRAG** | String lowercase del nombre, sin merge | Cero tipos (entities = strings sin label) | No los maneja (index pass único) | Sin lifecycle, grafo monotónico | Offline (index batch) | El concepto "synonymy edges" como middle-ground |
| **MemPalace** | `name.lower().replace(' ','_').replace("'","")` + EntityRegistry persistente | Semi-cerrado: 4 tipos (person/project/concept/place) + unknown | KG con valid_from/valid_to; invalidate() explícito; sin auto-detection | Drawers inmutables; closets/hallways regenerables; triples append-only | Offline (mining batch) | El patrón EntityRegistry + ambiguous_flags + onboarding wizard |

---

## §4 — Patrones que aparecen ≥2 veces

Estos son los más probables de ser "correctos" porque aparecen
independientemente en sistemas distintos.

**P1 — Identidad por nombre normalizado (lowercase + collapse separators).**
Aparece en: Cognee (`uuid5` sobre `name.lower().replace(" ","_").replace("'","")`,
`generate_node_id.py:5`), MemPalace (`name.lower().replace(" ","_").replace("'","")`,
`knowledge_graph.py:219-220`), Mem0 (`entity_text.strip().lower()`,
`memory/main.py:875`), Graphiti (`_normalize_string_exact`,
`dedup_helpers.py:39-42`), HippoRAG (lowercase implícito).
**Aparece en 5/6 sistemas. Es el baseline indiscutido para Q2.**

**P2 — Vocabulario abierto a nivel de schema, semi-cerrado a nivel de
aplicación.** Aparece en: Cognee (`type: str` abierto en KnowledgeGraph,
pero `Entity/EntityType/Event/Tool` cerrado en código), Graphiti
(`labels: list[str]` abierto, pero `entity_types: dict[str, BaseModel]`
opt-in para validation), MemPalace (`entity_type TEXT DEFAULT 'unknown'`
en SQL, pero 4 valores hardcoded en app), HippoRAG y A-Mem (sin tipos en
absoluto pero ni esos sistemas tienen schema cerrado). **El consenso es
NO usar un enum estricto.** Esto cuestiona la decisión del doc 16 §3 de
listar 10 tipos rígidos — ningún sistema estudiado los hace rígidos en
schema, todos dejan extensibility.

**P3 — Conflictos resueltos por preservación temporal, no override.**
Aparece en: Graphiti (`valid_at`/`invalid_at`/`expired_at` en edges, NUNCA
borra), MemPalace (`valid_from`/`valid_to`, `invalidate()` solo UPDATE).
Mem0 viejo modo lo hacía con override y está deprecando. **2 de 6 sistemas
adoptan append-only temporal; los demás son más pobres en este dimension**
(A-Mem hace override puro, Cognee silencia el viejo, HippoRAG no toca el
viejo). Si durin quiere Q3 robusto, el patrón temporal es el camino —
es el único que preserva auditoría.

**P4 — Dedup multi-etapa: lookup directo → similitud (lexical o
embedding) → LLM como último recurso.** Aparece en: Graphiti (exact-norm
→ MinHash+Jaccard ≥0.9 → LLM, `dedup_helpers.py:220-279` +
`node_operations.py:467+`), MemPalace (exact → wiki → ambiguity context
patterns + LLM opcional, `entity_registry.py:448+`), Cognee (uuid5 directo,
pero el ontology resolver opcional puede llamar LLM,
`expand_with_nodes_and_edges.py:115+`). **3 de 6 sistemas usan dedup
multi-etapa.** Es el patrón operacional correcto para minimizar costo LLM
mientras se mantiene precisión.

**P5 — Datos crudos inmutables, índices regenerables.** Aparece en:
MemPalace (drawers verbatim vs closets regenerables, ver dedup.py:79+ y
palace.py:298+ purge_file_closets), Cognee (DocumentChunk preserved, los
grafos se re-extraen). En durin's roadmap: `memory/episodic/` debería ser
inmutable, `memory/entities/` regenerable. **Patrón fuerte para
arquitectura.**

**P6 — Retrieval entity-aware via graph traversal, no solo vector
similarity.** Aparece en: Graphiti (BFS + vector + cross-encoder
opcional), Cognee (triplet search desde nodos seed,
`brute_force_triplet_search.py:49+`), HippoRAG (Personalized PageRank,
`HippoRAG.py:1596-1610`), MemPalace (KG queries + closets). **4 de 6
sistemas usan estructura de grafo en retrieval.** Sin entity-aware
retrieval, las entidades quedan como decoración. Si durin extrae
entidades, debe usarlas en retrieval o no vale la pena hacerlo.

**P7 — Onboarding manual / aliases declarados.** Aparece en: MemPalace
(EntityRegistry.seed() con aliases dict del user) y de manera indirecta
en Mem0 (custom_instructions). Cognee/Graphiti/HippoRAG/A-Mem no lo
tienen — asumen que todo viene del corpus. **Pero el caso durin SÍ
tiene onboarding** (install-wizard); este es un pattern que la mayoría
de literatura ignora pero MemPalace usa con éxito.

---

## §5 — Lo que ningún sistema resuelve bien

**G1 — Decisión "esta entidad es consolidable (página propia)" vs
"esta es solo referenciable (tag)".** Es el split central de doc 16 §3.
Ningún sistema estudiado lo tiene como decisión arquitectónica. Cognee
hace todo un EntityType. Graphiti hace todo un EntityNode con labels.
MemPalace tiene wings (proyectos / personas grandes) vs entities en el
KG. Pero **ninguno establece "estos tipos cardinality baja merecen
storage propio, estos otros no"**. Es un huerto donde durin debe innovar
con su propia respuesta.

**G2 — `incident` y `tool` como tipos first-class de memoria personal.**
Ninguno de los 6 tiene un tipo `incident` (webui-crash-2026-05-15 con
causa+fix+lección). MemPalace tiene "diary entries" pero no incidents
tipados. Cognee tiene `Tool` pero específicamente como "tool callable por
el agente para retrieval" — no como "herramienta de software que el
usuario usa". Este es vocabulario doc16-específico. Hay que justificarlo
con casos de uso reales o eliminarlo.

**G3 — Decay automático sin LLM-call.** Ningún sistema hace decay
determinístico — todos confían en feedback explícito (Cognee) o en
nunca borrar (Graphiti/MemPalace). El doc 16 §6 pide "regla determinista
para archivado". La literatura abierta no ofrece template aquí — durin
tendría que diseñarlo. Las heurísticas posibles (last_accessed,
n_references, age) están en los papers pero no en producción.

**G4 — Manejo de evolución temporal SIN llm-call per write.** Graphiti es
el más sofisticado pero requiere 3-5 LLM calls per episode. MemPalace
exige llamar `invalidate()` manualmente. Ningún sistema tiene "regla
determinística para detectar contradicción semántica". Esto es coherente
con el state of the art — detectar contradicción semántica sin LLM es un
problema abierto.

**G5 — Unificación de aliases sin LLM ni state explícito.** El caso
canónico: `"Marcelo"`, `"Marcelo Marmol"`, `"mmarmol@mxhero.com"`. Cognee
y MemPalace requieren aliases declarados (state). Graphiti requiere LLM
call. Ningún sistema tiene "fuzzy match cross-form de email + name +
nickname" robusto sin state. Para durin que aspira a un setup local
mínimo, esto es un gap.

---

## §6 — Recomendación parcial para Q1-Q4

**Q1 — Granularidad**. Evidencia desde §4-P2: todos los sistemas usan
vocabulario abierto a nivel schema, semi-cerrado a nivel app. Recomendación:
**no fijar set de 10 tipos en el schema** — usar campo libre `type: str`
y mantener una lista canónica en código de "tipos que durin reconoce y
consolida" (puede arrancar con 6 consolidables del doc 16 §3.1). Esto
permite que el modelo introduzca tipos nuevos (vía LLM extraction) sin
romper el schema; la decisión "este tipo se consolida en página" es de
código, no de DB.

Sub-q de doc 16 §3.3 ("¿vale la pena `place`?"): evidencia mixta.
MemPalace lo usa con éxito vía wiki; Mem0 explícitamente lo descarta como
genérico. Decisión depende del corpus durin — si el user usa el agent
mucho en contexto "estoy en la oficina, ..." (place anchorable), sí. Si
no, no. **Empezar SIN `place` y agregarlo si los datos lo justifican** es
la dirección más prudente — es más fácil agregar tipo que migrar páginas.

`incident` y `tool` no aparecen en literatura (§5-G2). Si durin los
quiere, los debe justificar con casos. Mi sospecha: `tool` overlap con
`topic` (un tool ES un topic con metadata extra); `incident` overlap
con `event` referenciable. **Reducir a 4 tipos consolidables (person /
project / topic / place opcional) puede ser suficiente**.

**Q2 — Identidad y unificación**. Evidencia §4-P1 y §4-P4: el patrón
canonical es:

1. Normalizar nombre: `name.lower().replace(" ", "_").replace("'", "")`.
2. Generar ID: o bien `uuid5(NAMESPACE, normalized_name)` (Cognee) o
   directamente el normalized_name como ID (MemPalace).
3. Para casos ambiguos (mismo nombre, varias entidades): usar contextos
   tipo MemPalace (`PERSON_CONTEXT_PATTERNS`) o escalation a LLM
   (Graphiti).
4. Aliases declarados por user en onboarding (MemPalace pattern).
5. Fuzzy matching opcional para nombres con entropía suficiente
   (Graphiti MinHash/Jaccard ≥0.9).

**Para durin: arrancar con normalized-name como ID + alias dict en
onboarding. Diferir fuzzy + LLM-dedupe hasta que sea necesario.** El path
de bajo costo cubre 80% de casos sin LLM.

**Q3 — Conflictos y evolución**. Evidencia §4-P3: el patrón
**append-only temporal** (Graphiti + MemPalace) es el único que preserva
auditoría. A-Mem demuestra que el override pierde info. Mem0 está
deprecando el override.

**Recomendación: durin debe adoptar append-only.** Específicamente, los
hechos sobre una entidad (preferencias, estados) deberían tener
`valid_from` / `valid_to`. Cuando dream detecta contradicción ("Marcelo
ya no usa pytest"), cierra el viejo con `valid_to = ahora` y abre uno
nuevo con `valid_from = ahora`. **Página `entities/person/marcelo.md`
puede mostrar solo el current state (valid_to IS NULL) pero el storage
preserva todo.**

El modelo más simple posible (MemPalace, 2 fields) es suficiente para fase
1; agregar `expired_at` separado (Graphiti) solo si la distinción
"world-time vs db-time" se vuelve relevante.

**Q4 — Lifecycle**. Evidencia §4-P5 + §5-G3:

- Las **memory entries** (`memory/episodic/<id>.md`) deben ser
  **inmutables** — nunca borrar, nunca actualizar. Es lo que hacen
  Cognee (chunks), MemPalace (drawers), Graphiti (episodes).
- Las **páginas de entidad** (`memory/entities/<type>/<value>.md`) son
  **derivadas y regenerables**. El dream puede reescribir basándose en las
  entries. Si el dream pierde una página, la regenera.
- **NO hay archive automático de páginas** en ningún sistema estudiado.
  Recomendación: NO implementar decay en fase 1. Solo "delete manual" via
  user. Si la página crece demasiado, el dream truncates/summarizes pero
  no borra.
- Para tracking de "esta entidad sigue siendo activa" usar
  `last_referenced_at` en el frontmatter de la página. Si el user
  pregunta "qué pasó hace 6 meses con X", la búsqueda en `memory/episodic`
  sigue funcionando incluso si la página de X no existe ya.

Estas son direcciones, no decisiones cerradas — el doc 17 hace la
síntesis cruzando los outputs de los tres agentes de investigación.

---

## Last updated: 2026-05-23 (post-investigación open-source, agent 2)
