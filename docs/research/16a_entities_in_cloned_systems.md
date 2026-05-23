# 16a — Entidades en sistemas clonados localmente

> Investigación de referencia para `docs/16_entity_centric_memory.md`.
> Cubre todos los sistemas con código clonado en `~/git_personal/`. NO
> propone decisión final — alimenta el doc 17 de síntesis.

---

## §1 — Cobertura

Sistemas/plugins revisados:

| Sistema | Plugin/módulo | ¿Modela entidades como first-class? |
|---|---|---|
| Hermes | `plugins/memory/holographic/` | Sí, tabla `entities` SQLite con HRR retrieval |
| Hermes | `plugins/memory/honcho/` | Sí, "peer" como entidad principal (peer card + conclusions) |
| Hermes | `plugins/memory/openviking/` | No (tipa item, no entidad nombrada) |
| Hermes | `plugins/memory/mem0/` | Delegado a backend (server-side fact extraction) |
| Hermes | `plugins/memory/hindsight/` | Delegado a backend (opaco) |
| Hermes | `plugins/memory/retaindb/` | No (tipa item con 6 valores) |
| Hermes | `plugins/memory/supermemory/` | No |
| Hermes | `plugins/memory/byterover/` | No |
| Hermes | `agent/curator.py` + `tools/skill_usage.py` | Sí, indirectamente: **skills** como entidades con lifecycle state machine |
| Hermes | `agent/background_review.py` | No clasifica entidades; itera sobre umbrellas |
| OpenClaw | `extensions/memory-lancedb/` | No (categoría plana enum cerrado por entry) |
| OpenClaw | `extensions/memory-core/` | No (concept tags planos sin tipo) |
| OpenClaw | `extensions/memory-wiki/` | **Sí**, página por kind ∈ {entity, concept, source, synthesis, report} con `entityType` libre, `aliases`, `canonicalId`, `claims`, `personCard`, `relationships` |
| OpenClaw | `extensions/active-memory/` | No (sub-agent de filtrado, no manipula entidades) |
| OpenClaude | `src/memdir/`, `src/services/extractMemories/` | No (tipa item con 4 valores: `user|feedback|project|reference`); memorias son archivos sueltos |
| OpenHands | `skills/agent_memory.md` | No (un único `.openhands/microagents/repo.md` por repo) |
| OpenCode | `packages/identity/` (no es memoria) | Sin sistema de memoria con entidades |
| Pi | — | Sin sistema de memoria local |

Sistemas que NO aportaron evidencia para entity-centric memory: OpenHands, OpenCode, Pi (mencionados en el brief — verificados, no tienen mecanismos aplicables).

El sistema **más cercano conceptualmente** al modelo propuesto en doc 16 es **OpenClaw memory-wiki** — tiene exactamente la estructura `entities/<entity>.md` con frontmatter tipado, aliases para unificación, claims con status para conflictos, freshness para lifecycle. Es la única referencia "completa" de la muestra. Lo siguiente en cercanía es **Hermes skill curator** — aplica el patrón "entidad como página markdown con lifecycle automático" pero a skills, no a entidades semánticas.

---

## §2 — Por sistema

### §2.1 — Hermes / Holographic plugin

**Localización**: `/Users/marcelo/git_personal/hermes-agent/plugins/memory/holographic/`
- `store.py` (DB + entity extraction)
- `retrieval.py` (search/probe/related/reason/contradict)
- `__init__.py` (tool wrapper + auto-extract)

#### Q1 — Modelo de identidad

**Identidad = nombre case-insensitive con aliases comma-separated**. Ver `store.py:30-36`:

```python
CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Resolución en `store.py:433-461` (`_resolve_entity`):

```python
# Exact name match (case-insensitive via LIKE)
row = self._conn.execute(
    "SELECT entity_id FROM entities WHERE name LIKE ?", (name,)
).fetchone()
if row is not None:
    return int(row["entity_id"])

# Search aliases — aliases stored as comma-separated
alias_row = self._conn.execute(
    """
    SELECT entity_id FROM entities
    WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'
    """,
    (name,),
).fetchone()
```

No hay normalización lexical (no `slugify`), no hay embedding match, no hay LLM resolution. Es **string equality case-insensitive + alias lookup**. El `aliases` es una columna `TEXT` comma-separated, no una tabla aparte.

#### Q2 — Granularidad / tipos modelados

**Vocabulario abierto declarado, vocabulario vacío en la práctica**. La columna `entity_type` (línea 33) tiene `DEFAULT 'unknown'`. Confirmé que ningún path de escritura la setea:

- `_resolve_entity` (`store.py:457-458`): `INSERT INTO entities (name) VALUES (?)` — no incluye `entity_type`.
- No hay update path en todo el archivo que toque `entity_type`.

El path de lectura tampoco usa `entity_type` para filtrar/ranking:

- `retrieval.probe()` (`retrieval.py:114-190`): no consulta `entity_type`. Resuelve entity por encode_atom del nombre lowercased y unbind del HRR vector.
- `retrieval.related()` (`retrieval.py:192-258`): no consulta `entity_type`.
- `retrieval.reason()` (`retrieval.py:260-336`): no consulta `entity_type`.
- `retrieval.contradict()` (`retrieval.py:338-442`): usa el conjunto de entidades por fact pero no su tipo.

**Confirmo doc 14**: `entity_type` es anti-patrón. Columna declarada, nunca asignada, nunca usada en lectura.

Por separado, **el item de memoria sí está tipado** — los `facts` tienen `category TEXT DEFAULT 'general'` (`store.py:20`) con índice (línea 45). El schema del tool expone enum cerrado de 4 valores (`__init__.py:66`):

```python
"category": {"type": "string", "enum": ["user_pref", "project", "tool", "general"]}
```

#### Q3 — Conflictos y evolución

**No hay resolución de contradicciones automática en escritura**. Lo que sí hay es un **detector** de contradicciones por algebra HRR + entity overlap (`retrieval.py:338-442`):

```python
def contradict(self, category=None, threshold=0.3, limit=10) -> list[dict]:
    """Find potentially contradictory facts via entity overlap + content divergence."""
    ...
    # Compare all pairs: high entity overlap + low content similarity = contradiction
    entity_overlap = len(ents1 & ents2) / len(ents1 | ents2)
    if entity_overlap < 0.3:
        continue
    content_sim = hrr.similarity(v1, v2)
    contradiction_score = entity_overlap * (1.0 - (content_sim + 1.0) / 2.0)
```

Esto **identifica** pares contradictorios; **no los resuelve**. La resolución queda al modelo (lee output del tool y decide). Si el usuario marca uno como `unhelpful` (`store.py:353-392`), su `trust_score` baja `-0.10`, bajando su ranking. Es **trust-weighted soft deprecation**, no override.

`update_fact` (`store.py:242-304`) acepta cambios parciales pero **siempre re-extrae** entidades del nuevo contenido (líneas 285-293). No marca diff entre observaciones — sobreescribe.

#### Q4 — Lifecycle

**Lifecycle por trust score + opcional temporal decay**. Tres mecanismos:

1. **Trust score** (`store.py:78-82`, `353-392`): cada fact tiene `trust_score ∈ [0,1]`, default 0.5. `record_feedback` ajusta asimétrico: `+0.05` helpful, `-0.10` unhelpful. Búsquedas filtran por `min_trust=0.3` (línea 195). **No archiva**: el fact sigue en la tabla, simplemente deja de aparecer en queries con threshold.

2. **Temporal decay opcional** (`retrieval.py:569-593`): `temporal_decay_half_life` en config — multiplica score por `0.5^(age_days / half_life)`. Disabled por default (`0`).

3. **`remove_fact`** (`store.py:306-321`): borrado duro a pedido del modelo. No automático.

**No hay archive separate from delete**. **No hay borrado automático**.

#### Q5 — Retrieval entity-aware

**Fuertemente entity-aware**, vía algebra HRR. Cuatro operaciones distintas (`retrieval.py:48-336`):

- `search(query)`: FTS5 + Jaccard + HRR similarity sobre content (no usa entity_type).
- `probe(entity)`: extracts facts where entity plays structural role. Encode entity como atom + bind con role_entity, unbind del bank.
- `related(entity)`: facts conectados estructuralmente con la entity.
- `reason(entities)`: **multi-entity intersection** — facts donde TODAS las entidades juegan rol (AND via `min(entity_scores)`).

La estructura HRR (`store.py:474-496`, `_compute_hrr_vector`) embebe entidades como roles en el vector binding del fact. La entidad participa **algebraicamente** del retrieval, no como filtro plano.

Pero — y esto es clave — **el `entity_type` no entra en ninguna operación**. Toda la riqueza es por nombre.

#### Q6 — Costo operacional

- **Extracción**: regex determinista, **sin LLM** (`store.py:398-431`, `_extract_entities`). 4 reglas: capitalized multi-word, double-quoted, single-quoted, "aka" patterns.
- **Auto-extract de session** (`__init__.py:359-397`, `_auto_extract_facts`): también regex (`_PREF_PATTERNS`, `_DECISION_PATTERNS`), sin LLM.
- **Inline**: la extracción corre cada `add_fact`. No hay job offline.

Costo por fact ≈ regex matches + HRR encode (numpy). Trivial.

#### Q7 — Lección directa para durin

- **Adoptar**: la operación `contradict` (entity overlap + content divergence) como heurística para el dream — detectar pares de entries que mencionan la misma entidad y tienen baja similaridad de contenido. Implementable sin LLM si tenés embeddings.
- **Descartar**: la columna `entity_type` declarada-pero-vacía. Es trampa real: si tipás, **tipá en el productor** (consolidator/dream con LLM), no en el schema. La declaración sin asignación crea expectativas falsas.
- **Descartar**: regex `_extract_entities` para nombres propios. Capitaliza-multiword es ruidoso ("Phase 2", "United States" capturados como entidades) y no captura snake_case ni dotted paths.

---

### §2.2 — Hermes / Honcho plugin

**Localización**: `/Users/marcelo/git_personal/hermes-agent/plugins/memory/honcho/`
- `__init__.py` (tool wrapper)
- `session.py` (cliente)
- backend remoto opaco (Honcho SDK)

#### Q1 — Modelo de identidad

**Identidad = `peer_id` slug sanitizado**. `session.py:266-268`:

```python
def _sanitize_id(self, id_str: str) -> str:
    """Sanitize an ID to match Honcho's pattern: ^[a-zA-Z0-9_-]+"""
    return re.sub(r'[^a-zA-Z0-9_-]', '-', id_str)
```

La regla de construcción del peer_id (`session.py:299-314`):

1. Prefer `runtime_user_peer_name` si pin_peer_name=False.
2. Fallback a `config.peer_name`.
3. Fallback a `user-{channel}-{chat_id}`.

El AI también es peer (`assistant_peer_id`). Aliases built-in: `"user"` y `"ai"` (`__init__.py:51-53`).

**No hay resolución alias-a-peer en el cliente**. Si el modelo pasa `peer="marcelo"` y existe `peer_id="user-cli-default"`, son peers distintos. Honcho asume que el caller mantiene la convención de IDs.

#### Q2 — Granularidad / tipos modelados

**Un solo tipo: `peer`**. No hay `entity_type`. Honcho modela cada peer como una entidad rica con tres facetas (en el cliente Hermes):

- **Peer card** (`session.py:1006-1023`): lista plana de strings — "key facts about the peer (name, role, preferences, communication style, patterns)".
- **Conclusions** (`session.py:1071-1118`): facts persistentes que un peer hace sobre otro (o sobre sí mismo).
- **Representation**: estado interno semi-opaco del backend que se sintetiza por peer (no expuesto en cliente, solo se lee).

El concepto subyacente es **peer-as-mental-model**: Honcho mantiene un modelo del peer que el cliente actualiza con conclusions y lee con dialectic Q&A.

#### Q3 — Conflictos y evolución

**Self-healing por LLM en el backend**. `__init__.py:160-162`:

```python
"Deletion is only for PII removal — Honcho self-heals incorrect conclusions over time."
```

El usuario no debe corregir manualmente conclusiones erradas — el backend internamente reconcilia con observaciones nuevas. **Delegado al servicio remoto**. Sin visibilidad del mecanismo.

El **dialectic query** (`session.py:531-583`) admite `reasoning_level ∈ {minimal, low, medium, high, max}` para resolver preguntas que requieren reconciliación cross-session.

`delete_conclusion` (`session.py:1120-1150`) borra una conclusión por ID — **explícitamente reservado para PII removal**, no para corrección.

#### Q4 — Lifecycle

**Sin lifecycle visible en cliente**. El backend administra la representación del peer; no hay archivado client-side, no hay decay configurable. El cliente solo:

- Crea conclusiones (`create_conclusion`).
- Lee peer card / representation / dialectic.
- Borra conclusiones por ID (solo PII).

Hay manejo de cadence/staleness pero **del prefetch**, no de las entidades: el dialectic se invalida si pasó X turnos sin uso (`__init__.py:799-870`).

#### Q5 — Retrieval entity-aware

**Toda la API es por peer**. Cada tool toma `peer` parameter:

- `honcho_profile(peer="user")`: peer card.
- `honcho_search(query, peer)`: semantic search dentro del contexto del peer.
- `honcho_reasoning(query, peer)`: dialectic sobre ese peer.
- `honcho_context(peer)`: full session context.

**No hay query cross-peer**. Si querés "qué saben sobre Marcelo Y sobre durin", son dos llamadas distintas (no hay `reason([peer1, peer2])` como Holographic).

#### Q6 — Costo operacional

**Pesado, offline-ish**. Cada turno:
- Sync de mensajes al backend (`sync_turn`, `session.py:1120-1151`) — async, fire-and-forget.
- Background dialectic prefetch (`__init__.py:702-774`) — cadencia configurable (default cada turno), opcional `dialecticDepth` 1-3 passes.

El backend Honcho ejecuta LLM passes para mantener la representación. Cost-aware con knobs: `injectionFrequency`, `contextCadence`, `dialecticCadence`, `dialecticDepth`, `_BACKOFF_MAX` (`__init__.py:806-808`) tras empty streaks.

#### Q7 — Lección directa para durin

- **Adoptar conceptualmente**: la idea de que **cada persona** (o entidad de alta cardinalidad como `person:marcelo`) tiene una "card" de facts editable (`set_peer_card`/`get_peer_card`) **separada** del log episódico. Eso encaja con el modelo entity-centric de doc 16: la página `entities/person/marcelo.md` es exactamente análoga a la peer card de Honcho.
- **Adoptar**: la idea de que la edición de la card está **autorizada al curator/dream**, no al usuario directo. En Honcho el modelo edita conclusions pero NO borra (excepto PII).
- **Descartar**: delegar la reconciliación a un backend opaco. durin necesita determinismo y auditoría. "Self-healing por LLM" sin visibilidad es justamente lo que el doc 16 marca como riesgo.
- **Cuidado**: Honcho no tiene `entity_type` — todo es peer. Asume cardinalidad baja. Si durin tiene `person`, `project`, `topic`, `incident` simultáneamente, el modelo Honcho-style necesita escalar a N tablas de "cards" por tipo — o, equivalente, N directorios `entities/<type>/`.

---

### §2.3 — Hermes / OpenViking plugin

**Localización**: `/Users/marcelo/git_personal/hermes-agent/plugins/memory/openviking/__init__.py`

#### Q1 — Modelo de identidad

**Delegado al backend remoto**. El cliente solo envía contenido + categoría hint. La extracción y resolución de entidades vive server-side. Líneas 587-590:

```python
"""Commit the session to trigger memory extraction.

OpenViking automatically extracts 6 categories of memories:
profile, preferences, entities, events, cases, and patterns."""
```

#### Q2 — Granularidad / tipos modelados

**El cliente tipa el item, no la entidad**. Tool schema (`__init__.py:285-289`):

```python
"category": {
    "type": "string",
    "enum": ["preference", "entity", "event", "case", "pattern"],
    "description": "Memory category (default: auto-detected).",
}
```

Nota: `entity` es una categoría de item — significa "esta memoria habla de una entidad" — no es un tipo de entidad. Mezcla problemática.

El backend extrae 6 categorías internas (`profile, preferences, entities, events, cases, patterns`) pero el cliente solo expone 5.

#### Q3-Q4 — Conflictos / Lifecycle

**Opacos**. El cliente no maneja ninguno.

#### Q5 — Retrieval entity-aware

No hay tool entity-specific. Solo búsqueda semántica plana. Sub-tools (`__init__.py:281-289`): `query` (semantic search), `remember` (explicit fact). El `category` se inyecta como **prefijo textual** del mensaje a OpenViking (`__init__.py:866`):

```python
text = f"[Remember — {category}] {content}"
```

#### Q6 — Costo

Server-side. Cliente paga el round-trip.

#### Q7 — Lección directa para durin

- **Descartar**: enum cerrado en el tool. OpenViking lo paga: la asimetría 5-cliente vs 6-backend muestra deuda de versionado.
- **Descartar**: "category como prefijo textual" en el contenido. Acopla categorización a contenido escrito — irrecuperable si después querés filtrar/contar.
- **Observación**: las 5 categorías de OpenViking (`preference|entity|event|case|pattern`) NO se mapean limpiamente a los 6 tipos consolidables de doc 16. `entity` colapsa todo lo nombrado. `case` y `pattern` no tienen análogo directo.

---

### §2.4 — Hermes / Mem0 plugin

**Localización**: `/Users/marcelo/git_personal/hermes-agent/plugins/memory/mem0/__init__.py`

#### Q1-Q5 — Todo server-side

El cliente Hermes Mem0 tiene 3 tools: `mem0_search`, `mem0_conclude`, `mem0_recall_facts`. **Metadata libre** (`__init__.py:213-217`):

```python
def _read_filters(self):
    """Filters for search/get_all — scoped to user only for cross-session recall."""
    ...
def _write_filters(self):
    """Filters for add — scoped to user + agent for attribution."""
```

`mem0_conclude` (`__init__.py:345-359`) usa `infer=False` — **bypass de extracción server-side** para escribir un fact literal:

```python
client.add(
    [{"role": "user", "content": conclusion}],
    **self._write_filters(),
    infer=False,
)
```

Eso es el patrón: el cliente declara explícitamente "este es un fact, no analices nada".

#### Q6 — Costo

Server-side LLM extraction cuando `infer=True` (default).

#### Q7 — Lección directa para durin

- **Adoptar**: el patrón `infer=False` para conclusions explícitas. Análogo a `memory_store` de durin con entidades pre-extraídas: si el caller ya sabe qué entidades menciona el fact, no relanzar extracción.
- **Sin información** sobre cómo Mem0 modela tipos de entidad — totalmente delegado.

---

### §2.5 — Hermes / Hindsight, Supermemory, RetainDB, ByteRover

Patrón común: **clientes thin sobre backends opacos**. Solo tipan **el item de memoria**, no entidades.

#### RetainDB (`__init__.py:85-95`)

```python
"memory_type": {
    "type": "string",
    "enum": ["factual", "preference", "goal", "instruction", "event", "opinion"],
    "description": "Category (default: factual).",
}
```

6 valores. **Doc 14 no anotó este enum** — vale corregir el record: RetainDB tipa el item con 6 categorías (factual/preference/goal/instruction/event/opinion), comparable a OpenViking-5 y LanceDB-5.

También tiene `scope ∈ {USER, PROJECT, ORG}` para uploads de archivos (`__init__.py:118`), que es una **dimensión ortogonal** — tier de privacidad, no tipo de contenido.

#### Hindsight

Solo `content + context + tags` libres (`__init__.py:241-260`). Sin tipo.

#### Supermemory

Sin categoría. `content`, `query` (`__init__.py:135-163`).

#### ByteRover

Sin tipo en cliente. Operación contra árbol jerárquico server-side.

#### Q7 — Lección directa para durin

- **Patrón cross-3-clientes**: cuando el backend hace la extracción, el cliente no tipa entidades. Si durin va a hacer la extracción **localmente** (con su consolidator/dream), tipar entidades es **una decisión del lado del productor**, no del schema. RetainDB confirma que tipar el item es razonable; ningún cliente tipa la entidad.
- **Convergencia parcial**: los enums de item-type tienen 4-6 valores cada uno, sobreposición moderada. Los 4 de Holographic (user_pref|project|tool|general) y los 4 de OpenClaude (user|feedback|project|reference) están en el mismo orden de magnitud. Pero los conjuntos NO se solapan exactamente.

---

### §2.6 — Hermes / agent/curator.py + skill_usage.py

**Localización**:
- `/Users/marcelo/git_personal/hermes-agent/agent/curator.py`
- `/Users/marcelo/git_personal/hermes-agent/tools/skill_usage.py`
- `/Users/marcelo/git_personal/hermes-agent/tools/skill_manager_tool.py`

**Observación crítica**: aunque "skill" no es un tipo de entidad semántica como `person` o `project`, **el modelo operacional es exactamente "entidad como página markdown con lifecycle"** — el patrón que durin propone. Vale estudiarlo aunque no sea memory.

#### Q1 — Modelo de identidad

**Identidad = `name` slug del directorio**. `tools/skill_manager_tool.py:178-190` (`_validate_name`):

```python
VALID_NAME_RE = re.compile(r"^[a-z0-9._-]+$")  # implied from context
def _validate_name(name: str) -> Optional[str]:
    ...
```

Skills viven en `~/.hermes/skills/<category>?/<name>/SKILL.md`. La identidad es el `name` del directorio, no el `name` del frontmatter (aunque deberían coincidir).

`category` opcional como agrupador (otro dir, `skill_manager_tool.py:271-275`):

```python
def _resolve_skill_dir(name: str, category: str = None) -> Path:
    if category:
        return SKILLS_DIR / category / name
    return SKILLS_DIR / name
```

No hay aliases. Si dos skills tienen nombres similares (`hermes-config-foo` vs `hermes-config-bar`), son skills distintas — la consolidación es responsabilidad del curator (ver Q3).

#### Q2 — Granularidad / tipos

**Vocabulario abierto, sin tipo per-se**. Las skills tienen `category` libre (single dir segment, validado por regex pero no por enum, `skill_manager_tool.py:192-214`):

```python
def _validate_category(category: Optional[str]) -> Optional[str]:
    if "/" in category or "\\" in category:
        return "Invalid category..."
    if not VALID_NAME_RE.match(category):
        return "Invalid category..."
```

La frontmatter de SKILL.md (`skill_manager_tool.py:217-253`) requiere solo `name` y `description`. **No requiere `type` o `kind`**.

El curator targetea por **prefix clusters** (`agent/curator.py:361-365`):

> "Identify PREFIX CLUSTERS (skills sharing a first word or domain keyword). Examples you are likely to find: hermes-config-*, hermes-dashboard-*, gateway-*, codex-*, ollama-*, anthropic-*, gemini-*, mcp-*, salvage-*..."

O sea: el agrupamiento es **post-hoc por nombre**, no por tipo declarado.

#### Q3 — Conflictos / evolución

**`absorbed_into` declara fusión explícita**. `skill_manager_tool.py:557-611` (`_delete_skill`):

```python
def _delete_skill(name: str, absorbed_into: Optional[str] = None):
    """Delete a skill.

    ``absorbed_into`` declares intent:
      - ``None`` / missing  → caller didn't declare (legacy / non-curator path);
        accepted for backward compat but logs a warning
      - ``""`` (empty)      → explicit "truly pruned, no forwarding target".
      - ``"<skill-name>"``  → content was absorbed into that umbrella; the
        target must exist on disk. Validated here so the model can't claim an
        umbrella that doesn't exist.
    """
    ...
    if absorbed_into is not None and target_name:
        target = _find_skill(target_name)
        if not target:
            return {"success": False, "error": "absorbed_into=... does not exist"}
```

Tres modos de eliminación: pruning real, fusión declarada, legacy. **La fusión exige que el target exista** — chequeo defensivo contra "fui absorbido por una umbrella inventada".

La evolución dentro de una skill se hace por **patch con fuzzy match** (`skill_manager_tool.py:463-554`, `_patch_skill`). El curator agrega contenido patcheando, no reescribiendo.

El prompt del background-review prefiere editar antes de crear (`agent/background_review.py:71-100`):

> "Preference order — prefer the earliest action that fits, but do pick one when a signal above fired:
>   1. UPDATE A CURRENTLY-LOADED SKILL...
>   2. UPDATE AN EXISTING UMBRELLA (via skills_list + skill_view)...
>   3. ADD A SUPPORT FILE under an existing umbrella...
>   4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing..."

Esto es **append/merge** vs override: si hay umbrella plausible, se le agrega como patch o como `references/<topic>.md` subfile. Solo se crea skill nueva cuando no encaja.

#### Q4 — Lifecycle

**State machine determinista**: `active → stale → archived → (restorable)`. `tools/skill_usage.py:18-23`:

```python
"""Lifecycle states:
    active    -> default
    stale     -> unused > stale_after_days (config)
    archived  -> unused > archive_after_days (config); moved to .archive/
    pinned    -> opt-out from auto transitions (boolean flag, orthogonal to state)
"""
```

Transición automática en `agent/curator.py:256-296` (`apply_automatic_transitions`):

```python
def apply_automatic_transitions(now=None) -> Dict[str, int]:
    if now is None: now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=get_stale_after_days())
    archive_cutoff = now - timedelta(days=get_archive_after_days())
    ...
    for row in _u.agent_created_report():
        if row.get("pinned"): continue
        anchor = last_activity or created_at or now

        if anchor <= archive_cutoff and current != STATE_ARCHIVED:
            ok, _msg = _u.archive_skill(name)
        elif anchor <= stale_cutoff and current == STATE_ACTIVE:
            _u.set_state(name, STATE_STALE)
        elif anchor > stale_cutoff and current == STATE_STALE:
            # Skill got used again after being marked stale — reactivate.
            _u.set_state(name, STATE_ACTIVE)
```

Tres puntos importantes:

1. **`pinned`** flag — opt-out manual de la transición automática. Análogo al `_MEMORY_AUTHOR=user_authored` de durin: protege user-curated.
2. **Reactivación es automática**: si una skill `stale` se usa de nuevo, vuelve a `active`. Ver `set_state` (`skill_usage.py:444-460`).
3. **Archive = mover directorio**: `archive_skill` (`skill_usage.py:482-518`) mueve `~/.hermes/skills/<name>` a `~/.hermes/skills/.archive/<name>`. Reversible vía `restore_skill` (`skill_usage.py:521-...`). No es soft-delete con flag — es físico, en disco.

`_u.agent_created_report()` excluye bundled + hub skills, por provenance (`skill_usage.py:290-294`). Análogo a `_MEMORY_AUTHOR` distinguiendo `user_authored` vs `agent_created`.

**Telemetría per-skill**: `view_count`, `use_count`, `patch_count`, `last_activity_at` viven en sidecar `~/.hermes/skills/.usage.json` (`skill_usage.py:62-63`). El curator decide transiciones en base a `last_activity_at` (`curator.py:275-278`):

```python
last_activity = _parse_iso(row.get("last_activity_at"))
# If never active, treat created_at as the anchor so new skills don't
# immediately archive themselves.
anchor = last_activity or _parse_iso(row.get("created_at")) or now
```

#### Q5 — Retrieval entity-aware

`skills_list` enumera; `skill_view` lee una skill por nombre. **Búsqueda por nombre/prefix**, no por contenido (excepto si el modelo grep-ea el dir). El curator opera al nivel de lista de skills, no de búsqueda semántica.

#### Q6 — Costo

- Transiciones automáticas: deterministic, **sin LLM**. Corre en background cuando `should_run_now(now)` (`curator.py:199-...`) — heurística por intervalo y idle hours.
- Background review (`background_review.py`): **sí usa LLM** (forked agent). Pero el LLM no clasifica skills — decide qué crear/patchear.

Costo amortizado: el curator state machine es free; el review forkado es costoso (un turn entero por sesión) pero opt-in y con guards (`is_paused`, `agent_context not in {cron, flush}`).

#### Q7 — Lección directa para durin

- **Adoptar**: state machine `active → stale → archived` con thresholds en días, basada en `last_activity_at`. Determinista, sin LLM, recuperable. Aplicable directo a `entities/<type>/<value>.md`.
- **Adoptar**: el flag `pinned` para opt-out manual. durin ya tiene `_MEMORY_AUTHOR=user_authored` — extender al concept: si el usuario tocó la página de entidad, marcarla pinned.
- **Adoptar**: `absorbed_into` como contrato de fusión. Cuando el dream unifica `Durin` + `durin` + `durin-agent` en `project:durin`, la entry/página descartada graba `absorbed_into: project:durin`, el target debe existir.
- **Adoptar**: archive físico (mover a `.archive/`) vs soft-delete. Más auditable, más recuperable.
- **Adoptar**: telemetría sidecar (`.usage.json` style) para no contaminar el frontmatter de la entidad con counters operacionales.
- **Cuidado**: el patrón "agrupar por prefix cluster" del curator de Hermes no escala bien a entidades semánticas — los prefixes (`hermes-config-*`) son convención de naming, no semántica intrínseca. Para `person:marcelo` vs `person:sergio`, el "cluster" es el `type=person` — eso es exactamente lo que doc 16 propone con `entities/<type>/`.

---

### §2.7 — Hermes / agent/background_review.py

**Localización**: `/Users/marcelo/git_personal/hermes-agent/agent/background_review.py`

#### Q1-Q5 — No clasifica entidades

Background review **NO clasifica ni extrae entidades**. Es un fork del agente que ejecuta uno de dos prompts (`_MEMORY_REVIEW_PROMPT`, `_SKILL_REVIEW_PROMPT`) sobre el snapshot de la conversación. Decide:

- Si guardar a memoria (vía la `memory` tool built-in del agente)
- Si crear/patchear/borrar skills (vía `skill_manage`)

Es el **productor de skills**, no clasificador. La "clasificación" emerge implícita: el modelo escoge si añadir o patchear bajo qué `name/category`.

#### Q6 — Costo

LLM fork. **Inline post-turn**, no offline. Es decisión del agente principal lanzarlo o no (heurística en `AIAgent.run_conversation`).

#### Q7 — Lección directa para durin

- **Confirma** que la inteligencia de "qué entidad es" vive en el **productor** (consolidator + dream en durin, background-review + skill-extractor en Hermes). El schema de la entidad no se infiere; se emite.
- **Cuidado**: el approach "fork agent con tool whitelist" es complejo. durin con el consolidator + memory_store ya tiene la rueda más simple.

---

### §2.8 — OpenClaw / memory-lancedb

**Localización**: `/Users/marcelo/git_personal/openclaw/extensions/memory-lancedb/`
- `config.ts` (enum `MEMORY_CATEGORIES`)
- `index.ts` (store/search/auto-capture)

#### Q1 — Modelo de identidad

**No hay identidad de entidad nombrada**. Las memorias son rows en LanceDB con UUID generado (`index.ts:254`):

```ts
const fullEntry: MemoryEntry = {
  ...entry,
  id: randomUUID(),
  createdAt: Date.now(),
};
```

Sin nombre, sin alias, sin slug. Si el modelo guarda dos memorias sobre "Marcelo", quedan como dos rows independientes — la "identidad" Marcelo no existe en el schema.

#### Q2 — Granularidad / tipos

**Enum cerrado de 5 valores para item-category**. `config.ts:23-24`:

```ts
export const MEMORY_CATEGORIES = ["preference", "fact", "decision", "entity", "other"] as const;
export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];
```

Notar: `entity` es uno de los valores — significa "esta memoria es sobre una entidad" — no es un tipo de entidad. Ambigüedad ya marcada en doc 14.

Auto-asignación con regex multilingüe (`index.ts:610-627`, `detectCategory`):

```ts
export function detectCategory(text: string): MemoryCategory {
  const lower = normalizeLowercaseStringOrEmpty(text);
  if (/prefer|like|love|hate|want|喜欢|偏好.../i.test(lower)) return "preference";
  if (/rozhodli|decided|will use|决定|これから/i.test(lower)) return "decision";
  if (/\+\d{10,}|@[\w.-]+\.\w+|is called/i.test(lower)) return "entity";
  if (/is|are|has|have|je/i.test(lower)) return "fact";
  return "other";
}
```

#### Q3 — Conflictos

**Ninguno**. Las memorias se acumulan; el dream del memory-core puntúa pero no detecta contradicciones por entidad.

#### Q4 — Lifecycle

**Borrado individual por UUID** (`index.ts:313-321`). No automático.

El dream (separado, en memory-core) puntúa por 6 ejes (frequency, relevance, diversity, recency, consolidation, conceptual) — pero esa promoción opera sobre **memorias**, no entidades.

#### Q5 — Retrieval entity-aware

**No**. Vector search puro sobre el texto. `category` se imprime como decoración (`index.ts:731`):

```ts
`${i + 1}. [${r.entry.category}] ${r.entry.text} (${(r.score * 100).toFixed(0)}%)`
```

No filtra ranking. No filtra por entidad nombrada (no hay entidad nombrada).

#### Q6 — Costo

- Auto-capture: regex `detectCategory` por message. Trivial.
- Inline embedding cost para vector search.

#### Q7 — Lección directa para durin

- **Descartar**: enum cerrado 5-valores donde `entity` es uno-de-cinco. Mezcla "categoría del item" con "tipo de entidad" — el peor de los dos mundos.
- **Descartar**: `detectCategory` con regex multilingüe — frágil, sin escala más allá de "I prefer / decided / +1234567890".
- **Confirma doc 14**: tipar el item-category con LanceDB-style no es lo mismo que tipar la entidad nombrada. Son ejes ortogonales.

---

### §2.9 — OpenClaw / memory-core (concept-vocabulary)

**Localización**: `/Users/marcelo/git_personal/openclaw/extensions/memory-core/src/concept-vocabulary.ts`

#### Q1-Q5 — Tags planas, sin entidades

`deriveConceptTags` (`concept-vocabulary.ts:399-424`):

```ts
export function deriveConceptTags(params: {
  path: string;
  snippet: string;
  limit?: number;
}): string[] {
  const source = `${path.basename(params.path)} ${params.snippet}`;
  const limit = ...;
  const tags: string[] = [];
  for (const rawToken of [
    ...collectGlossaryMatches(source),
    ...collectCompoundTokens(source),
    ...collectSegmentTokens(source),
  ]) {
    pushNormalizedTag(tags, rawToken, limit);
    if (tags.length >= limit) break;
  }
  return tags;
}
```

Salida: `string[]` planos. `MAX_CONCEPT_TAGS = 8` (`concept-vocabulary.ts:4`).

Stop-words filtradas en múltiples idiomas (latin + CJK). Sin tipo, sin entidad. Esos tokens van a clustering en el dream.

#### Q6 — Costo

Léxico determinista. Sin LLM.

#### Q7 — Lección directa para durin

- **Confirma doc 14**: este patrón = los `topics: list[str]` planos de durin. **Tags lexicales no se tipan**. Se tipan los nombres propios (`entities`).
- No mezclar los dos ejes.

---

### §2.10 — OpenClaw / memory-wiki

**Este es el sistema más cercano al modelo entity-centric propuesto en doc 16. Le dedico análisis detallado.**

**Localización**: `/Users/marcelo/git_personal/openclaw/extensions/memory-wiki/`
- `src/markdown.ts` — parser, kinds, frontmatter
- `src/apply.ts` — mutaciones de página
- `src/compile.ts` — compile + dashboards
- `src/claim-health.ts` — freshness + contradictions
- `src/memory-palace.ts` — vista agregada
- `src/query.ts` — search backend
- `README.md` — overview

#### Q1 — Modelo de identidad

**Identidad por `id` en frontmatter + `slug` del filename + `aliases`**. Estructura de página:

```yaml
---
id: entity.marcelo
title: Marcelo
entityType: person
canonicalId: entity.marcelo
aliases: [Marcelo M, marcelo, mmarmol]
sourceIds: [source.session-2026-05-23]
claims: [...]
relationships: [...]
personCard:
  canonicalId: entity.marcelo
  handles: [...]
  socials: [...]
  emails: [mmarmol@example.com]
  ...
confidence: 0.85
privacyTier: private
updatedAt: 2026-05-23T...
---
```

Construcción de filename con slug + hash truncation (`markdown.ts:145-163`):

```ts
export function slugifyWikiSegment(raw: string): string {
  const slug = normalizeLowercaseStringOrEmpty(raw)
    .replace(/[^\p{L}\p{N}\p{M}]+/gu, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!slug) return "page";
  return capWikiValueWithHash(slug, MAX_WIKI_SEGMENT_BYTES, "page");
}

export function createWikiPageFilename(stem: string, extension = ".md"): string {
  ...
  return `${capWikiValueWithHash(stem, maxStemBytes, "page")}${normalizedExtension}`;
}
```

Resolución por search incluye **aliases**, `canonicalId` y campos derivados (`query.ts:337-389`, `buildPageSearchText`):

```ts
function buildPageSearchText(page: QueryableWikiPage): string {
  return [
    page.title,
    page.relativePath,
    page.id ?? "",
    page.pageType ?? "",
    page.entityType ?? "",
    page.canonicalId ?? "",
    page.aliases.join(" "),
    ...
    page.personCard?.canonicalId ?? "",
    page.personCard?.handles.join(" ") ?? "",
    page.personCard?.socials.join(" ") ?? "",
    page.personCard?.emails.join(" ") ?? "",
    ...
  ].filter(Boolean).join("\n");
}
```

O sea: la **búsqueda** matchea contra aliases. La **identidad canónica** es `canonicalId` + filename slug. Cuando dos páginas refieren la misma entidad, una contiene `canonicalId` apuntando a la otra — esto unifica sin borrar el alias-side.

#### Q2 — Granularidad / tipos

**Dos niveles de tipo**:

**Nivel 1 — `kind`** (5 valores enum cerrado, `markdown.ts:10`):

```ts
const WIKI_PAGE_KINDS = ["entity", "concept", "source", "synthesis", "report"] as const;
```

Determinado por directorio (`markdown.ts:415-433`, `inferWikiPageKind`):

```ts
export function inferWikiPageKind(relativePath: string): WikiPageKind | null {
  const normalized = relativePath.split(path.sep).join("/");
  if (normalized.startsWith("entities/")) return "entity";
  if (normalized.startsWith("concepts/")) return "concept";
  if (normalized.startsWith("sources/")) return "source";
  if (normalized.startsWith("syntheses/")) return "synthesis";
  if (normalized.startsWith("reports/")) return "report";
  return null;
}
```

**Nivel 2 — `entityType`** (string libre dentro de `kind=entity`). Aparece en `WikiPageSummary` (`markdown.ts:78`):

```ts
entityType?: string;
```

Pero **no hay enum cerrado para `entityType`**. Es texto libre — `person`, `tool`, `service`, lo que el productor escriba. Lo único que el sistema garantiza es persistencia del valor y búsqueda contra él.

Cardinalidades implícitas del README:

> "The plugin initializes a vault like this:
> <vault>/
>   entities/
>   concepts/
>   syntheses/
>   sources/
>   reports/"

5 dirs top-level. `entities/` puede contener subdirs por `entityType` o flat (no es enforced por el plugin — la convención queda al usuario).

#### Q3 — Conflictos / evolución

**Sistema rico de claim-level status + contradiction clusters**. `claim-health.ts:9`:

```ts
const CONTESTED_CLAIM_STATUSES = new Set(["contested", "contradicted", "refuted", "superseded"]);
```

Cada **claim** dentro de una página tiene `status ∈ {supported | contested | contradicted | refuted | superseded}` (default `supported`, `claim-health.ts:121-123`):

```ts
export function normalizeClaimStatus(status?: string): string {
  return normalizeLowercaseStringOrEmpty(status) || "supported";
}

export function isClaimContestedStatus(status?: string): boolean {
  return CONTESTED_CLAIM_STATUSES.has(normalizeClaimStatus(status));
}
```

Estructura de claim (`markdown.ts:33-40`):

```ts
export type WikiClaim = {
  id?: string;
  text: string;
  status?: string;
  confidence?: number;
  evidence: WikiClaimEvidence[];
  updatedAt?: string;
};
```

Y `WikiClaimEvidence` (`markdown.ts:21-31`):

```ts
export type WikiClaimEvidence = {
  kind?: string;
  sourceId?: string;
  path?: string;
  lines?: string;
  weight?: number;
  confidence?: number;
  privacyTier?: string;
  note?: string;
  updatedAt?: string;
};
```

**Cluster building** por `claim.id` (`claim-health.ts:174-210`):

```ts
export function buildClaimContradictionClusters(params): WikiClaimContradictionCluster[] {
  const claimHealth = collectWikiClaimHealth(params.pages, params.now);
  const byId = new Map<string, WikiClaimHealth[]>();
  for (const claim of claimHealth) {
    if (!claim.claimId) continue;
    const current = byId.get(claim.claimId) ?? [];
    current.push(claim);
    byId.set(claim.claimId, current);
  }

  return [...byId.entries()]
    .flatMap(([claimId, entries]) => {
      if (entries.length < 2) return [];
      const distinctTexts = new Set(entries.map((entry) => normalizeClaimTextKey(entry.text)));
      const distinctStatuses = new Set(entries.map((entry) => entry.status));
      if (distinctTexts.size < 2 && distinctStatuses.size < 2) return [];
      return [{ key: claimId, label: claimId, entries: ... }];
    });
}
```

O sea: dos páginas pueden tener un claim con **el mismo `claim.id`** pero distinto `text` o `status` — eso es lo que detecta el cluster de contradicción.

**Resolución**: no automática. Se **reporta** en `reports/contradictions.md` (vía dashboard, `compile.ts:64-...`). La resolución la hace el usuario o el modelo via `wiki_apply` (mutación de metadata, `apply.ts:270-296`). Equivalente al `contradict()` de Holographic, pero más rico (status escala, evidencia explícita).

`updatedAt` también arrastra `evidence.updatedAt` por claim — el último timestamp gana en freshness (`claim-health.ts:138-143`):

```ts
const latestTimestamp = resolveLatestTimestamp([
  params.claim.updatedAt,
  params.page.updatedAt,
  ...params.claim.evidence.map((evidence) => evidence.updatedAt),
]);
```

#### Q4 — Lifecycle

**Freshness levels deterministas con thresholds en días**. `claim-health.ts:6-7`:

```ts
export const WIKI_AGING_DAYS = 30;
const WIKI_STALE_DAYS = 90;
```

Función (`claim-health.ts:73-105`, `buildFreshnessFromTimestamp`):

```ts
function buildFreshnessFromTimestamp(params): WikiFreshness {
  const now = params.now ?? new Date();
  const timestampMs = parseTimestamp(params.timestamp);
  if (timestampMs === null || !params.timestamp) {
    return { level: "unknown", reason: "missing updatedAt" };
  }
  const daysSinceTouch = clampDaysSinceTouch(...);
  if (daysSinceTouch >= WIKI_STALE_DAYS) {
    return { level: "stale", ..., daysSinceTouch };
  }
  if (daysSinceTouch >= WIKI_AGING_DAYS) {
    return { level: "aging", ..., daysSinceTouch };
  }
  return { level: "fresh", ..., daysSinceTouch };
}
```

4 niveles: `fresh | aging | stale | unknown`. Por **claim** Y por **página entera** (`assessPageFreshness`).

**No hay archive automático**. Stale pages aparecen en `reports/stale.md` (un dashboard). El usuario/modelo decide qué hacer (mutar updatedAt = refresh; borrar; ignorar).

Eso es diferente del lifecycle de Hermes skills (que sí mueve a `.archive/`). Memory-wiki **no toca el disco automáticamente**.

#### Q5 — Retrieval entity-aware

**Sí, fuertemente**. Tools:

- `wiki_search`: busca con `buildPageSearchText` que incluye aliases, canonicalId, claims, evidence.
- `wiki_get(id|path)`: lee página por identidad.

Compile-time genera **backlinks** y `## Related` blocks deterministas (`README.md:96-100`):

> "When `render.createBacklinks` is enabled, compile adds deterministic `## Related` blocks to pages. Those blocks list source pages, pages that reference the current page, and nearby pages that share the same source ids."

Y **dashboards** para queries entity-aware sin LLM:

- `reports/open-questions.md`
- `reports/contradictions.md`
- `reports/low-confidence.md`
- `reports/stale-pages.md`

Esos son **vistas materializadas** del grafo de entidades.

#### Q6 — Costo

- **Lectura/búsqueda**: ripgrep / FTS-style sobre archivos. Sin LLM.
- **Compile** (`compile.ts`): determinista, sin LLM. Reconstruye backlinks, dashboards, agent-digest. Se ejecuta on demand (`openclaw wiki compile`) o auto (`ingest.autoCompile`).
- **Ingest** (`ingest.ts`): puede usar LLM si configurado, pero el plugin acepta input pre-estructurado vía `wiki_apply`.
- **Productor canónico**: el modelo a través de `wiki_apply` (sin LLM interno del plugin). El plugin almacena lo que el productor declare.

Costo amortizado **bajo**: el plugin es deterministic indexing + reporting. La inteligencia vive arriba (el agente que llama a `wiki_apply`).

#### Q7 — Lección directa para durin

Este sistema es **el más útil de la muestra como referencia para el doc 16**. Adoptar:

- **Estructura de directorios**: `entities/`, `concepts/`, `syntheses/`, `sources/`, `reports/`. Mapea casi 1-a-1 con el modelo de durin: `entities/<type>/` + `episodic/` (sources equivalente) + el dream produciría `syntheses/` y `reports/`.
- **Doble tipo**: `kind` (cerrado, por directorio) + `entityType` (libre, en frontmatter). Eso resuelve el debate de doc 14 (vocabulario abierto) sin renunciar a estructura. durin lo puede aplicar: `<type>` (directorio, cerrado) determina la página; el frontmatter puede tener un sub-tipo libre.
- **Claim-status enum**: `supported | contested | contradicted | refuted | superseded`. Aplicable directo. Resuelve Q3 a nivel sub-entidad: cada claim dentro de una página tiene su propio status; no se sobreescribe — se marca.
- **`canonicalId` + `aliases`**: resuelve Q2 (unificación). Si `Marcelo` y `marcelo` aparecen, hay dos paths posibles: (a) crear ambas páginas con la "no-canónica" teniendo `canonicalId: entity.marcelo`; (b) merge al detectar — solo una página, ambas formas en `aliases`. Memory-wiki soporta ambas. La búsqueda matchea aliases.
- **Freshness por timestamp con thresholds** (`WIKI_AGING_DAYS=30`, `WIKI_STALE_DAYS=90`): determinista, sin LLM. Aplicable directo a durin: `entities/person/marcelo.md` envejece según `updatedAt`.
- **Reports as dashboards**: `reports/contradictions.md`, `reports/stale.md` son vistas materializadas. Equivalente para durin: el dream genera `reports/` para que el usuario revise sin tener que correr LLM cada vez.
- **Backlinks deterministas en `## Related`** (managed blocks): el compile inserta secciones marcadas con `<!-- ... -->` que el reader puede ignorar pero que el sistema mantiene. Aplicable a durin: agregar a cada página de entidad un bloque "## Related episodic entries" auto-mantenido.
- **`personCard` sub-estructura**: la entidad `person` tiene un schema rico (handles, socials, emails, timezone, lane, askFor, avoidAskingFor). Eso es **especialización por tipo** — no todos los tipos necesitan los mismos campos. Para durin, `person:` puede tener `personCard`, `project:` puede tener `projectCard` (deps, milestones), etc. Cada tipo define su schema secundario.
- **Evidence con `sourceId`, `path`, `lines`, `weight`, `confidence`**: cada claim de la entidad apunta a su fuente. Aplicable a durin: cada item de la página `entities/<type>/<name>.md` cita la `episodic/<id>.md` de origen.

**No adoptar**:

- `privacyTier` — durin no necesita PII tiers (por ahora).
- `obsidian` integration — out of scope.

---

### §2.11 — OpenClaw / active-memory

**Localización**: `/Users/marcelo/git_personal/openclaw/extensions/active-memory/index.ts`

#### Q1-Q5 — Sub-agent de filtrado, no maneja entidades

Plugin completo (~3000 líneas) pero confirmé con grep que no menciona `entity` ni `entities`. Es un mecanismo de:

- Sub-agente que pre-procesa el turno
- Filtra qué memorias del corpus son relevantes
- Inyecta solo las relevantes

Opera sobre **memorias arbitrarias** sin estructura interna. No es entity-aware.

#### Q7 — Lección directa para durin

- Sin información sobre entidades. **Skip**.
- Aclaración: durin ya tiene mecanismos parecidos (hot_layer, prefetch en consolidador), no necesita copiar este patrón.

---

### §2.12 — OpenClaude

**Localización**: `/Users/marcelo/git_personal/openclaude/src/memdir/`, `/Users/marcelo/git_personal/openclaude/src/services/extractMemories/`

#### Q1 — Modelo de identidad

**Identidad = filename del .md**. Memorias son archivos sueltos en `~/.claude/projects/<path>/memory/<name>.md`. Frontmatter pide `name` y `description` (`memoryTypes.ts:262-265`):

```ts
'```markdown',
'---',
'name: {{memory name}}',
'description: {{...}}',
`type: {{${MEMORY_TYPES.join(', ')}}}`,
'---',
```

No hay aliases, no hay canonicalId, no hay tabla relacional. Si el modelo guarda 2 memorias sobre Marcelo, son 2 archivos.

#### Q2 — Granularidad / tipos

**Enum cerrado de 4 valores para item-type** (`memoryTypes.ts:14-19`):

```ts
export const MEMORY_TYPES = [
  'user',
  'feedback',
  'project',
  'reference',
] as const
```

Notar diferencia con LanceDB/Holographic: estos 4 valores son **roles del fact** (user info / feedback to AI / project state / external reference), no categorías de entidad.

Tipado tolerante en lectura (`memoryTypes.ts:28-31`):

```ts
export function parseMemoryType(raw: unknown): MemoryType | undefined {
  if (typeof raw !== 'string') return undefined
  return MEMORY_TYPES.find(t => t === raw)
}
```

Si frontmatter no tiene `type` o es desconocido → `undefined`, no error. Análogo al "lenient en lectura" de doc 14.

#### Q3 — Conflictos

**Sin reconciliación automática**. El doc filosófico (`memoryTypes.ts:60`):

> "Before saving a private feedback memory, check that it doesn't contradict a team feedback memory — if it does, either don't save it or note the override explicitly."

Eso es **prompt-level guidance al modelo**, no infraestructura. El modelo lo decide.

`MEMORY_DRIFT_CAVEAT` (`memoryTypes.ts:201-202`):

> "Memory records can become stale over time. ... If a recalled memory conflicts with current information, trust what you observe now — and update or remove the stale memory rather than acting on it."

Mismo patrón: instrucción al modelo, no policy automatizada.

#### Q4 — Lifecycle

**Anotación textual de edad, sin archivado**. `memoryAge.ts:33-42`:

```ts
export function memoryFreshnessText(mtimeMs: number): string {
  const d = memoryAgeDays(mtimeMs)
  if (d <= 1) return ''
  return (
    `This memory is ${d} days old. ` +
    `Memories are point-in-time observations, not live state — ` +
    `claims about code behavior or file:line citations may be outdated. ` +
    `Verify against current code before asserting as fact.`
  )
}
```

Esto **inyecta el warning textualmente** al modelo cuando recupera una memoria vieja. No archiva, no borra. La decisión queda al modelo: "es vieja, ¿confío?".

Comentario revelador (`memoryAge.ts:11-13`):

> "Human-readable age string. Models are poor at date arithmetic — a raw ISO timestamp doesn't trigger staleness reasoning the way '47 days ago' does."

Diseño deliberado: **el LLM es mejor reaccionando a "47 days ago" que parseando timestamps**.

#### Q5 — Retrieval entity-aware

Memorias son archivos en disco. Búsqueda es por contenido (grep/glob); no hay grafo de entidades. El modelo lee y decide.

#### Q6 — Costo

**Forked agent al final del loop** (cuando el modelo emite final response sin tool calls). Es un LLM call por sesión, costoso pero amortizado. `extractMemories.ts:1-15`:

> "It runs once at the end of each complete query loop (when the model produces a final response with no tool calls) via handleStopHooks in stopHooks.ts.
>
> Uses the forked agent pattern (runForkedAgent) — a perfect fork of the main conversation that shares the parent's prompt cache."

Análogo al background_review de Hermes pero solo para memoria, no para skills.

#### Q7 — Lección directa para durin

- **Adoptar**: 4-tipo item-classification (`user|feedback|project|reference`). **Es uno de los conjuntos más limpios y testeable** de los que vi. Pero observación importante: estos son tipos del **item**, no de la entidad. durin ya tiene `class ∈ {stable|episodic|corpus|pending}` cubriendo el rol equivalente.
- **Adoptar**: el patrón "freshness as warning text, not auto-archive". Anti-pattern recurrente: archivar agresivamente destruye contexto que el modelo podría recuperar. Mejor: marcar viejo, dejarlo, anotar el riesgo. Este patrón **complementa** el archive de Hermes — usar ambos: archive después de un threshold mayor; warning text antes.
- **Adoptar**: prompt-level guidance para Q3 ("check team-vs-private antes de override; trust what you observe now if conflict"). Reglas en el prompt son baratas y a veces suficientes — no toda contradicción requiere infraestructura de reconciliation.
- **No adoptar**: archivos sueltos sin grafo. OpenClaude funciona porque los corpus de memoria de un usuario individual son pequeños. durin con dream consolidando muchas sesiones va a necesitar grafo.

---

### §2.13 — OpenHands

**Localización**: `/Users/marcelo/git_personal/openhands/skills/agent_memory.md`

Memoria de OpenHands es **un solo archivo markdown** (`.openhands/microagents/repo.md` por repositorio), curado interactivamente con el usuario. Sin entidades, sin tipado, sin grafo.

#### Q7 — Lección directa para durin

- **Confirmación negativa**: no todos los sistemas de "memoria" necesitan entidades. OpenHands funciona con un solo markdown bien curado por repo. Pero ese modelo es **incompatible** con lo que doc 16 plantea (memoria cross-session, persistencia automática, consolidación).
- Vale tenerlo en mente: a veces un único `MEMORY.md` por scope es suficiente. durin va por el camino más ambicioso a propósito.

---

### §2.14 — OpenCode, Pi

**OpenCode**: `packages/identity/` existe pero es identity management (auth/users), no memoria semántica. No relevante.

**Pi**: no encontré memoria con entidades.

Sin aportes.

---

## §3 — Tabla síntesis cross-sistema

| Sistema | Tipo de identidad | Vocabulario tipos entidad | Conflictos | Lifecycle | Inline/offline | Adopta? |
|---|---|---|---|---|---|---|
| Hermes Holographic | name (case-insens) + aliases CSV | Declarado (`entity_type`), **vacío en práctica** | Detectado vía HRR + entity overlap; no resuelto | Trust score + opt decay, no archive automático | Inline (regex) | `contradict()` señal, no estructura |
| Hermes Honcho | peer_id slug sanitizado | Solo `peer` (un tipo) | Self-heal por LLM en backend opaco | Sin lifecycle visible | Offline async + dialectic cadence | Concepto peer-card como entidad-page |
| Hermes OpenViking | Server-side | Tipa item con 5: pref/entity/event/case/pattern; entidades opacas | Server-side | Server-side | Server (LLM en backend) | Nada |
| Hermes Mem0 | Server-side | Metadata libre | Server-side | Server-side | Server | Patrón `infer=False` |
| Hermes Hindsight/Supermemory/ByteRover | Server-side | Sin tipos | Server-side | Server-side | Server | Nada |
| Hermes RetainDB | Server-side + scope tier | Tipa item con 6: factual/preference/goal/instruction/event/opinion | Server-side | Server-side | Server | Set de 6 valores |
| Hermes skill curator | name slug + category dir | Vocabulario libre (no enum) | Patch (append), `absorbed_into` (merge), prefer-update-over-create | **State machine determinista**: active→stale→archived; thresholds en días; `pinned` opt-out; restore | Background offline + on-demand | **Mucho**: lifecycle, pinned, absorbed_into, telemetry sidecar |
| Hermes background_review | n/a (productor) | n/a | n/a (productor, no clasificador) | n/a | Inline post-turn (fork LLM) | Confirma que tipado vive en productor |
| OpenClaw memory-lancedb | UUID generado (no nombre) | Enum 5: pref/fact/decision/entity/other; `entity` ambiguo | Sin reconciliación | Borrado individual; dream score multi-eje | Inline regex (`detectCategory`) | Nada — anti-patrón claro |
| OpenClaw memory-core (concept) | n/a (tags planos) | Sin tipos | n/a | n/a | Inline léxico | Confirma topics-no-se-tipan |
| OpenClaw memory-wiki | `id` + slug filename + `aliases` + `canonicalId` | **`kind` cerrado de 5: entity/concept/source/synthesis/report** + `entityType` libre | Claim-status: supported/contested/contradicted/refuted/superseded; cluster por id; reports/contradictions.md dashboard | **Freshness deterministic: fresh/aging/stale/unknown; AGING=30d, STALE=90d**; no auto-archive; reports/stale.md dashboard | Compile determinista, sin LLM; productor externo (`wiki_apply`) | **Casi todo**: dirs, dual-type, aliases, claim-status, freshness thresholds, evidence-with-source, related-blocks, personCard sub-schema |
| OpenClaw active-memory | n/a | n/a | n/a | n/a | n/a | Nada |
| OpenClaude | filename | Enum 4: user/feedback/project/reference (item-type) | Prompt-level guidance al modelo | Texto-warning por días, sin auto-archive | Forked LLM post-loop | "Stale warning as text" antiestructura |
| OpenHands | n/a (un solo archivo) | n/a | n/a | n/a | Interactive curation | Nada (modelo opuesto) |

**Compresión cross-sistema** — qué hace cada uno en cada Q:

| Pregunta | Respuestas observadas (count) |
|---|---|
| Q1 identidad | UUID generado (1), filename/slug (3), name+aliases (2), peer_id slug (1), id+slug+aliases+canonicalId (1), server-opaco (5) |
| Q2 vocabulario | Enum cerrado 4-6 (5), libre (2), 1-tipo (1), n/a (4), abierto+kind-cerrado (1) |
| Q3 conflictos | Sin reconciliación (5), detector sin resolución (1), claim-status explícito (1), `absorbed_into` patch-merge (1), prompt-guidance (1), server-side opaco (4) |
| Q4 lifecycle | Sin (5), trust score (1), state machine archive (1), warning text (1), freshness thresholds sin archive (1), server-side (4) |
| Q5 retrieval | No entity-aware (5), entity-aware via HRR (1), entity-aware via aliases+backlinks (1), peer-scoped (1), n/a (4) |
| Q6 costo | Server LLM (5), inline regex/léxico (4), inline + LLM forked (2), background determinista (1), compile determinista (1) |

---

## §4 — Patrones que aparecen ≥2 veces

Patrones convergentes (los más probables de ser correctos para durin):

### 4.1 — Tipar el item con enum cerrado pequeño (4-6 valores)

**Aparece en**: LanceDB (5), OpenViking-client (5), OpenClaude (4), RetainDB (6), Holographic-facts (4).

5 sistemas distintos tipan **el item de memoria** con un enum chico. Los conjuntos NO se solapan exactamente (LanceDB tiene `preference|fact|decision|entity|other`; OpenClaude tiene `user|feedback|project|reference`), pero el **shape** converge.

**Implicación para durin**: ya tiene `class ∈ {stable|episodic|corpus|pending}` (4 valores). Está alineado. **Tipar el item NO es lo mismo que tipar la entidad nombrada**.

### 4.2 — Aliases para unificación (Q2)

**Aparece en**: Holographic (aliases TEXT CSV), memory-wiki (aliases array + canonicalId).

Ambos usan aliases para que `Marcelo` y `marcelo` apunten a la misma entidad. Implementaciones distintas — Holographic CSV string, memory-wiki array YAML — pero el concepto convergente.

**Implicación**: durin debe tener `aliases` en el frontmatter de la página de entidad. Probablemente como YAML array (más limpio).

### 4.3 — La identidad es slug del filename + id en frontmatter

**Aparece en**: memory-wiki (`id: entity.marcelo`, filename `entities/marcelo.md`), Hermes skill (filename slug + frontmatter `name`), OpenClaude (filename + frontmatter `name`).

3 sistemas. La identidad **canónica** es el slug del filename; el frontmatter `id`/`name` actúa como override redundante (para auditabilidad).

**Implicación**: para `entities/person/marcelo.md`, el slug `marcelo` ES la identidad. El frontmatter `id: person.marcelo` o `name: marcelo` agrega redundancia explícita.

### 4.4 — Productor canónico es el LLM, no clasificador determinista

**Aparece en**: Hermes background_review (forked LLM), OpenClaude extractMemories (forked LLM), Mem0 (server LLM `infer=True`), Honcho (dialectic LLM), OpenViking (server LLM), memory-wiki (`wiki_apply` desde productor externo, plugin no clasifica).

6 sistemas convergen en que la **clasificación** de qué guardar, bajo qué tipo, con qué entidades vive en el LLM. Los sistemas con regex (Holographic, LanceDB `detectCategory`) son la excepción y se notan como heurísticas frágiles.

**Implicación**: durin va en el camino correcto delegando al consolidator/dream. Confirma doc 14.

### 4.5 — Lifecycle por timestamp con thresholds en días

**Aparece en**: Hermes skill curator (`STATE_STALE`, `STATE_ARCHIVED` con `stale_after_days`, `archive_after_days`), memory-wiki (`WIKI_AGING_DAYS=30`, `WIKI_STALE_DAYS=90`), OpenClaude (`memoryAge` con texto progresivo).

3 sistemas. Thresholds en días, basados en `last_activity_at` o `updatedAt` o `mtime`. **Determinista, sin LLM.**

Variantes:
- Hermes: muta state + mueve a `.archive/`.
- Memory-wiki: solo cambia `level` derivado para reports; no muta disco.
- OpenClaude: solo agrega texto warning al output del read.

**Implicación**: durin debe escoger entre los tres modos. El más rico es "warning text para todo + archive físico solo cuando se cruza threshold alto". Combinar memory-wiki + Hermes + OpenClaude.

### 4.6 — Sub-agentes/forks para extracción

**Aparece en**: Hermes background_review (forked AIAgent), OpenClaude extractMemories (forked agent).

2 sistemas. Ambos: post-turn, daemon-thread, comparten prompt cache. **No bloquean conversación principal**.

**Implicación**: si durin quiere extracción rica (no regex), el patrón es fork con tool whitelist (`memory_store`, etc.). Pero durin ya tiene consolidator inline — ese rol ya está cubierto.

### 4.7 — Opt-out manual de transiciones automáticas

**Aparece en**: Hermes skill curator (`pinned=True`), implícito en OpenClaude (memorias `user`-type son intocables).

2 sistemas. Cualquier lifecycle automático tiene una vía de bypass para user-curated content.

**Implicación**: durin ya tiene `_MEMORY_AUTHOR=user_authored` ContextVar (sobreviviente de propuesta C según doc 16). Extender a entidades: una página `entities/person/marcelo.md` con `pinned: true` o `_MEMORY_AUTHOR=user_authored` queda fuera de archivado automático.

### 4.8 — Telemetry sidecar separada del contenido user-facing

**Aparece en**: Hermes skill (`~/.hermes/skills/.usage.json`), memory-wiki (`.openclaw-wiki/cache/agent-digest.json`).

2 sistemas mantienen contadores y derivados en archivos sidecar bajo dot-prefixed dirs, no contaminan el frontmatter user-facing.

**Implicación**: para durin, si las páginas de entidad llevan `last_referenced_at`, `reference_count`, eso debería ir en `.usage.json` o similar — no en el frontmatter de `entities/person/marcelo.md`.

### 4.9 — Patches/append over rewrite

**Aparece en**: Hermes skill manager (`_patch_skill` fuzzy match), memory-wiki (`apply.ts` añade claims a array existente sin reemplazar).

2 sistemas. Cuando una entidad evoluciona, el patrón es **agregar** evidencia/claims, no reescribir la página entera. La página acumula, no se sobrescribe.

**Implicación**: el dream de durin debe usar patches/inserts dentro de las páginas, no reescribir desde cero. La página acumula claims con `valid_from` y `status`.

### 4.10 — Evidence linking back to source

**Aparece en**: memory-wiki (`WikiClaimEvidence` con `sourceId`, `path`, `lines`), Holographic (fact_entities table linking facts to entities).

2 sistemas mantienen un grafo bidireccional: la entidad apunta a sus fuentes; las fuentes pueden enumerarse por entidad. Sin esto la "página viva" se vuelve folklore — no hay forma de auditar de dónde vino una afirmación.

**Implicación**: cada claim/observación en `entities/person/marcelo.md` debe llevar `source: episodic/<id>.md` para auditar.

---

## §5 — Lo que ningún sistema resuelve bien

Áreas donde la evidencia open-source es débil o fragmentada — durin va a tener que innovar o decidir entre opciones imperfectas:

### 5.1 — Vocabulario cerrado vs abierto para tipo-de-entidad

**Estado del arte**: ningún sistema clonado tiene un enum cerrado bien justificado para tipo-de-entidad. Memory-wiki tiene `kind` cerrado de 5 (entity/concept/source/synthesis/report) pero esos son **kinds de página** — `entity` es uno solo. Dentro de `kind=entity`, el `entityType` es **libre**.

Holographic declaró `entity_type` y no lo llenó. Los demás no tipan.

**Lo que doc 16 propone** (10 tipos divididos consolidables/referenciables) **no tiene precedente directo en código open-source**. Es una decisión sin warm precedent. Habrá que validarla contra paper/blogs (Cognee, Graphiti, A-Mem).

### 5.2 — Unificación cross-type

**Estado del arte**: si `Marcelo` aparece como `person:marcelo` y también, por error, como `topic:marcelo` o `tool:marcelo`, ningún sistema clonado lo resuelve. Memory-wiki tendría dos páginas distintas, una en `entities/marcelo.md` y otra... bueno, también en `entities/`. Pero si fuesen distintos `kind`, viviría en dirs distintos.

Hermes Holographic tiene aliases dentro de `entities` table pero sin diferenciar tipo. Si tipara, no está claro cómo cross-type aliases funcionarían.

**Implicación**: durin debe decidir si "un mismo string puede ser dos entidades distintas" (`person:marcelo` ≠ `topic:marcelo`). Probablemente sí, pero entonces unification cross-type necesita ser explícita: `equivalentTo: topic:marcelo` o similar. Sin precedente claro.

### 5.3 — Cuándo el dream decide crear `entities/<type>/<value>.md` vs solo agregar al log episódico

**Estado del arte**: memory-wiki delega a un productor externo (no plugin) la decisión. Hermes skill curator solo crea umbrella **cuando hay 2+ skills consolidables** (`agent/curator.py:362-365` habla de prefix clusters). OpenClaude crea memoria cuando el modelo decide (sin regla de "N observaciones, créame umbrella").

Ningún sistema tiene "umbral observado X observaciones sobre la misma entidad → crear página". Memory-wiki lo deja al productor; el plugin acepta lo que reciba.

**Implicación**: durin debe definir esta heurística — la propuesta natural es "si ≥N entries del corpus episódico mencionan la entidad → consolidar página". El valor de N habrá que validarlo empíricamente. Sin precedente.

### 5.4 — Garbage collection: borrado vs archive vs warning

**Estado del arte**: tres approaches (Hermes archive físico, memory-wiki freshness sin archive, OpenClaude warning text). Cada uno tiene un trade-off:

- Archive físico: caro recuperar; visible en disk listing; útil para skills (numerosos).
- Freshness sin archive: páginas viejas consumen tokens si se leen; señal visible solo en dashboards.
- Warning text: páginas siempre se leen; el modelo aprende a tratarlas con escepticismo.

durin necesitará combinar (probablemente los tres en niveles distintos por threshold), pero **ningún sistema combina los tres**. Cada uno eligió uno.

### 5.5 — Resolución de contradicciones por evidencia temporal (Q3 con valid_from/invalid_at)

**Estado del arte**: memory-wiki tiene claim-status `superseded` pero **el sucesor no es declarativo** — un claim "Marcelo prefiere pytest" con status=superseded no tiene puntero explícito a "Marcelo prefiere unittest". Hermes skill tiene `absorbed_into` pero a nivel skill, no a nivel claim/evidence.

Holographic detecta contradicciones por similarity, no las marca temporalmente.

OpenClaude solo dice al modelo "trust what you observe now" — prompt-level.

**El concepto "temporal valid_from + supersedes pointer"** que doc 16 menciona como opción para Q3 **NO tiene precedente claro en los sistemas clonados**. Vale buscar en Graphiti (paper / blog), que es el más mencionado para "temporal KG".

### 5.6 — Costo del dream

**Estado del arte**: ningún sistema tiene un costo medido de "consolidar una sesión a memoria entity-centric". Hermes background_review usa fork LLM pero el scope es decidir qué memoria/skill tocar. OpenClaude extractMemories usa fork — costo no medido en docs.

Memory-wiki es deterministic compile — sin costo LLM. Pero **el productor externo** (no plugin) sí gasta LLM, y memory-wiki no lo restringe.

**Implicación**: durin necesita estimar antes de implementar (doc 16 §6 ya menciona "$0.10/sesión con Haiku" como threshold). No hay número de referencia confiable en la muestra.

---

## §6 — Recomendación parcial para Q1-Q4

> No cierro decisión. El doc 17 (síntesis de los 3 agentes) hace la decisión. Pero acá apunto para cada Q hacia dónde apunta la evidencia recolectada.

### Q1 — Granularidad

**Hacia dónde apunta la evidencia**:

- **Mantener `kind` cerrado top-level + `subType` libre**, à la memory-wiki. Los 5 kinds (`entity, concept, source, synthesis, report`) son demasiado generales; durin probablemente quiere directamente lo que llama "tipos consolidables" (`person, project, place, topic, incident, tool`) como dirs top-level, sin nivel `kind` separado. El `entityType` libre del frontmatter podría seguir ahí para sub-clasificación dentro del tipo (`tool` → `model | cli | service`...).

- **El set de 10 propuesto en doc 16** (6 consolidables + 4 referenciables) **no se valida contra esta muestra** porque ningún sistema tiene 10 tipos de entidad. Memory-wiki tiene 1 (`entity`), Honcho 1 (`peer`), Holographic 1 (`entity` sin tipar). Doc 16 está **delante del estado del arte open-source** en granularidad. Eso no es necesariamente malo — puede ser correcto y simplemente más ambicioso. Pero hay que justificarlo desde el dominio de durin, no desde precedente.

- **No hay evidencia para "referenciables vs consolidables"** como distinción binaria. Memory-wiki trata `concept` como kind separado de `entity`, lo cual es análogo: conceptos consolidan, archivos no. Pero ningún sistema distingue "archivado por baja cardinalidad" vs "archivado por inmutabilidad" (la justificación de `decision` y `event` como referenciables en doc 16).

**Apunta a**:
- Reducir el set inicial a 4-6 tipos consolidables, no 6. Aumentar después si emerge necesidad.
- Mantener `file` y `symbol` fuera de `entities/` (ya viven naturalmente en frontmatter `source_refs`).
- Reconsiderar `decision` y `event` como consolidables si el doc 17 encuentra precedentes (Cognee, Graphiti). Si no, mantener como tags.

### Q2 — Identidad y unificación

**Hacia dónde apunta la evidencia**:

- **Slug filename + `aliases` array + `canonicalId` opcional**, à la memory-wiki. Cubre los 3 patrones de identidad observados (Holographic, memory-wiki, Honcho).

- **Productor canónico = LLM en el dream/consolidator**. Ningún sistema con regex-extractor (Holographic, OpenClaw `detectCategory`) escala bien. La evidencia es contundente: 6 sistemas usan LLM para extracción.

- **Resolución alias = lookup contra `aliases` array en lectura**, no resolución LLM-runtime. Memory-wiki demuestra que esto es suficiente si la búsqueda incluye el campo aliases.

- **Caso `Marcelo` / `marcelo` / `Marcelo M`**: el dream genera la página con `aliases: [Marcelo, Marcelo M, mmarmol@...]` la primera vez. Si después aparece una entry nueva con `person:marcelo-m`, el dream detecta el alias en el momento de procesar (LLM call de consolidación) y agrega como nueva alias o decide que es entidad nueva. Mecanismo claro, precedente fuerte.

**Apunta a**:
- Schema mínimo: `entities/person/marcelo.md` con frontmatter `{ aliases: [...], canonicalId?: ... }`. Slug del filename ES la identidad canónica.
- Productor: dream con LLM. Sin fallback determinista.
- Resolución: lookup contra aliases.

### Q3 — Conflictos y evolución

**Hacia dónde apunta la evidencia**:

- **Claim-status enum** à la memory-wiki es el mejor precedente. 5 valores (`supported|contested|contradicted|refuted|superseded`) cubren la mayoría de casos.

- **Append, no override**. 4 sistemas convergen en esto (skill_curator patch, memory-wiki claims array, Holographic agregar facts, OpenClaude crear nueva memoria). El override es la excepción.

- **`absorbed_into` pattern** del skill curator es valioso: cuando una página se fusiona con otra, dejar puntero explícito al destino para auditoría.

- **Temporal `valid_from` + `invalid_at`** que doc 16 menciona NO tiene precedente fuerte en código. Es teoría (Graphiti, papers). Implementable pero requiere validación.

- **`updatedAt` per evidence + per claim + per page** (memory-wiki) es buen patrón. Cada nivel registra su timestamp independientemente.

**Apunta a**:
- Cada entry en `entities/<type>/<value>.md` es un claim con `{ text, status, evidence: [...], valid_from, updatedAt }`.
- Status default `supported`. Override solo cuando el dream tiene evidencia explícita de cambio.
- Append-only: dream agrega claims, marca viejos con status (no los borra).
- `absorbed_into: entities/project/durin.md` cuando se mergea otra entidad.

### Q4 — Lifecycle

**Hacia dónde apunta la evidencia**:

- **Thresholds en días basados en `updatedAt`/`last_referenced_at`** — 3 sistemas convergen (Hermes 30/90+? configurable, memory-wiki 30/90, OpenClaude texto progresivo). Determinista, sin LLM.

- **Combinar 3 niveles**: (a) warning text para páginas aging (à la OpenClaude); (b) marca como `stale` para visibilidad en reports (à la memory-wiki); (c) archive físico tras threshold alto (à la skill curator).

- **`pinned` flag** para opt-out manual (skill curator). Aplicable a `_MEMORY_AUTHOR=user_authored`.

- **Telemetría en sidecar**, no en frontmatter user-facing. `~/.durin/memory/.usage.json` o similar con counters.

- **Reactivación automática on access** (skill curator: si una skill `stale` se usa, vuelve a `active`). Útil para evitar que páginas relevantes pero infrecuentemente referenciadas envejezcan permanentemente.

**Apunta a**:
- 3 thresholds: aging 30d → warning text; stale 90d → marcar en `reports/stale.md`; archive 180d → mover a `entities/.archive/`. Determinista, configurable.
- `pinned: true` en frontmatter o `_MEMORY_AUTHOR=user_authored` → skip all thresholds.
- Reactivación: cualquier read o write resetea `last_referenced_at`.
- `restore` operation para recuperar archivos.

---

## §7 — Anti-patrones identificados (qué NO hacer)

Confirmaciones cross-sistema de cosas que duelen:

1. **Declarar columna `type` sin productor que la llene** (Holographic `entity_type`). La columna grita "úsame" y nadie la usa. Mejor: no declarar hasta tener el productor.

2. **Enum cerrado para `category` con `entity` como una de las 5 opciones** (LanceDB). Mezcla "tipo del item" con "tipo de entidad". Confunde a productores y consumidores.

3. **Regex multilingüe como classifier** (LanceDB `detectCategory`, Holographic `_extract_entities`). Frágil, no escala más allá de "I prefer / decided". Pierde nombres propios mixed-case, snake_case, dotted paths.

4. **"Category como prefix textual"** (OpenViking `[Remember — preference] ...`). Acopla tipo a contenido. Irrecuperable si el modelo escribe el prefix mal o si después querés filtrar.

5. **Backend opaco para reconciliación** (Honcho self-heal). Sin visibilidad ni determinismo. Aceptable si lo construyó alguien con compromiso de SLA; inviable para self-hosted.

6. **Borrado sin `absorbed_into`** (Hermes skill curator pre-fix). Pérdida de auditoría: imposible distinguir "lo borré porque era basura" de "lo fusioné con la umbrella X".

7. **Sin lifecycle automático** (Holographic, LanceDB, memorias OpenClaude). Las entidades crecen sin tope. El sistema se degrada por acumulación.

8. **Archive agresivo** sin warning step previo (algunos sistemas). Pierde contexto que el modelo podría recuperar. Combinar con warning text es mejor.

---

## §8 — Notas finales sobre el set de 10 tipos propuesto en doc 16

Confronto el set tentativo de doc 16 §3 contra la evidencia recolectada:

| Tipo propuesto | Evidencia en sistemas clonados | Mi anotación |
|---|---|---|
| `person` (consolidable) | Honcho peer (1 sistema), memory-wiki `personCard` sub-schema | Fuerte precedente. Mantener |
| `project` (consolidable) | Sin precedente directo. Memory-wiki podría tener `entity`/`concept` para esto | Razonable pero sin precedente |
| `place` (consolidable) | Sin precedente | Cuestionable. Memory-wiki lo metería en `concept` o `entity`. Considerar fusionar con `topic` salvo evidencia de uso recurrente en bitácora |
| `topic` (consolidable) | OpenClaw `concept-vocabulary` tiene "concept tags" pero planos, no consolidados. Memory-wiki tiene `concepts/` dir | Algo de precedente; mantener pero **separar bien** de `tags planos` |
| `incident` (consolidable) | Sin precedente | Sin evidencia. doc 16 lo justifica por "causa + fix + lección"; memory-wiki probablemente lo metería en `synthesis` |
| `tool` (consolidable) | Hermes skill curator es exactamente esto (cada skill = tool) | Fuerte precedente. Mantener |
| `file` (referenciable) | Memory-wiki source paths, no entity. Skills sourcerefs | Fuerte precedente para NO consolidar |
| `symbol` (referenciable) | Sin precedente | Razonable. No consolidar |
| `decision` (referenciable) | OpenClaw LanceDB tiene `decision` como item-category. Sin precedente como entity | Cuestionable. ¿Es categoría de item o tipo de entidad? Doc 14 ya marcaba la ambigüedad. Memory-wiki lo metería en `synthesis`. Considerar **NO incluirlo como tipo**, dejarlo emergir como `synthesis` cuando aplique |
| `event` (referenciable) | OpenViking, RetainDB lo tienen como item-type. Sin precedente como entity | Similar a `decision` |

**Observación general**: el doc 16 propone tipos por dominio durin (lo que aparece en bitácora). La muestra open-source habla más de **patrones generales** (memory-wiki: 5 kinds genéricos; Honcho: 1 kind peer). Hay tensión:

- **Si durin opina mucho** sobre los tipos: el modelo aprende fácil del prompt; el grafo es expresivo. Pero la decisión es prescriptiva, no derivada.
- **Si durin imita memory-wiki** (5 kinds genéricos): menos opinión, más universal. Pero requiere `entityType` libre para llegar al nivel de granularidad del doc 16.

**Mi sugerencia parcial** (no decisión):
- Adoptar el approach de memory-wiki: directorios cerrados por **kind** (probablemente 3-4: `entity/`, `concept/`, `synthesis/`, `incident/`); `entityType` libre dentro de `entity/` para `person|project|place|tool`.
- Eso reduce de 10 tipos a un esquema en 2 niveles, validado por memory-wiki, más extensible.

Pero **doc 17** decide.

---

## §9 — Resumen ejecutivo

12 sistemas/módulos revisados, 1 cercano (memory-wiki), 2 con lecciones operacionales fuertes (Hermes skill curator, Honcho), el resto aporta confirmaciones o anti-patrones.

**El precedente más útil de la muestra es OpenClaw memory-wiki**. Tiene exactamente la estructura de directorios + frontmatter tipado + aliases + claims-with-status + freshness-thresholds + reports-as-dashboards que durin propone en doc 16.

**El segundo más útil es Hermes skill curator**. Aplica el patrón "entidad como página markdown con lifecycle automático" a skills. Aporta: state machine, archive físico, pinned opt-out, absorbed_into pattern, telemetry sidecar.

**Los patrones convergentes (≥2 sistemas)** son los más probables de ser correctos:
- Tipar item con enum chico 4-6 (5 sistemas).
- Aliases para unificación (2 sistemas).
- Productor canónico = LLM (6 sistemas).
- Lifecycle por threshold en días (3 sistemas).
- Append/patch sobre rewrite (2 sistemas).
- Evidence con source linking (2 sistemas).
- Sub-agent fork para extracción (2 sistemas).
- Opt-out manual de auto-transitions (2 sistemas).
- Telemetry sidecar (2 sistemas).

**Lo que la muestra NO resuelve** y durin tendrá que innovar:
- Set de 10 tipos de entidad (sin precedente — máximo 5 visto).
- Unificación cross-type.
- Heurística "cuándo consolidar página".
- Combinación archive físico + warning text + freshness levels.
- `valid_from` / `supersedes` temporal-explicit (probablemente Graphiti, no en muestra clonada).
- Costo amortizado del dream.

**Direcciones tentativas por Q** (sujeto a doc 17):
- Q1: reducir set inicial; kinds cerrados top-level + subType libre.
- Q2: slug filename + aliases array + canonicalId; productor LLM.
- Q3: claim-status enum 5-valores; append-only; absorbed_into pointer.
- Q4: 3 niveles (warning/stale-report/archive físico) con thresholds en días; pinned opt-out; telemetry sidecar.
