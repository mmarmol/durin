# Skills Quarantine Feedback — Plan B (live reasoning streaming) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream the AI audit's reasoning live into the triage pane — show the latest reasoning line while it runs, then the structured final summary — by reusing the chat's `reasoning_delta` event vocabulary over the websocket the webui already holds.

**Architecture:** Fully async streaming. `litellm.acompletion(stream=True)` yields `reasoning_content`/`content` deltas; a new async streaming judge forwards reasoning chunks via callback; a new `skill_judge` websocket envelope runs it and emits `reasoning_delta` / `reasoning_end` / `skill_audit_done` keyed by a synthetic `chat_id` = `audit:<name>`; the webui subscribes via the existing `onChat` fan-out and renders the live line + final summary in the triage pane. Builds on Plan A (JudgeOutcome, gate reasons, summary parsing).

**Tech Stack:** Python 3.11 / pytest / asyncio / litellm, React + TypeScript / vitest.

**Spec:** `docs/superpowers/specs/2026-06-07-skills-quarantine-feedback-design.md` (Section 3, streaming layer).

**Depends on:** Plan A merged into this branch (JudgeOutcome, `_retry_llm_call`, `web_skill_judge`, gate reasons, triage UI).

---

## File Structure

- `durin/memory/llm_invoke.py` — add `_aretry_llm_call` (async mirror of `_retry_llm_call`, same `LLMProvider` policy) and `default_llm_invoke_astream(prompt, *, model, on_reasoning, on_content)` using `litellm.acompletion(stream=True)`; returns the full assembled text.
- `durin/security/skill_judge.py` — add `async def judge_skill_astream(skill_dir, *, ainvoke_stream, model, max_severity, on_reasoning)` → `JudgeOutcome`, forwarding reasoning chunks to `on_reasoning`.
- `durin/agent/skills_store.py` — extract `_persist_judge_result(qdir, source, verdict, findings, summary)` from `web_skill_judge` (shared by HTTP + ws paths).
- `durin/channels/websocket.py` — new envelope `type == "skill_judge"`; async handler streams `reasoning_delta`/`reasoning_end`/`skill_audit_done` on `audit:<name>`, persists via `_persist_judge_result`.
- `webui/src/lib/durin-client.ts` — `judgeStream(name)` sends `{type:"skill_judge", chat_id, name}`.
- `webui/src/components/SkillsView.tsx` — audit click subscribes to `audit:<name>`, renders the live reasoning line, then the final summary on `skill_audit_done` (replacing the HTTP path for the webui).
- Tests across the above.

---

## Task 0: De-risk — verify async streaming yields reasoning_content

- [ ] **Step 1: Run a one-off async streaming probe**

```bash
cd /Users/marcelo/.durin/workspace && /Users/marcelo/git_personal/durin/.venv/bin/python - <<'PY'
import asyncio, litellm
from durin.security.secrets import get_secret_store
key = get_secret_store().get("ZHIPU_API_KEY").value
async def go():
    resp = await litellm.acompletion(
        model="openai/glm-5.1",
        messages=[{"role":"user","content":"Is rm -rf / dangerous? Think briefly."}],
        api_key=key, api_base="https://api.z.ai/api/coding/paas/v4",
        temperature=0.1, stream=True)
    r=c=0
    async for chunk in resp:
        d = chunk.choices[0].delta
        if getattr(d,"reasoning_content",None): r+=1
        if d.content: c+=1
    print("reasoning chunks:", r, "content chunks:", c)
asyncio.run(go())
PY
```

Expected: non-zero reasoning chunks. (Run from worktree cwd so `durin` resolves to the worktree.)
If reasoning is zero, the live line falls back to content tokens — note it and proceed; the design degrades gracefully.

---

## Task 1: Async streaming LLM invoke

**Files:**
- Modify: `durin/memory/llm_invoke.py`
- Test: `tests/memory/test_llm_invoke_astream.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_llm_invoke_astream.py
import asyncio
import pytest
from durin.memory import llm_invoke


class _FakeChunk:
    def __init__(self, rc=None, c=None):
        self.choices = [type("C", (), {"delta": type("D", (), {"reasoning_content": rc, "content": c})()})()]


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for ch in self._chunks:
                yield ch
        return gen()


def test_astream_forwards_reasoning_and_assembles_text(monkeypatch):
    chunks = [_FakeChunk(rc="think"), _FakeChunk(rc="ing"), _FakeChunk(c="ans"), _FakeChunk(c="wer")]

    async def fake_acompletion(**kwargs):
        assert kwargs["stream"] is True
        return _FakeStream(chunks)

    monkeypatch.setattr(llm_invoke, "_acompletion", fake_acompletion)
    seen = []

    async def go():
        return await llm_invoke.default_llm_invoke_astream(
            "p", model="m", on_reasoning=lambda s: seen.append(s), on_content=None
        )

    text = asyncio.run(go())
    assert text == "answer"
    assert "".join(seen) == "thinking"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/marcelo/git_personal/durin/.claude/worktrees/skills-fixes && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/memory/test_llm_invoke_astream.py -q`
Expected: FAIL — `default_llm_invoke_astream` / `_acompletion` undefined.

- [ ] **Step 3: Implement**

Add to `durin/memory/llm_invoke.py` (after `_retry_llm_call`):

```python
async def _aretry_llm_call(acall, *, mode: str = "standard"):
    """Async mirror of :func:`_retry_llm_call` — same LLMProvider policy."""
    import asyncio

    from durin.providers.base import LLMProvider

    delays = LLMProvider._CHAT_RETRY_DELAYS
    attempt = 0
    identical = 0
    last_text: str | None = None
    while True:
        try:
            return await acall()
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if not LLMProvider._is_transient_error(text):
                raise
            if mode == "persistent":
                identical = identical + 1 if text == last_text else 1
                last_text = text
                if identical >= LLMProvider._PERSISTENT_IDENTICAL_ERROR_LIMIT:
                    raise
                delay = min(delays[min(attempt, len(delays) - 1)], LLMProvider._PERSISTENT_MAX_DELAY)
            else:
                if attempt >= len(delays):
                    raise
                delay = delays[attempt]
            logger.info("memory LLM (stream) transient error, retrying in %ss: %s", delay, text)
            await asyncio.sleep(delay)
            attempt += 1


async def _acompletion(**kwargs):
    """Indirection seam so tests can stub the litellm async call."""
    import litellm

    return await litellm.acompletion(**kwargs)


async def default_llm_invoke_astream(
    prompt: str,
    *,
    model: str = "glm-5.1",
    temperature: float = 0.1,
    on_reasoning=None,
    on_content=None,
) -> str:
    """Stream a completion: forward ``reasoning_content`` chunks to ``on_reasoning``
    and ``content`` chunks to ``on_content`` (each may be sync or async), returning
    the assembled answer text. The stream OPEN is retried per the chat policy."""
    from durin.security.secrets import get_secret_store

    entry = get_secret_store().get("ZHIPU_API_KEY")
    if entry is None:
        raise DreamError("ZHIPU_API_KEY missing from secret store")
    api_key = entry.value

    try:
        from durin.config.loader import load_config
        mode = load_config().defaults.provider_retry_mode
    except Exception:  # noqa: BLE001
        mode = "standard"

    async def _emit(cb, text):
        if cb is None or not text:
            return
        r = cb(text)
        if hasattr(r, "__await__"):
            await r

    async def _open():
        return await _acompletion(
            model=f"openai/{model}",
            messages=[{"role": "user", "content": prompt}],
            api_key=api_key,
            api_base="https://api.z.ai/api/coding/paas/v4",
            temperature=temperature,
            stream=True,
        )

    stream = await _aretry_llm_call(_open, mode=mode)
    parts: list[str] = []
    async for chunk in stream:
        delta = chunk.choices[0].delta
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            await _emit(on_reasoning, rc)
        c = getattr(delta, "content", None)
        if c:
            parts.append(c)
            await _emit(on_content, c)
    return "".join(parts)
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add durin/memory/llm_invoke.py tests/memory/test_llm_invoke_astream.py
git commit -m "feat(memory): async streaming LLM invoke (reasoning + content callbacks)"
```

---

## Task 2: Async streaming judge

**Files:**
- Modify: `durin/security/skill_judge.py`
- Test: `tests/security/test_skill_judge_astream.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_skill_judge_astream.py
import asyncio
from pathlib import Path
from durin.security import skill_judge


def _skill(tmp_path):
    d = tmp_path / "demo"; d.mkdir()
    (d / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\nbody\n", encoding="utf-8")
    return d


def test_judge_astream_streams_reasoning_and_parses(tmp_path):
    raw = ("===SUMMARY===\nReviewed; clean.\n===VERDICT===\nsafe\n===FINDINGS===\nnone\n===END===\n")

    async def fake_astream(prompt, *, model, on_reasoning=None, on_content=None):
        for piece in ("look", "ing"):
            r = on_reasoning(piece)
            if hasattr(r, "__await__"):
                await r
        return raw

    seen = []

    async def go():
        return await skill_judge.judge_skill_astream(
            _skill(tmp_path), ainvoke_stream=fake_astream, model="m",
            on_reasoning=lambda s: seen.append(s),
        )

    out = asyncio.run(go())
    assert out.verdict == "safe"
    assert out.summary == "Reviewed; clean."
    assert "".join(seen) == "looking"
```

- [ ] **Step 2: Run to verify it fails** — Expected: FAIL (`judge_skill_astream` undefined).

- [ ] **Step 3: Implement** — add to `durin/security/skill_judge.py`:

```python
async def judge_skill_astream(skill_dir: Path, *, ainvoke_stream, model: str,
                              max_severity: str = "caution", on_reasoning=None) -> JudgeOutcome:
    """Streaming variant of :func:`judge_skill`: forwards the model's reasoning to
    ``on_reasoning`` as it arrives, then parses the assembled text into a
    JudgeOutcome. Raises JudgeError if the markers are missing."""
    if max_severity not in _SEV:
        max_severity = "caution"
    name, content = _gather_content(skill_dir)
    if not content.strip():
        return JudgeOutcome()
    prompt = _PROMPT.format(name=name, content=content)
    raw = await ainvoke_stream(prompt, model=model, on_reasoning=on_reasoning, on_content=None)
    raw = raw if isinstance(raw, str) else str(raw)
    return _parse_outcome(raw, max_severity)
```

- [ ] **Step 4: Run to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add durin/security/skill_judge.py tests/security/test_skill_judge_astream.py
git commit -m "feat(skills): async streaming judge (judge_skill_astream)"
```

---

## Task 3: Extract shared judge-result persistence

**Files:**
- Modify: `durin/agent/skills_store.py`
- Test: `tests/agent/test_persist_judge_result.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_persist_judge_result.py
import json
from pathlib import Path
import durin.agent.skills_store as ss


def test_persist_writes_scan_json(tmp_path):
    q = tmp_path / "demo"; q.mkdir()
    (q / ".scan.json").write_text(json.dumps({"source": "github:o/r", "verdict": "safe", "findings": []}), encoding="utf-8")
    ss._persist_judge_result(q, "github:o/r", "caution",
                             [{"category": "llm:x", "severity": "caution", "where": "SKILL.md", "detail": "y"}],
                             "Found one issue.")
    stored = json.loads((q / ".scan.json").read_text())
    assert stored["verdict"] == "caution"
    assert stored["summary"] == "Found one issue."
    assert stored["findings"][0]["category"] == "llm:x"
```

- [ ] **Step 2: Run to verify it fails** — Expected: FAIL (`_persist_judge_result` undefined).

- [ ] **Step 3: Implement** — add the helper to `durin/agent/skills_store.py` and call it from `web_skill_judge`:

```python
def _persist_judge_result(qdir, source: str, verdict: str, findings: list, summary: str) -> None:
    """Write the merged judge result to the quarantine ``.scan.json`` (shared by
    the HTTP and websocket audit paths)."""
    import json as _json
    (qdir / ".scan.json").write_text(
        _json.dumps({"source": source, "verdict": verdict, "findings": findings, "summary": summary}),
        encoding="utf-8",
    )
```

In `web_skill_judge`, replace the inline `sj.write_text(_json.dumps({...}))` block with:

```python
    _persist_judge_result(qdir, source, merged.verdict, findings, outcome.summary)
```

(Keep the `source` lookup from the existing `.scan.json` above it.)

- [ ] **Step 4: Run to verify it passes** + the existing `tests/agent/test_skill_judge_web.py` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_store.py tests/agent/test_persist_judge_result.py
git commit -m "refactor(skills): extract _persist_judge_result (shared by HTTP + ws audit)"
```

---

## Task 4: Websocket `skill_judge` streaming envelope

**Files:**
- Modify: `durin/channels/websocket.py` (envelope dispatch near line 2898; add a handler method)
- Test: `tests/channels/test_skill_judge_ws.py` (create)

- [ ] **Step 1: Write the failing test** (drives the handler with fakes; asserts the emitted event sequence)

```python
# tests/channels/test_skill_judge_ws.py
import asyncio
import json
from pathlib import Path

import durin.channels.websocket as ws_mod
from durin.security.skill_judge import JudgeOutcome


class _Server:
    """Minimal harness exercising WebSocketChannel._run_skill_audit in isolation."""

    def __init__(self, tmp_path):
        self.events = []
        self._ws = ws_mod.WebSocketChannel.__new__(ws_mod.WebSocketChannel)
        self._ws._endpoint_workspace = lambda: tmp_path

    async def send_reasoning_delta(self, chat_id, delta, metadata=None):
        self.events.append(("reasoning_delta", chat_id, delta))

    async def send_reasoning_end(self, chat_id, metadata=None):
        self.events.append(("reasoning_end", chat_id))

    async def _send_event(self, conn, event, **kw):
        self.events.append((event, kw))


def _quarantined(tmp_path):
    q = tmp_path / ".durin" / "import-quarantine" / "demo"
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\nbody\n", encoding="utf-8")
    (q / ".scan.json").write_text(json.dumps({"source": "github:o/r", "verdict": "safe", "findings": []}), encoding="utf-8")
    return tmp_path


def test_run_skill_audit_streams_then_done(tmp_path, monkeypatch):
    ws = _quarantined(tmp_path)
    srv = _Server(ws)
    # bind real methods onto the bare instance
    srv._ws.send_reasoning_delta = srv.send_reasoning_delta
    srv._ws.send_reasoning_end = srv.send_reasoning_end
    srv._ws._send_event = srv._send_event

    async def fake_astream(skill_dir, *, ainvoke_stream, model, max_severity, on_reasoning):
        await on_reasoning("look")
        await on_reasoning("ing")
        return JudgeOutcome(findings=[], verdict="safe", summary="Clean.")

    monkeypatch.setattr("durin.security.skill_judge.judge_skill_astream", fake_astream)
    monkeypatch.setattr(ws_mod, "_import_judge_cfg", lambda: ("", "caution"), raising=False)

    asyncio.run(ws_mod.WebSocketChannel._run_skill_audit(srv._ws, object(), "demo"))

    kinds = [e[0] for e in srv.events]
    assert "reasoning_delta" in kinds
    assert "reasoning_end" in kinds
    assert any(e[0] == "skill_audit_done" for e in srv.events)
    done = next(e for e in srv.events if e[0] == "skill_audit_done")
    assert done[1]["summary"] == "Clean."
    assert done[1]["judged"] is True
    stored = json.loads((ws / ".durin" / "import-quarantine" / "demo" / ".scan.json").read_text())
    assert stored["summary"] == "Clean."
```

- [ ] **Step 2: Run to verify it fails** — Expected: FAIL (`_run_skill_audit` undefined).

- [ ] **Step 3: Implement the handler + dispatch**

Add the dispatch branch in the envelope handler (after the `if t == "message":` block, near line 2942):

```python
        if t == "skill_judge":
            name = envelope.get("name")
            if not isinstance(name, str) or not name:
                await self._send_event(connection, "error", detail="missing skill name")
                return
            chat_id = f"audit:{name}"
            self._attach(connection, chat_id)
            await self._run_skill_audit(connection, name)
            return
```

Add the handler method (near `_handle_skill_judge`):

```python
    async def _run_skill_audit(self, connection, name: str) -> None:
        """Stream an on-demand LLM audit of a quarantined skill: reasoning deltas
        on ``audit:<name>`` (reusing the chat reasoning stream), then a terminal
        ``skill_audit_done`` with the structured outcome. Persists .scan.json."""
        import json as _json

        from durin.agent import skills_store as ss
        from durin.memory.llm_invoke import default_llm_invoke_astream
        from durin.providers.base import LLMProvider
        from durin.security.skill_judge import JudgeError, judge_skill_astream
        from durin.security.skill_scan import ScanReport, scan_skill

        chat_id = f"audit:{name}"
        workspace = self._endpoint_workspace()
        qdir = Path(workspace) / ".durin" / "import-quarantine" / name
        if not (qdir / "SKILL.md").is_file():
            await self._send_event(connection, "skill_audit_done", name=name,
                                   judged=False, error_code="not_found")
            return
        _, model, max_sev = ss._import_judge()
        det = scan_skill(qdir)

        async def on_reasoning(text: str) -> None:
            await self.send_reasoning_delta(chat_id, text)

        try:
            outcome = await judge_skill_astream(
                qdir, ainvoke_stream=default_llm_invoke_astream,
                model=model or "glm-5.1", max_severity=max_sev, on_reasoning=on_reasoning,
            )
        except JudgeError as exc:
            await self.send_reasoning_end(chat_id)
            code = "parse" if "parse" in str(exc).lower() else "unreachable"
            await self._send_event(connection, "skill_audit_done", name=name,
                                   judged=False, error_code=code)
            return
        except Exception as exc:  # noqa: BLE001
            await self.send_reasoning_end(chat_id)
            code = "unreachable" if LLMProvider._is_transient_error(str(exc)) else "no_model"
            await self._send_event(connection, "skill_audit_done", name=name,
                                   judged=False, error_code=code)
            return

        await self.send_reasoning_end(chat_id)
        merged = ScanReport(findings=det.findings + outcome.findings)
        findings = [{"category": f.category, "severity": f.severity, "where": f.where,
                     "detail": f.detail} for f in merged.findings]
        source = name
        sj = qdir / ".scan.json"
        if sj.is_file():
            try:
                source = _json.loads(sj.read_text()).get("source", name)
            except Exception:  # noqa: BLE001
                pass
        ss._persist_judge_result(qdir, source, merged.verdict, findings, outcome.summary)
        await self._send_event(connection, "skill_audit_done", name=name, judged=True,
                               verdict=merged.verdict, findings=findings, summary=outcome.summary)
```

Note: the test stubs `judge_skill_astream`; the `_send_event` must include a `chat_id` so the webui routes it — add `chat_id=chat_id` to both `skill_audit_done` emits. (Update the test's `done[1]` access accordingly: `kw["summary"]`.)

- [ ] **Step 4: Run to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add durin/channels/websocket.py tests/channels/test_skill_judge_ws.py
git commit -m "feat(skills): websocket skill_judge envelope streams audit reasoning + done"
```

---

## Task 5: webui client — `judgeStream`

**Files:**
- Modify: `webui/src/lib/durin-client.ts`

- [ ] **Step 1: Add the send method** (near `sendMessage`):

```typescript
  /** Kick off a streaming skill audit. Reasoning arrives as ``reasoning_delta``
   *  on ``audit:<name>``; the terminal ``skill_audit_done`` carries the result.
   *  Subscribe with ``onChat("audit:" + name, handler)`` before calling. */
  judgeStream(name: string): void {
    const chatId = `audit:${name}`;
    this.attach(chatId);
    this.queueSend({ type: "skill_judge", chat_id: chatId, name });
  }
```

(If `queueSend` is private but `sendMessage` uses it, this method lives in the same class so it has access.)

- [ ] **Step 2: Typecheck**

Run: `cd webui && npx tsc -p tsconfig.build.json --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add webui/src/lib/durin-client.ts
git commit -m "feat(webui): DurinClient.judgeStream — start a streaming skill audit"
```

---

## Task 6: SkillsView — live reasoning line + final summary

**Files:**
- Modify: `webui/src/components/SkillsView.tsx`
- Test: `webui/src/tests/skills-view.test.tsx` (extend)

- [ ] **Step 1: Write the failing test** (drives a fake client that emits reasoning_delta then skill_audit_done)

```typescript
  it("streams audit reasoning then shows the final summary", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([
      { name: "firecrawl", status: "quarantined", source: "github:o/r", verdict: "safe", findings: [],
        needs: "confirm", reasons: [{ code: "untrusted_source", detail: "github:o/r" }] },
    ]);

    // a fake client whose judgeStream replays reasoning then done to the onChat handler
    const handlers: Record<string, (ev: unknown) => void> = {};
    const client = {
      onChat: (id: string, h: (ev: unknown) => void) => { handlers[id] = h; return () => {}; },
      judgeStream: (name: string) => {
        const id = `audit:${name}`;
        handlers[id]?.({ event: "reasoning_delta", chat_id: id, text: "inspecting scripts" });
        handlers[id]?.({ event: "skill_audit_done", chat_id: id, name, judged: true,
          summary: "Reviewed; no injection.", findings: [], verdict: "safe" });
      },
    };

    const user = userEvent.setup();
    render(
      <ClientProvider client={client as unknown as import("@/lib/durin-client").DurinClient} token="tok">
        <SkillsView />
      </ClientProvider>,
    );
    await screen.findByText("clean");
    await user.click(screen.getByRole("button", { name: /pending/i }));
    await user.click(await screen.findByRole("button", { name: /firecrawl/i }));
    await user.click(screen.getByRole("button", { name: /audit with llm/i }));

    expect(await screen.findByText(/Reviewed; no injection/i)).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run to verify it fails** — Expected: FAIL (still uses HTTP `judgeSkill`; no streaming).

- [ ] **Step 3: Implement** — switch `judgeOne` to the streaming client path.

Add a live-line state near `auditMsg`:

```typescript
  const [auditLive, setAuditLive] = useState<string>("");
```

Pull `client` from the provider (the hook already returns it): change `const { token } = useClient();` to `const { token, client } = useClient();`.

Replace `judgeOne` so it subscribes to the audit stream and renders the live line, finalizing on `skill_audit_done`:

```typescript
  const judgeOne = useCallback(
    (name: string) => {
      setActing(name);
      setAuditMsg(null);
      setAuditLive("");
      const id = `audit:${name}`;
      const off = client.onChat(id, (ev: { event?: string; text?: string; judged?: boolean; summary?: string; error_code?: string }) => {
        if (ev.event === "reasoning_delta" && ev.text) {
          setAuditLive((prev) => (prev + ev.text).slice(-280));
        } else if (ev.event === "skill_audit_done") {
          off();
          setAuditLive("");
          setActing(null);
          if (ev.judged) {
            setAuditMsg({ kind: "summary", text: ev.summary?.trim() || t("skills.audit.clean") });
          } else {
            setAuditMsg({ kind: "error", text: t(`skills.audit.${ev.error_code ?? "unreachable"}`) });
          }
          void refresh();
        }
      });
      client.judgeStream(name);
    },
    [client, refresh, t],
  );
```

Render the live line in the audit block (the `acting === triageRow.name` branch added in Plan A) — show the streamed reasoning under the spinner:

```tsx
                  {acting === triageRow.name ? (
                    <div className="mt-3">
                      <p className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                        {t("skills.audit.running")}
                      </p>
                      {auditLive ? (
                        <p className="mt-1 line-clamp-2 text-[11px] italic text-muted-foreground/80">
                          {auditLive}
                        </p>
                      ) : null}
                    </div>
                  ) : auditMsg ? (
                    /* ...unchanged summary/error block from Plan A... */
```

(`judgeSkill` HTTP import can stay for non-webui callers but is no longer used here; remove its import if it becomes unused to satisfy lint.)

- [ ] **Step 4: Run to verify it passes** — `cd webui && npx vitest run src/tests/skills-view.test.tsx` — Expected: PASS (all, including the prior Plan A audit test which must be updated to the fake-client shape or kept HTTP-free).

- [ ] **Step 5: Build + commit**

```bash
cd webui && npm run build && cd ..
git add webui/src/components/SkillsView.tsx webui/src/tests/skills-view.test.tsx
git commit -m "feat(webui): live audit reasoning line + streamed final summary in triage"
```

---

## Task 7: Verification

- [ ] **Step 1:** `pytest tests/ -q --maxfail=5` → PASS.
- [ ] **Step 2:** `cd webui && npx vitest run && npm run build` → PASS, clean.
- [ ] **Step 3: Live** — run the worktree gateway so the new `skill_judge` envelope exists, then dev server proxied to it; click "Audit with LLM" on a pending skill and confirm the live reasoning line streams, then the final summary appears. (Requires the worktree backend serving; coordinate with the user since the default gateway is the frozen install.)

---

## Self-Review notes

- **Spec coverage:** Section 3 streaming layer → Tasks 1 (async invoke), 2 (async judge), 4 (ws transport), 6 (UI live line). Persistence DRY → Task 3.
- **Type consistency:** `JudgeOutcome` (Plan A) reused in Tasks 2/4. `skill_audit_done` fields (`judged`, `verdict`, `findings`, `summary`, `error_code`, `chat_id`) match between Task 4 emit and Task 6 handler. `audit:<name>` chat_id namespace identical in client (Task 5), server (Task 4), UI (Task 6).
- **Degradation:** if `reasoning_content` is absent, `auditLive` simply stays empty; the spinner + final summary still work (Task 0 verifies the common case).
- **Risk:** the ws handler runs in the event loop and `await`s litellm.acompletion streaming — no thread bridge. The off-thread concern from sync judging does not apply.
