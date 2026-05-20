# 🐈 durin

Personal AI assistant with persistent posture, graph-based memory, and a daily-driver CLI.

durin runs locally, stores its state under `~/.durin/`, and talks to any LLM
backend you wire up (Z.AI, Anthropic, OpenAI, Gemini, OpenRouter, local
llama.cpp, …). The default surface is a Textual TUI; an OpenAI-compatible API
gateway and channel plugins (Telegram, Slack, WhatsApp, Discord) ship in the
same package.

## Quick start

```bash
git clone git@github.com:mmarmol/durin.git
cd durin
python -m venv .venv && source .venv/bin/activate
pip install -e .
durin onboard --wizard          # creates ~/.durin/config.json
durin agent --tui               # launches the TUI
```

See [docs/INSTALL.md](docs/INSTALL.md) for prerequisites, optional extras,
and platform notes.

## Lifecycle commands

| Need | Command |
|---|---|
| First-time setup | `durin onboard --wizard` |
| Inspect state | `durin status` |
| Show config | `durin config show` |
| Read one key | `durin config get agents.defaults.model` |
| Change one key | `durin config set agents.defaults.model glm-5.1` |
| Edit config in `$EDITOR` | `durin config edit` |
| Where does state live? | `durin config path` |
| Pull the latest build | `durin upgrade` |
| Remove durin + data | `durin uninstall --purge` |

## Day-to-day

```bash
durin agent --tui        # rich TUI (default)
durin agent -m "hola"    # one-shot
durin gateway            # bring up channels (Telegram, Slack, …)
durin serve              # OpenAI-compatible API on :8000
```

Inside the TUI:

- `/sessions` — modal picker over saved sessions (Esc to cancel)
- `/model` or `Ctrl+L` — modal picker over configured presets
- `/memory list|show|search|drill` — inspect the agent's memory
- `/remember <fact>` / `/forget <id>` — author memory directly
- `/compact [hint]` / `/copy` / `/name <name>` — session ergonomics
- Drag-and-drop a file path into the input to attach it
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

- [docs/INSTALL.md](docs/INSTALL.md) — prerequisites, optional extras
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system layout
- [docs/09_daily_driver_plan.md](docs/09_daily_driver_plan.md) — daily-driver roadmap
- [docs/01_roadmap.md](docs/01_roadmap.md) / [docs/02_bitacora.md](docs/02_bitacora.md) — historical context

## License

MIT
