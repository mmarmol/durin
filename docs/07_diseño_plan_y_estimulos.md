# Diseño: Plan, Estímulos y Reflexión

> Decisiones de diseño post-benchmark mayo 2026.
> Qué implementar, qué documentar para después, y por qué.

---

## 1. Sistema de Plan — IMPLEMENTAR

### Problema

El agente ejecuta reactivamente sin plan. En tareas de pocas iteraciones (2-3), salta directamente a editar sin explorar ni verificar. Ejemplo concreto: `astropy-6938` — el agente aplicó un fix incorrecto en 2 iters sin leer el archivo completo ni correr tests. Con un plan que fuerza el ciclo, habría sido mínimo 4 fases.

### Estado del arte — Agentes existentes

| Agente | Plan explícito | Verificación obligatoria | Plan vivo | Re-planificación | Historial para aprendizaje |
|---|---|---|---|---|---|
| **Hermes (NousResearch)** | Sí — tokens `<PLAN>/<EXECUTION>/<REFLECTION>` entrenados | No (gate suave) | Sí — goals persistentes, memoria multi-nivel | ReAct loop + RL (Atropos) | Sí — memoria persistente cross-session |
| **Devin (Cognition)** | Sí — plan upfront visible al usuario | Iterativo (tests en loop) | Sí — se actualiza al descomponer | Decompose→Execute→Analyze failure→Retry | Contexto multi-hora, sin learning público |
| **OpenHands/OpenDevin** | No rígido — CodeAct paradigm | Sandbox disponible, no obligatorio | Event stream (event-sourced) | Observation-action loop con error feedback | Event stream almacenado, replay determinístico |
| **SWE-Agent (Princeton)** | No — ReAct puro con ACI tools | Puede correr tests, no forzado | No — context window es la "memoria" | Error messages via ACI feedback | No cross-session |
| **Agentless (UIUC)** | Sí — pipeline rígido 3 fases | **Obligatoria** — regression + reproduction tests | No (estático) | No retry, samplea múltiples candidatos | No |
| **AutoCodeRover** | Semi — plan corto por iteración | Tests en patches generados | Contexto iterativo que evoluciona | Re-genera patches si tests fallan | No |
| **Claude Code (/plan)** | Sí — modo read-only para planificar | Humano revisa antes de ejecutar | Plan como markdown persistente | Humano re-entra a plan mode | No |
| **Aider** | Sí — Architect/Editor split | Linter + tests configurables, auto-fix | No formal | Retry automático en lint/test failures | Git como log implícito |

### Patrones académicos (2024-2025)

| Patrón | Mecanismo | Propiedad clave |
|---|---|---|
| **ReAct** | Thought→Action→Observation loop | Dinámico, sin plan upfront |
| **Plan-and-Execute** (LangChain) | Planner genera plan, Executor ejecuta pasos | Separación planning/execution |
| **Plan-Act-Correct-Verify** (2024) | 4 módulos iterativos | Verificador explícito — supera ReAct en tareas complejas |

**Hallazgo clave**: Los agentes con verificación obligatoria (Agentless) superan consistentemente a los que "pueden verificar pero no están forzados" (SWE-Agent, OpenHands). El plan como TODO funciona porque LLMs no saltan pasos de una checklist.

### Diseño propuesto para Durin — Modelo de 3 Tiers + Ciclo fijo

**Insight del benchmark**: El agente declara victoria sin verificar (5/5 "resueltos" internamente, solo 3/5 pasan tests reales). El sistema de plan obliga a verificar.

#### Modelo de 3 Tiers de ejecución

No toda tarea necesita un plan completo. El LLM elige el tier apropiado vía tool call al inicio:

```
┌─────────────────────────────────────────────────────────────┐
│  TIER 1 — DIRECTO                                           │
│  Respuestas, edits triviales.                               │
│  Sin artefactos. Hook no interviene.                        │
│  Ejemplo: "Qué hace esta función?" / "Renombrá X a Y"      │
├─────────────────────────────────────────────────────────────┤
│  TIER 2 — EJECUTA + VERIFICA                                │
│  Bug fix claro, cambio localizado.                          │
│  Hook inyecta recordatorio de verificar post-edit.          │
│  Sin plan ni bitácora persistente.                          │
│  Ejemplo: Fix de un test que falla, single-file change      │
├─────────────────────────────────────────────────────────────┤
│  TIER 3 — PLAN COMPLETO                                     │
│  Multi-paso, incertidumbre, cambios estructurales.          │
│  Ciclo fijo obligatorio + plan incremental + bitácora.      │
│  Ejemplo: Feature nueva, bug sin causa clara, refactoring   │
└─────────────────────────────────────────────────────────────┘
```

**Selección del tier**: El modelo recibe instrucciones sobre los 3 modos en el system prompt y declara cuál usar vía `set_execution_mode(tier)` como tool call. El hook captura la declaración y enforces el comportamiento correspondiente.

**Por qué tool call y no detección de patrón**: Es un gate explícito sin ambigüedad. Permite al hook reaccionar inmediatamente sin parsear texto libre.

#### Ciclo fijo (solo Tier 3)

**Filosofía**: No generamos un plan completo una vez. El plan EMERGE de un ciclo que se repite:

```
┌──────────────────────────────────────────────┐
│  CICLO FIJO (Tier 3 únicamente):             │
│                                              │
│    INVESTIGA → PLANEA → EJECUTA → CONFIRMA   │
│        ↑                              │      │
│        └──────── si falla ────────────┘      │
└──────────────────────────────────────────────┘
```

- **INVESTIGA**: Lee archivos, entiende contexto. No puede editar.
- **PLANEA**: Formula/actualiza pasos concretos. Plan crece incrementalmente.
- **EJECUTA**: Aplica cambios (edit, write).
- **CONFIRMA**: Verifica (exec tests, validación). Oracle real — NO se puede saltar.

El plan es **incremental** (como metodologías ágiles): cada ciclo puede agregar pasos, modificar existentes, o marcar completos. La bitácora registra cada modificación.

#### Artefactos por tier

| Tier | Plan en disco | Bitácora (events.jsonl) | Postura reacciona |
|---|---|---|---|
| 1 | No | No | Sí (stimuli normales) |
| 2 | No | No | Sí + VALIDATION_SUCCESS/FAILURE |
| 3 | Sí | Sí | Sí + Capa 2 (CONFIRM) + Capa 3 (bias) |

Solo Tier 3 genera artefactos persistentes. Los otros son "gratis" en overhead.

### Implementación

```python
# durin/plan/types.py

class ExecutionTier(StrEnum):
    DIRECT = "direct"           # Tier 1: respuestas, trivial
    EXECUTE_VERIFY = "execute_verify"  # Tier 2: edit + verify
    FULL_PLAN = "full_plan"     # Tier 3: ciclo completo

class Phase(StrEnum):
    INVESTIGATE = "investigate"
    PLAN = "plan"
    EXECUTE = "execute"
    CONFIRM = "confirm"

@dataclass
class PlanItem:
    description: str
    status: Literal["pending", "in_progress", "done", "failed"]
    added_at_cycle: int
    completed_at_cycle: int | None = None

@dataclass  
class PlanState:
    tier: ExecutionTier
    goal: str
    items: list[PlanItem]          # Solo populated en Tier 3
    current_phase: Phase | None    # None para Tier 1-2
    cycle_count: int
```

```python
# durin/plan/tool.py — Tool que el LLM llama para declarar tier

class SetExecutionModeTool(Tool):
    """El LLM declara qué tier de ejecución usar."""
    name = "set_execution_mode"
    parameters = {
        "tier": {"type": "string", "enum": ["direct", "execute_verify", "full_plan"]},
        "reason": {"type": "string", "description": "Por qué este tier (1 oración)"}
    }
```

```python
# durin/plan/hook.py — PlanHook

class PlanHook(AgentHook):
    """Manages execution tiers. Only enforces cycle for Tier 3."""
    
    _state: PlanState
    _store_path: Path  # workspace/plans/{session_key}/
    
    async def before_iteration(self, ctx):
        if self._state.tier == ExecutionTier.DIRECT:
            return  # No intervention
        
        if self._state.tier == ExecutionTier.EXECUTE_VERIFY:
            # Solo inyecta recordatorio post-edit: "Verificá con tests"
            if self._detected_edit_last_iter:
                ctx.inject("Recordá verificar tu cambio con tests antes de declarar completo.")
            return
        
        # Tier 3: Inyecta estado completo del plan + fase
        # "[Plan] Ciclo 2 | Fase: CONFIRMA
        #  1. [✓] Fix replace in fitsrec.py  
        #  2. [→] Verificar con tests
        #  Acción requerida: ejecutá los tests relevantes."
        
    async def after_iteration(self, ctx):
        # 1. Detecta fase actual por tools usadas
        # 2. Detecta transiciones de fase
        # 3. Captura resultado de CONFIRM (oracle)
        # 4. Actualiza plan items
        # 5. Emite eventos de postura
        # 6. Append a bitácora en disco
```

### Inyección en prompt (lo que el LLM ve)

```
[Plan System]
Ciclo 1 | Fase: EJECUTAR
Plan actual:
  1. [✓] Entender: output_field es numpy chararray view (in-place)
  2. [→] Implementar: usar output_field[...] = para escritura in-place
  3. [ ] Verificar: correr tests astropy.io.fits
Bitácora: 
  [C1-INVEST] Leído fitsrec.py:1255-1270, chararray.replace() retorna copia
```

El LLM NO puede declarar "done" con paso 3 pendiente — es un TODO activo.

### Almacenamiento en disco (para auditoría y aprendizaje)

```
workspace/plans/{session_key}/
  events.jsonl    — append-only, cada evento del ciclo
  summary.json    — al finalizar: outcome, cycles, plan evolution
```

```jsonl
{"ts": ..., "type": "cycle_start", "cycle": 1, "phase": "investigate"}
{"ts": ..., "type": "phase_transition", "from": "investigate", "to": "plan"}
{"ts": ..., "type": "plan_item_added", "item": "Fix replace in fitsrec.py", "cycle": 1}
{"ts": ..., "type": "phase_transition", "from": "execute", "to": "confirm"}
{"ts": ..., "type": "confirm_result", "outcome": "fail", "signal": "pytest exit=1"}
{"ts": ..., "type": "cycle_start", "cycle": 2, "phase": "investigate"}
{"ts": ..., "type": "plan_item_modified", "item": "Usar [...] assignment", "reason": "replace returns copy"}
{"ts": ..., "type": "confirm_result", "outcome": "pass", "signal": "pytest exit=0"}
{"ts": ..., "type": "plan_completed", "cycles": 2, "total_iters": 8}
```

**Valor a futuro**: Este log permite detectar patrones (qué tareas requieren >2 ciclos, qué tipo de planes iniciales suelen ser incorrectos, correlación entre investigación insuficiente y fallos en confirmación).

---

## 2. Estímulos — Modelo de 3 capas

### Arquitectura de ajustes de postura

Los estímulos operan en 3 frecuencias complementarias que conviven:

```
Capa 1 — PER-ITERACIÓN (rápida, ya implementada):
  step_succeeded, consecutive_3, STUCK, MULTI_FILE_EDIT...
  Micro-ajustes: ±0.02-0.08 por evento
  Función: reaccionar al momento inmediato

Capa 2 — TRANSICIÓN DE FASE del ciclo (media, nueva):
  CONFIRM pass/fail, ciclo 2+ iniciado, re-plan triggered
  Macro-ajustes: ±0.10-0.15 por transición
  Función: reaccionar al resultado real (oracle)

Capa 3 — CREACIÓN/RE-CREACIÓN del plan (lenta, nueva):
  Al generar el plan: evaluar complejidad → bias inicial
  One-shot: ajuste al inicio del ciclo
  Función: preparar la postura para lo que viene
```

### Capa 1: Estímulos por iteración (IMPLEMENTADO)

| Evento | Ejes | Delta | Estado |
|---|---|---|---|
| `STEP_FAILED` | cautela +0.10, profundidad +0.05 | | ✓ |
| `CONSECUTIVE_SUCCESSES_3` | exploracion +0.02, profundidad -0.03 | | ✓ |
| `CONSECUTIVE_FAILURES_3` | cautela +0.15, conformidad -0.10 | | ✓ |
| `MULTI_FILE_EDIT` | disciplina | +0.08 | ✓ |
| `VALIDATION_SUCCESS` | cautela -0.05, exploracion -0.03 | | ✓ |
| `VALIDATION_FAILURE` | cautela +0.10, profundidad +0.08 | | ✓ |
| `STUCK_NO_PROGRESS` | exploracion +0.10, profundidad +0.10 | | ✓ |
| `PHASE_TRANSITION` | profundidad | -0.10 | ✓ |

**Removido**: `STEP_SUCCEEDED` ya no afecta cautela. Señal demasiado débil (ausencia de error ≠ progreso). Cautela solo baja con oracle real (VALIDATION_SUCCESS).

### Calibración de pesos — Targets de comportamiento

Config cautela: media=0.6, varianza=0.15, fuerza_retorno=0.3, bounds=[0.30, 0.90]

| Escenario | Target | Resultado simulado | Criterio |
|---|---|---|---|
| 3 fallos consecutivos | Cautela ~0.85+ | Peak 0.858, recupera en 8 iters | Agente replantea completamente |
| 1 VALIDATION_FAILURE | +10-15% | +12% (0.60→0.67) | Investiga antes de re-editar |
| 1 VALIDATION_SUCCESS | Baja poco | -6% (0.60→0.565) | Confianza sin relajarse |
| Operación normal | Estable en media | 0.600 constante | Sin drift |
| Fail→investiga→test pasa | Recuperación natural | 0.67→0.589 | Ciclo saludable |
| 2 iters sin test (caso 6938) | NO debe bajar | 0.600 estable | Sin recompensa falsa |

**Asimetría intencional**: subir cautela (+0.10) es 2x bajarla (-0.05). Perder confianza es fácil, ganarla requiere oracle.

**Evolución futura**: La bitácora del plan registra postura en cada decisión + outcome. Después de N sesiones permite correlacionar rangos de postura con resolve rate → ajustar deltas para mantener rango óptimo empírico.

### Capa 2: Estímulos del ciclo de plan (POR IMPLEMENTAR)

| Evento | Ejes | Delta | Trigger |
|---|---|---|---|
| `CONFIRM_PASS` | cautela -0.10, exploracion -0.05 | | Tests pasan en fase CONFIRM |
| `CONFIRM_FAIL` | cautela +0.15, profundidad +0.10 | | Tests fallan en fase CONFIRM |
| `CYCLE_2_PLUS` | disciplina +0.05, profundidad +0.05 | | Inicio de ciclo 2 o posterior |
| `REPLAN_TRIGGERED` | exploracion +0.10 | | Plan modificado por fallo |

### Capa 3: Bias por plan (POR IMPLEMENTAR)

| Señal del plan | Ajuste | Razón |
|---|---|---|
| Plan con >3 pasos | profundidad +0.10, cautela +0.05 | Tarea compleja, necesita cuidado |
| Plan con 1 paso | mantener defaults | Tarea simple, no sobre-pensar |
| Re-plan (ciclo 2+) | cautela +0.10, exploracion +0.05 | Primer approach falló, necesita alternativas |

### Relación entre capas

Las capas NO se contradicen — operan en frecuencias distintas:
- Capa 1: "este step individual fue bien" (señal débil, frecuente)
- Capa 2: "el ciclo completo funcionó/falló" (señal fuerte, poco frecuente)
- Capa 3: "este problema va a ser difícil" (señal contextual, una vez por tarea)

Las capas se suman. No se eliminan las existentes — solo se agregan señales más fuertes en momentos más significativos.

---

## 3. Metacognición / Auto-reflexión — DOCUMENTAR (no implementar aún)

### Por qué no implementar ahora

Evidencia del benchmark y literatura:
- Sin oracle externo (tests pass/fail), la reflexión refuerza errores propios
- Rendimientos decrecientes después de 1-2 iteraciones
- ReMA (NeurIPS 2025) requiere entrenamiento via RL — inaccesible
- Reflexion (Shinn 2023) refleja ENTRE intentos, no mid-task
- Nuestro benchmark mostró que evaluadores LLM no suman sin verificación real

### Cuándo tendría sentido

- Cuando tengamos **plan con ciclo CONFIRM** → el oracle (tests) da señal real
- Cuando tengamos **bitácora persistente** → datos para detectar patrones
- Cuando los tasks sean **suficientemente largos** (>50 iteraciones) que justifiquen el costo

### Insight clave: Oracle + Aprendizaje

Los eventos de éxito/fracaso (oracle) tienen valor dual:
1. **Inmediato**: decidir si re-planificar para la tarea actual
2. **A futuro**: material para ajustar defaults posturales por tipo de tarea

Esto conecta con consolidación futura: los momentos de éxito/fracaso marcados en la bitácora se convierten en datos para evolución del agente.

---

## 4. Fix del bug carry-posture — ✓ IMPLEMENTADO

Separar `valor_actual` de `media` en el schema. Fix aplicado en commit `453a070`.

---

## 5. Benchmark mayo 2026 — Resultados post-fix

### Segunda ejecución (carry-posture fix + nuevos estímulos)

| Condición | Resultado | Resueltas |
|---|---|---|
| Durin nodelib (sin delib) | 3/5 | 12907, 14365, 14995 |
| Durin delib V2 | 3/5 | 12907, 14182, 14995 |
| Durin carry + nodelib | 3/5 | 12907, 14182, 14995 |
| Durin carry + delib | 3/5 | 12907, 14365, 14995 |

### Comparación con primera ejecución

| Condición | Antes → Después | Cambio |
|---|---|---|
| nodelib | 4/5 → 3/5 | Perdió 6938 |
| delib V2 | 3/5 → 3/5 | Ganó 14182, perdió 14365 |
| carry + nodelib | 3/5 → 3/5 | Ganó 14182, perdió 6938 |
| carry + delib | 4/5 → 3/5 | Perdió 14182 |

### Análisis

- **6938 regresión consistente**: Agente va en 2-4 iters sin verificar. Cree que resolvió (5/5 interno) pero SWE-bench dice que no. Caso exacto para CONFIRM obligatorio.
- **14182 mejora**: Tarea compleja (40-69 iters), los nuevos estímulos ayudan en exploraciones largas.
- **Profundidad ahora se mueve**: 0.42-0.73 (antes fijo en 0.5). Los nuevos estímulos funcionan.
- **Conclusión**: No hay regresión por los cambios. Variabilidad LLM en instancias borderline. El plan system con CONFIRM resolverá el caso 6938.

---

## 6. Orden de implementación actualizado

```
1. [✓] Fix carry-posture bug
2. [✓] Nuevos estímulos capa 1 (9 reglas nuevas, 12→21 total)
3. [✓] Re-benchmark — validado: postura se mueve, no hay regresión
4. [→] Sistema de plan: modelo 3 tiers + ciclo fijo + bitácora
   4a. [ ] Types: ExecutionTier, Phase, PlanItem, PlanState
   4b. [ ] Tool: set_execution_mode (LLM declara tier)
   4c. [ ] PlanHook: enforce por tier (Tier 1 noop, Tier 2 reminder, Tier 3 cycle)
   4d. [ ] Storage: plan.json + events.jsonl en workspace/plans/{session}/
   4e. [ ] Inyección prompt: instrucciones de 3 tiers en system prompt
5. [ ] Conectar plan → estímulos capa 2 (CONFIRM_PASS/FAIL, CYCLE_2_PLUS)
6. [ ] Bias por plan capa 3 (ajuste inicial por complejidad)
7. [ ] Re-benchmark con plan system (mismas 5 instancias, target: 6938)
8. [ ] Metacognición (cuando plan + oracle estén estables)
```

---

## 6. Referencias

| Sistema/Paper | Año | Relevancia para Durin |
|---|---|---|
| **Hermes Agent (NousResearch)** | 2025 | Tokens PLAN/EXECUTION/REFLECTION entrenados, goals persistentes, memoria multi-nivel. Modelo más cercano a nuestro diseño |
| **Devin 2.0 (Cognition)** | 2025 | Plan vivo visible al usuario, iteración hasta éxito, 18% mejora en planning |
| **Agentless (UIUC)** | 2024 | Verificación OBLIGATORIA es lo que lo hace funcionar. 32% SWE-bench |
| **Plan-Act-Correct-Verify** | 2024 | 4 módulos con verificador explícito supera ReAct. Valida nuestro ciclo |
| **OpenHands** | 2025 | Event-sourced state, CodeAct. Event stream almacenado para replay |
| **SWE-Agent (Princeton)** | 2024 | ReAct sin plan = sin verificación obligatoria = inferior |
| **Manus Context Engineering** | 2025 | Plan-file como attention manipulation. 30% tokens en rewrites |
| **ReMA (NeurIPS)** | 2025 | Meta-agent monitorea progreso. Requiere RL, inspiración para futuro |
| **Mind Evolution (DeepMind)** | 2025 | Evolución con LLM. Inspiró deliberación original |
| **Cambridge Position Paper** | 2025 | Framework formal: metacognitive knowledge + planning + evaluation |

---

## Fecha: 2026-05-17
