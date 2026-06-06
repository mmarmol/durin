# Secuencia: Ingesta de memoria (escritura)

> **Estado**: working — diagrama vivo. Lo refinamos **etapa por etapa**:
> cada etapa describe qué hace, quién, qué escribe, con qué provenance.
> Parte de [memory_model_redesign.md](memory_model_redesign.md).

Actores: **Usuario** · **Agente** (loop) · tools (`memory_upsert_entity` /
`memory_ingest`) · **Conocimiento** (entities + references + corpus-index) ·
**Experiencia** (`sessions/` crudo + `session_summary/`) · **Dream-CORTO**
(extract reciente, ~2h, cursor por-sesión) · **Dream-LARGO** (refine/consolidar,
~diario, graph-wide).

## Diagrama

```mermaid
sequenceDiagram
  actor U as Usuario
  participant A as Agente (loop)
  participant K as Conocimiento<br/>(entities/refs/corpus-idx)
  participant E as Experiencia<br/>(sessions + summaries)
  participant DC as Dream-CORTO<br/>(extract ~2h)
  participant DL as Dream-LARGO<br/>(refine ~diario)

  U->>A: "investiga X / acá un doc / recordá Y"
  Note over A: [E2] ruteo por intención (2 verbos)
  A->>K: [E3a] memory_upsert_entity(company:x, name, relations, body-prosa)<br/>author=agent, prov=turno → dream_apply (lock+git+index)
  Note over K: la entidad EXISTE ya (prosa searchable)
  A->>K: [E3b] memory_ingest(doc) → ingested/source (entero, REFERENCE) +<br/>corpus chunks (índice → page)
  A-->>E: [E4] la interacción se graba sola en sessions/ (crudo = verdad)
  rect rgb(238,238,238)
    Note over DC,DL: ASÍNCRONO (background)
    DC->>K: [E5-corto] lee SESIONES CRUDAS post-cursor (por-sesión)<br/>EXTRAE hechos+attributes → entidades; prefs→person:u<br/>crea/arregla skills recientes · author=dream → patch+prov
    DL->>K: [E5-largo] sobre el GRAFO COMPLETO (sin cursor)<br/>dedup/unifica/splitea entidades; unifica/mejora skills<br/>resuelve contradicciones; self-heal índices
  end
  Note over A,E: [E6] SOUL no la toca dream — user_authored (edición manual del usuario)
```

## Etapas (a refinar al fino)

### E1 — Usuario aporta
- **Qué**: el usuario manda un hecho, un documento, o una instrucción de recordar.
- **Pendiente**: —

### E2 — Agente rutea por intención (dos verbos)
- **Qué**: el agente clasifica en {hecho-de-entidad → upsert, documento → ingest}. La interacción en sí no necesita verbo: se graba sola en la sesión (§2.6).
- **Pendiente**: cómo se le instruye el ruteo (prompt / tool descriptions); aportes mixtos.

### E3a — Upsert entidad (agente autora, light) — DECIDIDO

```
memory_upsert_entity(
  ref:        "<type>:<slug>"   # clave (la tool normaliza el slug); requerido
  name:       "..."             # display (requerido al crear)
  aliases?:   [...]
  relations?: [{to:"<ref>", type:"...", ...metadata}]
  body?:      "markdown prosa"
)
→ author=agent · prov=turno (auto) · dream_apply (validación + .md.bak + git + index)
```

Reglas (decididas):
- **Merge, no replace.** name/aliases = set/union; relations = add (dedup `(to,type)`); `ref` inexistente → **crea**, existe → **merge**.
- **body = append** sección atribuida. **El body es del AGENTE; el dream NO lo
  reescribe** (Sim 2B-1): extrae `attributes` de la prosa (decisión b) y a lo
  sumo appendea una sección atribuida, pero no clobberea la prosa del agente.
  (Antes decía "dream cura la prosa", lo que contradecía la propiedad del body.)
- **Sin `attributes`** — la prosa lleva los hechos; el Dream-corto los extrae (decisión b).
- **Relation a entidad inexistente = dangling permitido** (sin placeholder huérfano; el agente upserta el target si importa; dream resuelve).
- **Dedup → dream**, no en el write (la tool no consulta alias-index). Agente rápido, dream autoridad.
- **La entidad existe ya** e indexa (prosa searchable).

### E3b — Ingesta de documento (referencia) — DECIDIDO
- **Quién/qué**: `memory_ingest(doc)` → `ingested/<id>/source` (entero = fuente canónica, marcador **REFERENCE**). **FTS indexa el doc entero**; **vector = chunks por sección** (token-split ≤~480 tok, structure-aware, parent-pointer a la page). Dream NO sintetiza referencias.
- **Links a entidades**: el agente taggea al ingerir (lo que sabe) + dream-corto extrae el resto → aristas reference↔entidad.
- **Búsqueda**: surface la **page una vez** (dedup por parent; mejor sección como snippet), no N chunks.
- **Invariante**: chunks/summary son DERIVADOS re-derivables desde la fuente (§2.11) → embedder/chunk-size tuneables sin migración.
- **Pendiente (tuneable)**: blob multi-artículo → split en varias pages (v1: una page por doc); embedder long-context.

### E4 — La interacción se graba sola
- **Qué**: el turno queda en `sessions/<id>` (crudo = verdad, anchors de turno estables). NO hay tool de observación (`memory_store`/`episodic`/`stable` se disuelven). El `session_summary` lo produce el summarizer en compaction (vista de recall hot-path, NO input de dream).
- **Pendiente**: ¿`history.jsonl` se elimina (sessions+summaries lo cubren)? (abierto §4).

### E5-corto — Dream CORTO (extract reciente, async)
- **Quién/qué**: lee **sesiones crudas post-cursor** (cursor por-sesión, forward). Extrae hechos + **attributes** → entidades; rutea prefs de usuario a `person:u`; crea/arregla **skills** de la ejecución reciente. `author=dream` (gana). patch+prov por `dream_apply`.
- **JUSTIF**: lee crudo (no summaries) por fidelidad + provenance a anchor de turno (§2.6).
- **Pendiente**: disparo (cron ~2h / reactivo / debounce); presupuesto de visión (recupera por entidad, no todo en un prompt); reset manual de cursor.

### E5-largo — Dream LARGO (refine/consolidar, async)
- **Quién/qué**: sobre el **grafo completo** (sin cursor). Entidades: dedup/absorb, unificar claves, splitear, contradicciones cross-grafo. Skills: unificar duplicadas, mejorar eficiencia, refactor. Self-heal de índices/orphans.
- **NUANCE**: skills son ejecutables → refine conservador (git-revert, merges proponer-no-auto/alta-confianza, verificar validez antes de reemplazar).
- **Pendiente**: ¿diario alcanza? coordinación corto↔largo (que no se pisen; provenance por-campo arbitra).

### E6 — SOUL (fuera de dream)
- **Quién/qué**: `SOUL.md` = constitución del agente, **`user_authored`**, edición manual del usuario. Dream **nunca** la toca. (USER.md/MEMORY.md disueltos → [memory_context_preload.md](memory_context_preload.md).)
- **Pendiente**: —
