# 19 — Plan de implementación + validación empírica

> Implementación step-by-step del modelo entity-centric definido en doc 18.
> Cada fase incluye (a) qué se construye, (b) qué asunción de diseño se
> testea, (c) cómo se valida end-to-end, (d) criterio de pass/fail, (e)
> qué reabrir en doc 18 si falla.
>
> Premisa: durin no se usa en producción hasta cerrar todas las fases.
> Esto da libertad para ordenar por **dependencia técnica y aprendizaje**,
> no por valor incremental al usuario. La validación se hace contra
> datasets sintéticos + corpus durin acumulado, no contra uso real.

---

## §1 — Aproximación general

### Estructura

Cada fase es **build + validate + decide**:

1. **Build**: implementar lo planificado.
2. **Validate**: ejecutar el test plan end-to-end de la fase.
3. **Decide**: pasar a la próxima fase, ajustar diseño, o reabrir doc 18.

### Asunciones de diseño (master list)

Las preguntas empíricas que el plan testea:

| # | Asunción | Fase que la testea |
|---|---|---|
| A1 | Los embeddings actuales (paraphrase-multilingual-MiniLM-L12-v2) acercan razonablemente variaciones de nombre (`Marcelo`/`marcelo`/`Marcelito`) | Phase 0.1 |
| A2 | El LLM genera consolidaciones markdown coherentes que preservan facts + temporalidad + sources cuando se le da N entries y la página actual | Phase 0.3, Phase 2 |
| A3 | El LLM produce commit messages estructurados con trailers (Sources, Entities-touched, etc.) en formato parseable | Phase 0.3, Phase 2 |
| A4 | Markdown libre + prose ("previously X / now Y") es suficiente para expresar temporalidad sin schema YAML | Phase 2 |
| A5 | Read-time reconciliation funciona: el LLM lee página consolidada + entries post-cursor coexistentes y reconcilia correctamente | Phase 3 |
| A6 | L1 light (alias expansion + boost/demote por cursor) mejora retrieval significativamente sobre vector search puro | Phase 3 |
| A7 | `git log` + commit messages estructurados proveen el "why" útilmente al hacer drill-down | Phase 4 |
| A8 | Anti-fragilidad real: si dream no corre por N días, el sistema sigue funcionando (corpus crece, no se rompe) | Phase 6 |
| A9 | Los 8 tipos amplios (`person/place/project/topic/event/artifact/stance/practice`) cubren cross-profession sin forzar | Phase 2, Phase 6 |
| A10 | Trigger heuristic produce consolidaciones útiles (no premature, no redundant) | Phase 2 |
| A11 | Absorción de aliases via archive subfolder preserva traceability sin contaminar la jerarquía visible | Phase 5 |

Cada asunción se testea con criterio explícito de pass/fail.

### Cuándo reabrir doc 18

Si en cualquier fase un test falla y la asunción correspondiente no se sostiene:

1. Documentar qué falló y por qué (en bitácora).
2. Pausar la fase.
3. Revisar la sección relevante de doc 18 con la evidencia nueva.
4. Decidir: ajustar diseño, ajustar test, o aceptar limitación.
5. Reanudar con doc 18 actualizado.

---

## §2 — Phase 0: Pre-implementación — validación de asunciones baratas

Antes de cualquier código de producción, testear las asunciones más
testeable con costo mínimo. Esto reduce el riesgo de construir sobre
premisas incorrectas.

### 0.0 — Smoke test del estado actual

**Asunción testeada**: ninguna nueva — verificar que el trabajo
commiteado (embedding catalog, telemetría composition, /status web,
render_as, slash picker, config get) sigue funcionando antes de tocar
nada nuevo.

**Build**:

Suite de smoke tests que ejercita los 4 commits recientes:

- Smoke 1: `durin install` en `/tmp/durin-smoke/` → wizard llega al
  final sin error con un embedding model conocido.
- Smoke 2: arrancar TUI + escribir mensaje → footer muestra
  `cache:X% · conv:Y% · infra:Z%`.
- Smoke 3: `/status` en TUI → output formateado con sección de
  composition correcta.
- Smoke 4: web `/status` → renderiza verbatim (pre con whitespace
  preserved), no flattened.
- Smoke 5: slash picker en web → tipear `/` muestra TODOS los
  comandos, navegación con flechas scrollea.
- Smoke 6: `durin config get memory.embedding.model` → devuelve el
  default desde schema, no error.
- Smoke 7: `durin memory ingest <dir>` → genera entries + vector
  embeddings con el modelo nuevo.

**Validate**:

- Run los 7 smokes en orden, manual o scripted.
- Cada uno tiene pass/fail binario.

**Pass criteria**:

- 7/7 pasan.

**Fail criteria → bloqueante**:

- Si falla cualquiera, NO avanzar a Phase 0.1. Fix primero.

**Duración estimada**: 1 hora si todo está bien; +tiempo de fix si no.

### 0.1 — Test de embedding de variaciones de nombre

**Asunción testeada**: A1

**Build**:

Script standalone `scripts/test_embedding_name_variations.py` (~30 LOC):

```python
from durin.memory.embedding import FastembedProvider
import itertools

PAIRS = [
    # nombres comunes en durin
    ("Marcelo", "marcelo"),
    ("Marcelo", "Marcelito"),
    ("Marcelo Marmol", "mmarmol@mxhero.com"),
    ("Marcelo Marmol", "Marcelo M."),
    # proyectos
    ("durin", "durin-agent"),
    ("durin", "el proyecto que estamos construyendo"),
    ("project:durin", "Durin"),
    # mismos cross-profession
    ("María", "maria"),
    ("María García", "mgarcia@..."),
    # contraejemplos (deben dar bajo similarity)
    ("Marcelo", "María"),
    ("durin", "hermes"),
    ("python", "javascript"),
]

provider = FastembedProvider("paraphrase-multilingual-MiniLM-L12-v2")
for a, b in PAIRS:
    sim = cosine(provider.embed([a])[0], provider.embed([b])[0])
    print(f"{a:40s} | {b:40s} | {sim:.3f}")
```

**Validate**:

- Run el script, recolectar matriz de similarities.
- Generar reporte: histograma de matches positivos vs contraejemplos.

**Pass criteria**:

- Variaciones lowercase/case (Marcelo/marcelo): sim > 0.85.
- Variaciones de truncamiento (Marcelo/Marcelo M.): sim > 0.70.
- Cross-form email/name: sim > 0.50 (baja pero detectable).
- Contraejemplos (Marcelo/María): sim < 0.50.

**Fail criteria → reabrir**:

- Si email/name (mmarmol vs Marcelo) da sim < 0.30: doc 18 §7 L1 light necesita marcar alias expansion como **crítico bloqueante**, no opcional.
- Si lowercase variants dan sim < 0.75: el embedding model elegido no sirve; reevaluar.

**Duración estimada**: 1 hora.

### 0.2 — Telemetría baseline de retrieval

**Asunción testeada**: A6 (línea base — sin entity-aware queremos saber dónde estamos)

**Build**:

Instrumentación minimalista en `durin/memory/vector_index.py`:

```python
# Por cada query, loggear a memory/.telemetry/retrieval.jsonl:
{
  "ts": "2026-...",
  "query": "<text>",
  "k": 5,
  "candidates": [
    {"id": "...", "score": 0.87, "entities_in_entry": [...]}
  ],
  "session_id": "...",
}
```

No cambia behavior; solo loggea. ~50 LOC + un test que verifica que loggea.

**Validate**:

Dos caminos paralelos para acumular evidencia:

- **(a) Uso real** del autor por 1-2 semanas (memoria viva, sesiones
  normales). Ideal pero depende del ritmo de uso real.
- **(b) Acumulación scripted** — replay sintético de ~150 queries
  típicas (preguntas sobre proyectos, personas, decisiones) contra
  el corpus actual. Cubre gap si el uso real no acumula suficiente.
  Las queries se construyen a partir de los corpus de
  `openclaw-aule/workspace*/memory/` (ver §13 fuentes de datos).

**Pass criteria**:

- El log se escribe sin errores.
- ≥ 100 queries acumuladas (via a o b o ambos).
- Las queries quedan recuperables para analyses posteriores.

**Fail criteria → reabrir**:

- Si la instrumentación causa latencia perceptible (>50ms): refactor.

**Duración estimada**: 4 horas implementación + 1-2 semanas acumulación
(real); o 1 día scripted si se acelera.

### 0.3 — Dry-run manual del dream

**Asunción testeada**: A2, A3, A9 (sobre el LLM dream — antes de invertir en infra)

**Build**:

Cero código de producción. Trabajo manual + un script de invocación
LLM.

1. **Fuente de entries**: usar `openclaw-aule/workspace*/memory/` como
   corpus base (ver §13). Extraer entradas que mencionen:
   - 1 person: `person:marcelo` (todas las menciones del autor en
     los diarios).
   - 1 project: `project:mxhero` o `project:openclaw` (algo cross-
     profession, no solo dev).
   - 1 topic: ej. `topic:helpjuice` o `topic:slack-routing`
     (algo técnico-operativo).
   - Total: 30-50 entries reales por entidad. Si una entidad no
     llega, complementar con synthetic.
2. Construir un prompt template del dream con instrucciones detalladas:
   - Input: lista de entries + (re-consolidación) página actual.
   - Output: (a) markdown nuevo, (b) commit message con trailers.
3. Invocar Haiku con el prompt en un script standalone
   (`scripts/dream_dryrun.py`).
4. Iterar el prompt hasta que el output sea satisfactorio (5-10
   iteraciones).
5. Repetir para los 3 tipos de entidad.

**Artefacto trackeado**: el prompt final iterado se guarda en
`durin/templates/dream/consolidator.md` (versionado en repo). Cada
iteración del prompt durante Phase 0.3 es un commit en `scripts/`
con la versión del prompt + el output que produjo, para audit
posterior.

**Validate**:

Para cada output revisar manualmente:

- ¿Preserva todos los facts de las entries?
- ¿Marca temporalidad en prosa donde aplica (previously / now / since)?
- ¿Linkea sources en el cuerpo o frontmatter?
- ¿El commit message tiene trailers parseable?
- ¿El tipo (person/project/topic) es el correcto?

**Pass criteria**:

- 3/3 outputs pasan la inspección manual.
- El prompt resultante (estable) queda como base para Phase 2.
- Costo medido: < $0.10 / consolidación con Haiku (R3 threshold).

**Fail criteria → reabrir**:

- Si el LLM no genera trailers consistentes: re-evaluar formato.
- Si pierde facts: ajustar prompt para forzar enumeración exhaustiva.
- Si confunde tipos: marcar como riesgo y considerar lista cerrada vs abierta.
- Si costo > $0.10 con Haiku: revisar batch size o cambiar modelo.

**Duración estimada**: 1-2 días de trabajo manual.

---

## §3 — Phase 1: Foundations

Pre-requisitos técnicos que el resto de la arquitectura asume.

### 1.1 — Propuesta A: entities tipadas en entries episódicas

**Asunción testeada**: ninguna directa — habilita A6, A9.

**Build**:

Per doc 14 (typed entities proposal):

- `entities: [type:slug, ...]` en frontmatter de cada nueva memory entry.
- Validación: formato `^[a-z]+:[a-z0-9_-]+$` o similar.
- Set canónico inicial: 8 tipos del doc 18 §4. Vocabulario abierto.
- `memory_store` tool actualizado para aceptar `entities` parameter.
- `consolidator_tags` (o equivalente): agente que en `memory_store` automático extrae entities del contenido vía LLM si no se especifican.
- Backward compatibility: entries existentes sin `entities:` son tolerados en lectura.

**Validate**:

- Test unit: schema acepta/rechaza formatos correctos.
- Test integración: `memory_store` con entities specified → entry en disco con frontmatter correcto.
- Test integración: `memory_store` sin entities → LLM extracta + frontmatter completado.

**Pass criteria**:

- Suite de unit tests pasa.
- Inspección manual: 10 entries sintéticas con/sin entities producen output esperado.
- Cero regresión en `memory_store` existente.

**Fail criteria → reabrir**:

- Si LLM extraction no es confiable (>20% errores en muestra): considerar marcar entities como obligatoria en el prompt en vez de auto-extract.

### 1.2 — Git substrate

**Asunción testeada**: indirecta para A7.

**Build**:

- `durin install` / wizard hooks: `git init memory/` + commit inicial vacío.
- `durin/memory/git_ops.py` (nuevo): wrapper alrededor de subprocess git para `git add`, `git commit`, `git log`, `git show`, `git diff`, `git revert`.
- Author fijo: `durin-dream <dream@durin.local>` configurable.
- `.gitignore` template per doc 18 §5.
- Manejo de errores: si git no está disponible, durin sigue funcionando pero con warning (memoria sin tracing).

**Validate**:

- Test: `durin init` en directorio vacío → `memory/.git/` existe + initial commit.
- Test: write a entry + commit programmatic → aparece en `git log`.
- Test: revert commit → file reverted, history preserved.
- Test: install sin git available → warning visible, no crash.

**Pass criteria**:

- Suite de tests pasa.
- `memory/` queda como repo independiente del repo de código durin.
- Operaciones git no fallan en filesystems exóticos (macOS/Linux probados).

**Fail criteria → reabrir**:

- Si performance de commit es > 500ms (suficiente para causar UX issues): considerar batch commits.

### 1.3 — Entity page parser

**Asunción testeada**: indirecta — habilita Phase 2, 3, 4.

**Build**:

- `durin/memory/entity_page.py` (nuevo): clase `EntityPage` con métodos `from_file()`, `to_markdown()`, `frontmatter`, `body`, `aliases`, `cursor`, etc.
- Parser tolerante a frontmatter mal formado (no crashear si una page tiene typo).
- Schema mínimo per doc 18 §4: `type`, `name`, `aliases`, `dream_processed_through`, `created_at`, `updated_at`.

**Validate**:

- Test unit: parsear pages sintéticas con/sin secciones, frontmatter válido/inválido.
- Test: roundtrip (parse → modify → write → parse) preserva contenido.

**Pass criteria**:

- 95%+ coverage en parser.
- Roundtrip lossless.

### 1.4 — Aliases index sidecar

**Asunción testeada**: indirecta para A6.

**Build**:

- `durin/memory/aliases_index.py` (nuevo): build/load/save de
  `memory/.aliases.json` con shape `{alias_string: entity_slug}`.
- Build incremental: al crear/actualizar una entity page, refrescar el index.
- Build full: comando `durin memory rebuild-aliases` que parsea todos los entity pages.
- Auto-rebuild al boot si el index no existe o está más viejo que cualquier page.

**Validate**:

- Test: build sobre 5 entity pages sintéticas → index correcto.
- Test: agregar nuevo alias → index actualizado.
- Test: corrupt index → rebuild automático al boot.

**Pass criteria**:

- Index queryable en < 10ms para corpus de 100 entidades.

---

## §4 — Phase 2: Dream consolidador (vertical slice)

End-to-end de la materia prima al output consolidado. Es la fase donde
más asunciones se tocan a la vez.

### 2.1 — Dream prompt template + invocación

**Asunción testeada**: A2, A3, A4, A9, A10.

**Build**:

- `durin/templates/dream/consolidator.md` (nuevo): prompt template
  refinado en Phase 0.3.
- `durin/memory/dream.py` (nuevo): clase `DreamConsolidator` con métodos:
  - `pending_entities()` → lista de entidades con entries post-cursor.
  - `consolidate(entity, entries, current_page)` → llama LLM, devuelve (new_markdown, commit_message).
  - `apply(entity, new_markdown, commit_message)` → write file + git commit.
- Modelo: Haiku por default, configurable.
- Cursor avance: tras successful consolidate, cursor se mueve al último msg_idx procesado.

**Validate**:

- Test sintético: 5 entries mencionando `person:marcelo` → consolidator produce página + commit.
- Test idempotencia: consolidar 2 veces sin entries nuevas → no genera commit nuevo.
- Test contradicción: entries con info contradictoria → prose marca temporalidad.
- Test errror handling: LLM falla → no commit, no entity page corruption.

**Pass criteria**:

- 10 escenarios sintéticos cubiertos: all pass.
- Costo medido por consolidación: < $0.10 con Haiku.
- Output de commit message es 100% parseable (trailers correctos).

**Fail criteria → reabrir**:

- Si el LLM no marca temporalidad de manera consistente: considerar agregar back claim-status enum en YAML (doc 18 §3 descarte revisar).
- Si idempotencia falla: el sistema escribe commits "no-op" repetidos. Bug crítico.

### 2.2 — Trigger del dream

**Asunción testeada**: A10.

**Build inicial — manual trigger**:

- Comando `durin memory dream` manual.
- Sin trigger automático todavía.

**Build subsiguiente — session-end trigger**:

- Hook en context compaction / `/quit` / idle timer.
- Dispara `DreamConsolidator.consolidate()` para entities con entries post-cursor.

**Validate manual trigger**:

- Run `durin memory dream` tras varias sesiones acumuladas → output esperado.

**Validate auto-trigger**:

- Sesión sintética con N entries → al terminar, dream corre → consolidaciones generadas.

**Pass criteria**:

- Manual trigger: cero errores en runs típicos.
- Auto-trigger: no afecta latencia perceptible en `/quit`.
- Dream corre en background (async) — no bloquea UX.

### 2.3 — End-to-end test vertical slice

**Asunción testeada**: A2, A3, A4, A9, A10 (integrados).

**Build**:

Test fixture de tamaño realista (no toy):

- **80-100 episodic entries** distribuidas a lo largo de ~15-20
  sesiones simuladas (timestamps espaciados días/semanas).
- **Menciones a 5-6 entidades** mezcla:
  - 1 `person:marcelo` (alto volumen, hub natural).
  - 1 `project:durin` o `project:mxhero` (alto volumen).
  - 1 `topic:embeddings` o `topic:slack-routing`.
  - 1 `practice:tdd` o `practice:morning-standup` (test del tipo
    procedural).
  - 1 `event:tui-bug-recurring` (test de event consolidable).
  - 1 `artifact:settings.py` o `artifact:helpjuice-deck` (test del
    tipo artifact).
- **Fuente**: derivada de `openclaw-aule/workspace*/memory/` con
  curation manual + entries sintéticas adicionales para inyectar
  casos específicos (ver §13).
- **Casos específicos a inyectar**:
  - Contradicción temporal: entry sesión 3 dice "uso pytest", entry
    sesión 12 dice "ahora uso unittest".
  - Aliases: una entry usa "Marcelo", otra "marcelo", otra
    "mmarmol@mxhero.com".
  - Cross-profession: al menos 2 entidades no-coder (marketing/business
    context desde mxhero-ai-vault, ver §13).

Test script:

```python
# Tests end-to-end Phase 2
1. Setup: write 30 entries al memory store
2. Run dream consolidator
3. Verify:
   - entities/person/marcelo.md existe
   - entities/project/durin.md existe
   - entities/topic/embeddings.md existe
   - cada page tiene aliases array correcto
   - cada page tiene dream_processed_through cursor avanzado
   - Para entidades con contradicción: prose marca temporalidad
   - git log muestra commits con trailers Sources/Entities-touched
   - Re-run dream sin nuevas entries: cero commits nuevos
```

**Pass criteria**:

- Suite end-to-end pasa.
- Inspección manual de los 3 outputs: coherentes y útiles.
- Costo total: < $0.50 para los 3.

**Fail criteria → reabrir Phase 2**:

- Si las páginas son ruido (LLM hallucina, pierde facts, escribe inconsistente): prompt necesita más trabajo.
- Si los commits no son parseable: formato necesita ajuste.

---

## §5 — Phase 3: Retrieval entity-aware L1 light

Una vez hay pages consolidadas + cursor + aliases index, integrar al retrieval.

### 3.1 — Query-time entity extraction

**Asunción testeada**: A6.

**Build**:

- En `durin/memory/search.py` (o equivalente): pre-processing del query
  que matchea contra aliases index (string match exact + lowercase).
- Devuelve lista de `entities_in_query: [type:slug, ...]`.
- Sin LLM call.

**Validate**:

- Test: query "qué prefiere marcelo" → `[person:marcelo]`.
- Test: query "decisiones sobre embeddings" → `[topic:embeddings]`.
- Test: query "general question" → `[]` (cae a vector puro sin error).

**Pass criteria**:

- Latencia < 10ms para query típica.
- Coverage: 80%+ de queries con menciones explícitas detectan la entidad.

### 3.2 — Boost/demote por cursor

**Asunción testeada**: A5, A6.

**Build**:

- En el ranker post-vector search:
  - Para cada `ent in entities_in_query`:
    - Boost a entries con `ent in entry.entities AND entry.created_at > ent.cursor`.
    - Demote a entries con `ent in entry.entities AND entry.created_at <= ent.cursor`.

**Validate**:

- Test sintético: 20 entries sobre marcelo, cursor en msg 10. Query "marcelo".
  - Pre-cursor (1-10): rank más bajo.
  - Post-cursor (11-20): rank más alto.
  - Página canónica: surface en top.

**Pass criteria**:

- Top-K resultados muestran orden esperado en 5+ escenarios sintéticos.
- Sin pérdida de respuestas correctas al añadir el boost/demote.

### 3.3 — Multi-factor ranking integrado

**Asunción testeada**: A5, A6.

**Build**:

- Score final = `vector_score × tag_boost × cursor_factor × recency_factor`.
- Recency: leve boost por entries más recientes (desempate de pesos iguales).
- Sin weight explícito (peso implícito — doc 18 §7 pregunta 5).

**Validate**:

- Test sintético con corpus realista (50 entries + 5 pages): rankings comparados manualmente vs expected.

**Pass criteria**:

- Top-5 contiene la página canónica + entries post-cursor relevantes en 90%+ de queries sintéticas.

### 3.4 — End-to-end Phase 3

**Asunción testeada**: A5, A6 (integrados con Phase 2).

**Build**:

Test fixture: usar el corpus de Phase 2.3 + agregar más entries post-cursor sin consolidar.

Test script:

```python
1. Estado pre-Phase-3 (vector puro): correr 10 queries sobre marcelo,
   recolectar top-5 results.
2. Activar Phase 3 (L1 light).
3. Re-correr las mismas 10 queries.
4. Compare:
   - ¿Páginas canónicas surface más arriba?
   - ¿Entries post-cursor sin consolidar surface junto a la página?
   - ¿Pre-cursor entries demote correctamente?
```

**Pass criteria**:

- 80%+ de queries mejoran (página o post-cursor entries surface más arriba).
- Cero regresión en queries que no tocan entidades (deberían comportarse igual).

---

## §6 — Phase 4: Drill-down + commands

Capacidades de inspección/expansion para el user (y futuras herramientas).

### 4.1 — `durin memory history <entity>`

**Build**:

- `git log <entity_page> --pretty` formateado custom.
- Cada commit muestra: timestamp, author, subject, trailers parseados.

**Validate**:

- Tras Phase 2.3 con dream corrido 3+ veces: `durin memory history person:marcelo` muestra las 3 revisiones con razonamiento.

**Pass criteria**:

- Output legible y útil.
- Cada commit muestra subject + cuerpo + trailers + diff stats.

### 4.2 — `durin memory diff <entity> <revs>`

**Build**:

- `git diff <commit_a>..<commit_b> <entity_page>` formateado.

**Validate**:

- Diff entre revisiones muestra changes claramente.

### 4.3 — `durin memory revert <commit>`

**Build**:

- `git revert <commit>` con safety check (no es el initial commit).
- Confirmación interactiva.

**Validate**:

- Revert undo successful, page restaurada a estado previo.
- Audit visible en git log.

### 4.4 — `durin memory expand <node>`

**Build**:

- Toma un identificador (entity slug o entry id).
- Devuelve:
  - Sources (entries que contribuyeron).
  - Versiones previas (git history).
  - Entidades relacionadas (parseando markdown links + frontmatter).
  - Archive subfolder content (si aplica).

**Validate**:

- Tras Phase 5: `durin memory expand entities/person/marcelo.md` lista sources + archived absorptions.

**Pass criteria**:

- Output útil para "drill-down" cognitivo. Inspección manual.

---

## §7 — Phase 5: Absorción + archive

Manejo de aliases detectados como entidad duplicada.

### 5.1 — Alias detection durante dream

**Asunción testeada**: A11.

**Build**:

- En consolidador, si dream detecta que dos entity pages refieren a la misma persona (vía aliases overlap, similar content, embedding similarity > 0.95), proponer merge.
- Decisión: si `_MEMORY_AUTHOR` de la canónica es user_authored, NO mergear sin confirmación. Sino, dream procede.

**Validate**:

- Test sintético: 2 pages `person:marcelo` y `person:marcelo-m` con aliases overlap → dream detecta y propone merge.

### 5.2 — Move to archive subfolder

**Build**:

- `git mv entities/person/marcelo-m.md entities/person/marcelo/archive/marcelo-m.md`.
- Frontmatter del archivado: añade `absorbed_into: ../../marcelo.md`, `absorbed_at`, `absorbed_reason`.

**Validate**:

- Tras absorption: filesystem queda con la estructura esperada.

### 5.3 — Indexer de-indexa archive subfolders

**Build**:

- LanceDB indexer: regla "skip `**/archive/**`".
- Aliases index: también skip archive.

**Validate**:

- Test: archived page NO surface en vector search normal.
- Test: `durin memory expand` SÍ traversa al archive cuando se le pide.

### 5.4 — Canonical linkea al archive

**Build**:

- La canónica `marcelo.md` agrega en alguna sección (e.g. `## Archived aliases`) un markdown link: `[marcelo-m](marcelo/archive/marcelo-m.md)`.

**Validate**:

- Inspección manual: link funciona, parser markdown lo entiende.

### 5.5 — End-to-end Phase 5

**Asunción testeada**: A11.

**Build**:

Test fixture: 2 pages `marcelo` y `marcelo-m` con overlap declarado en aliases.

Test script:

```python
1. Setup: 2 entity pages + entries históricas de cada uno.
2. Run dream → detecta merge proposal → ejecuta absorption.
3. Verify:
   - entities/person/marcelo.md absorbió content de marcelo-m
   - entities/person/marcelo/archive/marcelo-m.md existe con frontmatter
     absorbed_into correcto
   - vector search por "marcelo-m" surface marcelo (canónica), NO archive
   - git log muestra el commit de absorption con trailers correctos
   - durin memory expand marcelo lista el archive
```

**Pass criteria**:

- All checks pass.
- Restore via `git mv` funciona si el merge fue erróneo.

---

## §8 — Phase 6: Validación contra outcomes + telemetría

Tras las 5 phases anteriores, validar que el modelo entrega su promesa
operativa (doc 18 §11).

### 6.1 — Outcomes operativos como tests

**Asunción testeada**: A1-A11 integrados, contra promesa real.

**Build**:

Suite de tests acceptance basados en doc 18 §11. Cada outcome es un
test ejecutable:

**Test O1**: Coherencia cross-sesión sobre proyecto.

```python
def test_project_decisions_consolidated():
    # Setup: 10 entries sintéticas across 5 simulated sessions,
    # all mentioning project:durin, with decisions on embeddings.
    seed_synthetic_corpus(scenario="durin_embedding_decisions")
    run_dream()
    
    # Query (simulated user input):
    result = memory_search("¿qué decisiones tomamos sobre embeddings?")
    
    # Assert:
    assert any("entities/project/durin.md" in r.source for r in result.top_k)
    page_content = read_page("entities/project/durin.md")
    assert "embedding" in page_content.lower()
    assert "decision" in page_content.lower() or "decidimos" in page_content.lower()
```

**Test O2**: Unificación automática por aliases.

```python
def test_aliases_unify_person():
    # Setup: entries en sesión 1 dicen "soy Marcelo".
    # Entries en sesión 5 dicen "soy mmarmol@mxhero.com".
    seed_synthetic_corpus(scenario="marcelo_multi_alias")
    run_dream()
    
    # Assert:
    page = read_page("entities/person/marcelo.md")
    assert "mmarmol@mxhero.com" in page.aliases
    assert "Marcelo" in page.aliases
```

**Test O3**: Consolidación de incident recurrente.

```python
def test_recurring_incident_consolidated():
    # Setup: 3 sesiones distintas mencionan un bug en TUI con causa+fix.
    seed_synthetic_corpus(scenario="tui_bug_recurring")
    run_dream()
    
    # Assert:
    assert exists("entities/event/tui-bug-empty-bubbles.md") or similar
    page = read_page(<that file>)
    assert "causa" in page.body and "fix" in page.body
```

**Test O4**: Drill-down "why".

```python
def test_drill_down_why():
    # Setup: history fixture con 3 revisions de marcelo.md.
    seed_synthetic_history("marcelo_3_revisions")
    
    # Action:
    history = durin_memory_history("entities/person/marcelo.md")
    
    # Assert:
    assert len(history) == 3
    for revision in history:
        assert revision.reason  # body del commit
        assert revision.sources  # trailers
```

**Test O5**: Drill-down "expand".

```python
def test_drill_down_expand():
    # Action:
    expansion = durin_memory_expand("entities/person/marcelo.md")
    
    # Assert:
    assert expansion.sources  # entries que contribuyeron
    assert expansion.versions  # git history
    assert expansion.related_entities  # via links + frontmatter
```

**Pass criteria**:

- 5/5 outcomes pass.
- Cada fallo identifica una fase a revisitar específicamente.

### 6.2 — Análisis de telemetría #9

**Asunción testeada**: A6 (validación final).

**Build**:

- Script de análisis sobre `memory/.telemetry/retrieval.jsonl` acumulada.
- Categorías de queries:
  - "Tag matcheado retornado" (caso ideal).
  - "Tag matcheado NO retornado" (bucket diagnóstico per doc 18 §7).
  - "Sin tag matcheado" (vector puro alcanza).
  - "Sin entity en query" (vector puro normal).

**Validate**:

- Después de N semanas de uso del autor con el sistema completo: análisis del log.

**Pass criteria**:

- Bucket "Tag matcheado NO retornado" se mantiene chico (< 10% de queries con tag).
- Si es grande: señal para abrir L2+ con experimento controlado.

### 6.3 — Anti-fragilidad

**Asunción testeada**: A8.

**Build — simulación, no wall-clock**:

- Test fault injection vía **manipulación de timestamps + replay
  scripted**, no esperando días reales.
- Setup: corpus con páginas consolidadas + cursor por entidad. Luego:
  - Phase 1: inyectar 100 entries nuevas mencionando entidades existentes,
    con timestamps post-cursor.
  - Phase 2: deshabilitar el dream (config flag o no-op).
  - Phase 3: ejecutar 30 queries sobre esas entidades.
  - Phase 4: medir.
- También: simular crash del dream a mitad de consolidación (kill
  durante `consolidate()`) → verificar que git deja el estado consistente
  (sin commits parciales).

**Validate**:

- 30 queries de Phase 3 → respuestas correctas (página consolidada
  vieja + entries nuevos post-cursor coexistiendo).
- Crash a mitad: `git status` queda clean (commit-or-nothing); página
  no queda mid-edit.

**Pass criteria**:

- Cero crashes en queries durante el "outage".
- Respuestas siguen siendo correctas (LLM reconcilia via read-time).
- Context size growth: aceptable (< 2x del caso con dream activo).
- Recuperación tras re-habilitar dream: una pasada nueva consolida lo
  acumulado, no pierde nada.

---

## §9 — Test matrix consolidado

Resumen de qué se testea, dónde, con qué fixture.

| Test | Fase | Fixture | Asunción |
|---|---|---|---|
| Embedding name variations | 0.1 | Pares hardcoded | A1 |
| Retrieval baseline telemetry | 0.2 | Uso real autor | A6 baseline |
| Dream dry-run manual | 0.3 | 30-50 entries reales | A2, A3, A9 |
| Propuesta A typed entities | 1.1 | Synthetic entries | (foundational) |
| Git substrate | 1.2 | Empty + populated repos | (foundational) |
| Entity page parser | 1.3 | Synthetic pages valid/invalid | (foundational) |
| Aliases index | 1.4 | 5 entity pages | A6 |
| Dream consolidator | 2.1, 2.3 | 30 entries / 3 entidades | A2, A3, A4, A9, A10 |
| Dream trigger | 2.2 | Synthetic session-end | A10 |
| Retrieval boost/demote | 3.1-3.4 | Pre/post cursor entries | A5, A6 |
| Memory history command | 4.1 | Phase 2 output | A7 |
| Memory diff/revert | 4.2-4.3 | Phase 2 output | A7 |
| Memory expand | 4.4 | Phase 5 output | A7 |
| Alias absorption | 5.1-5.5 | 2 pages with overlap | A11 |
| Outcomes acceptance (O1-O5) | 6.1 | Synthetic scenarios per outcome | A1-A11 integrados |
| Telemetría análisis | 6.2 | Uso real autor post-Phase-5 | A6 final |
| Anti-fragility | 6.3 | Fault injection (dream off 30d) | A8 |

---

## §10 — Riesgos durante implementación

| Riesgo | Manifestación | Mitigación |
|---|---|---|
| Phase 0.3 revela que el prompt no converge | LLM no produce output consistente | Iterar prompt; si tras 10 iteraciones no converge, considerar modelo más capaz (Sonnet) o claim-status YAML para forzar estructura |
| Phase 2 cuesta más de $0.10/sesión | Costo prohibitivo a escala | Batch consolidations, reducir context window al LLM, o cambiar a modelo más barato |
| Phase 3 L1 light no mejora respuesta | A6 falsa | Reabrir doc 18 §7 — quizás necesitamos L2 antes de lo esperado, o el problema está en otra parte (Phase 2 mala) |
| Phase 5 absorpción fusiona incorrectamente | Pierde info por merge erróneo | git revert + ajustar prompt para ser más conservador en merge proposals |
| Phase 6 outcomes fallan | A1-A11 no se sostienen integrados | Análisis: qué outcome falla → qué fase reabrir → qué asunción revisar |

---

## §11 — Decision points (cuándo parar y reevaluar)

Después de cada fase, decision point explícito:

- **Tras Phase 0**: ¿Las asunciones más baratas se validaron? Si A1, A2, A3 fallan → reabrir doc 18 fundamental.
- **Tras Phase 1**: ¿Las foundations son sólidas? Si propuesta A o git substrate tienen issues → resolver antes de avanzar.
- **Tras Phase 2**: ¿El dream produce output útil? Si no → no avanzar a Phase 3 hasta que sí.
- **Tras Phase 3**: ¿L1 light realmente ayuda? Si telemetría muestra que no → reevaluar el approach entity-aware completo.
- **Tras Phase 4**: ¿Drill-down se siente útil? Si nadie lo usa (testing manual del autor) → simplificar.
- **Tras Phase 5**: ¿La absorpción funciona sin pérdida? Si pierde info → fix antes de Phase 6.
- **Tras Phase 6**: ¿El modelo entrega su promesa? Si outcomes fallan → revisita doc 18 con evidencia. Si pasan → release / production.

---

## §12 — Estimación temporal

Estimaciones honestas, sujeto a aprendizaje. **Incertidumbre marcada
con ⚠ donde la variación puede ser >2x**.

| Phase | Estimación | Incertidumbre |
|---|---|---|
| 0.0 smoke | ~1 hora | baja |
| 0.1 embedding test | ~1 hora | baja |
| 0.2 telemetría baseline | ~4h impl + 1-2 sem acumulación (o 1 día scripted) | media |
| 0.3 dream dry-run | ~1-2 días | ⚠ alta — si LLM no converge, puede ser 1+ semana |
| 1 (4 sub) | ~1-2 semanas (código + tests) | media |
| 2 (3 sub) | ~1-2 semanas | ⚠ alta — iteración de prompt acoplada al éxito de Phase 0.3 |
| 3 (4 sub) | ~3-4 días | baja-media |
| 4 (4 sub) | ~3-4 días | baja |
| 5 (5 sub) | ~3-5 días | media |
| 6 (3 sub) | ~1 semana de análisis y acceptance tests | media |
| **Total** | **~5-9 semanas** (rango ampliado vs estimación previa) | |

Las phases con ⚠ son las que dictan el rango: si Phase 0.3 muestra
que el LLM no produce output consistente al primer intento, Phase 2
hereda esa incertidumbre.

No incluye telemetría real-world accumulation (semanas en paralelo si
se elige el camino real vs scripted).

### Organización de tests en el suite

Convención sugerida:

- `tests/memory/entity_centric/` — tests específicos de las nuevas
  capacidades (dream, page parser, alias index, archive subfolder).
- `tests/integration/entity_centric/` — tests end-to-end por fase
  (Phase 2.3, 3.4, 5.5, 6.1).
- Fixtures sintéticas en `tests/fixtures/entity_centric/` (entries,
  pages, expected outputs).
- Una makefile target `make test-entity-centric` que corre solo este
  subset durante desarrollo.

---

## §13 — Fuentes de datos para testing

durin todavía no tiene corpus propio suficiente para validación
empírica. Tres fuentes locales sirven como base — todos son corpora
reales (no toy data sintética) para que los tests reflejen uso
realista.

### Fuente A — openclaw-aule (memory backups)

**Path**: `/Users/marcelo/git/openclaw-aule/workspace*/memory/`

**Qué es**: backup del propio Marcelo con su agente openclaw,
abril 2026. Archivos diarios `YYYY-MM-DD.md` con structure custom
de openclaw (Light Sleep candidates con confidence/evidence/recalls/
status).

**Tamaño**: ~41 archivos diarios + sub-workspaces (workspace,
workspace-elrond, workspace-legolas, workspace-samwise).

**Contenido relevante**:

- Conversaciones reales en español/inglés entre Marcelo y el agent.
- Menciones a personas (Marcelo, posibles colegas de mxhero).
- Menciones a proyectos (openclaw, mxhero, helpjuice).
- Decisiones técnicas (migración QMD, slack thread routing,
  memory-wiki activation).
- Incidentes (memorias que se perdían, plugin sin activar).
- Patrones de skill curator y dream consolidation que ya hizo
  openclaw.

**Cómo usarlo**:

- **Phase 0.3 dream dry-run**: extraer las "Candidate: User: ..." y
  "Candidate: Assistant: ..." de varios diarios como entries
  sintéticas para alimentar al consolidator de durin. Curation
  manual para elegir 30-50 entries por entidad.
- **Phase 2.3 vertical slice**: ampliar la curation a 80-100 entries
  / 5-6 entidades, incluyendo contradicciones temporales reales
  (e.g., "uso pytest" → "uso unittest") que se hayan capturado en
  esos diarios.
- **Phase 6.1 outcomes acceptance**: derivar de las decisiones
  reales tomadas en abril 2026 los escenarios de test.

**Limitaciones**:

- Formato openclaw difiere de durin episodic; un parser one-off
  convierte. ~50 LOC en `scripts/openclaw_to_durin_entries.py`.
- Datos reales — sensible. Mantener en local, no commitear a
  fixtures públicos. Las fixtures de tests usan derivadas anonimizadas
  o sintéticas inspiradas en estos.

### Fuente B — mxhero-ai-vault (vault de conocimiento empresa)

**Path**: `/Users/marcelo/git/mxhero-ai-vault/vaults/mxhero/`

**Qué es**: vault de Obsidian con conocimiento de la empresa mxHero,
organizado en categorías: Clientes, Empresa, Producto, Soporte,
Box/Dropbox/Egnyte/Google Drive/OneDrive Partner, Funcionalidad,
Helpjuice, Agentes, Arquitectura.

**Contenido relevante**:

- **Cross-profession real**: marketing/ventas (Clientes, Partners),
  soporte (Soporte, Helpjuice), producto (Producto, Funcionalidad),
  empresa (Empresa).
- Notas estructuradas en markdown con frontmatter.
- Probablemente con menciones cruzadas (clientes ↔ partners ↔
  productos).

**Cómo usarlo**:

- **Phase 0.3 dream dry-run cross-profession**: usar como source
  para entidades NO-coder. Ej: `topic:helpjuice`, `project:partner-
  egnyte`, `person:cliente-X`. Valida A9 (cross-profession real, no
  inventado).
- **Phase 2.3 fixture**: ampliar con entidades de marketing/sales
  para verificar que el dream produce páginas coherentes para
  dominios no-técnicos.
- **Phase 6.1 outcome O3** (incident recurrente): si hay incidentes
  de soporte recurrentes en Helpjuice o cliente notes, usar para
  test.

**Limitaciones**:

- Mismas restricciones de privacidad — datos reales empresa.
- El vault está en formato Obsidian, hay que parsear frontmatter
  + wikilinks.

### Fuente C — agente-memory vault

**Path**: `/Users/marcelo/git/mxhero-ai-vault/vaults/agent-memory/`

**Qué es**: vault complementario con Conversaciones, Decisiones,
Decisions, Orquestaciones, Skills, README.

**Cómo usarlo**:

- Posible fuente para extraer **patrones de decision-making
  histórico** (Decisiones/Decisions) para outcome O1 (decisiones de
  proyecto consolidadas).
- **Skills** puede dar input para el tipo `practice` (rutinas
  documentadas).

### Convención de uso

Para todos los tests:

1. **Fixtures sintéticas** en `tests/fixtures/entity_centric/`:
   derivadas pero **no copy-paste** de las fuentes reales. Texto
   curado + anonimizado + posiblemente abreviado.
2. **Tests de validación manual** (Phase 0.3, 6.1) pueden usar las
   fuentes directas (en local), sin copy a repo.
3. **Tests de regresión / CI** usan solo fixtures derivadas en
   `tests/fixtures/`, nunca paths a `/Users/marcelo/git/...`.
4. **Datos sensibles** (nombres de clientes, email reales, etc.)
   van anonimizados en las fixtures: `client-A`, `partner-X`, etc.

### Eventualmente — corpus durin propio

Cuando Phase 0.2 telemetría acumule queries reales del autor + cuando
durin se use con la memoria nueva activada, el propio corpus de
durin se vuelve fuente primaria. Las fuentes A/B/C son bootstrap.

---

## §14 — Lo que NO está en este plan

Explícito para evitar scope creep:

- Visualización Obsidian-style → fuera de scope inicial.
- Sub-paging para mega-hub → fuera (sólo si Phase 6 muestra evidence).
- L2+ retrieval (graph traversal, cross-encoder, PageRank) → fuera (sólo si Phase 6 telemetría lo justifica).
- User editing manual de páginas → fuera.
- Sync remoto → fuera.
- Benchmark público (LoCoMo, EverMemBench) → fuera del implementation plan; podría hacerse en una fase posterior si binding.

---

## Last updated: 2026-05-23 (post-doc-18 / pre-implementación)
