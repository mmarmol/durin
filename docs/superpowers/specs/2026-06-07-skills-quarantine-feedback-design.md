# Skills quarantine: audit feedback, gate reasons, retry reuse

Date: 2026-06-07
Status: approved (design), pending implementation plan
Surface: webui Skills panel + skills backend (judge, gate, quarantine surface)

## Problem

Three issues observed while triaging quarantined skill imports in the webui:

1. **"Audit with LLM" (judge) blocks without feedback, and fails confusingly.**
   The judge runs a single non-streaming `litellm.completion` (multi-second)
   with no UI progress, so it looks frozen; on success with no findings it
   repaints the same "scan clean" line, so it looks like nothing happened.
   When it fails it surfaces the raw `litellm InternalServerError - Connection
   error` text. Verified empirically: the failure is a **transient connection
   error to the model provider, not skill-specific** — a 1.3 KB skill failed
   while a 16 KB one succeeded. The judge only retries once with no backoff.

2. **The panel never explains *why* a skill sits in Pending.** Every import
   lands in quarantine awaiting approval; the gate (`decide_action`) holds it
   for a concrete reason (source not in the trust allowlist, carries code,
   caution/dangerous verdict), plus it may declare dependencies. None of these
   reasons reach the UI — a safe skill shows only "scan clean", never the
   actual reason approval is required.

3. **The AI audit never says what it checked.** The judge returns only
   findings; it ignores even the `===VERDICT===` block its own prompt requests,
   and has no summary. There is no "I verified X, Y, Z and found no / these
   problems", and no visibility into the agent's work while it runs.

## Goals

- Make the audit transparent: live reasoning while it runs, a clear final
  explanation always (with or without findings), and readable, retryable errors.
- Explain in plain language why each pending skill needs approval, beyond
  security.
- Make the judge's LLM call resilient using the **same retry policy as chat**,
  not a hardcoded number.

## Non-goals

- Turning the judge into a multi-step tool-using agent. It stays a single
  streamed LLM call; "show the work" means streaming its reasoning, not a
  tool loop.
- Changing the deterministic `scan_skill` regex pass or the gate semantics
  (`decide_action`). We surface existing decisions; we do not change them.

## Relevant current code

- `durin/security/skill_judge.py` — `judge_skill` (single call, `max_retries=1`,
  parses only `===FINDINGS===`), `_PROMPT`, `_gather_content`, `_BODY_BUDGET=12000`.
- `durin/memory/llm_invoke.py` — `default_llm_invoke`: raw `litellm.completion`,
  no retry/backoff; used by judge, dream, absorb.
- `durin/providers/base.py` — `LLMProvider`: `_CHAT_RETRY_DELAYS=(1,2,4,8,15,30)`
  (6 retries / 7 attempts), `_PERSISTENT_*`, transient-error markers/classifiers,
  `chat_with_retry` / `chat_stream_with_retry`, `chat_stream(on_content_delta,
  on_thinking_delta)`. `config.providers...provider_retry_mode` = standard|persistent.
- `durin/agent/skills_import.py` — `validate_skill` (`carries_code`,
  `code_artifacts`), `decide_action(source, verdict, carries_code, allowlist)
  -> allow|confirm|block`, `declared_install_specs`, `trust_prefix_for`.
- `durin/agent/skills_store.py` — `web_quarantine`, `web_skill_judge`,
  `_import_judge`, `_import_allowlist`.
- `durin/agent/skills_surface.py` — `web_quarantine` builder (emits per-row
  fields).
- `durin/channels/websocket.py` — `/api/skills/{name}/judge` handler; chat
  stream events `reasoning_delta` / `reasoning_end` / `delta` / `progress`.
- `webui/src/components/SkillsView.tsx` — triage pane (`judgeOne`, gate),
  `webui/src/lib/api.ts` (`QuarantineRow`, `judgeSkill`), i18n `skills.*`.

## Empirical findings (verified this session)

- Judge failure reproduced: `litellm InternalServerError - Connection error`
  on both attempts for `web-scraping`; `firecrawl` judged OK. Transient, not
  content-driven.
- `_import_judge()` returns an empty model in the current config → falls back
  to hardcoded `glm-5.1`.
- glm-5.1 via z.ai **streams `reasoning_content`** (473 chunks, reasoning first,
  then 36 content chunks). The hybrid live-reasoning line is viable with this model.

## Design

### Section 1 — Retry reuse (#1)

Single source of truth for the LLM retry policy, shared by chat and memory/judge
calls.

- Extract the policy into reusable helpers (module-level, not new magic numbers):
  - the delay schedule per mode (standard: `(1,2,4,8,15,30)`; persistent: capped
    at `_PERSISTENT_MAX_DELAY` with the identical-error limit),
  - the transient-vs-non-retryable classifier (the existing markers / status
    codes / 429 token logic in `LLMProvider`).
  Both `LLMProvider.chat_with_retry` and `default_llm_invoke` consume these, so
  there is one definition. Behavior of `chat_with_retry` is preserved.
- `default_llm_invoke` wraps its `litellm.completion` in the standard/persistent
  loop, reading `provider_retry_mode` from config. This fixes judge + dream +
  absorb at once.
- `judge_skill` drops its hardcoded transient-retry; it keeps a small
  **parse-retry** (re-send on malformed marker output), which is orthogonal.
- Errors are classified and returned with a machine code (e.g. `unreachable`,
  `no_model`, `parse`) so the UI shows a readable, localized message plus Retry,
  instead of raw `litellm` text.

Open decision (low-risk default chosen): the shared helpers live next to the
provider policy (extracted from `base.py` into module-level functions in
`durin/providers/base.py` or a sibling `retry.py`), consumed by both call sites.

### Section 2 — Gate reasons (#2)

`web_quarantine` rows gain:

- `needs`: `allow | confirm | block` (the `decide_action` outcome).
- `reasons[]`: structured, each `{ code, detail }`, where `code` is one of:
  - `untrusted_source` — source prefix not in the trust allowlist,
  - `carries_code` — has `scripts/` or a `metadata.<vendor>.install`,
  - `declared_deps` — declares install specs (info only, never auto-run),
  - `verdict_caution` / `verdict_dangerous` — security verdict raised the gate.

Computed from `validate_skill`, `decide_action`, `_import_allowlist`,
`declared_install_specs`, and the scan verdict. Existing fields
(`verdict`, `findings`, `trust_prefix`, `install_specs`) are unchanged.

`SkillsView` triage pane adds a **"Why it's here"** section (separate from
Security) rendering the reasons in plain, localized language. `api.ts`
`QuarantineRow` gains `needs` and `reasons`.

### Section 3 — Hybrid AI audit: live reasoning + final summary (#3)

**Prompt / parsing.** Extend `_PROMPT` to require `===SUMMARY===` (1–3 sentences:
what was examined + the conclusion) and parse the already-requested
`===VERDICT===` alongside `===FINDINGS===`. The judge result becomes
`{ verdict, findings, summary }`, persisted into `.scan.json` and returned by
`web_skill_judge`.

**Streaming transport.** The audit runs as a **streamed operation over the
websocket the webui already holds**, reusing the chat streaming machinery
(`chat_stream` with `on_thinking_delta` / `on_content_delta`) and the existing
event vocabulary, scoped by skill name:
- `reasoning_delta` chunks → the panel shows the **latest reasoning line** live,
- on completion → a terminal event carries `{ verdict, findings, summary }`,
- the panel replaces the live line with the **structured summary** + findings
  (or an explicit "no problems found").

Recommended: model the audit as a websocket-streamed request like a chat turn
(one channel, reuses `chat_stream`). Alternative considered and rejected unless
needed: keep HTTP GET `/judge` to start and push deltas over the ws with a
job id (extra correlation for no real gain).

**Degradation.** If a provider streams no `reasoning_content`, the live line
falls back to content tokens; the final structured summary is always produced
(the Section-3 value never depends on live reasoning being available).

## Error handling

- Transient model errors: retried per the shared policy (Section 1); after
  exhaustion the panel shows a localized "couldn't reach the audit model —
  retry" with a Retry button. No raw provider text in the UI.
- Parse failures: small parse-retry in `judge_skill`; on final failure, the
  deterministic scan verdict still stands and is shown.
- Streaming drop mid-audit: the panel surfaces a readable interrupted state and
  offers Retry; `.scan.json` is only written on a complete result.

## Testing

Backend:
- Retry policy: transient error retries and eventually succeeds; non-retryable
  (e.g. quota) does not retry; persistent mode honors its caps. One definition
  exercised by both chat and `default_llm_invoke`.
- `reasons` computation: untrusted source, carries_code, declared_deps,
  caution/dangerous each produce the right codes; an allowlisted safe skill
  with no code yields `needs=allow` and no blocking reasons.
- Judge parsing: SUMMARY + VERDICT + FINDINGS parsed; missing SUMMARY tolerated;
  malformed output triggers parse-retry then degrades to the deterministic scan.

Frontend (vitest):
- Triage pane states: idle → live reasoning line → final summary; error → Retry;
  "Why it's here" renders each reason code in plain language.
- Existing SkillsView behavior tests stay green (or are updated to the new
  contract where the triage pane changes).

## Rollout

Frontend + skills-backend change; no migration. `.scan.json` gains a `summary`
field (older files without it render with the live/empty fallback). CI runs
pytest only (webui not built in CI), so backend tests gate the PR; webui is
verified locally (vitest + build + live against the running gateway).
