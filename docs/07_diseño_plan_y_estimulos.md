# Diseño: Plan, Estímulos y Reflexión

> Decisiones de diseño post-benchmark mayo 2026.
> Qué implementar, qué documentar para después, y por qué.

---

## 1. Sistema de Plan — IMPLEMENTAR

### Problema

El agente ejecuta reactivamente sin plan. En 30+ iteraciones pierde dirección. La postura lo hace más cauteloso pero no más inteligente — valida más pero sin strategy.

### Estado del arte

| Sistema | Mecanismo | Resultado |
|---|---|---|
| Manus | `todo.md` reescrito cada step (attention manipulation) | Funciona bien, ~30% tokens en rewrites |
| planning-with-files | 3 archivos (plan, findings, progress) + hooks | 9.6k stars, popular en Claude Code |
| Superpowers | Plan detallado → TodoWrite → checkpoint cada 3 tasks | 150k stars |
| Nanobot | Solo `long_task` (string de objetivo, sin pasos) | Insuficiente |
| Agentless (2024) | Sin agente, pipeline Localizar→Reparar→Validar | 32% SWE-bench a $0.70/fix |

### Diseño propuesto para Durin

**Nivel 1** (implementar primero): Plan interno como state del hook

```python
@dataclass
class TaskPlan:
    goal: str
    phases: list[Phase]          # investigar, implementar, validar
    current_phase: int
    findings: list[str]          # descubrimientos relevantes
    steps_in_phase: int          # iteraciones en la fase actual
    total_steps: int

@dataclass
class Phase:
    name: str
    description: str
    done_condition: str
    started_at: int | None       # iteration number
    completed_at: int | None
```

**Nivel 2** (a futuro): Plan como archivo persistente (para tareas largas que sobreviven compactación).

### Cómo se genera el plan

Opción A: La deliberación lo produce (en vez de perspectivas sueltas, produce un plan estructurado).
Opción B: El propio LLM principal lo produce como primer step (tool `create_plan`).
Opción C: Heurísticas sin LLM (detectar tipo de tarea → plan template).

**Decisión pendiente** — evaluar tras implementar plan básico.

### Cómo alimenta a la postura

| Señal del plan | Eje afectado | Mecanismo |
|---|---|---|
| Cambio de fase detectado | profundidad | Reset a media (nueva fase = mente fresca) |
| Stuck: N iters sin avanzar de fase | exploración | +delta (necesita alternativas) |
| Finding que contradice approach | profundidad | +delta (necesita re-pensar) |
| Validación exitosa | cautela | −delta (confirmación de progreso) |
| Múltiples files editados | disciplina | +delta (necesita método) |

---

## 2. Metacognición / Auto-reflexión — DOCUMENTAR (no implementar aún)

### Por qué no implementar ahora

Evidencia del benchmark y literatura:
- Sin oracle externo (tests pass/fail), la reflexión refuerza errores propios
- Rendimientos decrecientes después de 1-2 iteraciones
- ReMA (NeurIPS 2025) requiere entrenamiento via RL — inaccesible
- Reflexion (Shinn 2023) refleja ENTRE intentos, no mid-task
- Nuestro benchmark mostró que evaluadores LLM no suman sin verificación real

### Cuándo tendría sentido

- Cuando tengamos **plan con fases** → la transición de fase es un natural checkpoint
- Cuando tengamos **oracle** (tests runner integrado que diga pass/fail)
- Cuando los tasks sean **suficientemente largos** (>50 iteraciones) que justifiquen el costo

### Diseño conceptual (para Fase 3)

```
Cada N iteraciones (o en cambio de fase):
1. LLM barato recibe: plan + progreso + últimas 5 acciones
2. Pregunta: "¿Estás avanzando? ¿Necesitás cambiar de approach?"
3. Output: {progressing: bool, stuck: bool, suggested_adjustment: str}
4. Si stuck → señal para postura + posible re-plan
```

**Prerequisito**: sistema de plan implementado.

### Insight del usuario sobre oracle y aprendizaje

Marcelo observó que los eventos de éxito/fracaso (oracle) tienen valor dual:
1. **Inmediato**: decidir si disparar auto-reflexión para la tarea actual
2. **A futuro**: marcar el evento como candidato para aprendizaje general (consolidación)

Esto conecta con Fase 3 (consolidación/sueño): los momentos de éxito/fracaso marcados se convierten en material para ajustar medias del vector postural a largo plazo.

---

## 3. Re-evaluación periódica del plan — IMPLEMENTAR (con plan)

### Mecanismo

No es "LLM reflexiona". Es detección heurística de progreso basada en el plan:

```python
def _check_plan_progress(self, plan: TaskPlan, iteration: int) -> set[StimulusEvent]:
    events = set()
    steps_in_phase = iteration - (plan.phases[plan.current_phase].started_at or 0)
    
    # Stuck detection: muchas iteraciones sin avanzar
    if steps_in_phase > STUCK_THRESHOLD:
        events.add(StimulusEvent.STUCK_NO_PROGRESS)
    
    # Phase transition: detectar que el agente pasó a otra actividad
    if self._detect_phase_change(plan, context):
        events.add(StimulusEvent.PHASE_TRANSITION)
    
    return events
```

### Frecuencia

- Cada iteración: check barato (contadores, heurísticas)
- En cambio de fase: re-evaluar plan (posiblemente con LLM si hay oracle disponible)
- Al final de la tarea: marcar resultado para consolidación futura

---

## 4. Estímulos faltantes — IMPLEMENTAR

### Nuevos estímulos propuestos

| Evento | Eje | Delta | Trigger |
|---|---|---|---|
| `PHASE_TRANSITION` | profundidad | reset (→ media) | Detectar cambio de tipo de actividad (read→edit, explore→implement) |
| `STUCK_NO_PROGRESS` | exploración +0.10, profundidad +0.10 | N iteraciones sin avance de fase en el plan |
| `MULTI_FILE_EDIT` | disciplina +0.08 | edit_file invocado en >2 archivos distintos |
| `VALIDATION_SUCCESS` | cautela −0.05, exploración −0.03 | exec con exit_code 0 que parece test/validación |
| `VALIDATION_FAILURE` | cautela +0.10, profundidad +0.05 | exec con exit_code != 0 en contexto de validación |
| `FINDING_RELEVANT` | profundidad +0.05 | El agente descubre info que cambia el approach |

### Ajustes a estímulos existentes

| Cambio | Razón |
|---|---|
| `CONSECUTIVE_SUCCESSES_3` delta: 0.05 → 0.02 | Sobre-estimula exploración, tools casi nunca fallan |
| Agregar `CONSECUTIVE_SUCCESSES_3` a profundidad: −0.03 | Si todo va bien, no necesitás pensar tanto |
| `GOAL_AMBIGUOUS` trigger: relajar condición | Actualmente imposible de activar, buscar proxy mejor |

### Detección de PHASE_TRANSITION (sin LLM)

```python
# Heurística basada en patrón de tools usados
EXPLORATION_TOOLS = {"read_file", "grep", "list_dir", "web_search"}
IMPLEMENTATION_TOOLS = {"edit_file", "write_file"}
VALIDATION_TOOLS = {"exec"}

def _detect_phase_change(self, recent_tools: list[str], window: int = 5) -> bool:
    """Si los últimos N tools son mayoritariamente de un tipo distinto al anterior."""
    ...
```

---

## 5. Fix del bug carry-posture — IMPLEMENTAR (trivial)

### Problema

En `scripts/swebench_eval.py:191`, al hacer carry:
```python
axes[axis_name] = {"media": val, ...}  # BUG: pone valor_actual como media
```

### Fix

```python
axes[axis_name] = {
    "media": defaults["media"],          # mantener media original
    "varianza": defaults["varianza"],
    "fuerza_retorno": defaults["fuerza_retorno"],
    "valor_actual": val,                 # solo esto cambia
}
```

Esto es configurable via el schema — verificar que `AxisState` acepte `valor_actual` distinto de `media`.

---

## 6. Orden de implementación

```
1. Fix carry-posture bug (5 min, trivial)
2. Nuevos estímulos + ajustes a existentes (posture/stimulus.py, hook.py)
3. Sistema de plan básico (nuevo módulo durin/plan/)
4. Conectar plan → estímulos (PHASE_TRANSITION, STUCK_NO_PROGRESS)
5. Re-benchmark (mismas 5 instancias para comparar)
```

Metacognición queda documentada para cuando tengamos plan + oracle.

---

## 7. Referencias académicas

| Paper | Año | Relevancia para Durin |
|---|---|---|
| ReMA (NeurIPS) | 2025 | Meta-agent monitorea progreso, interviene. Inspiración para Fase 3 |
| Metacognition is All You Need? | 2024 | System 1/2, detecta goal drift. Valida concepto |
| Reflexion (Shinn, NeurIPS) | 2023 | Solo entre intentos, no mid-task. No aplica directo |
| Self-Refine (NeurIPS) | 2023 | LLM se critica a sí mismo. Funciona en output, no en strategy |
| Agentless | 2024 | Pipeline simple > agente complejo para bug fixing. Contrapunto |
| MIRROR | 2025 | Inner monologue persistente entre turnos. Reconstructivo |
| Cambridge Position Paper (ICML) | 2025 | Framework formal: metacognitive knowledge + planning + evaluation |
| Mind Evolution (DeepMind) | 2025 | Evolución con LLM. Inspiró diseño original de deliberación |
| Manus Context Engineering | 2025 | Plan-file como attention manipulation. Implementación práctica |

---

## Fecha: 2026-05-17
