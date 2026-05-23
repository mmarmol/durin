# 16c — Memoria entity-centric: lado académico y discusión online

> Investigación complementaria al doc 16. Cubre papers académicos
> (2023-2026) y discusión online de practitioners (blogs, threads,
> HN). El otro flujo (16b) cubre código de sistemas open-source.
> Este doc NO cierra Q1-Q4 — los inputs convergen en el doc 17.

---

## §1 — Papers académicos cubiertos

Ordenados por relevancia directa al problema entity-centric.

| Paper | Fecha | Venue | Link |
|---|---|---|---|
| Generative Agents | abr-2023 | UIST '23 | https://arxiv.org/abs/2304.03442 |
| Reflexion | mar-2023 | NeurIPS 2023 | https://arxiv.org/abs/2303.11366 |
| MemoryBank | may-2023 | arXiv | https://arxiv.org/abs/2305.10250 |
| MemGPT / Letta | oct-2023 | arXiv (ICLR'24 rejected, plataforma activa) | https://arxiv.org/abs/2310.08560 |
| GraphRAG (From Local to Global) | abr-2024 | arXiv (Microsoft Research) | https://arxiv.org/abs/2404.16130 |
| HippoRAG | may-2024 | NeurIPS 2024 | https://arxiv.org/abs/2405.14831 |
| AriGraph | jul-2024 | arXiv | https://arxiv.org/abs/2407.04363 |
| Extract-Define-Canonicalize (EDC) | abr-2024 | NAACL 2024 | https://arxiv.org/abs/2404.03868 |
| Zep / Graphiti | ene-2025 | arXiv | https://arxiv.org/abs/2501.13956 |
| HippoRAG 2 (From RAG to Memory) | feb-2025 | ICML 2025 | https://arxiv.org/abs/2502.14802 |
| A-MEM | feb-2025 | arXiv | https://arxiv.org/abs/2502.12110 |
| Mem0 | abr-2025 | ECAI 2025 | https://arxiv.org/abs/2504.19413 |
| Memory in the Age of AI Agents (survey) | dic-2025 | arXiv | https://arxiv.org/abs/2512.13564 |
| Graph-based Agent Memory (survey) | feb-2026 | arXiv | https://arxiv.org/abs/2602.05665 |
| EverMemBench | feb-2026 | arXiv | https://arxiv.org/abs/2602.01313 |
| CompassMem (Memory Matters More) | ene-2026 | arXiv | https://arxiv.org/abs/2601.04726 |
| GAAMA | mar-2026 | arXiv | https://arxiv.org/abs/2603.27910 |
| HyperMem | abr-2026 | ACL 2026 | https://arxiv.org/abs/2604.08256 |

Notas honestas:

- **HyperMem (ACL 2026)** tiene la cifra fuerte (92.73% LoCoMo) pero el
  PDF de la versión disponible **no documenta entity identity ni
  conflict resolution** explícitamente. Sólo describe la jerarquía
  topics/episodes/facts. Posible omisión del paper o pieza que dejaron
  al LLM. No encontré repo público al momento de escribir esto.
- **Mem0** publicó paper + librería + servicio. La librería expone
  `categories` (no `entity_types` puros) en su forma "vanilla"; el
  `Mem0g` (graph variant) sí trabaja con entidades como nodos. Hay
  desalineación entre marketing y código observable.
- **Zep paper** describe la arquitectura pero el repo Graphiti es el
  artifact más usable. Las extracciones más concretas vienen del repo
  y la doc, no del PDF.

---

## §2 — Por paper

### 2.1 Generative Agents (Park et al., 2023)

**Cita.** Park, J.S., O'Brien, J.C., Cai, C.J., Morris, M.R., Liang, P.,
Bernstein, M.S. *Generative Agents: Interactive Simulacra of Human
Behavior*. UIST '23 (October 2023). arXiv:2304.03442.

**Arquitectura.** Memory stream lineal: cada observación se appendea
con `(text, timestamp, last_accessed)`. Retrieval combina tres scores
normalizados al rango `[0, 1]`:

```
score = α_recency · recency + α_importance · importance + α_relevance · relevance
```

Con los `α` todos en 1 en su implementación. **Recency** es decay
exponencial sobre last_accessed. **Importance** lo emite el LLM como
entero (1-10) en el momento de almacenar. **Relevance** es coseno
entre embedding de la query y embedding de la memoria.

Sobre eso corren dos procesos sintéticos: **reflections** (cuando la
suma de importance de las últimas N observaciones supera un threshold,
el agente genera preguntas top-level sobre sí mismo y luego responde
con árboles de reflection apuntando a observaciones-base) y **plans**
(actuar diariamente).

**Las 6 respuestas.**

1. *Entidades first-class?* **No.** El memory stream es texto plano con
   metadatos. No hay tabla de entidades. Las reflections son nodos
   sintetizados, no entidades.
2. *Granularidad?* Una sola: "observación". Las reflections son
   observaciones-sobre-observaciones.
3. *Identidad / unificación?* No existe. Si la misma persona aparece
   con nombres distintos, son textos distintos. La unificación es
   implícita por el LLM al generar reflection.
4. *Conflictos / evolución?* No hay mecanismo explícito. La nueva
   observación se agrega; el LLM resuelve al recuperar.
5. *Lifecycle?* `last_accessed` se updateaa en cada retrieval (mantiene
   memorias usadas "frescas" vía recency). **No hay eviction explícito**;
   el stream crece sin límite en el paper original.
6. *Insight para durin.* La fórmula `recency + importance + relevance`
   es el baseline mínimo. Las reflections son el primer patrón
   formalizado de "consolidación periódica" — equivalente conceptual
   al dream entity-centric pero sin tipado.

> "When the sum of the importance scores for the latest events perceived by
> the agents exceeds a threshold, we generate a reflection."

### 2.2 Reflexion (Shinn et al., 2023)

**Cita.** Shinn, N., Cassano, F., Berman, E., Gopinath, A., Narasimhan,
K., Yao, S. *Reflexion: Language Agents with Verbal Reinforcement
Learning*. NeurIPS 2023. arXiv:2303.11366.

**Arquitectura.** Memoria episódica orientada a failures. Tras una
trayectoria, el agente genera un "verbal feedback" sobre por qué
falló (o tuvo éxito subóptimo). Ese texto se acumula en un buffer
episódico que se inyecta al próximo intento.

**Las 6 respuestas.**

1. *Entidades first-class?* **No.** El objeto de memoria es el
   trajectory feedback (texto natural).
2. *Granularidad?* Una unidad: "lección de un intento fallido".
3. *Identidad / unificación?* No aplica — no hay entidades
   referenciables.
4. *Conflictos?* Acumulación; el modelo resuelve en lectura.
5. *Lifecycle?* Buffer FIFO acotado en tokens. Lo más viejo se cae.
6. *Insight para durin.* El **patrón "failure as first-class memory"**
   merece quedarse: si durin tipa `incident:<id>` (causa + fix +
   lección), está aplicando Reflexion + categorización. Reflexion
   valida que las lecciones puntuales valen como unidad consolidable.

### 2.3 MemoryBank (Zhong et al., 2023)

**Cita.** Zhong, W., Guo, L., Gao, Q., Ye, H., Wang, Y. *MemoryBank:
Enhancing Large Language Models with Long-Term Memory*. arXiv:2305.10250
(May 2023).

**Arquitectura.** Memoria de conversaciones con **decay basado en
Ebbinghaus forgetting curve**: `R = exp(-t/S)`, donde `S` (memory
strength) se incrementa cuando una memoria es accedida (refuerzo). La
memoria también construye un "user portrait" derivado del histórico.

**Las 6 respuestas.**

1. *Entidades first-class?* **Parcialmente.** El "user portrait" es la
   única entidad con página. No hay `project`, `place`, `topic`.
2. *Granularidad?* Dos tipos: episodios conversacionales y portrait
   del usuario.
3. *Identidad?* Una sola entidad — el usuario. No hay unificación
   porque hay un único hub.
4. *Conflictos?* La memoria menos usada decae y eventualmente se
   purga; las que se acceden suben de strength. **Conflicto se resuelve
   por decay**: lo viejo y poco usado muere; lo nuevo sobrevive.
5. *Lifecycle?* Forgetting determinista (R bajo threshold → purge).
   Refuerzo por recall (`S` sube).
6. *Insight para durin.* El **patrón "decay con refuerzo"** es la
   formalización más limpia de Q4 que vi. Para `topic` (alta
   cardinalidad esperada), decay + reinforcement es atractivo. Para
   `person` y `project` (baja cardinalidad), probablemente sobreingeniería.

### 2.4 MemGPT / Letta (Packer et al., 2023-25)

**Cita.** Packer, C., Wooders, S., Lin, K., Fang, V., Patil, S.G.,
Gonzalez, J.E. *MemGPT: Towards LLMs as Operating Systems*.
arXiv:2310.08560 (October 2023). Letta es la continuación comercial
(2024+).

**Arquitectura.** Tres niveles:

- **Core memory**: blocks fijos en contexto, editables por el agente
  vía tool calls. Blocks típicos: `persona` (autoconcepto del agente)
  y `human` (info del usuario). Tamaño limitado por chars.
- **Recall memory**: histórico de mensajes (FIFO en contexto + búsqueda
  externa).
- **Archival memory**: store externo (vector / graph) para overflow.

El agente decide a qué block escribir y cuándo "page out" a archival.
Letta agrega **memory blocks compartidos entre agents** y `read_only`
flag por block.

**Las 6 respuestas.**

1. *Entidades first-class?* **No estructuralmente.** Los blocks son
   strings con label (`human`, `persona`). Listas / dicts viven como
   texto dentro del block. No hay índice de entidades.
2. *Granularidad?* Por label. Cardinalidad práctica: 2-10 blocks. Es
   la dimensión más baja del espectro.
3. *Identidad?* No aplica directamente — un block por concepto, slug
   manual.
4. *Conflictos?* El agente edita el block (tool `core_memory_replace`).
   Override directo; no preserva historia salvo que el block sea
   append-only por convención.
5. *Lifecycle?* Eviction core→archival cuando se llena el block (LLM
   decide vía tool call).
6. *Insight para durin.* **MemGPT modela el rol estructural mínimo de
   "entidad como página"** (los blocks). Pero su tipología cerrada
   (`human`, `persona`) no escala al modelo del doc 16. Lo útil: las
   páginas son **editables por el agente**, no append-only — esto es
   lo que el dream necesita hacer con `entities/<type>/<value>.md`.

### 2.5 GraphRAG (Microsoft, 2024)

**Cita.** Edge, D., Trinh, H., Cheng, N., Bradley, J., Chao, A.,
Mody, A., Truitt, S., Larson, J. *From Local to Global: A Graph RAG
Approach to Query-Focused Summarization*. arXiv:2404.16130 (April 2024).

**Arquitectura.** Pipeline en dos etapas:

1. **Build**: LLM extrae entities + relations + claims de cada chunk.
   Entidades dedup-eadas por `(title, type)`; las descripciones se
   **resumen via LLM** combinando todas las instancias por entidad
   (entity description summarization). Comunidades detectadas con
   Leiden, resumidas jerárquicamente.
2. **Query**: cada community summary genera respuesta parcial; se
   funden en respuesta global.

**Las 6 respuestas.**

1. *Entidades first-class?* **Sí.** Nodos en el grafo con propiedades
   (`title`, `type`, `description`, embeddings).
2. *Granularidad?* Abierto pero típicamente `PERSON`, `ORGANIZATION`,
   `LOCATION`, `EVENT` (NER clásico). Cardinalidad: miles.
3. *Identidad?* **Dedup por título exacto + tipo**. Sin disambiguation
   más allá. Reportado explícitamente como limitación: "Jon" y "Jon
   Márquez" quedan como nodos separados. Este es un agujero
   reconocido.
4. *Conflictos?* La descripción consolidada (LLM combina todas las
   menciones) ABSORBE la contradicción en prosa. No hay marca
   "supersedes". Conflicto resuelto por síntesis textual.
5. *Lifecycle?* No hay decay. El grafo se rebuildea o se hace
   incremental. No hay archive.
6. *Insight para durin.* GraphRAG **demuestra que entity dedup naive
   (match exacto por título + tipo) no alcanza** y se vuelve un agujero
   conocido en producción. Lo positivo: la "description summarization"
   por LLM es el patrón estándar para construir la página de entidad
   desde múltiples observaciones — exactamente lo que el dream haría.

### 2.6 HippoRAG / HippoRAG 2 (Gutiérrez et al., 2024-25)

**Cita.** Gutiérrez, B.J., Shu, Y., Gu, Y., Yasunaga, M., Su, Y.
*HippoRAG: Neurobiologically Inspired Long-Term Memory for Large
Language Models*. NeurIPS 2024. arXiv:2405.14831. Continúa en *From
RAG to Memory* (ICML 2025, arXiv:2502.14802).

**Arquitectura.** Pipeline:

1. OpenIE sobre cada passage → noun-phrase nodes + relation edges.
2. **Synonymy edges**: para pares `(n_i, n_j)` cuya cosine similarity
   entre embeddings supere un threshold τ, se agrega edge sinónimo.
   Esta es la dedup soft.
3. Query → entidades de la query como seeds → Personalized PageRank
   sobre el grafo → ranking de passages.

Direct quote: "we use M to add the extra set of synonymy relations E′
when the cosine similarity between two entity representations in N is
above a threshold τ."

**Las 6 respuestas.**

1. *Entidades first-class?* **Sí**, como noun phrases del corpus.
2. *Granularidad?* Granularidad fina (todo noun phrase). No tipado.
3. *Identidad?* **Synonymy edges por umbral coseno** — no merge duro;
   el PPR usa las edges para "fluir" entre variantes. Es la mejor
   solución soft que vi en la literatura: no decide que "Marcelo" ==
   "marcelo", agrega una edge fuerte y deja que el ranking las trate
   como casi-equivalentes.
4. *Conflictos?* No hay manejo explícito; el grafo simplemente acumula.
5. *Lifecycle?* No hay decay.
6. *Insight para durin.* **El umbral coseno + synonymy edges resuelve
   Q2 sin commit prematuro a una decisión de merge.** Es una solución
   intermedia entre "slug manual" y "LLM merge agresivo". Útil para el
   dream de durin si quisiera evitar merges duros sobre `person:` que
   resulten errados.

### 2.7 AriGraph (Anokhin et al., 2024)

**Cita.** Anokhin, P., Semenov, N., Sorokin, A., Evseev, D., Burtsev,
M., Burnaev, E. *AriGraph: Learning Knowledge Graph World Models with
Episodic Memory for LLM Agents*. arXiv:2407.04363 (July 2024).

**Arquitectura.** Grafo donde nodos son entidades semánticas y aristas
son "edges episódicas" que pueden tocar múltiples relaciones del grafo
en simultáneo. Pensado para agentes embodied en TextWorld.

**Las 6 respuestas.**

1. *Entidades first-class?* **Sí**, parte central del modelo.
2. *Granularidad?* Determinada por el dominio (TextWorld game state):
   rooms, items, characters.
3. *Identidad?* Inicialización por extracción del modelo; cuando el
   agente revisita la misma entidad, actualiza el nodo. No hay merge
   complejo porque el dominio tiene IDs sin ambigüedad.
4. *Conflictos?* La arista episódica preserva la observación; el nodo
   semántico se actualiza. **Híbrido append-en-aristas + override-en-nodos**.
5. *Lifecycle?* No hay decay en el paper.
6. *Insight para durin.* El **separar "observación episódica" (arista)
   de "estado semántico" (nodo)** es exactamente lo que el doc 16
   plantea entre `episodic/<id>.md` y `entities/<type>/<value>.md`.
   Es validación arquitectónica directa.

### 2.8 EDC — Extract, Define, Canonicalize (Zhang et al., 2024)

**Cita.** Zhang, B., Reddy, H.S., Yu, B., Wang, X., Chen, X.,
Soltani, A., Cohen, S.B. *Extract, Define, Canonicalize: An LLM-based
Framework for Knowledge Graph Construction*. NAACL 2024.
arXiv:2404.03868.

**Arquitectura.** Tres fases:

1. **Extract**: LLM extrae triples (h, r, t) en zero-shot.
2. **Define**: LLM genera schema definitions para cada relación
   extraída.
3. **Canonicalize**: vector similarity sobre schema definitions identifica
   componentes "casi iguales" y los unifica.

**Las 6 respuestas.**

1. *Entidades first-class?* Sí, pero el paper enfoca canonicalización
   de **relaciones**, no entidades. Reportan explícitamente la limitación.
2. *Granularidad?* Schema abierto, post-hoc.
3. *Identidad?* Por embedding de la **definición** generada, no del
   nombre. Patrón distintivo: dedup semántico, no léxico.
4. *Conflictos?* Reescritura del schema.
5. *Lifecycle?* No aplica.
6. *Insight para durin.* **Cuando dedup-eás por embedding, embedebé la
   descripción/definición del nodo, no solo el nombre**. Aplicable
   directo a `person:` (embed bio + roles) y a `project:` (embed
   purpose + scope).

### 2.9 Zep / Graphiti (Rasmussen et al., 2025)

**Cita.** Rasmussen, P., Paliychuk, P., Beauvais, T., Ryan, J., Chalef,
D. *Zep: A Temporal Knowledge Graph Architecture for Agent Memory*.
arXiv:2501.13956 (January 2025). Repo: github.com/getzep/graphiti.

**Arquitectura.** Grafo temporal bi-temporal:

- **Entity nodes** (`n_i ∈ N_s`) con `name`, `name_embedding`,
  `summary`, `labels`, `attributes` (Pydantic-typed).
- **Fact edges** (`e_i ∈ E_s`) son relaciones semánticas extraídas.
- **Episode nodes**: contienen el mensaje/texto/JSON original.

Cada fact carries cuatro timestamps:

```
created_at  ← cuándo lo aprendió el sistema
valid_at    ← cuándo el hecho empezó a ser cierto
invalid_at  ← cuándo dejó de serlo (si aplica)
expired_at  ← cuándo se "descubrió" la invalidación
```

**Resolución de identidad** combina:

1. Embed entity name en R^1024, k-NN por coseno.
2. Full-text search sobre nombres y summaries existentes.
3. LLM final resolver con los candidatos + contexto del episodio.
4. Si duplicado: merge → genera `name` y `summary` actualizados.

**Resolución de conflictos**: cuando un nuevo edge contradice uno
existente, el LLM detecta la contradicción y se setea `t_invalid` del
edge viejo al `t_valid` del nuevo. **Los facts NO se borran**.

**Las 6 respuestas.**

1. *Entidades first-class?* **Sí, completamente.** Es el sistema
   más alineado conceptualmente con el doc 16.
2. *Granularidad?* Pydantic-typed, default `Person`/`Company`/`Product`
   en la doc pero **completamente custom por el usuario**. Esquema
   abierto.
3. *Identidad?* La pipeline más explícita de los sistemas vistos:
   embedding + full-text + LLM resolver. Caro pero documentado.
4. *Conflictos?* Bi-temporal invalidation. Preserva historia
   permanentemente.
5. *Lifecycle?* `invalid_at` + `expired_at` cubren "ya no aplica";
   no hay decay por uso, no hay forget.
6. *Insight para durin.* **Es el blueprint más completo que existe.**
   Las 4 timestamps son probablemente overkill para single-user CLI
   (durin), pero `valid_from` + `invalid_at` mínimos resuelven Q3
   limpio. La pipeline de identity resolution (embed + fts + LLM) es
   pesada — durin probablemente puede empezar con slug + embed match.

> "When a new episode contradicts an existing fact, the old fact gets
> an invalid_at marker and the new fact takes over with its own valid_from."

### 2.10 A-MEM (Xu et al., 2025)

**Cita.** Xu, W., Mei, K., Gao, H., Tan, J., Liang, Z., Zhang, Y.
*A-MEM: Agentic Memory for LLM Agents*. arXiv:2502.12110 (February 2025).

**Arquitectura.** Inspirado en Zettelkasten. Cada nota es un tuple:

```
m_i = {c_i, t_i, K_i, G_i, X_i, e_i, L_i}
```

Donde `c` es contenido original, `t` timestamp, `K` keywords (LLM),
`G` tags (LLM), `X` contextual description (LLM), `e` embedding, `L`
links a otras notas.

Linking dinámico:

1. Cosine similarity → top-k candidatos.
2. LLM decide si linkear y por qué.
3. Si linkea, **puede updatear keywords/tags/context de notas
   linkeadas** ("memory evolution").

**Las 6 respuestas.**

1. *Entidades first-class?* **No.** Las notas son la unidad atómica;
   los links sustituyen al grafo de entidades.
2. *Granularidad?* Una sola: la nota.
3. *Identidad?* No hay entidades a identificar; las notas son únicas
   por construcción.
4. *Conflictos?* No los maneja explícitamente. Es agujero reconocido
   del paper.
5. *Lifecycle?* No hay decay.
6. *Insight para durin.* A-MEM es el "anti-doc-16" — propone que **no
   necesitás entidades si tenés notas bien linkeadas + LLM como
   navegador del grafo**. Cuestiona si la inversión en `entities/`
   vale la pena. Argumento débil contra durin: A-MEM no maneja
   conflictos ni evolución de identidad — exactamente lo que durin
   prioriza.

### 2.11 Mem0 / Mem0g (Chhikara et al., 2025)

**Cita.** Chhikara, P., Khant, D., Aryan, S., Singh, T., Yadav, D.
*Mem0: Building Production-Ready AI Agents with Scalable Long-Term
Memory*. ECAI 2025. arXiv:2504.19413.

**Arquitectura — Mem0 (vanilla).**

- **Extraction**: LLM extrae "salient memories" del mensaje + contexto.
- **Update**: por cada memoria nueva, top-s memorias similares (vector)
  → LLM decide entre 4 operaciones:
  - `ADD` (no hay equivalente)
  - `UPDATE` (complementa una existente)
  - `DELETE` (la nueva contradice una vieja → borrar la vieja)
  - `NOOP` (la nueva es redundante)

**Arquitectura — Mem0g (graph variant).**

Grafo dirigido `G = (V, E, L)`:

- `V`: entidades con type label (`Person`, `City`).
- `E`: triples `(v_s, r, v_d)`.
- `L`: etiqueta semántica de tipo.

Retrieval dual: entity matching + triplet similarity.

**Las 6 respuestas.**

1. *Entidades first-class?* **En Mem0g sí; en Mem0 vanilla no.**
2. *Granularidad?* Tipos abiertos (`Person`, `City`, `Event`) en
   Mem0g. En vanilla son `categories` (`personal_details`,
   `professional_details`, `food`, etc.) — orientado a usuario, no a
   entidad.
3. *Identidad?* En Mem0g, basado en label + embedding. Sin LLM
   resolver explícito documentado.
4. *Conflictos?* Las 4 operaciones (ADD/UPDATE/DELETE/NOOP) son
   **definitivas** — `DELETE` literalmente borra. Sin historia. Más
   destructivo que Zep.
5. *Lifecycle?* `DELETE` cubre forget. No hay decay.
6. *Insight para durin.* **El patrón ADD/UPDATE/DELETE/NOOP como
   decisión LLM-driven en write-time es directamente aplicable** al
   dream. Pero la decisión de borrar (vs invalidar) tiene tradeoff:
   Mem0 prioriza simplicidad y costo; Zep prioriza auditabilidad.
   Para durin (cuenta personal, no enterprise), el balance correcto
   probablemente sea "invalidate, no delete, salvo basura clara".

> "ADD for creation of new memories when no semantically equivalent
> memory exists; UPDATE for augmentation of existing memories with
> complementary information; DELETE for removal of memories
> contradicted by new information; and NOOP when the candidate fact
> requires no modification."

### 2.12 Memory in the Age of AI Agents (survey, Liu et al., 2025)

**Cita.** Liu, S. et al. *Memory in the Age of AI Agents: A Survey of
Forms, Functions and Dynamics*. arXiv:2512.13564 (December 2025; updated
January 2026). 107 páginas.

**Taxonomía Forms × Functions × Dynamics.**

- **Forms**: token-level (texto), parametric (weights), latent (hidden states).
- **Functions**:
  - **Factual memory** — declarative (preferences, env states).
  - **Experiential memory** — case/strategy/skill (workflows reusables).
  - **Working memory** — contexto activo.
- **Dynamics**: formation → evolution (consolidación + forgetting) → retrieval.

**Las 6 respuestas.**

1. *Entidades first-class?* La survey las trata como uno de los
   patrones de **factual memory + token-level**, no como dimensión
   ortogonal.
2. *Granularidad?* Reseña 80+ papers de "factual + token-level"
   cubriendo KGs, episodic structures, etc.
3. *Identidad?* No prescriptiva; reseña enfoques múltiples.
4. *Conflictos?* Cubierto bajo "Evolution → Updating".
5. *Lifecycle?* Cubierto bajo "Evolution → Forgetting".
6. *Insight para durin.* La survey **no impone un consenso sobre
   entity-centric** — confirma que es UNA de las direcciones, no LA
   dirección. Útil como reality check: ningún paper único cierra Q1-Q4
   convincentemente.

### 2.13 Graph-based Agent Memory (survey, Yang et al., 2026)

**Cita.** Yang, C., Zhou, C., Xiao, Y., et al. *Graph-based Agent
Memory: Taxonomy, Techniques, and Applications*. arXiv:2602.05665
(February 2026). 29 páginas.

**Taxonomía** específicamente sobre **graph-based** memory: KGs, trees
temporales, hipergrafos. Ciclo de vida: extracción → storage →
retrieval → evolution.

**Las 6 respuestas.**

1. *Entidades first-class?* En todo el dominio cubierto, sí — por
   definición.
2. *Granularidad?* Reseña triples, n-arias hyperedges, jerarquías.
3. *Identidad?* Bi-temporal modeling (de Zep) destacado como técnica
   estado del arte para conflictos.
4. *Conflictos?* "Temporal invalidation rather than overwrites" como
   patrón consensuado.
5. *Lifecycle?* "Consolidation, reasoning, reorganization" — los 3
   patrones de evolution.
6. *Insight para durin.* La survey **canoniza la dirección
   entity-centric + temporal invalidation** como el frontier 2026.
   Reduce ambigüedad sobre si la decisión arquitectónica del doc 16
   está alineada con la dirección de la literatura.

### 2.14 EverMemBench (Cheng et al., 2026)

**Cita.** Cheng, Z., et al. *EverMemBench: Benchmarking Long-Term
Interactive Memory in Large Language Models*. arXiv:2602.01313
(February 2026).

**Arquitectura del benchmark.** Multi-party group chats, 1M+ tokens,
calendar-aligned timeline, 2400 QA pairs en tres dimensiones:

- Fine-grained recall (single-hop, multi-hop, temporal).
- Memory awareness (constraint application, proactivity, updating).
- Profile understanding (style, skill, title de cada participante).

**Las 6 respuestas (sobre el benchmark).**

1. *Entidades first-class?* El benchmark TESTEA profile understanding
   por participante — esto fuerza a sistemas a tener algún tipo de
   `person:` modelado.
2. *Granularidad?* Personas + roles + skills. Mínimo viable.
3. *Identidad?* Implícito: los sistemas deben mantener identity
   estable de N personas a lo largo de la conversación.
4. *Conflictos?* "Updating" es dimensión explícita.
5. *Lifecycle?* Implícito en "updating".
6. *Insight para durin.* **El benchmark valida indirectamente que sin
   `person:` modelado, los sistemas no superan threshold básicos de
   profile understanding** (11-58% en style — el más difícil). Es
   evidencia empírica de que la entidad `person:` paga.

### 2.15 CompassMem / Event-Centric Memory (2026)

**Cita.** *Memory Matters More: Event-Centric Memory as a Logic Map
for Agent Searching and Reasoning*. arXiv:2601.04726 (January 2026).

**Arquitectura.** Cuestiona el patrón entity-centric y propone
**event-centric**: la unidad atómica es el evento, no la entidad. Los
events tienen participants embedded como atributos, no como nodos
externos.

**Las 6 respuestas.**

1. *Entidades first-class?* **No** — los participantes son atributos
   embedded en eventos.
2. *Granularidad?* Una: el event.
3. *Identidad?* "Normalized entities and source attributions" dentro
   del event — pero no hay nodos persistentes.
4. *Conflictos?* Node fusion: events equivalentes mergen, related se
   linkean.
5. *Lifecycle?* No documentado en detalle.
6. *Insight para durin.* Es la **crítica más afilada al enfoque
   entity-centric**. Argumento: las entidades estáticas pierden el
   contexto temporal y los participants. Pero observación clave: durin
   ya tiene **memory entries con `event_idx` y participants** — el
   doc 16 no propone reemplazar entries por entidades, sino agregar
   `entities/<type>/<value>.md` como CAPA DE CONSOLIDACIÓN. La crítica
   event-centric aplica si rompés los entries; no aplica si los
   conservás.

### 2.16 GAAMA (Paul et al., 2026)

**Cita.** Paul, S.K., et al. *GAAMA: Graph Augmented Associative
Memory for Agents*. arXiv:2603.27910 (March 2026).

**Arquitectura.** Cuatro tipos de nodos + cinco tipos de aristas:

- **Episode**: turn verbatim.
- **Fact**: atomic assertion.
- **Reflection**: pattern across multiple facts.
- **Concept**: 2-5 word snake_case label (`pottery_hobby`).

Aristas: `NEXT`, `DERIVED_FROM`, `DERIVED_FROM_FACT`, `HAS_CONCEPT`,
`ABOUT_CONCEPT`. Retrieval: k-NN + PPR edge-aware.

**Decisión clave**: GAAMA evita entity nodes para personas porque
"recurring participants accumulate hundreds of edges, causing PPR mass
to distribute uniformly across all connected memories" — el problema
del **mega-hub**. Usan concept nodes (topics) como pivotes.

**Las 6 respuestas.**

1. *Entidades first-class?* **No para personas**, sí para concepts.
2. *Granularidad?* 4 tipos: episode / fact / reflection / concept.
3. *Identidad?* Concepts en snake_case canonicalizados por LLM al
   extraer.
4. *Conflictos?* Prompts explícitos: "Do NOT duplicate existing facts"
   y "Do NOT duplicate existing reflections".
5. *Lifecycle?* No documentado.
6. *Insight para durin.* **El problema del mega-hub es una advertencia
   real para `person:marcelo`** — si durin acumula 5000 facts apuntando
   a Marcelo, la utilidad como hub se diluye. Soluciones posibles:
   sub-tipar (`person:marcelo:preferences` vs `person:marcelo:projects`),
   reflections intermedias, o (como GAAMA) usar topics como pivotes.

### 2.17 HyperMem (Yue et al., 2026)

**Cita.** Yue, J., et al. *HyperMem: Hypergraph Memory for Long-Term
Conversations*. ACL 2026. arXiv:2604.08256.

**Arquitectura.** Hipergrafo tres niveles:

- **Topic nodes**: `(title, summary)`.
- **Episode nodes**: `(dialogue, title, episode_summary)`.
- **Fact nodes**: `(content, potential_queries, keywords)`.

Hiperaristas agrupan episodios y sus facts relacionados. Retrieval
híbrido lexical + semantic.

**Las 6 respuestas.**

1. *Entidades first-class?* **No.** El paper deliberadamente NO
   modela entidades como nodos separados. La info de entidades vive
   embedded en facts/episodes.
2. *Granularidad?* 3 niveles, ninguno es "entity".
3. *Identidad?* No abordado en el paper.
4. *Conflictos?* No abordado.
5. *Lifecycle?* No abordado.
6. *Insight para durin.* HyperMem es **SOTA en LoCoMo (92.73%) sin
   modelar entidades como first-class**. Esto es la observación más
   incómoda para durin: si el SOTA del benchmark de memoria
   conversacional larga no necesita entidades tipadas, **¿por qué
   durin sí?** Respuesta provisoria: porque el caso de uso de durin
   no es "responder QA sobre la conversación" sino "operar
   coherentemente sesión a sesión sobre los mismos
   proyectos/personas/herramientas". El benchmark de LoCoMo no
   captura ese caso. Pero la pregunta queda: **¿necesitamos
   entidades, o necesitamos buena retrieval + reflections?**

---

## §3 — Discusión online relevante

### 3.1 Threads de Hacker News

- **"Ask HN: Are we close to figuring out LLM/Agent Memory"**
  (https://news.ycombinator.com/item?id=47449389, ~marzo 2026).
  Punto clave: el OP nota que markdown files + memory tools simples
  (estilo OpenClaw) **están outperformeando** soluciones complejas
  RAG/embedding. Comentarista `kageroumado` describe "lossless context
  management" con summaries multi-nivel. `AndyNemmity`: "there are no
  reasonable metrics... we are all exploring in paths". **Importa
  porque**: el practitioner consensus es escéptico de la sofisticación;
  cualquier propuesta entity-centric de durin tiene que justificar
  por qué supera markdown + retrieval simple.

- **"Show HN: AI memory with biological decay (52% recall)"**
  (https://news.ycombinator.com/item?id=47914367, ~mayo 2026).
  Implementación de Ebbinghaus decay para agent memory. Reacciones
  divididas: `SwellJoe` argumenta que ha **deshabilitado memory en
  todo lo que usa** porque "infer connections between conversations
  where there is none". `mtrifonov` defiende "type-conditional
  half-life" — decay diferente por tipo de info. **Importa porque**:
  hay sentimiento real de que la memoria de agentes ES UN PROBLEMA NO
  RESUELTO, y que muchas implementaciones empeoran la experiencia.
  Confirma la línea del doc 16: "tener un sistema y entidades muy
  confiables importa más que sumar features".

- **HN sobre Memary** (https://news.ycombinator.com/item?id=40196879,
  abril 2024). KG-based memory open source. Recibió crítica
  constructiva sobre dedup y escalabilidad. No accesible en este
  fetch (HTTP 429), pero la línea de discusión sigue siendo: KG vale
  cuando hay temporal reasoning, falla cuando es overhead para chat
  simple.

### 3.2 Blogs de practitioners / vendors

- **Zep blog: "Stop Using RAG for Agent Memory"**
  (https://blog.getzep.com/stop-using-rag-for-agent-memory/, junio 2025).
  Argumento: embeddings tratan facts como puntos aislados; no manejan
  temporal sequence ni fact invalidation. Ejemplo: usuario prefiere
  Adidas → cambia a Puma → RAG matchea Adidas igual. Recomiendan KG +
  bi-temporal. **Honesto sobre el sesgo**: Zep vende KG-based memory.
  Pero el argumento técnico es sólido y el ejemplo Adidas/Puma es la
  misma forma del problema que durin tiene en mente.

- **Mem0 blog: "State of AI Agent Memory 2026"**
  (https://mem0.ai/blog/state-of-ai-agent-memory-2026, mayo 2026).
  Producción gaps identificados: **(1)** temporal abstraction degrada
  ~25% pasando de 1M a 10M tokens, **(2)** cross-session structure
  treats changes as replacements, no como evolution, **(3)** memory
  staleness (high-relevance memories que se vuelven confidently wrong),
  **(4)** cross-session identity resolution roto (IDs inestables),
  **(5)** privacy sin estándar, **(6)** benchmark scores no predicen
  performance real. Mem0 reporta 92.5 LoCoMo / 94.4 LongMemEval con
  ~6900 tokens/query. **Importa porque**: la lista de problemas
  abiertos es **exactamente** la lista de problemas que el doc 16
  identifica (Q1-Q4). Convergencia.

- **Lanham, "Memory, Not Magic"**
  (https://medium.com/@Micheal-Lanham/memory-not-magic..., abril 2026).
  Argumento práctico: **"Store distilled playbooks, not flight
  recorders"**. Cita: "What you actually want is a meta-layer digest.
  What was it trying to do. What decisions it already made." **Importa
  porque**: refuerza que `entities/<type>/<value>.md` como página
  consolidada (vs entries crudos) es la dirección correcta.

- **Octoco.ai (Herman Lintvelt): "Knowledge Graphs as Memory"**.
  Recomienda tipos abiertos: `LifeEvent`, `PersonalInsight`, `Goal`,
  `Achievement`, `Challenge`, `Habit`, `Person`, `Preference`, con
  relations `IMPACTS`, `CAUSES`, `PREVENTS`, `MOTIVATES`, `RELATES_TO`,
  `KNOWS`. Conclusión: "You are NOT limited to any fixed schema."
  **Importa porque**: los 10 tipos del doc 16 caen dentro de este
  espectro, pero la posición del autor es **schema abierto** —
  posición intermedia entre el set fijo de durin y el caos total.

- **Fountain City: "Agent Memory & Knowledge Systems Compared (2026)"**
  (https://fountaincity.tech/.../agent-memory-knowledge-systems-compared/,
  mayo 2026). Compara 8 frameworks: Mem0, Zep/Graphiti, LangMem, Letta,
  Semantic Kernel, Cognee, Supermemory, Redis. Observación crítica:
  **"none of them resolve that account_id in Salesforce, org_id in
  Stripe... are the same company."** Cross-system identity gap es
  agujero universal. **Importa porque**: ninguno resuelve Q2 (identidad)
  para el caso difícil — y durin tendrá ese caso si quiere unificar
  `marcelo` de un git commit con `mmarmol@mxhero.com` de un email.

- **Vectorize.io: "Zep vs Cognee"**
  (https://vectorize.io/articles/zep-vs-cognee, 2026). Zep gana en
  temporal entity tracking; Cognee gana en breadth of ingestion. Zep
  benchmarkea 63.8% en LongMemEval; Cognee no publica número. **Importa
  porque**: la comparación práctica confirma que cuando el caso
  prioriza Q3 (evolución temporal), Zep es la opción. Para durin, Q3
  es claramente prioritario.

- **Atlan: "Best AI Agent Memory Frameworks 2026"**
  (https://atlan.com/know/best-ai-agent-memory-frameworks-2026/).
  Identifica que **solo Supermemory trata "memory expiration as a
  first-class operation"** — los otros 7 frameworks tienen lifecycle
  débil. **Importa porque**: confirma que Q4 (lifecycle) es agujero
  universal, no solo de durin.

- **dev.to (Vektor Memory): "State of AI Agent Memory 2026"**
  (https://dev.to/.../state-of-ai-agent-memory-in-2026..., mayo 2026).
  Sintetiza ECAI 2025 paper de Mem0. Cita relevante: **"None of these
  systems solve the fundamental challenge: deciding what to remember
  and what to forget"**. Lista frameworks: Mem0 (48k stars, $24M),
  Letta (16.4k, $10M), Zep, Cognee. **Importa porque**: confirma que
  Q4 (lifecycle/forgetting) no está resuelto en producción, no en
  papers — gap real.

- **dev.to (Eahm60): "I replaced my agents markdown memory with a
  semantic graph"** (marzo 2026). Practitioner experience: markdown
  funcionó hasta que necesitó verificación cruzada entre agents. Pasó
  a un sistema con triples + cryptographic proofs (AIngle Protocol).
  **Importa porque**: ejemplifica el threshold real — markdown
  alcanza hasta cierto punto; KG/triples valen cuando hay multiple
  writers o necesidad de verification. Durin = single user, single
  agent: el threshold no aplica hoy.

### 3.3 Documentación de sistemas (técnica + posicionamiento)

- **Letta docs**: confirma que Letta NO trata entidades como first-class
  objects; los blocks son strings labeled. Multi-agent sharing es
  selling point. Para durin (single-agent local), poco aprovechable.

- **LangMem docs**: tipa memorias en `semantic` / `episodic` /
  `procedural` (línea cognitiva clásica). Profiles vs Collections como
  representaciones de semantic. **No tiene entity model explícito**.
  Confirma que el ecosistema más usado (LangChain) **no presiona por
  entity-centric** — está en otra dimensión taxonómica.

- **Mem0 docs**: separa `categories` (organizacionales: personal,
  professional, food...) de `entity types` (Person, Location, Event)
  en Mem0g. Default categories abren con `personal_details`,
  `professional_details`, `sports`, `travel`, `food`, `music`,
  `health`, `technology`, `hobbies`, `fashion`, `entertainment`,
  `milestones`, `user_preferences`, `misc`. **Importa porque**: el
  enfoque "categorías" de Mem0 vanilla **NO es entity-centric**; es
  topic-centric con orientación al usuario. Diferente de durin.

- **Graphiti docs (Zep)**: Pydantic-based custom entity types. Ejemplo
  estándar: `Person`, `Company`, `Product`; edge types: `Employment`,
  `Investment`, `Partnership`. **Esquema abierto + tipos custom + auto
  dedup**. Es el patrón más cercano a lo que durin necesita.

- **Cognee docs**: pipeline `Extract → Cognify → Load`. LLM extrae
  entidades y types sin schema fijo. Acepta ontologías personalizadas.
  Es schema-flexible, no schema-fija.

### 3.4 Lo que NO encontré (honesto)

- **Discusión específica sobre el límite de `place` como tipo**.
  Ningún paper lo trata como tipo crítico. Mem0g lo incluye, pero la
  cardinalidad en los datasets de benchmark es baja.
- **Best practice formal sobre slugificación canónica**. Convención
  observada: lowercase snake_case (`new_york`, `pottery_hobby`). No es
  formalizada en ningún paper; es práctica común.
- **Quantificación de "cuándo entity-centric paga el costo"**. La
  literatura asume que sí o que no según la sección; no encontré un
  cost/benefit estudio empírico.

---

## §4 — Convergencia académica vs práctica

**Donde académicos y practitioners coinciden.**

1. **Las entidades dedup por nombre exacto fallan.** GraphRAG (paper)
   y todos los blogs (Fountain City, Atlan) lo reportan. Practitioner
   side lo cita como **el** problema de producción más visible.
2. **Temporal validity es necesario para preferences/estados que
   cambian.** Zep paper + blog + Mem0 production report + casi todos
   los practitioner blogs lo dicen.
3. **Lifecycle (Q4) está sin resolver.** Survey de diciembre 2025,
   mem0.ai state of 2026, atlan, dev.to all flag it.
4. **Reflections / consolidación periódica es valioso.** Generative
   Agents (paper) + Mem0 (paper) + Lanham (blog) coinciden.

**Donde difieren.**

1. **Academia papers ENTITY-CENTRIC vs practitioners "markdown +
   semantic search bastan a menudo".** Los blogs prácticos (HN, dev.to)
   son notablemente más escépticos del overhead de KGs que los papers.
   Mem0's own report admite que full-context (no memory tiered) gana
   por <6 puntos pero a 14x el costo. La academia mide accuracy; el
   practitioner mide $/sesión + UX.

2. **Academia formaliza `valid_from`/`invalid_at` como must-have.
   Practitioners reportan que la mayoría de los sistemas en producción
   NO lo tienen y "anda igual"** porque el LLM resuelve contradicciones
   en read-time. La temporal validity es promesa de academia + Zep,
   no práctica universal.

3. **Academia confía en LLM-driven entity resolution (Zep, Mem0g,
   EDC). Practitioners reportan que cuesta caro y a veces decide mal**
   (Fountain City note sobre cross-system identity). El gap entre
   "el LLM lo decide" en paper vs "el LLM se equivoca y rompemos el
   grafo" en producción es real.

4. **Academia tiende a "más tipos = más expresivo". Practitioners
   tienden a "menos tipos = menos deuda".** Mem0 vanilla tiene
   categories abiertas pero acotadas (~15). Octoco recomienda schema
   libre pero baja cardinalidad inicial. GAAMA argumenta contra
   entities mega-hub. Las 10 tipos del doc 16 están en el sweet spot,
   pero el riesgo de "demasiados" es real según practitioners.

5. **HyperMem (SOTA LoCoMo) NO usa entity nodes.** Esto es la
   incomodidad mayor. Si el benchmark más maduro lo gana un sistema
   sin entity nodes, la pregunta "vale la pena ir entity-centric"
   queda abierta. La respuesta provisoria es que LoCoMo mide QA
   conversacional, no operación coherente cross-session sobre
   proyectos / personas / tools — el caso de durin.

---

## §5 — Conceptos que aparecen ≥3 veces

Patrones load-bearing en la literatura:

1. **Bi-temporal modeling**: `valid_at` + `invalid_at` (Zep paper,
   survey GraphMemory 2026, mem0 blog, atlan, fountain city, dev.to).
   Es el patrón canonizado para Q3.
2. **Entity dedup por embedding similarity + threshold** (HippoRAG,
   Zep, EDC, Cognee, Graphlet entity resolution blog, Mem0g). Es el
   primer corte universal para Q2.
3. **LLM-driven resolver para casos ambiguos** (Zep entity resolution
   pipeline, Mem0 UPDATE/ADD/DELETE/NOOP, GraphRAG entity summary,
   EDC canonicalization). El LLM siempre interviene cuando el embedding
   no decide claro.
4. **Reflections / consolidación periódica** (Generative Agents, A-Mem
   evolution, GAAMA reflections, Lanham "distilled playbooks"). El
   patrón "destilar observaciones en páginas" es consensuado.
5. **Episode + entity separados** (Zep episodes vs entity nodes,
   AriGraph episodic vs semantic, GAAMA episode/fact/reflection,
   doc 16 episodic/<id> vs entities/<type>). **Validación directa
   de la arquitectura del doc 16.**
6. **Snake_case / lowercase slugs como identificador canónico**
   (GAAMA concepts, Cognee normalization, EDC, KG construction blogs).
   Convención de facto.
7. **Forgetting / decay como problema NO resuelto** (Mem0 state of
   2026, atlan, dev.to vektor, HN show, survey 2025). Q4 es agujero
   reconocido. MemoryBank (Ebbinghaus) es la solución más formal pero
   recibe críticas prácticas.
8. **Description summarization para construir página de entidad**
   (GraphRAG, Zep entity summary update, A-Mem context update). El
   patrón "LLM combina N menciones en un summary canónico" es estándar.
9. **Mega-hub problem para entidades centrales** (GAAMA explícito,
   implícito en cualquier KG de single-user agent). Si una entidad
   acumula miles de edges, su utilidad como pivote se diluye.
10. **Schema custom Pydantic** (Graphiti, Cognee con ontologías). El
    patrón "el usuario define los tipos con campos" gana sobre "tipos
    fijos del framework".

---

## §6 — SOTA real (no marketing)

Benchmarks reales 2026, sin atajos:

**LoCoMo** (1540 QAs sobre conversaciones largas):

- HyperMem: 92.73% (ACL 2026) — **hipergrafo topics/episodes/facts,
  SIN entity nodes**.
- Mem0 (2026 algoritmo): 92.5% con ~6900 tokens/query.
- GAAMA: 78.9% (concept-mediated, sin person entities).
- A-MEM: ~47% (notas + dynamic links, sin entities).
- HippoRAG: 69.9%.

**LongMemEval** (ICLR 2025):

- Mem0: 94.4%.
- Zep: 63.8%.
- (Otros menos documentados).

**EverMemBench** (2026, multi-party, profile-aware):

- EverMemOS: 17.27% en multi-hop Gemini-3-Flash (mejor del benchmark).
- Otros sistemas: 3-6%. El benchmark es deliberadamente difícil.

**Lectura honesta del SOTA.**

- En **LoCoMo**, HyperMem gana sin entity nodes; Mem0 (con o sin
  entity matching extendido) es 0.2pts por debajo. **Conclusión 1:
  entity nodes formales no son condición necesaria para SOTA en QA
  conversacional**.
- En **LongMemEval**, Mem0 (con entity collections internas + multi-
  signal retrieval) gana por amplio margen sobre Zep (KG temporal
  puro). **Conclusión 2: entity matching como SEÑAL de retrieval gana,
  pero el grafo completo no es necesario**.
- En **EverMemBench**, el ganador (EverMemOS) usa event-boundary
  segmentation. Profile understanding sigue siendo el subscore más
  débil (11-58%). **Conclusión 3: profile/persona modelado sigue
  siendo el subproblema más duro y donde entity-centric debería pagar
  más — pero ningún sistema lo está clavando todavía**.
- **Mem0 production report**: la diferencia entre el mejor memory
  system y full-context (zero memory tiering, todo en contexto) es
  ~6 puntos a 14x el costo. **Conclusión 4: el ROI de cualquier
  arquitectura de memoria es modesto; el caso lo justifica más por
  costo que por accuracy**.

**Resumen.** El "SOTA real" no es entity-centric puro. Es **multi-signal
retrieval (vector + entity matching + temporal filters) con
consolidación periódica**. La pregunta para durin no es "¿entity-centric
gana?" sino "¿el caso de uso de durin justifica entity-centric por
sobre multi-signal sin entity grafo?".

---

## §7 — Recomendación parcial para Q1-Q4 (no cierra el doc 17)

### Q1 — Granularidad de tipos

**Hacia dónde apunta la literatura.**

- Schema custom + Pydantic-typed (Graphiti) gana sobre tipos fijos.
- Cardinalidad de tipos consolidables baja (5-10) en producción.
- `place` aparece poco en literatura — es el tipo más débilmente
  soportado.
- **GAAMA advierte sobre mega-hub** en `person:user` — si Marcelo
  acumula 5000 facts, deja de ser hub útil.
- Practitioners (Octoco) recomiendan **schema abierto con tipos guía,
  no fijos**.

**Implicancia para durin.** Los 10 tipos del doc 16 están **alineados
con literatura** pero con dos reservas:

- `place` está sobre-modelado; consolidarlo con `topic` o hacerlo
  opcional baja deuda inicial.
- Hace falta plan para mitigar mega-hub en `person:marcelo` y
  `project:durin` (sub-pagging o concept layer estilo GAAMA).

### Q2 — Identidad y unificación

**Hacia dónde apunta la literatura.**

- Pipeline en cascada: **(a)** embedding similarity con threshold τ,
  **(b)** full-text search por nombre/summary, **(c)** LLM resolver
  para casos ambiguos. Zep es el blueprint.
- Synonymy edges (HippoRAG) como alternativa SOFT — no decide merge,
  agrega edges fuertes.
- Slug canónico en snake_case lowercase es convención de facto.
- **Cross-system identity (email vs username) sigue siendo agujero
  abierto en TODOS los sistemas.**

**Implicancia para durin.** Para single-user CLI, una versión liviana
basta: **slug manual (canonical name) + embedding match para detectar
nuevos aliases + LLM resolver solo cuando aparece nombre con embedding
en zona gris (threshold dual: definitivo dedup arriba de τ_high,
definitivo nuevo abajo de τ_low, LLM en medio)**. No necesita full-
text search separado; con grep sobre el repo alcanza.

### Q3 — Conflictos y evolución

**Hacia dónde apunta la literatura.**

- Bi-temporal invalidation (Zep) es el patrón canonizado para
  preservar historia.
- Mem0 ADD/UPDATE/DELETE/NOOP es la versión destructiva pero más
  simple.
- Por tipo varía: `incident` típicamente append (las lecciones se
  acumulan), `person.preferences` típicamente override+history,
  `project.decisions` típicamente append con `superseded_by` link.

**Implicancia para durin.** Una posición intermedia: **invalidate +
preserve last N versions inline, no full bitemporal**. Para entries
`user_authored` (corrections, identity), NO mutar — son ground truth.
Para entries `agent_created`, el dream puede invalidar (con
`invalid_at` lógico) sin borrar. Override total (delete) solo cuando
la nueva observación literal corrige la vieja (ej: "no era 2025, era
2026").

### Q4 — Lifecycle

**Hacia dónde apunta la literatura.**

- **Es el agujero universal.** Ningún sistema en producción lo tiene
  resuelto bien.
- MemoryBank (Ebbinghaus decay) es el patrón académico, recibe
  escepticismo práctico en HN.
- "Type-conditional half-life" (`person` nunca decae, `topic` decae
  si no se accede en 60 días) tiene argumento defensible (mtrifonov
  HN comment).
- Supermemory es el único que trata `expiration` como first-class.

**Implicancia para durin.** Probablemente la decisión correcta es
**lifecycle determinista por tipo, no decay numérico**:

- `person`, `project`: nunca archivan automáticamente.
- `incident`: archivable si no se referencia en N sesiones consecutivas.
- `topic`: candidato a decay/merge si baja densidad de observaciones
  nuevas.
- `place`, `tool`: como `topic`.

Si necesitás scoring, el más conservador es **count de referencias
recientes**, no formula Ebbinghaus. Más simple, más auditable.

---

## §8 — Tres riesgos identificados

Por si la conversación quiere encararlos antes del doc 17:

1. **El SOTA en LoCoMo lo gana un sistema sin entity nodes.** Si la
   defensa de entity-centric en durin es "porque mejora retrieval",
   esa defensa **no se sostiene contra HyperMem**. La defensa que sí
   se sostiene es: "porque mejora coherencia operativa cross-sesión
   sobre los mismos proyectos/personas — un caso que LoCoMo no testea".
   Convendría escribir explícitamente cuál es el outcome operativo
   esperado.
2. **El mega-hub en `person:marcelo` o `project:durin`** es real y
   se va a manifestar entre 3-12 meses de uso. GAAMA lo documentó.
   El doc 17 debería incluir mitigación (sub-paging, concept layer,
   sharding por sub-tipo).
3. **Cross-identity (email vs username vs git author)** no lo resuelve
   nadie en la literatura. Si durin va a unificar Marcelo entre git
   commits, emails y conversaciones, **va a inventar acá**. No copiar
   un sistema; resolverlo originalmente o vivir con identidades
   separadas.

---

## §9 — Bibliografía consolidada

Papers (orden cronológico):

- Park, J.S., et al. *Generative Agents: Interactive Simulacra of Human
  Behavior*. UIST 2023. https://arxiv.org/abs/2304.03442
- Shinn, N., et al. *Reflexion: Language Agents with Verbal
  Reinforcement Learning*. NeurIPS 2023. https://arxiv.org/abs/2303.11366
- Zhong, W., et al. *MemoryBank: Enhancing Large Language Models with
  Long-Term Memory*. arXiv:2305.10250 (May 2023).
- Packer, C., et al. *MemGPT: Towards LLMs as Operating Systems*.
  arXiv:2310.08560 (October 2023).
- Edge, D., et al. *From Local to Global: A Graph RAG Approach...*
  arXiv:2404.16130 (April 2024).
- Gutiérrez, B.J., et al. *HippoRAG: Neurobiologically Inspired Long-
  Term Memory for LLMs*. NeurIPS 2024. https://arxiv.org/abs/2405.14831
- Zhang, B., et al. *Extract, Define, Canonicalize: An LLM-based
  Framework for Knowledge Graph Construction*. NAACL 2024.
  https://arxiv.org/abs/2404.03868
- Anokhin, P., et al. *AriGraph: Learning Knowledge Graph World Models
  with Episodic Memory for LLM Agents*. arXiv:2407.04363 (July 2024).
- Rasmussen, P., et al. *Zep: A Temporal Knowledge Graph Architecture
  for Agent Memory*. arXiv:2501.13956 (January 2025).
- Gutiérrez, B.J., et al. *From RAG to Memory: Non-Parametric
  Continual Learning for LLMs*. ICML 2025. arXiv:2502.14802.
- Xu, W., et al. *A-MEM: Agentic Memory for LLM Agents*.
  arXiv:2502.12110 (February 2025).
- Chhikara, P., et al. *Mem0: Building Production-Ready AI Agents with
  Scalable Long-Term Memory*. ECAI 2025. arXiv:2504.19413.
- Liu, S., et al. *Memory in the Age of AI Agents: A Survey of Forms,
  Functions and Dynamics*. arXiv:2512.13564 (December 2025).
- Yang, C., et al. *Graph-based Agent Memory: Taxonomy, Techniques,
  and Applications*. arXiv:2602.05665 (February 2026).
- Cheng, Z., et al. *EverMemBench: Benchmarking Long-Term Interactive
  Memory in Large Language Models*. arXiv:2602.01313 (February 2026).
- *Memory Matters More: Event-Centric Memory as a Logic Map for Agent
  Searching and Reasoning*. arXiv:2601.04726 (January 2026).
- Paul, S.K., et al. *GAAMA: Graph Augmented Associative Memory for
  Agents*. arXiv:2603.27910 (March 2026).
- Yue, J., et al. *HyperMem: Hypergraph Memory for Long-Term
  Conversations*. ACL 2026. arXiv:2604.08256.

Blogs / threads:

- Hacker News thread "Ask HN: Are we close to figuring out LLM/Agent
  Memory" (~mar 2026). https://news.ycombinator.com/item?id=47449389
- Hacker News thread "Show HN: AI memory with biological decay (52%
  recall)" (~may 2026). https://news.ycombinator.com/item?id=47914367
- Zep blog. "Stop Using RAG for Agent Memory" (junio 2025).
  https://blog.getzep.com/stop-using-rag-for-agent-memory/
- Mem0 blog. "State of AI Agent Memory 2026" (mayo 2026).
  https://mem0.ai/blog/state-of-ai-agent-memory-2026
- Lanham, M. "Memory, Not Magic: What Agents Actually Remember Between
  Sessions" (abril 2026). https://medium.com/@Micheal-Lanham/memory-not-magic-what-agents-actually-remember-between-sessions-c05dadb53dc7
- Lintvelt, H. "Knowledge Graphs as Memory". https://www.octoco.ai/blog/knowledge-graphs-as-memory
- Fountain City. "Agent Memory & Knowledge Systems Compared (2026)"
  (mayo 2026). https://fountaincity.tech/resources/blog/agent-memory-knowledge-systems-compared/
- Vectorize.io. "Zep (Graphiti) vs Cognee" (2026).
  https://vectorize.io/articles/zep-vs-cognee
- Atlan. "Best AI Agent Memory Frameworks in 2026".
  https://atlan.com/know/best-ai-agent-memory-frameworks-2026/
- dev.to (Vektor). "The State of AI Agent Memory in 2026" (mayo 2026).
  https://dev.to/vektor_memory_43f51a32376/the-state-of-ai-agent-memory-in-2026-what-the-research-actually-shows-3aja
- dev.to (Eahm60). "I replaced my agents markdown memory with a
  semantic graph" (marzo 2026). https://dev.to/eahm60/i-replaced-my-agents-markdown-memory-with-a-semantic-graph-1elp
- Shereshevsky, A. "Entity Resolution at Scale: Deduplication Strategies
  for Knowledge Graph Construction" (enero 2026). https://medium.com/@shereshevsky/entity-resolution-at-scale-deduplication-strategies-for-knowledge-graph-construction-7499a60a97c3

Docs técnicas / repos:

- Graphiti custom entity types. https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types
- Mem0 memory types. https://docs.mem0.ai/core-concepts/memory-types
- Mem0 custom categories. https://mem0.ai/blog/understanding-custom-categories-in-mem0
- Letta agent memory. https://www.letta.com/blog/agent-memory
- Letta memory blocks. https://www.letta.com/blog/memory-blocks
- LangMem conceptual guide. https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
- GraphRAG (Microsoft). https://microsoft.github.io/graphrag/
- Cognee cognify pipeline. https://docs.cognee.ai/core-concepts/main-operations/cognify
- Awesome-GraphMemory. https://github.com/DEEP-PolyU/Awesome-GraphMemory
- Agent-Memory-Paper-List. https://github.com/Shichun-Liu/Agent-Memory-Paper-List

---

## Last updated: 2026-05-23
