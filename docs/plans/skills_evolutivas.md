# Plan — Skills evolutivas (crear · importar · adaptar · evolucionar)

> 🧭 **MAPA / VISIÓN (vigente).** Roadmap de etapas E1→E5 — el norte. El diseño
> de *lo que se construye ahora* (E2) vive en su spec:
> [`superpowers/specs/2026-06-02-skills-evolution-e2-design.md`](../superpowers/specs/2026-06-02-skills-evolution-e2-design.md).
> Este doc es fuente de verdad de la **visión**; para el **estado y diseño actual**, ese spec manda.

> **Fuente de inicio**, no decisión final. Captura la discusión sobre darle a
> durin un sistema de skills que el usuario crea bajo demanda, importa de
> marketplaces, y que evolucionan con el uso — estilo Hermes, con la
> disciplina de mecanismo de Microsoft SkillOpt. Marca explícitamente los
> puntos abiertos para resolver en sus fases. No reemplaza un plan de
> implementación por fases; lo antecede.
>
> Refs externas al pie. Refs internas: `docs/research/16a_entities_in_cloned_systems.md`
> (Hermes modela skills como entidades con lifecycle state machine).

---

## Estado de implementación (2026-06-04, verificado contra código)

**Construido:** crear/cristalizar (§6.A — dream + `curate_catalog`) · importar (§6.B)
+ piso §8.C + gate · interop agentskills.io · Skills-Surface · retrieval Nivel-1
(índice + hot-tier) · **discovery/registries** (skills.sh + clawhub: search,
resolución name-match, CLI, web — spec `2026-06-03-skill-discovery-registries-design.md`)
· **drift→evolución §8.D** (`durin/agent/skill_drift.py` + `curate_catalog`) ·
**barrida origen-no-verificado → cuarentena** (`durin/agent/skill_lifecycle.py`:
un skill de workspace sin provenance se reubica a cuarentena al cargar el contexto
+ surfaces → inerte para el agente, surfaceado para el humano) · **§6.C adquirir-on-gap**
(search→gate→semilla: in-session interactivo + dream phase-2 autónomo safe-only —
spec `2026-06-03-skill-acquire-on-gap-design.md`, PR #25).

**Pendiente:** **P6** (ejecutor de install-specs / sandbox runtime — hoy info-only,
policy `never`) · adapters extra de discovery (github-taps / well-known / lobehub).

**Descartado / resuelto (2026-06-04):**
- **§6.D completo (Etapa-1 traducción + Etapa-2 optimizer) ≡ §8.F GEPA/SkillOpt —
  DESCARTADO.** *Etapa-2 / optimizer:* la evolución por contenido ya la hace
  `curate_catalog`; la señal de uso (replay/canary) es demasiado escasa en skills
  personales para ser un reward (§4 ya lo difirió). GEPA-el-algoritmo y SkillOpt-el-harness
  (benchmark/train-val) no aplican sin benchmark; su única parte útil (bounded edit +
  rollback) ya es git. *Etapa-1 / traducción a tools nativas:* durin no tiene superficie
  de tools suficiente para que un skill importado referencie comúnmente tools que durin
  no tenga — no justifica una pasada de adaptación. Nada de §6.D aporta sobre lo que hay.
- **§5.2 capa `original/` — RESUELTO por git.** El contenido tal-como-llegó ya vive en el
  commit de import del GitStore (inmutable, diffeable, incluso para fuentes
  no-refetcheables); HEAD = actual; `git diff import..HEAD` = el delta. **No hay capa/dir
  que construir.** Con §6.D descartado tampoco queda flujo de re-adaptación que consuma un
  `original/` explícito. El invariante (import = commit pristino; la curación no toca
  imports porque son `mode=manual`) ya se cumple.

## §1 — La idea

durin debe dar al usuario las herramientas para que, **mientras usa durin**:

1. **Cree** skills bajo demanda (incluido código).
2. **Importe** skills de marketplaces remotos, conservando origen y original.
3. **Adapte** lo importado a las herramientas propias de durin.
4. **Evolucione** esas skills con el uso, registrando cada decisión y su
   porqué, reversible.
5. **Adquiera** skills de repos remotos por iniciativa propia cuando enfrenta
   algo complejo y no tiene skill — buscando varias, comparándolas, y
   usándolas como semilla.

Es la forma de **aprender comportamiento**: no repetir desde cero lo que ya
se resolvió. El modelo de referencia es **Hermes** (creación experiencial +
evolución en uso); **SkillOpt** entra como *disciplina de cómo editar el
documento sin romperlo*, no como su harness de entrenamiento.

---

## §2 — Concepto unificado: skill = plugin

En durin **skill y plugin son el mismo concepto**. Una skill es un
procedimiento multipaso (pasos para hacer X de manera Y/Z). Un "plugin" es
simplemente la skill que cruzó el gradiente y ahora **carga/instala/ejecuta
código**.

Consecuencia: una sola entidad, una sola tubería. "Instala código" no es otro
tipo de objeto — es el **flag de capacidad de mayor riesgo** de una skill, y
es justo lo que el piso de seguridad (§8.C) vigila. La evolución cruza ese
gradiente sola: cuando una skill automatiza una tarea repetitiva con un
script, *crece código* y se vuelve plugin.

---

## §3 — Cómo lo hace Hermes (modelo de referencia)

Hermes tiene **dos capas** de evolución de skills:

**Capa A — online, dentro del loop.** Evolución barata y continua:
- **Auto-creación:** tras una tarea compleja (reportado por terceros como
  ≥5 tool calls — *no confirmado en doc oficial*), el agente genera un
  `SKILL.md` capturando procedimiento, *pitfalls* y pasos de verificación.
- **Loop:** Observe → Plan → Execute → Evaluate → **Crystallize Skill** →
  Reuse. Evalúa si el workflow vale la pena guardar; si sí, lo retiene.
- **Refinamiento en uso:** si durante la ejecución encuentra un mejor enfoque
  que el que describe la skill, edita el doc sin que se lo pidan.
- Sin score ni validation set. Experience-driven.
- Formato bajo estándar abierto (`agentskills.io`), portable/compartible vía
  Skills Hub. Importa de OpenClaw a `~/.hermes/skills/openclaw-imports/`.
- **Skills como entidades con lifecycle state machine** (`agent/curator.py`,
  `tools/skill_usage.py`, `agent/background_review.py`) — ver `16a`.

**Capa B — offline, batch.** Optimización con score real vía **DSPy + GEPA**
(Genetic-Pareto), repo aparte `hermes-agent-self-evolution`:
- Lee trazas de ejecución para entender *por qué* falla, no solo *que* falló.
- Loop: leer config → generar dataset → mutar (trace-aware) → evaluar →
  gates (tests, tamaño, benchmarks) → seleccionar → PR.
- Fasado: Fase 1 `SKILL.md` (hecho) → tool descriptions → system prompts →
  código de tools (*Darwinian Evolver*).
- Sin GPU, solo API, ~$2-10/run, CLI offline.

**Retrieval context-efficient:** Hermes busca skills e inyecta **solo las
necesarias, solo cuando se necesitan**. Clave para eficiencia de contexto.

---

## §4 — Cómo entra SkillOpt (Microsoft)

SkillOpt trata **el documento de skill como el "estado entrenable" de un
agente congelado**. Loop: rollouts puntuados → un *modelo optimizador*
propone ediciones acotadas `add/delete/replace` → **se aceptan solo si suben
un score de validación held-out** (nunca degrada). Estabilizadores tipo DL:
learning-rate textual + cosine decay + rejected-edit buffer + slow/meta
updates por época. Deploy = un `best_skill.md` estático, cero costo en
inference. Código MIT corrible (backend Anthropic incluido). Bate a GEPA en
las 52 celdas del paper.

**Qué aporta a durin — y qué no:**

| | |
|---|---|
| **SkillOpt-como-harness** (train/val splits, batch, reward checkeable) | **No encaja** para skills personales on-demand: el usuario no tiene benchmark |
| **SkillOpt-como-disciplina** (bounded edit + gate + rollback + rejected-buffer) | **Sí suma**: tapa los huecos que deja la evolución estilo Hermes (drift, bloat, sobreescritura ciega) |

**Síntesis clave:** lo que SkillOpt llama *versionado + rollback +
rejected-edit buffer* **es literalmente git** — el historial es el buffer de
rechazos, el rollback es `git revert`, el "por qué" es el commit message.
durin **ya corre git para memoria**; no hay que construir el mecanismo.

Y el *validation set* deja de ser un benchmark: en durin la señal es **el uso
real** (replay contra invocaciones pasadas exitosas / canary en los próximos
N usos / feedback del usuario). El gate monótono estricto se vuelve **gate
blando**: requiere corroboración, default = conservar versión vieja, usuario
puede vetar. Se cambia "nunca degrada" por "raramente degrada y siempre
reversible" — trade-off correcto para producto.

---

## §5 — El modelo durin

### 5.1 — Tres fuentes-semilla, una tubería

```
FUENTES (semilla)                        TUBERÍA ÚNICA
importar (inicia usuario) ───────┐
crystallize (desde experiencia) ─┼─→ adapted/ → git-tracked → evoluciona → retrieval
adquirir-on-gap (remoto, ────────┘
  confirmado por usuario)
```

Las tres terminan iguales: versión adaptada, versionada en git, que evoluciona
y se recupera bajo demanda. **Solo las distingue la `provenance`**:
`source = "marketplace:agentskills.io/..."` · `"experience:2026-06-01"` ·
`"experience+remote[hub/x, hub/y]"`. No son tres sistemas — son tres
*entradas* a uno.

### 5.2 — Tres capas por skill

- `original/` — inmutable, tal como llegó (formato abierto). No se toca.
- `provenance` — de qué marketplace, URL, fecha, hash del original (o
  `experience` si es auto-creada).
- `adapted/` — la que durin **realmente usa**: traducida a sus tools, con
  scripts donde había repetición. La única que evoluciona.

> **Corrección (2026-06-04):** `original/` **no es una capa/dir a construir** — el
> contenido tal-como-llegó **ya está preservado** en el commit de import del GitStore
> de skills (`skill(name): import from <source>`, `skills_import.py`), inmutable y
> diffeable, **incluso para fuentes no-refetcheables** (se commitea el contenido, no
> sólo el hash). Sobre git, las "capas" son: **commit de import = `original`**,
> **HEAD = actual**, **`git diff import..HEAD` = el delta**. **Resuelto (2026-06-04):
> no hay nada que construir** — git ya da inmutabilidad + diff + rollback + historial,
> incluso para fuentes no-refetcheables. Y con **§6.D descartado** no queda flujo de
> re-adaptación que necesite un `original/` explícito (es `curate_catalog` quien
> evoluciona, sobre git). El único invariante —import = commit pristino; la curación no
> toca imports porque son `mode=manual`— ya se cumple.

### 5.3 — Pipeline de dos etapas

- **Etapa 1 — traducción mínima (siempre):** hacer que corra en durin
  (mapear tools, arreglar paths, lo imprescindible). Piso obligatorio.
- **Etapa 2 — evolución (opcional):** hacia el **reward** = *tools-nativas
  primero + automatizar lo repetitivo con bash/python*. Es checkeable
  localmente (detectar loops manuales, tools genéricas con equivalente
  nativo). **El opt-out "importar sin evolucionar" = parar tras la Etapa 1.**

### 5.4 — Git como columna vertebral

Reutiliza el git de memoria. Cada cambio/decisión = commit con rationale.
Rollback = `git revert`. Buffer de rechazos = historial. Permite siempre ir
para atrás o cuestionar decisiones.

### 5.5 — Retrieval de dos niveles (eficiencia de contexto)

- **Nivel 1 — local:** skills indexadas en el motor de búsqueda de memoria
  (vector + FTS5 + RRF). Inyección **híbrida**: las `always:true` quedan en
  el tier cacheado (piso), la cola larga se recupera **on-demand**. El flag
  `always` ya existente es la línea divisoria.
- **Nivel 2 — remoto:** al fallar el nivel 1 en una tarea compleja, federar
  la búsqueda a marketplaces (§6.C). Caro, red, runtime.

### 5.6 — Meta-skill (el sistema se auto-hospeda)

El proceso de buscar/adquirir/preguntar **es a su vez una skill propia** que
mejora y se adapta al usuario (qué/cómo/cuándo preguntar):
- Sigue el mismo esquema `original(builtin)/adapted/provenance`. La semilla
  inmutable es el builtin de durin (descendiente del `skill-creator` actual).
- **Procedimiento en la skill; preferencias aprendidas en `USER.md`** (que
  Dream ya gestiona observando qué aprobó/rechazó el usuario). Equivale al
  "Honcho user modeling" de Hermes con piezas que durin ya corre.
- **Dos capas dentro del meta-skill:**
  - *Afinable* (evoluciona libre): estilo de interrupción, default
    merge-vs-single, verbosidad de candidatos, cuándo molestar en cosas
    reversibles.
  - *Piso invariante* (NO puede optimizar para abajo): ver §8.C.

---

## §6 — Flujo de trabajo (etapas end-to-end)

### A. Crear / cristalizar (desde experiencia)
1. Detectar candidato (trigger: §8.A).
2. Autorear `SKILL.md` con procedimiento + pitfalls + verificación. Si la
   tarea es mecánica/repetitiva → emitir script (skill→plugin).
3. Etapa 1 (ya nativa) → commit con rationale.

### B. Importar (inicia el usuario)
1. Traer original + provenance.
2. Etapa 1 (traducción mínima) → commit.
3. Si el usuario quiere evolución → Etapa 2; si no → congelar.

### C. Adquirir-on-gap (inicia el agente, confirma el usuario)
1. Tarea compleja ∧ miss en nivel 1 ∧ "huele estándar".
2. Federar búsqueda a marketplaces → traer **varios** candidatos.
3. **Avisar al usuario y pedir confirmación con `AskUserQuestion`:**
   - candidatos como items (de dónde, cómo);
   - **opinión** (opción recomendada primero);
   - opciones: *merge de A+B y adaptar* / *usar A* / *usar B* / *otra*;
   - **antes de integrar: avisar qué candidatos exigen instalar
     herramientas** y cuáles, para que el usuario decida;
   - usar `preview` para comparar contenidos lado a lado.
4. Sintetizar desde la(s) semilla(s) elegida(s) → Etapa 1 → commit.

### D. Evolucionar (en uso)
1. Edición acotada hacia el reward (§5.3).
2. Gate blando contra señal de uso (replay / canary / feedback).
3. Aceptar o `git revert`. Todo commiteado con porqué.

### E. Recuperar (cada turno)
Nivel 1 local híbrido; nivel 2 remoto solo on-gap (→ C).

---

## §7 — Reuso de lo que durin ya tiene

| Pieza durin | Rol en este plan |
|---|---|
| **Sueño 2h** (`Dream`, `agent/memory.py`) | Aprende comportamiento (SOUL/USER/MEMORY); ya autorea skills (crudo). E2 → autoría/parche **local** vía `skills_store` |
| **Sueño diario** (`DreamConsolidator`, `memory/dream.py`) | Consolidación exhaustiva de entidades. E2 → **curación global** de skills (fusionar/unificar) |
| **SkillsLoader** (`agent/skills.py`) | Carga, frontmatter, flag `always` (= divisoria de retrieval) |
| **ContextBuilder** (`agent/context.py`) | Punto de inyección; mover cola larga de stable→on-demand |
| **git de memoria** | Versionado/rollback/rationale de skills (= mecanismo SkillOpt) |
| **search pipeline** (`memory/search_pipeline.py`) | Motor de retrieval nivel 1; corpus de skills |
| **USER.md** (Dream-managed) | Preferencias aprendidas del meta-skill |
| **builtin `skill-creator`** | Semilla inmutable del meta-skill |
| **`AskUserQuestion`** | Flujo de confirmación de adquisición (§6.C) |

---

## §8 — Puntos abiertos (resolver en sus fases)

- ~~**A. Trigger de cristalización.**~~ **RESUELTO (E2, 2026-06-02, tras
  auditoría de código).** No es eager-vs-lazy: es **híbrido por reparto entre los
  dos sueños existentes**. El de **cada-2h** (aprende comportamiento, ya autorea
  skills) hace autoría/parche **local rápido**; el **diario** (consolidación
  exhaustiva) hace la **curación global** (fusionar/unificar). La señal de uso se
  registra como `skill_calls` (read/edit/create + ancla de turno) en el bloque
  `derived` del `.meta.json` de la sesión — **durable** (el meta no se capa),
  porque lo que el sueño lee de `history.jsonl` son resúmenes LLM y la señal cruda
  se perdería. Toda escritura pasa por el `skills_store` de E1. Ver
  [`2026-06-02-skills-evolution-e2-design.md`](../superpowers/specs/2026-06-02-skills-evolution-e2-design.md)
  (§2 reparto, §4 señal, Apéndice A evidencia).

- ~~**B. Estándar de interop.**~~ **RESUELTO (2026-06-03).** Se adopta
  **[agentskills.io](https://agentskills.io/specification)** — el estándar abierto
  al que TODO el ecosistema convergió (Hermes, OpenClaw, Pi, Claude Code, Codex,
  Cursor, 30+ tools, 490k+ skills; agentskills.io ES la forma abierta/gobernada
  del formato de Anthropic, no su producto). durin = core estándar en root +
  comportamiento bajo `metadata.durin.*` + **fidelidad de round-trip** (preserva
  frontmatter ajeno) → import es casi un no-op. Diseño + porqué:
  [`docs/superpowers/specs/2026-06-03-skill-interop-standard-design.md`](../superpowers/specs/2026-06-03-skill-interop-standard-design.md);
  contrato canónico: [`docs/architecture/skills/01_format_and_interop.md`](../architecture/skills/01_format_and_interop.md).
  Implementado (round-trip + `platforms` + spellings + version/license) y
  verificado live con skills reales de Hermes. El import (§6.B) es el siguiente plan.

- ~~**C. Piso de seguridad invariante**~~ **RESUELTO (§8.C, 2026-06-03).** Piso
  determinista (`scan_skill`) + gate `decide_action` enforced en código
  (`install_imported_skill`): `dangerous` → block (necesita override explícito);
  trae código / `caution` / fuera-de-allowlist → confirm. Allowlist
  user-configurable; juez LLM opt-in (cap=caution, nunca bloquea solo). El
  meta-skill no puede bajarlo: el gate vive en el install (código), no en el prompt.

- ~~**D. Drift de upstream.**~~ **RESUELTO (§8.D, 2026-06-03).** El skill NUNCA se
  pisa — se **evoluciona**, no se reemplaza. `check_upstream_drift`
  (`durin/agent/skill_drift.py`) re-fetchea `provenance.source`, escanea (§8.C) y
  compara `content_hash`; el gate `decide_action` decide: `allow` (safe + sin código
  + fuente confiable) → el pase diario de dream (`curate_catalog`) mete el upstream
  como contexto y el juez lo **incorpora vía `evolve`** (merge quirúrgico que
  preserva lo local — validado live con el modelo real); `confirm`/`block` (código /
  caution / fuera-de-allowlist / dangerous) → gate humano, nunca se auto-funde
  código no-confiable. La "política de merge" = el juez del pase diario, gateado por
  §8.D.

- **E. Latencia de adquisición.** §6.C ocurre mid-tarea. Política:
  ¿bloquear / intentar one-shot y escalar / proponer? (Resuelto parcialmente:
  se propone al usuario, pero falta el umbral de cuándo escalar.)

- **F. ¿GEPA, SkillOpt, o ambos?** Para la eventual Capa B con score (donde
  exista reward: código con tests, adaptación checkeable). Coarse-to-fine
  posible: GEPA explora/adapta (trace-aware, multi-objetivo Pareto) →
  SkillOpt pule/mantiene sano (gate monótono). Decisión diferida; el
  substrato compartido (skill-slot + scorer + atribución + edit/rollback) es
  lo que paga primero.

---

## §9 — Referencias

- SkillOpt — <https://github.com/microsoft/SkillOpt> · paper arXiv:2605.23904
  · <https://microsoft.github.io/SkillOpt/>
- GEPA / Hermes self-evolution — <https://github.com/NousResearch/hermes-agent-self-evolution>
- Hermes Agent — <https://github.com/nousresearch/hermes-agent> ·
  <https://hermes-agent.nousresearch.com/docs/>
- ProcMEM (procedural memory desde experiencia) — arXiv:2602.01869
- Interno — `docs/research/16a_entities_in_cloned_systems.md`
