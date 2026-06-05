# Rediseño del modelo de memoria — nota de diseño (en discusión)

> **Estado**: working design, 2026-06-05. Documento vivo — capturamos las
> decisiones de la sesión de diseño Marcelo↔agente para iterar sobre él.
> **No** describe lo shipped (eso está en `docs/architecture/memory/`);
> cuando esto se acuerde, esos docs se actualizan o se archivan.
>
> Reto explícito: critícame, no asumas que algo está bien por estar escrito.

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

Marcelo lo afinó: **dos cosas son ciudadanos de primera** — la
**iteración** usuario↔agente (qué pasó, qué acciones se tomaron) y la
**base de conocimiento** del mundo. Y se **acoplan**: el conocimiento se
consulta al momento de decidir la próxima iteración/acción, y la
iteración hace crecer el conocimiento.

---

## 1. Diagnóstico (verificado en código + workspace real)

El sistema actual no tiene "una grieta": son **tres tracks
medio-construidos que no se hablan**, y el único que funciona es el que
pensábamos jubilar.

```
                 INPUT                       PROCESA (Dream)        OUTPUT             ¿Funcionó (vault real)?
TRACK A  history.jsonl (auto/turno)      →  dream legacy (2 fases) → MEMORY/SOUL/USER → SÍ
TRACK B  episodic (el agente elige)      →  dream entity          → entities/<t>/<s>  → NO (0 episodic, 0 pages)
TRACK C  ingest/store(corpus)            →  (nada)                → corpus chunks     → crudo (203 chunks, 1 doc)
```

Hallazgos clave:

1. **Track A funciona** porque su input (`history.jsonl`) se llena solo en
   cada turno. Mantiene SOUL/USER/MEMORY vía un AgentRunner Fase-2 con
   `edit_file`. Es el legacy.
2. **Track B (entidades) nunca se activó** en uso real: 0 entity pages, 0
   episodic. Las 14 entidades del grafo son **phantom** (tags sobre
   entradas `stable`). Toda la maquinaria entity-centric (consolidador,
   attributes, relations, dedup) jamás produjo un byte.
3. **Track C (referencia) está crudo**: 1 documento ingerido →
   203 chunks `corpus`, nunca consolidados. El doc coherente queda
   enterrado en `ingested/`.
4. **El eje `episodic/stable` es el error de base.** Pide al agente
   clasificar por *durabilidad*, y esa adivinanza decide si el
   conocimiento se estructura (→entidad) o no. La durabilidad **no es**
   ninguna distinción real del objetivo. El agente investigando guardó
   hechos como `stable` y un KB como `corpus` — ambos invisibles para B.
   (Metáfora Marcelo: clasificar con una categoría ajena al dominio =
   "fruta en una carnicería". El eje episodic/stable/Tulving es taxonomía
   de **memoria conversacional** — benchmarkeada contra LoCoMo —
   aplicada a un dominio de **trabajo de conocimiento**.)
5. **El agente sólo puede taggear, nunca autorar entidades.** No hay tool
   de autoría; la página estructurada es 100% trabajo del dream. Agente y
   entidades están **desacoplados** → etiquetas huecas (phantom) cuando el
   pipeline se estanca.
6. **Duplicación/fragmentación**: el conocimiento de mxHERO vive partido en
   los tres tracks (resumen en MEMORY.md, hechos en stable/entidades,
   chunks en corpus), sin que ninguno referencie al otro ni esté completo.

---

## 2. El modelo propuesto (decisiones tomadas)

### 2.1 Dos capas de primera + acoplamiento bidireccional

```
EXPERIENCIA (qué pasó / qué hicimos)        CONOCIMIENTO (qué se sabe del mundo)
- sesiones, acciones, modelo del usuario     - entidades + relaciones + documentos
- indexada en el tiempo                       - durable, consultable
        │  ── extrae hechos ───────────────────────────►  (la experiencia hace crecer el conocimiento)
        ◄── consulta para decidir ────────────────────│   (el conocimiento informa la acción)
```

Carril aparte (ni experiencia ni mundo): **SOUL** (cómo se comporta el
agente) + **skills** (procedimientos) = el sí-mismo del agente.

### 2.2 Ruteo por intención (mata el eje durabilidad)

El destino lo decide el agente por **intención**, no adivinando clase:

| Intención | Destino |
|---|---|
| "sé un hecho estructurado sobre una cosa" | **autora/actualiza la entidad** |
| "pasó algo en la interacción" | **guarda observación** (experiencia) |
| "tengo un documento" | **ingiere** (referencia) |

Esto **es** la distinción cosa / experiencia / documento, y **elimina el
eje `episodic/stable`**. Resuelve "B dormido" por construcción: el agente
de research habría autorado `company:mxhero` directo.

### 2.3 Agente autora entidades; Dream es la autoridad de coherencia

- **Agente**: upserta entidades (campos/relaciones) directo, citando
  fuente. Rápido, posiblemente sucio. La entidad **existe de inmediato**
  (no más phantom-hasta-que-corra-dream).
- **Dream**: **autoridad de coherencia**, no autor deferente. Mandato
  **estrecho y único**: sanitizar / unificar / splitear / mergear — todo
  lo relacionado a higiene de memoria, **sin objetivos de tarea**. Tiene
  **visibilidad total** (puede leer toda la memoria + sesiones,
  recuperando el contexto relevante por decisión — no todo en un prompt).
- **Provenance por campo como árbitro**: cada campo lleva
  `{source_ref, at, author: agent|dream|user}`. Es el libro de arbitraje.
- **Precedencia (decidida): `user > dream > agent`.** Dream **gana** sobre
  el agente — clave para coherencia entre **múltiples agentes y modelos**
  escribiendo: ninguna escritura de un agente es canónica por sí sola;
  dream, con visión global y mandato de higiene, arbitra. El humano
  (`user_authored`) sigue arriba de todo. Dentro de un nivel → recencia.
  Contradicción real → el valor viejo va a history (`valid_from/until`),
  no overwrite ciego.
- **División de estructuración (DECIDIDA: opción b)**: el agente autora
  **name + aliases + relations + body (prosa)** — la entidad existe e
  indexa ya (prosa searchable). **Dream es el dueño único del esquema
  estructurado**: extrae/normaliza `attributes` desde la prosa + las
  observaciones. Racional: si dream tiene la autoridad de coherencia, debe
  ser el único estructurador (N modelos emitiendo claves distintas =
  incoherencia). Costo aceptado: los atributos finos demoran hasta que
  corra dream; la prosa cubre la inmediatez vía búsqueda.
- Todo escribe por el **mismo pipeline** (`dream_apply`: JSON Patch +
  validación + `.md.bak` + commit) — dos editores de un wiki, una pluma.

**Implicación / riesgo** (vigilar): dream-gana sube la vara sobre dream —
debe correr **fiable y seguido** (si tarda, los datos provisionales del
agente quedan canónicos en el ínterin) y ser **correcto** (autoridad
única → una mala decisión de dream pisa datos buenos). El salvavidas es
`user > dream` (el humano corrige). Conecta con la fiabilidad de cron que
ya arreglamos.

### 2.6 Concurrencia de agentes — git como sustrato, merge semántico encima

Requisito de primera: **puede haber múltiples agentes concurrentes** sobre
la misma memoria. El diseño no asume un único escritor.

- **Git = sustrato** (versionado, auditoría, sync distribuida, revert).
  Cada escritura = un commit en `memory/.git`.
- **Pero git NO resuelve el conflicto**: su merge **textual** mangla YAML/
  markdown estructurado. La resolución debe ser **semántica**: parsear el
  frontmatter como datos → merge de dict (claves disjuntas = unión
  trivial) → mismo campo = precedencia (`user > dream > agent`) + recencia.
- **Dos niveles de conflicto**:
  1. *Write-time* (síncrono, por página): patch optimista con retry si la
     base cambió. Como los patches son por-campo, lo común auto-mergea;
     mismo campo → precedencia. (Lock o optimistic-retry.)
  2. *Refine-time* (async, cross-entidad): dream resuelve incoherencia con
     visión total (dedup, split, normalize, contradicción).
- El que los patches sean por-campo (no reescrituras de página) es lo que
  **hace tratable** la concurrencia: la mayoría de escrituras concurrentes
  tocan campos distintos → mergean solas.

### 2.4 USER.md / MEMORY.md → inyección dinámica, no archivos del dream

- **USER.md** se disuelve: el usuario es una **entidad `person:`**; al
  inicio de sesión se resuelve quién se identifica y se **inyecta su
  entidad** al contexto. No un archivo que el dream toca constantemente.
- **MEMORY.md** se disuelve: no un archivo, sino **contexto ensamblado**
  (eventos importantes + recientes) inyectado al armar la sesión, y
  enriquecido al consolidar/compactar.
- Ambos se apoyan en el **`hot_layer`** que ya existe
  (`durin/memory/hot_layer.py`: inyecta entity pages, fragments recientes,
  known-entities). La decisión = apoyarse en él y **tirar el archivo plano
  + el dream legacy que lo mantiene**.
- Detalle al fino → [memory_context_preload.md](memory_context_preload.md).

### 2.5 Referencias = documentos coherentes (no sintetizados por Dream)

*(tentativo — falta cerrar, ver §3)*

- Un documento ingerido se conserva **entero** como **reference page**
  navegable (no 200 chunks; el doc coherente deja de estar enterrado en
  `ingested/`).
- Los chunks `corpus` pasan a ser **índice de recuperación** que apunta a
  la página, no la representación.
- Marcador **REFERENCE** propio (≠ CANONICAL = nuestra síntesis de
  entidades; ≠ chunk crudo).
- **Dream NO sintetiza referencias** (un doc ya está escrito coherente —
  meterlo por una síntesis LLM es un round-trip lossy). Dream consolida lo
  **disperso** (observaciones), no lo coherente.

---

## 3. Preguntas abiertas (lo que falta resolver)

1. **Coordinación agente↔dream** — precedencia **DECIDIDA**:
   `user > dream > agent`; estructuración **DECIDIDA** (agente prosa+links,
   dream dueño de attributes). Sigue abierto: forma exacta del tool de
   upsert; mecanismo write-time de concurrencia (lock vs optimistic); y que
   **dream corra fiable y seguido**. Se trabaja en
   [memory_seq_ingesta.md](memory_seq_ingesta.md).
2. **Capa de experiencia** (§2.1): ¿qué es exactamente? ¿`history.jsonl` +
   session summaries? ¿cómo referencia entidades del grafo? ¿cómo se
   "extrae hechos → grafo" sin volver al problema de episodic?
3. **Referencias/documentos** (§2.5): cerrar el diseño de reference page +
   marcador REFERENCE + relación con entidades (`[[company:mxhero]]`);
   ingestión structure-aware vs blind-chunk; evidencia de que el agente hoy
   ingiere mal (blob de KB).
4. **Resolución user-de-sesión → entidad** (§2.4): el mapeo
   channel-user-id → `person:` es en parte el problema **no resuelto** de
   identidad cross-channel (R4, `01_data_and_entities.md` §1). Trivial para
   webui (dueño único); manual/LLM en multi-channel. Se trabaja en
   [memory_context_preload.md](memory_context_preload.md).
5. **Curación de la capa siempre-presente** (§2.4): el dream legacy curaba
   con LLM "qué es importante". El `hot_layer` cura por recencia +
   top-headlines. ¿Basta la heurística, o perdemos algo al quitar la
   curación-LLM?
6. **Destino del dream legacy y los dos crons** (`dream` cada 2h /
   `memory_dream` 03:00): si A se disuelve, ¿qué queda del cron `dream`?
   ¿Se fusiona el refinamiento de entidades en un solo cron?
7. **Riesgo aceptado a vigilar**: "agente autora basura estructurada"
   (entidad duplicada, atributo alucinado) no desaparece — se traslada al
   refinador. El dream-refinador debe ser **más fuerte** que el actual
   (dedup activo + coherencia + corrección humana).

---

## 4. Relación con los docs actuales

Esto desafía partes de `docs/architecture/memory/`:
- `01_data_and_entities.md` §2/§10#6 (stable nunca se consume; eje
  episodic/stable) → el eje se elimina.
- `05_dream_cold_path.md` (dream = autor único desde episodic) → dream pasa
  a refinador; el agente autora.
- El track legacy (`agent/memory.py::Dream`, SOUL/USER/MEMORY) → se
  disuelve hacia inyección dinámica.

Cuando se acuerde el modelo, esos docs se reescriben o archivan con nota.
