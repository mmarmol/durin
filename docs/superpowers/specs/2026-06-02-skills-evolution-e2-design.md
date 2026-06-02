# Spec — Skills Evolution E2 (Cristalización + Curación)

> **Estado:** diseño en revisión, pre-implementación. Etapa 2 del sistema de
> skills evolutivas. Construye sobre **E1 (shipped** — PR #19, `e595fd6`).
> Decisiones + **auditoría de código** vía brainstorming el 2026-06-02.
>
> **Nota de proceso:** un primer borrador de este spec se apoyó en supuestos
> sobre el flujo de datos (drill, qué lee Dream, un solo dream) que **no
> resistieron la lectura del código**. Esta versión está reescrita sobre hechos
> verificados — ver **Apéndice A** para la evidencia `file:line`.
>
> **Fuentes:** [`docs/plans/skills_evolutivas.md`](../../plans/skills_evolutivas.md)
> · [`2026-06-01-skills-evolution-mvp-design.md`](2026-06-01-skills-evolution-mvp-design.md) (E1).

---

## §0 — Una frase

En E2, durin **aprende y cura skills desde tu uso real**, repartido en sus dos
sueños existentes según el ritmo de cada uno: el de **cada-2h** (aprendizaje de
comportamiento) hace la **autoría/parche local y rápido**, y el **diario**
(consolidación exhaustiva) hace la **curación global** —fusionar, unificar,
revisar el catálogo entero—. La señal que los alimenta vive en el **`.meta.json`
de cada sesión** (durable, estructurada), y toda escritura pasa por el
`skills_store` de E1 (provenance + commit + fork-on-write). Las `manual` no se
tocan: solo se leen.

El **buscador** de skills (Spec 2, lado lectura) queda **comprometido en el
plan**, después: la curación hace crecer el catálogo y eso es lo que vuelve al
buscador no-opcional.

---

## §1 — Dónde encaja (roadmap)

```
E1 ─ MVP ─────────  [HECHO] Skills versionadas + modo auto/manual + skill_edit
       │                   (substrato local; provenance; fork-on-write)
       ▼
E2 ─ Cristalizar    ◀── ESTÁS ACÁ
       │
       ├─ Spec 1 (ahora) ── ESCRITURA, repartida en los dos sueños
       │     · Señal: skill_calls en el .meta.json de la sesión
       │     · Parte A — sueño 2h:  autoría/parche local (rápido)
       │     · Parte B — sueño diario: curación global (exhaustivo)
       │
       └─ Spec 2 (después, comprometido) ── LECTURA
             · Local: recientes + buscador (corpus en search_pipeline)
             · Remoto: federado / acquire-on-gap  ── converge con E4
       ▼
E3 ─ Import+Adapt   ·  E4 ─ Acquire+Search  ·  E5 ─ Meta-skill
```

E2 escribe por el `skills_store` de E1; la señal de Spec 1 alimenta los
"recientes" de Spec 2; el corpus que Spec 2 indexa es el que Spec 1 vuelve real.

---

## §2 — Los dos sueños y su reparto (hecho verificado)

durin tiene **tres** mecanismos de sueño, con roles reales distintos (Apéndice A):

| Mecanismo | Cadencia | Rol real hoy |
|---|---|---|
| `threshold_trigger` → entity dream | reactivo (por actividad) | absorción de **entidad** caliente |
| `Dream` (job `dream`) | **cada 2h** | **comportamiento + procedimientos**: edita SOUL/USER/MEMORY *y* ya autorea skills |
| `DreamConsolidator` (job `memory_dream`) | **diario, 3am** | consolidación **factual** exhaustiva de entidades |

E2 usa los **dos de cadencia fija**, según su ritmo natural:

- **Parte A — sueño de 2h (rápido):** autoría y parche **local** de skills. Ya es
  el sueño que aprende comportamiento y ya autorea skills — pero **crudo**
  (ver §3). E2 lo enruta por E1.
- **Parte B — sueño diario (exhaustivo):** curación **global** — fusionar
  solapadas, unificar, revisar el catálogo. Necesita ver todo junto; la cadencia
  diaria es la baja-frecuencia que evita oscilación.

> **Decisión (2026-06-02):** se hacen **las dos partes**, empezando por
> **Parte A** (autoría/parche local, sueño 2h); Parte B después.

El reactivo (`threshold_trigger`) es por-entidad; las skills no son entidades, así
que **no** se usa en Spec 1.

---

## §3 — El problema que resuelve (verificado)

1. **El sueño de 2h ya cristaliza skills, pero crudo.** En su Phase 2 usa el
   builtin `skill-creator` + `WriteFileTool` directo para escribir `SKILL.md`
   — **sin provenance, sin `mode`**, y su `auto_commit` apunta al git de
   *memoria*, que **no trackea `skills/`** → las skills que escribe quedan **sin
   commitear por nadie**. No son ciudadanas de primera clase de E1.
2. **El catálogo actual no es fuente de verdad.** Las builtin (`clawhub, cron,
   github, long-goal, memory, my, skill-creator, summarize, tmux, update-setup,
   weather`) son **heredadas del fork base**; ninguna tiene provenance, varias no
   sobreviven.
3. **No hay señal de uso de skills.** Hoy no se registra qué skill se activó.

E2 ataca los tres: enruta la escritura por E1, le da la señal de uso, y reparte el
trabajo entre los dos sueños.

---

## §4 — La señal: `skill_calls` en el `.meta.json` (verificado)

### Por qué en el meta y no en lo que “lee Dream”

Hecho clave: lo que el sueño consume de `history.jsonl` son **resúmenes LLM**
(Consolidator), no la transcripción cruda — el detalle de tool-calls **se resume
y se pierde**. Por eso la señal **no** puede vivir en `tools_used` (solo nombres,
y encima se resume). Tiene que vivir en un **side-channel estructurado que
sobreviva**: el `.meta.json` de la sesión, que **nunca se capa**.

```
turno N (sesión)                        sueño (después)
────────────────                        ───────────────
tool sobre una skill ──► anota skill_calls en      lee skill_calls del .meta.json
  read_file(skills/X)     Session.metadata          (señal durable, estructurada)
  skill_edit(X)               │                          │
  crear X                     ▼                          ▼
                       <key>.meta.json            + opcional: drillea la sesión
                       (derived.skill_calls)        sessions/<key>.jsonl para
                                                     contexto extra mientras esté
```

### Qué se anota

En `Session.metadata["skill_calls"]` (clave nueva en `_DERIVED_METADATA_KEYS`,
ver §10), que la máquina de save persiste sola al bloque `derived` del
`.meta.json`:

```jsonc
"skill_calls": [
  { "skill": "git-helper", "op": "read",   "turn": 41 },
  { "skill": "git-helper", "op": "edit",   "turn": 47 },
  { "skill": "deploy-flow", "op": "create", "turn": 52 }
]
```

- **`op`** ∈ `read` (`read_file` sobre `skills/<name>/SKILL.md`) · `edit`
  (`skill_edit`) · `create` (alta vía `skills_store`).
- **`turn`** = ancla al turno (durin ya usa `#turn-N` en el drill de memoria) →
  permite ir a la sesión por posición.

### Por qué es durable y suficiente

El `.meta.json` no se capa; el `sessions/<key>.jsonl` sí (cap alto de 2000 msgs,
y al cruzarlo el prefijo viejo se **archiva, no se pierde**). Conclusión: el meta
carga la **señal durable**; la sesión es **contexto extra mientras esté**. Si un
turno viejo ya migró, `skill_calls` sigue teniendo skill+op+turn.

### Cursor "desde la última vez" (decisión 2026-06-02)

Se adopta el patrón que **memoria ya usa**, no uno nuevo:

- **Parte A (sueño 2h):** se apoya en el **cursor global** del sueño de 2h
  (`.dream_cursor`, entero monótono) para "qué sesiones son nuevas".
- **Parte B (sueño diario):** **cursor por-skill guardado en el frontmatter de la
  skill** (`metadata.durin.provenance.dream_processed_through`), **idéntico** al
  `dream_processed_through` que el sueño de entidades guarda en cada página
  ([dream_runner.py:700](../../../durin/memory/dream_runner.py#L700)). Cada skill
  lleva su propio "procesado hasta" → la staleness por-skill (§6) es nativa.

Se descarta el *date-filter* (una query recomputada, no estado durable → frágil y
desalineado con memoria).

---

## §5 — Parte A: autoría/parche local (sueño de 2h)

Rápido y local. Sobre las sesiones nuevas desde el cursor:

```
┌──────────────────────────────────────────────────────────┐
│  ¿hay un patrón recurrente que NINGUNA skill cubre?       │  señal = CONTENIDO
│   sí → autorear borrador (skill-creator) vía skills_store  │  de sesiones
├──────────────────────────────────────────────────────────┤
│  ¿una `auto` activada mostró fricción/fallo?              │  señal = skill_calls
│   sí → parche acotado vía skills_store                     │  + transcripción
└──────────────────────────────────────────────────────────┘
        manual: solo se lee, nunca se toca
```

- **Crear** usa el **contenido** de las sesiones (una skill nueva nunca fue
  "vista"); `skill_calls` no aplica a crear. Dedup contra catálogo antes de
  escribir.
- **Parchear** usa `skill_calls` (qué `auto` se activó) + la transcripción para
  decidir si hay algo que arreglar.
- **Reemplaza** el `WriteFileTool` crudo de hoy por `skills_store` (provenance,
  commit, fork-on-write).

---

## §6 — Parte B: curación global (sueño diario)

Exhaustivo. Ve el catálogo entero; la cadencia diaria es la baja-frecuencia
anti-oscilación.

```
┌──────────────────────────────────────────────────────────┐
│  FASE 0 — Mapa global (sin LLM)                           │
│  agrega skill_calls de las sesiones desde el cursor        │
│  → { skill → [sesiones, ops] }                             │
├──────────────────────────────────────────────────────────┤
│  FASE 1 — Fusionar solapadas (juicio por CONTENIDO)       │
│  dos `auto` cubren lo mismo → fusionar A+B→C               │
├──────────────────────────────────────────────────────────┤
│  FASE 2 — Mejorar las `auto` muy usadas                   │
│  revisión de catálogo, no per-sesión                       │
└──────────────────────────────────────────────────────────┘
        VERIFICACIÓN → skills_store (dedup, provenance, commit)
```

- **Fusión A+B→C:** escribir C vía `skills_store`; quitar A y B (workspace) o
  fork-and-disable (builtin); **un** commit con rationale. Git guarda el
  histórico (`revert` recupera).
- **Sin poda activa de muertas en Spec 1** (decisión 2026-06-02): una skill sin
  uso simplemente queda; el buscador (Spec 2) la vuelve inocua.
- **Cadencia = staleness por-skill (decisión 2026-06-02).** El pase corre diario,
  pero re-cura **cada skill según su propio** `dream_processed_through`: en cada
  día las skills tienen distinta antigüedad sin reprocesar; se re-curan las que
  cruzaron su umbral (p.ej. ≥7d) o que acumularon `skill_calls` nuevos. No es un
  throttle global — es per-skill, igual que el sueño de entidades cura cada página
  por su cuenta. Eso evita re-tocar la misma skill a diario (anti-oscilación) sin
  frenar el pase.

---

## §7 — Reuso de E1 + el límite `manual`

E2 **no inventa** escritura: usa `skills_store`. Diferencia con el `WriteFileTool`
crudo de hoy:

| Dimensión | Sueño 2h hoy (crudo) | E2 (vía E1) |
|---|---|---|
| Vía | `WriteFileTool` directo | `skills_store.save_skill_content` / `apply_skill_edit` |
| Provenance | ninguna | `metadata.durin.provenance.source = "dream"` |
| Modo | ninguno | `mode = auto` |
| Commit | git de memoria (**no trackea skills/**) → sin commit | subtree de skills, **rationale por-skill** |
| Builtins | sobrescribe | **fork-on-write** |

### El límite `manual` (decisión tomada)

```
        skill.mode == "auto"    →  sueños: leer ✔  crear ✔  parchear ✔  fusionar ✔
        skill.mode == "manual"  →  sueños: leer ✔  modificar ✗  proponer ✗
```

`manual` = del usuario, intocable. Los sueños la **consultan** (pueden inspirarse
para una `auto`), nunca la editan ni dejan propuestas. El gate vive en
`skills_store` (E1 ya valida `mode`) → imposible saltárselo.

---

## §8 — Manejo de errores

| Falla | Comportamiento |
|---|---|
| tool sobre un path no-skill | no se anota `skill_calls` (no-op correcto) |
| `.meta.json` ausente/corrupto | esa sesión se omite del mapa; se loguea; el sueño sigue |
| juez-LLM falla en una fase | aborta esa skill, **no** avanza cursor de skills; reintenta próximo pase |
| `skills_store` rechaza por `mode=manual` | se descarta + se loguea; nunca error fatal |
| fusión A+B→C falla a mitad | **un** commit atómico; si falla, no se quita nada |
| oscilación | cadencia: la global corre diario (no en cada turno); la local solo ante señal nueva |

---

## §9 — Plan de tests

1. **skill_calls recorder** — `read_file`/`skill_edit`/create sobre una skill
   anota `{skill, op, turn}` en `Session.metadata`; un path no-skill no anota.
2. **Persistencia** — `skill_calls` sobrevive el save→load del `.meta.json`
   (sidecar derived), y **no** se pierde aunque la sesión se cape.
3. **Parte A crear** — patrón recurrente sin skill que lo cubra → autorea (vía
   `skills_store`); ya cubierto → no crea (dedup).
4. **Parte A parchear** — `auto` activada con fallo en transcripción → parche;
   `manual` activada → **no** se toca (gate `skills_store`).
5. **Parte B fusión** — A+B solapadas → C escrita, A/B retiradas, **un** commit.
6. **Provenance** — toda skill creada/parcheada por un sueño queda con
   `source=dream`, `mode=auto`, commit con rationale en el subtree de skills.
7. **Cursor** — sesiones ya procesadas no se re-procesan; las nuevas sí.

---

## §10 — Mapa de cambios por archivo

| Archivo | Cambio |
|---|---|
| boundary de ejecución de tools ([runner.py](../../../durin/agent/runner.py) / [tools/filesystem.py](../../../durin/agent/tools/filesystem.py) `ReadFileTool`, [tools/skill_edit.py](../../../durin/agent/tools/skill_edit.py)) | al tocar una skill, anotar `{skill, op, turn}` en `Session.metadata["skill_calls"]` |
| [`durin/session/manager.py`](../../../durin/session/manager.py) | agregar `"skill_calls"` a `_DERIVED_METADATA_KEYS` (persiste solo al `.meta.json`) |
| [`durin/agent/memory.py`](../../../durin/agent/memory.py) (`Dream`, 2h) | Parte A: enrutar autoría por `skills_store` y **eliminar** el `WriteFileTool` crudo de Phase 2; leer `skill_calls` para el parche |
| [`durin/memory/dream.py`](../../../durin/memory/dream.py) (`DreamConsolidator`, diario) | Parte B: paso de curación global (mapa `skill_calls` → fusionar/mejorar) vía `skills_store` |
| [`durin/agent/skills_store.py`](../../../durin/agent/skills_store.py) | helper de escritura con `source=dream` + quitar A/B en fusión |
| `DreamConfig` / `memory.dream` ([config/schema.py](../../../durin/config/schema.py)) | cursor de skills por-sueño; (opcional) `last_curation_at` para throttle de Parte B |
| `tests/agent/test_skill_calls.py` · `test_skill_curation.py` | nuevos |

---

## §11 — Spec 2 (lado lectura) — esbozo comprometido

No se construye en Spec 1, pero queda fijado: la curación hace crecer el catálogo.

```
Local                                   Remoto (converge E4)
─────                                    ────────────────────
recientes pre-cache  ◀── reusa          búsqueda federada a
  skill_calls (Spec 1)                   marketplaces (acquire-on-gap)
buscador de skills   ◀── indexa skills   confirma con AskUserQuestion
  como corpus en search_pipeline
inyección por RELEVANCIA  (reemplaza el catálogo-entero de cada turno)
```

**Disparador para empezar Spec 2:** cuando el catálogo curado deje de ser
"mirable a ojo", o cuando el costo de inyección del catálogo se vuelva medible.

---

## §12 — Decisiones y preguntas abiertas

**Resueltas (2026-06-02):**

1. **Orden de construcción:** se empieza por **Parte A** (autoría/parche local en
   el sueño 2h). Parte B después.
2. **Cursor de skills:** patrón de memoria — Parte A se apoya en el cursor global
   del 2h; Parte B usa cursor **por-skill en frontmatter**
   (`dream_processed_through`). Se descarta el date-filter (§4).
3. **Cadencia de Parte B:** **staleness por-skill** (cada skill por su
   `dream_processed_through`), no throttle global (§6).
4. **Retiro de la autoría cruda del 2h:** al enrutar por `skills_store`, **se
   elimina** el `WriteFileTool` crudo de Phase 2 — una sola vía sancionada de
   escritura de skills.

**Abiertas:** ninguna. Spec listo para `writing-plans` (Parte A).

---

## §13 — Referencias

- Plan-fuente: [`docs/plans/skills_evolutivas.md`](../../plans/skills_evolutivas.md)
  (§6.A crear, §6.D evolucionar, §8.A trigger — resuelto acá).
- E1: [`2026-06-01-skills-evolution-mvp-design.md`](2026-06-01-skills-evolution-mvp-design.md).

---

## Apéndice A — Hechos verificados en código (auditoría 2026-06-02)

Cada fila se chequeó leyendo el código, no infiriendo de nombres.

| Hecho | Evidencia |
|---|---|
| Skills se surfacean por catálogo **con path** y se cargan vía `read_file` (no hay tool loader) | `skills.py` `build_skills_summary` → `…— {desc} \`{entry['path']}\`` |
| El sueño de **2h** (`Dream`) analiza history + edita SOUL/USER/MEMORY **y autorea skills** | `memory.py:1082-1090`; tools en `:1136-1159` (`WriteFileTool` sobre `skills_dir`) |
| Schedule del 2h = **cada 2h** | `config/schema.py:182` ("legacy `dream` job's every-2h schedule") |
| El **diario** (`memory_dream` / `DreamConsolidator`) consolida **entidades**, **no skills**; cron `0 3 * * *` | `config/schema.py:163,183`; `cli/commands.py:1606` |
| `threshold_trigger` = reactivo por-entidad (throttle 300s) | `memory/threshold_trigger.py` |
| `history.jsonl` que el sueño lee = **resúmenes LLM** (Consolidator); el crudo es solo fallback | `memory.py:814-832` (`raw_archive` = fallback) |
| `auto_commit` del sueño 2h = git de memoria, que trackea **solo** `SOUL/USER/MEMORY/.dream_cursor` (**no `skills/`**) → skills sin commitear | `memory.py:67` |
| `.meta.json` sidecar con `read_derived`/`write_derived` + registro `_DERIVED_METADATA_KEYS` ("add new entries here") | `session/manager.py:17-18, 314-323` |
| `.meta.json` **nunca se capa**; `sessions/<key>.jsonl` se capa a `FILE_MAX_MESSAGES=2000`, y el prefijo viejo se **archiva (no se borra)** | `session/manager.py:30, 269-297` |
| `drill()` = lector de secciones markdown **read-only** para el recall del **agente** (no parsea tool_calls; **no** es mecanismo de Dream) | `memory/drill.py:9, 52` |
| `skills_store` (E1) expone `apply_skill_edit`, `save_skill_content`, `fork_on_write`, `set_mode`, `_store_init` (git) | `agent/skills_store.py` |
| `tools_used` = **solo nombres** (pierde el path/args) | `runner.py:702`, `hook.py:140` |
