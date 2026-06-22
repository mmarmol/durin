# ⚒️ durin

Personal AI assistant with graph-based memory and a daily-driver CLI.

durin runs locally, stores its state under `~/.durin/`, and talks to any LLM
backend you wire up (Z.AI, Anthropic, OpenAI, Gemini, OpenRouter, local
llama.cpp, …). The default surface is a Textual TUI; an OpenAI-compatible API
server and a browser dashboard (`durin gateway`) plus channel plugins
(Telegram, Slack, WhatsApp, Discord) ship in the same package.

> durin is named for Tolkien's dwarf-king of Khazad-dûm — the ⚒️ mark is
> the dwarven hammer-and-pick, not a logo to be mistaken for anything else.

## Quick start

```bash
# Install (no git checkout required)
pipx install --pre durin-agent          # PyPI, recommended

durin onboard                           # interactive setup wizard
durin doctor                            # confirm setup is healthy
durin agent --tui                       # launches the TUI
```

For development you can still install from a checkout:

```bash
git clone git@github.com:mmarmol/durin.git
cd durin
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

See [docs/guide/install.md](docs/guide/install.md) for prerequisites, optional extras,
and platform notes. Maintainers cutting a release: [docs/releasing.md](docs/releasing.md).

## Lifecycle commands

| Need | Command |
|---|---|
| First-time setup | `durin onboard` (re-runnable — keeps what's configured) |
| Snapshot of what's configured | `durin status` |
| Diagnose what's wrong | `durin doctor` (`--fix` safe auto-fixes, `--ping-model` real round-trip) |
| Show config | `durin config show` |
| Change one key | `durin config set agents.defaults.model glm-5.1` |
| Edit config in `$EDITOR` | `durin config edit` |
| Pull the latest build | `durin upgrade` |
| Remove durin + data | `durin uninstall --purge` |

`status` = a factual snapshot; `doctor` = health checks with fixes.

## Day-to-day

```bash
durin agent              # rich TUI (default)
durin agent -m "hola"    # one-shot
durin gateway start      # background daemon: webui dashboard + channels + cron
durin gateway status     # is it running? where's the dashboard?
durin gateway stop
durin serve              # OpenAI-compatible API on :8900
```

The browser dashboard is served by `durin gateway` when
`config.gateway.webui_enabled` is true (default). `durin gateway status`
prints the URL.

Inside the TUI:

- `/sessions` — modal picker over saved sessions (Esc to cancel)
- `/model` or `Ctrl+L` — modal picker over configured presets
- `/memory list|show|search|drill` — inspect the agent's memory
- `/remember <fact>` / `/forget <id>` — author memory directly
- `/compact [hint]` / `/copy` / `/name <name>` — session ergonomics
- Drag-and-drop a file path into the input to attach it
- Attach or record audio — transcribed to text locally before reaching the
  agent (`[stt]`/`[voice]` extras; see [docs/guide/install.md](docs/guide/install.md)).
  Default engine: Parakeet TDT v3 (~30× real-time on CPU, 25 European
  languages). Use `sensevoice` for Chinese/Japanese/Korean, or configure a
  cloud provider (Groq/OpenAI). In the TUI: drag an audio file or `/voice`
  to record. In the webui: attach a clip or use the 🎙 mic button.
- `@<prefix>` — fuzzy-complete a workspace file
- `!cmd` / `!!cmd` — shell shortcut (publishes / silent)
- `Alt+Enter` — newline; `Enter` — submit; `Esc` — cancel turn

## Where state lives

| Path | What |
|---|---|
| `~/.durin/config.json` | Main config |
| `~/.durin/workspace/` | Default workspace |
| `~/.durin/sessions/` | Legacy global sessions |
| `~/.durin/history/cli_history` | Shell-style input history |
| `~/.durin/cron/` | Scheduled jobs |
| `~/.durin/media/` | Channel-attached media |
| `~/.durin/bridge/` | WhatsApp bridge install |
| `~/.cache/durin/telemetry/` | JSONL telemetry per session |
| `~/.cache/durin/models/` | Downloaded local-model weights |
| `~/.cache/durin/archive/` | Archived session payloads |
| `<workspace>/.durin/{plans,spills,tool-results}/` | Per-workspace agent scratch |

`durin uninstall` enumerates these before deleting anything.

## Documentation

- [docs/guide/install.md](docs/guide/install.md) — prerequisites, optional extras
- [docs/internals/README.md](docs/internals/README.md) — system layout
- [docs/roadmap.md](docs/roadmap.md) — direction

## License

MIT
