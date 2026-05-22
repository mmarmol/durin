# 13 · Render de tools interactivas (ask_user, request_secret)

## Problema

`exec` tiene un render propio en TUI y web (`$ comando` + salida). Las
tools que **interactúan con el humano** —`ask_user_question` y
`request_secret`— no: caen en el render genérico, que vuelca el texto
crudo del tool result, incluyendo la instrucción interna
`YIELD TO USER. Present this exact question...`. El usuario ve ruido de
prompt en vez de una pregunta legible o una petición de credencial
clara.

## Estado actual

Las dos tools son **"yield tools"**: no bloquean esperando respuesta.

- `ask_user_question(question, options?)` — registra la pregunta en
  `session.metadata['pending_question']`, devuelve un mensaje YIELD. El
  modelo presenta la pregunta como su mensaje; la respuesta del usuario
  llega como el siguiente mensaje normal. No hay round-trip al tool.
- `request_secret(name, service, purpose?)` — devuelve un YIELD con el
  comando `durin secret set ...`. El usuario lo corre en su terminal;
  el valor nunca pasa por el chat ni por el agente.

Render: `tool_call_bubble.py` (TUI) y `ToolCallBlock.tsx` (web)
ramifican por `name` — `edit_file`, `exec`, `read_file/list_dir/grep`,
y un genérico. Ninguna rama cubre las dos tools interactivas. No existe
plumbing para mandar una respuesta de la UI de vuelta a un tool call en
curso.

## Cómo lo hacen otros

| Cliente | Preguntas | Secret/credencial | Forma |
|---|---|---|---|
| Claude Code | `AskUserQuestion`: picker de opciones + "Other" libre | prompt de permiso aparte | bloque interactivo, envía al elegir |
| Hermes | `clarify.request`: lista numerada + texto libre | `secret.request`: input enmascarado (`*`) | overlay inline al pie del transcript, RPC `clarify.respond` / `secret.respond` |
| Openclaw | — (no tiene) | — (no tiene) | sólo aprobación de exec en modal |

Patrones a copiar: lista numerada con atajo de teclado (Hermes),
input enmascarado para secretos (Hermes), opción "Other" libre
(Claude). Lo interactivo va **inline**, no en modal, y bloquea el input
hasta resolverse.

## Diseño

Dos fases. La Fase 1 entrega paridad con `exec` (un render propio,
legible); la Fase 2 lo vuelve interactivo de verdad.

### Fase 1 — render legible propio (este commit)

Render por-tool en ambas superficies, reconstruido **desde los
argumentos**, ignorando el ruido del YIELD en el result:

- `ask_user_question` → `❓ <pregunta>` + opciones numeradas.
- `request_secret` → `🔑 <NAME> · service` + propósito + el comando
  `durin secret set NAME --service SERVICE --scope exec` (copiable).
  Si el secret ya existía, lo dice en vez del comando.

Además, en web el `TraceGroup` (colapsado por defecto) se abre solo
cuando contiene una tool interactiva: la pregunta o la petición de
credencial no deben quedar enterradas en "🔧 1 tool".

### Fase 2 — `ask_user_question` interactivo (hecho)

Cada opción sugerida es **editable**: se selecciona y se puede ajustar
o reemplazar antes de mandar (idea del usuario; patrón Hermes/Claude).

- **Web** — `ToolCallBlock` renderiza el panel `AskUserAnswer`: la
  pregunta, las opciones como chips, y un campo editable. Click en un
  chip → carga esa opción en el campo (editable); el campo acepta texto
  libre para una respuesta "otra". Enviar va por `ThreadActions`, un
  context que expone `sendUserMessage` — evita drillear `chatId` por
  `ThreadShell → viewport → list → bubble`.
- **TUI** — `ToolCallBubble` renderiza las opciones como filas
  clickeables; click carga la opción en el `InputArea`, editable, y le
  da foco. El usuario ajusta y manda con ⏎.

### Fase 2b — `request_secret` interactivo (propuesto)

- Input enmascarado + "Guardar"; en web llama a `setSecret`
  (`/api/secrets/set`, ya existe) con `scope=['exec']`; en TUI un modal
  de input enmascarado que escribe en `SecretStore`. Tras guardar, hint
  "ya podés reintentar". El valor nunca toca el chat.

Nada de esto requiere el round-trip con `Future` (la "V2" del docstring
de `ask_user`): el modelo de durin —"la respuesta es el próximo
mensaje"— alcanza, y guardar el secret es un side-effect fuera de la
conversación.
