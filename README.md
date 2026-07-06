<p align="center">
  <img src="docs/assets/durin-banner.svg" alt="durin" width="820">
</p>

<p align="center">
  <a href="https://pypi.org/project/durin-agent/"><img alt="PyPI" src="https://img.shields.io/pypi/v/durin-agent?color=e0843f&label=pypi"></a>
  <img alt="Python" src="https://img.shields.io/pypi/pyversions/durin-agent?color=e0843f">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-e0843f"></a>
</p>

**durin is a local-first AI agent that lives on your machine, remembers across
sessions, and gets sharper the more you use it.** One agent loop answers you
everywhere — terminal, TUI, browser dashboard, Telegram, Slack, Discord, email —
all funnelling through the same memory and the same brain. Bring any LLM (Claude,
GLM, OpenAI, Gemini, OpenRouter, or a local llama.cpp model); durin keeps its
state under `~/.durin/` and never phones home.

> durin is named for Tolkien's dwarf-king of Khazad-dûm; the mark is a dwarven
> anvil lit by a forge flame.

## Why durin

- **One loop, every surface.** Every message — from the terminal, the TUI, the web
  dashboard, or any chat channel — arrives on the same internal bus and runs
  through the same agent loop. Channels differ only in their I/O; the agent
  behaves identically no matter where you talk to it.
- **A memory that persists — and searches without an LLM in the hot path.**
  Cross-session knowledge is kept as plain markdown over an FTS + vector index, so
  recall is fast and offline. Documents you hand it (PDF, Office, EPUB, web pages)
  land in a **Library** kept apart from everyday recall.
- **It learns while you sleep.** A cold-path *dream* consolidates your
  conversations into an entity graph and curates reusable skills; *cron* runs
  scheduled work. durin builds a deepening model of what you care about over time.
- **Local-first, any model.** It runs as a daemon on your own machine. Markdown is
  the source of truth; the search indexes are derived and rebuildable. Switch
  provider or model at runtime.
- **A real daily-driver runtime.** A gateway daemon serving a browser dashboard,
  an OpenAI-compatible API, MCP servers connected on demand, permission-as-data
  agent modes (plan / build / explore), skills, and multi-step workflows.

## Quick start

```bash
pipx install --pre durin-agent   # PyPI
durin onboard                    # interactive setup wizard
durin doctor                     # confirm setup is healthy
durin agent                      # launch the TUI
```

Run the gateway for the browser dashboard plus chat channels:

```bash
durin gateway start              # dashboard at http://127.0.0.1:8765
```

<!--
  SCREENSHOT PLACEHOLDER — drop a TUI or webui capture from a CLEAN demo
  workspace here (not a personal home), then uncomment:

  <p align="center">
    <img src="docs/assets/screenshot.png" alt="durin in action" width="820">
  </p>
-->

See the [install guide](docs/guide/install.md) for prerequisites, optional extras
(memory, local models, audio), and platform notes. For everyday and in-session
commands, see the [CLI reference](docs/guide/cli.md).

## Documentation

- [Install · configure · uninstall](docs/guide/install.md)
- [CLI & in-session commands](docs/guide/cli.md)
- [Configuration reference](docs/guide/configuration.md)
- [Providers & models](docs/guide/providers.md)
- [Channels](docs/guide/channels.md) — Telegram, Slack, Discord, email, and more
- [Documents & your knowledge](docs/guide/documents.md)
- [Workflows](docs/guide/workflows.md)
- [How it works (internals)](docs/internals/README.md)

## License

MIT
