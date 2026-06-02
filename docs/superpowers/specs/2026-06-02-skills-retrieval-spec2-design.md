# Spec — Skills Retrieval (Spec 2, lado lectura): memoria de skills en dos niveles

> 🟢 **FUENTE DE VERDAD** del lado-lectura de skills (el "Spec 2" comprometido en
> [`2026-06-02-skills-evolution-e2-design.md`](2026-06-02-skills-evolution-e2-design.md) §11).
> Diseño cerrado vía brainstorming 2026-06-02, **tras auditar Hermes en detalle**.
> Estado: pre-implementación.

---

## §0 — Una frase

Las skills son una **clase de memoria** (procedural), y se manejan como **memoria
en dos niveles**: un **set caliente cacheado** (sistema/fijas + recientes +
frecuentes) siempre presente para recall instantáneo y sin-miss de lo común; y el
**resto del catálogo, buscable** vía `memory_search` (clase `skill`) **on-demand**
cuando el agente detecta que lo que tiene a mano no cubre la tarea — como recordar
algo deliberadamente, pero con recall **confiable**. El mismo search se extiende
después al scope remoto (E4). **E1 (autoría/versión) intacto.**

---

## §1 — Por qué así (la decisión, contra Hermes y contra "search-first")

Auditamos los dos extremos:

- **Hermes (verificado en código):** inyecta el **índice compacto completo** de
  todas las skills activas (nombre + desc 60-char) en el tier cacheado; **sin
  búsqueda**. Escala a cientos vía encoding compacto + caching, y **acota
  crecimiento podando/archivando inactivas (>90d)**. Limitación: **necesita podar
  y unificar constantemente** para que el índice no explote.
- **Search-first (reemplazar el catálogo por retrieval por-query):** descartado.
  Dos fallas: (a) **miss silencioso** — el agente nunca se entera de una skill que
  el retrieval no surfaceó; (b) **rompe el prefix-cache** — el contexto de skills
  pasa a ser dinámico por-turno → cache-miss constante → **más caro**, no menos.

**El modelo de dos niveles toma lo bueno de ambos y evita lo malo:**
- **Sin poda constante:** las skills frías viven en el corpus buscable; no se
  archivan a la fuerza por costo. El catálogo crece libre.
- **Sin miss silencioso:** lo común está siempre caliente; lo frío se busca
  **deliberadamente** (tool call), así el "no lo encuentro" es **visible y
  recuperable**, no una omisión.
- **Sin romper el cache:** el set caliente es **estático/cacheado**; el search es
  un **tool call on-demand**, fuera del prefix cacheado.
- **Costo justificado:** el search se paga **solo cuando hay un gap real**.

---

## §2 — Alcance

> **Modelo (decisión 2026-06-02):** las skills son una **clase de memoria nueva**
> (`skill` / procedural), hermana de `episodic`/`corpus`/`entity` en la taxonomía
> que durin **ya tiene** (`MEMORY_CLASSES`; resultados tipados por `kind` que el
> LLM usa para decidir — [search.py:101](../../../durin/memory/search.py#L101)).
> **No** es un corpus paralelo ni un `skill_search` aparte: `memory_search` las
> devuelve **tipadas `kind="skill"`**. **Home físico = E1** (`workspace/skills/`);
> la clase es un registro de índice sobre E1, **no** un movimiento.

### Entra (Spec 2), en orden de dependencia

0. **FUNDACIÓN — clase `skill` + acoplamiento de ciclo de vida.** Registrar la
   clase `skill` en el índice de memoria, e indexar **sincrónicamente desde
   `skills_store`** en cada mutación (crear → upsert · editar → re-index ·
   fusionar → upsert C + remove A/B · modo/disable → update). El índice **siempre**
   refleja la realidad. Esto va **antes que todo lo demás** — un search sobre
   índice viejo es peor que no tener search. (Consistente con E1: tool-in-loop,
   sin watcher.)
1. **Recuperación unificada:** `memory_search` devuelve skills **tipadas**
   (`kind="skill"`) junto a hechos; **filtro por clase** (skills-only / facts-only
   / ambas, como ya hay `scope`). El embed del corpus = `nombre + descripción
   completa + when_to_use` (sin cuerpo). El agente abre la `SKILL.md` con
   `read_file` (realimenta `skill_calls`).
2. **Set caliente (hot tier), cacheado:** sistema/fijas (`always`) + **N recientes
   de la sesión** + **X más usadas de la semana** (§8.1) — working-set procedural
   en el "stable anchor" cache-friendly de `ContextBuilder`.
3. **Trigger** (§5): que el agente sepa cuándo buscar (estilo tools de memoria).
4. **Telemetría de misses:** un `memory_search(skill)` sin hit útil = señal (base de E4).

### No entra (futuro, con su etapa)

| Diferido | Etapa | Por qué no ahora |
|---|---|---|
| **Search remoto / federado** a repos/marketplaces | **E4** | Es el **mismo** search (`memory_search` clase `skill`) con scope ensanchado; se construye cuando exista el catálogo/uso que lo justifique (acquire-on-gap §6.C plan-fuente) |
| **Ajuste del hot-set por-chat** (cache context-aware) | post-Spec2 | Depende de mejoras de manejo de contexto; el search ya deja la base lista |
| **Poda agresiva de muertas** | — | Reencuadrada como **calidad** (E2), no escala (ver §6); no es control de costo acá |

---

## §3 — Arquitectura (los dos niveles + la extensión)

```
                        CONTEXTO POR-TURNO (cacheado, estático)
   ┌───────────────────────────────────────────────────────────────┐
   │  HOT TIER  (siempre presente, prefix-cache-friendly)           │
   │   · sistema / fijas (always)                                   │
   │   · N recientes de la sesión        ◀── skill_calls           │
   │   · X más usadas de la semana       ◀── skill_calls (7d)      │
   │   → solo nombre + desc; cuerpo on-demand (read_file)           │
   └───────────────────────────────────────────────────────────────┘
                    │  el agente no encuentra lo que necesita
                    ▼  (trigger §5 → tool call, NO rompe el cache)
   ┌───────────────────────────────────────────────────────────────┐
   │  memory_search(..., clase=skill)  → resto del catálogo         │
   │   skills = clase de memoria (kind="skill"), indexada desde E1   │
   │   scope hoy:   todas las skills locales (cacheadas o no)        │
   │   scope E4:    + repos/marketplaces remotos  ── mismo search    │
   │   motor: search_pipeline (vector + FTS + RRF) — ya existe       │
   │   devuelve nombre+desc tipado → el agente abre la skill (read_file) │
   └───────────────────────────────────────────────────────────────┘
```

**La clave:** el hot tier **acota lo que se inyecta** (working set), pero **no se
poda el corpus** (se guarda todo, buscable). Escala = split caliente/frío, no poda.

---

## §4 — Reuso del motor (search_pipeline)

**Skill = clase de memoria, indexada desde E1.** Cada skill se indexa en el índice
de memoria (vector+FTS+RRF, **cero motor nuevo**) con `type="skill"`, embebiendo
`nombre + descripción completa + when_to_use` (**sin el cuerpo**). Home físico = el
store de E1; la clase apunta ahí. `memory_search` la devuelve con `kind="skill"`;
el LLM ya usa el `kind` para tratarla como procedimiento, no hecho.

**Acoplamiento de ciclo de vida (la fundación, §2.0).** El índice se sincroniza
**síncronamente dentro de `skills_store`** — la única vía de mutación (E1):

| Mutación (en `skills_store`) | Op de índice |
|---|---|
| `dream_create_skill` / alta por tool/web | upsert |
| `apply_skill_edit` / `save_skill_content` | re-index |
| `dream_fuse_skills` | upsert(C) + **remove(A, B)** |
| `set_mode` / disable | update |

Por construcción, el índice nunca diverge del catálogo real. La **descripción es
el campo load-bearing** del retrieval → la autoría/curación debe producir buenas
descripciones (es el "handle" buscable). El hot tier *muestra* solo nombre+desc
corta; el corpus *embebe* más (§8.3).

---

## §5 — El trigger (el punto crítico, honesto)

El riesgo real del modelo: **¿el agente sabe cuándo buscar?** Los LLM no siempre
saben lo que no saben. Mitigaciones (capas, no una sola):

1. **Hot tier generoso** — cubre el caso común; el gap solo aplica a la cola larga.
2. **Nudge estructural** en el prompt: *"si nada del hot tier cubre la tarea,
   buscá (`memory_search` clase `skill`) antes de proceder o de decir que no hay skill."* (Sabemos
   que el texto en prompts es señal débil — por eso no es la única capa.)
3. **Opcional/futuro:** señal estructural — p.ej. si el turno cruza el umbral de
   complejidad (estilo Hermes ≥N iters de tool) sin haber tocado una skill,
   sugerir buscar. Telemetría de misses calibra esto.

Honestidad: es un lever más débil que la inyección determinista del catálogo
completo (Hermes), pero el trade vale porque **lo común está caliente** (sin
dependencia del trigger) y la cola larga **tiene recuperación** (el tool), en vez
del miss silencioso del search-first.

---

## §6 — Qué le hace esto a la curación de E2 (reencuadre)

Con el split caliente/frío, **la escala ya no la maneja la curación.** Por lo
tanto la fusión/dedup de **E2 Parte B deja de ser control-de-costo y pasa a ser
CALIDAD**: evitar que el search devuelva 5 casi-duplicadas como ruido.
→ **No hace falta podar agresivo y constante** (la limitación de Hermes). Curación
liviana por calidad alcanza; el costo/escala lo absorbe el hot/cold split.

---

## §7 — Mapa de cambios (preliminar, para el plan)

| Archivo | Cambio | Fase |
|---|---|---|
| `durin/agent/skills_store.py` | **hook de índice síncrono** en cada mutación (upsert/re-index/remove). El acoplamiento de ciclo de vida | **0 (fundación)** |
| `durin/memory/paths.py` (`MEMORY_CLASSES`) + `search_pipeline.py` / `vector_index.py` | registrar la clase `skill`; indexar skills (type="skill") desde el store de E1 | 0 |
| `durin/memory/search.py` / `memory_search` | devolver resultados `kind="skill"`; filtro por clase (skills/facts/ambas) | 1 |
| `durin/agent/skill_usage.py` | helper "working set": N recientes (sesión) + X frecuentes (7d) desde `skill_calls` | 2 |
| `durin/agent/context.py` (`ContextBuilder`) | el bloque de skills del stable-anchor pasa de catálogo-entero a **hot tier** (working set + always) | 2 |
| prompt / tool-instructions (estilo memoria) | nudge "si nada cubre la tarea, buscá" | 3 |
| telemetría | evento de `memory_search(skill)` miss | 4 |

> **Sin tool nuevo:** no hay `skill_search`. La búsqueda es `memory_search` con la
> clase `skill`. **E1 intacto** (autoría/versión/modo).

---

## §8 — Decisiones (resueltas 2026-06-02)

1. **N / X del hot tier:** **favorecer frecuentes** — arrancar ~**15 recientes-
   sesión / ~30 frecuentes-semana** (~40 únicas tras dedup). Las recientes-sesión
   ya están medio cubiertas por la conversación; las frecuentes-semana son el
   working set durable (el aporte real). Generoso porque es cacheado; **calibrar
   con la telemetría de misses**. Config-driven.
2. **Trigger = estilo tools de memoria:** descripción del tool + **bloque fijo de
   instrucciones** insertado en los prompts. **Medir primero** (telemetría de
   misses) antes de invertir en el trigger estructural por complejidad — las
   instrucciones de tool son señal débil; la medición valida si alcanzan.
3. **Granularidad — dos niveles distintos:**
   - **Hot tier (inyectado):** compacto, `nombre + descripción corta` (estilo Hermes).
   - **Corpus de búsqueda (embebido):** más rico — `nombre + descripción completa
     + when_to_use` (si está), **sin el cuerpo**. El corpus se matchea, no se
     inyecta → más señal mejora el retrieval a costo cero de contexto.
4. **No hay "reemplazar vs suplementar".** Un solo sistema: el **hot tier es
   siempre el working-set**, el **corpus es siempre todo**. Todas las skills
   existentes se **indexan al corpus** (= "migrar las anteriores al nuevo"). Con
   catálogo chico el working set cubre casi todo (search casi no dispara); al
   crecer, el split paga. El catálogo actual es circunstancial: entradas iniciales.

---

## §9 — Referencias

- E2 (escritura): [`2026-06-02-skills-evolution-e2-design.md`](2026-06-02-skills-evolution-e2-design.md).
- Plan-fuente: [`docs/plans/skills_evolutivas.md`](../../plans/skills_evolutivas.md) (§6.C/E acquire-on-gap → E4).
- Hermes (referencia auditada): [`docs/plans/hermes_skills_codebase.md`](../../plans/hermes_skills_codebase.md) — inyección sin search, archiving como control de escala.
