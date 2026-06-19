# Audio Transcription — Design Spec

- **Date:** 2026-06-19
- **Status:** Approved (brainstorm complete) → pending implementation plan
- **Author:** Design session (brainstorming skill)
- **Branch target:** `feat/audio-input` in worktree `/Users/marcelo/git_personal/durin-audio`

## 1. Goal

Add first-class audio input to durin across three surfaces (webui, TUI,
channels) so users can attach or record audio and have it transcribed to
text before reaching the LLM. The design optimizes for:

- **Token efficiency** — the LLM receives text, never raw audio.
- **Transparency** — the user sees the transcript before it is sent and
  can edit it.
- **Multilingual fidelity** — strong coverage including Japanese and
  Mandarin, with auto language detection by default.
- **Portability** — runs on Linux, Windows, and macOS **on CPU**, with
  no GPU required.
- **Minimal install friction** — zero-config default for the common
  case; rich configuration for power users.

## 2. Decisions (locked during brainstorm)

| Decision | Outcome |
|---|---|
| Strategy | Always transcribe to text; the LLM never sees raw audio. |
| Model | **Whisper Large V3** (1.55B params). Multilingual (99 langs), MIT, only mature CPU-runnable option covering JP/CN well. |
| Runtime | **faster-whisper** (CTranslate2) in-process as default. Any OpenAI-compatible HTTP server as passthrough (Groq, OpenAI, mlx-qwen3-asr, vLLM, whisper.cpp). |
| Architecture | **Hybrid**: in-process default + HTTP passthrough. Unified behind a `TranscriptionProvider` interface. |
| Where transcription runs | Backend (`TranscriptionService`). Frontends upload raw audio. |
| Default mode | `auto` (transcribe and insert text; user can edit before sending). |
| Surfaces (MVP) | webui (attach + mic), TUI (drag-drop + `/voice`), channels (refactored to share service). |
| Worktree | Sibling worktree `durin-audio`, branch `feat/audio-input`. |

### 2.1 Model rationale (research summary)

Constraint: CPU-only, Linux/Win/Mac, MIT license, multilingual with strong
JP/CN. No single model satisfies all four at top quality:

- **Qwen3-ASR 1.7B** — best CER on JP/CN (3.6 / 2.71 on FLEURS vs Whisper's
  8.3 / 5.06) but only runnable on Mac (via `mlx-qwen3-asr`) or GPU (via
  vLLM). Not CPU-portable to Linux/Windows. Apache-2.0.
- **NVIDIA Nemotron 3.5 ASR** — excellent streaming on NVIDIA GPUs; unusable
  CER on Mandarin (~19); NeMo runtime does not run on Mac/CPU-only.
- **Moonshine** — excellent low-latency English on CPU, but JP (13.6) and
  Mandarin (25.8) CER are unacceptable; non-commercial license for
  non-English models (incompatible with durin's MIT); 8 languages only.
- **Whisper Large V3 via faster-whisper** — the only option that satisfies
  CPU + 3 OSes + MIT + 99 languages. Not top CER on JP/CN, but the only
  viable default. Qwen3-ASR remains available via HTTP passthrough for
  users who have a Mac or a GPU server.

### 2.2 Size & memory notes

faster-whisper INT8 weights (default compute type):
`tiny`~150MB, `base`~290MB, `small`~250MB, `medium`~800MB, `large-v3`~1.5GB
on disk, downloaded once and cached. RAM at inference: ~2-3 GB for
`large-v3`, ~1.5 GB for `medium`, ~0.5 GB for `small`. The configured
model is selectable so constrained hardware can drop to `medium`/`small`.

## 3. Architecture

```
Frontends (webui React, TUI Textual, channels)
  └─ upload/attach raw audio ─────────────────────┐
                                                  ▼
                          Channels (websocket.py, tui, telegram, ...)
                            - accept audio/* MIME
                            - store under ~/.durin/media/<channel>/
                                                  ▼
                          TranscriptionService (new, durin/service/)
                            - resolves provider from config
                            - enforces mode (auto / preview / off)
                            - caches transcript + meta next to audio
                                                  ▼
                          Providers (durin/providers/transcription.py)
                            LocalWhisperProvider (new, faster-whisper)
                            OpenAITranscriptionProvider (existing)
                            GroqTranscriptionProvider (existing)
                                                  ▼
                          Agent loop (durin/agent/context.py)
                            - receives TEXT only (transcript)
                            - audio no longer a content part
                            - audio original preserved in media store
```

**Key change:** today `agent/context.py:600` silently discards non-image
media. With this design audio is transcribed upstream, so the loop only
receives text — no content-part change needed, and token consumption
stays minimal. The original audio file is retained in media store.

## 4. Data model

### 4.1 `TranscriptionProvider` interface

A structural interface (Protocol). Existing providers already conform;
`LocalWhisperProvider` will too.

```python
class TranscriptionProvider(Protocol):
    async def transcribe(self, file_path: str | Path) -> str: ...
```

### 4.2 `LocalWhisperProvider` (new)

In-process faster-whisper, optional dependency. Lazy import so a missing
extra does not break import of the module.

```python
class LocalWhisperProvider:
    def __init__(self, model="large-v3", device="auto",
                 compute_type="auto", language=None, download_root=None): ...
    async def transcribe(self, file_path) -> str: ...
    # runs the synchronous model in asyncio.to_thread(...)
```

### 4.3 Configuration schema

New global `transcription` section. Channel-level
`transcription_provider` / `transcription_api_key` /
`transcription_language` keys continue to work as overrides of the global
values for that channel (no breakage to existing Telegram config).

```jsonc
{
  "transcription": {
    "enabled": true,
    "mode": "auto",                  // "auto" | "preview" | "off"
    "provider": "local",             // "local" | "openai" | "groq" | "http"
    "language": null,                // ISO-639-1; null = Whisper auto-detect
    "local": {
      "model": "large-v3",           // tiny|base|small|medium|large-v3|large-v3-turbo
      "device": "auto",              // auto|cpu|cuda
      "compute_type": "auto",        // auto|int8|int8_float16|float16|float32
      "download_root": null          // null = default cache dir
    },
    "http": {
      "base_url": null,              // e.g. http://localhost:8080/v1
      "api_key": null,
      "model": null
    },
    "openai": { "api_key": null, "api_base": null },
    "groq":   { "api_key": null, "api_base": null },
    "max_duration_s": 600,
    "cache_transcripts": true
  }
}
```

### 4.4 Provider resolution (`TranscriptionService`)

1. `provider == "local"` → `LocalWhisperProvider` (requires `[stt]` extra).
2. `provider == "groq"` → `GroqTranscriptionProvider`.
3. `provider == "openai"` → `OpenAITranscriptionProvider`.
4. `provider == "http"` → `OpenAITranscriptionProvider` constructed with
   `api_base=http.base_url`, `api_key=http.api_key`,
   `model=http.model`. Reuses the existing HTTP client; any
   OpenAI-compatible server works.

### 4.5 Media store layout

Audio and transcript are co-located for cache and audit:

```
~/.durin/media/<channel>/
  2026-06-19-<uuid>.opus           # original audio
  2026-06-19-<uuid>.opus.txt       # cached transcript (same stem + .txt)
  2026-06-19-<uuid>.opus.meta.json # {model, language, duration_s, transcribed_at}
```

`TranscriptionService.transcribe(path)` is idempotent: if the `.txt`
exists it returns the cached text without retranscribing. The `.meta.json`
records which model produced the transcript so a future re-transcribe
with a different model can decide whether to invalidate the cache.

### 4.6 Recording format (webui mic)

`MediaRecorder` prefers `audio/webm;codecs=opus` (Chrome/Firefox);
Safari falls back to `audio/mp4`. faster-whisper and cloud providers
accept both natively, so there is no client-side transcoding.

## 5. Webui flow

### 5.1 Attach audio (drag-drop / button)

New hook `useAttachedAudio.ts`, mirroring `useAttachedImages.ts`:
- MIME whitelist: `audio/mpeg`, `audio/ogg`, `audio/opus`, `audio/wav`,
  `audio/webm`, `audio/x-m4a`, `audio/aac`, `audio/flac`.
- Cap: `MAX_AUDIO_PER_MESSAGE = 1`.
- Size cap: 25 MB (aligns with Groq/OpenAI and existing video cap).
- Lifecycle states: `transcribing → ready | error` (instead of
  `encoding`).
- Chip: mini `<audio controls>` player + duration + status.

### 5.2 Microphone recording

New `<MicButton />` in the composer:
- `navigator.mediaDevices.getUserMedia({ audio: true })` — native Web API,
  no external libs.
- `MediaRecorder` with `pickAudioMime()` preferring webm/opus.
- While recording: pulsing red button + elapsed timer (`0:03`); waveform
  deferred to v2.
- On stop: produce a `Blob`, inject into `useAttachedAudio` as if it
  were an attached file (same downstream flow).
- Permission denied → clear toast "Allow microphone access".

### 5.3 Transcription + preview (the transparency core)

Three modes controlled by `transcription.mode`:

- **`auto` (default):** attach/record → chip shows "Transcribing…" → on
  completion the transcript is inserted into the input box as an editable
  quoted block; the chip flips to "✓ transcribed" with the player. User
  can correct before pressing Enter. Only text is sent.
- **`preview`:** transcript appears in a preview bubble with
  `[Accept] [Re-transcribe] [Discard]`. Explicit accept required.
- **`off`:** audio is attached raw and not transcribed; stored in media
  with a note so the agent can later use `interpret_audio` if an aux
  multimodal model is configured.

### 5.4 WebSocket protocol

New envelope, extending the existing message flow:

```
→ { type: "audio_transcribe", chat_id, media: [{data_url, name}] }
← { type: "audio_transcript", chat_id, name, transcript, error? }
```

The backend responds asynchronously when `TranscriptionService`
completes; the chip reacts to the reply. Keeps the existing WS pattern;
no HTTP polling introduced.

## 6. TUI flow

### 6.1 Attach via drag-drop

`durin/cli/dragdrop.py` already copies audio (`_AUDIO_EXTS`) into
`workspace/.media/` and populates `InboundMessage.media`. The change is
to route those audio paths through `TranscriptionService` **before** the
loop receives the message:
- `mode == "auto"`: insert the transcript into the input text as an
  editable quote `[transcripción]: "..."`.
- `mode == "preview"`: prompt "Transcription detected: Accept / Edit /
  Discard".
- `mode == "off"`: leave the path; the agent may invoke `interpret_audio`
  if an aux model is configured.

### 6.2 Record via `/voice` (or `Ctrl+R`)

- Uses `sounddevice` (cross-platform; PortAudio-based) to record WAV.
- Banner: "🔴 Recording… (0:03) — press Enter to stop" (waveform in v2).
- On stop: saves `workspace/.media/<sha>.wav`, calls
  `TranscriptionService`, same flow as attach.
- Dependency: `sounddevice` provided by the optional `[voice]` extra.
  Linux may require `libportaudio2` (documented in INSTALL.md).

## 7. Channels (Telegram, WhatsApp, Slack, …)

Refactor `BaseChannel.transcribe_audio` (`durin/channels/base.py:52`) to
delegate to a shared `TranscriptionService` injected into each channel:

```python
# Before
text = await self.transcribe_audio(path)   # ad hoc; audio file discarded
content = f"{content}\n[voice] {text}"

# After
result = await self.transcription.transcribe_and_cache(path)
content = f"{content}\n[voice] {result.text}"
# audio + transcript retained under ~/.durin/media/<channel>/
```

Channel-level `transcription_provider` / `transcription_api_key` /
`transcription_language` continue to override the global `transcription.*`
for that channel. Telegram's existing behaviour is preserved, except the
`.ogg` is now retained alongside the transcript.

The `interpret_audio` tool (`durin/agent/tools/interpret_audio.py`) is
**orthogonal and unchanged** — it is the path for sending audio natively
to a multimodal aux model when the user explicitly wants that.

## 8. Operations

### 8.1 `durin doctor` checks

New `stt.*` checks mirroring existing provider checks:
- `stt.installed` — is `faster-whisper` importable?
- `stt.model_cached` — is the configured model on disk? (warn: first
  transcription will be slow if not)
- `stt.round_trip` (with `--ping-model`) — transcribe a 2 s "hello world"
  clip, assert the transcript contains "hello", report latency.
- `stt.cloud_keys` — if `provider in {groq, openai}`, is an API key set?
- `stt.mic` (TUI only) — does `sounddevice` import and is there an input
  device?

Output follows the existing ✓/✗ + fix-hint style.

### 8.2 Dependencies

- `[stt]` extra → `faster-whisper` (pulls CTranslate2; prebuilt wheels for
  x64/arm64 on Linux/macOS/Windows — no compilation).
- `[voice]` extra → `sounddevice` (pulls PortAudio; prebuilt for
  macOS/Windows; Linux may need `apt install libportaudio2`).

## 9. Testing

### 9.1 Backend (pytest)

- `tests/providers/test_transcription.py`
  - `LocalWhisperProvider`: mock `WhisperModel` (no weights download in
    CI); assert correct `language`, `model`, `device` forwarding.
  - Provider resolution table in `TranscriptionService`.
  - Cache: transcribe the same path twice → one call; second returns
    cache.
  - `max_duration_s` rejection with a clear message.
- `tests/service/test_transcription_service.py`
  - Idempotency; modes auto/preview/off; error handling (corrupt file,
    provider down).
- `tests/agent/test_context_build.py`
  - Confirm audio in `media` no longer triggers the silent `continue`;
    since audio is transcribed upstream it should not arrive as media.
- `tests/channels/test_*`
  - Update Telegram/WhatsApp tests to verify the `.ogg` is retained and
    the shared service is used.

### 9.2 Frontend (vitest)

- `useAttachedAudio.test.ts` — whitelist, cap, size limit, lifecycle.
- `<MicButton />` — mock `getUserMedia` / `MediaRecorder`; permission
  denied → toast.
- WS `audio_transcript` handler → chip transitions to ready.

### 9.3 Manual smoke test

Record a Spanish and an English message with the microphone; verify the
transcript is correct and editable in all three modes.

## 10. Documentation

- `docs/INSTALL.md` — "Audio transcription" section: `[stt]` for local,
  `[voice]` for TUI recording, PortAudio prerequisite on Linux.
- `docs/config.md` (or the existing config doc) — full `transcription.*`
  schema with examples for local, Groq, OpenAI, and custom `http`
  (mlx-qwen3-asr / vLLM).
- `README.md` — one bullet under "Day-to-day" mentioning voice input in
  webui/TUI.

## 11. Scope

### 11.1 MVP (this feature)

- `TranscriptionProvider` interface + `LocalWhisperProvider`
  (faster-whisper, in-process).
- `TranscriptionService` with cache + three modes.
- `transcription.*` schema + webui config section.
- Webui: attach audio (hook + chips) + record with `<MicButton />`.
- TUI: drag-drop integrated with `TranscriptionService` + `/voice` via
  sounddevice.
- Channels: refactor `BaseChannel` → `TranscriptionService`, retain
  audio.
- `durin doctor` STT checks.
- Backend + frontend tests.
- HTTP passthrough via `provider: "http"`.

### 11.2 Deferred to v2

- Streaming partial transcripts while recording (today: batch on stop).
- Webui waveform / visualizer.
- Multi-speaker diarization.
- Native Qwen3-ASR provider (when a mature CPU runtime exists).
- UI-driven re-transcription with a different model (infra via
  `.meta.json` already supports it).
- Surfacing detected language to the user.

## 12. Risks

- **Cold start of Large V3** (~5-10 s on first transcription): mitigated
  by lazy loading + a chip message "Preparing model… (first time only)".
- **Native dependencies:** CTranslate2 and PortAudio ship prebuilt for
  the three platforms, but Linux without `libportaudio2` will break
  `sounddevice` — `doctor` detects it, INSTALL.md documents it.
- **Safari MediaRecorder** produces mp4 rather than webm; faster-whisper
  and cloud providers accept it, but the frontend MIME detector must be
  tolerant.
