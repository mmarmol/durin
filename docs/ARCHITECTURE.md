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
- Sistema de deliberación (generadores de perspectivas)
- Telemetría postural
- Hook factory que wirea postura + deliberación automáticamente

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
│     └── DeliberationHook: iter 0 → run deliberation         │
│  4. LLM request → response                                  │
│  5. Parse response (tool_calls, content, reasoning)          │
│  6. If tool_calls:                                           │
│     a. hook.before_execute_tools(context)                    │
│     b. Execute tools (sequential or concurrent)              │
│     c. Append tool results to messages                       │
│  7. hook.after_iteration(context)                            │
│     └── PostureHook: detect events → update vector           │
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
| `STEP_SUCCEEDED` | cautela −0.03 | Tool call sin error |
| `CONSECUTIVE_SUCCESSES_3` | exploración +0.05 | 3 éxitos seguidos |
| `CONSECUTIVE_FAILURES_3` | cautela +0.15, conformidad −0.10 | 3 fallos seguidos |
| `GOAL_AMBIGUOUS` | profundidad +0.10 | Iter sin tools, sin content, sin error (raro) |
| `USER_CORRECTED` | conformidad +0.05 | Mensajes inyectados en la sesión |
| `USER_APPROVED_RISKY` | cautela −0.05 | (No implementado como trigger activo) |
| `CRITICAL_ACTION` | cautela +0.10 | Tool en CRITICAL_TOOLS set |
| `EXPLORATORY_TASK` | exploración +0.10 | (Solo via goal_bias keywords) |
| `EXPLICIT_PROTOCOL` | disciplina +0.10 | Markers como "## checklist" en system prompt |

### Problemas conocidos (benchmark mayo 2026)
- **profundidad** y **disciplina** nunca se activan — sus estímulos son non-events en práctica
- **exploración** se sobre-estimula por `CONSECUTIVE_SUCCESSES_3` (tools raramente fallan)
- **carry-posture** tiene bug: pone `valor_actual` como nueva `media`, causando drift geométrico

---

## 4. Sistema de Deliberación (V2 actual)

### Archivos clave
| Archivo | Responsabilidad |
|---|---|
| `deliberation/hook.py` | `DeliberationHook` — trigger, run, inject |
| `deliberation/engine.py` | `DeliberationEngine` — genera perspectivas |
| `deliberation/generator.py` | LLM calls para cada generador (pragmático, explorador, crítico) |
| `deliberation/synthesis.py` | Formatea las perspectivas para inyección |
| `deliberation/types.py` | Dataclasses: `Proposal`, `ScoredProposal`, `Verdict`, etc. |
| `deliberation/modulator.py` | Postura modula cantidad/params de generadores |
| `deliberation/constants.py` | `CRITICAL_TOOLS` set |

### Flujo V2
```
1. DeliberationHook.before_iteration (solo iter 0)
2. → engine.deliberate(context)
3.   → Genera 3 perspectivas en paralelo (pragmático, explorador, crítico)
4.   → Sin evaluadores, sin scoring real (all 0.5)
5.   → Pragmático gana por convención
6. → synthesis.render_synthesis() → texto formateado
7. → Inyecta como system message antes del último user message
```

### Formato de inyección
```
[Deliberación pre-análisis]
Perspectiva directa: {pragmático}
Perspectiva alternativa: {explorador}
Riesgos a considerar: {crítico}
```

### Cuándo NO delibera
- `iteration != 0` (solo al inicio)
- Si hay goal activo (`long_task` en curso)
- Si el provider falla

---

## 5. Hook Factory

`agent/hook_factory.py` wirea todo al construir el agente:

```python
build_hooks_from_config(config) → [PostureHook, DeliberationHook]
```

Orden importa: PostureHook primero (para que el vector esté inicializado cuando DeliberationHook lo consulte).

El `CompositeHook` ejecuta ambos en secuencia para cada lifecycle event.

---

## 6. Herencia de Nanobot — Lo que NO tocamos

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

## 7. Telemetría

`telemetry/logger.py` — escribe eventos JSONL por sesión en `~/.cache/durin/telemetry/`.

Eventos registrados:
- `posture.initial` — vector al arranque
- `posture.change` — cada cambio de vector (axes, deltas, events)
- `deliberation.start` — inicio de deliberación
- `deliberation.result` — resultado (proposals, winner, timing)

---

## 8. Lo que NO existe aún

| Componente | Estado | Doc de referencia |
|---|---|---|
| **Plan/task tracking** | No existe. Solo `long_task` (goal string) | Investigado, no implementado |
| **Metacognición/reflexión** | No existe | Investigado (ReMA, Reflexion), dudoso sin oracle |
| **Grafo de memoria** | Diseñado en docs, no implementado | `docs/03_durin_memoria.md` |
| **Proyección de contexto** | No implementada | Doc diseño §4.1 |
| **Consolidación (sueño)** | No implementada | Fase 3 del roadmap |
| **Ajuste de medias** | No implementado | Fase 3 del roadmap |

---

## 9. Scripts de evaluación

| Script | Propósito |
|---|---|
| `scripts/swebench_eval.py` | Benchmark Durin en SWE-bench Lite |
| `scripts/swebench_nanobot_eval.py` | Benchmark nanobot base (sin postura/delib) |
| `scripts/simulate_posture_session.py` | Simulación manual de sesión postural |

Resultados guardados en `benchmarks/swebench_5/`.

---

## 10. Tests

```bash
pytest tests/deliberation/ -v   # Engine, synthesis, hook (52 tests)
pytest tests/posture/ -v         # Vector, homeostasis, stimulus
pytest tests/ -q                 # Full suite
```
