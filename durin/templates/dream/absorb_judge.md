# Absorb judge prompt — v2

> LLM-judge para decidir qué hacer con DOS entity pages que colisionan
> (comparten un alias o están muy cerca en embeddings). Usado por el refine
> pass `durin/memory/refine_dream.py::run_refine`.
>
> Diseñado adversarial: el alias compartido es NECESARIO y NO suficiente. El
> judge defaultea a "different" cuando la evidencia de contenido es débil.
> Incluye timestamps en cada página para mitigar self-consistency bias cuando
> `judge_model == dream_model`.
>
> v2: además del veredicto, el judge propone una **resolución** — qué página
> sobrevive a un merge, claves más claras, a quién pertenece un alias en
> disputa, y la relación tipada entre páginas relacionadas. Veredicto nuevo
> `related`: identidades distintas donde una es parte, versión o
> especialización de la otra.
>
> Output esperado: `===VERDICT===` (one of `same` / `different` / `related` /
> `unclear`), `===CONFIDENCE===` (entero 0-100), `===REASONING===` (1-3
> oraciones), `===RESOLUTION===` (objeto JSON, `{}` si no hay nada que
> proponer), terminado por `===END===`.
>
> Variables a sustituir:
> - `{shared_aliases}` — lista de alias que comparten ambos refs
> - `{ref_a}`, `{ref_b}` — entity refs (e.g. `person:marcelo`)
> - `{page_a_block}`, `{page_b_block}` — cada uno con header de
>   metadatos temporales + body del page

---

## Template

```
Eres durin, evaluando qué hacer con DOS páginas de entidad que colisionan.

IMPORTANTE: ambas páginas comparten al menos un alias ("{shared_aliases}") o están
muy cerca semánticamente. Esto es NECESARIO pero NO suficiente para fusionar:
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

1) Decide la relación entre A y B, basándote en CONTENIDO (no solo alias):

- same — describen la MISMA entidad real. Señales fuertes (cualquiera basta):
  identifiers que coinciden literalmente (email, github, slack, jira, phone);
  detalles biográficos / factuales consistentes; una página menciona a la otra
  como sí misma.
- related — son entidades DISTINTAS pero una es parte, versión, edición,
  instancia o especialización de la otra (una edición y el juego al que
  pertenece; una regla específica y la general que la contiene). No es "same":
  fusionarlas perdería la distinción.
- different — entidades distintas sin esa relación estructural. Señales:
  contradicciones de hecho; contextos desconectados; timestamps de períodos
  no superpuestos; solo homonimia.
- unclear — la evidencia no alcanza para decidir.

2) Propone la resolución (todo opcional; solo lo que el contenido justifique):

- survivor (solo si same): el ref cuya clave es la más clara y canónica.
- renames: una clave (slug) o nombre más claro cuando la actual es críptica o
  ambigua ("5e" → "dnd-5e"). Slug en minúsculas, dígitos y guiones; nunca
  cambies el tipo. Si same, solo puede renombrarse el survivor.
- alias_moves: para un alias que en realidad pertenece a UNA sola de las dos
  (keep_on: ese ref) o que es basura — ruido de OCR, fragmentos, variantes
  rotas — (keep_on: "none"). Los homónimos legítimos (un nombre de pila
  compartido por dos personas) se quedan en ambas: no los muevas.
- relation (solo si related): {{"from": <el más específico>, "type": <etiqueta
  en snake_case: edition_of, part_of, instance_of, specializes, …>,
  "to": <el más general>}}.

Output exacto en este formato (sin texto antes ni después):

===VERDICT===
same | different | related | unclear
===CONFIDENCE===
<entero 0-100 — qué tan seguro estás de tu verdict>
===REASONING===
<1-3 oraciones cortas explicando la decisión y cada operación propuesta. Citá señales concretas vistas.>
===RESOLUTION===
{{"survivor": "<ref>", "renames": {{"<ref>": {{"slug": "<slug>", "name": "<nombre>"}}}}, "alias_moves": [{{"alias": "<alias>", "keep_on": "<ref>|both|none"}}], "relation": {{"from": "<ref>", "type": "<tipo>", "to": "<ref>"}}}}
(omití las claves que no apliquen; {{}} si no propones nada)
===END===
```
