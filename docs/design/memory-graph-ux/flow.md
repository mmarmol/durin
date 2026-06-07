---
title: Memoria webui — interaction model (flow spec)
status: draft for validation
companion: ./mockup.html
---

# Memoria — modelo de interacción

> Doc de trabajo. Lo validamos sobre el `mockup.html` (autónomo, abrible en el
> navegador). NO se implementa hasta confirmar. Vamos **de a un caso por vez**.

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

### Caso 1 — Nodo: hover / click / close  ⟵ *validar primero*

- **Hover** sobre nodo → resalta nodo + vecinos directos (atenúa el resto, en el lugar) + popover de preview (título + snippet). Salir el mouse → todo vuelve. No abre nada, no cambia alcance.
- **Click** sobre nodo → abre su contenido en **EL panel**; el grafo mantiene el resalte nodo+vecinos en el lugar (no se reemplaza). Click en otro nodo → reemplaza foco+contenido (no acumula).
- **Close (✕)** → cierra panel **y restaura el grafo al estado previo al click** (saca resalte; si hubo ego temporal, vuelve al global). Nunca queda filtrado.
- **Click en vacío** → si hay panel abierto, cierra (=restaura); si no, limpia cualquier resalte de hover.

### Caso 2 — Search

- Resultados en panel izquierdo (lista). Skills excluidos (`kinds=fact`), deduplicados, contador = filas mostradas.
- **Click en cualquier resultado** (canonical / fragment / reference) → abre en **EL MISMO panel** derecho, misma posición y comportamiento que un click de nodo. Nada de "uno arriba, otro al costado".
  - canonical (entity) → foco del nodo + contenido.
  - reference → contenido del doc (no es nodo → sin foco de grafo, panel igual).
  - fragment/entry → foco de la entidad que etiqueta + contenido.
- **Off-cap**: si el resultado es un nodo que el grafo global dejó afuera (cap 500), al abrirlo se trae su **ego-grafo temporal** (excepción del eje C) y **close restaura el grafo global**.

### Caso 3 — "Grafo completo" / volver

- Cuando hay un ego-grafo temporal cargado (solo por el caso off-cap), aparece "← Grafo completo". Cerrar el panel o tocar ese botón → restaura. (Si nunca se carga ego, el botón no aparece.)

## Estados del panel (uno solo)

| Estado | Cuándo | Qué muestra |
|---|---|---|
| cerrado | default | nada (grafo limpio) |
| abierto-compacto | tras click/seleccionar | header (tipo+título+✕+⤢) · tabs · contenido (Contenido por defecto) |
| abierto-ancho | botón ⤢ | igual, ancho completo para lectura |
| reference | item es reference | header (reference) · contenido renderizado (sin tabs de entity) |

## Decisiones abiertas (a confirmar en el mock)

1. **¿Click centra el nodo** (pan/zoom) además de resaltar, o solo resalta en el lugar? (el mock hoy solo resalta — más simple, sin cámara).
2. **Ego temporal**: ¿solo para off-cap (recomendado), o querés que cualquier click reemplace por ego? (recomiendo solo off-cap para no perder el contexto global).
3. **Doble-click**: ¿lo eliminamos? (con click=abre, el doble-click sobra; el ⤢ del panel da el ancho completo).
4. **Hover mientras hay panel abierto**: ¿se ignora (recomendado, no pelea con la selección) o también previsualiza?

## Cómo evaluar el mock

`docs/design/memory-graph-ux/mockup.html` — abrilo en el navegador (doble click al archivo, o `file://…`). Probá: hover sobre nodos, click (abre panel + foco), ✕ (restaura), search "vacuna" → click en canonical y en reference (mismo panel), ⤢ (ancho).
