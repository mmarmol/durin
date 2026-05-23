# Dream consolidator prompt — v1

> Prompt para el dream LLM. Toma N observaciones episódicas sobre una
> entidad y produce (a) página markdown consolidada, (b) commit message
> con trailers parseables.
>
> **Versión 1** — validado en Phase 0.3 (doc 19) contra corpus
> openclaw-aule con 3 entidades (`person:marcelo`, `project:mxhero`,
> `topic:helpjuice`). Modelo testeado: glm-5.1 (`api_base:
> https://api.z.ai/api/coding/paas/v4`). Output coherente, fact-preserving,
> ~36s per consolidation, ~$0.005 estimado para Haiku equivalent.
>
> **Variables a sustituir**:
> - `{entity_id}` — ej. `person:marcelo`, `project:durin`
> - `{n_entries}` — número de entries
> - `{entries_text}` — lista de entries formateadas (ver
>   `scripts/dream_dryrun.py:format_entries_for_prompt`)
>
> Iteraciones futuras: ver "Hallazgos de v1" abajo y
> `docs/research/phase0_results.md`.

---

## Template

```
Eres durin, asistente con sistema de memoria entity-centric.

Tu tarea: tomar N observaciones episódicas sobre la entidad `{entity_id}`
y producir DOS outputs:

1. **Página markdown consolidada** para la entidad. Schema:
   - Frontmatter YAML con: `type`, `name`, `aliases` (array de variantes textuales del nombre), `identifiers` (dict opcional con claves como `email`, `phone`, `slack`, `github`, `jira`, etc. — sólo si aparecen en las entries), `dream_processed_through` (cursor msg_idx), `created_at`, `updated_at`.
   - Cuerpo: secciones markdown libres (## Current state, ## History, ## Background, etc.) según el contenido.
   - Si hay contradicciones temporales, marcar en prosa: "previously X / now Y" o "until <fecha> X, since <fecha> Y".
   - NO claims YAML estructurados — todo en prosa natural.
   - Linkear sources en el cuerpo o en sección "## Sources" al final.
   - **Para `type: person`**: ser proactivo extrayendo identifiers (emails, phones, slack IDs, github users, etc.) que aparezcan en las entries — son críticos para desempate cross-system.

2. **Commit message** que explique la consolidación. Schema:
   - Subject line: `Consolidate {entity_id} (rev N)` (asume rev 1 para esta primera consolidación).
   - Cuerpo en lenguaje natural explicando QUÉ se consolidó y POR QUÉ.
   - Trailers estructurados al final:
     - `Sources: <list of episodic ids>`
     - `Entities-touched: {entity_id}`
     - `Entities-referenced: <other entities mentioned>`
     - `Dream-session: <timestamp>`
     - `Cursor-before: 0`
     - `Cursor-after: <msg_idx of last entry processed>`

Output FORMATO ESTRICTO:

\`\`\`
===PAGE===
<contenido markdown de la página, incluyendo frontmatter>
===COMMIT===
<contenido del commit message, subject + body + trailers>
===END===
\`\`\`

---

ENTIDAD A CONSOLIDAR: `{entity_id}`

PÁGINA ACTUAL (si existe — usar como base, NO duplicar facts ya presentes):

{current_page}

OBSERVACIONES EPISÓDICAS ({n_entries} entries):

{entries_text}

---

Produce los dos outputs en el formato indicado arriba. Sé conciso pero
preserva los facts importantes.
```

---

## Hallazgos de v1 — para iteración futura

Resultados del dry-run Phase 0.3 (`docs/research/phase0_results.md`):

**Strengths confirmados:**

1. Frontmatter YAML well-formed con todos los fields.
2. Cuerpo en prosa natural sin sobre-estructurar.
3. Sources linkeados (con rangos compactos cuando aplica).
4. Commit message subject + body + trailers parseables.
5. Tablas markdown cuando el contenido lo amerita (sync history para helpjuice).
6. Extracción de entidades referenciadas (`Entities-referenced`) razonablemente accurate.
7. Detección de patterns repetidos cross-day ("carried forward across dream sessions").

**Iteraciones potenciales (no aplicadas todavía):**

1. **Sources con rangos `[A] through [B]`**: pierden IDs individuales. Cuando se hace drill-down ("¿qué entry específico dijo X?") es menos útil. Mejor pedir lista explícita o anotar rangos solo cuando sean realmente contiguous.

2. **`dream_processed_through`**: en v1 el LLM usó el ID de la última entry (`2026-04-13-004`). Cuando integremos cursors reales por entidad, este field debe usar el `msg_idx` numérico del runtime, no el ID de archive. Probablemente este field se inyecta por el código que llama al prompt, no se le pide al LLM.

3. **Vocabulario abierto vs sugerido**: el LLM creó `agent:sam`, `agent:aule`, `org:henngo` en `Entities-referenced`. Esto valida vocabulario abierto pero también muestra que el dream extiende el set sin freno. Para v2 considerar:
   - (a) Aceptarlo: vocabulario abierto, el set sugerido es solo guía.
   - (b) Validar contra una whitelist y descartar/normalizar tipos desconocidos.
   - (c) Dejarlo emerger y revisar manualmente en `entities/<unknown>/` cada N consolidaciones.

4. **Tamaño del output**: ~4000 chars para 30 entries → ratio ~7.4x compression. Para una entidad con 200+ entries, el cuerpo podría llegar a 25-30KB. Mega-hub problem (doc 18 R2). Pero esto es futuro, no v1.

5. **No probó contradicciones temporales fuertes**: los 30 entries de openclaw-aule no tenían contradicciones explícitas (e.g., "X prefiere pytest" → "X ya no usa pytest"). Phase 2.3 testfixture debería inyectar este caso para validar el patrón "previously / now" en prosa.

6. **Costo**: ~3000 tokens input + 1000 output = 4000 tokens. Para Haiku: ~$0.005 per consolidation. Para glm-5.1 (coding plan): subscription incluida. Ambos bien debajo del threshold $0.10/sesión (asumiendo ~10 entidades por session = ~$0.05).

7. **Latencia**: 36s per consolidación con glm-5.1. Para una session con 10 entidades nuevas, dream tomaría ~6 min. OK como background async, no UX-blocking.

---

## Last updated: 2026-05-23 (post-Phase 0.3, validado con 3 entidades)
