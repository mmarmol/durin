# Rediseño del modelo de memoria — nota de diseño (en discusión)

> **Estado**: working design, 2026-06-05. Documento vivo — fuente de verdad
> de las decisiones de la sesión de diseño Marcelo↔agente. **No** describe lo
> shipped (eso está en `docs/architecture/memory/`); cuando esto se acuerde,
> esos docs se reescriben o archivan.
>
> Convención: cada decisión va con **DECISIÓN** (qué, determinista) +
> **JUSTIFICACIÓN** (por qué). Si algo está abierto, dice ABIERTO.
> Reto explícito del usuario: critícame, no asumas que algo está bien por
> estar escrito.

**Secuencias al fino (un doc por punto, con su diagrama):**
- [Ingesta (escritura)](memory_seq_ingesta.md)
- [Consulta (lectura)](memory_seq_consulta.md)
- [Contexto pre-cargado en sesión](memory_context_preload.md)

---

## 0. El objetivo (la vara con la que se juzga todo)

> Un agente LLM que resuelve tareas generales necesita: **(a)** recordar
> lo que importa de su mundo y de su usuario, **(b)** recuperar el
> conocimiento correcto al decidir/actuar, **(c)** no ahogarse en ruido —
> con el usuario pudiendo inyectar hechos/documentos y el agente
> absorbiendo material, todo manteniéndose coherente con el tiempo.

**Afinado (Marcelo):** dos cosas son **ciudadanos de primera** — la
**iteración** usuario↔agente (qué pasó, qué acciones se tomaron) y la **base
de conocimiento** del mundo. Se **acoplan**: el conocimiento se consulta al
momento de decidir la próxima iteración/acción, y la iteración hace crecer
el conocimiento. Ambos dominios (recall conversacional Y trabajo de
conocimiento) deben quedar bien servidos.

---

## 1. Diagnóstico (verificado en código + workspace real)

No hay "una grieta": son **tres tracks medio-construidos que no se hablan**,
y el único que funciona es el que pensábamos jubilar.

```
                 INPUT                       PROCESA (Dream)        OUTPUT             ¿Funcionó (vault real)?
TRACK A  history.jsonl (auto/turno)      →  dream legacy (2 fases) → MEMORY/SOUL/USER → SÍ
TRACK B  episodic (el agente elige)      →  dream entity          → entities/<t>/<s>  → NO (0 episodic, 0 pages)
TRACK C  ingest/store(corpus)            →  (nada)                → corpus chunks     → crudo (203 chunks, 1 doc)
```

Hallazgos clave (todos verificados):

1. **Track A funciona** porque su input (`history.jsonl`) se llena solo en
   cada turno. Mantiene SOUL/USER/MEMORY vía un AgentRunner Fase-2 con
   `edit_file` (y crea skills). Es el legacy (`agent/memory.py::Dream`,
   cron `dream` ~2h).
2. **Track B (entidades) nunca se activó**: 0 entity pages, 0 episodic en el
   vault real. Las 14 entidades del grafo son **phantom** (tags sobre
   entradas `stable`). La maquinaria entity-centric jamás produjo un byte.
   (cron `memory_dream`, diario 03:00.)
3. **Track C (referencia) está crudo**: 1 documento ingerido → 203 chunks
   `corpus` nunca consolidados; el doc coherente queda enterrado en
   `ingested/<id>/source.md`.
4. **El eje `episodic/stable` es el error de base.** Pide al agente
   clasificar por *durabilidad*, y esa adivinanza decide si el conocimiento
   se estructura o no. La durabilidad **no es** ninguna distinción real del
   objetivo. (Metáfora: "fruta en una carnicería" — taxonomía de memoria
   conversacional/Tulving/LoCoMo aplicada a trabajo de conocimiento.)
5. **El agente sólo puede taggear, nunca autorar entidades.** No hay tool de
   autoría; la página estructurada es 100% trabajo del dream → agente y
   entidades **desacoplados** → etiquetas huecas (phantom).
6. **Fragmentación**: el conocimiento de mxHERO vive partido en los tres
   tracks (resumen en MEMORY.md, hechos en stable, chunks en corpus), sin
   que ninguno referencie al otro ni esté completo.

---

## 2. Decisiones

### 2.1 Dos capas de primera + acoplamiento bidireccional

**DECISIÓN.** El modelo tiene dos capas de primera, más un carril aparte:

```
EXPERIENCIA (qué pasó / qué hicimos)        CONOCIMIENTO (qué se sabe del mundo)
- sesiones (crudo) + sus resúmenes           - entidades + relaciones + documentos de referencia
- indexada en el tiempo                       - durable, consultable
        │  ── extrae hechos ───────────────────────────►  (la experiencia hace crecer el conocimiento)
        ◄── consulta para decidir ────────────────────│   (el conocimiento informa la acción)

Carril aparte (sí-mismo del agente): SOUL (constitución) + skills (procedimientos)
```

**JUSTIFICACIÓN.** Es la articulación del objetivo (§0). Mantener ambas de
primera evita el error del legacy (mezclar conocimiento del mundo dentro de
la working-memory). El carril sí-mismo no es ni experiencia ni mundo.

### 2.2 Ruteo por intención (mata el eje durabilidad)

**DECISIÓN.** El destino lo decide el agente por **intención**, no
adivinando clase:

| Intención | Destino | Tool |
|---|---|---|
| "sé un hecho sobre una cosa" | autora/actualiza la **entidad** | `memory_upsert_entity` |
| "tengo un documento" | **ingiere** (referencia) | `memory_ingest` |
| "pasó algo en la interacción" | queda en el **registro de sesión** (no tool dedicado) | — |

**JUSTIFICACIÓN.** Esto **es** la distinción cosa/documento/experiencia (la
real, §0), y **elimina el eje `episodic/stable`** (el red herring de
durabilidad). Resuelve "B dormido" por construcción: un agente de research
habría autorado `company:mxhero` directo. Nota: el tercer verbo
(observación) **desaparece** como tool — ver §2.6.

### 2.3 Inventario de clases (0 nuevas — se simplifica)

**DECISIÓN.** **0 clases nuevas · remociones · el resto reuse con cambio de
lógica/rol.** El error nunca fue "faltan clases": el agente las usaba por el
eje equivocado y dream miraba sólo una.

| Clase hoy | En el modelo nuevo | Cambio |
|---|---|---|
| `entity` | entidades (grafo) | REUSE — lógica: agente autora, dream refina (§2.4) |
| `ingested` | doc coherente = reference page | REUSE — index + marcar REFERENCE (§2.8) |
| `corpus` | índice de recuperación → page | REUSE — cambia rol (de "la representación" a "el índice") |
| `episodic` | — | **SE DISUELVE** (§2.6: la observación se pliega en sessions) |
| `stable` | — | **SE DISUELVE** (→ entidad o registro de sesión) |
| `session` | experiencia: crudo = verdad = input de dream | REUSE |
| `session_summary` | experiencia: vista de recall para hot-path | REUSE |
| `history.jsonl` | — | **SE ELIMINA** (era el feed aplanado del dream legacy; el dream nuevo lee sessions directo; recall = search SESSION) |
| `pending` | buffer de intake | REUSE |
| `archive` | terminal | REUSE |
| `SOUL.md` | constitución del agente | REUSE — pasa a `user_authored`, fuera de dream (§2.9) |
| `skills/` | procedimientos | REUSE — dream corto crea, dream largo refina (§2.7) |
| `MEMORY.md` / `USER.md` | — | **SE DISUELVEN** (→ inyección hot_layer, §2.9) |

Micro-decisiones (ninguna es clase nueva):
- **Reference**: marcador REFERENCE + indexar el `ingested/source.md` que ya
  existe — **sin carpeta nueva** (minimal).
- **Observación**: se pliega en sessions+summaries (§2.6) — no hay clase de
  observación.

### 2.4 Capa de conocimiento: el agente autora, Dream es la autoridad de coherencia

**DECISIÓN — autoría.** El agente autora entidades directo
(`memory_upsert_entity`), citando fuente. **La entidad existe de inmediato**
e indexa (prosa searchable) — no más phantom-hasta-que-corra-dream.

**DECISIÓN — división de estructuración (opción b).** El agente autora
**name + aliases + relations + body (prosa)**. **Dream es el dueño único del
esquema estructurado**: extrae/normaliza `attributes` desde la prosa + las
sesiones.
**JUSTIFICACIÓN.** Si dream tiene la autoridad de coherencia, debe ser el
único estructurador — N agentes/modelos emitiendo claves distintas =
incoherencia. Costo (atributos finos demoran) lo cubre el dream **corto**
(~2h, no el diario; §2.7) + la prosa searchable da inmediatez.

**DECISIÓN — precedencia: `user > dream > agent`.** Dream **gana** sobre el
agente.
**JUSTIFICACIÓN.** Clave para coherencia entre **múltiples agentes y
modelos** escribiendo: ninguna escritura individual es canónica; dream, con
visión global y mandato de higiene, arbitra. El humano (`user_authored`) por
encima de todo (es su punto de control).

**DECISIÓN — provenance por campo como árbitro.** Cada campo lleva
`{source_ref, at, author: agent|dream|user}`. Conflicto → precedencia +
recencia dentro de un nivel. Contradicción real → el valor viejo va a history
(`valid_from/until`), nunca overwrite ciego.

**DECISIÓN — pipeline compartido.** Agente y dream escriben por el mismo
`dream_apply` (JSON Patch + validación + `.md.bak` + commit). Dos editores de
un wiki, una pluma.

**RIESGO a vigilar.** Dream-gana sube la vara: debe correr fiable/seguido y
ser correcto (autoridad única). Salvavidas: `user > dream`.

### 2.5 Concurrencia — DOS capas ortogonales (DECIDIDO)

Escritores reales de la memoria: gateway (agente + cron-dream in-process +
thread de threshold), **procesos CLI independientes**, y el **editor humano**
(Obsidian). No hay lock in-process posible entre todos → coordinación
cross-process (un host) y cross-host (futuro).

**Capa 1 — write-time: OPTIMISTIC (sin write-lock), implementado CON git.**
El CAS **lo da git**, no se inventa a mano:
- *Local (cross-process, mismo repo)*: `git update-ref refs/heads/main <new>
  <old>` es un **compare-and-swap atómico** sobre la branch. El write construye
  el commit por **plumbing** (`hash-object` + `commit-tree`, **sin tocar el
  working tree**) → `update-ref` CAS → si falla (HEAD se movió), re-lee HEAD,
  **re-aplica el patch por-campo** sobre la versión nueva, reintenta.
- *Cross-host (clones)*: `git push --force-with-lease` es el **mismo CAS** sobre
  la red.
- Conflicto mismo-campo → precedencia (`user>dream>agent`) + recencia. Fuente
  canónica → reintentar es seguro. Patches por-campo → conflictos raros (campos
  distintos = re-aplicar auto-mergea). Nunca merge **textual** de git (manglaría
  YAML); el merge es **semántico** (re-aplicar el patch sobre la base nueva).
- **Escritores de primera clase** (agente, dream, **humano vía dashboard**):
  todos pasan por `memory_writer` → field-patch → CAS por plumbing (**no tocan
  el working tree** → sin race de working tree en el camino crítico).
- **Edición raw en Obsidian = best-effort, NO garantía v1**: el working tree se
  mantiene **ff a HEAD** → Obsidian es un gran **lector** + editor seguro cuando
  nada más escribe. El watcher intenta diffear el raw-edit → field-patch
  `user_authored`; si hubo write programático concurrente, best-effort
  (precedencia user, o warn). La danza editor-en-vivo sale del camino crítico.
- **Implementación**: **dulwich** (ya es dep, ya envuelto en `GitRepo`/`GitStore`).
  `repo.refs.set_if_equals(ref, old, new)` = el CAS atómico; `object_store` +
  `Blob/Tree/Commit` = el commit por plumbing. Ops multi-entidad (merge/split) =
  **un commit multi-archivo** → atómico por construcción.
- **JUSTIF**: único modelo uniforme (gateway+CLI+dashboard+multi-host) y
  file-first (markdown sigue siendo la verdad legible/versionada; los writes
  seguros van por `memory_writer`). git **ya** tiene el primitivo atómico — no
  se construye CAS a mano.

**Capa 2 — exclusión de pasada de dream: SE MANTIENE un lock (≠ write-lock).**
Un lock por working-tree (`.dream.lock`-style, cross-process, stale-takeover)
asegura **una sola pasada de dream a la vez** (CORTO y LARGO **mutuamente
exclusivos** — LARGO es graph-wide, solaparía). NO coordina escrituras (eso es
la capa 1); evita **dos dreams** haciendo trabajo redundante/thrashing y
quemando LLM (failure mode real — los fixes de cron `is_executing`). Dream, aun
con este lock, escribe cada entidad **optimistic** (el agente/humano pueden
tocarla durante la pasada).

**No confundir**: capa 1 = no corromper escrituras (optimistic/git-CAS); capa 2
= no correr dos dreams (lock de pasada). Ortogonales.

### 2.6 Capa de experiencia: sessions + summaries; Dream lee crudo

**DECISIÓN — la experiencia se colapsa en `sessions/` + `session_summary/`.**
No hay clase "observación" (`memory_store`/`episodic`/`stable` desaparecen).
Los hechos sobre cosas → entidad (agente); la **interacción se registra
sola** en la sesión.
**JUSTIFICACIÓN.** Con el agente autorando entidades, lo único que le
quedaría a la observación es "interacción que no es hecho-de-entidad" — y eso
ya queda en el transcript. Dos verbos de escritura (autora-entidad,
ingiere-doc) + la sesión que se graba sola. Recall conversacional = search
sobre `sessions/`+`summaries` (marcador SESSION).

**DECISIÓN — Dream lee las SESIONES CRUDAS (turnos), no los summaries.** Los
summaries son del **hot-path** (inyección barata de recall), no input de
dream.
**JUSTIFICACIÓN (4 problemas de extraer del summary).** (1) El summary
comprime para *continuar la conversación*, no para *extraer hechos* → pérdida
silenciosa. (2) Doble compresión (summary lossy + extract lossy). (3) Gap de
timing: sesión corta que no compacta → sin summary → dream nunca la ve. (4)
Provenance degradada: el `source_ref` apuntaría al summary, no al anchor de
turno estable (`session:<id>/turn-N`, contrato doc 01 §3.1). Dream es
cold-path con visibilidad total → puede leer crudo; recupera los turnos
relevantes de la entidad que refina, no todos siempre.

**DECISIÓN — cursores.** Hoy hay dos (verificado): `.dream_cursor` entero
sobre `history.jsonl` (valor actual `6`) + `dream_processed_through` por
entidad sobre episodic. En el modelo nuevo: **un cursor de extracción
por-sesión** (forward: "extraje hasta turno N de esta sesión").
**JUSTIFICACIÓN — por-sesión, no global.** Concurrencia: un puntero global
podría saltarse una sesión activa con turnos viejos cuando otra más reciente
avanza el puntero. El **refinamiento** (§2.7 largo) NO usa cursor — opera
sobre el grafo completo.
**ARISTA.** Cursor-forward = dream no re-extrae turnos viejos; si la
extracción se perdió un hecho, el cursor solo no lo reintenta. Mitigan: el
refine graph-wide, futuras re-menciones, la provenance, y un reset manual
"re-procesar desde turno X" (dejar explícito).

### 2.7 Los DOS dreams — split por cadencia (no por "working-memory vs entity")

**DECISIÓN.** Hay **dos** dreams, separados por **cadencia/trabajo**,
re-significando los dos crons existentes. El eje cadencia es **ortogonal al
tipo de contenido**: ambos (entidades Y skills) reciben extract + refine.

| Material | **CORTO** (integrar lo reciente, ~2h, cursor por-sesión) | **LARGO** (consolidar/limpiar el todo, ~diario, graph-wide) |
|---|---|---|
| **Entidades** | extraer hechos + `attributes` + **summary de embedding** de sesiones nuevas; aplicar prefs de usuario a `person:` | dedup/absorb, unificar claves sinónimas, splitear, resolver contradicciones cross-grafo |
| **Skills** | crear/arreglar skills desde la ejecución reciente | unificar duplicadas, mejorar eficiencia, refactor |
| **Índices** | — | self-heal / orphans |

Re-uso de crons: `dream` (~2h) → **CORTO**; `memory_dream` (diario) → **LARGO**.

**JUSTIFICACIÓN.** El diseño original ya diferenciaba corto=reciente /
largo=consolidar; el error era cargar al diario con TODO. El corto mantiene
la memoria viva turno a turno (barato, incremental, cursor-forward); el largo
hace la higiene profunda (caro, sobre todo el grafo, sin cursor).
Verificado: el corto (legacy) **ya creaba skills** (`skill_write` en
`dream_phase2.md`); el largo (entity) no las tocaba.

**Tres velocidades** (resuelve la inmediatez de la decisión b): síncrono
(agente autora) → corto (~2h: attributes + skills + extract) → largo (diario:
consolidación).

**Disparo (decidido).** **CORTO** = reactivo (*session-close* + *post-compaction*,
triggers que ya existen) + safety-net cron ~2h + **gate** barato (skip si no
hay turnos nuevos desde el cursor por-sesión). **LARGO** = diario baseline +
disparo por **refine-pressure** (muchas entidades nuevas o alias-overlap
detectado → refine antes). Ambos comparten **lock + throttle** (se serializan;
nunca corren a la vez sobre el mismo grafo).
**JUSTIF.** El agente autora síncrono → el lag de extract solo afecta hechos
auto-extraídos. Reactivo = timely; safety-net = no se pierde nada; gate = no
quema LLM en vacío. El refine es caro → diario, pero adaptivo ante bursts.

**NUANCE — skills-refine conservador.** Las skills son **ejecutables**: un
merge malo no ensucia datos, **rompe una capacidad que funcionaba**. El
refine-de-skills del largo debe ser más conservador que el de entidades:
apoyarse en el git store de skills (revert seguro), merges proponer-no-auto /
alta-confianza (como el absorb-judge), y **verificar validez** de la skill
unificada antes de reemplazar.

### 2.8 Capa de referencias = documentos coherentes (no sintetizados por Dream)

**DECISIÓN.** Un documento ingerido se conserva **entero** como **reference
page** navegable (el original ya vive en `ingested/<id>/source.md`); se
**indexa y se marca REFERENCE**. Los chunks `corpus` pasan a ser **índice de
recuperación** que apunta a la page. Marcador **REFERENCE** propio (≠
CANONICAL = síntesis de entidades; ≠ chunk crudo). **Dream NO sintetiza
referencias.**
**JUSTIFICACIÓN.** Un doc ya está escrito coherente — meterlo por una
síntesis LLM es un round-trip lossy. Dream consolida lo **disperso**
(experiencia), no lo coherente.
**ABIERTO** (§4): ingestión structure-aware vs blind-chunk; cómo linkea la
page a entidades.

### 2.9 Carril sí-mismo: SOUL (usuario) + skills (dream corto/largo)

**DECISIÓN — SOUL sale del scope de dream.** SOUL = **constitución intrínseca
del agente** (honestidad, solve-by-doing, principios). Pasa a
**`user_authored`, edición manual del usuario**; dream nunca la toca. Es el
**punto de control del usuario** sobre el comportamiento del agente.
**JUSTIFICACIÓN.** Hoy el dream mete en SOUL cosas que **no son** SOUL —
prefs aprendidas ("responder en español", "batch", "confirmar"). Eso es
**perfil del usuario**, no identidad del agente → se rutea a la **entidad
`person:`** (maneja el corto, §2.7). No perdemos el aprendizaje de prefs; lo
ruteamos bien. SOUL queda estable y bajo control humano.

**DECISIÓN — USER.md / MEMORY.md se disuelven** en **inyección dinámica**
(no archivos del dream):
- **USER.md** → el usuario es una entidad `person:`; se resuelve quién se
  identifica en la sesión y se inyecta su entidad (ver
  [memory_context_preload.md](memory_context_preload.md)).
- **MEMORY.md** → contexto ensamblado (importantes + recientes) por el
  `hot_layer` (`durin/memory/hot_layer.py`, ya existe), enriquecido al
  consolidar/compactar.
**JUSTIFICACIÓN.** El propósito (capa siempre-presente) es válido; la
implementación (prosa plana paralela al grafo) duplicaba contenido. La capa
siempre-presente debe ser **vista/pin** sobre las capas reales, no un store
aparte.

### 2.10 Feedback aprendido = entidades `stance` / `practice` (no es clase nueva)

**DECISIÓN.** El **feedback** (cómo trabajar con el usuario, correcciones,
preferencias, principios de operación que el agente aprende) se modela como
entidades del vocab existente:
- **`stance`** (preference/opinion/belief/position) → "una pregunta a la vez",
  "no Claude attribution", "responder en español".
- **`practice`** (skill/routine/method/habit) → "verificá en vivo antes de
  decir done", "deploy vía wheel local", "verificá desde /tmp".

Autoradas por el **agente** (desde correcciones), refinadas por **dream**,
relacionadas a su sujeto (`person:`, `project:`, `tool:`, o global). **NO es
un miembro nuevo del carril SELF** (SELF queda SOUL+skills): el feedback es
**CONOCIMIENTO** ("lo aprendido sobre cómo trabajar"), pinneado al always-on.

**JUSTIFICACIÓN.** El vocab ya tiene `stance`/`practice` exactamente para
esto; reusa grafo + dream-refine (dedup/generaliza feedback repetido) +
provenance (de qué corrección salió) + relaciones — sin clase nueva. (El
"siempre-inyectado" que me hizo dudar es política del `hot_layer`, no una
necesidad de storage.)

**Always-on (qué se inyecta siempre) — cae de la decisión (b):**
- La condición "siempre-activa" es un **atributo estructurado** (`always_on`).
  Por (b), **dream es su dueño**. El agente la crea con default **`true`** (la
  corrección **aplica de inmediato**); **dream rectifica** al consolidar
  (decide qué queda always-on). Unifica las dos variantes ("todas activas
  hasta que dream decida" = agente default-true + dream-owns-attribute).
- **"Demote" ≠ borrar**: salir de always-on = **on-demand** (sigue
  searchable), no se pierde. El set always-on es un subconjunto curado.
- **Criterio de dream para mantener active**: load-bearing/frecuente,
  general > narrow, scoped al usuario/proyecto activo, no superseded.
- **SAFETY (ventana pre-dream)**: aunque el default sea `true`, el `hot_layer`
  aplica un **presupuesto de tokens** (recencia + relevancia + scope al
  usuario/proyecto activo) — un burst de feedback nuevo no debe explotar el
  contexto antes de que dream pode. "Default active" ≠ "inyectar sin límite".

### 2.11 Indexación: FTS vs vector, embedding por tipo, re-derivabilidad

**Restricción real (verificada).** El embedding `intfloat/multilingual-e5-small`
tiene **máximo 512 tokens**; lo que pasa se trunca **silencioso** (no
recuperable por vector). **FTS5/BM25 no tiene límite** → indexa el texto
completo. ⇒ **el chunking/summary es SOLO problema del vector.**

**DECISIÓN — embedding por tipo:**
- **FTS (keyword)**: texto **completo** para todo (entidad:
  name+aliases+attributes+relations+body; referencia: doc entero). No se trocea.
- **Vector — entidad** → embeber un **summary** (≤512 tok) que **mantiene el
  dream-corto**. NO se chunkea una entidad (es unidad coherente). Generaliza el
  fallback `_effective_summary` actual (summary-o-body-cortado, hoy solo para
  *entries*) → dream **escribe** un summary real para entidades largas.
- **Vector — referencia** → **chunks por sección** (≤512 tok, parent-pointer a
  la page, structure-aware). Se recupera la sección relevante de un doc largo.

**INVARIANTE anti-migración (lo irreversible — lockear ahora).** La **fuente
canónica** (body de la entidad / `ingested/source.md` de la referencia) es la
verdad; **summary y chunks son DERIVADOS y re-derivables** con anclas estables
(parent-ref + offset/sección). ⇒ cambiar embedder, tamaño de chunk o estrategia
= **re-derivar desde la fuente**, nunca migración imposible. El "infierno" solo
pasa si los chunks fueran la fuente o no se guardara el original — ninguno aplica.

**BUG a corregir.** El splitter corta por **chars** (`DEFAULT_CHUNK_SIZE=1500`)
pero el límite es **tokens** (512) y durin es multilingüe → en CJK/scripts
densos 1500 chars puede pasar 512 tok y truncar. Cortar por **tokens del
tokenizer del embedding** (≤~480 con margen), no por chars.

**ABIERTO (tuneable, no urgente porque es re-derivable):** migrar a un embedder
long-context (bge-m3 / nomic-embed, 8192 tok) para chunks/summaries más grandes.

---

## 3. Modelo consolidado (vista de un vistazo)

```
CAPAS                     CLASES (storage)                 QUIÉN ESCRIBE
─────────────────────────────────────────────────────────────────────────────
EXPERIENCIA               sessions/ (crudo=verdad)         se graba sola (loop)
  (qué pasó)              session_summary/ (recall view)    summarizer (compaction)

CONOCIMIENTO — entidades  entities/<type>/<slug>.md         agente autora (prosa+links)
  (qué se sabe)                                              + dream corto (attributes)
                                                             + dream largo (dedup/unify/split)
CONOCIMIENTO — referencias ingested/source (REFERENCE)      agente ingiere
                          corpus/ (índice→page)             (dream NO sintetiza)
CONOCIMIENTO — feedback   entities stance:/practice:        agente autora (de correcciones)
  (cómo trabajar)         (always_on attr, pin hot_layer)   + dream refina/decide always-on

SÍ-MISMO                  SOUL.md (constitución)            SOLO usuario (manual)
  (cómo soy/qué sé hacer) skills/<name>/SKILL.md            dream corto (crea) + largo (refina)

SIEMPRE-PRESENTE          (no es clase — se ensambla)       hot_layer (pin/vista sobre lo de arriba)

PRECEDENCIA de escritura:   user > dream > agent          (provenance por campo arbitra)
TRES VELOCIDADES:           síncrono (agente) → corto (~2h, extract) → largo (diario, refine)
CONCURRENCIA:               git sustrato + merge semántico (no textual)
```

---

## 4. Preguntas abiertas (lo que falta resolver)

1. **Concurrencia** — **DECIDIDO** (§2.5): 2 capas ortogonales. Write-time =
   **optimistic con git** (`update-ref` CAS local / `push --force-with-lease`
   cross-host + re-aplicar patch por-campo + precedencia, vía plumbing sin tocar
   el working tree). Exclusión de pasada de dream = lock por working-tree (se
   mantiene). Pendiente fino: reconciliación del working tree con la edición
   humana en vivo.
2. **Tool `memory_upsert_entity`** — **DECIDIDO** (spec en
   [memory_seq_ingesta.md](memory_seq_ingesta.md) E3a): merge no-replace, body
   append atribuido, sin `attributes` (dream extrae), relation dangling
   permitido, dedup a dream, crea-si-no-existe.
3. **`history.jsonl`** — **DECIDIDO: se elimina** (§2.3). Era el feed plano
   global que alimentaba al dream legacy (batches `[RAW] N messages` por
   `cursor`); su único consumidor se disuelve y el dream nuevo lee `sessions/`
   directo. `sessions/<id>/<id>.jsonl` (por-sesión) es la fuente canónica.
4. **Referencias** — **DECIDIDO** (§2.8, §2.11, ingesta E3b): doc entero =
   reference page (marcador REFERENCE); FTS indexa entero; vector = chunks por
   sección (token-split, parent-pointer); agente+dream linkean entidades;
   búsqueda surface la page una vez (dedup por parent). **ABIERTO/tuneable**:
   embedder long-context (8192) y split de blob multi-artículo en varias pages.
5. **Resolución user-de-sesión → entidad `person:`** (§2.9): el mapeo
   channel-user-id → entidad es el problema de identidad cross-channel (R4).
   Trivial para webui (dueño único); manual/LLM en multi-channel.
6. **Curación de la capa siempre-presente** (§2.9, §2.10): el `hot_layer` cura
   por recencia + top-headlines. ¿Basta, o se necesita curación-LLM? Y el
   **presupuesto de pin de always-on** (§2.10): qué `stance`/`practice` se
   inyectan en la ventana pre-dream (recencia + relevancia + scope al
   usuario/proyecto activo) sin explotar el contexto.
7. **Cadencia/disparo de los dreams** — **DECIDIDO** (§2.7): CORTO reactivo
   (session-close + post-compaction) + safety-net ~2h + gate; LARGO diario +
   refine-pressure; ambos comparten lock + throttle.
8. **Reset de cursor** — **DECIDIDO** (§2.6): acción CLI + dashboard
   (`durin memory reprocess --session <id> [--from-turn N]`) que rebobina el
   cursor de extracción por-sesión (default turno 0) → el próximo dream-corto
   re-extrae. Seguro: re-aplicar field-patches es idempotente bajo
   provenance+precedencia (+ dedup). Cursor storage: meta/sidecar por-sesión.

---

## 5. Relación con los docs actuales

Esto desafía partes de `docs/architecture/memory/`:
- `01_data_and_entities.md` §2/§10#6 (stable nunca se consume; eje
  episodic/stable) → el eje se elimina; episodic/stable se disuelven.
- `05_dream_cold_path.md` (dream = autor único desde episodic) → dream pasa a
  extract(corto)/refine(largo); el agente autora; dream lee sesiones crudas.
- El track legacy (`agent/memory.py::Dream`, SOUL/USER/MEMORY) → SOUL pasa a
  `user_authored`; USER/MEMORY se disuelven en hot_layer; skills se reparten
  corto/largo.

Cuando se acuerde el modelo, esos docs se reescriben o archivan con nota.
