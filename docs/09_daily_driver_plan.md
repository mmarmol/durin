# Daily Driver Plan — CLI & Memory Surface

> Pausa explícita sobre Phase 3 (dream cron) para invertir primero en que
> durin sea usable como daily driver. Sin sesiones navegables, drag-and-drop
> de archivos, footer persistente y una superficie CLI para inspeccionar y
> editar memoria, el agente es difícil de adoptar para uso continuo — y
> sin uso continuo la decisión sobre dream queda sin datos reales.

---

## 0. Context

Después de Phase 1 (memoria foundation) y Phase 2 (LanceDB vector retrieval),
las capacidades del agente subieron. La superficie de control no.
`durin/cli/` hoy expone `/new`, `/stop`, `/status`, `/model`, `/history`,
`/goal`, `/dream*`, `/plan`, `/build`, `/mode` — pero no `/resume`,
`/compact`, `/copy`, `/sessions`, ni nada de memoria. El editor es
prompt_toolkit básico (sin `@file`, sin drag-and-drop, sin footer
persistente). Eso lo hace incómodo para uso diario.

**Inspiración**: [pi.dev](https://pi.dev) y
[earendil-works/pi](https://github.com/earendil-works/pi). Pi es un
TypeScript/Node coding agent con UI minimal-but-rich (header / mensajes /
editor / footer persistente) más sesiones como árboles. Opencode aporta
el patrón de drag-and-drop de archivos.

**Scope explícitamente fuera**: branching de sesiones (`/tree`, `/fork`,
`/clone`). Pi guarda sesiones como árboles con `parentId` por mensaje;
durin las guarda como `.jsonl` flat. El refactor es ~2 semanas y la
mayoría del valor diario lo dan D1+D2+D3.

---

## 1. Phases

### D1 — Session ergonomía + input ergonomía (1 semana)

Lo mínimo para que el daily driver no duela en operación normal.

| # | Item | Notas |
|---|---|---|
| D1.1 | `/sessions` + `/resume` | Selector interactivo de sesiones existentes. Read del directorio `sessions/`, ordenar por updated_at desc, fuzzy filter. |
| D1.2 | `/compact [hint]` | Manual compaction. Hoy se dispara solo por threshold; expose explicit user control. Hint opcional pasa como prefix al consolidator prompt. |
| D1.3 | `/copy` | Última respuesta del agente al clipboard (pyperclip o `pbcopy`/`xclip` shell-out). |
| D1.4 | `/name <name>` | Display name persistente en `session.metadata`. Aparece en footer y en `/sessions` list. |
| D1.5 | `/hotkeys`, `/quit` aliases | Discoverability. `/hotkeys` lista las teclas disponibles; `/quit` alias de `:q`/`exit`. |
| D1.6 | **Footer persistente** | ANSI hybrid: save/restore cursor + clear-line en la última fila. Updates: `sesión · tokens-usados/budget · cost · ctx % · modelo · mem N entries vec✓/✗`. Refresh on each turn + post-tool. |
| D1.7 | Ctrl+L model picker | Interactive selector sobre `model_presets` del config. |
| D1.8 | Drag-and-drop de archivos | Pre-process del input: detectar paths absolutos en el text. Por mimetype: imagen → copy a `<workspace>/.media/<sha>.<ext>` + add a `media[]` + reemplazar en texto por `[Image: filename]`. Audio → mismo pattern con aux audio model. PDF / markdown / texto → ofrecer "ingest a memory? (y/N)" o forward directo según contexto. |
| D1.9 | `@archivo` fuzzy completion | prompt_toolkit `Completer` que walka cwd y propone files por prefijo. |

**Output entregable**: agente usable como daily driver con sesiones
navegables, archivos arrastrables, footer informativo continuo.

### D2 — Memory surface (1 semana)

Phase 1+2 entregaron 4 tools pero **cero comandos CLI** que los invoquen
directamente. El usuario quiere ver, editar y forget memoria sin tener
que pedirle al agente.

| # | Item | Notas |
|---|---|---|
| D2.1 | `/memory list [class]` | Lista entries de `memory/<class>/*.md` con headline + author + valid_from. Sin class → todas. |
| D2.2 | `/memory show <id>` | Read del archivo .md. Renderiza frontmatter + body con markdown. |
| D2.3 | `/memory search <query>` | Wrap del tool `memory_search`. Muestra strategy (vector/grep/hybrid) + top-K headlines + URIs. |
| D2.4 | `/memory drill <uri>` | Wrap del tool `memory_drill`. Render la sección apuntada. |
| D2.5 | `/remember <fact>` | `memory_store` con `author=user_authored`. Curator/dream NO van a tocar este entry. |
| D2.6 | `/forget <id>` | Delete del .md + del row del vector index. Confirm prompt. |
| D2.7 | `/sources` + `/sources ingest <path>` | Lista `ingested/<id>/` con summary + size. Subcomando `ingest` wrappea `memory_ingest`. |
| D2.8 | `/audit` | Único de durin. Vista de "qué cree el agente sobre mí": top headlines de `memory/stable/` con valid_from + conteo de source_refs. Edit / delete inline. |
| D2.9 | `/why <claim>` | Search memoria por claim + render `source_refs` como links navegables al turn/sección que produjo cada conclusión. Demuestra la cadena de provenance que Phase 1 construyó. |

**Output entregable**: el usuario puede operar sobre la memoria del
agente sin pedirle nada — listar, ver, buscar, recordar, olvidar,
auditar, y rastrear procedencia.

### D6 — Lifecycle commands (install / configure / upgrade / uninstall)

Sin estos comandos el agente no era operable como producto. El usuario que
clona el repo no tenía cómo modificar una clave sin editar JSON a mano,
no había vía explícita para subir de versión, y un `pip uninstall` dejaba
huérfano `~/.durin/` + `~/.cache/durin/`.

| # | Item | Notas |
|---|---|---|
| D6.1 | `README.md` + `docs/INSTALL.md` | Prerequisitos, comando exacto, qué crea `onboard`, dónde vive el estado, extras opcionales. |
| D6.2 | `durin config path \| show \| get \| set \| edit` | Dotted paths con normalización snake_case → camelCase. Secretos enmascarados por defecto; `--raw` opt-in. Validación contra el schema antes de escribir. |
| D6.3 | `durin upgrade [--check\|--migrate-only\|--ref]` | Detecta editable vs wheel (busca `pyproject.toml` junto al paquete cargado). Editable: `git pull --ff-only` + `pip install -e .`. Wheel: `pip install --upgrade durin`. Siempre replay del migrate. |
| D6.4 | `durin uninstall [--purge --keep-config --keep-workspace --keep-cache --workspace]` | Enumera + tabula paths + bytes antes de borrar. `--purge` lanza `pip uninstall` en subproceso para no pisarse a sí mismo. Per-workspace `<ws>/.durin/` solo opt-in. |

**Output entregable**: durin es desinstalable, actualizable y editable
sin abrir nano. El operador ve exactamente qué va a borrarse antes de
consentir.

### D7 — Doctor

`durin doctor` ejecuta una batería de checks pequeños (system / config /
providers / tools / extras / state), agrupa por categoría, sugiere fixes
puntuales y devuelve exit code 0 salvo que algún check sea `fail`. Se
puede integrar en CI con `--json`.

| # | Item | Notas |
|---|---|---|
| D7.1 | `CheckResult` framework | Cada check devuelve `CheckResult(name, status, message, fix?, category)`. Status ∈ ok/warn/fail. Función pura, fácil de testear. |
| D7.2 | Checks de sistema y config | Python ≥ 3.11; durin version; config exists/parses/validates; workspace writable; ~/.durin + ~/.cache/durin writable. |
| D7.3 | Checks de provider | Al menos uno usable (api_key, OAuth token, o `api_base` local); preset.model resolvible. |
| D7.4 | Checks de tools y extras | `git` presente (warn si falta); `fastembed/lancedb/mcp` importables (warn con `pip install 'durin[memory]'`). |
| D7.5 | Cache size + `--ping` opt-in | Warn si `~/.cache/durin > 10 GB`; `--ping` testea reachability del `api_base` del provider activo. |
| D7.6 | `--fix` seguro | Crea workspace si falta; replay del migrate. Nada destructivo ni que involucre claves. |
| D7.7 | `--json` para CI | Output machine-readable; exit code refleja el peor status. |

**Output entregable**: cuando algo no anda, una sola línea (`durin
doctor`) le dice al operador qué está roto y cómo arreglarlo, en vez
de hacerle leer logs.

### D3 — Editor avanzado (1 semana, opcional según presión)

Patterns de pi que el editor actual de prompt_toolkit no expone.

| # | Item | Notas |
|---|---|---|
| D3.1 | Shift+Enter multi-línea | prompt_toolkit `key_bindings` para `c-j` (LF) que inserta newline; Enter sigue submitting. |
| D3.2 | `!cmd` ↔ `!!cmd` | Si la línea empieza con `!`: correr en subprocess. `!cmd` manda output al LLM como contexto user; `!!cmd` solo lo corre. |
| D3.3 | Message queue | Enter durante un turn en curso = "steering" (entregado después del turn). Alt+Enter = "follow-up" (entregado cuando termina todo el agent work). Settings configurables: `one-at-a-time` (default) vs `all`. |
| D3.4 | Esc abort | Cancela el turno en curso. Esc Esc = abort + clear queue. |

**Output entregable**: editor con la ergonomía de pi sin migrar a
Textual. Si esto todavía no alcanza, D5 (Textual migration) se evalúa
post-uso.

---

## 2. Out-of-scope para daily driver

| Item | Por qué |
|---|---|
| **D4 — Branching de sesiones** (`/tree`, `/fork`, `/clone`) | Refactor del session model: `parent_id` por mensaje, múltiples branches en un archivo, navegador interactivo. ~1-2 semanas. **No es daily-driver crítico** — la mayoría del valor lo dan D1+D2+D3. Re-evaluar cuando llevemos semanas usando el resto. |
| Migración a [Textual](https://textual.textualize.io/) | Full TUI framework. ~3-4 semanas de rewrite del render loop. **No empezar hasta que el footer hybrid + drag-and-drop NO alcancen**. |
| `/export` HTML, `/share` gists | Nice-to-have. No daily-driver crítico. |
| Ctrl+V paste de imágenes | Drag-and-drop cubre el 95% del caso. |
| OAuth `/login` | durin ya tiene config-based auth. |
| Phase 3 (dream cron + KG + freshness trends) | **Pausado hasta tener datos reales de uso**. Sin daily-driver, no hay datos. Sin datos, dream se diseña a ciegas. |

---

## 3. Order of execution

1. **D1** (1 semana, 9 sub-tasks): un branch único `daily-driver-d1` con
   commits granulares + un solo PR a `main` cuando esté completo. Igual al
   patrón Phase 1 / Phase 2.
2. **D2** (1 semana, 9 sub-tasks): branch `daily-driver-d2`. Empieza
   cuando D1 está mergeado.
3. **D3** (1 semana, opcional): branch `daily-driver-d3` solo si el
   editor empieza a doler durante el uso real de D1+D2.

**Después de D1+D2 (y opcionalmente D3)**: regreso a Phase 3 (dream) con
datos reales de uso recolectados via la telemetría que ya está en
producción (`memory.recall`, `memory.recall.vector`,
`memory.embedding.{load,embed}`).

---

## 4. Riesgos

| Riesgo | Mitigación |
|---|---|
| Footer ANSI rompe terminales raros | Detect ANSI support via `sys.stdout.isatty()` + `TERM` env. Fallback a no-footer en non-TTY. |
| Drag-and-drop paths con espacios / unicode | Normalizar via `Path(...).expanduser().resolve()`. Test explícito con espacios y emojis en filename. |
| `/forget <id>` accidental | Confirm prompt obligatorio. `--force` opcional para automation. |
| `/remember` colisiona con `memory_store` del agente | Distinto `author` (user_authored vs agent_created) ya separa. No hay colisión real. |
| Modelo del footer queda stale después de `/model X` | Footer escucha eventos del runner; refresh on `model.switched`. |

---

## 5. ARCHITECTURE.md updates

Cada phase actualiza `docs/ARCHITECTURE.md`:

- D1 → nueva sección sobre footer persistente + drag-and-drop processor + lista de slash commands.
- D2 → extender §8 (Memory Subsystem) con la CLI surface + provenance commands.
- D3 → nota sobre message queue + editor extensions.

---

## Last updated: 2026-05-20
