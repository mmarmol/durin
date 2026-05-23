# 17 — Síntesis de research entity-centric

> Síntesis de los tres outputs en `docs/research/` (16a, 16b, 16c) contra la
> propuesta de `docs/16_entity_centric_memory.md`. Estructurado en tres
> categorías: **convergencias** (lo que la muestra hace y nosotros también
> proponemos), **divergencias intencionales** (donde nos apartamos del
> consenso con justificación), y **gaps reales** (donde ellos cubren algo
> que doc 16 no contempla — la parte más importante).
>
> Cierra con riesgos no mencionados en doc 16 y recomendaciones revisadas
> para Q1-Q4.

---

## §1 — Material de entrada

| Doc | Cobertura | Líneas |
|---|---|---|
| 16a | 12 sistemas open-source ya clonados: Hermes (8 plugins + curator + skill manager + background_review), OpenClaw (memory-lancedb, memory-core, memory-wiki, active-memory), OpenClaude, OpenHands | 1485 |
| 16b | 6 sistemas open-source nuevos: Cognee, Graphiti, Mem0, A-Mem, HippoRAG, MemPalace (clonados ad-hoc para la investigación) | 1409 |
| 16c | 18 papers académicos (2023-2026), 3 hilos HN, 8 blogs / posts de practitioners, benchmarks (LoCoMo, LongMemEval, EverMemBench) | 1158 |

Total: 36 sistemas/papers analizados. Cada uno cruzado contra Q1-Q4 del doc 16.

---

## §2 — Convergencias (doc 16 alineado con consenso)

Lo que la propuesta del doc 16 acierta porque coincide con lo que la muestra
hace, en al menos 3 fuentes independientes:

### C1 — Episodic inmutable vs entities derivadas/regenerables

**Aparece en**: Cognee (DocumentChunk preserved, KG re-extraíble), MemPalace
(drawers verbatim vs closets regenerables), Graphiti (episodes inmutables,
edges regenerables), AriGraph (episodic vs semantic separados), Zep
(episodes vs entity nodes), GAAMA (episode/fact/reflection en capas),
memory-wiki (sources/ inmutables, syntheses/ derivadas).

7 fuentes convergen. Doc 16 §2 lo propone igual. **Mantener.**

### C2 — Slug del filename como identidad canónica

**Aparece en**: memory-wiki (`entities/marcelo.md` con `id: entity.marcelo`
redundante), skill curator (filename slug + frontmatter `name`), OpenClaude,
Cognee (`uuid5(normalized_name)`), MemPalace (`name.lower().replace(...)`
directo como ID), GAAMA concepts.

6 fuentes convergen sobre la misma idea: lowercase + snake_case + filename
como identidad. Doc 16 lo asume implícitamente. **Mantener y explicitarlo
en el schema.**

### C3 — Aliases array en frontmatter

**Aparece en**: memory-wiki (`aliases: [...]` YAML), Holographic (aliases
TEXT CSV), MemPalace (EntityRegistry con aliases dict), Cognee
(opcional via ontology resolver).

4 fuentes. Doc 16 no lo menciona explícitamente. **Incorporar** —
necesario para la unificación que doc 16 §3 asume sin especificar.

### C4 — Productor canónico = LLM (no regex, no clasificador determinista)

**Aparece en**: Hermes background_review (forked LLM), OpenClaude
extractMemories (forked LLM), Mem0 server LLM, Honcho dialectic, OpenViking
server LLM, memory-wiki `wiki_apply` desde productor externo, Cognee
KnowledgeGraph extraction, Graphiti EdgeOperations LLM, AriGraph
extraction LLM, Generative Agents reflections LLM.

10+ fuentes convergen. Los sistemas con regex (Holographic
`_extract_entities`, OpenClaw `detectCategory`) se notan explícitamente como
heurísticas frágiles. Doc 16 ya asume que el dream usa LLM. **Mantener.**

### C5 — Append, no override

**Aparece en**: skill curator patch (`_patch_skill` agrega evidence), memory-
wiki claims array (sin reemplazar), Graphiti edges con `valid_at` (NUNCA
borra), MemPalace KG append-only, A-Mem (anti-patrón: hace override,
documentado como pérdida de info), Mem0 (deprecando override).

6 fuentes a favor + 1 contra-ejemplo documentado como anti-patrón. Doc 16
§2 lo asume. **Mantener — pero ver gap G2 sobre estructura concreta.**

### C6 — Evidence con link al source

**Aparece en**: memory-wiki (`WikiClaimEvidence` con `sourceId, path,
lines`), Holographic (fact_entities table linking facts to entities),
Graphiti (edges referencian episode_id), GraphRAG (entity summary con
provenance), GAAMA (facts referencian episodes).

5 fuentes. Doc 16 §5 lo menciona. **Mantener y aterrizar en schema** (cada
claim en `entities/<type>/<value>.md` debe llevar `source: episodic/<id>`).

### C7 — Schema custom Pydantic / vocabulario semi-cerrado

**Aparece en**: Graphiti (`entity_types: dict[str, BaseModel]` opt-in),
Cognee (KnowledgeGraph schema abierto + tipos hardcoded en código),
MemPalace (4 tipos hardcoded + `unknown`), Mem0 (categories abiertas pero
acotadas ~15), Octoco blog (recomendación: schema libre + cardinalidad
baja).

5 fuentes. Patrón: **vocabulario abierto a nivel DB/schema, semi-cerrado
a nivel de aplicación/prompt**. Doc 16 está en este territorio. **Mantener
pero ver D1 sobre cardinalidad inicial.**

### C8 — Reflections / consolidación periódica

**Aparece en**: Generative Agents (reflexión por session), A-Mem evolution,
GAAMA reflections, Mem0 (consolidación batch), skill curator (background
cross-session), memory-wiki (compile determinista cross-source), Lanham blog
("distilled playbooks").

7 fuentes. Doc 16 §4 lo asume vía el dream. **Mantener — pero ver gap G6
sobre costo medido.**

---

## §3 — Divergencias intencionales (doc 16 se aparta con justificación)

Donde doc 16 toma una decisión distinta a la mayoría de la muestra. En cada
una analizo si la divergencia se sostiene o no.

### D1 — Markdown files como storage principal (vs DB graph)

**Mayoría hace**: Cognee → NetworkX/Kuzu/Neo4j. Graphiti → Neo4j/FalkorDB.
Mem0 → graph DB + vector store. A-Mem → ChromaDB. HippoRAG → NetworkX.

**Doc 16 propone**: markdown files (`memory/entities/<type>/<value>.md`).

**Precedentes que apoyan**: memory-wiki (markdown + frontmatter), skill
curator (markdown files), OpenClaude (markdown), Hermes Holographic
(markdown + SQLite sidecar para índice).

**Justificación**: greppable, version-control friendly, single-user, sin
server-side, observable, editable a mano. El doc 16 §2 lo justifica.

**Veredicto**: divergencia bien sostenida. **Mantener.** Coherente con la
postura general de durin (single-user CLI, sin servidor).

### D2 — Dream entity-centric (no session-centric)

**Mayoría hace**: Generative Agents reflexiona por session. A-Mem evoluciona
por write. Cognee cognify es por documento.

**Doc 16 propone**: dream global cross-session, cursor por entidad.

**Precedentes que apoyan**: memory-wiki (compile determinista cross-source,
NO por sesión), skill curator (background cross-session), Mem0 production
(batch).

**Justificación**: la unidad útil para coherencia operativa es la entidad
(person:marcelo, project:durin), no la session. Una entidad se referencia
en muchas sessions; consolidar por entidad evita reprocessar contenido
ya consolidado.

**Veredicto**: divergencia bien sostenida. **Mantener.** Pero ver G6
(costo).

### D3 — Tipos prescriptivos (10) vs schema abierto que emerge

**Mayoría hace**: vocabulario abierto en schema, semi-cerrado en app.
MemPalace tiene 4 hardcoded. Mem0 ~15 emergentes. Memory-wiki tiene 5
kinds genéricos + entityType libre.

**Doc 16 propone**: 10 tipos divididos consolidables (6) / referenciables
(4).

**Precedentes que apoyan**: **ninguno con 10**. Memory-wiki es el más
cercano y solo tiene 5 kinds. Doc 16 va delante del estado del arte
open-source.

**Justificación de doc 16**: derivada del dominio (lo que aparece en
bitácora), no del estado del arte. Apunta a expresividad inicial alta.

**Riesgos** (16c §8, 16a §8): GAAMA documenta mega-hub; practitioners
(Octoco, Mem0) recomiendan cardinalidad baja inicial. Sin precedente, sin
benchmark, sin caso documentado a esa cardinalidad.

**Veredicto**: divergencia **débilmente sostenida**. Tres lecturas:
- (a) durin tiene contexto suficiente del dominio para predecir los 10 sin
  testear. Riesgo: prescriptivismo.
- (b) Empezar con 4-6 (`person, project, topic, tool` + `incident` o
  `place` opcional) y dejar emerger el resto. Más alineado con consenso.
- (c) Schema abierto sin lista canónica inicial; cada dream propone tipos
  nuevos. Más radical, menos predecible.

**Recomendación**: (b). Es más fácil agregar tipo que migrar páginas.
Ver §6 Q1 abajo.

### D4 — Entity-centric en general (vs HyperMem que NO usa entity nodes)

**Mayoría académica**: usa entity nodes (Zep, Mem0g, GraphRAG, AriGraph,
GAAMA).

**Excepción importante**: **HyperMem** (SOTA LoCoMo 92.73%, ACL 2026) NO
usa entity nodes — usa hipergrafo topics/episodes/facts.

**Doc 16 propone**: entity-centric.

**Justificación necesaria** (no está escrita en doc 16): si la defensa de
entity-centric es "mejor accuracy en QA conversacional", **HyperMem la
refuta**. La defensa que SÍ se sostiene es: **"coherencia operativa
cross-sesión sobre los mismos proyectos/personas/tools"** — un caso que
LoCoMo no testea, pero que es el caso real de durin.

**Veredicto**: divergencia sostenible **si se escribe el outcome operativo
esperado**. Sin esa escritura explícita, la propuesta queda expuesta a la
crítica "HyperMem sin entity nodes gana en LoCoMo, ¿para qué entity-centric?".

**Acción**: doc 17 §4 G1 — escribir la defensa operativa explícita antes
de implementar.

---

## §4 — Gaps reales (doc 16 no cubre algo que ellos sí resuelven)

Esta es la parte más importante. Cada uno es algo a incorporar **antes** de
empezar a implementar, porque si no se piensa ahora se va a manifestar como
deuda.

### G1 — Outcome operativo explícito (defensa contra HyperMem)

**Qué cubren ellos**: HyperMem demuestra que entity nodes NO son
condición necesaria para SOTA en QA conversacional. Mem0 production report
demuestra que el mejor memory system gana solo 6 puntos sobre full-context
a 14x menos costo — el ROI es modesto.

**Qué falta en doc 16**: la pregunta "¿por qué entity-centric vale la pena
para durin?" no está respondida. Si el outcome es "mejor QA", entity-
centric no se justifica. Si es "coherencia cross-sesión sobre identidad de
proyecto/persona/tool", sí, pero hay que decirlo.

**Acción concreta**: agregar al doc 16 (o doc 18) una sección
"Outcomes operativos esperados" con 3-5 ejemplos verificables:
- "Después de 10 sesiones tocando `project:durin`, una pregunta tipo
  '¿qué decisiones tomamos sobre embeddings?' debe encontrarse en
  `entities/project/durin.md` sin grep sobre todo `episodic/`."
- "Si user dice 'soy Marcelo' en sesión 1 y 'mmarmol@mxhero.com' en
  sesión 5, ambas deben caer en `entities/person/marcelo.md` sin
  intervención manual."
- "Si 3 sesiones distintas mencionan un bug recurrente en TUI, debe
  aparecer una página `entities/incident/tui-bug-X.md` consolidada."

Sin estos outcomes, no hay forma de validar la decisión.

### G2 — Bi-temporal validity (`valid_from` / `valid_to` / `invalid_at`)

**Qué cubren ellos**: Graphiti (`valid_at`, `invalid_at`, `expired_at` en
edges, NUNCA borra). MemPalace (`valid_from`/`valid_to`, `invalidate()`
explícito). Zep paper (mismo modelo). Mem0 blog 2026 (lo recomienda).
Survey GraphMemory 2026 (lo canoniza).

**Qué falta en doc 16**: menciona "evolución temporal" pero no especifica
el modelo. Sin esquema concreto, el primer conflicto que aparezca va a
resolverse con override (anti-patrón A-Mem) o con prompt-guidance
(anti-patrón OpenClaude).

**Acción concreta**: incorporar al schema de claim dentro de la página:
```yaml
claims:
  - text: "prefiere pytest sobre unittest"
    status: superseded  # supported|contested|contradicted|refuted|superseded
    valid_from: 2026-03-01
    valid_to: 2026-05-15  # null cuando current
    superseded_by: <claim_id>  # opcional, puntero al claim que reemplaza
    evidence:
      - source: episodic/abc123.md
        text: "Marcelo dijo 'unittest tiene mejor stack trace ahora'"
```

Página muestra solo `valid_to is null` (current state); storage preserva
history. Esto es estándar canonizado.

### G3 — Pipeline dedup en cascada (Graphiti, MemPalace, EDC)

**Qué cubren ellos**: el patrón canonizado en 3+ sistemas:
1. Exact-norm match (lowercase + collapse) contra slug + aliases.
2. Embedding similarity con threshold τ (Graphiti τ≥0.9 con
   MinHash/Jaccard como prefiltro).
3. LLM resolver solo cuando cae en zona gris (threshold dual: definitivo
   dedup arriba de τ_high, definitivo nuevo abajo de τ_low, LLM entre los
   dos).

**Qué falta en doc 16**: doc 16 §3.2 dice "el dream decide", sin
especificar el pipeline. Sin esto, cada llamada al dream va a pagar LLM
para todos los matches, o va a hacer regex frágil.

**Acción concreta**: documentar el pipeline en doc 16 §3.2. Para durin
single-user con corpus pequeño, posiblemente alcanza:
- (1) exact-norm + aliases (cubre 80%).
- (2) skip embedding similarity al principio (overkill para corpus < 1000).
- (3) LLM resolver invocado solo cuando el dream detecta nombre nuevo cuya
  semántica está en zona gris (decisión del dream, no del runtime).

### G4 — Mega-hub problem (GAAMA)

**Qué cubren ellos**: GAAMA documenta explícitamente que `person:user`
con miles de edges se vuelve hub ruidoso. Octoco recomienda "no centralizar
todo en una entidad".

**Qué falta en doc 16**: para durin, `person:marcelo` y `project:durin`
son hubs garantizados — todo va a referenciarlos. En 3-12 meses de uso,
estas dos páginas tendrán cientos/miles de claims. Sin mitigación,
dejan de ser pivote útil (la página crece y el modelo no puede leerla
completa).

**Acción concreta**: tres mitigaciones combinables:
- **Sub-paging por scope**: cuando `person:marcelo` cruce N claims (¿200?),
  sub-dividir en `person/marcelo/preferences.md`,
  `person/marcelo/projects.md`, `person/marcelo/identity.md`. La página
  raíz queda como índice.
- **Concept layer** (estilo GAAMA): introducir capa intermedia
  `concept:<topic>` que agrupa claims sobre un tema, y la persona/proyecto
  apuntan a conceptos en vez de tener todo inline.
- **Compresión periódica en el dream**: cuando el dream procesa una entity
  con > N claims, comprime los más viejos en un summary y archiva los
  individuales (no los borra, los mueve a sub-sección de history).

**Recomendación inicial**: empezar con sub-paging por scope cuando se
cruce el threshold. Posponer concept layer hasta que se vea necesario.

### G5 — Claim-status enum (memory-wiki)

**Qué cubren ellos**: memory-wiki tiene 5 valores hardcoded:
`supported|contested|contradicted|refuted|superseded`. Cada claim lleva
status. Reports/contradictions.md es dashboard derivado.

**Qué falta en doc 16**: doc 16 §3.3 (conflictos) menciona "marcar evolución
de hechos" sin esquema concreto. Sin status enum, cada conflicto se
resuelve ad-hoc.

**Acción concreta**: adoptar el enum 5-valores de memory-wiki para los
claims dentro de las páginas de entidad. Es el único precedente
estructurado con que pude cruzar.

Default: `supported`. El dream cambia a `contested` o `contradicted`
cuando detecta evidence opuesta. `superseded` cuando hay claim sucesor
explícito.

### G6 — `absorbed_into` pointer (skill curator)

**Qué cubren ellos**: skill curator tiene `absorbed_into: <skill_id>` en
frontmatter cuando una skill se fusiona con otra. Permite auditoría tras
merge.

**Qué falta en doc 16**: doc 16 no contempla el caso "estoy fusionando
`entities/person/marcelo-m.md` en `entities/person/marcelo.md`". Sin
pointer, después del merge no hay forma de recuperar por qué se fusionó.

**Acción concreta**: cuando el dream fusiona dos entidades, escribir en el
frontmatter del archivo absorbido:
```yaml
absorbed_into: entities/person/marcelo.md
absorbed_at: 2026-05-23
reason: "duplicate detected — same canonical identity via aliases [Marcelo M]"
```

El archivo absorbido NO se borra — queda con un stub que apunta al
canonical. Telemetry sidecar puede contar absorptions.

### G7 — Telemetry sidecar separado del frontmatter

**Qué cubren ellos**: skill curator (`~/.hermes/skills/.usage.json`),
memory-wiki (`.openclaw-wiki/cache/agent-digest.json`). Counters
(`reference_count`, `last_accessed_at`) viven en sidecar, no en frontmatter
user-facing.

**Qué falta en doc 16**: si las páginas de entidad llevan
`last_referenced_at`, `reference_count`, etc., el frontmatter se llena de
metadata operacional que el user no necesita y que cambia constantemente
(diff noise en git).

**Acción concreta**: separar:
- Frontmatter user-facing (`entities/person/marcelo.md`): `name`, `aliases`,
  `claims`, `tags`, `pinned`.
- Sidecar (`memory/.usage.json` o per-entity `memory/entities/.usage/`):
  `last_referenced_at`, `reference_count`, `last_dream_processed_at`,
  `claim_count`.

Esto también permite `.gitignore` del sidecar si el user quiere versionar
solo lo significativo.

### G8 — `pinned` opt-out de auto-transitions

**Qué cubren ellos**: skill curator (`pinned: true` skip stale/archive).
OpenClaude (implícito: user-type memories son intocables). Memory-wiki
(`personCard` sub-schema implica curation manual).

**Qué falta en doc 16**: doc 16 lifecycle no contempla bypass. Si el user
edita `entities/person/marcelo.md` manualmente, el dream debe respetar.

**Acción concreta**: dos mecanismos combinables:
- `pinned: true` en frontmatter → skip stale/archive thresholds.
- `_MEMORY_AUTHOR=user_authored` (ya existe en durin desde propuesta C) →
  el dream NO sobreescribe claims marcados así.

### G9 — Lifecycle: combinar 3 niveles (warning + stale-report + archive)

**Qué cubren ellos**: 3 sistemas, cada uno con UNO de los tres niveles:
- skill curator: archive físico (`STATE_STALE` → `STATE_ARCHIVED` → mueve
  a `.archive/`).
- memory-wiki: freshness levels (fresh/aging/stale) **sin archive físico** —
  solo marca y aparece en reports.
- OpenClaude: warning text al leer una memoria antigua.

**Qué falta en doc 16**: no especifica nivel ni threshold. Si solo archive,
se pierde info. Si solo warning, las páginas viejas siguen consumiendo
tokens. Si solo report, el modelo no se entera.

**Acción concreta**: combinar los tres en niveles ascendentes:
- **aging** (30d sin referencia): el reader agrega warning text inline:
  "(página last_updated 35d ago)".
- **stale** (90d): marca en `entities/.reports/stale.md` como dashboard;
  warning sigue.
- **archive** (180d): mover a `entities/.archive/<type>/<value>.md`;
  el reader ya no la lee por defecto; `restore` operation disponible.

Thresholds configurables. `pinned: true` o `user_authored` skip todos.

### G10 — Cross-system identity (todos lo tienen abierto)

**Qué cubren ellos**: **nadie**. Es agujero universal documentado.

**Qué falta en doc 16**: lo asume implícitamente ("`person:marcelo` cubre
todas las representaciones").

**Acción concreta**: aceptar el limitado inicial. Documentar que durin NO
va a unificar automáticamente:
- `git author "Marcelo Marmol"` (commit history)
- `email mmarmol@mxhero.com`
- `conversación: "soy Marcelo"`

Vivimos con `aliases: [Marcelo, Marcelo Marmol, mmarmol@mxhero.com]`
**declarados manualmente** (al onboarding o vía edición de la página).
Solo cuando hay corpus suficiente y el caso de uso lo demande, evaluar
cross-system unification (probablemente requiera dream LLM con contexto
de múltiples fuentes).

---

## §5 — Riesgos no mencionados en doc 16

Resumen de los riesgos que la literatura identifica y que conviene escribir
explícitamente antes de implementar:

| # | Riesgo | Fuente | Mitigación propuesta |
|---|---|---|---|
| R1 | Defensa contra HyperMem no explícita | 16c §4, §8 | Escribir outcomes operativos verificables (ver G1) |
| R2 | Mega-hub en `person:marcelo` / `project:durin` | 16c §5, GAAMA paper | Sub-paging por scope a partir de threshold de claims |
| R3 | Costo del dream no medido | 16a §5.6, 16c §6 | Medir antes de Phase 4. Estimar con Haiku como baseline |
| R4 | `place` sin precedente fuerte | 16b §5, 16c §7 | Empezar SIN `place`, agregar si la bitácora lo justifica |
| R5 | Cross-system identity sin solución | 16c §8 | Aceptar limitado inicial, aliases manuales |
| R6 | Tipos prescriptivos (10) vs muestra (max 5) | 16a §8, 16c §4 | Reducir set inicial a 4-6 |
| R7 | LLM-driven entity resolution puede equivocarse | 16c §4 (Fountain City) | Pipeline en cascada (G3): regla determinista primero, LLM solo en zona gris |

---

## §6 — Recomendaciones revisadas para Q1-Q4

Tras los 3 outputs, las recomendaciones del doc 16 se ajustan así.

### Q1 — Granularidad

**Posición revisada**:

- Reducir set inicial a **5 tipos consolidables**:
  - `person` (precedente fuerte: memory-wiki personCard, MemPalace)
  - `project` (razonable, sin precedente directo pero alineado con dominio)
  - `topic` (precedente: OpenClaw concepts, memory-wiki concepts/)
  - `tool` (precedente fuerte: skill curator)
  - `incident` (sin precedente pero defendible por dominio — bug/incidente
    recurrente con causa+fix+lección)
- **Quitar** `place` (R4), `decision`, `event` del set inicial. Se pueden
  tratar como tags dentro de claims, no como entidades.
- **Quitar** `file`, `symbol` como tipos. Ya viven en `source_refs` o
  similar, no necesitan página propia (16a §8).
- Vocabulario abierto en schema (no enum), semi-cerrado en código:
  función que dado un tipo emergente decide "esto sí va a `entities/`"
  o "esto va a tags planos".
- Plan de mitigación mega-hub: sub-paging por scope cuando una entidad
  cruce N claims (ver G4).

### Q2 — Identidad y unificación

**Posición revisada**:

- **Slug filename canónico**: `entities/<type>/<slug>.md` donde
  `slug = lowercase(name).replace(' ', '_').replace("'", "")`.
- **Frontmatter mínimo**:
  ```yaml
  type: person
  name: Marcelo Marmol
  aliases: [Marcelo, mmarmol@mxhero.com]
  pinned: false
  ```
- **Pipeline dedup en cascada** (ver G3):
  1. Exact-norm contra slug + aliases.
  2. (Diferir) Embedding similarity con threshold dual.
  3. LLM resolver solo en zona gris, invocado por el dream.
- **Onboarding manual**: el wizard puede pedir aliases iniciales para
  `person:<user>` (sobreviviente útil de propuesta C — único caso donde
  declaración manual paga).
- **Cross-system identity**: aceptar limitado. Aliases manuales (G10).

### Q3 — Conflictos y evolución

**Posición revisada**:

- **Bi-temporal validity** en claims (G2):
  ```yaml
  claims:
    - text: "..."
      status: supported  # 5-enum (G5)
      valid_from: <date>
      valid_to: <date | null>
      superseded_by: <claim_id | null>
      evidence: [{source: episodic/<id>.md, ...}]
      author: agent_created | user_authored
  ```
- **Append-only**: el dream agrega claims, cambia status, marca
  `valid_to` — NO borra.
- **`absorbed_into`** pointer cuando se fusionan páginas (G6).
- **`user_authored` claims son inmutables** (G8).

### Q4 — Lifecycle

**Posición revisada**:

- **3 niveles deterministas** combinados (G9):
  - aging 30d → warning text inline
  - stale 90d → entry en `entities/.reports/stale.md`
  - archive 180d → mueve a `entities/.archive/<type>/<slug>.md`
- **`pinned: true`** o **`_MEMORY_AUTHOR=user_authored`** → skip todos
  los thresholds (G8).
- **Reactivación on access**: cualquier read o write resetea
  `last_referenced_at` (skill curator pattern).
- **Telemetry sidecar**: counters en `memory/.usage.json` o
  `memory/entities/.usage/<type>/<slug>.json`, no en frontmatter (G7).
- **Lifecycle por tipo**:
  - `person`, `project`: nunca archivan automáticamente.
  - `topic`, `tool`, `incident`: archivables según thresholds.
- **`restore` operation** disponible para recuperar de archive (skill
  curator pattern).

---

## §7 — Decisiones pendientes para el usuario

Tres elecciones no triviales antes de empezar a implementar. Cada una
abre/cierra rama distinta:

### D1 — Cardinalidad inicial de tipos consolidables

- **(a)** 5 tipos: `person, project, topic, tool, incident` (recomendado;
  alineado con muestra; agregar tipos cuesta poco)
- **(b)** 10 tipos como doc 16 propone (más expresivo upfront; sin
  precedente; riesgo prescriptivismo)
- **(c)** Vocabulario completamente abierto sin lista canónica inicial; el
  dream propone tipos nuevos (más radical, menos predecible)

### D2 — Profundidad del modelo temporal

- **(a)** Solo `valid_from` + `valid_to` (modelo MemPalace; suficiente para
  fase 1)
- **(b)** Bi-temporal completo: `valid_at`, `invalid_at`, `expired_at` (modelo
  Graphiti; distingue world-time vs DB-time; útil si después se quiere
  query "qué creíamos sobre Marcelo el 2026-03-01")
- **(c)** Solo `updatedAt` + `status` enum, sin temporal (modelo
  memory-wiki; más simple, menos auditable)

### D3 — Archive físico o no

- **(a)** Sí — combinar 3 niveles (warning + stale-report + archive físico),
  como propone G9
- **(b)** No — solo warning + stale-report; nunca mover a `.archive/`. Más
  simple, menos riesgo de "perdí la página de X"
- **(c)** Solo archive (sin warning ni report), como skill curator. Más
  agresivo, menos amigable

---

## §8 — Resumen ejecutivo

1. **Convergencias (C1-C8)**: lo que doc 16 propone está bien sustentado
   en 5-10 fuentes por ítem. La dirección general es correcta.

2. **Divergencias (D1-D4)**: tres están bien sostenidas (markdown, dream
   entity-centric, postura general). Una (10 tipos) está débilmente
   sostenida — recomendación es reducir a 5.

3. **Gaps reales (G1-G10)**: 10 ítems concretos que doc 16 no cubre y
   que la muestra resuelve. Los **más importantes** son:
   - **G1** (outcome operativo explícito) — sin esto, la propuesta no
     se defiende contra HyperMem.
   - **G2** (bi-temporal validity) — pattern canonizado, sin él los
     conflictos se resuelven con override.
   - **G3** (pipeline dedup en cascada) — sin esto, cada match paga LLM
     o se vuelve regex frágil.
   - **G4** (mega-hub) — garantizado en 3-12 meses si no se mitiga.
   - **G5** (claim-status enum), **G6** (absorbed_into), **G7**
     (telemetry sidecar), **G8** (pinned), **G9** (lifecycle 3 niveles)
     son refinamientos importantes pero menos críticos que G1-G4.
   - **G10** (cross-identity) es agujero universal: se acepta inicial.

4. **Riesgos (R1-R7)**: escribir explícitamente en doc 16 o doc nuevo
   antes de implementar.

5. **Decisiones pendientes** (§7): tres elecciones del user que abren
   rama distinta cada una.

El doc 16 sigue siendo válido como dirección. **No requiere reescribirse**;
requiere **complementarse con G1-G10** y resolver §7 antes de la primera
PR de implementación.

---

## Last updated: 2026-05-23 (post-síntesis 3 agentes)
