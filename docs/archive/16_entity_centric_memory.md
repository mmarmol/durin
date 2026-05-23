# 16 — Memoria entity-centric: estado de la discusión

> Mayo 2026. Documento de **discusión, no de plan**. Captura el modelo
> mental que cerramos en sesión sobre cómo va a operar la memoria de
> durin a mediano plazo, y aísla la pieza más crítica — el modelo de
> entidades — para validarla contra el estado del arte antes de
> implementar.
>
> El usuario marcó la regla: **tener un sistema y entidades muy
> confiables importa más que sumar features**. Este documento existe
> para evitar elegir el modelo de entidades por intuición.

---

## §1 — La arquitectura consolidada (lo que ya cerramos)

Tras la auditoría de Phase 1+2, las correcciones de embedding (familias
1/2/3) y la conversación sobre dónde encaja background_review, el
modelo mental quedó así:

### 1.1 Tres flujos cooperando, una sola superficie de búsqueda

```
                       ┌─────────────────────────────────┐
                       │  Modelo (turno del agente)      │
                       │                                 │
                       │  • Detecta aprendizaje          │
                       │  • Llama memory_store           │
                       │  • Stamp en meta:               │
                       │    type=memory_write            │
                       └────────────┬────────────────────┘
                                    │
                                    ▼
            ┌───────────────────────────────────────────┐
            │ Indexación pre-dream (siempre)            │
            │                                           │
            │ Vector:                                   │
            │   • memory entries (Phase 2, hecho)       │
            │   • último compaction summary por sesión  │
            │   • session-close summary (para sesiones  │
            │     cortas, gen. cuando aplique)          │
            │                                           │
            │ Grep:                                     │
            │   • sessions/<key>.md                     │
            │   • meta event timeline                   │
            └────────────┬──────────────────────────────┘
                         │
                         ▼ (eventualmente, fuera de sesión)
       ┌─────────────────────────────────────────────────┐
       │ Dream — entity-centric, no session-centric      │
       │                                                  │
       │ • Mira TODAS las memory entries acumuladas      │
       │ • Agrupa por entidad                            │
       │ • Consolida en memory/entities/<type>/<value>.md│
       │ • Resuelve contradicciones, unifica, archiva   │
       │ • Reemplaza summaries previos por el último    │
       │   "qué fue esta sesión" (índice, no conoc.)    │
       └─────────────────────────────────────────────────┘
```

### 1.2 Descartes que la conversación produjo

| Idea anterior | Descartada porque |
|---|---|
| `background_review` post-turn (propuesta C) | Modelo inline + indexación de sesión + dream cubren su rol sin el costo extra |
| Dream session-centric (un dream pasa, procesa una sesión) | El usuario lo movió a **entity-centric**: el dream toma todas las entidades acumuladas y consolida sin importar de qué sesión vinieron |
| Cursor global `dream/cursor.json` | Reemplazado por cursor per-sesión en `<key>.meta.json::derived.dream_processed_through_msg_idx` |
| Scoring multi-factor estilo OpenClaw (frequency/relevance/diversity/recency/...) | No aplica directo bajo modelo entity-centric: lo que importa es "esta entidad acumuló N observaciones, vale destilar página" — heurística más simple |
| Acumular summaries de TODAS las compactaciones en el índice | Tu propuesta de "indexación migrando capas": solo el ÚLTIMO summary se mantiene post-dream; los previos se borran del índice cuando sus aprendizajes ya migraron a `entities/` |

### 1.3 Lo que se conserva de propuestas previas

- **Propuesta A (entities tipadas)**: formato `type:value`, vocabulario abierto, validación de forma. Se vuelve MÁS importante bajo el modelo entity-centric (los tipos dirigen la consolidación). Detalles ajustados en §3.
- **Propuesta D (vector embed enriquecido)**: ya implementada, sin cambios.
- **Provenance `_MEMORY_AUTHOR` ContextVar**: se conserva. Distingue `agent_created` (dream puede tocar) de `user_authored` (intocable). Único sobreviviente conceptual de la propuesta C.

---

## §2 — La pieza compleja: entidades como first-class nodes

### 2.1 De etiquetas a nodos vivos

En Phase 1 las entidades son una `list[str]` plana en el frontmatter de
cada memory entry — etiquetas decorativas. Bajo el modelo del usuario
para el dream, **las entidades pasan a ser objetos con identidad
propia, persistencia y ciclo de vida**:

```
memory/
├── episodic/<id>.md           ← entries crudas (lo que el modelo guardó inline)
├── entities/
│   ├── person/marcelo.md      ← página viva: todo lo que sabemos de Marcelo
│   ├── person/sergio.md
│   ├── project/durin.md       ← página viva
│   ├── project/hermes.md
│   ├── place/oficina.md
│   ├── topic/autocompact.md
│   ├── incident/webui-crash-2026-05-15.md
│   └── tool/memory_store.md
├── stable/                    ← identity, corrections (sin cambio)
├── corpus/                    ← queryable archive (sin cambio)
└── pending/                   ← prospective (sin cambio)
```

Cada `entities/<type>/<value>.md` es una página que el dream actualiza:

- **Crece** cuando aparecen nuevas observaciones sobre la entidad
- **Resuelve contradicciones** cuando dos entries dicen cosas
  incompatibles ("Marcelo prefiere X" vs "Marcelo ya no usa X")
- **Unifica** duplicados ("durin" + "Durin" + "durin-agent" → una sola
  entidad canónica)
- **Archiva** cuando la información queda obsoleta o irrelevante

Las entries `episodic/<id>.md` son **observaciones puntuales** — el
material crudo. Las páginas `entities/<type>/<value>.md` son el
**conocimiento consolidado**.

### 2.2 Por qué esto es la decisión más difícil de la arquitectura

Es la única pieza donde una mala decisión arquitectónica produce deuda
permanente:

- **Si elegimos mal los tipos**: agregamos `place` después y el corpus
  ya está poblado sin él → o migramos retroactivamente (costoso) o
  vivimos con inconsistencia.
- **Si la unificación es frágil**: `durin` / `Durin` / `durin-agent` /
  `proyecto durin` quedan como entidades distintas y el grafo se
  fragmenta. La consolidación nunca converge.
- **Si la resolución de contradicciones es naive**: el dream
  reemplaza "Marcelo usa pytest" con "Marcelo usa unittest" cuando lo
  correcto era guardar la evolución temporal.
- **Si el ciclo de vida no está claro**: páginas de entidades crecen
  para siempre, se vuelven inusables, el modelo deja de aprovecharlas.

Estos cuatro problemas son los que cualquier sistema de memoria con
entidades enfrenta. Hay literatura. Hay implementaciones. **Vale la
pena revisarlas antes de comprometernos.**

### 2.3 Las cuatro preguntas a responder

| Pregunta | Por qué importa |
|---|---|
| **Q1 — Granularidad** | ¿`file` y `symbol` son entidades o solo refs? ¿`decision` y `event` son entidades temporales? Demasiados tipos = grafo explosivo. Pocos = pérdida de poder expresivo |
| **Q2 — Identidad y unificación** | ¿Cómo decide el sistema que `Marcelo`, `marcelo`, `Marcelo M.` y `mmarmol@mxhero.com` son la misma persona? Resolución por alias, embedding, LLM, regla manual? |
| **Q3 — Conflictos y evolución** | Cuando dos observaciones se contradicen sobre la misma entidad: ¿override (la nueva gana), append (lista temporal), merge (LLM resuelve)? |
| **Q4 — Lifecycle** | ¿Cómo se archiva una página? ¿Soft delete, decay temporal, threshold de relevancia? ¿Quién lo decide — el dream con scoring, una regla determinista, el usuario? |

---

## §3 — Set inicial propuesto (amplio y podar)

Approach: **set amplio sugerido, no enforced**. durin es agente genérico
(coders, marketing/sales, estudiantes, daily-life), no dev-only. Los tipos
emergen como hints para el dream LLM; si después de uso real un tipo no
acumula nada, se poda. Si emerge necesidad de uno nuevo, se agrega — el
schema permanece abierto via `entityType` libre en frontmatter.

### 3.1 Grounding académico

El set se ancla en literatura cognitiva consolidada:

- **Tulving (1972, 1985)** — tripartite memory: semantic (hechos sobre el
  mundo y entidades), episodic (experiencias con contexto temporal),
  procedural (cómo hacer cosas).
- **CoALA (Sumers et al., 2023)** — adapta Tulving a agents LLM:
  working memory + long-term (episodic + semantic + procedural). Es el
  framework más adoptado en literatura agentic 2024-2026.
- **Conway (1996)** — autobiographical memory: lifetime periods, general
  events, event-specific knowledge, identity-relevant facts.
- **Rosch — prototype theory**: la mente NO usa categorías rígidas; usa
  prototipos con bordes difusos. Una memoria puede caer en intersección
  de varias categorías. Implicancia: tipos sugeridos + tolerancia a
  overlap, no enforcement.
- **Generative Agents (Park 2023)**: observations / reflections / plans.
  Las reflections (incluye "aprendizaje", "error") son **derivadas**, no
  tipos primarios — emergen de procesar observations.

### 3.2 Set sugerido (8 tipos)

Cubre cross-profession y mapea directo a Tulving/CoALA. Cada tipo
aplica naturalmente a coders, marketing/ventas, estudiantes, y daily-life.

| Tipo | Qué es | Mapeo Tulving | Ejemplos cross-profession |
|---|---|---|---|
| `person` | Cualquier humano (self, user, otros mencionados) | Semantic | coworker, client lead, profesor, familia |
| `place` | Locación significativa | Semantic | oficina, mercado, campus, casa |
| `project` | Endeavor goal-directed (recurring activity) | Semantic | durin, Q3 campaign, tesis, mudanza |
| `topic` | Concepto / área de conocimiento abstracta | Semantic | embeddings, B2B funnels, ML, minimalismo |
| `event` | Ocurrencia time-bound (incluye incidents) | Episodic | el outage, el demo, el examen, cumpleaños |
| `artifact` | Cosa concreta (archivo, documento, herramienta, producto) | Semantic | settings.py, deck v3, textbook, pasaporte |
| `stance` | Preferencia, opinión, belief, posición subjetiva | Semantic | "prefiero pytest", "TikTok no convierte", "amo historia" |
| `practice` | Skill, rutina, método, hábito | Procedural | TDD, morning standup, spaced repetition, meditación |

**Cosas que NO son tipos primarios** (emergen como derivadas):

- **"aprendizaje"** → consolidado de `topic` o actualización de `practice`
  o `event` con reflection asociada. Es la "reflection" de Generative
  Agents.
- **"error"** → `event` con valencia negativa, o `stance` corregido, o
  `practice` actualizada.
- **"decisión"** → `event` puntual con `stance` asociado, no entidad
  evolutiva propia.
- **"file"**, **"symbol"** → caen en `artifact` (concretos) o se
  referencian desde frontmatter de entries sin necesitar página.
- **"tool"** del doc original → herramienta concreta = `artifact`;
  método/proceso de uso = `practice`.

### 3.3 Decisiones explícitamente abiertas

- **¿Cómo se llama una persona?** Por nombre? Email? Slug? Si `Marcelo` y
  `marcelo` son la misma, ¿bajo qué slug se canoniza? Igual para proyectos
  (`Durin` vs `durin` vs `durin-agent`). Resuelve: §3.5 (alias index) y
  pipeline dedup futuro.
- **¿`stance` vs `preference` vs `belief` como nombre?** Los tres apuntan
  a lo mismo cognitivamente. `stance` es más neutral y cubre los otros
  dos. Sujeto a revisión si en uso real un término gana naturalidad.
- **¿`place` aporta o sobra para coders / dev-heavy?** Empirical: si en
  6 meses no acumula nada en uso real, se poda. Justificación pre-uso:
  cubre audience no-coder y Tulving/Conway lo respaldan.
- **¿Vocabulario completamente abierto sin lista sugerida?** Se descartó:
  riesgo de explosión de tipos casi-equivalentes (`entities/cosa/`,
  `entities/thing/`, `entities/concepto/`). La lista sugerida funciona
  como ancla para el LLM consolidador.

### 3.4 Consolidación y consistencia — read-time reconciliation

Sobre cuándo y cómo consolida el dream (post-discusión doc 17 §3 D2):

- El **dream no es punto-de-consistencia, es punto-de-compresión**. Las
  entries episódicas son first-class para retrieval desde el momento en
  que se escriben, sin depender del dream.
- Si el dream no corrió todavía sobre entries recientes, esas entries
  siguen siendo buscables como objetos independientes. La página
  consolidada y las entries post-cursor coexisten en los resultados de
  retrieval; el LLM reconcilia en read-time con base en timestamps y
  contexto.
- Esto desacopla el trigger del dream del momento de coherencia. El
  trigger puede ser session-end, idle timer, batch nocturno, o
  threshold-based por entidad — la elección depende de costo y UX, no
  de correctness.
- **Anti-frágil**: si el dream crashea o no corre por días, el sistema
  degrada suavemente. Las entries siguen siendo buscables; solo el
  contexto crece sin compresión.

### 3.5 Entity-aware retrieval — mínimo L1 + medición direccional

El retrieval entity-aware tiene un mínimo no-opcional ("L1 light") que
todos los sistemas que tipan entidades implementan porque sin él los
casos básicos se rompen (`Marcelo` vs `marcelo`, alias, contexto stale).
Técnicas más sofisticadas (graph traversal, cross-encoder, PageRank,
bi-temporal) no tienen ganador claro en benchmarks publicados — se
defieren con medición.

**Mínimo L1 light:**

*Write-time:*

1. `aliases: [...]` en frontmatter de cada `entities/<type>/<slug>.md`.
2. `dream_processed_through: <msg_idx|timestamp>` en frontmatter — cursor
   por entidad.
3. `entities: [type:slug, ...]` en frontmatter de cada entry episódica
   (propuesta A del doc 14 — dependencia explícita).
4. Aliases index sidecar (`memory/.aliases.json` o equivalente):
   `alias_string → entity_slug`. Regenerable parseando frontmatters
   al boot.

*Read-time:*

5. Extracción de entidades del query: regex/string match contra aliases
   index. NO LLM call. Cubre ~80%; lo no cubierto cae a vector search puro.
6. Boost a entries post-cursor con tag matcheado.
7. Demote a entries pre-cursor con tag matcheado (su info está en la
   página).
8. La página canónica surface naturalmente vía vector.

**Medición direccional incluida en el mínimo:**

Para que la decisión L2+ futura sea empírica, dos instrumentaciones
baratas son parte del mínimo:

9. **Telemetría de retrieval** (costo: ~50 LOC + tabla/JSONL). Loggear
   por query: `query_text`, `entities_extracted`, `candidates_returned`,
   `candidates_with_matching_tag_NOT_returned`, `llm_actually_referenced`.
   El bucket "tag matcheado pero no retornado" es el indicador principal
   de si L1 está perdiendo cosas estructurales (señal a favor de L2).
10. **Test de variaciones de embedding** (one-off, ~1 hora). Script que
    mida cosine similarity entre pares conocidos del corpus durin:
    `(Marcelo, marcelo)`, `(Marcelo, Marcelito)`, `(Marcelo Marmol,
    mmarmol@mxhero.com)`, `(durin, durin-agent)`, etc. Si embeddings
    acercan las variaciones, alias expansion explícito tiene menos urgencia;
    si no, es crítico de día uno.

**Diferido a benchmark / experiment (L2+):**

Las técnicas siguientes no se adoptan ciegamente — se evalúan con
experimento controlado cuando emerja necesidad:

- Synonymy edges via cosine > 0.9 (versión soft de HippoRAG, sin DB
  graph). Experimento direccional barato (~80 LOC + batch script).
- Cross-encoder reranking (Graphiti pattern).
- PageRank / traversal multi-hop sobre grafo (requiere sidecar β del
  §3.4).
- Page-first intent detection.
- LLM-based entity extraction del query (vs regex/alias index).
- Bi-temporal validity per claim (`valid_from`/`valid_to`).
- LLM-as-judge sampling periódico sobre queries reales.
- Benchmark público (LoCoMo / EverMemBench /
  [[reference-memory-validation-benchmarks]]).

Disparadores para reabrir L2+:
- Telemetría (#9) muestra el bucket "no retornado" creciendo o las
  respuestas del LLM degradando.
- Decisión binding de validar contra benchmark público.
- Corpus a escala donde graph traversal tiene superficie (>10k entidades,
  queries multi-hop frecuentes).

### 3.6 Storage físico — decisión diferida

Las páginas de entidad viven como markdown files
(`memory/entities/<type>/<value>.md`). Esa es la fuente de verdad. Tras la
discusión post-research (doc 17 §3 D1) se evaluó si conviene agregar un
**sidecar derivado de grafo** (SQLite con tablas de edges y referencias
normalizadas) que resolvería bidireccionalidad enforced, queries multi-hop
y atomicidad parcial — al costo de un indexer adicional y disciplina de
schema.

**Decisión: diferir.** Doc 08 ya planteó dos sidecars sobre LanceDB
(Phase 2b vectores — implementado; Phase 2c BM25 lexical — propuesto, no
implementado). Si emerge dolor por queries multi-hop o inconsistencia
entre archivos de entidad, evaluar agregar un sidecar de grafo. El
indexer compartiría infraestructura con BM25 si se activa también
(parser único de markdown → poblar ambas tablas). Pasar de markdown-puro
a markdown+sidecar es aditivo, no requiere migración de datos. Reabrir
solo cuando el dolor sea real, no anticipado.

### 3.7 Versionado y tracing — git como substrato interno

Post-discusión doc 17 §3 D4 sobre entity-centric vs alternativas: las
consolidaciones (páginas de entidad regeneradas por el dream) deben
preservar historial completo de qué mudó, por qué, qué entries se usaron
y el diff exacto. Decisión: **`memory/` es un repo git interno** que
durin gestiona invisiblemente al user.

**Modelo:**

- `memory/` se inicializa como repo git en `durin install` /wizard.
- **Único actor que escribe**: durin (dream + write paths internos). El
  user no commitea ni edita directamente las páginas — esa capacidad
  queda fuera de scope inicial (no cerrada para siempre; reabrir si emerge
  demanda).
- Cada consolidación del dream produce **dos outputs**: (a) el contenido
  nuevo del archivo, (b) un commit message con razonamiento estructurado.
  Ambos los genera el dream LLM como parte de su prompt.
- durin ejecuta `git add` + `git commit` por debajo con author fijo
  `durin-dream <dream@durin.local>` (write paths raw pueden usar
  `durin-write` para distinguir).

**Formato de commit message (LLM-generated):**

```
Consolidate <entity-id> (rev N)

<cuerpo explicando razón del cambio en lenguaje natural>

Sources: episodic/<id1>.md, episodic/<id2>.md, ...
Entities-touched: <type:slug>
Entities-referenced: <type:slug>, <type:slug>
Dream-session: <id>
Cursor-before: <msg_idx>
Cursor-after: <msg_idx>
```

El cuerpo es justificación humano-leíble; los trailers son estructurados
para que durin pueda parsearlos vía `git log --format=...`.

**Repo local-only, sin remoto.** El repo git de `memory/` es **estrictamente
local** — durin no configura ni sugiere remote. La memoria del user no se
publica ni se sincroniza por defecto. Si en el futuro un user quisiera
sincronizar entre sus máquinas, debería ser opt-in explícito y con
controles de privacidad — pero **eso está fuera de scope** y no se
configura desde durin.

**Lo que git provee como substrato local:**

- "qué mudó" → `git diff <prev>..<curr> entities/<type>/<slug>.md` exacto.
- "por qué" → cuerpo del commit message.
- "entidades usadas" → trailers `Entities-touched` / `Entities-referenced`.
- "cambio exacto" → git diff es exacto.
- Inmutabilidad → garantizada para versiones pasadas (no requiere
  `supersedes`/`superseded_by` custom).
- Anti-fragilidad → `git revert` deshace una consolidación mala.
- Tracing/auditing → `git log`, `git blame` sobre cualquier página.

**`.gitignore` mínimo recomendado para `memory/`:**

```
# Sidecars derivados (regenerables desde markdown)
*.lance/
vectors/
.aliases.json
.usage.json
.usage/

# Estado runtime
.dream.lock
.locks/
```

**Comandos durin expuestos como wrappers de git:**

- `durin memory history <entity>` → `git log entities/<type>/<slug>.md` formateado.
- `durin memory diff <entity> <revs>` → `git diff` formateado.
- `durin memory revert <commit>` → para deshacer consolidación mala.

**Lo que NO entra en esta fase** (deferred, no cerrado):

- Edición manual del user sobre las páginas. Si emerge demanda, el
  patrón natural es: detectar diff vs HEAD antes del próximo dream,
  auto-commitear con author `durin-user-edit`, marcar las líneas
  afectadas como `user_authored` (existe `_MEMORY_AUTHOR` ContextVar en
  el código actual).
- Sync/remoto: explícitamente fuera de scope. Memoria es local.
- Branching (no necesario para memoria single-user).

---

## §4 — Lo que este documento NO hace

- **No es plan de implementación**. No fija fechas ni asigna sprints.
- **No cierra las cuatro preguntas Q1-Q4**. Las identifica como
  entrada a la fase de investigación.
- **No define el operacional del dream**. El dream queda como horizonte;
  este documento solo describe qué espera consumir de él.
- **No toca el código**. Las entidades tipadas (propuesta A) y el
  resto del trabajo de Phase 1+2 siguen como están.

---

## §5 — Plan de investigación

El usuario marcó la dirección: contrastar contra sistemas reales y
buscar discusión académica antes de cerrar el modelo. **Comparación de
código, no solo de docs.**

### 5.1 Sistemas open-source a leer / clonar

Ordenados por cercanía conceptual al modelo entity-centric que estamos
considerando:

| Sistema | Estado local | Por qué evaluar |
|---|---|---|
| **Hermes — Holographic plugin** | clonado (`~/git_personal/hermes-agent/plugins/memory/holographic/`) | El único de Hermes con tabla `entities` explícita en SQLite. Ya identificado en doc 14 como "anti-patrón" (entity_type declarado pero nunca asignado). Vale verificar cómo lo usa en lecturas |
| **OpenClaw** | clonado (`~/git_personal/openclaw/`) | `category` enum cerrado por entry. NO tiene entidades como first-class. Útil para entender qué NO hacer y por qué |
| **Cognee** | falta clonar | "Knowledge graph + LLM" como abstracción central. Pipeline `extract → cognify → improve`. Sus primitivas (Nodes, DataPoints) son lo más cercano al modelo entity-centric |
| **Graphiti** | falta clonar | Temporal knowledge graph para agentes. Maneja explícitamente `valid_from` / `invalid_at` para resolver Q3 (evolución temporal) |
| **Mem0** | falta clonar (open-source release) | Server-side fact extraction con dedup explícito. Manejo de Q2 (unificación) maduro |
| **MemPalace** | falta clonar | "Spatial memory for LLM agents". Vale ver si su modelo de "habitaciones" se traduce a entidades |
| **HippoRAG / HippoRAG 2** | falta clonar | NeurIPS 2024 + 2025. Personalized PageRank sobre KG construido por LLM. Resuelve recall en grafos densos |
| **A-Mem** | falta clonar (Princeton) | Zettelkasten-inspired memory para agentes. Notas + links explícitos, dynamic linking |

### 5.2 Papers académicos a revisar

| Paper / Trabajo | Por qué |
|---|---|
| **Generative Agents** (Park et al., Stanford, 2023) | Memoria reflexiva con árbol de "reflections" sobre observaciones. Patrón temprano de consolidación |
| **MemGPT / Letta** (Berkeley, 2023-25) | Memoria jerárquica: core / archival. Modelo más cercano a stable/episodic/corpus |
| **HyperMem** (ACL 2026) | SOTA en LoCoMo (92.73%). Estructura de memoria que ganó el benchmark — vale ver |
| **Reflexion** (Shinn et al., 2023) | Memoria episódica de failures explícita |
| **GraphRAG** (Microsoft, 2024) | Knowledge graph construction from LLM + community detection. Resuelve Q1 (granularidad) |
| **Cognee paper / blog** | Pipeline `extract → cognify → improve` documentado |
| **Mem0 paper** | "Building Production-Ready AI Agents with Scalable Long-Term Memory" |
| **A-Mem paper** (arXiv 2502.12110) | Zettelkasten patrón |

Plus **discusión activa**:
- Buscar discusiones recientes sobre "entity-centric agent memory"
- Hilos de LangChain / LlamaIndex / Mem0 sobre cómo modelan entidades
- Threads sobre "knowledge graph for AI agents"

### 5.3 Preguntas concretas que la investigación debe responder

Estas son las salidas que la fase de investigación debe producir, una
por sistema/paper:

1. **Modelo de identidad**: ¿cómo identifica entidades únicas? (slug
   manual, normalización, embedding match, LLM resolution)
2. **Granularidad**: ¿qué tipos modela como entidad? ¿Cuántos tipos
   típicamente? ¿Abierto o cerrado?
3. **Evolución / conflictos**: ¿cómo maneja Q3?
4. **Lifecycle**: ¿soft delete? ¿decay? ¿quién lo gobierna?
5. **Retrieval entity-aware**: ¿la búsqueda usa el grafo de entidades
   o trata todas las entries por igual?
6. **Costo operacional**: ¿LLM calls por consolidación? ¿offline o
   inline?
7. **Lección directa para durin**: una línea de qué adoptar / descartar.

---

## §6 — Criterios para cerrar las decisiones post-investigación

Una vez completada la fase de investigación, las decisiones se cerrarán
contra estos criterios:

- **Resolución de Q1 (granularidad)**: lista cerrada de tipos
  consolidables/referenciables, validada contra al menos 3 sistemas
  estudiados. El set propuesto en §3 es el punto de partida — puede
  cambiar.
- **Resolución de Q2 (unificación)**: mecanismo concreto elegido (no
  "el LLM lo decide"). Si es LLM, qué prompt + cuándo + con qué
  fallback. Si es slug-based, cómo se inicializa.
- **Resolución de Q3 (conflictos)**: política explícita (override /
  append / merge / temporal) por tipo de entidad. Puede variar entre
  `person` (típicamente override de preferencias) y `incident`
  (típicamente append de aprendizajes).
- **Resolución de Q4 (lifecycle)**: regla determinista para archivado.
  El curador del dream debe poder ejecutarla sin LLM call en el caso
  común.
- **Costo amortizado por sesión**: estimación numérica antes de
  implementar. Si el dream cuesta > $0.10/sesión consolidada con Haiku,
  reevaluamos.

---

## §7 — Lo que se mantiene operacional mientras investigamos

La conversación no bloquea el trabajo en curso:

- ✅ Propuesta A (entities tipadas, formato `type:value`) — implementable
  ahora con los 10 tipos como starting point. Si la investigación
  cambia el set, la migración es por prompt change (los entries
  existentes son tolerantes en lectura).
- ✅ Indexación de compaction summaries en LanceDB — implementable ahora
  como extensión de la familia 2 ya cerrada. Independiente del modelo
  de entidades.
- ✅ Evento `type=memory_write` en meta — implementable ahora.
- ✅ Cursor `dream_processed_through_msg_idx` per-sesión en meta —
  implementable ahora como campo nuevo, sin consumidor todavía.
- ✅ Idle-timer session-close summary con salida `(nothing)` —
  implementable ahora.
- ⏸ `memory/entities/<type>/<value>.md` layout — **NO implementar
  todavía**. Espera resultado de la investigación.
- ⏸ Dream — sin código todavía. Espera resultado.

---

## §8 — Próximo paso

Cuando este documento esté revisado:

1. Clonar los sistemas open-source faltantes a `~/git_personal/`
2. Ejecutar la investigación contra los 7 criterios de §5.3
3. Producir doc 17 con tabla comparativa + recomendación cerrada para
   cada Q1-Q4
4. Decidir en conjunto el modelo final
5. Recién entonces, planificar implementación de `entities/<type>/`

**No proceder a implementación del dream o de `entities/` antes del
doc 17 cerrado.**

---

## Last updated: 2026-05-23 (post-conversación entity-centric)
