# Absorb judge prompt — v1

> LLM-judge para decidir si dos entity pages representan la MISMA
> identidad real. Usado por el refine pass
> `durin/memory/refine_dream.py::run_refine` cuando
> `memory.dream.auto_absorb.enabled = true`.
>
> Diseñado adversarial: comparten al menos un alias (alias overlap)
> pero esto es NECESARIO y NO suficiente. El judge debe defaultear
> a "different" cuando la evidencia de contenido es débil. Incluye
> timestamps en cada página para mitigar self-consistency bias
> cuando `judge_model == dream_model` (glm peer review C2,
> 2026-05-24).
>
> Output esperado: bloque markdown-marker con `===VERDICT===` (one of
> `same` / `different` / `unclear`), `===CONFIDENCE===` (entero 0-100),
> `===REASONING===` (1-3 oraciones), terminado por `===END===`. Mismo
> patrón de envelope que `consolidator.md`.
>
> Variables a sustituir:
> - `{shared_aliases}` — lista de alias que comparten ambos refs
> - `{ref_a}`, `{ref_b}` — entity refs (e.g. `person:marcelo`)
> - `{page_a_block}`, `{page_b_block}` — cada uno con header de
>   metadatos temporales + body del page

---

## Template

```
Eres durin, evaluando si DOS páginas de entidad representan la MISMA identidad real.

IMPORTANTE: ambas páginas comparten al menos un alias ("{shared_aliases}"). Esto es
NECESARIO pero NO suficiente para fusionar:
- Dos personas pueden llamarse "Marcelo".
- Dos proyectos pueden compartir un acrónimo.
- Un alias casual ("admin", "user") puede aparecer en entidades no relacionadas.

Default a "different" cuando la evidencia de contenido es débil. La penalización
por un falso positivo (merge incorrecto) es alta — la información se conserva
en archive/ pero el slug se mueve y la búsqueda semántica cambia.

## Página A: {ref_a}

{page_a_block}

## Página B: {ref_b}

{page_b_block}

## Tu tarea

Decide si A y B describen la MISMA entidad real, basándote en CONTENIDO (no solo
alias). Señales fuertes (cualquiera basta para "same" con alta confianza):
- Identifiers que coinciden literalmente (email, github, slack, jira, phone).
- Detalles biográficos / factuales consistentes (rol, organización, fechas).
- Una página menciona explícitamente a la otra como sí misma.

Señales de "different":
- Contradicciones de hecho (rol distinto, organización distinta, fechas mutuamente
  excluyentes).
- Contextos completamente desconectados (una persona técnica vs un placeholder
  administrativo, ambos llamados "admin").
- Los timestamps sugieren entidades distintas en períodos no superpuestos
  (e.g. una se observó por última vez hace 2 años, la otra es nueva).

Señales débiles (no suficientes solas):
- Solo aliases que coinciden — puede ser homonimia.
- Tipo de entidad coincidente.

Output exacto en este formato (sin texto antes ni después):

===VERDICT===
same | different | unclear
===CONFIDENCE===
<entero 0-100 — qué tan seguro estás de tu verdict>
===REASONING===
<1-3 oraciones cortas explicando la decisión. Citá señales concretas vistas.>
===END===
```
