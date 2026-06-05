# Secuencia: Consulta de memoria (lectura)

> **Estado**: working — diagrama vivo. Lo refinamos **etapa por etapa**.
> Parte de [memory_model_redesign.md](memory_model_redesign.md).

Actores: **Usuario** · **hot_layer** (pre-carga, ver
[memory_context_preload.md](memory_context_preload.md)) · **Agente** (loop) ·
`memory_search` (FTS+vector) · **Dream-refiner** (async, no síncrono aquí).

## Diagrama

```mermaid
sequenceDiagram
  actor U as Usuario
  participant HL as hot_layer (pre-carga)
  participant A as Agente (loop)
  participant S as memory_search<br/>(FTS+vector)
  participant DR as Dreams corto/largo (async)

  Note over HL,A: [E0] bootstrap: HL ensambla contexto → prompt del agente
  U->>A: [E1] pregunta
  A->>S: [E2] memory_search(query) [2-3 fraseos]
  S-->>A: [E3] hits sectorizados<br/>CANONICAL(verdad/entidad) · REFERENCE(doc) · SESSION(experiencia)
  Note over A: [E4] sintetiza por marcador + cita fuentes
  A-->>U: [E5] respuesta
  Note over DR: [E6] dream NO es síncrono; aporta estado YA curado.<br/>(opcional: gap detectado → encola refine async, no bloquea)
```

## Etapas (a refinar al fino)

### E0 — Pre-carga (bootstrap)
- **Qué**: `hot_layer` ya inyectó SELF + GENERAL + USER + SESSION al prompt.
- **Detalle**: [memory_context_preload.md](memory_context_preload.md).

### E1 — Usuario pregunta
- **Qué**: pregunta en lenguaje natural.
- **Pendiente**: —

### E2 — Búsqueda
- **Quién/qué**: `memory_search(query)`; para multi-parte, 2-3 fraseos.
- **Pendiente**: ¿qué scopes recorre — solo grafo HEAD, o también experiencia reciente no-consolidada y referencias? ¿el agente decide nivel (warm/cold)?

### E3 — Pipeline devuelve marcadores
- **Quién/qué**: FTS+vector → hits sectorizados con marcadores:
  `CANONICAL` (verdad/entidad) · `REFERENCE` (documento curado) ·
  `SESSION` (experiencia: sesión/resumen), con qualifier de completitud.
  **Nota**: el marcador `FRAGMENT` (observación cruda episodic/stable)
  **desaparece** — esas clases se disuelven (§2.6); la experiencia reciente
  no-consolidada vive en `sessions/` → marcador SESSION.
- **Pendiente**: agregar/confirmar el marcador **REFERENCE** (hoy las refs caen en INGESTED); cap por-fuente.

### E4 — Síntesis
- **Quién/qué**: el agente sintetiza priorizando por marcador (CANONICAL manda como verdad; SESSION aporta lo reciente aún no consolidado, con timestamps; REFERENCE se cita como fuente), cita URIs.
- **Pendiente**: regla explícita de precedencia al leer (verdad vs reciente vs documento).

### E5 — Respuesta
- **Qué**: responde citando fuentes; nombra lo que falta.
- **Pendiente**: —

### E6 — Dreams no-síncronos (opcional reactivo)
- **Qué**: ni el corto ni el largo corren en el camino síncrono de consulta. Su aporte es el **estado ya curado** que el search lee. Opción: si el agente detecta incoherencia/gap en los hits, **encola** un refine async (no bloquea la respuesta).
- **Pendiente**: ¿vale la pena el refine reactivo, o solo scheduled/por-escritura (§2.7)?
