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
| `pending` | — | **SE ELIMINA** (clase declarada pero muerta: sin writer, excluida de watcher/hot_layer, vault vacío; ruteo-por-intención no necesita buffer) |
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
sesiones. **El body es del AGENTE — dream NO lo reescribe** (Sim 2B-1): extrae
attributes *de* él y a lo sumo appendea una sección atribuida; nunca clobberea
la prosa. (Body = agente; attributes = dream → ownership por-campo limpio, sin
conflicto agent-vs-dream sobre el body.)
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

**DECISIÓN — provenance por campo como árbitro (Gap A cerrado).** Cada campo
lleva `{source_ref, extracted_at, author}`. **DOS nociones de `author`
coexisten** (distinta granularidad/propósito, no se reemplazan):
- **Page-level** (`provenance.py` hoy, `user_authored | agent_created`): flag
  "esta **página entera** es del usuario, dream no la toca" (protege SOUL,
  anclas user_authored). Se mantiene tal cual.
- **Field-level (NET-NEW)**: `provenance.attributes.<k>.author` y
  `provenance.relations[i].author` ∈ **`{user, agent, dream}`** (3 valores).
  Arbitra la precedencia **por campo**.
Conflicto por-campo → `user > dream > agent`; empate → recencia
(`extracted_at`); contradicción real → temporal validity (`valid_from/until`),
nunca overwrite ciego. Implementado en `memory_writer`/`dream_apply`.
(Hoy `provenance.py` es page-level/2-valores → el field-level de 3 valores es
net-new; el diseño ya lo pedía, solo faltaba decir que NO reemplaza al
page-level.)

**DECISIÓN — pipeline compartido.** Agente y dream escriben por el mismo
`dream_apply` (JSON Patch + validación + `.md.bak` + commit). Dos editores de
un wiki, una pluma.

**DECISIÓN — autoría/control: default agente-administrado, opt-in del usuario
(corrige el `user_authored`-por-default del código viejo).** El agente autora →
las entidades son **agente-administradas por default** (extract/refine/dedup las
manejan). Lo que dream deja en paz es **siempre una decisión EXPLÍCITA del
usuario**, en cuatro formas:

| Nivel | Quién decide | Qué hace dream |
|---|---|---|
| **Entidad (default)** | agente autora | administra (extract/refine/dedup) |
| **Campo editado por el usuario** | usuario edita un campo (Obsidian/dashboard) | respeta ese campo (`author=user`, precedencia) — **no reclama la página** |
| **Entidad marcada "mía"** | usuario opt-in explícito (toggle dashboard / flag) | **hands-off de la página entera** (= el `user_authored` explícito, no default) |
| **Tombstone** (borrar / rechazar-merge) | usuario, acción explícita | respeta el "no" (no re-crea / no re-mergea) — §2.14 |

(`user_authored` deja de ser el default-seguro; pasa a ser un **opt-in
explícito**. Editar un campo ≠ reclamar la página — el código viejo las
confundía.) Las **referencias** son intocadas por dream por una regla
**distinta** (§2.8: no se sintetiza un doc coherente), no por autoría.

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
  **Requisito de track de base (Sim 2D-1)**: el watcher debe diffear contra la
  **base last-synced del working tree** (el blob al que se ff por última vez),
  **no contra HEAD** — si un write programático avanzó HEAD mientras el humano
  editaba una versión vieja, diffear vs HEAD perdería el cambio del dream;
  diffear vs la base + re-aplicar sobre HEAD los preserva ambos. Y **no ff sobre
  un working tree sucio** (esperar a que el humano guarde). Nota para cuando se
  construya (es best-effort).
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

**DECISIÓN — el extract dream lee MATERIAL NUEVO CRUDO: sesiones (turnos) +
referencias recién ingeridas (post-cursor) + el body de las entidades
tocadas, no los summaries.** (Gap #1: referencias→entidades — el extract dream
extrae menciones de docs nuevos igual que de sesiones; batched en la pasada,
NO per-ingest, por eso es viable donde el `post_ingest_threshold` per-write no
lo era. El agente igual taggea lo obvio al ingerir. Sim #3: también lee el
**body de las entidades tocadas** — los attributes se extraen de la prosa que
el agente escribió, decisión b.) Los
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
avanza el puntero. El **refinamiento** (§2.7 largo) no usa cursor de sesión,
pero **NO opera sobre el grafo completo cada vez** — es **incremental sobre un
dirty-set** (entidades que el extract tocó + nuevos alias-overlaps), con un
full-sweep raro de red de seguridad (Sim 4A-2, §2.15). "Grafo completo cada
día" no escala.
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
| **Entidades** | extraer hechos + `attributes` de sesiones nuevas; aplicar prefs de usuario a `person:` (el embedding se compone index-time de attributes+body-head, §2.11 — no hay summary que mantener) | dedup/absorb, unificar claves sinónimas, resolver contradicciones cross-grafo, setear `always_on` de feedback (Sim #2) |
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

**NO hay split automático (Sim-hallazgo #4).** El dream-largo **no** splitea
entidades. Razón: la conflación real ("una entidad que son dos") viene casi
siempre de un **mal merge del absorb**, y eso se recupera con **`git revert`
del commit de merge** (el absorb es un commit → restaura ambas, limpio),
agarrado temprano por el review en UI + quarantine. Conflaciones genuinas (el
agente autoró dos cosas bajo un ref) son raras con refs/slugs explícitos y se
arreglan manual / re-autorando. Split automático no tiene señal limpia (dedup
tiene alias-overlap; split no) y su blast-radius es fabricar una entidad de una
adivinanza. → recovery = `git revert` (temprano) + manual (tarde), no una
operación de split.

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
- La condición "siempre-activa" es un **atributo estructurado** (`always_on`),
  y por (b) **dream es su dueño** — el agente **NO lo setea** (la tool de upsert
  no emite attributes). **El default lo da el hot_layer, no el agente**: una
  `stance`/`practice` **recién autorada se trata como always_on por convención**
  (feedback reciente = pinneado) → la corrección aplica de inmediato **sin que
  el agente escriba el atributo**; **el dream-largo setea `always_on` explícito**
  al consolidar (rectifica/demota). (Sim-hallazgo #2: la versión anterior pedía
  que el agente setee `always_on=true`, lo que contradecía la decisión b.)
- **"Demote" ≠ borrar**: salir de always-on = **on-demand** (sigue
  searchable), no se pierde. El set always-on es un subconjunto curado.
- **Criterio de dream para mantener active**: load-bearing/frecuente,
  general > narrow, scoped al usuario/proyecto activo, no superseded.
- **SAFETY / política de pin (DECIDIDO)**: aunque el default sea `true`, el
  `hot_layer` aplica un **presupuesto de tokens configurable** (slice para
  feedback, aparte de entidades/sesión). Ranking cuando los `always_on`
  exceden: **(1) user-force-pin** (override, entra sí o sí) > **(2) global**
  (reglas transversales) > **(3) scoped al contexto activo**
  (person/project/topic/tool en uso); desempate **recencia +
  frecuencia/load-bearing**. **Tres tiers**: *must-inject* (globales + prefs del
  user activo, acotado) / *if-budget* (scoped) / *on-demand* (narrow/stale/
  superseded → dream las demota; searchable, no inyectadas). Una **corrección
  fresca** rankea alto por recencia → **sobrevive el budget aunque dream no haya
  corrido**; **dream-largo converge** el set (demote/generaliza) a algo chico y
  load-bearing. "Default active" ≠ "inyectar sin límite".

### 2.11 Indexación: FTS vs vector, embedding por tipo, re-derivabilidad

**Restricción real (verificada).** El embedding `intfloat/multilingual-e5-small`
tiene **máximo 512 tokens**; lo que pasa se trunca **silencioso** (no
recuperable por vector). **FTS5/BM25 no tiene límite** → indexa el texto
completo. ⇒ **el chunking/summary es SOLO problema del vector.**

**DECISIÓN — embedding por tipo:**
- **FTS (keyword)**: texto **completo** para todo (entidad:
  name+aliases+attributes+relations+body; referencia: doc entero). No se trocea.
- **Vector — entidad (Gap B cerrado)** → **NO hay campo `summary` separado** en
  `EntityPage`; el embedding se **compone en index-time**: name + aliases +
  **attributes + relations** + body-head, acotado a **≤512 tokens** (mismo fix
  char→token de las referencias). NO se chunkea una entidad (es unidad
  coherente). Racional: en el modelo nuevo los **`attributes` (dream-owned) SON
  la esencia** → se priorizan en la composición para que siempre entren; el
  body-head llena lo que sobra. Sin field → sin staleness, sin write extra,
  reusa `_compose_entity_page_text` (`vector_index.py:387`, hoy cap 1500 chars →
  pasar a ≤512 tok). (`_effective_summary` sigue siendo para *entries*, no
  entidades.)
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

### 2.12 Identidad del principal + entidad de sistema (#5)

**DECISIÓN — el "user" es el PRINCIPAL resuelto, no una entidad ni un archivo.**
El agente sirve a un **principal** (la entidad a la que sirve esta sesión):
humano, otro agente, o el sistema. La "vista de usuario" se **ensambla** del
principal (no se guarda como `user.md`).
- **Tipo único `person:`** (no `user:` aparte) — para que dream lo **unifique**
  (el absorb es **same-type**; dos tipos = duplicado permanente, lo contrario
  de lo que se busca).
- **Channel-ids → `identifiers`** de la persona (`slack:U123`, `telegram:456`,
  `email:…`, `hostname:…`). Extender el absorb-judge para que el overlap cuente
  identifiers además de aliases.
- **Rol interlocutor** → marcador (`attributes.is_user` / relación al agente).
- **Cadena de resolución (siempre aterriza)**: channel-id identificado →
  **owner** de la instalación (config default) → **`person:anonymous`** (piso).
- **Principal POR-MENSAJE, no por-sesión (Sim 2C-1)**: una sesión puede ser
  grupal/multi-usuario → **no hay un principal único por sesión**. El principal
  se **re-resuelve en cada mensaje entrante** (cacheado): el scope PRINCIPAL
  **swapea al hablante actual** ("a quién le respondo ahora"). Prefs en
  conflicto (marcelo→español, susana→inglés) → aplican las **del hablante
  actual**. La SESSION es compartida, pero cada turno se **atribuye a su
  hablante** (la experiencia se rutea a la `person:` correcta).
- **Cold-start (Sim-hallazgo #1)**: en la primera sesión la entidad owner aún
  no existe → **se auto-crea un placeholder** `person:<owner>` (`author=agent_created`
  para que dream/agente la enriquezcan), no se cae a anonymous. El placeholder
  se llena con el uso.
- **Otro agente como interlocutor**: es otra entidad-actor; el **principal
  humano detrás** se hereda por la cadena de delegación (default owner) — se
  sirven las prefs del humano, no del agente intermediario.
- **Autónomo (dream/cron)**: sin principal vivo → vista = SELF + GENERAL (+
  owner como default).
- **Desempate**: dream-absorb (overlap de identifiers/aliases) + UI eventual
  (confirmar/separar/mergear). **Límite R4 aceptado**: no hay solver universal.
- **Ancla user_authored (control manual)**: el usuario ajusta su modelo
  escribiendo una `stance`/`practice` **`user_authored` force-pinned** (dream no
  la toca, como SOUL); el dashboard expone un editor "mi perfil". **NO** un
  `user.md` plano gestionado por dream (era el bug original).

**DECISIÓN — entidad `system:` / `environment:` (el entorno, hueco que faltaba).**
La info de sistema (paths, tools instaladas, capacidades, config de
instalación) — que vivía en MEMORY.md y se quedó sin casa — pasa a una
**entidad de entorno** (`system:local`): **always-on pinned**, **autorada por el
agente** (descubre paths/tools) **+ editable por el usuario**. Reusa entidad +
pin budget + dream refine.

**El `hot_layer` ensambla entonces**: SELF (SOUL) + PRINCIPAL (person + sus
stance/practice + ancla user_authored) + **SYSTEM** (entorno) + GENERAL +
SESSION.

### 2.13 Borrado de entidades y referencias (Gap #3)

**DECISIÓN.** Hoy `memory_forget` borra **entries** (archive reversible) pero
**rechaza entidades** (`forget.py:65`) y no toca el `ingested/source` de una
referencia. En el modelo nuevo eso está del lado equivocado (las entidades/
referencias son lo que el agente autora). Se agrega:
- **Referencia** → agente/usuario la borra: archiva el `ingested/source` + sus
  chunks/page derivados. Unidad-documento, limpio.
- **Entidad** → el **usuario** borra (autoritativo, archive). El **agente** solo
  borra una entidad que **autoró solo** (sin contribuciones de dream/user) o
  **propone** el borrado (dream/user confirma) — dream-gana: una entidad que
  dream cura no se borra unilateral.
- Reusa `archive.py` (ya archiva entidades en absorb) + extiende
  `memory_forget`. Todo reversible por git.

### 2.14 Fallos y recuperación (Sim escenario 3)

- **3A — Extract falla a mitad**: el cursor por-sesión avanza
  **per-batch-aplicado** (no solo al final de la pasada); una falla a mitad
  mantiene lo aplicado. **Re-extracción idempotente por construcción**:
  attributes = set/replace (re-aplicar = mismo valor); relations = add con
  dedup `(to,type)`. → una falla a mitad nunca duplica.
- **3B-1 — Revert re-sincroniza el índice**: un `git revert` (de un mal merge u
  otra op) **toca el working tree** → el watcher re-indexa. Asegurar que el
  revert sea op que toca el working tree (o reindex explícito), si no el índice
  queda en el estado viejo.
- **3C — Quarantine per-entidad**: si extraer una entidad falla 3× estructural,
  se quarantena **esa entidad** (skip 7d, reusa `dream_quarantine`); el resto de
  la pasada y el **cursor de sesión avanzan igual** (una entidad rota no bloquea
  la pasada).
- **3E — Size-guard de referencias**: arriba de un umbral de tokens (tunable),
  la referencia se **indexa como REFERENCE pero NO se LLM-extrae** (bastan los
  tags del agente al ingerir); acota el envelope de costo (gap #8).
- **Tombstones (3B-2 / 3D-1) — DECIDIDO**: las **decisiones estructurales
  negativas del usuario** (delete, un-merge, reject) son **acciones EXPLÍCITAS
  del usuario** (§2.4, nivel tombstone) que dream respeta — `user > dream` cubría
  solo valores de campo, no estas. Mecanismo (reusa el modelo, sin store nuevo):
  - **Borrado**: el contenido va a archive (recuperable) pero el **ref queda
    ocupado por un marker tombstone** (`{tombstone: deleted}`). El extract ve el
    tombstone → **no re-crea** (a lo sumo re-surfacea "lo borraste y se mencionó
    de nuevo"). Des-borrar = el usuario saca el tombstone.
  - **Rechazo de merge**: un marker **`do_not_absorb: [X,Y]`**; el absorb-judge
    lo chequea → nunca re-propone ese par.
  - **Permanente, override-able por el usuario** (nueva evidencia NO lo levanta
    sola; el usuario sí). Solo el usuario tombstonea (es su autoridad).

### 2.15 Escala y costo (Sim escenario 4)

- **4A-2 — Refine incremental, NO graph-wide cada vez (refinamiento de diseño).**
  "Refine sobre el grafo completo diario" no escala (5,000 entidades = re-judge
  redundante de todos los pares). El dream-largo opera sobre un **dirty-set**:
  entidades que el extract tocó + **nuevos alias-overlaps** desde el último
  refine (vía alias index). **Full-sweep raro** (semanal/mensual) de red de
  seguridad. Corrige §2.6.
- **4B-1 — Budget per-pass + drain en el extract.** Una ingesta masiva (300
  refs) = spike de costo. El extract reusa el patrón del dream viejo
  (`max_seconds_per_run` + drain-loop): extrae N por pasada, **difiere el resto**
  a la próxima. Acota el envelope (gap #8).
- **4A-1 — known-entities = sample, no exhaustiva.** A escala, la inyección
  GENERAL del hot_layer es un **sample top/relevante**; el prompt debe dejar
  claro que **no es exhaustiva** → el agente **busca** las entidades
  task-specific. Degradación graceful (ya implícito en "GENERAL bounded").
- **Escalan bien (confirmado)**: el **cursor por-sesión** (el extract solo lee
  post-cursor, no re-lee meses de sesiones) y el **pin budget de always-on**
  (§2.10 converge/demota el set de feedback). El pin budget era load-bearing.

**Capa de grafo — punto de evolución (no graph DB, no ahora).** El diseño tiene
operaciones con forma de grafo (traversal, vecindario, dedup, dirty-set), hoy
servidas por **frontmatter `relations` + alias index + build in-memory**
(`graph.py`, `aliases_index.py`) — **suficiente para las operaciones actuales**
(ninguna pide multi-hop ni analytics). Una **graph DB NO puede reemplazar el
file-first** (markdown = canónico); a lo sumo sería un **índice de grafo
DERIVADO** (lo más liviano: tabla SQLite `edges(from,to,type)`, re-derivable),
al lado de FTS+vector. Se agrega **solo cuando** aparezca (a) una feature de
**graph-reasoning del agente** (multi-hop, analytics, shortest-path) — hoy no
existe; o (b) un **cuello de botella MEDIDO** a escala. Hasta entonces: YAGNI.

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
CONOCIMIENTO — sistema    entity system:local (always-on)   agente autora (descubre) + usuario edita
  (entorno/instalación)   paths/tools/capacidades/config

SÍ-MISMO                  SOUL.md (constitución)            SOLO usuario (manual)
  (cómo soy/qué sé hacer) skills/<name>/SKILL.md            dream corto (crea) + largo (refina)

PRINCIPAL (interlocutor)  person: resuelto (id→owner→anon)  vista compuesta (no se guarda)
  (a quién sirvo)         + sus stance/practice + ancla     ancla = stance user_authored force-pin

SIEMPRE-PRESENTE          (no es clase — se ensambla)       hot_layer = SELF+PRINCIPAL+SYSTEM+GENERAL+SESSION

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
5. **Identidad del principal** — **DECIDIDO** (§2.12): `person:` único +
   channel-ids en `identifiers` + rol interlocutor + vista compuesta del
   principal + cadena de resolución (identificado → owner → anonymous) +
   dream-absorb/UI para desempatar (límite R4 aceptado). Ancla `user_authored`
   force-pinned para control manual. Entidad `system:` nueva para el entorno.
6. **Presupuesto de pin de always-on** — **DECIDIDO** (§2.10): budget de tokens
   configurable + ranking (user-force-pin > global > scoped; desempate recencia
   + frecuencia) + tres tiers (must-inject / if-budget / on-demand) + dream-largo
   converge el set. (Sub-abierta menor: ¿la curación general del hot_layer
   —entidades/sesión— necesita LLM o basta heurística? Tuneable.)
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

---

## 6. Notas de auditoría (código verificado, gaps cerrados)

Pasada doc↔código antes de implementar (sin migración: borrón y cuenta nueva).

1. **Referencias→entidades** (§2.6): el extract dream lee **material nuevo =
   sesiones + referencias nuevas** (batched, NO per-ingest); el agente taggea lo
   obvio al ingerir. Resuelve la contradicción E3b↔§2.6 y el placeholder vacío
   de `ingestion.py:69` (Phase-3-over-ingested nunca construida).
2. **`pending`**: clase muerta (sin writer; excluida de watcher/hot_layer;
   vault vacío) → **ELIMINADA** (§2.3).
3. **Borrado** (§2.13): entidad/referencia borrables vía archive (usuario
   autoritativo; agente solo lo-que-autoró-solo / propone). Hoy `forget.py:65`
   rechaza entidades.
4. **Convergencia de creación / dedup**: exact-slug → **upsert-merge**
   (determinista, seguro, ya está); fuzzy → **absorb-judge ON conservador** en
   el modelo nuevo (hoy `enabled=False` por blast-radius, `schema.py:254`; el
   nuevo lo necesita activo porque dream es la autoridad + hay dos caminos de
   creación; seguridad = confianza 95 + quarantine + git-revert + review UI).
5. **El summarizer SOBREVIVE**: el `Consolidator` loop-driven (`loop.py:1481`)
   que escribe `session_summary/` es **separado** del dream — no se toca. El
   reframe "matar legacy" era solo el dream de MEMORY/SOUL/USER, no el summarizer.
6. **Extract dream = dos mecanismos**: JSON-Patch para entidades (decisión b) +
   agentic `SkillWrite` para skills. Reusa el motor del ex-"legacy".
7. **Schema del vector** (`vector_index.py:8`): falta **parent-ref/section** para
   chunks de referencia → extender el schema (impl, no cambia el diseño).
8. **Costo del extract dream**: leer sesiones+referencias crudas + extraer es
   LLM por pasada → **gate/envelope** (cadencia + min-tokens), acotado por la
   pasada (no per-write).

**Reframe de naming**: lo que llamé "dream legacy" **no se mata** — es el
**extract dream** renombrado/repurposed (input: sesiones+refs en vez de
`history.jsonl`; output: entidades+skills en vez de MEMORY/USER planos; SOUL →
user_authored). Crons (`dream`/`memory_dream`) a renombrar en inglés.

> **Pendiente de forma**: estos docs del plan están en español y deben pasar a
> **inglés** (regla: code + docs en inglés). Conversión al cerrar el diseño.

### 6.1 Segunda pasada (consistencia interna + schemas que el diseño asume)

Sin contradicciones internas nuevas. Schemas abiertos suficientes (tipos
arbitrarios `system:`/`stance:`/`practice:` parsean; `identifiers`/`always_on`
round-tripean vía `extra` de `EntityPage`). Dos huecos cerrados:

- **Gap A — provenance por-campo con `author` 3-valores**: el diseño ya lo pedía
  (§2.4); faltaba reconciliar con el page-level. Cerrado en §2.4: field-level
  `{user,agent,dream}` (net-new) coexiste con page-level `{user_authored,
  agent_created}` (se mantiene). `provenance.py` hoy es 2-valores/página.
- **Gap B — sin campo `summary` en `EntityPage`**: §2.11 lo asumía; no existe.
  Cerrado: embedding **index-time** (name+aliases+attributes+relations+body-head
  ≤512 tok); los attributes son la esencia. Sin field nuevo.

**Inventario net-new (esqueleto del plan de impl., no huecos)**: ~32 módulos
UNTOUCHED, ~18 MODIFY, 2 ORPHAN (`consolidator_tags`, `dream_archive_consumed` —
mueren con history.jsonl/episodic), ~10 net-new (dos runners extract/refine,
extracción sesión→entidad, provenance por-campo, ranking always-on + pin budget,
resolución de principal, descubrimiento `system:`, cursor por-sesión,
refine-pressure, forget de entidades). Config net-new: owner, pin-budget,
system-entity (absorb=flip-true; crons=reasignar). **`identity.md` y
`SKILL.md` son inconsistentes entre sí HOY** y ambos hay que reescribirlos
(sacar `memory_store`, agregar `memory_upsert_entity`, disolver SOUL/USER/MEMORY).
El **extract dream es composición de dos motores**: `DreamConsolidator` (patch,
entidades) + path agentic `SkillWrite` (skills) leyendo sesiones+refs.
