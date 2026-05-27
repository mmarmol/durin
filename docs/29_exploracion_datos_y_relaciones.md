# Exploración — Qué guardamos y cómo se relaciona

## Propósito

Ir despacio. Antes de cualquier plan de implementación, dejar claro:
1. Qué tipos de datos guarda durin hoy (y por qué cada uno existe).
2. Cuáles son fuentes crudas vs. síntesis.
3. Qué relaciones tienen sentido y cuáles serían basura.
4. Cómo capturamos lo valioso de las "menciones" sin inflar el grafo.

Este documento NO es plan de implementación. Es entendimiento compartido.

---

## 1. Inventario de datos (verificado contra el código)

### 1.1 Sessions
**Ubicación:** `<workspace>/sessions/<session_id>/...`
**Qué es:** la conversación cruda entre usuario y agente — turnos, tool calls, tool results, resúmenes auto-generados, metadata (título, fecha, etc.).
**Mutabilidad:** append-only durante la sesión. Después es read-only (excepto metadata como título).
**Quién escribe:** el AgentLoop, automáticamente.
**Rol:** evidencia primaria de lo que pasó. Es la "fuente original" de la cual todo lo demás se deriva.

### 1.2 Ingested
**Ubicación:** `<workspace>/ingested/<id>/`
**Qué es:** documentos externos que el usuario sube o el agente captura — PDFs, páginas web, notas largas, transcripciones.
**Mutabilidad:** immutable. Si se ingesta de nuevo, es otro `<id>`.
**Quién escribe:** el usuario (vía UI) o el agente (vía tool).
**Rol:** evidencia secundaria. Material externo que entra al sistema.

### 1.3 Memory/corpus
**Ubicación:** `<workspace>/memory/corpus/<id>.md`
**Qué es:** chunks del `ingested/` procesados + snapshots de contenido externo que el agente decide guardar como referencia citable.
**Mutabilidad:** mostly immutable. Se reemplazan si la fuente se reingesta.
**Quién escribe:** el sistema de ingestion + el agente vía `memory_ingest`.
**Rol:** unidad de búsqueda para fuentes largas (un PDF de 200 páginas se trocea en chunks corpus).

### 1.4 Memory/episodic
**Ubicación:** `<workspace>/memory/episodic/<id>.md`
**Qué es:** observaciones cortas, hechos atómicos, momentos — extraídos por el agente o por el sistema de las sesiones.
**Mutabilidad:** append-only típicamente.
**Quién escribe:** el agente vía `memory_store` o procesos automáticos.
**Rol:** materia prima para Dream. Hechos sueltos antes de sintetizarse en entities.

### 1.5 Memory/stable
**Ubicación:** `<workspace>/memory/stable/<id>.md`
**Qué es:** notas estables, hechos que el agente o usuario consideró importante guardar de manera explícita.
**Mutabilidad:** semi-mutable (editable).
**Quién escribe:** agente o usuario.
**Rol:** parecido a episodic pero con "más peso" / promovido.

### 1.6 Memory/pending
**Ubicación:** `<workspace>/memory/pending/<id>.md`
**Qué es:** buffer de entradas a clasificar/procesar.
**Rol:** intermedio del pipeline. No nos preocupa directamente.

### 1.7 Memory/entities
**Ubicación:** `<workspace>/memory/entities/<type>/<slug>.md`
**Qué es:** páginas canónicas por entidad (person:marcelo, project:durin, bug:auth_leak, ...). Tipo abierto, el agente crea nuevos tipos cuando hace falta.
**Mutabilidad:** mutable. El Dream las actualiza, el humano también puede editarlas.
**Quién escribe:** el DreamConsolidator (cold path) principalmente. El usuario opcionalmente.
**Rol:** **síntesis tipada** — el grafo de conocimiento del agente. Lo que sabemos *sobre* el mundo, no lo que pasó.

---

## 2. Dos roles distintos

Esto es la distinción clave:

| Rol | Tipos | Característica | Ejemplo |
|---|---|---|---|
| **Fuentes (evidencia)** | sessions, ingested, corpus, episodic, stable | "qué pasó / qué entró" | "El lunes Marcelo dijo que su email cambió" |
| **Síntesis (conocimiento)** | entities | "qué es / qué sabemos" | "Marcelo (person): email = nuevo@x.com" |

Las fuentes son la materia prima. Las entities son el resultado de procesarla. El Dream es quien hace la transformación fuente → entity.

---

## 3. Relaciones — qué sí, qué no

### 3.1 Las dos categorías de "relación"

| Categoría | Qué es | Cardinalidad | Aporta info? | Cómo se guarda |
|---|---|---|---|---|
| **Estructural (information-bearing)** | hechos nuevos sobre la entidad | baja (decenas) | Sí | `relations: [...]` en el .md de la entity |
| **Mención / referencia** | "esta entity aparece acá" sin info nueva | altísima (cientos/miles) | No | NO se guarda como relation. Vive en vector index. |

### 3.2 Ejemplos de relación estructural (sí first-class)

```yaml
# en person/marcelo.md
relations:
  - to: person:susana
    type: spouse
    since: 2010
  - to: project:durin
    type: maintains
    since: 2024-01

# en bug/auth_leak.md
relations:
  - to: file:src/auth/middleware.ts
    type: lives_in
  - to: commit:abc123
    type: introduced_by
  - to: person:marcelo
    type: assigned_to

# en deal/acmecorp_upgrade.md
relations:
  - to: org:acmecorp
    type: with
  - to: person:jane_doe
    type: champion
```

Cada una agrega **información nueva** sobre la entity. Lifecycle largo. Pocas por entity.

### 3.3 Ejemplos de mención (NO first-class)

- "Marcelo apareció en session:abc123" — Marcelo aparece en cientos de sessions. Cero valor estructural. Si lo guardamos como relation, su page se llena de basura.
- "Marcelo fue mencionado en corpus:paper_xyz porque el doc lo cita" — igual.
- "session:foo discutió project:durin" — durin se discute en TODAS las sessions probablemente.

**Si esto fuera relation:** entity `person:marcelo` tendría miles de relations, cada query SQL devolvería floods, el ranker se rompería.

### 3.4 Cómo se capta lo valioso de las menciones sin relations

Tres mecanismos, ninguno requiere relations explícitas:

1. **Vector search.** Las sessions, ingested, corpus están vectorizadas. Cuando el agente busca "qué sé de Marcelo", las sessions relevantes emergen por similarity. No hace falta materializar el edge.

2. **Provenance en attributes/relations.** Cuando una mención SÍ creó o modificó información estructural, se registra:
   ```yaml
   provenance:
     attributes:
       email:
         source_entry: session:abc123/turn-42  # ← acá queda
         extracted_at: 2026-05-23T10-30Z
   ```
   Esto es "creación/actualización" exactamente como pediste. Las menciones que NO crearon ni actualizaron nada no quedan rastreadas. Es correcto.

3. **Búsqueda dinámica al vuelo.** "¿En qué sessions hablé de Marcelo?" → grep + vector search sobre `sessions/`. Computamos al momento, no materializamos.

### 3.5 Regla operativa

Una relación entity→algo es first-class **si y solo si** cumple las tres:

1. Aporta información estructural nueva (no es solo "apareció acá").
2. Su existencia justifica un edge persistente (no se reconstruye trivialmente con vector search).
3. Tiene lifecycle propio (puede actualizarse, deprecarse, tener atributos como `since`, `intensity`).

Si falla alguna → no es relation. O es provenance, o es resultado de search dinámico.

---

## 4. Qué puede ser target de una relación

Una entity puede apuntar a:

| Target válido | Ejemplo de relation |
|---|---|
| Otra entity | `person:marcelo --spouse--> person:susana` |
| Una fuente cruda con identidad (ingested, corpus chunk) | `paper:gpt4_tech --introduces--> concept:rlhf` ✓ (si modelás "paper" como entity) |
| Una session específica | `decision:abandon_g3b --decided_in--> session:abc123` ✓ (poco común pero válido si la sesión tiene rol estructural) |
| Un episodic entry | normalmente NO; el episodic ya alimenta la entity en provenance |

**Importante:** sessions/ingested/episodic/corpus **NO emiten** relations. Las **reciben** (cuando una entity las apunta como source o como referencia estructural). Esto preserva la asimetría: la síntesis (entities) habla del grafo, las fuentes solo participan como nodos pasivos.

---

## 5. Hasta dónde llegamos con solo markdown + grep

Experimento mental: si todo lo que tenemos son archivos `.md` con frontmatter YAML y ningún índice (ni SQLite ni LanceDB), ¿qué podemos hacer con sólo `grep` / `ripgrep` + parseo de YAML on-the-fly?

### 5.1 Lo que SÍ cubrimos con grep

| Tipo de query | Cómo se resuelve |
|---|---|
| Lookup exacto de attribute | `grep -l "email: marcelo@mxhero.com" memory/entities/**/*.md` |
| Filtro estructural por tipo | walk `entities/<type>/*.md` + parse YAML en memoria |
| "Todos los bugs con status:open" | walk `entities/bug/*.md` + filtro Python sobre frontmatter |
| Substring / keyword search | `ripgrep` recursivo sobre body + frontmatter |
| Búsqueda dentro de sessions / corpus | `rg` sobre `sessions/` o `memory/corpus/` |
| Inverso de una relación (quién apunta a X) | `grep "to: person:susana"` en todos los entity pages |
| Auditoría / debugging | abrir el `.md` en un editor y leer |

Esto es esencialmente lo que hace **hermes-agent** (que ya estudiamos): SQLite FTS5 BM25 sobre plain files, sin vectores. Funciona para muchos casos.

### 5.2 Lo que NO cubre grep (necesita capa semántica/vectorial)

| Query que falla | Por qué |
|---|---|
| "¿Quién es la esposa de Marcelo?" sobre `spouse: susana` | "esposa" ≠ "spouse" literal |
| Cross-lingual: "donde vive" vs `lives_in: Spain` vs "vive en España" vs "住在" | grep es literal |
| Paráfrasis: "hace dos días" vs "recientemente" vs "the day before yesterday" | igual |
| Ranking por relevancia semántica | grep da hits binarios sin score (ripgrep+FTS5 ayuda con BM25 pero no llega a semantic) |
| "Concepts" relacionados sin mención literal | grep no infiere |

### 5.3 Lo que grep cubre PERO se degrada por escala

| Cantidad de entities | Latencia walk+parse on-the-fly | Veredicto |
|---|---|---|
| ~50 | < 50ms | imperceptible |
| ~500 | ~500ms | notable pero usable |
| ~5.000 | ~5s | inusable por turn |
| ~50.000 | minutos | imposible |

Para LoCoMo bench (~100 entities por workspace) → grep solo serviría. Para uso real prolongado (años de conversación) → no escala.

### 5.4 Tres niveles posibles

| Nivel | Stack | Cubre | Cuándo basta |
|---|---|---|---|
| **L0: solo markdown + grep** | grep / ripgrep + parse YAML | structural exacto + keyword | small N + queries literales |
| **L1: + LanceDB** (lo de hoy) | grep + LanceDB | + semántica + multilingual + paráfrasis | small-medium N |
| **L2: + SQLite** (plan v3 §1.2) | grep + LanceDB + SQLite | + queries estructurales rápidas + analíticas ("cuántos / cuáles") | large N o queries complejas |

### 5.5 Implicación para el MVP

Como markdown es **source of truth** y los índices son derivados, podemos elegir empezar con L0 o L1 y agregar capas cuando duelan, sin perder data. Decisiones que esto habilita:

- **MVP L1 (markdown + LanceDB, sin SQLite):** simplifica el plan v3. La phase 2 (SQLite indexer) se posterga. Hot path estructural se hace con walk+parse en memoria mientras N sea chico. Cuando bench o uso real exija más performance, se agrega SQLite.
- **L0 puro:** ya tenemos LanceDB funcionando, no parece que valga la pena renunciar a la capa semántica. Pero útil saber que es factible si por alguna razón LanceDB se cae o no quisiéramos depender de embeddings.

### 5.6 Consecuencia: grep como pilar mínimo, no como límite

Aunque agreguemos índices, **grep siempre sigue funcionando** sobre los `.md`. Es la línea base de "siempre vas a poder recuperar y debuggear con herramientas Unix estándar". Eso es un valor que el modelo de tres capas preserva: si alguien borra `.durin/index/`, el sistema sigue siendo navegable a mano.

---

## 6. Qué resuelve la búsqueda por embeddings

Si grep es la línea base (§5), el embedding es la capa que cubre lo que grep no puede.

### 6.1 Qué hace un embedding (en una frase)

Convierte un texto en un vector denso (768 dims típicamente) donde la **distancia entre vectores ≈ similitud de significado**. No similitud de palabras: similitud de significado. El modelo que usa durin (multilingual MiniLM) está entrenado para que "esposa de Marcelo", "spouse of Marcelo" y "马塞洛的妻子" produzcan vectores casi idénticos, y que `spouse: susana` en frontmatter quede cerca de los tres.

### 6.2 Casos donde el embedding gana sobre grep

| Caso | Query usuario | Documento que matchea | Por qué grep falla |
|---|---|---|---|
| **Sinónimo** | "esposa de Marcelo" | `spouse: susana` | "esposa" ≠ "spouse" literal |
| **Paráfrasis** | "¿dónde vive Marcelo?" | `current_residence: Spain` o "Marcelo se mudó a Madrid" | distintas palabras, mismo concepto |
| **Cross-lingual** | "电话" | `phone: "+34..."` | distinto alfabeto |
| **Concepto relacionado sin mención** | "problemas con autenticación" | doc que dice "auth middleware leaks memory" | "problemas" no aparece, "autenticación" tampoco |
| **Soft ranking** | "bug del login" | múltiples bugs ordenados por relevancia | grep es binario |
| **Robustez a typos** | "marselo" | `name: Marcelo` | grep falla salvo regex tolerante |
| **Conceptos amplios** | "lo que dijimos sobre arquitectura" | sessions que discuten "DreamRunner", "cold path" sin decir "arquitectura" | grep no infiere temas |
| **Búsqueda contextual** | "el plan que armamos ayer" | doc reciente con phases, decisions | "plan" puede no aparecer |

### 6.3 Casos donde el embedding falla o es peor que grep

| Caso | Query | Por qué falla |
|---|---|---|
| **Lookup exacto** | "email exacto de Marcelo" | embedding trae varios emails similares, no garantiza el correcto |
| **Identificadores únicos** | "commit abc123def" | el embedding no preserva IDs como concepto único |
| **Negación** | "Marcelo NO come carne" | embedding de "no come carne" está muy cerca de "come carne" |
| **Cuantificación / counting** | "¿cuántos bugs abiertos hay?" | trae chunks de bugs, pero contar es lógica |
| **Boolean estricto** | "bugs assigned to Marcelo AND status:open AND severity:high" | mezcla los tres pero no aplica AND lógico |
| **Recency / orden temporal** | "el último bug que cerré" | embedding ignora timestamps |
| **Discriminación fina** | "Marcelo Marmol" vs "Marcelo Marmolejo" | los puede confundir si los textos son parecidos |
| **Aritmética / razonamiento** | "¿cuánto debo a AcmeCorp?" sumando deals | embedding no suma |

### 6.4 Embedding y grep son complementarios, no sustitutos

- **Grep:** literal, preciso, determinístico, exact-match king.
- **Embedding:** semántico, tolerante, soft-match king, multilingual.

Lo que cada uno hace mal, el otro lo hace bien. En durin hoy el `entity_ranker` ya mergea resultados de ambos vía RRF (Reciprocal Rank Fusion). No es decoración: es porque cada modalidad cubre el agujero del otro.

### 6.5 Cómo se embebe en durin hoy (verificado en `vector_index.py`)

**Una sola embedding por archivo `.md`, no por chunk.**

| Para | Texto que se embebe | Budget |
|---|---|---|
| **Entity pages** | `name` + `aliases` + `body` (en ese orden) | 1500 chars (~375 tokens) |
| **Entries** (episodic, stable, corpus) | `headline` + `summary` + entities-list + `body` | 1500 chars |

Orden de prioridad: lo más destilado primero (headline / name), el body al final por ser lo más largo y truncable. Si el doc excede 1500 chars, **el body se recorta sin piedad desde el final**.

**Implicaciones operativas:**

1. **Documento largo → cola perdida.** Un entity page con 5000 chars de body: solo los primeros 1500 entran al embedding. Lo que está en el char 4000 nunca aparece en el vector. Grep sí lo encuentra.
2. **PDFs / ingested se trocean ANTES.** El pipeline de ingestion parte el PDF en múltiples corpus entries; cada chunk es su propio `.md` con su propio embedding. Eso resuelve "cola perdida" para fuentes largas (a costa de complejidad en el ingest).
3. **Frontmatter YAML estructurado NO entra al vector.** El embedding compone `name + aliases + body`, NO inyecta `attributes:` ni `relations:` literalmente. Un `email: x@y.com` en frontmatter no se "ve" semánticamente — depende de que el body lo mencione en prosa.
4. **Truncado puede ser problemático para entities populares.** Una entity con muchos hechos acumulados → body crece → cola se pierde.

### 6.6 Cinco puntos a evaluar sobre el embedding actual

#### #1 — Frontmatter NO entra al embedding

**Situación:** se embebe `name + aliases + body`. `attributes:` y `relations:` se ignoran.
**Implicación:** query "email de Marcelo" por embedding NO encuentra el frontmatter `email: x@y.com` salvo que el body lo mencione en prosa.

**Opciones:**
- (a) Renderizar attributes/relations como prosa al final del texto embebido.
- (b) Dejarlo. Queries estructurales van por grep, semánticas por body.
- (c) Híbrido: solo los attributes "semánticamente relevantes" se renderizan; los puramente lookup (email, phone, IDs) no.

**Trade-off:** (a) cubre más a costa de "contaminar centroide" (ver §6.7). (b) minimalista pero requiere router de intent. (c) ad-hoc.

#### #2 — Body se trunca a 1500 chars

**Situación:** budget hardcodeado por max_seq del modelo (~512 tokens).
**Implicación:** body > 1500 chars pierde la cola en el vector.

**Opciones:**
- (a) Cambiar a modelo con más contexto (gte-large 8192). Requiere re-indexar todo.
- (b) Chunking: múltiples vectores por entity con misma URI, RRF al rerankear. Más complejo.
- (c) Dream genera un `summary` para entity pages (hoy solo entries lo tienen). El embedding usa el summary cuando body excede budget. **Asimetría actual: entries tienen summary, entity pages no.**
- (d) Aceptar la limitación.

#### #3 — Aliases solo en entity pages, no en entries

**Situación:** entries no incluyen aliases en el texto embebido.
**Implicación:** episodic que dice "Marce" — embedding no menciona "Marcelo". Query "qué dijo Marcelo" matchea peor.

**Opciones:**
- (a) En entries, si el `entities:` field menciona `person:marcelo`, resolver alias y inyectar "(Marce/Marcelo)" en el texto embebido.
- (b) Dejarlo. Que el ranker resuelva vía el field `entities` (ya lo hace para boosting).
- (c) Inyectar TODOS los aliases del workspace en todos los entries (ruido alto).

#### #4 — ¿Se re-embebe cuando el .md cambia?

**Pendiente de verificar.** Si Dream al escribir un `.md` no dispara re-embed inmediato, los vectores quedan stale hasta el próximo rebuild. Es bug-or-not, no decisión de diseño.

#### #5 — Una sola embedding por documento mezcla facets

**Situación:** entity con múltiples roles (Marcelo: founder + esposo + dev) → un vector único promediado.
**Implicación:** queries específicas a un facet compiten con queries genéricas. Vector "promediado".

**Opciones:**
- (a) Multi-vector por facet (separar relations en grupos: family, work, projects → un embedding por grupo).
- (b) Aceptar. Pérdida real pero baja para entities pequeñas.
- (c) Particionar entity en múltiples `.md` cuando excede tamaño.

Se mezcla con #2: si hacés chunking de body largo, este caso se resuelve casi por accidente.

### 6.7 "Contaminar el centroide" — qué significa

El centroide es el vector final del documento. Un embedding "promedia" (conceptualmente) los conceptos del texto en un único punto del espacio de 768 dims.

**Ejemplo concreto.** Entity Marcelo con body = "Marcelo es fundador de durin y trabaja en arquitectura de agentes."

- Sin frontmatter renderizado → vector cerca de "founder, durin, architecture, agents".
- Con frontmatter renderizado → texto_embed = "Marcelo es fundador... Email: x@y.com. Phone: +34. Lives_in: Spain. Spouse: Susana." → vector se MUEVE hacia "email, phone, Spain, spouse".

Consecuencia:
- Query "fundador de durin" → matchea **menos fuerte** (centroide diluido).
- Query "email de Marcelo" → matchea **más fuerte**.

**Es trade-off, no bug.** Tres formas de mitigar si lo que se quiere es "ambas":
1. Multivector (#5): un vector "core" sin frontmatter + uno "attrs" con frontmatter. Mergeás. Resuelve a costa de complejidad del ranker.
2. Renderizar al **final** del texto embebido: los modelos de embedding pesan más las primeras palabras. Al final, el centroide se mueve menos.
3. Renderizado selectivo: solo attributes "semánticos" (residence, role, status) se renderizan; los lookup-only (email, phone) no.

### 6.8 Estado de la discusión

Resumen de lo conversado (NO es plan, es entendimiento + inclinaciones):

| Punto | Inclinación | Estado |
|---|---|---|
| #3 Aliases en entries | Tiene sentido | inclinación a favor |
| #2 Dream resume body largo (entity pages) | Mejor que truncar | inclinación a favor; falta decidir si embebe summary solo o summary+body |
| #4 Re-embed en write | Tiene sentido si falta | pendiente verificar implementación |
| #5 Multivector | Comprendido | decisión pendiente |
| #1 Renderizar frontmatter | Comprendido | decisión pendiente |

---

## 7. LanceDB — qué rol cumple en el pipeline de búsqueda

LanceDB no es un "tercer mecanismo" de búsqueda — es el **motor que hace que el embedding sea usable a escala**. Sin LanceDB, calcular embeddings sería intelectualmente correcto pero operativamente inviable.

### 7.1 Qué hace cada componente (clarificación)

| Componente | Rol |
|---|---|
| **MiniLM (embedding model)** | Convierte texto → vector de 768 dims. Es la función `text -> [0.21, -0.45, ...]`. |
| **LanceDB** | Base de datos vectorial. Almacena los vectores y encuentra los más cercanos a un vector de query en microsegundos. |
| **Grep / ripgrep** | Búsqueda literal de strings sobre los `.md` en disco. |
| **entity_ranker** | Mergea resultados de vector + grep + boosts por entity-tag via RRF. |

Sin LanceDB, los embeddings serían inútiles a escala: tendrías que comparar el vector de query contra TODOS los vectores almacenados uno por uno (brute force, O(n)). Con 100 entities funciona; con 100.000 son minutos por query.

LanceDB resuelve eso con un índice ANN (Approximate Nearest Neighbors): pre-organiza los vectores en una estructura que permite encontrar los top-K más cercanos en log(n). Para nuestro caso es **microsegundos** incluso con miles de vectores.

### 7.2 Cómo LanceDB entra en el pipeline de search hoy

Flujo concreto de una query `"esposa de Marcelo"`:

```
1. Query "esposa de Marcelo"
                ↓
2. MiniLM.embed("esposa de Marcelo") → vector_query [0.2, -0.4, ...]
                ↓
3. LanceDB.search(vector_query, top_k=10)
   ├─ scan del índice ANN sobre todos los vectores almacenados
   └─ devuelve 10 filas más cercanas con metadata (uri, path, type, entities, ...)
                ↓
4. Grep paralelo sobre keywords de la query → lista de hits literales
                ↓
5. entity_ranker (RRF) → mergea resultados vector + grep
                ↓
6. Boost por entity-tags (si query menciona entity X, prioriza hits con entities=X)
                ↓
7. Top-K final → al agent
```

**LanceDB hace SOLO el paso 3.** El embedding (paso 2) es MiniLM. El RRF (paso 5) es nuestro `entity_ranker`. Grep (paso 4) es el fallback léxico.

### 7.3 Qué guarda LanceDB exactamente

Cada fila en el índice contiene:

| Campo | Contenido |
|---|---|
| `vector` | vector de 768 dims del documento (es la clave del índice ANN) |
| `uri` | identificador del documento (`person:marcelo`, `episodic/2026-05-23.md`, ...) |
| `path` | ruta al `.md` en disco |
| `type` | clase del entry (entity/episodic/corpus/stable) |
| `entities` | lista de entity URIs mencionadas (usado por el ranker para boost) |
| otros metadatos | headline, summary, etc. para deduplicación y display |

**Importante:** LanceDB NO guarda el texto del documento. Solo el vector + metadata. El `.md` sigue viviendo en disco (es la SoT). LanceDB es un **índice derivado** sobre los `.md`. Si se corrompe o se borra, se reconstruye desde los archivos.

### 7.4 Qué rol cubre en el problema general

Mapeo problema → solución:

| Problema | Lo resuelve | Cómo |
|---|---|---|
| "Cómo convierto texto a vector que capture significado" | **MiniLM** | modelo neural pre-entrenado |
| "Cómo encuentro los vectores más parecidos a este, rápido y a escala" | **LanceDB** | índice ANN |
| "Cómo combino búsqueda semántica con literal" | **entity_ranker** | RRF entre vector results + grep results |
| "Cómo guardo el texto original editable" | **`.md` en disco** | source of truth |

Cada cosa hace una pieza distinta. LanceDB en particular es **el solucionador del problema de escala** del embedding.

### 7.5 Lo que LanceDB NO hace

| Lo que NO hace | Quién lo cubre |
|---|---|
| Generar embeddings | MiniLM |
| Entender semántica intrínsecamente | nadie (la semántica está en el vector que viene calculado) |
| Queries estructurales tipo SQL complejas | grep / parse YAML / SQLite si se agrega |
| Re-embebear automáticamente cuando un `.md` cambia | nuestro código (Dream apply / file watcher) |
| Reconciliar entities duplicadas | nadie hoy (es uno de los agujeros del plan v3) |

### 7.6 Por qué importa entender esto para nuestras decisiones

Las cinco decisiones de §6.6 (frontmatter, truncado, aliases, re-embed, multivector) son todas decisiones sobre **qué texto pasamos a MiniLM**. NO son decisiones sobre LanceDB. LanceDB acepta cualquier vector que le demos.

Esto simplifica el diseño: los cambios para mejorar el embedding viven en `vector_index.py::_embed_text` y `_entity_embed_text`. LanceDB ni se entera.

La única decisión que SÍ toca LanceDB directamente es el **schema de filas** — si en algún momento decidimos hacer multivector (§6.6 #5), agregaríamos un campo `facet` al schema y la lógica de dedup en el ranker. Pero hoy LanceDB ya soporta filtros sobre metadata, así que es factible sin re-arquitectura.

### 7.7 Resumen visual de las capas

```
┌─────────────────────────────────────────────────────────┐
│  USUARIO/AGENTE QUERY                                    │
└────────────────────┬────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        ▼                          ▼
   ┌─────────┐               ┌──────────┐
   │ MiniLM  │               │  Grep    │
   │ (embed) │               │ (rg)     │
   └────┬────┘               └─────┬────┘
        │                          │
        ▼                          │
   ┌─────────┐                     │
   │ LanceDB │ (búsqueda ANN)      │
   │  index  │                     │
   └────┬────┘                     │
        │                          │
        ▼                          ▼
   ┌─────────────────────────────────┐
   │      entity_ranker (RRF)         │
   │  vector results + grep results   │
   │  + boost por entity-tag          │
   └─────────────────┬────────────────┘
                     │
                     ▼
              ┌─────────────┐
              │  Top-K      │
              │  al agente   │
              └─────────────┘

  Source of truth abajo de todo:
   ┌──────────────────────┐
   │ memory/**/*.md       │  (los .md son la verdad)
   │ + sessions/          │
   │ + ingested/          │
   └──────────────────────┘
```

LanceDB es **una pieza dentro del pipeline**, no es "la búsqueda". La búsqueda es el conjunto `MiniLM + LanceDB + grep + entity_ranker` operando sobre los `.md`.

---

## 8. Sessions e ingested en el pipeline de búsqueda

### 8.1 Cómo entran hoy

Verificado en `memory_search.py` (flujo real, no idealizado):

```
Query → LanceDB.search → vector_results (sobre memory/<class>/*.md)
                              │
                              ▼
                       entity_ranker (RRF entity-aware)
                       reordena los vector rows
                              │
                              ▼
              ¿scope == "all" (default)?
                              │
                ┌─────────────┴─────────────┐
                SÍ                          NO
                │                           │
                ▼                           ▼
        grep sobre sessions + ingested    FINAL = vector_results
        (NO vectorizados)
                │
                ▼
        FINAL = vector_results + grep_results
        (concatenación, NO RRF cruzado)
```

**Importante (corrige imprecisión de §7.2):**

- El `entity_ranker` aplica RRF entre **ranking por distancia vectorial** y **ranking por entity-tag match**, NO entre vector y grep.
- Sessions e ingested se buscan por grep **únicamente**. No están en el índice vectorial.
- La unión vector + grep es concatenación literal, no merge por score.

### 8.2 ¿Por qué hoy NO se vectorizan sessions ni ingested crudos?

| Razón | Detalle |
|---|---|
| **Tamaño desproporcionado** | Session de 200 turnos × ~500 chars = 100k chars. El embedding solo capta los primeros 1500. La cola masiva se pierde. |
| **Multi-tema** | Una session larga toca 5-10 temas. Un único vector promedia todo → no representa bien ningún tema individual. |
| **El Dream ya destila** | Lo importante de la session sale a episodic + entities, que sí se vectorizan. La session cruda es evidencia, no canónica. |
| **Volatilidad** | Sessions vivas cambian cada turno. Re-vectorizar por turno es overhead inútil. |
| **Volumen** | Cientos de sessions por workspace a lo largo del tiempo. |

Para **ingested**, además: el pipeline ya parte el contenido crudo en chunks → cada chunk es un `corpus/<id>.md` que **sí se vectoriza**. La unidad de retrieval es el chunk, no el doc original. Esto está bien diseñado, no es un agujero.

### 8.3 El agujero: `_last_summary` existe pero no se vectoriza

Verificado en `agent/memory.py`: la compactación genera un rolling summary por session que vive en `<session>.meta.json::derived._last_summary`.

| Característica | Por qué importa |
|---|---|
| Es corto (cientos a pocos miles de chars) | Entra entero al budget de 1500 chars, sin cola perdida |
| Captura el tema central de la session | Buen candidato a representación semántica |
| Se actualiza solo cuando la compactación corre | No es por-turno → costo de re-embed acotado |
| **Hoy NO se vectoriza** | Query "¿en qué session hablamos de arquitectura?" hoy solo lo encuentra grep |

**Decisión (inclinación a favor):** vectorizar `_last_summary` por session. Es el candidato natural para llenar el agujero.

Esquema propuesto a nivel conceptual (no plan de implementación):
- Cada session tiene una entrada en LanceDB con `uri=session:<id>`, `vector=embed(_last_summary)`, `path=<session_meta_path>`.
- El embedding se regenera cuando la compactación actualiza el summary (un evento ya existente, no necesita scheduler nuevo).
- Sessions sin summary aún (cortas, pre-compactación) quedan fuera del vector — sigue cubriéndolas grep por el flujo actual.

### 8.4 Sub-agujeros que quedan abiertos

| Sub-agujero | Cuándo aparece | Posible mitigación (no decidir ahora) |
|---|---|---|
| Sessions sin compactación aún | Sessions cortas o muy recientes | Forzar summary mínimo desde el primer turno, o aceptar que solo grep las cubra |
| Sessions multi-tema (1 summary lossy) | Sessions largas que tocan 5-10 temas | Chunks por bloques de N turnos, cada uno con su propio summary + embedding (mezcla con multivector §6.6 #5) |
| Calidad del summary | Si el summarizador omite Y, query sobre Y falla por vector | Mejora del prompt de compactación; ortogonal al embedding |
| Sessions actualizadas vs. embedding stale | Summary cambia pero re-embed no se dispara | Mismo problema que §6.6 #4 (re-embed en write) — solución compartida |

### 8.5 Síntesis

| Caso | Diagnóstico actual |
|---|---|
| Sessions crudas sin vectorizar | Correcto |
| `_last_summary` sin vectorizar | **Agujero real**. Candidato directo a llenar. |
| Sessions sin summary aún | Sub-agujero menor (grep las cubre) |
| Multi-tema en sessions largas | Sub-agujero. Mejor solución es chunks, pero más complejo |
| Ingested crudo sin vectorizar | Correcto. Cubierto vía corpus chunks. |

---

## 9. API de la tool `memory_search` — qué parámetros le damos al agente

### 9.1 Estado actual

La tool acepta **un solo string** `query`. Internamente ese mismo string se usa para:
1. Embedding → LanceDB.
2. Grep léxico sobre archivos.

**El agente no sabe que existen dos modos.** Por eso a veces escribe queries optimizadas para uno pero malas para el otro:

| Query del agente | Buena para | Mala para |
|---|---|---|
| "¿dónde vive Marcelo?" | vector (paráfrasis) | grep (solo matchea "Marcelo" literal) |
| `marcelo@mxhero.com` | grep (exact) | vector (emails no tienen "significado" capturable por MiniLM) |

### 9.2 Mejora del ranker (decisión: a favor)

**Hoy:** la unión vector + grep es concatenación literal (no RRF cruzado real). Un item que aparece en AMBOS sets no rankea más alto que uno que aparece en uno solo.

**Mejora propuesta:** RRF cruzado real entre vector_results y grep_results. Si un item está en ambos, su score combinado lo prioriza.

**Beneficio:** resuelve el problema de "precisión por co-ocurrencia" SIN pedirle al agente que decida AND/OR. La mecánica vive en el ranker, transparente.

**Costo:** chico. El ranker ya hace RRF entity-aware; agregar otro nivel de fusión es factible.

### 9.3 Cuatro opciones para la API expuesta al agente

| Opción | API | Pros | Contras |
|---|---|---|---|
| **A. Status quo + hint** | `query` único, descripción mejor explicada | Cero cambio estructural. Cualquier cambio interno queda transparente al agente. | El agente sigue sin poder afinar. Hint en la description tiene impacto limitado (tool description = weak signal). |
| **B. Híbrido opcional ← inclinación** | `query` (required, usado para ambos) + `keywords` (opcional, solo grep literal) | Compat backward total. Cuando el agente no afina, funciona como hoy. Cuando afina, gana precisión. | Dos campos a documentar bien. Riesgo medio de misuse (agente pone lo mismo en los dos). |
| **C. Modo explícito** | `query` + `mode: auto\|semantic\|literal` | Agente declara intent. Telemetría limpia (sabés qué modo usó). | El agente tiende a elegir mal el modo (tool description = weak signal). |
| **D. Dos tools separadas** | `memory_search_semantic(query)` + `memory_search_keyword(pattern)` | Cada tool clara y focalizada. Telemetría perfecta. | Más tool calls por turno. El agente puede olvidarse de hacer las dos cuando ambas convienen. |

**Inclinación:** **Opción B** — agregar un `keywords` opcional para queries literales, manteniendo `query` como hoy.

### 9.4 Tres decisiones laterales

#### 9.4.1 ¿AND / OR entre los sets?

**No exponer al agente como parámetro.** El problema real no es booleano, es scoring. Se resuelve mejor en el ranker (§9.2) que pidiéndole al agente que decida.

Si tanto vector como grep matchearon un item, el ranker lo eleva. Eso da el beneficio sin agregar superficie de API ni riesgo de misuse.

#### 9.4.2 ¿Regex en grep?

**Inclinación: dejarlo afuera por ahora.**

Pros: habilita queries imposibles hoy (`commit:abc[0-9a-f]+`, `email:.*@mxhero`).
Contras: ReDoS si el LLM escribe regex con backtracking explosivo. Bajo ROI esperado — la mayoría de queries reales son keywords simples.

Si se agregara después: `regex: bool = false` flag + timeout estricto + escape por default.

#### 9.4.3 ¿Esconder o exponer la semántica al agente?

| Filosofía | Pro | Contra |
|---|---|---|
| **Esconder** | Simple. Cambios internos transparentes. | Agente no puede optimizar. |
| **Exponer** | Agente puede afinar. | API más compleja. Riesgo de misuse. |

La opción B es exposición parcial: `query` esconde la decisión por default, `keywords` la expone cuando importa.

### 9.5 Síntesis

| Decisión | Inclinación |
|---|---|
| Mejora del ranker (RRF cruzado real con boost por co-ocurrencia) | A favor |
| API de la tool (opción A/B/C/D) | **B** — query required + keywords opcional |
| AND/OR como parámetro | NO. Resolverlo en el ranker. |
| Regex en grep | Dejar afuera por ahora. Bajo ROI, riesgo no trivial. |
| Esconder vs exponer | Exposición parcial (la que la opción B implica). |

---

## 10. FTS5 BM25 y otros mecanismos — precedente OpenClaw

### 10.1 Qué es cada mecanismo (clarificación)

| Mecanismo | Qué hace |
|---|---|
| **Grep / ripgrep** | Búsqueda literal de strings sobre archivos. Output binario (matchea o no). Cero scoring. |
| **BM25** | Algoritmo de scoring para keyword search. Da un número (0-N) por documento según frecuencia de términos × longitud × rareza del término en el corpus. |
| **FTS5** (Full-Text Search en SQLite) | Implementación práctica de BM25: índice invertido + tokenización + boolean operators dentro de SQLite. |
| **SQLite estructural** | Base relacional para queries analíticas (WHERE, COUNT, GROUP BY, JSON_EXTRACT). Ortogonal a FTS5. |
| **MySQL / Postgres** | Server-based. Overhead sin valor diferencial para agente local. |

**Importante:** BM25 es el algoritmo. FTS5 es una implementación de ese algoritmo dentro de SQLite.

### 10.2 Qué resolvería cada uno en durin

| Mecanismo | Cubre lo que NO cubren los otros |
|---|---|
| **Grep** | Exact-match literal sobre archivos crudos. Debugging directo. |
| **FTS5 BM25** | Keyword search con scoring real, multi-término, boolean. Ranking léxico que grep no tiene. |
| **Embedding + LanceDB** | Sinónimos, paráfrasis, cross-lingual. Significado sin coincidencia literal. |
| **SQLite estructural** | Counting, aggregations, joins sobre attributes/relations parseados. |

Cada uno cubre un nicho. Ninguno es sustituto del otro.

### 10.3 Precedente OpenClaw (verificado en `~/git_personal/openclaw/`)

OpenClaw es **el sistema más comparable a durin en stack y diseño general** entre los que hemos estudiado. Single-user local agent, file-based memory, SQLite + vectores. Y hace exactamente el patrón híbrido que veníamos discutiendo:

```
Query → Embedding → Vector Search ┐
      ↓                            ├── Weighted Merge → Top Results
      → Tokenize → BM25 Search ────┘
```

**Características clave de OpenClaw (`docs/concepts/memory-search.md` + `extensions/memory-core/src/memory/manager.ts`):**

| Característica | Detalle |
|---|---|
| **Pipeline híbrido** | Vector y BM25 corren en paralelo (no concatenación). |
| **Weighted merge** | `textWeight` (default 0.3) + `vectorWeight` configurables. |
| **Fallback graceful** | Si no hay embeddings o no hay FTS, el otro corre solo. |
| **Degraded mode sin vectores** | BM25 + boost por path coverage. |
| **CJK tokenizer** | `unicode61` o `trigram` para Han/CJK queries. |
| **QMD sidecar** | Local-first con BM25 + vectores + reranking + query expansion. |

**El "weighted merge" de OpenClaw es lo que en §9.2 llamamos "RRF cruzado real con boost por co-ocurrencia".** Mismo concepto, distinta implementación (sum ponderada de scores normalizados vs RRF rank-based — ambos válidos).

**Otro precedente complementario:** Hermes-Agent usa FTS5 BM25 sin vectores. Sirve como prueba de que FTS5 funciona standalone para uso real (memoria de skills), pero OpenClaw es el reflejo más fiel del modelo durin.

### 10.4 Comparación durin vs OpenClaw

| Aspecto | durin hoy | OpenClaw |
|---|---|---|
| Vector | ✓ LanceDB + MiniLM | ✓ sqlite-vec o sidecar QMD |
| Léxico | ✗ grep binario | ✓ FTS5 BM25 con scoring |
| Fusión vector + léxico | Concatenación sin score | Weighted merge configurable |
| RRF entity-aware | ✓ propio | ✗ no lo tienen |
| Temporal decay | ✗ | ✓ (half-life 30d default) |
| MMR diversidad | ✗ | ✓ |
| Stack | Python + LanceDB + grep | TS + SQLite (FTS5+sqlite-vec) o QMD |

**Lecturas:**

- durin tiene una pieza única que OpenClaw no (entity-aware RRF). Eso es ventaja propia.
- durin tiene tres piezas que OpenClaw sí y nosotros no: BM25 scoring, weighted merge, temporal decay, MMR.
- El stack de OpenClaw es diferente (TS) pero el patrón de búsqueda es directamente trasladable.

### 10.5 Features adyacentes que OpenClaw tiene y vale considerar

#### 10.5.1 Temporal decay

**Qué hace:** los documentos viejos pierden ranking weight gradualmente. Half-life default 30 días: una note de hace 30 días score 50% de su peso original. Documentos "evergreen" marcados (como `MEMORY.md`) nunca decaen.

**Cuándo importa:** cuando un agente tiene meses o años de notas y entries antiguas siguen rankeando alto compitiendo con context reciente. En LoCoMo bench no aplica (sessions sintéticas cortas). En uso real prolongado sí.

**Costo:** chico. Es un factor multiplicativo en el ranker. Necesita timestamp confiable por documento (ya lo tenemos).

#### 10.5.2 MMR (Maximal Marginal Relevance)

**Qué hace:** reduce redundancia en top-K. Si 5 entries mencionan el mismo router config, MMR ensures que el top-K cubre 5 temas distintos en vez de repetir el mismo.

**Cuándo importa:** sessions multi-tema donde el mismo fact aparece en varios chunks. Sin MMR, top-3 puede ser tres chunks casi idénticos.

**Costo:** medio. Necesita comparar similarity entre los hits del top-K. No es trivial implementarlo bien.

### 10.6 Síntesis revisada

El precedente OpenClaw fortalece el caso de FTS5 BM25 más de lo que parecía:

| Decisión | Estado |
|---|---|
| **FTS5 BM25** complementando vector | Inclinación a favor reforzada por precedente OpenClaw |
| **Weighted merge** (en lugar de concatenación) | Inclinación a favor — es §9.2 + alineado con OpenClaw |
| **Temporal decay** | A considerar — nuevo, no estaba en discusión previa |
| **MMR diversidad** | A considerar — nuevo, no estaba en discusión previa |
| **SQLite estructural** (separado de FTS5) | Depende de queries analíticas esperadas — sin cambio |
| **MySQL / Postgres** | No |

### 10.7 Lo que sigue abierto

- Decidir si FTS5 BM25 va antes o después de las mejoras de embedding (§6.6, §8.3).
- Decidir si temporal decay / MMR entran en el MVP o se difieren.
- Si va FTS5, decidir si usar `sqlite-vec` también (alineando full stack con OpenClaw) o mantener LanceDB.
- Verificar comportamiento de FTS5 con queries mixtas español/inglés/CJK (OpenClaw tuvo bugs en eso — ver CHANGELOG.md).

---

## 11. Duplicación de información entre sessions / episodic / entities

### 11.1 El problema verificado

El mismo fact puede vivir simultáneamente en **tres lugares** del workspace:

```
Usuario: "vivo en España"
        │
        ├─→ 1. session crudo (turno conversacional)
        │    └─ retrievable por grep sobre sessions/
        │
        ├─→ 2. memory_store crea episodic/<ts>.md "Marcelo lives in Spain"
        │    └─ retrievable por vector + grep sobre memory/
        │
        └─→ 3. Dream lo consolida en entities/person/marcelo.md (attribute lives_in: Spain)
             └─ retrievable por vector + grep sobre memory/
                el episodic NO se borra después de consolidación
```

Verificado en `memory_store.py`, `dream.py`, `search.py`:

- `memory_store` crea entries en `memory/episodic/` (default) o `stable/` o `corpus/`.
- Dream consolida los episodic en entity pages y avanza el cursor `dream_processed_through`.
- **El episodic NO se borra**. Sigue en disco, sigue en LanceDB, sigue siendo retornado por `memory_search`.

### 11.2 Consecuencias

| Problema | Manifestación |
|---|---|
| Ruido en top-K | Mismo fact ocupa 2-3 slots de los 10 retornados → menos espacio para info nueva |
| Tokens desperdiciados | LLM ve el mismo dato 3 veces → contexto inflado |
| Coherencia frágil | Si entity dice "Spain" pero episodic dice "España", el agente ve ambas |
| Drift temporal | Si el usuario actualiza ("ahora vivo en Argentina"), nuevo episodic + entity actualizado coexisten con el viejo episodic → ambigüedad |
| Bench inflado | Datasets sintéticos generan muchos episodic, Dream consolida pero no limpia → duplicación crece |

### 11.3 Opciones evaluadas

| Opción | Mecánica | Pros | Contras |
|---|---|---|---|
| **A. Borrar episodic post-consolidación** | Dream elimina el episodic después de actualizar la entity | Elimina duplicación de raíz | Pierde provenance y detalles que la entity puede no haber preservado. Destructivo. |
| **B. Archivar (no borrar)** | Mover `memory/episodic/<ts>.md` → `memory/archive/episodic/<ts>.md`. `memory_search` por default no scans archive. Re-indexar lance. | Preserva provenance. Elimina ruido en queries normales. Permite re-procesar si Dream consolidó mal. | Un directorio más. Coordinación con re-index. |
| **C. Marker `consolidated_into:` en frontmatter** | El episodic queda donde está pero con marca. Ranker filtra hits con marker cuando la entity también está en results. | Zero file ops. Trazabilidad perfecta. | Lógica de filtrado en ranker. Más complejo. |
| **D. Dedup por content-hash en el ranker** | `memory_search` calcula hash y dedup post-merge | Mínimo cambio. Resuelve también dedup entre vector y grep. | Hash sobre body completo no captura paráfrasis ("Lives in Spain" vs "Marcelo lives in Spain"). |
| **E. Hot layer compensa** | El hot_layer ya inyecta entity pages canónicas | Sin cambios. | No resuelve memory_search results; LoCoMo bench invoca memory_search, no se beneficia. |

### 11.4 Decisión: Opción B — archivar episodic post-consolidación

**Inclinación confirmada.** Mecánica:

- Cuando Dream consolida un episodic en una entity page, mover el archivo a `memory/archive/episodic/<ts>.md`.
- `memory_search` por default NO escanea `memory/archive/`. Vector index excluye archive del rebuild.
- Un scope opcional `archive: bool` o un namespace dedicado permite consultar archived cuando hace falta auditoría/recuperación.
- Si Dream consolida mal, el episodic original está en archive — recuperable.

**Sub-decisiones pendientes:**

- ¿Borrar de LanceDB al archivar o mantener con flag? Borrar es más limpio; flag preserva trazabilidad. Probablemente borrar.
- ¿Trigger del archive es síncrono en `Dream.apply()` o async post-batch? Síncrono es más simple; async evita bloqueo.
- ¿Qué pasa si el episodic NO termina en una sola entity (multi-entity)? Posible regla: archivar solo cuando TODAS las entities mencionadas lo consolidaron.

---

## 12. Resultados sectorizados con marcadores estructurales

### 12.1 El patrón ya existe parcialmente

Verificado en `memory_search.py:348-356`:

```python
# §2.H: rendered block carries explicit `=== CANONICAL/FRAGMENT ===`
# markers so the LLM can distinguish the main answer from recent
# post-cursor context at parse time.
```

Cada hit hoy se rinde con un marker `=== CANONICAL: <ref> ===` o `=== FRAGMENT: <ref> (ts: ...) ===`. **No estamos partiendo de cero.**

### 12.2 La propuesta: extender a más categorías

| Sección | Qué iría | Cómo se identifica |
|---|---|---|
| `=== CANONICAL ===` (ya existe) | Entity pages consolidadas | Path en `memory/entities/` |
| `=== FRAGMENT ===` (ya existe) | Episodic post-cursor | Path en `memory/episodic/`, ts > cursor |
| `=== SESSION ACTUAL ===` | Turnos relevantes de la session en curso | Session ID = sesión activa |
| `=== SESSION HISTÓRICA ===` | Hits sobre sessions previas | Path en `sessions/<otro_id>/` |
| `=== INGESTED ===` | Hits sobre corpus/ingested | Path en `memory/corpus/` o `ingested/` |

### 12.3 Qué ayuda (a priori)

| Beneficio | Detalle |
|---|---|
| **Resolución de conflictos** | Si CANONICAL dice "vive en Spain" y FRAGMENT dice "se muda a Argentina ayer", el LLM puede responder con matices temporales. |
| **Confianza calibrada** | CANONICAL = pasó por Dream (validado). FRAGMENT = observación cruda no consolidada (provisional). El LLM modula tono. |
| **Citation cleaner** | El LLM cita "según la entity page" vs "según la session del lunes". |
| **Patrón ya validado** | Hermes usa `<memory-context>`. Hot layer de durin ya usa CANONICAL/FRAGMENT. Comportamiento conocido. |

### 12.4 Riesgos identificados

| Riesgo | Mitigación |
|---|---|
| **Bias hacia "canonical"** (LLM confía en obsoleto, ignora reciente) | Etiquetas descriptivas (timestamps + ubicación), NO valorativas. NO usar nombres como "MEMORIA PERMANENTE". Mejor `CANONICAL (consolidated 2026-05-20)`. |
| **Tokens extra** | Headers cortos. Sin explanations inline largas. |
| **Tool description = weak signal** | NO esperar que el LLM siga instrucciones tipo "treat as authoritative". Confiar en que infiera del marker estructural + timestamp, no de instrucciones explícitas. |
| **Secciones vacías** | Omitir secciones sin hits. |
| **Categorías no ortogonales** | Reglas claras de pertenencia. Si una entity se actualizó hoy, sigue siendo CANONICAL (no es FRAGMENT) — fragment se define por path en `episodic/` post-cursor, no por recencia. |

### 12.5 Patrón concreto propuesto

```
=== CANONICAL: person:marcelo (consolidated 2026-05-20) ===
type: person
attributes:
  current_residence: Spain
relations:
  - to: project:durin, type: maintains, since: 2024-01

=== FRAGMENT: episodic/2026-05-26T10-12.md (ts 2026-05-26) ===
Marcelo mentioned moving to Argentina next month for personal reasons.

=== SESSION: c155274d... turn 47 (ts 2026-05-25) ===
User: "estoy pensando en mudarme"
Assistant: "¿a dónde?"
User: "Argentina"
```

El LLM al ver esto puede inferir el estado temporal y responder con matices.

### 12.6 Decisión: a favor de extender el patrón

**Inclinación confirmada.** Con dos guardas:
1. Etiquetas descriptivas (ubicación + timestamp), nunca valorativas.
2. Mantener ≤5 categorías. Más es ruido.

### 12.7 Implicación: tocar las instrucciones que pasamos al agente

**Punto crítico:** este cambio requiere actualizar el texto donde le explicamos al agente cómo funciona la memoria. Hoy:

- `durin/templates/agent/identity.md` tiene una sección sobre Memory.
- La tool description de `memory_search` explica los results.

Ambos lugares deben:
- Explicar las categorías (sin ser valorativos).
- Indicar cómo interpretar marcadores y timestamps.
- NO incluir instrucciones tipo "priorizá X sobre Y" — esas tienden a ser weak signals.

Lo correcto: descripción estructural + ejemplos. Confiar en que el LLM use bien los markers si están bien definidos.

---

## 13. Aprendizajes de experimentos descartados

### 13.1 G3.b — query rewriter LLM antes del search

**Qué hacía:** llamada a LLM (glm-5.1) ANTES de cada vector search. El LLM generaba:
- 5 paráfrasis de la query (incluyendo la original)
- intent (factual_lookup / list / temporal / comparison / open_ended)
- entities extraídas
- predicates relevantes
- language_hint

Cada paráfrasis se vectorizaba y se buscaba en LanceDB. Los results se mergeaban vía RRF.

**Por qué se revertió (2026-05-26):**

| Problema | Detalle |
|---|---|
| 1 LLM call por search | Cada `memory_search` gastaba 1 LLM call extra |
| N searches por turn | Agente hace 2-3 searches por turn → 2-3 LLM calls extra solo en rewriting |
| z.ai rate limits | Timeouts + empty answers en bench 102-QA |
| Asimetría invertida | Search es hot path (cada turno), LLM es operación cara. Patrón: "es como escribir en la DB por cada SELECT" |

**Por qué eran hacks (la lección real):**

Los rewrites **compensaban debilidades upstream en lugar de arreglarlas**:

| El problema real | Lo que el rewriter compensaba | Solución no-hack |
|---|---|---|
| Body solo embebe 1500 chars (cola perdida) | Genera paráfrasis para "rescatar" lo perdido | Chunking / summary para entity pages (§6.6 #2) |
| Frontmatter no entra al vector | Inventa palabras del body | Renderizar frontmatter al embedding (§6.6 #1) |
| MiniLM-L12 es chico | Pide a GLM sinónimos para hacer match | Embedding model más grande, o complementar con BM25 |
| Cross-lingual depende del modelo | LLM traduce/expande la query | BM25 con tokenizer multilingual + embedding fuerte |
| Aliases solo en entity pages | LLM extrae entities libres | Aliases en entries (§6.6 #3) |

**Query expansion es técnica clásica de IR** (Rocchio 1971, pseudo-relevance feedback). Lo que la hizo hack en G3.b fue:
- Costo O(LLM call) por query vs O(1) lookup table de la versión clásica.
- Compensa síntomas en vez de causas.
- Estocástica, opaca, depende del modelo.

### 13.2 Lo que sobrevivió

`durin/memory/query_rewriter.py` se preservó como **librería opt-in** para uso en cold path (no en hot path). Componentes maduros: cache thread-locked (cross-loop deadlock learning), normalización CJK, `_lenient_json_loads` con `json_repair`, `build_memory_llm_invoke` con preset selection.

### 13.3 Lección general (regla operativa)

**Cuando consideremos agregar LLM en cualquier path frecuente, preguntar primero:**

1. ¿Qué debilidad upstream estoy compensando?
2. ¿Puedo arreglarla en su origen (mejor texto indexado, mejor embedding model, mejor index)?
3. ¿Costo operativo total (tokens × rate limits × latencia × turns) es sostenible?

Si las respuestas a 1 y 2 sugieren un fix upstream → arreglar ahí.
Si 3 da rojo → no meter LLM en hot path.

### 13.4 Cómo esto refuerza el plan actual

Las decisiones que ya tomamos en este doc atacan las causas que G3.b intentaba parchear:

| Decisión actual | Causa raíz que ataca |
|---|---|
| §6.6 #1 Renderizar frontmatter al embedding | Frontmatter ausente del vector |
| §6.6 #2 Dream summary para entity pages | Cola perdida por truncado |
| §6.6 #3 Aliases en entries | Recall por apodos |
| §10 FTS5 BM25 | Complemento real al vector (no LLM disfrazado) |
| §12 Sectorización con marcadores | Estructurar input al LLM final, no pre-digerir |

Cada una resuelve una pieza del problema que G3.b cubría con parche.

---

## 14. Mecanismos no evaluados — gap-list de otros sistemas

Sistemas de memoria que estudiamos (mem0, letta/MemGPT, Zep, Graphiti, Cognee, Hermes-Agent, OpenClaude, OpenClaw, OpenHands, GAAMA) implementan mecanismos que durin **no ha evaluado todavía**. Esta sección los lista para que ninguno quede como blind spot.

### 14.1 Tabla maestra

| # | Mecanismo | Origen | Valor potencial | Costo | Lectura inicial |
|---|---|---|---|---|---|
| **A** | Reflection / pattern detection | GAAMA, Zep | Medio. Útil para agente generalista. | Alto. Nuevo Dream tier 2. | A futuro. |
| **B** | Concepts como entities mediadoras | GAAMA hypergraph | Bajo en MVP. | Alto. Cambio arquitectónico. | A futuro lejano. |
| **C** | HyDE (Hypothetical Document Embeddings) | Clásico IR moderno | Bajo (LLM en hot path) | Bajo. | **Descartado** por lección §13. |
| **D** | Cross-encoder reranker | Estándar IR moderno (BEIR, MTEB) | Alto. Sin LLM en hot path. | Medio. Dep + 50-100 LOC. | **El más prometedor de esta lista.** |
| **E** | Versioning temporal explícito de memoria | Zep, mem0 | Bajo (ya tenemos git history) | Bajo. | Aprovechar `memory/.git/` existente. |
| **F** | Consistency checks cross-entity | Graphiti, Cognee | Medio-alto. Detecta drift inevitable. | Medio. Dream tier 2 cross-workspace. | A futuro, no MVP. |
| **G** | Forgetting / pruning policies activas | Letta, mem0 | Medio. Necesario a escala. | Medio. Política de compresión es UX. | A futuro. |
| **H** | Tool call history como memoria estructurada | Letta | Bajo en MVP. | Medio. | Si surge demanda específica. |
| **I** | Trust scoring por fuente | Letta | Bajo en MVP. | Bajo. | A considerar después. |
| **J** | Embedding hybrid (SPLADE / ColBERT) | IR moderno | Medio. Pero requiere modelo nuevo. | Alto. Re-indexar todo. | A futuro. |

### 14.2 Detalle por mecanismo

#### A. Reflection / Pattern detection

Además de consolidar facts (Dream actual), un proceso periódico detecta **patrones recurrentes** y emite "reflections" — meta-observaciones tipo "X tiende a Y" o "cada vez que Z ocurre, A sigue".

**Ejemplo:** Marcelo postergó 5 PRs en distintos sprints. Dream consolida cada uno como evento individual. Reflection emite: "Marcelo tiende a posponer entregas en sprints largos". Útil para predicción/planeamiento.

**Durin status:** no existe. Sería un "Dream tier 2" — cold path adicional sobre las consolidations ya hechas.

#### B. Concepts como entities mediadoras (GAAMA hypergraph)

Conceptos (`durin`, `rlhf`, `agile`, etc.) son entities de primer nivel que median retrieval. Las queries pueden ir al concepto y propagar por PPR (Personalized PageRank) a entities que lo mencionan.

**Durin status:** existe el tipo `topic` pero no se usa como mediator. El entity_ranker no propaga por concept overlap.

#### C. HyDE (Hypothetical Document Embeddings)

Alternativa al query rewriter: el LLM **imagina un documento que respondería la query** y embebe ESE documento (no la query). El embedding del doc imaginario está semánticamente más cerca del doc real que el de la query corta.

**Durin status:** descartado. Sigue siendo LLM en hot path → reproduciría la lección G3.b (§13).

#### D. Cross-encoder reranker

**Patrón estándar IR moderno.** Después del bi-encoder retrieval (query y docs embebidos por separado), un **cross-encoder** (query+doc juntos en una pasada) rerankea top-50 → top-10. Mucho más preciso que cosine similarity.

**Crítico:** NO es LLM. Es modelo dedicado tipo `cross-encoder/ms-marco-MiniLM-L-6-v2` (~80M params, latencia <100ms para top-50).

**Durin status:** no evaluado. Cubriría queries semánticamente difíciles sin LLM-en-hot-path. **Es la pieza más prometedora de toda esta lista.**

#### E. Versioning temporal explícito

Cada entity tiene historial de versiones — permite queries "¿qué sabía durin de Marcelo el 1 de enero?". Distinto a estados temporales (plan v3 §4.2): no es sobre `status` de un attribute, es sobre versión completa de la entity en tiempo T.

**Durin status:** técnicamente ya está en `memory/.git/` (cada commit de Dream es una versión). NO está expuesto al agente como mecanismo de query. Aprovechable.

#### F. Consistency checks cross-entity

Batch periódico cross-workspace detecta **inconsistencias**. Ej: `person:marcelo.spouse = susana` pero `person:susana.spouse = pedro` → flag o auto-resolver.

**Durin status:** no existe. Dream consolida per-entity, no chequea coherencia inter-entity.

#### G. Forgetting / pruning policies activas

Estrategia explícita para borrar/comprimir info vieja. Más allá de temporal decay (§10.5.1) que solo cambia ranking.

Ejemplos: "comprimir 100 episodic viejos en 1 summary cuando entity tiene > N entries", "borrar entities sin acceso en N meses".

**Durin status:** §11 (archive episodic post-consolidación) es step 1. No hay políticas de **compresión** ni **deletion** activa todavía.

#### H. Tool call history como memoria estructurada

El historial de tool calls del agente (URLs visitadas, comandos corridos, archivos leídos) es memoria de sí mismo. Sistemas tipo Letta lo estructuran como "actions log" buscable.

**Durin status:** sessions contienen tool calls pero no están estructurados como memoria buscable explícita. Solo grep.

#### I. Trust scoring por fuente

Memorias del usuario pesan más que memorias inferidas por LLM. Memorias verificadas (preguntadas y confirmadas) pesan más que aspiradas de prosa casual.

**Durin status:** no existe. Todas las memorias rankean igual.

#### J. Embedding hybrid (SPLADE / ColBERT)

SPLADE = sparse + dense hybrid embeddings. ColBERT = late interaction (multi-vector por documento, max-sim al query). En benchmarks IR superan tanto a vectores puros como a BM25 puro.

**Durin status:** no evaluado. Sería alternativa al combo LanceDB + FTS5.

### 14.3 Síntesis

**El gap más impactante a evaluar pronto:** **(D) Cross-encoder reranker.** Es la pieza estándar IR que llena el agujero "vector retrieval imperfecto" sin LLM-en-hot-path, sin parches, alineada con la filosofía actual (cold path para LLM, hot path determinístico).

**Gaps importantes para "memoria que envejece bien":** (A) Reflections, (F) Consistency checks, (G) Forgetting policies. No MVP, pero deberían quedar documentados como features esperables a largo plazo.

**Gaps descartados explícitamente:** (C) HyDE — repetiría G3.b.

**Gaps que podemos aprovechar sin trabajo nuevo:** (E) Versioning vía git history.

---

## 15. Qué falta entender / decidir

Cosas que dejo abiertas para discusión, no para resolver ahora:

1. **¿Las fuentes (sessions/ingested/corpus) tienen "identidad" como entity?** Ej: ¿`session:abc123` es un nodo del grafo o solo una referencia textual? Yo lo veo como URI referenciable pero sin entity page propia (la session es self-describing por su contenido).

2. **¿Los documentos largos en ingested merecen entities derivadas?** Ej: paper extenso → ¿se crea `paper:gpt4_tech` como entity con attributes (autor, año, abstract) y relations (introduces concept:X)? Probablemente sí, pero es Dream-driven, no obligatorio.

3. **¿Resúmenes con links internos en corpus largos?** Tu mencionaste esto. Caso: PDF de 200 páginas, corpus lo trocea en chunks, ¿además genera un resumen-índice con links a los chunks por concepto? Es valioso pero es trabajo extra. Decisión separada del modelo entity/relations.

4. **¿Hay tipos de "fuentes" que sí merecen relations entre sí (sin pasar por entity)?** Ej: `session:foo --continued_in--> session:bar`. Es relación de continuidad, no information-bearing en el sentido de attributes. Podría vivir en metadata de la session, no como first-class relation. Sospecho que no necesitamos esto.

5. **Límites duros para evitar basura.** ¿Cap de relations por entity (ej: 50)? ¿Si Dream excede, se particiona o se resume? Esto es para evitar que una entity tipo `concept:durin` termine con 500 relations a everything.

---

## 16. Diferencias respecto al plan v3 anterior

El plan v3 (`/tmp/plan_v3.md`) tenía esto correcto implícitamente pero no lo articulaba bien. Cambios a aplicar cuando volvamos al plan:

- **Aclarar en §1.1** que `relations` son information-bearing only.
- **Aclarar que sessions/ingested/corpus son targets posibles** de relations pero no emiten.
- **Provenance es el mecanismo para "creación/actualización"**, NO relations. Cubre menciones que aportaron data nueva.
- **Menciones puras se resuelven via vector search dinámico**, no se materializan.
- **Considerar cap duro de relations por entity** (Riesgo nuevo en §6).
