# Install · Upgrade · Configure · Uninstall

This is the operations manual for the durin binary and its on-disk state.
For everyday usage, see [README.md](../README.md).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | `pyproject.toml` pins `requires-python = ">=3.11"`. |
| `pip` (or `uv`) | Editable installs use `pip install -e .`. |
| `git` | For cloning + `durin upgrade` on editable installs. |
| `bun` *or* `npm` (only for source builds) | The hatch build hook compiles the webui. Skip via `DURIN_SKIP_WEBUI_BUILD=1` if you only need the CLI. |

Optional system dependencies, depending on what you use:

- `pbcopy` (macOS) / `xclip` or `wl-copy` (Linux) → required by `/copy`
- A TTY emulator that supports drag-and-drop (iTerm2, Kitty, WezTerm, …) for image attachments

---

## Install

The distribution name on PyPI is **`durin-agent`** — the CLI command stays
`durin` and the import package stays `durin`.

### From PyPI (recommended for users)

```bash
# Alpha / pre-releases require --pre
pipx install --pre durin-agent
# or, plain pip into the current environment:
pip install --pre durin-agent

# Once we cut a stable release:
pipx install durin-agent
```

`pipx` is preferred for CLI installs because it isolates durin's
dependency tree from anything else on your Python.

### From a GitHub Release wheel (no PyPI required)

Every tag also produces a GitHub Release with the wheel + sdist
attached:

```bash
pipx install https://github.com/mmarmol/durin/releases/latest/download/durin_agent-0.1.0a1-py3-none-any.whl
```

Replace the version in the URL with the release you want.

### From a checkout (recommended for development)

```bash
git clone git@github.com:mmarmol/durin.git
cd durin
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

The editable install skips the webui bundle. If you need the webui dist
locally, run `cd webui && bun run build` after installing.

### Local wheel build

```bash
pip install build
DURIN_SKIP_WEBUI_BUILD=1 python -m build         # set the env var if you don't have bun/npm
pip install ./dist/durin_agent-*.whl
```

The wheel build normally calls `bun` (preferred) or `npm` to bundle
`webui/` into `durin/web/dist/` — see [hatch_build.py](../hatch_build.py).

### Optional extras

`pyproject.toml` exposes opt-in dependency groups. Combine with brackets:

```bash
# Installed from PyPI
pipx install --pre 'durin-agent[memory,mcp,web]'

# Editable from a checkout
pip install -e ".[memory,mcp,web]"
```

| Extra | Pulls in | When you need it |
|---|---|---|
| `memory` | `fastembed`, `lancedb` | Vector recall + lexical FTS over `memory/`. Default embedding is `intfloat/multilingual-e5-small` (~450 MB, 100+ langs, MIT). |
| `cross-encoder` | `sentence-transformers` (+ `torch` ~1 GB) | Optional reranker for `memory_search`. Default model `BAAI/bge-reranker-base` (~100M params, MIT). Off by default — opt in via the wizard or `memory.search.cross_encoder.enabled = true`. |
| `mcp` | `mcp` | Use durin as an MCP server. |
| `web` | `ddgs`, `readability-lxml` | The web-search and reader tools. |
| `slack` | `slack-sdk`, `slackify-markdown` | Slack channel. |
| `discord` | `discord.py` | Discord channel. |
| `oauth` | `oauth-cli-kit` | OAuth login (`durin provider login …`). |
| `local` | `llama-cpp-python`, `huggingface-hub` | Local GGUF model serving. |
| `dev` | `pytest`, `ruff`, … | Run the test suite + lint. |

Memory subsystem note: with `[memory]` installed, durin's workspace at
`~/.durin/workspace/` becomes a navigable knowledge vault — a
`VAULT_README.md` is auto-generated at the workspace root on first
boot explaining the layout, and the on-disk format (markdown +
frontmatter + wikilinks) opens natively in Obsidian or any markdown
reader. See `docs/architecture/memory/` for the subsystem deep-dive.

### First-time configuration

```bash
durin onboard           # creates ~/.durin/config.json with defaults
durin onboard --wizard  # adds the interactive questionnaire on top
```

Onboarding is **idempotent**: re-running with `--wizard` opens the
questionnaire pre-filled from the current config; running without `--wizard`
merges any new schema defaults into the existing file without overwriting
your values.

After onboarding, drop your provider API key in `~/.durin/config.json`
(field `providers.<vendor>.api_key`) or via `durin config set`:

```bash
durin config set providers.zhipu.api_key sk-...
```

---

## Configure

durin keeps a single canonical config at `~/.durin/config.json`, validated by
the Pydantic `Config` schema in [durin/config/schema.py](../durin/config/schema.py).

### Inspect

```bash
durin status                  # high-level: paths + which providers are wired
durin config path             # absolute path to config.json
durin config show             # whole config, with secrets masked
durin config show --raw       # whole config, unmasked (dump as written on disk)
durin config show providers   # only the providers section
```

### Read / write single keys

`durin config get` and `set` take **dot-paths** through the schema:

```bash
durin config get agents.defaults.model
durin config set agents.defaults.model glm-5.1
durin config get providers.zhipu.api_key
durin config set providers.zhipu.api_key sk-...
durin config set model_presets.fast.model glm-5-turbo
```

Values that look like JSON (`true`, `false`, `null`, integers, floats,
arrays, objects) are decoded automatically; otherwise the literal string
is stored.

`set` validates the result against the schema before writing — if the new
value would break the config, the file is left untouched and the original
ValidationError is printed.

### Hand-edit

```bash
durin config edit             # opens $EDITOR (or vi) on the live file
```

The file is reloaded + validated when the editor exits; on validation
failure the previous version is restored and the diff is reported.

### Channel & provider auth

These remain dedicated subcommands because they involve OAuth or external
plugins, not single-key edits:

```bash
durin channels status
durin channels login telegram
durin provider login openai_codex
durin provider login github_copilot
durin provider logout github_copilot
```

---

## Upgrade

`durin upgrade` figures out which install mode you're on and does the right
thing:

| Detected mode | What `durin upgrade` runs |
|---|---|
| Editable checkout (`pip install -e .`) | `git pull --ff-only` + `pip install -e .` |
| Wheel install (PyPI / local wheel) | `pip install --upgrade durin` |

Useful flags:

- `--check` — print the current version, the latest available, and exit
  without installing.
- `--ref <git-ref>` — for editable installs only, fetch and check out a
  specific branch or tag before reinstalling.
- `--migrate-only` — skip the package step; just rerun config migration.

After the package upgrade, durin replays the config migration pass
(`_migrate_config` in [durin/config/loader.py](../durin/config/loader.py))
and re-injects any newly added schema defaults — same merge that `durin
onboard` (no-wizard) performs.

---

## Uninstall

`durin uninstall` knows where state lives and asks for confirmation before
touching anything:

```bash
durin uninstall                 # dry-run by default: lists what would be deleted
durin uninstall --yes           # delete user data (~/.durin, ~/.cache/durin)
durin uninstall --purge --yes   # also `pip uninstall durin` afterwards
durin uninstall --keep-config   # remove caches/workspace, preserve config.json
```

Flags:

- `--yes` / `-y` — skip the interactive confirmation.
- `--purge` — additionally run `pip uninstall durin`. Self-uninstalls in a
  subprocess so the running command finishes cleanly first.
- `--keep-config` — preserve `~/.durin/config.json` and `pairing.json`;
  everything else still goes.
- `--keep-cache` — preserve `~/.cache/durin/`.
- `--keep-workspace` — preserve `~/.durin/workspace/`.

The command prints the exact paths and byte counts before prompting, so
you can sanity-check before committing.

### What lives outside the package

| Path | Removed by default? | Flag to keep |
|---|---|---|
| `~/.durin/config.json` | yes | `--keep-config` |
| `~/.durin/pairing.json` | yes | `--keep-config` |
| `~/.durin/workspace/` | yes | `--keep-workspace` |
| `~/.durin/sessions/` | yes | — |
| `~/.durin/history/` | yes | — |
| `~/.durin/cron/` | yes | — |
| `~/.durin/media/` | yes | — |
| `~/.durin/bridge/` | yes | — |
| `~/.durin/webui/` | yes | — |
| `~/.cache/durin/telemetry/` | yes | `--keep-cache` |
| `~/.cache/durin/models/` | yes | `--keep-cache` |
| `~/.cache/durin/archive/` | yes | `--keep-cache` |
| `<workspace>/.durin/{plans,spills,tool-results}/` | only if `--workspace <path>` is passed | — |

Per-workspace scratch (`<workspace>/.durin/...`) is **not** removed
automatically — many users keep their workspace under a project repo and
don't want durin to touch it. Pass `--workspace <path>` to opt-in.

---

## Sanity checks

After install or upgrade, run the diagnostic battery:

```bash
durin doctor                  # full report; exit 0 unless something fails
durin doctor --ping           # also test reachability of the active provider
durin doctor --fix            # apply safe fixes (create workspace, replay migrations)
durin doctor --json | jq      # machine-readable for CI / scripts
```

`durin doctor` is the canonical "is everything wired?" check. It groups
results by category (system, config, providers, tools, extras, state) and
prints a list of suggested fixes at the bottom. Exit code 0 means no
`fail` — `warn` results don't break the exit code, so you can wire it
into CI.

Lower-level individual checks:

```bash
durin --version
durin status
durin config show | head -20
pytest tests/cli/ -q          # if you installed `[dev]`
```

If `status` reports a missing config path, rerun `durin onboard`.
If a provider line says `not set`, drop your key in via `durin config set
providers.<name>.api_key …` (or `durin config edit`).

---

Last updated: 2026-05-20.
