# Bitacora de benchmark — SWE-bench evaluation

> Evaluacion objetiva de Durin como sistema agentico usando SWE-bench Lite.
> Comparacion directa contra baseline publicado de GLM-5.1 + OpenHands (58.4%).

---

## Objetivo

Medir si el sistema de deliberacion evolutiva de Durin agrega valor medible sobre el modelo base.

| Condicion | Modelo | Agente | Deliberacion | Budget |
|---|---|---|---|---|
| **Baseline publicado** | GLM-5.1 | OpenHands | No aplica | 100 steps |
| **A: Durin sin delib** | GLM-5.1 | Durin | OFF | 100 iterations |
| **B: Durin con delib** | GLM-5.1 | Durin | ON (evolutiva) | 100 iterations |

**Metrica**: % de issues resueltos (tests pasan) en SWE-bench Lite (300 instancias).

---

## Setup

### API

- **Endpoint**: `https://api.z.ai/api/coding/paas/v4` (OpenAI-compatible)
- **Modelo principal**: `glm-5.1` (754B MoE, Z.ai/Zhipu)
- **Modelo deliberacion**: `glm-5-turbo` (para generadores baratos)
- **Context window**: 200K tokens (GLM-5.1)

### Baseline publicado

GLM-5.1 fue evaluado por Z.ai con:
- **Agente**: OpenHands (open-source)
- **Budget**: 100 steps max por issue
- **Parametros**: temperature=1, top_p=0.95, max_new_tokens=32768
- **Score SWE-bench Pro**: 58.4%
- **Fuente**: arXiv:2602.15763 "GLM-5: from Vibe Coding to Agentic Engineering"

Nota: el baseline es SWE-bench **Pro** (mas dificil que Lite). Nuestras runs son en **Lite**. No comparamos directamente score vs score, pero la tendencia relativa entre condiciones A y B es valida.

### Hardware evaluacion

- **Generacion de patches**: Mac ARM (Apple Silicon)
- **Evaluacion de patches**: Docker Desktop (aarch64, Rosetta para x86 si es necesario)
- **Validado**: 1 issue Django evaluada OK en ARM (2:17 min)

---

## Adapter

Script: `scripts/swebench_eval.py`

Pipeline por instancia:
1. Clone/checkout del repo al commit base (cache en `/tmp/swebench_repos`)
2. Instanciar Durin con workspace=repo, config apuntando a GLM-5.1
3. Prompt: issue description + instruccion de fix
4. Durin trabaja: lee archivos, edita, ejecuta (max 100 iterations)
5. Captura `git diff` como `model_patch`
6. Append a predictions JSONL
7. Evaluacion via `swebench.harness.run_evaluation` (Docker)

Configuracion deliberacion:
- OFF: `posture.enabled=false`, `deliberation.enabled=false`
- ON: ambos enabled, provider apuntando a `glm-5-turbo` para generadores/evaluadores

---

## Metricas a capturar

| Metrica | Por instancia | Agregado |
|---|---|---|
| Patch generado (si/no) | si | % con patch no-vacio |
| Tests pasan | si | % resolved (la metrica clave) |
| Tiempo | si | promedio, p50, p95 |
| Iteraciones usadas | si | promedio |
| Tools invocados | si | distribucion |
| Errores/timeouts | si | conteo total |
| Tokens consumidos | si (via usage) | costo estimado |

---

## Estado

| Paso | Estado | Notas |
|---|---|---|
| API GLM-5.1 verificada | ✅ | Responde OK, usa reasoning_content |
| swebench instalado (Python 3.12) | ✅ | v4.1.0 |
| Docker ARM funciona | ✅ | 1 issue validada (django__django-10914) |
| Adapter script creado | ✅ | `scripts/swebench_eval.py` |
| Durin importable desde venv | ✅ | SDK + providers funcionan |
| Run piloto (1 issue, sin delib) | pendiente | |
| Run completo condicion A (sin delib) | pendiente | |
| Run completo condicion B (con delib) | pendiente | |
| Evaluacion final | pendiente | |
| Analisis comparativo | pendiente | |

---

## Notas tecnicas

1. **GLM-5.1 usa extended thinking** — el campo `reasoning_content` se devuelve separado del `content`. El provider OpenAI-compat de Durin ya lo soporta.

2. **Budget = iterations, no steps** — OpenHands cuenta "steps" (que incluyen observaciones). Durin cuenta iteraciones del loop (cada una puede incluir multiples tool calls). Un iteration de Durin ~ 1-3 steps de OpenHands. Budget efectivo es comparable pero no identico.

3. **SWE-bench Lite vs Pro** — Lite tiene 300 instancias (subset mas facil). Pro tiene 2294. El baseline publicado es Pro. Para comparar A vs B (nuestra meta principal) esto no importa. Para comparar con el 58.4% publicado, hay que notar que Lite deberia dar scores MAS ALTOS que Pro.

4. **Deliberacion necesita provider funcional** — Si usamos GLM-5-turbo para generadores, el costo por deliberacion es ~3 calls x 3 rounds = 9 LLM calls extra por issue. Con tokens de turbo barato, ~$0.01-0.05 extra por issue.

---

## Fecha inicio: 2026-05-17
