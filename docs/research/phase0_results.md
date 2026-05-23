# Phase 0 — Results

> Resultados empíricos de los experimentos de Phase 0 (doc 19 §2).
> Documento vivo — se actualiza con cada sub-fase de Phase 0.

---

## Phase 0.0 — Smoke tests

**Fecha**: 2026-05-23

**Aproximación**: validación unit + spin-up live gateway. No se hizo
clickthrough en TUI/web por costo bajo y unit tests robustos.

**Resultados**:

- Backend: 4171 passed, 15 skipped, 0 failed
- Webui: 142 passed
- Doctor: ✓ python, config, providers, git, extras, services
- Gateway: start/stop clean, webui SPA responde 200
- `durin config get memory.embedding.model` → default correcto LIVE

**Decisión**: pasar a Phase 0.1. Smokes interactivos pendientes
verificación visual quedan delegados a uso real.

---

## Phase 0.1 — Embedding name variations

**Fecha**: 2026-05-23

**Script**: `scripts/test_embedding_name_variations.py`

**Modelo testeado**: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`

**Asunción A1**: embeddings acercan razonablemente variaciones de nombre.

### Resultados (22 pares testeados)

| Categoría | Pass/Total | Observación |
|---|---|---|
| case (Marcelo/marcelo) | 3/4 | **Inconsistente** — `durin/Durin` 0.931, `María/maria` 0.936, `mxHero/mxhero` 0.890, pero `Marcelo/marcelo` solo 0.719 |
| truncate (Marcelo M./Marcelo Marmol) | 3/3 | Bien — 0.73-0.81 |
| nickname (Marcelo/Marcelito) | 1/1 | 0.705 (threshold 0.50) |
| email (mmarmol@mxhero.com vs Marcelo Marmol) | 0/2 | **CRITICAL FAIL** — 0.271 y 0.382 |
| project_slug | 1/3 | `durin/durin-agent` 0.755 ✓; `project:durin/durin` 0.517 ✗ |
| desc (descripción larga vs nombre) | 0/1 | 0.229 — entendible, descripción tiene mucho más contexto |
| multilang (es↔en, ja↔en, zh↔en) | 4/4 | Excelente — > 0.97 todos |
| negative (entidades distintas) | 4/4 | Bien — < 0.47 todos |
| **Total** | **16/22** | 73% match threshold |

### Hallazgos críticos

1. **A1 falla en case variations de persona específica**. `Marcelo/marcelo`
   da 0.719 (por debajo del 0.85 esperado), pero `durin/Durin` y
   `María/maria` lo hacen bien. La variación no es uniforme — depende del
   nombre específico y de cómo el tokenizer del model lo descompone.

2. **A1 falla totalmente en email/name cross-form**. `mmarmol@mxhero.com`
   vs `Marcelo Marmol` da 0.271 — claramente debajo del threshold mínimo
   crítico de 0.30. **Embeddings no resuelven cross-system identity**.

3. **`type:slug` form no se acerca a `slug` bare**. `project:durin` vs
   `durin` da 0.517 — el prefijo `type:` introduce token noise. Si el
   indexer embed pages con `project:durin` literal, queries por `durin`
   no las van a encontrar bien.

4. **Multilingüe excelente**. CJK ↔ English supera 0.97 consistentemente.
   Esto es buena noticia para el caso cross-profession en empresas
   multilingual (mxhero corpus en es/en).

### Decisiones que esto fuerza

- **Doc 18 §7 L1 light**: alias_index sidecar pasa de "necesario" a
  **bloqueante día 1, crítico**. Sin él, el sistema no funciona para
  casos básicos de identidad.
- **Doc 18 §7 retrieval**: cuando se embed una entity page para vector
  index, **NO incluir el `type:` prefix en el texto embedded**. Embed
  solo el `name + aliases + body`. El tipo va como metadata estructural,
  no como tokens.
- **Doc 18 §7 G3 dedup pipeline**: la pieza "LLM para zona gris" es
  crítica para resolver email↔name. El embedding solo no puede.
- **A1 no se sostiene tal como estaba enunciada**. Refinada: "embeddings
  ayudan con variations *cuando los tokens del original están preservados*
  (truncamiento, multilingual semantic, lowercase de algunos nombres),
  pero **no ayudan con transformaciones radicales** (case de algunos
  nombres, email forms, slug prefixes)".

### Costo

- Tiempo: ~10 minutos (script + run).
- LLM cost: $0 (solo embeddings locales).
- Valor del hallazgo: alto — refinó 3 decisiones de diseño con evidencia.

### Próximo paso

Phase 0.2 (telemetría baseline) se difiere — ver abajo.

---

## Phase 0.2 — Telemetría baseline — DIFERIDA

**Fecha**: 2026-05-23

**Estado actual del sistema**:

- `memory.enabled = false`
- 0 memory docs en vector index
- 6 sessions sin entries memorizadas

**Implicación**: el ranker vector NO está ejecutándose. No hay qué loggear.
La instrumentación propuesta en doc 19 §0.2 captura `query/candidates/
scores/entities_in_entry`, pero:

1. Sin `memory.enabled=true`, vector_index ni se invoca.
2. Sin propuesta A implementada (Phase 1.1), las entries no tienen
   `entities` en frontmatter — el bucket "tag matcheado pero NO
   retornado" siempre estaría vacío.

**Decisión**: defer Phase 0.2 hasta después de Phase 1 (propuesta A +
foundations). Cuando el sistema tenga entity tags en entries + vector
index activo, la instrumentación capturará información significativa.

**Modificación al plan original (doc 19 §0.2)**: el orden cambia a:

1. Phase 0.3 (dream dry-run manual) — ahora.
2. Phase 1.1 (propuesta A) — luego.
3. Phase 0.2 instrumentación + acumulación — después.
4. Continuar con Phase 2.

Esto no rompe el plan; solo reordena con honestidad sobre dependencias
reveladas. Doc 19 §12 ya marcó Phase 0.2 con incertidumbre media.

---

---

## Phase 0.3 — Dream dry-run con corpus real

**Fecha**: 2026-05-23

**Script**: `scripts/dream_dryrun.py`

**Modelo**: `glm-5.1` via z.ai coding plan (`api_base:
https://api.z.ai/api/coding/paas/v4`).

**Asunciones testeadas**: A2, A3, A4, A9.

### Setup

- **Fuente de entries**: openclaw-aule corpus (doc 19 §13 Fuente A).
  Script extrae líneas "Candidate: ..." de los archivos diarios,
  filtra por mención de la entidad target.
- **3 entidades testeadas**:
  - `project:mxhero` (30 entries)
  - `person:marcelo` (30 entries)
  - `topic:helpjuice` (20 entries)

### Resultados

| Entidad | Entries | Latencia | Output chars | Veredicto |
|---|---|---|---|---|
| project:mxhero | 30 | 36.6s | 4089 | ✓ Coherente, dedupa "carried forward across dream sessions", trailers parseables |
| person:marcelo | 30 | ~30s | ~4000 | ✓ Background organizado, identifiers extraídos sin pedir |
| topic:helpjuice | 20 | ~25s | ~3000 | ✓ Eligió tabla markdown para sync history sin pedir |

### Hallazgos clave

1. **A2/A3/A4 sostienen** con glm-5.1: el modelo produce páginas
   coherentes en markdown libre + commit messages estructurados con
   trailers parseables. La temporalidad en prosa funciona (e.g.,
   "Updated around 2026-04-12").

2. **Emergencia de campos no pedidos**: en el run de `person:marcelo`,
   el LLM agregó `identifiers: { slack_sender_id: UM7TCSZRN }` al
   frontmatter sin que se le pidiera. Indica que el modelo entiende
   "identidad cross-system" intuitivamente. Esto valida la apuesta de
   "vocabulario abierto + dejar emerger" del doc 18.

3. **Tablas markdown emergen orgánicamente**: para `topic:helpjuice`,
   el LLM eligió tabla para la sync history (columnas Date, Articles,
   New, Updated, Errors, Notes). No se le pidió formato; eligió la
   forma natural para el contenido.

4. **Tipos extendidos espontáneamente**: el LLM creó tipos no listados
   en los 8 del doc 18 §4 (e.g., `agent:sam`, `agent:aule`, `org:henngo`)
   en el campo `Entities-referenced`. Valida vocabulario abierto pero
   también marca que el dream extenderá tipos sin freno — Phase 1
   tendrá que decidir si validar contra whitelist o aceptar.

5. **Costo**: ~3000-4000 tokens input / 1000-1500 tokens output por
   consolidación. Para Haiku equivalente: ~$0.005 per consolidation.
   Para glm-5.1 (coding plan): subscription incluida. Ambos bien debajo
   del threshold $0.10/sesión (asumiendo ~10 entidades por session =
   ~$0.05).

6. **Latencia**: 25-37s per consolidación con glm-5.1. Para una session
   con 10 entidades nuevas, dream tomaría ~5-6 min. OK como background
   async, no UX-blocking.

### Hallazgos para Phase 2.3 (vertical slice)

Cosas que el dry-run reveló y que el dream real debe manejar:

1. **`dream_processed_through`**: el LLM puso el ID textual de la
   última entry (`2026-04-13-004`). En el runtime real, este field
   debe ser msg_idx numérico inyectado por el código que llama al
   prompt, no producido por el LLM.

2. **Sources con rangos**: el LLM usó rangos `[A] through [B]` para
   compactar referencias. Drill-down (Phase 4.4 `memory expand`)
   necesita resolver estos rangos a IDs específicos.

3. **Entities-referenced fuera del set**: el dream crea tipos no
   previstos (`agent:`, `org:`). Phase 1.1 (propuesta A schema
   validation) debe decidir: aceptar todos, whitelist con warning, o
   normalizar.

4. **Identifiers emergentes**: el LLM ya los extrae. Phase 5 absorción
   debe unionear identifiers cuando merge entidades.

### Decisiones aplicadas a doc 18 post-0.3

- **§3.2 Frontmatter mínimo**: removido el ejemplo prescriptivo de
  `identifiers: {email: [], phone: [], ...}`. Dejado solo el shape
  base (`type`, `name`, `aliases`, `dream_processed_through`,
  `created_at`, `updated_at`). Fields emergentes esperados y
  bienvenidos.
- **Constraints forward-looking**: agregadas dos notas — Phase 5 debe
  unionear arrays/listas en merge; UI futura (doc 20 P4) bindings al
  frontmatter sugiere preservar structure cuando emerja.

### Artifacts

- `scripts/dream_dryrun.py` — script de extracción + invocación
- `durin/templates/dream/consolidator.md` — prompt v1 versionado
- `docs/research/dream_dryrun_project_mxhero.md` — output completo
- `docs/research/dream_dryrun_person_marcelo.md` — output completo
- `docs/research/dream_dryrun_topic_helpjuice.md` — output completo

### Próximo paso

Phase 1: foundations. Empezar por propuesta A (typed entities en
entries) + git substrate. Phase 0.2 baseline se cubre después de
Phase 1 cuando hay entries con tags + memory.enabled=true.

---

## Last updated: 2026-05-23 (Phase 0.0, 0.1, 0.2 diferida, 0.3 done)
