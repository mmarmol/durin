# Configuring providers and models

durin uses a **provider-first model picker**: every chat session runs on a
`(provider, model)` pair. The provider holds your credentials and endpoint;
the model is the name you send to that provider. You choose both explicitly —
there is no silent auto-routing based on model name alone.

## Quick start with the wizard

Run `durin onboard` to open the interactive hub wizard. If no working provider
is configured yet, the wizard first walks you through provider → API key →
default model (the minimum durin needs to talk to an LLM). It then enters the
hub: a re-entrant menu where each row shows a section and its current state —
for example:

```
Model & provider   ▸ anthropic · claude-opus-4-5
Vision / audio     ▸ vision ✓  audio ✗
Vector memory      ▸ on (intfloat/multilingual-e5-small)
Web search         ▸ off
…
✓ Finish onboarding
```

Select any row to configure that section; the hub returns after each one. When
you choose **✓ Finish onboarding** the wizard writes `~/.durin/config.json` and
secrets to `~/.durin/secrets.json`. You can also jump straight to a section
with `durin onboard <section>` (e.g. `durin onboard memory`).

For the legacy section-by-section menu (`[P] LLM Provider` / `[A] Agent
Settings` / `[S] Save and Exit`), run `durin onboard --advanced`.

To skip the wizard and set values from the command line:

```
durin config set providers.anthropic.api_key "sk-ant-..."
durin config set agents.defaults.model "anthropic/claude-opus-4-5"
durin config set agents.defaults.provider "anthropic"
```

`durin config show` prints the full config with secrets masked. `durin config
get agents.defaults.model` returns a single value. `durin config edit` opens
`$EDITOR` and validates the file before writing.

## Provider-first model selection

The active model is set by two fields under `agents.defaults`:

```json
"agents": {
  "defaults": {
    "provider": "anthropic",
    "model": "claude-opus-4-5"
  }
}
```

When `provider` is `"auto"`, durin matches the model name against each
provider's keyword list (defined in `durin/providers/registry.py`), then
falls through to local providers and any provider that has an API key
configured. Setting `provider` explicitly skips matching and routes directly
to that provider.

You can also configure a `model_preset` (see [Model presets](#model-presets))
which bundles both fields plus generation parameters into a named entry you
can switch atomically.

### Per-model parameter overrides

Each provider section accepts a `models` map. Entries there override the
catalog defaults (`provider_models.json`) and fall back to `agents.defaults`
for any unset field:

```json
"providers": {
  "openai": {
    "api_key": "${secret:OPENAI_KEY}",
    "models": {
      "gpt-4o": {
        "max_tokens": 4096,
        "temperature": 0.2
      }
    }
  }
}
```

The `ModelEntry` class (see `durin/config/schema.py`) accepts `max_tokens`,
`context_window_tokens`, `temperature`, and `reasoning_effort`. Any field
left `null` falls back to the catalog, then to `agents.defaults`.

## API keys and the secret store

API keys go into the secret store, not directly into `config.json`. The config
holds a `${secret:NAME}` reference; the value is resolved at call time and
never written to the config file.

Store a key:

```
durin secret set ANTHROPIC_KEY --service provider:anthropic
```

Then reference it in the config:

```
durin config set providers.anthropic.api_key '${secret:ANTHROPIC_KEY}'
```

Or let the wizard do this for you — it stores the key automatically.

To move any plaintext keys that are already in `config.json` into the store:

```
durin secret migrate
```

`durin secret list` shows stored secrets with masked values. `durin secret
show NAME --reveal` prints the actual value.

### Environment variables

Because `Config` inherits from `pydantic_settings.BaseSettings`, any config
field can be set via environment variables using the prefix `DURIN_` and `__`
as the path separator. For example:

```
DURIN_PROVIDERS__ANTHROPIC__API_KEY="sk-ant-..." durin ...
```

Provider-specific environment variables (e.g. `ANTHROPIC_API_KEY`) are
**not** read directly by durin's config layer. Use the `DURIN_` prefix form
or the secret store instead.

### OAuth providers

`openai_codex` and `github_copilot` are credentialed via OAuth, not static
API keys. These providers are excluded from config serialization (they have
`exclude=True` on their schema field) and cannot hold an `api_key`. Use
`durin mcp login` to complete the OAuth flow for those providers.

## Model presets

A model preset is a named bundle of `(provider, model, max_tokens,
context_window_tokens, temperature, reasoning_effort)` that you can activate
by name. The implicit `default` preset is always derived from
`agents.defaults`; the name `"default"` is reserved and cannot be defined
in `model_presets`.

Define a preset:

```json
"model_presets": {
  "fast": {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "max_tokens": 4096,
    "context_window_tokens": 128000,
    "temperature": 0.1
  },
  "reasoning": {
    "provider": "anthropic",
    "model": "claude-opus-4-5",
    "max_tokens": 16384,
    "context_window_tokens": 200000,
    "temperature": 0.4,
    "reasoning_effort": "high"
  }
}
```

Activate a preset for all sessions:

```
durin config set agents.defaults.model_preset "fast"
```

When `model_preset` is set it takes precedence over `agents.defaults.model`
and `agents.defaults.provider`. Switching it back to `null` (or clearing it)
restores the direct `model`/`provider` fields.

A preset also accepts an optional `preemptive_compact_ratio` — the fraction
of the context window at which durin compresses history before the next LLM
call. When unset it inherits from `agents.defaults.preemptive_compact_ratio`.

## Aux-model presets

Several subsystems make their own LLM calls outside the main chat loop. You
can route those calls to a different model via `agents.aux_models`:

```json
"agents": {
  "aux_models": {
    "vision": {
      "model": "gpt-4o",
      "provider": "openai"
    },
    "memory": {
      "preset": "fast"
    },
    "audio": null
  }
}
```

Each entry is an `AuxModelConfig` with either a `preset` (referencing a named
entry in `model_presets`) or an inline `model` + `provider` pair. When a
field is `null` the subsystem has no dedicated model.

**`vision`** — the interpret-image bridge. Used when the primary model lacks
vision capability. Leaving it `null` disables the bridge tool.

**`audio`** — the interpret-audio bridge. Same pattern as `vision`.

**`memory`** — model for the `memory_dream` passes (entity extraction,
refinement, skill-signal detection). Resolution order: `aux_models.memory` →
`memory.dream.model_override` → `agents.defaults`. Setting this to a smaller,
faster model keeps dream passes cheap without changing your chat model.

The **skill judge** (`skills.security.llm_judge.model`) is a plain model-name
string, not an `AuxModelConfig`. It resolves the same way: the named model is
looked up against the default provider; set it empty to use the default preset.

None of the aux-model fields hardcode a specific provider or model. Resolution
always falls back to the user's configured default, so leaving them unset
simply uses the same model as regular chat.

## Common provider setups

### Anthropic

```json
"providers": {
  "anthropic": {
    "api_key": "${secret:ANTHROPIC_KEY}"
  }
},
"agents": {
  "defaults": {
    "provider": "anthropic",
    "model": "claude-opus-4-5"
  }
}
```

No `api_base` needed — the native Anthropic SDK uses its built-in endpoint.

### OpenAI

```json
"providers": {
  "openai": {
    "api_key": "${secret:OPENAI_KEY}"
  }
},
"agents": {
  "defaults": {
    "provider": "openai",
    "model": "gpt-4o"
  }
}
```

### OpenRouter (gateway)

OpenRouter routes any model. Its API keys start with `sk-or-` and
`api_base` defaults to `https://openrouter.ai/api/v1`.

```json
"providers": {
  "openrouter": {
    "api_key": "${secret:OPENROUTER_KEY}"
  }
},
"agents": {
  "defaults": {
    "provider": "openrouter",
    "model": "anthropic/claude-opus-4-5"
  }
}
```

### Gemini

```json
"providers": {
  "gemini": {
    "api_key": "${secret:GEMINI_KEY}"
  }
},
"agents": {
  "defaults": {
    "provider": "gemini",
    "model": "gemini-2.5-pro"
  }
}
```

Default base: `https://generativelanguage.googleapis.com/v1beta/openai/`.

### Ollama (local)

Ollama needs no API key. Set `api_base` to your running instance if it is not
on `http://localhost:11434/v1`:

```json
"providers": {
  "ollama": {
    "api_base": "http://localhost:11434/v1"
  }
},
"agents": {
  "defaults": {
    "provider": "ollama",
    "model": "llama3.2"
  }
}
```

Via CLI:

```
durin config set providers.ollama.api_base "http://localhost:11434/v1"
durin config set agents.defaults.provider "ollama"
durin config set agents.defaults.model "llama3.2"
```

### AWS Bedrock

Bedrock uses the native Converse API with IAM credentials. The
`BedrockProviderConfig` adds `region` and `profile` on top of the base
provider fields:

```json
"providers": {
  "bedrock": {
    "region": "us-east-1",
    "profile": "my-aws-profile"
  }
},
"agents": {
  "defaults": {
    "provider": "bedrock",
    "model": "anthropic.claude-opus-4-5-20250514-v1:0"
  }
}
```

When `profile` is omitted, the standard AWS credential chain (env vars,
`~/.aws/credentials`, instance profile) applies.

### Azure OpenAI

`azure_openai` is a direct provider. The model name is the deployment name
in your Azure workspace:

```json
"providers": {
  "azure_openai": {
    "api_key": "${secret:AZURE_KEY}",
    "api_base": "https://my-resource.openai.azure.com/"
  }
},
"agents": {
  "defaults": {
    "provider": "azure_openai",
    "model": "my-gpt4o-deployment"
  }
}
```

### Custom OpenAI-compatible endpoint

Use the `custom` provider for any OpenAI-compatible server not listed above:

```json
"providers": {
  "custom": {
    "api_key": "${secret:CUSTOM_KEY}",
    "api_base": "https://my-llm-server.example.com/v1"
  }
},
"agents": {
  "defaults": {
    "provider": "custom",
    "model": "my-model-name"
  }
}
```

## Listing supported providers

The authoritative provider list is derived from `ProvidersConfig.model_fields`
in `durin/config/schema.py`. To print the current set:

```
python -c "from durin.config.schema import ProvidersConfig; print(list(ProvidersConfig.model_fields.keys()))"
```

The most common starting points are `anthropic`, `openai`, `openrouter`,
`gemini`, and `ollama` for local inference. The full set also includes
`deepseek`, `groq`, `zhipu`, `dashscope`, `moonshot`, `mistral`, `minimax`,
`gemini`, `bedrock`, `azure_openai`, `huggingface`, `vllm`, `lm_studio`, and
several others — run the command above for the complete current list.
