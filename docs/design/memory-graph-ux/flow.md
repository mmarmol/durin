---
title: Memoria webui — interaction model (flow spec)
status: implemented (verified live)
companion: ./mockup.html
---

# Memoria — modelo de interacción

> Validado sobre `mockup.html` e **implementado** en `MemoryGraphView.tsx`,
> verificado en vivo contra el binario real. Estado por caso:
> **Caso 0** (layout re-encaja) ✓ · **Caso 1** (click foco-en-lugar, ego solo
> off-cap, sin doble-click) ✓ · **Caso 2** (search recede + lista+lectura) ✓ ·
> **Caso 4** (mobile sin grafo → lista de entidades) ✓ · **Estados**
> (vacío/loading/error+reintentar/sin-resultados) ✓.

## Diagnóstico del caos actual (qué romper)

El código actual mezcla **3 ejes ortogonales** en los mismos gestos, y abre
contenido en **paneles distintos según el tipo**:

| Eje | Qué controla | Hoy (mal) |
|---|---|---|
| **A. Énfasis** | qué nodo/vecinos se resaltan en el grafo | lo disparan hover *y* click, y **queda pegado** |
| **B. Contenido** | abrir el item en un panel | entity → panel A; reference → panel B; search canonical → otro lugar |
| **C. Alcance** | grafo global vs ego/filtrado | click carga ego y **no revierte** al cerrar |

## Principios (la regla única)

1. **Un gesto = una intención.** Hover *survey*, click *abre*, close *restaura*.
2. **Un solo panel de contenido** para TODO (entity / session / reference / entry): misma posición (derecha), mismo comportamiento.
3. **El énfasis es efímero.** Hover lo pone y lo saca; click lo mantiene mientras el panel está abierto; **close lo limpia siempre**.
4. **El alcance (C) solo cambia por excepción** (un nodo que no está en el grafo, traído por search) y **siempre se revierte al cerrar**. Nunca queda filtrado.

## Casos (uno por uno)

### Caso 0 — Layout & responsive  ⟵ *fundacional (arregla el bug de reflow)*

**Bug actual**: los paneles flotan (absolute) sobre el canvas; el canvas ocupa todo y nunca sabe que le quedó menos espacio → los nodos no se reacomodan, y search+selección+grafo se enciman.

**Modelo**: el stage es **columnas que se reparten el espacio**, no overlays.
- `[ columna grafo (flex:1) | panel de contenido (0 cerrado / ~42% abierto / 72% ancho) ]`.
- Al abrir/cerrar el panel **o al hacer resize**, el canvas **re-encaja** sus nodos al tamaño real de su columna (re-centra/escala). Los nodos siempre reacomodan.
- **Search = overlay transitorio** sobre la columna grafo; **se cierra al elegir un resultado**. Nunca hay search + contenido + grafo persistentes a la vez.
- **Responsive**: cuando la columna grafo quedaría < ~280px (ventana chica o panel "ancho"), el contenido pasa a **pantalla completa** y el grafo se oculta, con "← volver al grafo". Una superficie por vez.

Verificado en el mock: split que re-encaja (wide) + full-screen (narrow) + search que cierra al elegir.

### Caso 1 — Nodo: hover / click / close  ⟵ *validar primero*

- **Hover** sobre nodo → resalta nodo + vecinos directos (atenúa el resto, en el lugar) + popover de preview (título + snippet). Salir el mouse → todo vuelve. No abre nada, no cambia alcance.
- **Click** sobre nodo → abre su contenido en **EL panel**; el grafo mantiene el resalte nodo+vecinos en el lugar (no se reemplaza). Click en otro nodo → reemplaza foco+contenido (no acumula).
- **Close (✕)** → cierra panel **y restaura el grafo al estado previo al click** (saca resalte; si hubo ego temporal, vuelve al global). Nunca queda filtrado.
- **Click en vacío** → si hay panel abierto, cierra (=restaura); si no, limpia cualquier resalte de hover.

### Caso 2 — Search

Buscar es un **modo lista + panel de lectura** (como mail/Finder). Mientras buscás importan los resultados; el grafo **recede** detrás. La lista **persiste** mientras navegás resultados — no se pierde al elegir.

- Al tipear: lista de resultados (izquierda) + **grafo atenuado/recedido detrás** (no interactivo). Skills excluidos (`kinds=fact`), deduplicados, contador = filas mostradas.
- **Click en un resultado** (canonical / fragment / reference): abre su contenido en **EL panel** (derecha). **La lista NO se cierra** → podés clickear otro y el panel cambia. El grafo sigue recedido. Mismo panel para todos los tipos (nada de "uno arriba, otro al costado").
- **Cerrar el search** (✕ de la lista): la lista se cierra, el **grafo reaparece enfocado** en el último item que abriste (buscar → ubicar → explorar; el valor de durin). Sin item abierto → grafo completo.
- **Off-cap**: si el item es un nodo que el grafo global dejó afuera (cap 500), al volver del search se trae su **ego-grafo temporal** y un siguiente close restaura el global.

> Corrección (descartado lo anterior): "click cierra el search" hacía **perder la lista** y dejaba solo grafo+contenido. El modo lista+lectura lo arregla sin volver al 3-paneles-activos, porque el grafo está recedido durante el search.

### Caso 3 — "Grafo completo" / volver

- Cuando hay un ego-grafo temporal cargado (solo por el caso off-cap), aparece "← Grafo completo". Cerrar el panel o tocar ese botón → restaura. (Si nunca se carga ego, el botón no aparece.)

### Caso 4 — Mobile / compacto (no entra todo)

Premisa: en pantalla chica **no hay side-by-side**. Una superficie por vez + navegación con "←" (back-stack). El split es solo desktop.

**Dos tiers** (el quiebre es donde la columna grafo quedaría < ~300px ≈ ventana < ~720px):

| | Desktop (≥ ~720px) | Compacto / mobile (< ~720px) |
|---|---|---|
| Layout | split: `[grafo \| contenido]` (re-encaja) | **una superficie full-screen por vez** |
| Rail nav | visible | colapsa a hamburguesa (oculto) |
| Gesto survey | **hover** (preview) | no existe (touch) → **tap = abre** |
| Click/tap nodo | resalta + abre panel (columna) | abre contenido **full-screen** (← vuelve al grafo) |
| Search | lista (izq) + lectura (der), grafo recede | **lista full-screen**; tap → contenido full-screen (← vuelve a la lista; ← otra vez → grafo) |
| Cerrar | ✕ restaura | "←" sube un nivel en el stack |

**Back-stack en compacto**:
- Grafo → (tap nodo) → Contenido → ← → Grafo.
- Grafo → (search) → Resultados → (tap) → Contenido → ← → Resultados → ← → Grafo.

**Grafo en touch**: el force-graph es denso para dedos. Mobile **arranca pero apoya en search** (la lista full-screen es el camino principal); el grafo queda como exploración secundaria con pinch-zoom y targets más grandes. (A validar: ¿en mobile el landing debería ser search en vez del grafo? — decisión abierta mobile #1).

## Estados del panel (uno solo)

| Estado | Cuándo | Qué muestra |
|---|---|---|
| cerrado | default | nada (grafo limpio) |
| abierto-compacto | tras click/seleccionar | header (tipo+título+✕+⤢) · tabs · contenido (Contenido por defecto) |
| abierto-ancho | botón ⤢ | igual, ancho completo para lectura |
| reference | item es reference | header (reference) · contenido renderizado (sin tabs de entity) |

## Decisiones (resueltas)

1. **Click NO centra (sin cámara/pan-zoom).** El grafo re-encaja en su columna y el nodo queda resaltado+agrandado; eso alcanza. El centrado real (cámara) es infra grande y el ego-grafo ya centra el caso off-cap. → **solo resalta en el lugar**.
2. **Ego temporal SOLO off-cap.** Click normal resalta en el lugar (mantiene contexto global). El ego-reemplazo solo cuando el nodo no está en el grafo (search off-cap), y se revierte al cerrar. (Evita "queda filtrado").
3. **Doble-click ELIMINADO.** Click abre; el ⤢ del panel da ancho completo. El doble-click solo agregaba ambigüedad.
4. **Hover IGNORADO con panel abierto.** No pelea con la selección/contenido. En touch no hay hover (ver mobile).
5. **Ancho del panel: 42%** por defecto en desktop, ⤢ → 72%. (Resizable por drag: futuro, no ahora).
6. **Umbral a stacked: columna de grafo < ~300px** (≈ ventana < ~720px) → se pasa al modo compacto/mobile (ver abajo).

## Cómo evaluar el mock

`docs/design/memory-graph-ux/mockup.html` — abrilo en el navegador (doble click al archivo, o `file://…`). Probá: hover sobre nodos, click (abre panel + foco), ✕ (restaura), search "vacuna" → click en canonical y en reference (mismo panel), ⤢ (ancho).
