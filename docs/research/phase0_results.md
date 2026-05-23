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

Phase 0.2: telemetría baseline. La calibración del threshold de cosine
para "match" o "ambiguous zone" en el L1 light se hará con datos reales.

---

## Last updated: 2026-05-23 (Phase 0.0 + 0.1)
