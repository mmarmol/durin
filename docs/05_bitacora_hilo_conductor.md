# Bitácora de implementación — Hilo Conductor + Deliberación

> Estado actual de la implementación del vector de postura y el sistema de deliberación multi-generador.

---

## Estado general

**Postura**: completa (7 fases originales + expansiones + gap closure).  
**Deliberación**: evolutiva (Mind Evolution: mutación + crossover + convergencia por plateau).  
**Tests**: 3479 pasando, 0 fallas.  
**Fecha última actualización**: 2026-05-17.

---

## 1. Vector de postura (hilo conductor)

### Implementado

| Componente | Archivo | Estado |
|---|---|---|
| Vector (5 ejes, AxisState) | `durin/posture/vector.py` | ✅ |
| Homeostasis (retorno a media + clamp) | `durin/posture/homeostasis.py` | ✅ |
| Tabla de estímulos (12 reglas) | `durin/posture/stimulus.py` | ✅ |
| Frase de postura (tabla determinista) | `durin/posture/phrase.py` | ✅ |
| Persistencia (session metadata) | `durin/posture/persistence.py` | ✅ |
| PostureHook (ciclo de vida) | `durin/posture/hook.py` | ✅ |
| Config schema (PostureConfig) | `durin/config/schema.py` | ✅ |
| Detección de eventos avanzados | `durin/posture/hook.py` | ✅ |
| Goal-sensitive initialization | `durin/posture/goal_bias.py` | ✅ |

### Estímulos activos (detectados automáticamente)

| Evento | Condición | Efecto |
|---|---|---|
| `STEP_FAILED` | Error o tool failure | cautela +0.10, profundidad +0.05 |
| `STEP_SUCCEEDED` | Tool calls exitosos | cautela −0.03 |
| `CONSECUTIVE_SUCCESSES_3` | 3+ éxitos seguidos | exploración +0.05 |
| `CONSECUTIVE_FAILURES_3` | 3+ fallos seguidos | cautela +0.15, conformidad −0.10 |
| `USER_CORRECTED` | Usuario inyecta mensaje mid-turn | conformidad +0.05 |
| `GOAL_AMBIGUOUS` | Iteración vacía (sin tools, sin output) | profundidad +0.10 |
| `CRITICAL_ACTION` | Herramienta peligrosa ejecutada | cautela +0.10 |
| `EXPLICIT_PROTOCOL` | System prompt con marcadores de protocolo | disciplina +0.10 |

### Goal-sensitive initialization (§3.4)

Al inicio (iteration 0), se escanea el goal con keywords deterministas:

| Keywords detectadas | Eje | Delta |
|---|---|---|
| producción, deploy, delete, force push, migration... | Cautela | +0.10 |
| explorá, research, brainstorm, alternativas... | Exploración | +0.10 |
| protocolo, checklist, compliance, paso a paso... | Disciplina | +0.10 |

Elimina el cold-start problem: el vector reacciona a la naturaleza del goal antes del primer paso.

### Persistencia y decaimiento

| Componente | Archivo | Estado |
|---|---|---|
| Serialización del vector | `durin/posture/persistence.py` | ✅ |
| Decaimiento temporal (tau=4h) | `durin/posture/persistence.py:apply_time_decay` | ✅ |
| Restore con decay al inicio de turno | `durin/agent/loop.py:_restore_posture_from_session` | ✅ |
| Save al final de turno | `durin/agent/loop.py:_save_posture_state` | ✅ |

Fórmula: `valor += (1 - exp(-elapsed/tau)) * (media - valor)` con tau=4 horas.  
Tras 4h inactivo el vector habrá decaído ~63% hacia la media. Tras 12h, ~95%.

### Pendiente / Futuro

- `USER_APPROVED_RISKY`: definido pero sin detector (requiere detección semántica del contenido del usuario)
- `EXPLORATORY_TASK`: definido pero sin detector (requiere clasificación del goal — parcialmente cubierto por goal_bias)
- ~~`EXPLICIT_PROTOCOL`: definido pero sin detector~~ → ✅ Implementado (marcadores en system prompt)
- Decaimiento reforzado por goal distinto (§6 del diseño — actualmente solo temporal)
- Ajuste de medias por consolidación histórica (Fase 3 del diseño, diferido)

---

## 2. Deliberación evolutiva (Mind Evolution)

### Implementado

| Componente | Archivo | Estado |
|---|---|---|
| Tipos (Proposal, Verdict, RoundResult, ConvergenceReason) | `durin/deliberation/types.py` | ✅ |
| Scoring (pesos por cautela, umbral por profundidad) | `durin/deliberation/scoring.py` | ✅ |
| Generador (seeds round 1, evolución round 2+) | `durin/deliberation/generator.py` | ✅ |
| Evaluador (LLM score 0-10) | `durin/deliberation/evaluator.py` | ✅ |
| Director (decisión pura, multi-ronda) | `durin/deliberation/director.py` | ✅ |
| Engine (orquestador evolutivo + crossover) | `durin/deliberation/engine.py` | ✅ |
| **Modulador estructural** | `durin/deliberation/modulator.py` | ✅ |
| DeliberationHook (inyección pre-message) | `durin/deliberation/hook.py` | ✅ |
| Config (DeliberationConfig) | `durin/config/schema.py` | ✅ |
| Constantes compartidas | `durin/deliberation/constants.py` | ✅ |

### Arquitectura evolutiva (Mind Evolution)

| Round | Comportamiento | Inspiración |
|---|---|---|
| Round 1 | Divergente: seeds cortos desde cada perspectiva (pragmático, explorador, crítico) | Pensamiento divergente |
| Round 2+ | Evolutiva: generadores reciben ganador previo + su propia propuesta + scores → refinan | Mutación |
| Crossover | Si gap entre top 2 < 0.10, genera propuesta HIBRIDO combinando ambas | Crossover genético |
| Convergencia | Por threshold (score suficiente), plateau (mejora < 0.05 entre rondas), o max_rounds | Fitness plateau |

### Inyección al LLM principal

La deliberación se inyecta como **mensaje system antes del último user message** (no en system prompt):
```
[Deliberación pre-análisis]

Enfoque recomendado: [propuesta ganadora evolucionada]
Riesgos identificados: [perspectiva del crítico]
Alternativa considerada: [mejor runner-up]
Confianza: alta/media/baja
```

Esto **enriquece** al LLM principal para que construya un plan mejor, sin dictarle qué hacer.

### Modulación estructural (doc §4.2)

La postura no solo pesa el scoring — **cambia la arquitectura de la deliberación** en cada invocación:

| Eje | Condición | Efecto estructural |
|---|---|---|
| Profundidad | < 0.3 | Crítico se omite (deliberación rápida) |
| Profundidad | >= 0.6 | +1 ronda extra de generación |
| Profundidad | >= 0.8 | +2 rondas extra (max 5 total) |
| Exploración | valor actual | Temperatura del explorador: `base + 0.3*(expl - 0.5)`, clamped [0.5, 1.2] |
| Conformidad | < 0.3 | Explorador recibe permiso de cuestionar la tarea |
| Cautela | > 0.7 | Pragmático duplicado (variante con +0.15 temp) → 4 propuestas |
| Cautela | > 0.85 | Crítico también duplicado → 5 propuestas |
| Cautela | valor actual | Drift threshold dinámico: `0.15 - 0.05*(cautela-0.5)` rango [0.10, 0.20] |
| Disciplina | >= 0.6 | Todos los generadores reciben sufijo de adherencia a protocolo |
| Disciplina | < 0.3 | Pragmático +0.1 temperatura (más flexible) |

Resultado: un agente con cautela 0.9 genera 5 propuestas y re-delibera con drift >= 0.10 vs un agente con cautela 0.3 que genera 3, tolera drift hasta 0.18. Un agente con profundidad 0.9 delibera hasta 5 rondas; con profundidad baja omite el crítico y usa máximo 3. Disciplina alta fuerza adherencia al procedimiento; baja permite improvisación.

### Synthesis (enriquecida)

| Componente | Archivo | Estado |
|---|---|---|
| SynthesisResult (structured) | `durin/deliberation/types.py` | ✅ |
| `synthesize()` → SynthesisResult | `durin/deliberation/synthesis.py` | ✅ |
| `render_synthesis()` → texto | `durin/deliberation/synthesis.py` | ✅ |
| Razonamiento postura-driven | automático | ✅ |
| Alternativas (top 2 runners-up) | automático | ✅ |
| Confianza (alta/media/baja) | automático | ✅ |

### Triggering inteligente

| Trigger | Cuándo | Comportamiento |
|---|---|---|
| Planning moment | Iteración 0, sin goal activo | Delibera y inyecta en system prompt |
| Critical action | `before_execute_tools` con tool peligroso | Re-delibera y actualiza dirección |
| Posture drift | Drift ≥0.15 en cualquier eje desde última deliberación | Re-delibera |
| Goal active skip | "Goal (active):" en system prompt | Salta deliberación (no interfiere con plan) |

### Verdict History

| Componente | Archivo | Estado |
|---|---|---|
| VerdictHistory (ring buffer, max 20) | `durin/deliberation/history.py` | ✅ |
| VerdictEntry (frozen dataclass) | `durin/deliberation/types.py` | ✅ |
| `dominant_role()` (patrón en últimas 5) | `durin/deliberation/history.py` | ✅ |
| Serialize/deserialize | `durin/deliberation/history.py` | ✅ |
| Hook acumula automáticamente | `durin/deliberation/hook.py` | ✅ |
| Persistencia a session metadata | `durin/deliberation/persistence.py` | ✅ |
| Restore al inicio de turno | `durin/agent/loop.py:_restore_verdict_history` | ✅ |
| Save al final de turno | `durin/agent/loop.py:_save_verdict_history` | ✅ |

### Context Projection

Los generadores reciben contexto enriquecido:

| Campo | Fuente | Límite |
|---|---|---|
| `goal_summary` | Último mensaje del usuario | 500 chars |
| `active_objective` | "Goal (active):" en system prompt | 300 chars |
| `conversation_summary` | Últimos 5 mensajes assistant | 100 chars c/u |
| `previous_verdict_brief` | VerdictHistory.last | 80 chars |
| `recent_context` | Tool names a ejecutar | — |

---

## 3. Infraestructura

### UI Visualization

| Canal | Componente | Estado |
|---|---|---|
| CLI (Rich) | `durin/cli/agent_ui_render.py` | ✅ |
| WebUI (React) | `webui/src/components/thread/PosturePanel.tsx` | ✅ |
| WebUI (React) | `webui/src/components/thread/DeliberationPanel.tsx` | ✅ |
| Hook → UI pipe | `emit_ui` callback en AgentHookContext | ✅ |

### Telemetry

| Evento | Logger method | Datos |
|---|---|---|
| `posture.initial` | `log_posture_initial` | snapshot 5 ejes |
| `posture.change` | `log_posture_change` | ejes, deltas, eventos |
| `deliberation.start` | `log_deliberation_start` | trigger, goal, posture |
| `deliberation.result` | `log_deliberation_result` | winner, scores, rounds, duration |
| `deliberation.skipped` | `log_deliberation_skipped` | reason |
| `deliberation.error` | `log_deliberation_error` | error msg |

Archivo: JSONL append-only en `~/.cache/durin/telemetry/`.

### Wiring automático

| Componente | Archivo | Estado |
|---|---|---|
| Hook factory (config → hooks) | `durin/agent/hook_factory.py` | ✅ |
| Providers: ollama, local (llama-cpp) | `durin/agent/hook_factory.py` | ✅ |
| AgentLoop.from_config auto-wiring | `durin/agent/loop.py` | ✅ |

---

## 4. Mapping al documento de diseño

| Momento (doc §4) | Estado | Notas |
|---|---|---|
| 1. Proyección de contexto | ⚡ Parcial | Flat text, no grafo. Context projection enriquece con resumen + objetivo + verdict previo |
| 2. Generación (SLMs) | ✅ Completo | 3 generadores paralelos con posture phrase + contexto enriquecido |
| 3. Evaluación (scores) | ✅ Completo | 2 evaluadores (avance, reversibilidad), pesos por cautela |
| 4. Director (umbral) | ✅ Completo | Umbral por profundidad, multi-ronda (max_rounds + extra por profundidad, cap 5), under_doubt |
| 5. Síntesis (LLM grande) | ⚡ Parcial | Dirección inyectada como texto estructurado, no hay paso explícito de síntesis del LLM |
| 6. Ajuste post-paso | ✅ Completo | PostureHook.after_iteration detecta eventos y actualiza vector |

| Aspecto transversal (doc §5-6) | Estado | Notas |
|---|---|---|
| Persistencia entre sesiones | ✅ Completo | Save/restore con decay temporal (tau=4h) |
| Inyección jerárquica | ⚡ Parcial | Plan-level skip (goal active), no subplan/paso differentiation |
| Observabilidad | ✅ Completo | Telemetría JSONL + UI visualization + verdict history |

---

## 5. Lo que falta (priorizado)

### Media prioridad

1. **Momento 1 completo (grafo)** — la proyección de contexto actual es flat text. El diseño pide selección de nodos del grafo según postura. Requiere el sistema de memoria de grafo (doc 03).

2. **Detectores semánticos restantes** — `USER_APPROVED_RISKY` (requiere clasificación del contenido del usuario), `EXPLORATORY_TASK` (parcialmente cubierto por goal_bias, falta detector dinámico mid-turn).

3. **Momento 5 explícito** — actualmente el LLM principal recibe la dirección y actúa. El diseño sugiere un paso de síntesis explícito donde el LLM combina propuesta + contexto + críticas en un plan de acción detallado.

4. **Postura sesga selección de herramientas** — la postura afecta deliberación pero no el bias del agente hacia tools seguros vs arriesgados. Sería un "momento 2.5" nuevo.

### Baja prioridad

5. **Inyección jerárquica** (doc §5) — plan/subplan/paso. Actualmente se inyecta siempre o se skipea por goal activo. Optimización para planes largos.

6. **Decaimiento reforzado por goal distinto** (doc §6) — si el goal nuevo no tiene relación con el anterior, decay debería ser más fuerte. Hoy es puramente temporal.

7. **Calibración empírica de deltas** — los valores actuales de la tabla de estímulos son educated guesses. Se necesita data real para ajustar.

8. **Ajuste de medias por consolidación** (doc §6, Fase 3) — las medias son constantes hoy. El "dream" de memoria podría ajustarlas basándose en historia de outcomes.

---

## 6. Tests por módulo

| Directorio | Tests | Foco |
|---|---|---|
| `tests/posture/` | ~95 | Vector, homeostasis, stimulus, phrase, hook, emit_ui, advanced triggers, persistence, goal_bias |
| `tests/deliberation/` | ~200 | Types, scoring, generator, evaluator, director, engine, hook, synthesis, history, context projection, triggering, persistence, emit_ui, integration, modulator |
| `tests/agent/test_hook_factory.py` | 5 | Factory wiring desde config |
| `tests/telemetry/` | 14 | Logger, events, path sanitization |
| `tests/cli/test_agent_ui_render.py` | 10 | Rich panel rendering |

Total nuevo sobre el tema: **~300 tests dedicados al hilo conductor + deliberación**.

---

## 7. Decisiones de diseño tomadas

1. **Pure functions para scoring/director** — cero I/O, deterministas, auditables.
2. **Frozen dataclasses** — inmutabilidad en todo el pipeline de deliberación.
3. **SLMs locales baratos** — no hay presión de costo, se delibera liberalmente.
4. **Postura lee, nunca escribe desde deliberación** — el hook de postura actualiza el vector independientemente.
5. **Degradación graciosa** — si Ollama no está, se logea warning y el agente funciona sin deliberación.
6. **Drift como trigger orgánico** — no necesita timer ni heurística manual para re-deliberar mid-plan.
7. **Goal active = skip deliberation** — cuando hay plan activo, no se interfiere (el plan YA fue deliberado).
8. **Ring buffer (20 max)** — memoria de verdicts acotada, no crece indefinidamente.
9. **Modulación estructural, no solo textual** — la postura cambia la arquitectura (qué generadores, cuántas rondas, qué thresholds), no solo las frases inyectadas.
10. **Goal bias con keywords simples** — no LLM para cold-start. Heurísticas de keywords son suficientes y predecibles.
11. **Drift threshold dinámico** — cautela modula la sensibilidad al cambio, no es un número fijo.
