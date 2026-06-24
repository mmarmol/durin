# Configuration

durin is configured through a single JSON file that lives inside the **durin home
directory**. This guide covers where that file lives, how to inspect and edit it,
and what every top-level configuration section controls.

> **This reference is schema-sourced.** The key names, defaults, and descriptions
> below come directly from `durin/config/schema.py`. To regenerate the full key
> list with live defaults at any time, run:
>
> ```
> python -c "import json; from durin.config.schema import Config; print(json.dumps(Config.model_json_schema(), indent=2))"
> ```

---

## Where config lives

### The durin home directory

durin stores all instance state — config, memory, sessions, secrets — under a
single directory called the **durin home**. By default that is `~/.durin`. Set
the environment variable `DURIN_HOME` to any path to use a different location:

```
DURIN_HOME=/path/to/my-instance durin chat
```

Each value of `DURIN_HOME` is a fully independent instance: its own config, its
own ports, its own memory store. This makes it straightforward to run a
development instance alongside your daily-driver instance without any
interference (see `durin/config/home.py` for the resolution logic).

### The config file

The configuration file is `config.json` inside the durin home. A split-directory
layout is also supported: if `config.json.d/` exists next to `config.json`,
topic-specific overrides are stored there and merged at load time. Both layouts
are transparent to the CLI commands.

To see the exact path for the active instance:

```
durin config path
```

### Environment variable overrides

Because the root config class is a pydantic `BaseSettings` with the prefix
`DURIN_` and nested delimiter `__`, any config key can be overridden by an
environment variable. Example:

```
DURIN_GATEWAY__PORT=19000 durin gateway
```

Nested keys use double-underscores: `DURIN_AGENTS__DEFAULTS__MODEL=...`.
Environment variables always win over the file value.

---

## Inspecting and editing config

All config commands read (and write) through the schema, so defaults are
always applied and invalid edits are rejected before anything is written to
disk.

| Command | What it does |
|---|---|
| `durin config path` | Print the absolute path to `config.json` |
| `durin config show` | Show the full config (secrets masked) |
| `durin config show providers.anthropic` | Show one section |
| `durin config show --raw` | Show config with secrets unmasked |
| `durin config get agents.defaults.model` | Print one effective value (defaults applied) |
| `durin config set gateway.port 19000` | Set one value (validated before write) |
| `durin config edit` | Open config in `$EDITOR`; restore on validation failure |
| `durin config import ~/.durin_backup` | Import a config and migrate plaintext secrets |

**Casing.** `snake_case` is the canonical form everywhere — the on-disk config,
the HTTP/WS API, and the webui all use `snake_case` (matching the Python field
names). Input is case-tolerant: `set`/`get` and the loader accept both
`snake_case` and `camelCase`, so a legacy camelCase config still loads and is
rewritten to `snake_case` on the next save. Output is always `snake_case`.

---

## Configuration sections

### `agents`

Controls the agent loop: the active model, generation parameters, session
behaviour, tool iteration limits, and per-model capability overrides.

**`agents.defaults`** — default parameters applied to every new agent session:

| Key | Default | Meaning |
|---|---|---|
| `workspace` | `<durin_home>/workspace` | Working directory for file tools |
| `model` | `anthropic/claude-opus-4-5` | Active model (`provider/name` form) |
| `provider` | `auto` | Provider name or `"auto"` for auto-detection |
| `model_preset` | `null` | Named preset from `model_presets`; takes precedence over `model` + `provider` |
| `max_tokens` | `8192` | Maximum output tokens per turn |
| `context_window_tokens` | `65536` | Context window size hint (tokens) |
| `temperature` | `0.4` | Generation temperature |
| `reasoning_effort` | `null` | `low` / `medium` / `high` / `adaptive` / `none`; `null` preserves the provider default |
| `max_tool_iterations` | `200` | Tool call iterations cap per turn |
| `max_concurrent_subagents` | `1` | Parallel sub-agent concurrency cap |
| `max_tool_result_chars` | `16000` | Truncation limit on individual tool results |
| `provider_retry_mode` | `standard` | `standard` or `persistent` retry strategy |
| `fallback_models` | `[]` | Ordered list of preset names or inline model specs to try on provider failure |
| `timezone` | `UTC` | IANA timezone for date-aware tools (e.g. `America/New_York`) |
| `bot_name` | `durin` | Display name shown in CLI prompts |
| `bot_icon` | `⚒️` | Icon shown next to the bot name in CLI; `""` to omit |
| `unified_session` | `false` | Share one session across all channels (single-user multi-device) |
| `ask_user_blocking` | `true` | `ask_user` tool awaits the user's next message inside the same turn |
| `ask_user_answer_timeout_s` | `300` | Timeout (seconds) before `ask_user` degrades to yield |
| `plan_stall_turns` | `8` | Turns without todo progress before a reassess reminder is injected; `0` disables |
| `disabled_skills` | `[]` | Skill names to exclude from loading |
| `max_messages` | `120` | Max messages replayed from session history; `0` uses default |
| `consolidation_ratio` | `0.5` | Target ratio of context budget retained after compaction |
| `preemptive_compact_ratio` | `0.5` | Fraction of context window that triggers pre-emptive compaction |
| `decision_log_enabled` | `true` | Record key decisions/findings across compaction boundaries |
| `decision_log_max_entries` | `10` | Cap on decision-log entries re-injected each turn |
| `decision_log_max_chars` | `1500` | Total character cap on the decision log |
| `parallel_tool_calls` | `{}` | Per-model substring → bool map for the `parallel_tool_calls` request flag |
| `tool_hint_max_length` | `40` | Max characters for tool-call hints shown in the channel (e.g. `$ cd …/project`) |
| `context_block_limit` | `null` | Hard limit on context blocks (overrides token budget when set) |

**`agents.aux_models`** — optional auxiliary model bridges (used only when the primary model lacks the modality):

| Key | Default | Meaning |
|---|---|---|
| `aux_models.vision` | `null` | Aux model for vision inputs (`preset` or `model`+`provider`) |
| `aux_models.audio` | `null` | Aux model for audio inputs |
| `aux_models.memory` | `null` | Highest-priority model for memory dream passes; overrides `dream.model_override` and the bundled default |

**`model_presets`** — named sets of model + generation parameters for quick switching.
Each entry under `model_presets` is a `ModelPresetConfig`:

| Key | Default | Meaning |
|---|---|---|
| `model` | (required) | Model identifier |
| `provider` | `auto` | Provider or `"auto"` |
| `max_tokens` | `8192` | Output token cap |
| `context_window_tokens` | `65536` | Context window hint |
| `temperature` | `0.1` | Temperature |
| `reasoning_effort` | `null` | Thinking effort hint |
| `preemptive_compact_ratio` | `null` | Per-preset compaction trigger; `null` inherits from `agents.defaults` |

**`model_capabilities`** — user-declared capability overrides keyed by model name
(bare or `provider/model`). Provider-qualified keys win over bare names. Any field
left `null` falls through to the vendored snapshot. Useful for local fine-tunes or
when the snapshot is wrong for your deployment.

---

### `appearance`

Visual theme shared by the TUI and the web dashboard.

| Key | Default | Meaning |
|---|---|---|
| `palette` | `ithildin` | Colour identity: `ithildin`, `forge`, or `mithril` |
| `mode` | `auto` | Light/dark mode: `auto` (detect from terminal/browser), `light`, or `dark` |

---

### `channels`

Global defaults for all chat channels. Each channel can override most of these
per-channel. Plugin and built-in channel-specific config lives as extra keys
under this section.

| Key | Default | Meaning |
|---|---|---|
| `send_progress` | `true` | Stream agent text progress to the channel |
| `send_tool_hints` | `false` | Stream tool-call hints (e.g. `read_file("…")`) |
| `show_reasoning` | `true` | Surface model reasoning when the channel implements it |
| `send_max_retries` | `3` | Maximum delivery attempts (initial send included) |
| `transcription_provider` | `groq` | Per-channel voice transcription backend override: `groq` or `openai` |
| `transcription_language` | `null` | Optional ISO-639-1 language hint for audio transcription (e.g. `en`, `es`) |

See [providers.md](../internals/providers.md) for wiring transcription API keys.
For channel-specific setup see the [channels guide](../internals/channels.md).

---

### `transcription`

Global voice transcription settings. Channel-level keys override these per-channel.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master toggle for transcription |
| `mode` | `auto` | `auto`, `preview`, or `off` |
| `provider` | `local` | Backend: `local`, `openai`, `groq`, or `http` |
| `language` | `null` | ISO-639-1 language hint |
| `max_duration_s` | `600` | Maximum audio clip duration in seconds |
| `cache_transcripts` | `true` | Cache transcript results to avoid re-transcribing |

**`transcription.local`** — on-device ASR via sherpa-onnx:

| Key | Default | Meaning |
|---|---|---|
| `engine` | `parakeet` | Engine: `parakeet` or `sensevoice` |
| `model_dir` | `null` | Model directory; `null` = auto-download to `<durin_home>/models/stt/<engine>` |
| `num_threads` | `null` | Thread count; `null` = provider default (2) |

**`transcription.http`** — OpenAI-compatible HTTP endpoint (whisper.cpp, mlx-qwen3-asr, vLLM):

| Key | Default | Meaning |
|---|---|---|
| `base_url` | `null` | Endpoint URL |
| `api_key` | `null` | API key for the endpoint |
| `model` | `null` | Model name to request |

**`transcription.openai`** and **`transcription.groq`** — cloud provider credentials:

| Key | Default | Meaning |
|---|---|---|
| `api_key` | `null` | API key for the provider |
| `api_base` | `null` | Optional base URL override |

---

### `tts`

Text-to-speech for spoken replies in conversational voice mode. See
[docs/internals/voice.md](../internals/voice.md) for architecture.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master toggle for text-to-speech |
| `provider` | `local` | Backend: `local` (Supertonic) or `openai` (cloud) |
| `language` | `null` | ISO-639-1 language hint; `null` = auto |
| `fallback` | `none` | `openai` to fall through to cloud when local synthesis fails |

**`tts.local`** — on-device TTS via Supertonic (ONNX, self-downloading):

| Key | Default | Meaning |
|---|---|---|
| `engine` | `supertonic` | Local engine identifier |
| `voice` | `F4` | Preset voice: `F1`–`F5` or `M1`–`M5` |
| `model_dir` | `null` | Model directory; `null` = auto-download (~260 MB) |
| `quality` | `normal` | `normal` (8 steps) or `high` (20 steps) |

**`tts.openai`** — cloud TTS credentials:

| Key | Default | Meaning |
|---|---|---|
| `api_key` | `null` | API key for the provider |
| `api_base` | `null` | Optional base URL override |

Local TTS needs the `[tts]` extra (`supertonic` + `onnxruntime`); cloud needs only an API key.

---

### `voice`

Hands-free conversational voice mode — the floating orb in the dashboard that
listens, thinks, and speaks. See [docs/internals/voice.md](../internals/voice.md).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master toggle for conversational voice |
| `barge_in` | `true` | Allow interrupting playback by speaking over it |
| `vad_threshold` | `0.5` | Browser voice-activity-detection sensitivity (0–1) |
| `end_of_turn_silence_ms` | `700` | Silence (ms) that ends an utterance |
| `idle_timeout_s` | `300` | Auto-exit voice after silence; `0` = never |

**`voice.spoken_render`** — what the voice speaks when a reply is long (the spoken text differs from the full text shown on screen):

| Key | Default | Meaning |
|---|---|---|
| `mode` | `model_led` | `model_led` (speak the opening, rest stays on screen) or `verbatim` (read everything) |
| `long_threshold_words` | `60` | Replies at or under this many words are always read in full |
| `pointer` | "The full answer is on screen." | Sentence appended after the opening in `model_led` mode |

---

### `memory`

Controls the memory subsystem: vector retrieval, dream passes (extract/refine/skill),
background file watching, and health checks. See
[docs/internals/memory/](../internals/memory/) for architecture details.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Enable vector retrieval; when false, memory tools work over markdown files only (grep-level recall) |
| `index_skills` | `true` | Make skills searchable as a `skill` memory class |
| `owner` | `null` | Workspace owner entity ref (e.g. `person:marcelo`); `null` defaults to anonymous |

**`memory.embedding`** — embedding model for the vector index:

| Key | Default | Meaning |
|---|---|---|
| `provider` | `fastembed` | Embedding adapter; currently only `fastembed` |
| `model` | `intfloat/multilingual-e5-small` | Embedding model from fastembed's catalog; multilingual, retrieval-tuned |
| `base_url` | `null` | HTTP provider base URL (reserved for future adapters) |
| `api_key` | `null` | HTTP provider API key (reserved for future adapters) |

**`memory.dream`** — entity-centric extract / refine / skill passes and their triggers:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch; `false` disables cron + reactive triggers; manual `durin memory dream` still works |
| `cron` | `0 3 * * *` | Cron expression for the daily extract pass |
| `post_compaction` | `true` | Run a dream pass after a session is compacted |
| `on_session_close` | `true` | Run a dream pass when a session ends |
| `discover_enabled` | `true` | Grow the entity graph from agent-mentioned facts (mention-based entity discovery) |
| `skill_signals_enabled` | `true` | Extract skill corrections/gaps from session turns and feed the observation queue |
| `model_override` | `null` | Dream model; `null` falls through to the bundled default (resolution order: `agents.aux_models.memory` → `memory.dream.model_override` → bundled default) |
| `min_seconds_between_runs` | `300` | Throttle for reactive triggers; `0` disables throttle (daily cron is never throttled) |
| `max_seconds_per_run` | `600` | Wall-clock cap per extract pass; `0` = run to completion |
| `always_on_token_budget` | `1500` | Token budget for the always-on guidance pin injected into every prompt; `0` disables |

**`memory.dream.auto_absorb`** — post-dream automatic entity deduplication (ON by default):

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | ON by default; the refine pass auto-merges judged duplicates (recoverable via git revert + tombstone) |
| `confidence_threshold` | `95` | LLM-judge confidence floor (0-100) for an auto-merge |

**`memory.search`** — search pipeline configuration:

| Key | Default | Meaning |
|---|---|---|
| `search.cross_encoder.enabled` | `false` | Enable cross-encoder reranker (triggers a one-time model download on first search) |
| `search.cross_encoder.model` | `BAAI/bge-reranker-base` | Reranker model; MIT, multilingual |
| `search.cross_encoder.batch_size` | `32` | Reranker batch size |
| `search.cross_encoder.top_n` | `10` | Top-N hits kept after the rerank step |
| `search.sectioning.max_per_source` | `3` | Max hits from the same ingested document surviving sectioning |

**`memory.file_watcher`** — background filesystem watcher:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Watch `memory/*.md` for changes and re-index on write; disable for one less background thread |

**`memory.health_check`** — periodic memory subsystem health probe:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Run periodic health probe; disable for one less background thread |
| `interval_seconds` | `900` | Probe interval in seconds (min 60, max 86400) |

---

### `skills`

Governance for the skill subsystem: import policy, security, and discovery registries.
Per-agent tuning (`skills_hot_tier`, `disabled_skills`) lives under `agents.defaults`.
See [docs/internals/skills/](../internals/skills/) for architecture details.

| Key | Default | Meaning |
|---|---|---|
| `install_policy` | `approve` | How `skill_install_deps` runs declared install specs: `never` (report only), `approve` (dry-run then confirm), or `auto` (run without per-call confirm) |

**`skills.security`** — import security floor:

| Key | Default | Meaning |
|---|---|---|
| `allowlist` | (vetted vendor list) | Trusted source-ref prefixes (e.g. `github:anthropics/`); a match skips the source confirmation but never overrides the verdict/code gate |
| `github_token_secret` | `""` | Durin secret name holding a GitHub API token (raises rate limits, enables private repos) |
| `max_files` | `100` | Maximum files in a fetched skill |
| `max_total_bytes` | `3145728` | Maximum total size of a fetched skill (3 MB) |
| `max_file_bytes` | `1048576` | Maximum size of a single skill file (1 MB) |

**`skills.security.llm_judge`** — optional LLM semantic audit of imported skills:

| Key | Default | Meaning |
|---|---|---|
| `trigger` | `off` | When to auto-run: `off`, `uncertain` (only when gate is already unsure), or `always` |
| `max_severity` | `caution` | Cap on how high the judge may raise the verdict: `caution` or `dangerous` |
| `model` | `""` | Aux model name; empty = default |

**`skills.discovery`** — which registries to search:

| Key | Default | Meaning |
|---|---|---|
| `search_limit` | `10` | Max results returned per search |
| `registries` | `[skills.sh, clawhub]` | List of `SkillRegistryConfig` entries (see below) |

Each `SkillRegistryConfig`:

| Key | Default | Meaning |
|---|---|---|
| `name` | (required) | Registry display name |
| `kind` | (required) | Adapter: `skills.sh`, `clawhub`, `github`, or `well-known` |
| `enabled` | `true` | Enable or disable this registry |
| `api_key_secret` | `""` | Durin secret name for the API key (empty = anonymous) |
| `taps` | `[]` | GitHub-only: list of repos to search |

**`skills.discovery.skills_hot_tier`** (under `agents.defaults`) — working-set skill injection:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Inject only the usage-ranked working set instead of the full catalog |
| `recent` | `15` | Number of recently-used skills included |
| `frequent` | `30` | Number of frequently-used skills included |
| `frequent_window_hours` | `168.0` | Window for frequency ranking (7 days) |
| `recent_window_hours` | `24.0` | Window for recency ranking |

---

### `providers`

API credentials and per-model parameter overrides for every LLM provider.
All fields are optional; only configure the providers you use.
See [providers.md](../internals/providers.md) for the full provider list and setup steps.

Each provider entry (`anthropic`, `openai`, `openrouter`, etc.) is a `ProviderConfig`:

| Key | Default | Meaning |
|---|---|---|
| `api_key` | `null` | API key; prefer `${secret:NAME}` references over plaintext |
| `api_base` | `null` | Custom base URL (local models, proxies, corporate endpoints) |
| `extra_headers` | `null` | Custom request headers (e.g. `APP-Code` for AiHubMix) |
| `extra_body` | `null` | Extra fields merged into every request body |
| `models` | `{}` | Per-model parameter overrides; each entry is `{max_tokens, context_window_tokens, temperature, reasoning_effort}` |

Supported providers (all use `ProviderConfig` unless noted):

`anthropic`, `openai`, `openrouter`, `gemini`, `deepseek`, `groq`, `zhipu`,
`dashscope`, `mistral`, `moonshot`, `minimax`, `minimax_anthropic`, `stepfun`,
`huggingface`, `nvidia`, `qianfan`, `siliconflow`, `volcengine`,
`volcengine_coding_plan`, `byteplus`, `byteplus_coding_plan`, `zai_coding_plan`,
`aihubmix`, `xiaomi_mimo`, `longcat`, `vllm`, `ollama`, `lm_studio`,
`atomic_chat`, `ovms`, `custom` (any OpenAI-compatible endpoint).

AWS Bedrock (`bedrock`) adds:

| Key | Default | Meaning |
|---|---|---|
| `region` | `null` | AWS region; falls back to `AWS_REGION` / `AWS_DEFAULT_REGION` / profile |
| `profile` | `null` | Optional AWS shared-config profile |

Azure OpenAI (`azure_openai`) uses the same `ProviderConfig`; set `model` to the
deployment name.

---

### `catalog_refresh`

Daily models.dev catalog refresh (top-level config field, peer of `providers` and `tools`).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Enable daily catalog refresh |
| `interval_hours` | `24` | Refresh interval in hours |

---

### `tools`

Configuration for built-in agent tools and MCP server connections.
See [docs/internals/tools.md](../internals/tools.md) for architecture details.

| Key | Default | Meaning |
|---|---|---|
| `restrict_to_workspace` | `false` | Restrict all tool file access to the workspace directory |
| `ssrf_whitelist` | `[]` | CIDR ranges exempt from SSRF blocking (e.g. `["100.64.0.0/10"]` for Tailscale) |

**`tools.exec`** — shell execution tool:

| Key | Default | Meaning |
|---|---|---|
| `enable` | `true` | Enable the shell exec tool |
| `timeout` | `60` | Default command timeout in seconds |
| `path_append` | `""` | Directories appended to `PATH` for executed commands |
| `sandbox` | `""` | Optional sandbox wrapper command |
| `allowed_env_keys` | `[]` | Env vars passed into the subprocess (in addition to the safe defaults) |
| `allow_patterns` | `[]` | Shell command glob-allow patterns |
| `deny_patterns` | `[]` | Shell command glob-deny patterns |

**`tools.web`** — web search and fetch tools:

| Key | Default | Meaning |
|---|---|---|
| `enable` | `true` | Enable web tools |
| `proxy` | `null` | HTTP proxy URL |
| `user_agent` | `null` | Custom User-Agent header |
| `search.provider` | `duckduckgo` | Search backend |
| `search.api_key` | `""` | Search API key (for providers that require one) |
| `search.base_url` | `""` | Custom search API base URL |
| `search.max_results` | `5` | Max search results returned |
| `search.timeout` | `30` | Search request timeout in seconds |
| `fetch.use_jina_reader` | `true` | Route fetches through Jina Reader for clean text extraction |

**`tools.my`** — self-inspection tool:

| Key | Default | Meaning |
|---|---|---|
| `enable` | `true` | Enable the `my` (self-inspection) tool |
| `allow_set` | `false` | Allow the agent to modify its own runtime state |

**`tools.post_edit_check`** — post-edit linter:

| Key | Default | Meaning |
|---|---|---|
| `enable` | `true` | Run a linter after write/edit operations |
| `timeout_s` | `10` | Linter timeout in seconds |
| `max_lines` | `20` | Max finding lines returned to the model |
| `checkers` | `{py: ruff check …}` | Extension → command template map; `{file}` is replaced with the edited file's path |

**`tools.code_execution`** — `execute_code` sandboxed Python tool:

| Key | Default | Meaning |
|---|---|---|
| `enable` | `true` | Enable the execute_code tool |
| `timeout_s` | `300` | Script execution timeout in seconds |
| `max_tool_calls` | `50` | Max durin tool calls a script may make |
| `max_stdout_bytes` | `50000` | stdout truncation limit |
| `max_stderr_bytes` | `10000` | stderr truncation limit |

**`tools.process`** — background process registry:

| Key | Default | Meaning |
|---|---|---|
| `max_running` | `16` | Concurrent background processes cap |
| `max_output_chars` | `200000` | Rolling tail buffer per process |
| `finished_ttl_s` | `1800` | How long finished process entries are kept (30 min) |

**`tools.mcp_servers`** — MCP server connections (keyed by server name):

Each entry is an `MCPServerConfig`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Enable/disable without removing the entry |
| `type` | `null` | Transport: `stdio`, `sse`, or `streamableHttp`; auto-detected when `null` |
| `command` | `""` | stdio: command to run (e.g. `npx`) |
| `args` | `[]` | stdio: command arguments |
| `env` | `{}` | stdio: extra environment variables |
| `url` | `""` | HTTP/SSE: endpoint URL |
| `headers` | `{}` | HTTP/SSE: custom headers |
| `tool_timeout` | `30` | Seconds before a tool call is cancelled |
| `tool_timeouts` | `{}` | Per-tool timeout overrides (raw tool name → seconds) |
| `catalog_timeout` | `1.5` | tools/list timeout at connect so a hung server can't stall startup |
| `keepalive_interval` | `180.0` | Seconds between idle keepalive heartbeats |
| `enabled_tools` | `["*"]` | Tools to register; `["*"]` = all, `[]` = none |
| `oauth` | `null` | Mark server as OAuth-requiring; `true` = DCR defaults |
| `allow_private_url` | `false` | Opt this server out of the SSRF private-IP block |
| `spawn_egress_policy` | `warn` | stdio: action on shell-interpreter+egress-tool spawn shape: `warn`, `refuse`, or `off` |
| `malware_check` | `true` | Query OSV API for MAL-* advisories before spawning stdio servers; fail-open on network error |

`oauth` can be `true` (DCR defaults) or an `MCPOAuthConfig` object with `scope`,
`client_id`, `client_secret`, and `callback_port` (default `1456`) for static
client registration. See [docs/internals/mcp.md](../internals/mcp.md) for OAuth setup.

MCP server sampling (server-initiated LLM calls) is governed by `sampling` under each server entry:

| Key | Default | Meaning |
|---|---|---|
| `sampling.enabled` | `false` | Allow this server to initiate LLM calls; off by default |
| `sampling.model` | `null` | Model for sampling; `null` = current default |
| `sampling.allowed_models` | `[]` | Allowlist of models the server may request |
| `sampling.max_tokens_cap` | `4096` | Hard cap on tokens per sampling request |
| `sampling.requests_per_minute` | `10` | Rate limit for sampling requests |
| `sampling.allow_tools` | `true` | Allow tool use in sampling responses |
| `sampling.max_tool_rounds` | `4` | Maximum tool-use rounds per sampling request |

**`tools.mcp_discovery`** — MCP server discovery:

| Key | Default | Meaning |
|---|---|---|
| `install_policy` | `approve` | `never`, `approve`, or `auto` |
| `quality` | `official` | Default discovery view: `official` (star/first-party gate) or `all` |
| `min_stars` | `100` | Star floor for the `official` gate |
| `search_limit` | `10` | Max results per search |

**`tools.mcp_deferral`** — defer MCP tool definitions behind a discovery bridge
when the aggregate schema size is large:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Enable the deferral bridge |
| `threshold_tokens` | `20000` | Schema size threshold; above this, `mcp_find_tools` / `mcp_invoke` replace individual tool definitions |

---

### `mcp_catalog_refresh`

Periodic refresh of the durin-owned MCP catalog (top-level config field, peer of `providers` and `tools`).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Enable periodic catalog refresh |
| `url` | (release asset URL) | Catalog download URL |
| `interval_hours` | `168` | Refresh interval in hours (7 days) |

---

### `gateway`

HTTP gateway and embedded web dashboard settings.

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Bind address; local-only by default |
| `port` | `18790` | Gateway listen port |
| `daemon` | `false` | Run detached with a PID file and log file; easier to debug when off |
| `webui_enabled` | `true` | Auto-enable the websocket channel so the embedded web dashboard is served |

---

### `api`

OpenAI-compatible API server settings.

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Bind address; local-only by default |
| `port` | `8900` | API server listen port |
| `timeout` | `120.0` | Per-request timeout in seconds |

---

### `cron`

Scheduled-work (cron) lifecycle settings.

| Key | Default | Meaning |
|---|---|---|
| `run_history_max` | `50` | Maximum run-history entries kept per job |
| `run_session_retention_hours` | `48` | How long run-session data is retained |

---

### `telemetry`

Local telemetry is always written to JSONL under `<durin_home>/telemetry/`.
The `push` sub-section enables optional fan-out to an HTTPS endpoint.

**`telemetry.push`**:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Enable HTTPS push of telemetry events |
| `url` | `null` | Destination endpoint URL |
| `token_secret_name` | `null` | Durin secret name for the bearer token (use `durin secret set <name> <token>`, never put the token in config directly) |
| `batch_size` | `10` | Events per HTTP batch |

---

### `logging`

Gateway daemon log lifecycle. Controls the `gateway.log` file sink only;
the telemetry subsystem has its own independent retention settings.

| Key | Default | Meaning |
|---|---|---|
| `max_file_mb` | `5` | File size at which `gateway.log` rotates to a new segment |
| `retention_days` | `7` | Age at which rotated gateway log segments are deleted |

---

### `install`

Persistent install-level state. Durin manages this section; you rarely need to
edit it manually.

| Key | Default | Meaning |
|---|---|---|
| `extras` | `[]` | Additive list of optional extras detected at any point; used by `durin doctor` |
| `auto_install_extras` | `true` | Auto-install a feature's pip extra when it is activated; `false` shows a `pip install durin-agent[X]` message instead |

---

## Common tasks

### Switch the default model

Set the model and provider directly on `agents.defaults`:

```
durin config set agents.defaults.model anthropic/claude-opus-4-5
durin config set agents.defaults.provider anthropic
```

Or define a named preset and activate it:

```json
{
  "model_presets": {
    "fast": {
      "model": "claude-haiku-3-5",
      "provider": "anthropic",
      "max_tokens": 4096
    }
  },
  "agents": {
    "defaults": {
      "model_preset": "fast"
    }
  }
}
```

See [providers.md](../internals/providers.md) for provider-specific setup.

### Wire a provider API key

Use the secret store rather than putting keys directly in config:

```
durin secret set anthropic_key sk-ant-...
durin config set providers.anthropic.api_key '${secret:anthropic_key}'
```

### Turn memory dream on or off

```
durin config set memory.dream.enabled false
```

To pause only the reactive triggers (not the daily cron):

```
durin config set memory.dream.post_compaction false
durin config set memory.dream.on_session_close false
```

### Change the gateway port

```
durin config set gateway.port 19000
```

Then restart the gateway:

```
durin gateway restart
```

### Enable the cross-encoder reranker

The cross-encoder significantly improves memory search ranking at the cost of a
one-time model download (~100 MB) and 300-800 ms added per search:

```
durin config set memory.search.cross_encoder.enabled true
```

### Connect a chat channel

Channel-specific keys live under `channels.<channel-name>`. For example, to
configure the Telegram channel, add its token as a secret and reference it:

```
durin secret set telegram_token 123:ABC...
durin config set channels.telegram.token '${secret:telegram_token}'
```

See [channels.md](../internals/channels.md) for the full per-channel setup guide.

### Run as a daemon

```
durin config set gateway.daemon true
durin gateway
```

The gateway now detaches, writing a PID file and log under `<durin_home>/logs/`.
