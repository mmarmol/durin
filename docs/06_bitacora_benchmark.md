# Bitacora de benchmark — SWE-bench evaluation

> Evaluacion objetiva de Durin como sistema agentico usando SWE-bench Lite.
> Comparacion directa contra baseline nanobot y entre condiciones de Durin.

---

## Objetivo

Medir si los sistemas de postura y deliberacion de Durin agregan valor medible sobre el agente base.

---

## Setup

### API
- **Endpoint**: `https://api.z.ai/api/coding/paas/v4` (OpenAI-compatible)
- **Modelo principal**: `glm-5.1` (754B MoE, Z.ai/Zhipu)
- **Modelo deliberacion**: `glm-5-turbo` (para generadores)
- **Context window**: 200K tokens
- **Temperature**: 0.1 (main), 0.7 (generators)

### Scripts
- `scripts/swebench_eval.py` — Durin (con/sin delib, con/sin carry-posture)
- `scripts/swebench_nanobot_eval.py` — Nanobot base (sin postura, sin delib)

### Evaluacion
- Docker Desktop (ARM, swebench v4.1.0)
- `swebench.harness.run_evaluation` — aplica patch, corre tests del issue

---

## Condiciones experimentales

| # | Condicion | Postura | Deliberacion | Carry |
|---|---|---|---|---|
| 1 | Nanobot base | OFF | OFF | — |
| 2 | Durin sin deliberacion | ON (fresh) | OFF | No |
| 3 | Durin delib V2 | ON (fresh) | ON (V2) | No |
| 4 | Carry sin deliberacion | ON (carry) | OFF | Si |
| 5 | Carry + deliberacion | ON (carry) | ON (V2) | Si |

**Carry-posture**: `posture_final` de instancia N se convierte en `posture_initial` de instancia N+1.

---

## Resultados — 2026-05-17

### 5 instancias: astropy (12907, 14182, 14365, 14995, 6938)

| Condicion | Resueltos | Tasa | Avg Time | Avg Iters | Posture Events | Delib Events |
|---|---|---|---|---|---|---|
| 1. Nanobot base | 2/5 | 40% | 91.7s | 12.8 | 0 | 0 |
| 2. Durin sin delib | 4/5 | 80% | 235.3s | 30.2 | 156 | 0 |
| 3. Durin delib V2 | 3/5 | 60% | 232.3s | 30.8 | 155 | 10 |
| 4. Carry sin delib | 3/5 | 60% | 209.5s | 28.6 | 271 | 10 |
| 5. Carry + delib | 4/5 | 80% | 198.9s | 24.8 | 233 | 10 |

### Matriz por instancia

| Instance | Nanobot | Sin delib | Delib V2 | Carry | Carry+D |
|---|---|---|---|---|---|
| 12907 (facil) | ✓ | ✓ | ✓ | ✓ | ✓ |
| 14182 (dificil) | ✗ | ✗ | ✗ | ✗ | **✓** |
| 14365 | ✗ | ✓ | ✓ | ✗ | ✓ |
| 14995 (facil) | ✓ | ✓ | ✓ | ✓ | ✓ |
| 6938 | ✗ | ✓ | ✗ | ✓ | ✗ |

---

## Analisis

### Hallazgos principales

1. **Postura agrega valor real**: Nanobot 40% → Durin 80% (+100% relativo). Las iteraciones extra inducidas por cautela producen patches mas correctos.

2. **Deliberacion V2 es neutral-a-negativa en fresh**: Condicion 3 (60%) es peor que condicion 2 (80%). La deliberacion fresh-posture pierde un caso que sin-delib resuelve.

3. **Carry + delib es la mejor combinacion**: Unica que resuelve 14182 (el mas dificil). Ademas es la mas rapida de las Durin (199s vs 235s) con menos iteraciones (24.8 vs 30.2).

4. **6938 es inconsistente**: Lo resuelven condiciones 2 y 4 pero no 3 y 5. Sugiere que deliberacion desvía al agente en este caso concreto.

5. **N=5 es insuficiente** para conclusiones estadisticas. La diferencia entre 3/5 y 4/5 es un solo caso.

### Trayectoria postural — Carry

**Problema: drift geometrico**

En carry-posture, el valor final se usa como nueva media (bug en `swebench_eval.py:191`). Esto causa:
- `exploracion`: 0.5 → 0.567 → 0.700 → 0.833 → 0.967 → 1.000 (saturacion)
- `cautela`: sube consistentemente sin freno
- `profundidad`, `disciplina`: siempre 0.500 (nunca se activan)

**Sin carry (fresh)**, todos los runs convergen a valores similares:
- `exploracion` ≈ 0.567
- `cautela` ≈ 0.6-0.8
- Todo lo demas fijo

### Ejes muertos

| Eje | Por que no se mueve | Estimulo actual | Problema |
|---|---|---|---|
| profundidad | `GOAL_AMBIGUOUS` requiere iter sin tools ni content (nunca ocurre) | +0.10 si ambiguo | No hay señal de "necesitas pensar mas" |
| disciplina | `EXPLICIT_PROTOCOL` requiere markers en system prompt (nunca presentes) | +0.10 si protocolo | No hay señal de "segui un proceso" |

### Overhead de deliberacion

| Version | Overhead por instancia | Calls extra |
|---|---|---|
| V1 (evaluadores, multi-round) | ~400s (+170%) | ~15 LLM calls |
| V2 (sin evaluadores, 1 round) | ~12s (+5%) | 3 LLM calls |

V2 elimino el overhead pero tambien elimino el valor: sin evaluacion real, las perspectivas se inyectan sin filtrar.

---

## Problemas identificados

1. **No hay sistema de plan**: El agente ejecuta reactivamente sin planificar. Nanobot usa 13 iters, Durin usa 30 — pero no porque planifique mas, sino porque la postura lo hace validar mas.

2. **Carry-posture tiene bug de media**: El fix es trivial — al hacer carry, setear solo `valor_actual` sin cambiar `media`.

3. **Estimulos insuficientes**: 2 de 5 ejes nunca se activan. Se necesitan señales nuevas vinculadas al progreso real (cambios de fase, stuck detection, multi-file patterns).

4. **Deliberacion no suma sin plan**: Las perspectivas se inyectan al inicio y se diluyen en 30 iteraciones reactivas. Para que sumen, necesitarian acompañar un plan que se actualice.

---

## Decisiones de diseño (proximos pasos)

Ver `docs/07_diseño_plan_y_estimulos.md` para el detalle.

---

## Datos guardados

Carpeta: `benchmarks/swebench_5/`

| Archivo | Contenido |
|---|---|
| `*_predictions.jsonl` | Patches en formato SWE-bench |
| `*_stats.json` | Metricas agregadas por condicion |
| `*_detailed.jsonl` | Per-instance: tools, iters, posture_final |
| `eval_reports/*.json` | Resultados de evaluacion SWE-bench |

---

## Fecha: 2026-05-17
