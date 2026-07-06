# CLI & in-session commands

A command cheatsheet for driving durin day-to-day. For installing, configuring,
and where durin keeps its state on disk, see [install.md](install.md).

## Lifecycle

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

## Inside the TUI

- `/sessions` — modal picker over saved sessions (Esc to cancel)
- `/model` or `Ctrl+L` — modal picker over configured presets
- `/memory list|show|search|drill` — inspect the agent's memory
- `/remember <fact>` / `/forget <id>` — author memory directly
- `/compact [hint]` / `/copy` / `/name <name>` — session ergonomics
- Drag-and-drop a file path into the input to attach it
- Attach or record audio — transcribed to text locally before reaching the
  agent (`[stt]`/`[voice]` extras; see [install.md](install.md)).
  Default engine: Parakeet TDT v3 (~30× real-time on CPU, 25 European
  languages). Use `sensevoice` for Chinese/Japanese/Korean, or configure a
  cloud provider (Groq/OpenAI). In the TUI: drag an audio file or `/voice`
  to record. In the webui: attach a clip or use the 🎙 mic button.
- `@<prefix>` — fuzzy-complete a workspace file
- `!cmd` / `!!cmd` — shell shortcut (publishes / silent)
- `Alt+Enter` — newline; `Enter` — submit; `Esc` — cancel turn
