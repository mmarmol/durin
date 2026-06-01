# Referencia de código — Skills en Hermes (`~/git_personal/hermes-agent`)

> **Fuente de consulta**, no propuesta. Investigación línea-por-línea del código
> real de Hermes sobre cómo usa, busca, crea y evoluciona skills, para
> consultar al implementar el sistema de durin (ver `skills_evolutivas.md`).
> Las citas `path:línea` son contra el árbol local en
> `/Users/marcelo/git_personal/hermes-agent`. Verificadas por lectura directa
> el 2026-06-01. Donde el código **contradice** el marketing/docs de Hermes,
> se marca explícitamente — es lo más valioso de este doc.

---

## §0 — Mapa de archivos

| Archivo | Rol |
|---|---|
| `agent/conversation_loop.py` | Trigger de review por-turno (creación/evolución) |
| `agent/background_review.py` | Fork de auto-mejora (escribe/parchea SKILL.md) |
| `agent/curator.py` | Consolidación periódica + state machine de lifecycle |
| `agent/agent_init.py` | Defaults de config (intervalos de nudge) |
| `agent/prompt_builder.py` | Índice de skills inyectado al system prompt (progressive disclosure) |
| `agent/system_prompt.py` | Call-site de inyección |
| `agent/skill_utils.py` | Parser de frontmatter, condiciones, storage dir |
| `agent/skill_preprocessing.py` / `skill_bundles.py` / `skill_commands.py` | Render de plantillas, bundles, slash-load |
| `tools/skills_tool.py` | Tools `skills_list` / `skill_view` (progressive disclosure) |
| `tools/skill_manager_tool.py` | Tool `skill_manage` (create/patch/write_file/delete) + validación |
| `tools/skill_usage.py` | Telemetría de uso + estados de lifecycle (sidecar JSON) |
| `tools/skill_provenance.py` | `ContextVar` de origen de escritura (fork vs usuario) — **no** es provenance de import |
| `tools/skills_hub.py` | Marketplace: source adapters, install, `lock.json` (provenance real de import) |
| `tools/skills_sync.py` | Sync de skills *bundled* con el repo (baseline por hash) |
| `tools/skills_guard.py` | Scanner estático de seguridad + matriz de política de install |
| `hermes_cli/skills_hub.py` | Gate real de install (quarantine → scan → policy → confirm) |
| `scripts/build_skills_index.py` | Índice de marketplace (JSON estático, **install-time, no runtime**) |
| `optional-skills/research/darwinian-evolver/` | Optimizador GEPA-style (prompts/regex/SQL/code) — **NO evoluciona skills** |

---

## §1 — Creación / cristalización (desde experiencia)

**No hay módulo "cristalizador" determinista. El autor es el LLM**, en un fork de
fondo. No se ensambla la SKILL.md desde una plantilla: el modelo escribe el
texto completo.

### Trigger real (≠ "≥5 tool calls")

El gate duro es un contador de **iteraciones de tool por turno** que alcanza
`_skill_nudge_interval`, **default 10** (no 5). El "5+" del marketing existe
solo como prosa no-vinculante en la descripción del tool.

`agent/agent_init.py:1067`
```python
    # Skills config: nudge interval for skill creation reminders
    agent._skill_nudge_interval = 10
    ...
    agent._skill_nudge_interval = int(skills_config.get("creation_nudge_interval", 10))
```

`agent/conversation_loop.py:4045`
```python
    # Check skill trigger NOW — based on how many tool iterations THIS turn used.
    _should_review_skills = False
    if (agent._skill_nudge_interval > 0
            and agent._iters_since_skill >= agent._skill_nudge_interval
            and "skill_manage" in agent.valid_tool_names):
        _should_review_skills = True
        agent._iters_since_skill = 0
```

El "5+" es solo guía para el modelo — `tools/skill_manager_tool.py:814`:
```python
"Create when: complex task succeeded (5+ calls), errors overcome, "
"user-corrected approach worked, non-trivial workflow discovered, ..."
```

### Es background, fuera del path crítico

El review se dispara **después** de entregar la respuesta, en daemon thread —
nunca compite con la tarea del usuario. `agent/conversation_loop.py:4060`:
```python
    # Background memory/skill review — runs AFTER the response is delivered
    if final_response and not interrupted and (_should_review_memory or _should_review_skills):
        agent._spawn_background_review(messages_snapshot=list(messages),
            review_memory=_should_review_memory, review_skills=_should_review_skills)
```

El fork hereda el prompt cacheado del padre, `max_iterations=16`, y queda
**restringido a tools de memoria/skills** — `agent/background_review.py:448`:
```python
            review_whitelist = { t["function"]["name"]
                for t in get_tool_definitions(enabled_toolsets=["memory", "skills"], quiet_mode=True) }
            set_thread_tool_whitelist(review_whitelist, ...)
```

### El prompt sesga a SIEMPRE produces algo (clave de comportamiento)

`agent/background_review.py:45` (`_SKILL_REVIEW_PROMPT`):
```python
    "Review the conversation above and update the skill library. Be "
    "ACTIVE — most sessions produce at least one skill update, even if "
    "small. A pass that does nothing is a missed learning opportunity, "
    "not a neutral outcome.\n\n"
```
Prefiere **parchear** lo existente (pasos 1–3) y solo crea umbrella nueva como
último recurso (paso 4), con guardas anti-nombres-de-sesión. Una corrección del
usuario es "FIRST-CLASS skill signal" → codificar como pitfall.

### Qué valida el código al crear (casi nada)

Solo `name` + `description` + body no vacío. Procedure/pitfalls/verification son
**prosa sugerida, no validada**. `tools/skill_manager_tool.py:242`:
```python
    if "name" not in parsed: return "Frontmatter must include 'name' field."
    if "description" not in parsed: return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > MAX_DESCRIPTION_LENGTH: ...
    body = content[end_match.end() + 3:].strip()
    if not body: return "SKILL.md must have content after the frontmatter ..."
```
(`MAX_DESCRIPTION_LENGTH=1024`, `MAX_NAME_LENGTH=64`, `MAX_SKILL_CONTENT_CHARS=100_000`.)

### Provenance de autoría (fork vs usuario)

Solo lo creado por el fork queda "agent-created" (curator-managed).
`tools/skill_manager_tool.py:780`:
```python
            if action == "create":
                if is_background_review():
                    mark_agent_created(name)
```
Vía `ContextVar` en `tools/skill_provenance.py:75`. Skills creadas en foreground
por el usuario nunca se auto-tocan.

> **→ durin:** el patrón "el LLM ES el autor, en fork sandboxed a tools de
> memoria/skill, post-respuesta" es casi idéntico a tu Dream. Diferencia: el
> trigger de Hermes es **complejidad-por-turno** (≥10 iters), no frecuencia.

---

## §2 — Búsqueda / retrieval / inyección

**Hallazgo que CONTRADICE lo que asumimos:** Hermes **no** usa embeddings, FTS5,
BM25 ni ranking de relevancia en runtime para skills. Usa **progressive
disclosure dirigido por el modelo**: inyecta un índice compacto (name +
description) de **TODAS** las skills en el system prompt cada turno, y el modelo
decide cuáles cargar llamando `skill_view`. (El único índice con backend de
búsqueda — `build_skills_index.py` — es para el **marketplace al instalar**, no
para el contexto.)

### El índice runtime = scan de filesystem, no DB

`agent/prompt_builder.py:1072` (cold path: scanea `~/.hermes/skills/**/SKILL.md`,
parsea frontmatter, agrupa por categoría, cachea snapshot en disco). Verificado:
cero ocurrencias de `embedding|fts5|bm25|vector` en los paths de skills.

### Se inyecta SIEMPRE el índice de todas las skills

`agent/system_prompt.py:169`:
```python
    has_skills_tools = any(name in agent.valid_tool_names for name in ['skills_list','skill_view','skill_manage'])
    if has_skills_tools:
        skills_prompt = _r.build_skills_system_prompt(...)
    ...
    if skills_prompt:
        stable_parts.append(skills_prompt)   # tier estable (cacheable)
```
El gate es "¿están los tools de skills presentes?", **no** relevancia a la tarea.

### El modelo se auto-selecciona (no hay clasificador)

`agent/prompt_builder.py:1188`:
```python
        "## Skills (mandatory)\n"
        "Before replying, scan the skills below. If a skill matches or is even partially relevant "
        "to your task, you MUST load it with skill_view(name) and follow its instructions. ..."
```
El único filtro programático es **estructural** (no de relevancia): ocultar
skills por tool/toolset disponible, OS, o disabled-list — `prompt_builder.py:961`
(`requires_tools` / `fallback_for_tools`).

### Cuerpo completo = on-demand

Entra al contexto solo al llamar `skill_view(name)` (`tools/skills_tool.py`) o
por slash-command `/skill` / `/bundle`. Palancas reales de costo de tokens:
metadata-only siempre + cuerpos on-demand + truncado de description a 1024 +
lectura de solo primeros 4000 bytes por SKILL.md al indexar
(`tools/skills_tool.py:583`).

> **→ durin:** lo que el usuario creía "sistema de búsqueda de skills" de Hermes
> es en realidad **progressive disclosure**, no retrieval por relevancia. Su
> punto débil declarado: el **metadata de TODAS las skills se inyecta cada
> turno** — no escala a catálogos grandes. Nuestra idea de retrieval híbrido
> (vector/FTS sobre corpus de skills, plan §5.5) sería **más avanzada** que
> Hermes, no una copia. Decisión real: progressive-disclosure (simple, lo que
> funciona hoy en Hermes) vs retrieval (escala mejor, más complejo).

---

## §3 — Evolución / lifecycle

**Dos sistemas separados y NO conectados:** (1) lifecycle real (curator +
background-review + telemetría de uso); (2) `darwinian-evolver`, optimizador
GEPA-style para prompts/regex/SQL/code que **no** toca skills (verificado: cero
referencias cruzadas).

### State machine = 3 constantes string + `pinned` bool (no Enum)

`tools/skill_usage.py:52`:
```python
STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
_VALID_STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}
```
Transiciones por timestamp (no LLM): `active→stale` (idle >30d), `stale→active`
(usada de nuevo), `active|stale→archived` (idle >90d, mueve dir a `.archive/`),
`archived→active` solo manual. **Nunca borra** — archive es el máximo destructivo.
`agent/curator.py:284`:
```python
        if anchor <= archive_cutoff and current != _u.STATE_ARCHIVED:
            ok, _msg = _u.archive_skill(name)
        elif anchor <= stale_cutoff and current == _u.STATE_ACTIVE:
            _u.set_state(name, _u.STATE_STALE)
        elif anchor > stale_cutoff and current == _u.STATE_STALE:
            _u.set_state(name, _u.STATE_ACTIVE)   # reactivar
```
(`DEFAULT_STALE_AFTER_DAYS=30`, `DEFAULT_ARCHIVE_AFTER_DAYS=90`, `curator.py:58`.)
No hay promote/demote-as-state. `pinned` exime de todas las transiciones.

### Auto-mejora en uso = el mismo fork de §1

El fork de background-review parchea/crea SKILL.md vía `skill_manage`
(`action=patch|write_file|create`). Es **event-driven por turno, no cron**.
Telemetría de uso en sidecar `~/.hermes/skills/.usage.json` (`use_count`,
`view_count`, `patch_count` + timestamps), atomic write + file lock.

### Uso NO es un "score" de evolución (deliberado)

Los timestamps de uso manejan stale/archive, pero los **counts están prohibidos**
de decidir consolidación — `agent/curator.py:351`:
```
"4. DO NOT use usage counters as a reason to skip consolidation. The counters
are new and often mostly zero. Judge overlap on CONTENT, not on use_count.
'use=0' is not evidence a skill is valuable…"
```

### Curator = consolidación periódica (inactivity-triggered, ~7d)

`should_run_now()` gatea por `enabled`, `not paused`, `last_run_at` > `interval_hours`
(default 7 días). Al correr: (1) transiciones automáticas, (2) fork LLM con
`CURATOR_REVIEW_PROMPT` para fusionar skills narrow en umbrellas / archivar
hermanas, (3) `REPORT.md` por corrida. Tiene `dry_run`.

### `darwinian-evolver` (el GEPA-style de Hermes, aparte)

Wrapper sobre el `darwinian_evolver` de Imbue (AGPL, vía subprocess). Evoluciona
un **prompt/regex/SQL/snippet** contra una fitness function — tres componentes:
- **Organism** (Pydantic con `run()`), **Evaluator** (score `[0,1]` + casos
  `trainable` visibles al mutator vs `holdout` para detectar overfitting),
  **Mutator** (LLM reflexiona sobre un failure y propone mejora).
- Loop poblacional con selección por percentil de score (`midpoint_score=p75`,
  `novelty_weight`, snapshots pickle + JSONL por iteración).
- Aislado por AGPL: "Never `from darwinian_evolver import …` inside Hermes core".

> **→ durin:** el lifecycle de Hermes es **timestamp-driven, sin score** — más
> simple que el gate-blando-contra-uso que propusimos (plan §4). Confirma que el
> "validation gate" de SkillOpt NO existe en Hermes; su evolución es la del fork
> que parchea + archivado por antigüedad. El darwinian-evolver es el análogo a
> SkillOpt/GEPA pero **no está cableado** a skills — exactamente el gap de "Capa
> B" que discutimos.

---

## §4 — Provenance / import / hub / sync

### Provenance real = `lock.json` del Hub (no `skill_provenance.py`)

`tools/skills_hub.py:2600` (`record_install`) — schema:
```python
        data["installed"][name] = {
            "source": source,            # github | clawhub | claude-marketplace | lobehub | well-known | url | official
            "identifier": identifier,    # e.g. "openai/skills/skill-creator"
            "trust_level": trust_level,  # builtin | trusted | community
            "scan_verdict": scan_verdict,
            "content_hash": skill_hash,
            "install_path": install_path, "files": files, "metadata": metadata or {},
            "installed_at": ..., "updated_at": ...,
        }
```
**No hay campo `source_url` ni `version` de upstream de primera clase** — la
procedencia es el par `(source, identifier)` + `content_hash`. URLs viven sueltas
en `metadata`. Hay además un audit-log append-only (`skills_hub.py:2693`).

### Flujo de import: search → fetch → quarantine → scan → confirm → install

Sources pluggables tras una ABC (`SkillSource.search/fetch/inspect/source_id`,
`skills_hub.py:294`); router con 9 adapters (`skills_hub.py:3136`); search en
paralelo con dedupe por trust. Driver `hermes_cli/skills_hub.py:408` (`do_install`).

### NO guarda original vs adapted (gap vs nuestro diseño)

No se retiene copia prístina del upstream. "¿Cambió upstream?" se responde
**re-fetcheando y comparando hashes** — `skills_hub.py:2893`:
```python
        current_hash = entry.get("content_hash", "")
        latest_hash = bundle_content_hash(bundle)
        status = "up_to_date" if current_hash == latest_hash else "update_available"
```
El único "user-modified vs original" existe para skills **bundled con el repo**
(`tools/skills_sync.py`), y solo como **baseline por hash** (`origin_hash`), no
copia. Si el user modificó → se salta el update (`skills_sync.py:255`).

### OpenClaw migration = copytree byte-por-byte (no traduce formato)

Los formatos ya coinciden, así que `migrate_skills` solo copia dirs a
`skills/openclaw-imports/` y aplica rebranding **solo a prosa** (SOUL/memoria),
no a los bodies de skill — `openclaw_to_hermes.py:1946` (`shutil.copytree`).
Importante: estas skills **no** entran al `lock.json` → quedan sin provenance.

### agentskills.io: sí, declarado en código

`tools/skills_tool.py:28` documenta el schema como "agentskills.io compatible"
(`name`, `description`, `version`, `license`, `compatibility`, `metadata`...).
`README.md:22` lo afirma; Hub URL = `https://agentskills.io`. Además convención
de descubrimiento `.well-known/skills/index.json` (`WellKnownSkillSource`,
`skills_hub.py:751`).

> **→ durin:** el **original/adapted split que queremos NO existe en Hermes** —
> es innovación nuestra. Hermes solo guarda hash + re-fetch. Su `lock.json`
> `(source, identifier, content_hash, scan_verdict, trust_level)` es buen punto
> de partida para nuestro campo `provenance`, agregándole el original retenido.

---

## §5 — Storage, schema y seguridad

### Storage = flat files, NO SQLite

`tools/skill_manager_tool.py:107`: `SKILLS_DIR = HERMES_HOME / "skills"`.
Discovery por `os.walk` de `SKILL.md`. Los únicos JSON son sidecars del Hub
(`.hub/lock.json`, `audit.log`, `quarantine/`) y `.usage.json` — metadata, no
las skills.

### Schema SKILL.md = abierto, solo `name`+`description` obligatorios

Parser YAML con fallback key:value (`agent/skill_utils.py:52`). Campos opcionales
consumidos por código: `platforms` (gating OS), `metadata.hermes.requires_tools /
fallback_for_tools` (visibilidad en prompt), `metadata.hermes.config` (declara
vars en `config.yaml`). `dependencies`/`prerequisites` son **informativos, no se
auto-instalan**. **No existe** campo `triggers` ni `install-steps`; `allowed-tools`
es tratado como **amenaza** (ver abajo).

### `skills_guard.py`: scanner estático + matriz de política (solo para externos)

Trust levels + política (verbatim) — `tools/skills_guard.py:39`:
```python
TRUSTED_REPOS = {"openai/skills", "anthropics/skills", "huggingface/skills"}
INSTALL_POLICY = {
    #                  safe      caution    dangerous
    "builtin":       ("allow",  "allow",   "allow"),
    "trusted":       ("allow",  "allow",   "block"),
    "community":     ("allow",  "block",   "block"),
    "agent-created": ("allow",  "allow",   "ask"),
}
```
~120 patrones regex de amenaza: exfiltration (lee `~/.ssh`,`~/.aws`,`.env`),
injection (prompt-injection, unicode invisible), destructive (`rm -rf /`),
persistence (crontab, rc, **referencias a `AGENTS.md`/`CLAUDE.md`/`SOUL.md` =
critical**), obfuscation (base64|sh), supply_chain (`curl|sh`, pip/npm sin pin),
symlink-escape = critical. Declarar `allowed-tools` = "high / privilege_escalation"
(`skills_guard.py:411`).

### Dos paths de gate, fuerza OPUESTA

**Path A — install externo (gate real):** quarantine → scan → policy →
**confirmación obligatoria** del usuario. `hermes_cli/skills_hub.py:570`:
```python
    if not force and not skip_confirm:
        ... # "You are installing a third-party skill at your own risk."
        answer = input("Confirm [y/N]: ").strip().lower()
        if answer not in {"y","yes"}: ...  # cancela, borra quarantine
```
Nota: `--force` override cualquier block; `skip_confirm` (TUI/gateway) saltea el
prompt pero **no** el scan/policy.

**Path B — skill creada por el agente (sin gate de ejecución):** el scan está
**OFF por default** y no hay check de permiso antes de que el código corra.
`tools/skill_manager_tool.py:60` (rationale — clave para nuestro §8.C):
```python
    """Read skills.guard_agent_created from config (default False).
    Off by default because the agent can already execute the same code
    paths via terminal() with no gate, so the scan adds friction without
    meaningful security. ..."""
```
Única protección estructural en path B: `pinned` impide **borrado** (no ejecución)
— `skill_manager_tool.py:150`.

> **→ durin (importante para §8.C):** Hermes **desafía nuestra premisa del piso
> invariante**. Su argumento: si el agente ya ejecuta código vía `terminal()`
> sin gate, gatear el *install* de la skill es fricción sin seguridad real. Para
> que nuestro piso ("instalar código siempre pide confirmación") tenga sentido,
> durin necesita que el gate esté en la **ejecución** (terminal/tool), no solo en
> el install — o aceptar que es defensa de supply-chain (contenido de terceros),
> que es exactamente para lo que Hermes SÍ lo usa (Path A). Su matriz
> `trust_level × verdict → allow/block/ask` es un modelo directo para nuestro
> allowlist + gate.

---

## §6 — Correcciones a suposiciones previas (resumen)

| Creíamos / decía el marketing | Lo que dice el código |
|---|---|
| Trigger de creación ≥5 tool calls | `creation_nudge_interval` **default 10** iters/turno; "5+" es prosa no-vinculante |
| Hermes busca skills con FTS5/BM25/vector | **Progressive disclosure**: índice de TODAS inyectado siempre; modelo auto-selecciona vía `skill_view`. (El FTS5/BM25 era de su *memoria*, no skills) |
| Evolución con score/validation gate | Lifecycle **timestamp-driven sin score**; fork que parchea + archivado por antigüedad |
| darwinian-evolver evoluciona skills | NO — optimiza prompts/regex/SQL/code, sin cablear a skills |
| Guarda original vs adapted | NO retiene original; re-fetch + hash-diff. Split original/adapted es **innovación de durin** |
| Skills auto-creadas pasan por gate de seguridad | Scan OFF por default; gate real solo en install externo (Path A) |
| Skills en SQLite | Flat files `~/.hermes/skills/`; SQLite solo sidecars de metadata |

---

## §7 — Archivos clave (paths absolutos)

- `/Users/marcelo/git_personal/hermes-agent/agent/conversation_loop.py` (trigger @4045)
- `/Users/marcelo/git_personal/hermes-agent/agent/background_review.py` (fork + prompts)
- `/Users/marcelo/git_personal/hermes-agent/agent/curator.py` (lifecycle + consolidación)
- `/Users/marcelo/git_personal/hermes-agent/agent/prompt_builder.py` (índice/inyección @961,1072,1188)
- `/Users/marcelo/git_personal/hermes-agent/agent/skill_utils.py` (parser frontmatter)
- `/Users/marcelo/git_personal/hermes-agent/tools/skills_tool.py` (skills_list/skill_view + schema agentskills.io @28)
- `/Users/marcelo/git_personal/hermes-agent/tools/skill_manager_tool.py` (skill_manage + validación + provenance @780)
- `/Users/marcelo/git_personal/hermes-agent/tools/skill_usage.py` (estados @52 + telemetría)
- `/Users/marcelo/git_personal/hermes-agent/tools/skills_hub.py` (lock.json @2600, sources @294/3136)
- `/Users/marcelo/git_personal/hermes-agent/tools/skills_guard.py` (INSTALL_POLICY @39, threats @86-488)
- `/Users/marcelo/git_personal/hermes-agent/hermes_cli/skills_hub.py` (gate de install @408,570)
- `/Users/marcelo/git_personal/hermes-agent/tools/skills_sync.py` (origin_hash baseline)
- `/Users/marcelo/git_personal/hermes-agent/optional-skills/research/darwinian-evolver/` (GEPA-style, aparte)
