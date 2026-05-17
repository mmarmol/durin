# Durin — Arquitectura Operativa

> Documento de referencia rápida para entender cómo funciona Durin internamente.
> **Mantener actualizado** cuando se modifiquen módulos core.

---

## 1. Origen y relación con Nanobot

Durin es un fork de [nanobot](vendor/nanobot/) (framework de agente ligero). Hereda:
- Agent loop (`runner.py`), bus de mensajes, canales, tools, session management
- Estructura de providers (Anthropic, OpenAI-compat, Azure, Bedrock, etc.)
- Skills, commands, memory (Dream consolidation)
- `long_task` / `complete_goal` para tracking de objetivos

**Durin agrega** sobre nanobot:
- Sistema de postura (vector de 5 ejes)
- Sistema de deliberación V3 (single-call multi-perspectiva + merge)
- Sistema de plan (3 tiers de ejecución + ciclo fijo + bitácora)
- Telemetría postural
- Hook factory que wirea postura + plan (con deliberación integrada) automáticamente

---

## 2. Flujo de una iteración

```
┌─────────────────────────────────────────────────────────────┐
│                    AgentRunner.run()                          │
│  for iteration in range(max_iterations):                     │
│                                                              │
│  1. Context governance (microcompact, snip, budget)          │
│  2. Build AgentHookContext(iteration, messages)              │
│  3. hook.before_iteration(context)                           │
│     ├── PostureHook: iter 0 → goal_bias + protocol_bias     │
│     ├── PlanHook: INVESTIGATE→PLAN triggers deliberation     │
│     └── PlanHook: inject tier instructions / phase prompt    │
│  4. LLM request → response                                  │
│  5. Parse response (tool_calls, content, reasoning)          │
│  6. If tool_calls:                                           │
│     a. hook.before_execute_tools(context)                    │
│     b. Execute tools (sequential or concurrent)              │
│     c. Append tool results to messages                       │
│  7. hook.after_iteration(context)                            │
│     ├── PostureHook: detect events → update vector           │
│     └── PlanHook: infer phase transitions, emit stimuli      │
│  8. If no tool_calls → final_content → break                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Sistema de Postura

### Archivos clave
| Archivo | Responsabilidad |
|---|---|
| `posture/vector.py` | Modelo de datos: `PostureVector`, `AxisState`, `AxisName` |
| `posture/hook.py` | `PostureHook` — lifecycle hook que detecta eventos y actualiza vector |
| `posture/stimulus.py` | `StimulusTable` — mapeo evento → delta por eje |
| `posture/homeostasis.py` | `update_vector` — return-to-mean + stimulus + clamp |
| `posture/goal_bias.py` | Cold-start: keywords en goal → deltas iniciales |
| `posture/phrase.py` | Traduce vector a frase textual para inyectar en prompts |
| `posture/persistence.py` | Guardar/cargar vector entre sesiones |

### Los 5 ejes
| Eje | Media default | Varianza | Fuerza retorno | Función |
|---|---|---|---|---|
| cautela | 0.6 | 0.15 | 0.3 | Peso del riesgo |
| exploracion | 0.4 | 0.20 | 0.4 | Explorar vs explotar |
| profundidad | 0.5 | 0.20 | 0.5 | Pensar vs actuar rápido |
| disciplina | 0.5 | 0.15 | 0.2 | Seguir protocolo vs improvisar |
| conformidad | 0.7 | 0.15 | 0.3 | Aceptar vs objetar tarea |

### Fórmula de actualización (cada iteración)
```
1. Return to mean:  valor += fuerza_retorno × (media − valor)
2. Apply stimulus:  valor += delta × (varianza / 0.15)
3. Clamp:           valor ∈ [media − 2×varianza, media + 2×varianza]
```

### Estímulos activos
| Evento | Eje(s) afectado(s) | Delta | Trigger en código |
|---|---|---|---|
| `STEP_FAILED` | cautela +0.10, profundidad +0.05 | Error o tool failure |
| `CONSECUTIVE_SUCCESSES_3` | exploración +0.02, profundidad −0.03 | 3 éxitos seguidos |
| `CONSECUTIVE_FAILURES_3` | cautela +0.15, conformidad −0.10 | 3 fallos seguidos |
| `GOAL_AMBIGUOUS` | profundidad +0.10 | Iter sin tools, sin content, sin error (raro) |
| `USER_CORRECTED` | conformidad +0.05 | Mensajes inyectados en la sesión |
| `USER_APPROVED_RISKY` | cautela −0.05 | (No implementado como trigger activo) |
| `CRITICAL_ACTION` | cautela +0.10 | Tool en CRITICAL_TOOLS set |
| `EXPLORATORY_TASK` | exploración +0.10 | (Solo via goal_bias keywords) |
| `EXPLICIT_PROTOCOL` | disciplina +0.10 | Markers como "## checklist" en system prompt |
| `MULTI_FILE_EDIT` | disciplina +0.08 | Editar >1 archivo en una iteración |
| `VALIDATION_SUCCESS` | cautela −0.05, exploración −0.03 | Tests pasan (oracle real) |
| `VALIDATION_FAILURE` | cautela +0.10, profundidad +0.08 | Tests fallan |
| `STUCK_NO_PROGRESS` | exploración +0.10, profundidad +0.10 | Sin progreso detectado |
| `PHASE_TRANSITION` | profundidad −0.10 | Cambio de fase en plan cycle |
| `CONFIRM_PASS` | cautela −0.10, exploración −0.05 | Plan: tests pasan en CONFIRM |
| `CONFIRM_FAIL` | cautela +0.15, profundidad +0.10 | Plan: tests fallan en CONFIRM |
| `CYCLE_RESTART` | disciplina +0.05, exploración +0.10 | Plan: reinicio de ciclo |
| `PLAN_COMPLEX` | profundidad +0.10, cautela +0.05 | Plan: >3 items (one-shot) |

**Nota**: `STEP_SUCCEEDED` fue **removido** — ausencia de error ≠ progreso real. Cautela solo baja con oracle (VALIDATION_SUCCESS, CONFIRM_PASS) o aprobación explícita (USER_APPROVED_RISKY).

### Diseño asimétrico de cautela
- Subir cautela: +0.10 a +0.15 por evento negativo (señal fuerte)
- Bajar cautela: −0.05 a −0.10 solo con validación real (señal débil, requiere oracle)
- Filosofía: un agente sobre-cauteloso es lento pero seguro; un agente sub-cauteloso genera falsos positivos

### Problemas conocidos (benchmark mayo 2026)
- **carry-posture** tiene bug: pone `valor_actual` como nueva `media`, causando drift geométrico
- **profundidad** y **disciplina** ahora se activan via MULTI_FILE_EDIT, VALIDATION_FAILURE, PHASE_TRANSITION (corregido)
- **exploración** reducida a +0.02 por CONSECUTIVE_SUCCESSES_3 (era +0.05, sobre-estimulaba)

---

## 4. Sistema de Deliberación (V3)

Single-call multi-perspective deliberation. No es un hook independiente — es un servicio inyectado en PlanHook.

### Archivos clave
| Archivo | Responsabilidad |
|---|---|
| `deliberation/engine.py` | `DeliberationEngine` — 1 LLM call con prompt estructurado |
| `deliberation/service.py` | `DeliberationService` — orquesta engine + telemetría |
| `deliberation/synthesis.py` | `render_for_injection()` — formatea para contexto del agente |
| `deliberation/types.py` | `Perspective`, `DeliberationResult`, `DeliberationContext`, `HistoryEntry` |
| `deliberation/modulator.py` | Postura modula intensidad del prompt por sección |
| `deliberation/history.py` | Ring buffer de deliberaciones pasadas |

### Flujo V3
```
1. PlanHook detecta transición INVESTIGATE → PLAN (primer update_plan)
2. → before_iteration: PlanHook._run_deliberation(context)
3.   → Construye DeliberationContext con investigation findings
4.   → DeliberationService.deliberate(context)
5.     → 1 LLM call con ordering forzado: [CRITICO] → [EXPLORADOR] → [PRAGMATICO] → [SINTESIS]
6.     → _parse_response(): regex split por markers → Perspective tuples + synthesis
7.   → Logs completo a telemetría (3 perspectivas + síntesis + postura + timing)
8. → render_for_injection() → inyecta como system message
```

### Ordering para divergencia
- **Crítico primero**: identifica riesgos sin solución previa que defender
- **Explorador segundo**: propone alternativa sin camino "obvio" establecido
- **Pragmático tercero**: camino directo, incorporando riesgos del crítico
- **Síntesis**: merge activo de las 3 perspectivas

### Formato de inyección
```
[Deliberación pre-análisis]

Riesgos identificados: {crítico}
Alternativa considerada: {explorador}
Enfoque directo: {pragmático}

Síntesis: {merge de las 3 perspectivas}
```

### Modulación por postura
- `cautela > 0.7` → crítico exhaustivo
- `cautela < 0.4` → crítico breve
- `exploracion > 0.6` → explorador radical
- `profundidad > 0.7` → perspectivas detalladas (3-5 oraciones)
- default → perspectivas concisas (1-3 oraciones)

### Cuándo delibera
- Transición INVESTIGATE → PLAN en full_plan mode
- Se re-activa en cycle restart (CONFIRM fail → nuevo ciclo → nueva investigación → PLAN)
- En retry: `previous_failure` enriquece el prompt con qué falló antes

### Telemetría
Cada deliberación genera evento completo en JSONL:
```json
{"type": "deliberation.result", "data": {"trigger": "investigate_to_plan", "cycle": 1,
  "model": "glm-5.1", "duration_ms": 6234, "posture": {"cautela": 0.65},
  "perspectives": {"critico": "...", "explorador": "...", "pragmatico": "..."},
  "synthesis": "..."}}
```

---

## 5. Hook Factory

`agent/hook_factory.py` wirea todo al construir el agente:

```python
build_hooks_from_config(config) → [PostureHook, PlanHook]
# DeliberationService se inyecta DENTRO de PlanHook (no es hook separado)
```

Orden:
1. **PostureHook** primero (vector inicializado antes que PlanHook lo consulte)
2. **PlanHook** segundo (tiene deliberation service + posture_snapshot_fn internos)

El `CompositeHook` ejecuta todos en secuencia para cada lifecycle event.

### Comunicación inter-hooks
`AgentHookContext.external_stimulus_events: list[str]` permite que PlanHook emita eventos posturales (CONFIRM_PASS, CONFIRM_FAIL, CYCLE_RESTART, PLAN_COMPLEX) que PostureHook consume en su siguiente iteración.

---

## 6. Sistema de Plan (3 tiers)

### Archivos clave
| Archivo | Responsabilidad |
|---|---|
| `plan/types.py` | `ExecutionTier`, `Phase`, `PlanItem`, `PlanState` |
| `plan/hook.py` | `PlanHook` — inyecta instrucciones, infiere transiciones, emite estímulos |
| `plan/store.py` | `PlanStore` — persistencia (plan.json + events.jsonl por sesión) |
| `agent/tools/plan.py` | Tools: `set_execution_mode`, `update_plan` (auto-discoverable) |

### Los 3 tiers de ejecución
| Tier | Cuándo | Qué hace el hook |
|---|---|---|
| `direct` | Preguntas simples, edits triviales | Nada — sin overhead |
| `execute_verify` | Bug fix localizado, cambio único | Reminder: "Run tests to verify" tras editar |
| `full_plan` | Multi-step, incertidumbre, refactors | Ciclo fijo + bitácora + estímulos posturales |

### Ciclo fijo (solo `full_plan`)
```
INVESTIGATE → PLAN → EXECUTE → CONFIRM ─┐
     ↑                                   │ (fail)
     └───────────────────────────────────┘
```

- **INVESTIGATE**: Leer, entender contexto. NO editar.
- **PLAN**: Definir pasos via `update_plan(add, ...)`.
- **EXECUTE**: Implementar. Editar archivos.
- **CONFIRM**: Correr tests. Si pasan → done. Si fallan → nuevo ciclo.

### Transiciones de fase (inferidas automáticamente)
| Transición | Trigger |
|---|---|
| INVESTIGATE → PLAN | `update_plan("add", ...)` es llamado |
| PLAN → EXECUTE | Se detecta uso de tool `edit_file` o `write_file` |
| EXECUTE → CONFIRM | Se detecta `exec` después de edits |
| CONFIRM → INVESTIGATE | Error en context (tests fallaron) → reinicia ciclo |

### Estímulos emitidos (bridge postura ↔ plan)
| Evento | Cuándo | Efecto postural |
|---|---|---|
| `confirm_pass` | Tests pasan en CONFIRM | cautela −0.10 (oracle real) |
| `confirm_fail` | Tests fallan en CONFIRM | cautela +0.15, profundidad +0.10 |
| `cycle_restart` | Confirm fail → nuevo ciclo | disciplina +0.05, exploración +0.10 |
| `plan_complex` | Plan cruza >3 items | profundidad +0.10, cautela +0.05 (one-shot) |

### Persistencia (bitácora)
Cada sesión con `full_plan` genera:
- `plans/{session_key}/plan.json` — estado actual (tier, phase, items, cycle_count)
- `plans/{session_key}/events.jsonl` — log de eventos (tier_set, plan_item_added, phase_transition, confirm_result)

### Filosofía de diseño
El agente **declara** su tier via tool call (`set_execution_mode`). El hook **enforce** el ciclo si elige `full_plan`. Esto evita que el agente "piense" que resolvió sin verificar — el bug central detectado en benchmarks (6938: agente declara victoria sin correr tests).

---

## 7. Herencia de Nanobot — Lo que NO tocamos

| Subsistema | Ubicación | Notas |
|---|---|---|
| Agent loop orchestration | `agent/loop.py` | Coordina channels → runner |
| Runner (iteration loop) | `agent/runner.py` | Ejecuta iteraciones, tools, hooks |
| Session/memory | `session/`, `agent/memory.py` | Dream consolidation, compaction |
| Tools | `agent/tools/` | 14 tools registradas |
| Providers | `providers/` | LLM backends |
| Channels | `channels/` | Telegram, Discord, WebSocket, etc. |
| Bus | `bus/` | Async message passing |
| Config | `config/schema.py` | Pydantic config with posture/delib sections |

---

## 8. Telemetría

`telemetry/logger.py` — escribe eventos JSONL por sesión en `~/.cache/durin/telemetry/`.

Eventos registrados:
- `posture.initial` — vector al arranque
- `posture.change` — cada cambio de vector (axes, deltas, events)
- `deliberation.start` — inicio de deliberación
- `deliberation.result` — resultado (proposals, winner, timing)
- `plan.tier_set` — tier declarado por el agente
- `plan.phase_transition` — cambio de fase en ciclo
- `plan.confirm_result` — resultado de confirmación (pass/fail)

---

## 9. Lo que NO existe aún

| Componente | Estado | Doc de referencia |
|---|---|---|
| **Metacognición/reflexión** | No existe | Investigado (ReMA, Reflexion), dudoso sin oracle |
| **Grafo de memoria** | Diseñado en docs, no implementado | `docs/03_durin_memoria.md` |
| **Proyección de contexto** | No implementada | Doc diseño §4.1 |
| **Consolidación (sueño)** | No implementada | Fase 3 del roadmap |
| **Ajuste de medias posturales** | No implementado | Fase 3 del roadmap |
| **Deliberación evolutiva** | Diseñado, no implementado | Plan: mutación/crossover entre rondas |
| **Deliberación como servicio del plan** | No integrado | Deliberation debería correr en fase PLAN |

---

## 10. Scripts de evaluación

| Script | Propósito |
|---|---|
| `scripts/swebench_eval.py` | Benchmark Durin en SWE-bench Lite |
| `scripts/swebench_nanobot_eval.py` | Benchmark nanobot base (sin postura/delib) |
| `scripts/simulate_posture_session.py` | Simulación manual de sesión postural |

Resultados guardados en `benchmarks/swebench_5/`.

---

## 11. Tests

```bash
pytest tests/deliberation/ -v   # Engine, synthesis, hook (52 tests)
pytest tests/posture/ -v         # Vector, homeostasis, stimulus
pytest tests/plan/ -v            # Plan hook, types, store, tools (33 tests)
pytest tests/ -q                 # Full suite
```
