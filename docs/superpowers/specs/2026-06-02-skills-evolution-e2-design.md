# Spec — Skills Evolution E2 (Cristalización + Curación)

> 🟢 **FUENTE DE VERDAD de E2.** Este doc manda sobre el diseño y estado de la
> Etapa 2. Construye sobre **E1 (shipped** — PR #19, `e595fd6`).
>
> **Estado: ✅ COMPLETO (ambas partes; actualizado 2026-06-04, verificado contra código).**
> - **Parte A** (sueño 2h: crear + parchear local) — ✅ **HECHO** (merged a main,
>   release local `v0.1.0a9`). Plan: `docs/archive/skills-plans/2026-06-02-skills-evolution-e2-part-a.md`.
> - **Parte B** (sueño diario: curación global del catálogo) — ✅ **HECHO**:
>   `durin/agent/skill_curation.py::curate_catalog` (delta `auto`+`workspace`,
>   `evolve`/`fuse`, drift §8.D incorporado) está cableado en el job `memory_dream`
>   (`cli/commands.py`). As-built: `docs/architecture/skills/00_overview.md §6`.
>
> Decisiones + **auditoría de código** vía brainstorming el 2026-06-02.
>
> **Nota de proceso:** un primer borrador de este spec se apoyó en supuestos
> sobre el flujo de datos (drill, qué lee Dream, un solo dream) que **no
> resistieron la lectura del código**. Esta versión está reescrita sobre hechos
> verificados — ver **Apéndice A** para la evidencia `file:line`.
>
> **Fuentes:** [`docs/archive/skills_evolutivas.md`](../../archive/skills_evolutivas.md)
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
  skill** (`metadata.durin.provenance.dream_processed_through` = **hash del body**
  con que se revisó), análogo al cursor por-página del sueño de entidades
  ([dream_runner.py:700](../../../durin/memory/dream_runner.py#L700)). Cada skill
  lleva su "revisado-en-este-estado" → el **corte por cambio** (§6) es nativo:
  body igual = ya revisada = se saltea.

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

> ⚠️ **Cabo suelto conocido (de la implementación de A).** `collect_recent_skill_calls`
> hoy lee **todos** los sidecars de sesión (`sessions/*.meta.json`), no solo los
> recientes — contradice "solo sesiones recientes". *Impacto:* bajo (el juez
> decide por contenido; a lo sumo surfacea como candidata una skill usada hace
> mucho). *Resolución:* acotar el colector a lo reciente (ventana por mtime del
> sidecar). Se hace como **Tarea 0 del plan de Parte B** (la señal se retoca ahí
> igual). No es un soft-deferral: tiene fix y lugar.

Holístico en **alcance**, acotado en **trabajo**. El *alcance* es el catálogo
entero (cualquier par de `auto` puede fusionarse, la coherencia es global). Pero
el *trabajo* **nunca es "revisar todo"**: el límite es el **DELTA** — solo las
skills **nuevas o cuyo cuerpo cambió** desde la última revisión. Una skill estable
que nadie tocó **no se re-revisa** (no hay motivo). El uso del día es señal/contexto,
no el alcance.

```
┌──────────────────────────────────────────────────────────┐
│  FASE 0 — Delta (sin LLM)  ← EL CORTE                     │
│  auto skills nuevas o con body-hash ≠ dream_processed_through │
│  estables → se saltean sin LLM   |   + señal del día (uso) │
├──────────────────────────────────────────────────────────┤
│  FASE 1 — Revisar el delta (juicio por CONTENIDO)         │
│  cada una del delta vs el catálogo/vecinas:                │
│  ¿debe evolucionar? ¿se solapa con otra? → fusionar        │
├──────────────────────────────────────────────────────────┤
│  FASE 2 — Aplicar: evolucionar / fusionar A+B→C            │
└──────────────────────────────────────────────────────────┘
        tope `budget`/día (bursts arrastran, se loguean)
        VERIFICACIÓN → skills_store (dedup, provenance, commit)
```

> **Diferencia clave con Parte A:** A mira **solo sesiones recientes** y actúa
> *local* (mejorar lo llamado, crear lo que falta). B tiene *alcance global*
> (cualquier par puede fusionarse) pero **trabaja sobre el delta** (lo cambiado),
> no sobre todo el catálogo cada día.

- **Fusión A+B→C:** escribir C vía `skills_store`; quitar A y B (workspace) o
  fork-and-disable (builtin); **un** commit con rationale. Git guarda el
  histórico (`revert` recupera).
- **Sin poda activa de muertas en Spec 1** (decisión 2026-06-02): una skill sin
  uso simplemente queda; el buscador (Spec 2) la vuelve inocua.
- **El corte = CAMBIO, no tiempo (decisión 2026-06-02, refinado).** El cursor
  `dream_processed_through` guarda el **hash del body** con que se revisó la skill.
  El delta = skills sin cursor (nuevas) o con body-hash distinto (cambiadas). Las
  estables se saltean sin LLM → el pase **no escala con el catálogo**: catálogo
  estable = no-op. Esto reemplaza la idea previa de "staleness por tiempo" (que
  habría re-revisado todo cada día). El catálogo se recorre entero **una sola vez**
  (primer pase, todas sin cursor); después, solo el delta.
- **Tope de presupuesto:** si el delta es grande (burst, p.ej. import E3), se
  revisan `budget`/día y el resto **arrastra** (siguen sin cursor → otro día), se
  **loguea** lo diferido (sin truncado silencioso).
- **Fusión a escala:** comparar una del delta contra *todo* es barato con pocas;
  con miles, alimentar el prompt solo con las **vecinas** vía el índice de
  búsqueda (**Spec 2**) — dependencia suave, no bloquea con catálogo chico.

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
3. **Corte de Parte B:** **por CAMBIO**, no por tiempo — el delta = skills
   nuevas o con body-hash ≠ `dream_processed_through`; estables se saltean.
   Tope `budget`/día con arrastre. La daily nunca "revisa todo" (§6).
4. **Retiro de la autoría cruda del 2h:** al enrutar por `skills_store`, **se
   elimina** el `WriteFileTool` crudo de Phase 2 — una sola vía sancionada de
   escritura de skills.

**Abiertas:** ninguna. Spec listo para `writing-plans` (Parte A).

---

## §13 — Referencias

- Plan-fuente: [`docs/archive/skills_evolutivas.md`](../../archive/skills_evolutivas.md)
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
