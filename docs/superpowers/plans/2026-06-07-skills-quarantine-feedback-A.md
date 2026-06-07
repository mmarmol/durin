# Skills Quarantine Feedback — Plan A (foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make quarantined-skill triage transparent and resilient — reuse the chat retry policy for the audit LLM call, explain *why* each skill needs approval, and have the AI audit always return a clear final summary — without the live-streaming layer (that is Plan B).

**Architecture:** Backend changes in `durin/memory/llm_invoke.py` (retry), `durin/security/skill_judge.py` (summary/verdict parsing, drop hardcoded transient retry), `durin/agent/skills_store.py` + `durin/agent/skills_surface.py` (judge result summary, gate reasons), exposed through the existing `/api/skills/...` endpoints. Frontend changes in `webui/src/lib/api.ts` and `webui/src/components/SkillsView.tsx` (Why-it's-here section, audit spinner, final summary, readable retryable errors).

**Tech Stack:** Python 3.11 / pytest, litellm, React + TypeScript + Tailwind / vitest, i18next.

**Spec:** `docs/superpowers/specs/2026-06-07-skills-quarantine-feedback-design.md` (Sections 1, 2, and the non-streaming part of 3).

---

## File Structure

- `durin/memory/llm_invoke.py` — add `_retry_llm_call(call, *, mode)` reusing `LLMProvider`'s retry constants + transient classifier; wrap `default_llm_invoke`'s completion in it. Owns: resilient single-shot LLM invocation for memory/judge.
- `durin/security/skill_judge.py` — extend `_PROMPT` with `===SUMMARY===`; parse summary + verdict; `judge_skill` returns `JudgeOutcome(findings, verdict, summary)`; drop the hardcoded transient retry (keep parse-retry). Owns: the audit prompt + parsing.
- `durin/agent/skills_store.py` — `web_skill_judge` returns/persists `summary`; classify errors into `error_code`. Owns: on-demand judge endpoint logic.
- `durin/agent/skills_surface.py` — `web_quarantine` rows gain `needs` + `reasons[]`. Owns: the quarantine row payload.
- `webui/src/lib/api.ts` — `QuarantineRow` gains `needs` + `reasons`; `JudgeResult` gains `summary` + `error_code`. Owns: API types.
- `webui/src/components/SkillsView.tsx` — triage pane: "Why it's here" section, audit spinner, final summary, readable retryable error. Owns: triage UI.
- `webui/src/i18n/locales/{en,es}/common.json` — new `skills.*` copy.

---

## Task 1: Reusable retry for `default_llm_invoke`

**Files:**
- Modify: `durin/memory/llm_invoke.py`
- Test: `tests/memory/test_llm_invoke_retry.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_llm_invoke_retry.py
import pytest
from durin.memory import llm_invoke


def _transient():
    return Exception("litellm.InternalServerError: OpenAIException - Connection error.")


def _fatal():
    return Exception("AuthenticationError: invalid api key")


def test_retry_recovers_from_transient(monkeypatch):
    monkeypatch.setattr(llm_invoke.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _transient()
        return "ok"

    assert llm_invoke._retry_llm_call(call, mode="standard") == "ok"
    assert calls["n"] == 3


def test_retry_does_not_retry_fatal(monkeypatch):
    monkeypatch.setattr(llm_invoke.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise _fatal()

    with pytest.raises(Exception, match="AuthenticationError"):
        llm_invoke._retry_llm_call(call, mode="standard")
    assert calls["n"] == 1


def test_standard_gives_up_after_full_schedule(monkeypatch):
    monkeypatch.setattr(llm_invoke.time, "sleep", lambda _s: None)
    from durin.providers.base import LLMProvider
    attempts = len(LLMProvider._CHAT_RETRY_DELAYS) + 1  # initial + each delay
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise _transient()

    with pytest.raises(Exception, match="Connection error"):
        llm_invoke._retry_llm_call(call, mode="standard")
    assert calls["n"] == attempts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/memory/test_llm_invoke_retry.py -q`
Expected: FAIL — `AttributeError: module 'durin.memory.llm_invoke' has no attribute '_retry_llm_call'` (and no `time`).

- [ ] **Step 3: Implement `_retry_llm_call` and wrap the completion**

In `durin/memory/llm_invoke.py`, add `import time` near the top imports, then add this function above `default_llm_invoke`:

```python
def _retry_llm_call(call, *, mode: str = "standard"):
    """Run ``call()`` with the same retry policy as chat (single source: the
    constants + transient classifier on ``LLMProvider``). ``standard`` walks the
    6-delay schedule (7 attempts); ``persistent`` caps each delay and keeps
    retrying until the same error repeats ``_PERSISTENT_IDENTICAL_ERROR_LIMIT``
    times. Non-transient errors are re-raised immediately."""
    from durin.providers.base import LLMProvider

    delays = LLMProvider._CHAT_RETRY_DELAYS
    attempt = 0
    identical = 0
    last_text: str | None = None
    while True:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — re-raised below if not transient
            text = str(exc)
            if not LLMProvider._is_transient_error(text):
                raise
            if mode == "persistent":
                identical = identical + 1 if text == last_text else 1
                last_text = text
                if identical >= LLMProvider._PERSISTENT_IDENTICAL_ERROR_LIMIT:
                    raise
                delay = min(delays[min(attempt, len(delays) - 1)],
                            LLMProvider._PERSISTENT_MAX_DELAY)
            else:  # standard
                if attempt >= len(delays):
                    raise
                delay = delays[attempt]
            logger.info("memory LLM transient error, retrying in %ss: %s", delay, text)
            time.sleep(delay)
            attempt += 1
```

Then change the body of `default_llm_invoke` so the `litellm.completion(...)` call is wrapped. Replace:

```python
    import litellm

    response = litellm.completion(
        model=f"openai/{model}",
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        api_base="https://api.z.ai/api/coding/paas/v4",
        temperature=temperature,
    )
```

with:

```python
    import litellm

    try:
        from durin.config.loader import load_config
        mode = load_config().defaults.provider_retry_mode
    except Exception:  # noqa: BLE001 — config optional; default to standard
        mode = "standard"

    response = _retry_llm_call(
        lambda: litellm.completion(
            model=f"openai/{model}",
            messages=[{"role": "user", "content": prompt}],
            api_key=api_key,
            api_base="https://api.z.ai/api/coding/paas/v4",
            temperature=temperature,
        ),
        mode=mode,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/memory/test_llm_invoke_retry.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add durin/memory/llm_invoke.py tests/memory/test_llm_invoke_retry.py
git commit -m "fix(memory): reuse chat retry policy for default_llm_invoke (no hardcoded retry)"
```

---

## Task 2: Judge returns a structured outcome (findings + verdict + summary)

**Files:**
- Modify: `durin/security/skill_judge.py`
- Test: `tests/security/test_skill_judge_summary.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_skill_judge_summary.py
from pathlib import Path

from durin.security import skill_judge


def _write_skill(tmp_path: Path) -> Path:
    d = tmp_path / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: demo\ndescription: x\n---\nbody\n", encoding="utf-8"
    )
    return d


def test_judge_parses_summary_verdict_findings(tmp_path):
    raw = (
        "===SUMMARY===\nChecked SKILL.md and scripts for injection and exfiltration; none found.\n"
        "===VERDICT===\nsafe\n"
        "===FINDINGS===\nnone\n===END===\n"
    )
    d = _write_skill(tmp_path)
    out = skill_judge.judge_skill(
        d, llm_invoke=lambda *a, **k: skill_judge.LLMResponseText(raw), model="x"
    )
    assert out.verdict == "safe"
    assert out.findings == []
    assert "exfiltration" in out.summary


def test_judge_parses_findings_and_caution(tmp_path):
    raw = (
        "===SUMMARY===\nFound a curl|bash installer.\n"
        "===VERDICT===\ncaution\n"
        "===FINDINGS===\ncaution | dangerous_code | scripts/go.sh | fetch-and-execute\n===END===\n"
    )
    d = _write_skill(tmp_path)
    out = skill_judge.judge_skill(
        d, llm_invoke=lambda *a, **k: skill_judge.LLMResponseText(raw), model="x"
    )
    assert out.verdict == "caution"
    assert len(out.findings) == 1
    assert out.findings[0].category == "llm:dangerous_code"
    assert out.summary.startswith("Found")


def test_missing_summary_is_tolerated(tmp_path):
    raw = "===FINDINGS===\nnone\n===END===\n"
    d = _write_skill(tmp_path)
    out = skill_judge.judge_skill(
        d, llm_invoke=lambda *a, **k: skill_judge.LLMResponseText(raw), model="x"
    )
    assert out.findings == []
    assert out.summary == ""
```

(`LLMResponseText` is a tiny helper added in Step 3 so tests can return an object with a `.text` attribute.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/security/test_skill_judge_summary.py -q`
Expected: FAIL — `judge_skill` returns a `list[Finding]`, has no `.verdict`/`.summary`; `LLMResponseText` undefined.

- [ ] **Step 3: Implement the structured outcome**

In `durin/security/skill_judge.py`:

Add near the top (after imports), a tiny response shim used by tests and a dataclass outcome:

```python
from dataclasses import dataclass, field


@dataclass
class LLMResponseText:
    """Minimal LLM response carrying just text (test/helper convenience)."""

    text: str


@dataclass
class JudgeOutcome:
    """Structured judge result: capped findings, the model's verdict, and a
    1-3 sentence summary of what was examined + the conclusion."""

    findings: list = field(default_factory=list)
    verdict: str = ""
    summary: str = ""
```

Extend `_PROMPT` — replace the response-marker section so it requests a summary first:

```python
Respond using these markers exactly:
===SUMMARY===
1-3 sentences: what you examined (instructions, scripts) and your conclusion.
===VERDICT===
safe | caution | dangerous
===FINDINGS===
One finding per line as `severity | category | where | exact what and why`,
where severity is one of info/caution/high/dangerous and where is the file or
location. Write `none` if there are no concrete problems.
===END===
```

Add marker regexes next to `_RE_FINDINGS`:

```python
_RE_SUMMARY = re.compile(r"===SUMMARY===\s*(?P<body>.*?)\s*===(?:VERDICT|FINDINGS)===", re.IGNORECASE | re.DOTALL)
_RE_VERDICT = re.compile(r"===VERDICT===\s*(?P<body>.*?)\s*===FINDINGS===", re.IGNORECASE | re.DOTALL)
```

Add a parser that returns the outcome (reuses `_parse_findings` for the findings body):

```python
def _parse_outcome(raw: str, max_severity: str) -> "JudgeOutcome":
    findings = _parse_findings(raw, max_severity)  # raises JudgeError if FINDINGS/END missing
    sm = _RE_SUMMARY.search(raw)
    summary = sm.group("body").strip() if sm else ""
    vm = _RE_VERDICT.search(raw)
    verdict = (vm.group("body").strip().lower() if vm else "")
    if verdict not in ("safe", "caution", "dangerous"):
        verdict = ""
    return JudgeOutcome(findings=findings, verdict=verdict, summary=summary)
```

Change `judge_skill` to return `JudgeOutcome` and to keep only the parse-retry (the transient retry now lives in `default_llm_invoke`, Task 1). Replace the body of the `for attempt ...` loop's return + the final raise:

```python
def judge_skill(skill_dir: Path, *, llm_invoke: LLMInvoke, model: str,
                max_severity: str = "caution", max_retries: int = 1) -> "JudgeOutcome":
    """Run the LLM judge over a skill dir. Returns a JudgeOutcome (findings may
    be empty). ``max_retries`` covers PARSE failures only — transient transport
    errors are retried inside the injected ``llm_invoke``. Raises JudgeError on
    parse failure after retries; the caller degrades to the deterministic scan."""
    if max_severity not in _SEV:
        max_severity = "caution"
    name, content = _gather_content(skill_dir)
    if not content.strip():
        return JudgeOutcome()
    prompt = _PROMPT.format(name=name, content=content)
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        resp = llm_invoke(prompt, model=model)  # transient retries handled inside
        raw = getattr(resp, "text", None)
        raw = raw if isinstance(raw, str) else str(resp)
        try:
            return _parse_outcome(raw, max_severity)
        except JudgeError as exc:
            last = exc
            logger.warning("skill judge parse failed (%d/%d): %s", attempt + 1, max_retries + 1, exc)
    raise JudgeError(f"skill judge parse failed after {max_retries + 1} attempts: {last}")
```

- [ ] **Step 4: Update `audit_skill` (the auto-run path) to the new return type**

`audit_skill` merges judge findings into the deterministic report. Change the merge to read `.findings` from the outcome:

```python
        outcome = judge_skill(skill_dir, llm_invoke=invoke, model=model,
                              max_severity=judge_max_severity)
        rep.findings += outcome.findings
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/security/test_skill_judge_summary.py tests/security -q -k "judge or scan or audit"`
Expected: PASS, including pre-existing judge/scan/audit tests (update any that asserted `judge_skill` returned a list — change them to `.findings`).

- [ ] **Step 6: Commit**

```bash
git add durin/security/skill_judge.py tests/security/test_skill_judge_summary.py
git commit -m "feat(skills): judge returns structured outcome (findings + verdict + summary)"
```

---

## Task 3: `web_skill_judge` persists + returns the summary, classifies errors

**Files:**
- Modify: `durin/agent/skills_store.py:578-616`
- Test: `tests/agent/test_skill_judge_web.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skill_judge_web.py
import json
from pathlib import Path

import durin.agent.skills_store as ss
from durin.security.skill_judge import JudgeOutcome


def _quarantined(tmp_path: Path) -> Path:
    q = tmp_path / ".durin" / "import-quarantine" / "demo"
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\nbody\n", encoding="utf-8")
    (q / ".scan.json").write_text(json.dumps({"source": "github:o/r", "verdict": "safe", "findings": []}), encoding="utf-8")
    return tmp_path


def test_web_judge_returns_and_persists_summary(tmp_path, monkeypatch):
    ws = _quarantined(tmp_path)
    monkeypatch.setattr(ss, "_import_judge", lambda: ("always", "m", "caution"))
    monkeypatch.setattr(
        "durin.security.skill_judge.judge_skill",
        lambda *a, **k: JudgeOutcome(findings=[], verdict="safe", summary="Reviewed instructions; clean."),
    )
    status, payload = ss.web_skill_judge(ws, "demo")
    assert status == 200 and payload["judged"] is True
    assert payload["summary"] == "Reviewed instructions; clean."
    stored = json.loads((ws / ".durin" / "import-quarantine" / "demo" / ".scan.json").read_text())
    assert stored["summary"] == "Reviewed instructions; clean."


def test_web_judge_unreachable_error_code(tmp_path, monkeypatch):
    ws = _quarantined(tmp_path)
    monkeypatch.setattr(ss, "_import_judge", lambda: ("always", "m", "caution"))

    def boom(*a, **k):
        raise Exception("litellm.InternalServerError: OpenAIException - Connection error.")

    monkeypatch.setattr("durin.security.skill_judge.judge_skill", boom)
    status, payload = ss.web_skill_judge(ws, "demo")
    assert status == 200 and payload["judged"] is False
    assert payload["error_code"] == "unreachable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skill_judge_web.py -q`
Expected: FAIL — payload has no `summary`/`error_code`; `web_skill_judge` still expects a list from `judge_skill`.

- [ ] **Step 3: Rewrite `web_skill_judge`**

Replace `durin/agent/skills_store.py:578-616` with:

```python
def web_skill_judge(workspace: Path, name: str) -> tuple[int, dict]:
    """`GET /api/skills/{name}/judge` — run the LLM judge ON-DEMAND over a
    quarantined skill, merge its findings into the quarantine .scan.json, and
    return the updated verdict + findings + summary. Errors carry a machine
    ``error_code`` (unreachable | parse | no_model) for a readable UI message."""
    import json as _json

    from durin.security.skill_judge import JudgeError, judge_skill
    from durin.security.skill_scan import ScanReport, scan_skill
    from durin.providers.base import LLMProvider

    qdir = Path(workspace) / ".durin" / "import-quarantine" / name
    if not (qdir / "SKILL.md").is_file():
        return 404, {"error": f"not in quarantine: {name}"}
    _, model, max_sev = _import_judge()
    det = scan_skill(qdir)
    try:
        from durin.memory.llm_invoke import default_llm_invoke
        outcome = judge_skill(qdir, llm_invoke=default_llm_invoke, model=model or "glm-5.1",
                              max_severity=max_sev)
    except JudgeError as exc:
        code = "parse" if "parse" in str(exc).lower() else "unreachable"
        return 200, {"name": name, "verdict": det.verdict, "judged": False,
                     "error": str(exc), "error_code": code}
    except Exception as exc:  # noqa: BLE001
        code = "unreachable" if LLMProvider._is_transient_error(str(exc)) else "no_model"
        return 200, {"name": name, "verdict": det.verdict, "judged": False,
                     "error": str(exc), "error_code": code}

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
    sj.write_text(_json.dumps({"source": source, "verdict": merged.verdict,
                               "findings": findings, "summary": outcome.summary}),
                  encoding="utf-8")
    return 200, {"name": name, "verdict": merged.verdict, "findings": findings,
                 "summary": outcome.summary, "judged": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skill_judge_web.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_store.py tests/agent/test_skill_judge_web.py
git commit -m "feat(skills): web judge returns/persists summary + error_code"
```

---

## Task 4: Gate reasons on quarantine rows

**Files:**
- Modify: `durin/agent/skills_surface.py` (the `web_quarantine` builder, ~lines 55-80)
- Test: `tests/agent/test_quarantine_reasons.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_quarantine_reasons.py
import json
from pathlib import Path

from durin.agent.skills_surface import web_quarantine


def _q(tmp_path: Path, name: str, *, verdict="safe", source="github:o/r", scripts=False):
    q = tmp_path / ".durin" / "import-quarantine" / name
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text("---\nname: %s\ndescription: x\n---\nbody\n" % name, encoding="utf-8")
    (q / ".scan.json").write_text(json.dumps({"source": source, "verdict": verdict, "findings": []}), encoding="utf-8")
    if scripts:
        (q / "scripts").mkdir()
        (q / "scripts" / "go.sh").write_text("echo hi\n", encoding="utf-8")
    return tmp_path


def _codes(row):
    return {r["code"] for r in row["reasons"]}


def test_untrusted_source_reason(tmp_path, monkeypatch):
    ws = _q(tmp_path, "demo")
    monkeypatch.setattr("durin.agent.skills_store._import_allowlist", lambda: [])
    rows = web_quarantine(ws)
    row = next(r for r in rows if r["name"] == "demo")
    assert row["needs"] == "confirm"
    assert "untrusted_source" in _codes(row)


def test_carries_code_reason(tmp_path, monkeypatch):
    ws = _q(tmp_path, "demo", scripts=True)
    monkeypatch.setattr("durin.agent.skills_store._import_allowlist", lambda: ["github:o/r"])
    rows = web_quarantine(ws)
    row = next(r for r in rows if r["name"] == "demo")
    assert "carries_code" in _codes(row)


def test_dangerous_blocks(tmp_path, monkeypatch):
    ws = _q(tmp_path, "demo", verdict="dangerous")
    monkeypatch.setattr("durin.agent.skills_store._import_allowlist", lambda: ["github:o/r"])
    rows = web_quarantine(ws)
    row = next(r for r in rows if r["name"] == "demo")
    assert row["needs"] == "block"
    assert "verdict_dangerous" in _codes(row)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_quarantine_reasons.py -q`
Expected: FAIL — rows have no `needs`/`reasons` keys.

- [ ] **Step 3: Add reasons to the builder**

In `durin/agent/skills_surface.py`, inside the `web_quarantine` row loop (after `install_specs` and `trust_prefix` are set, before `out.append(entry)`), add:

```python
        from durin.agent.skills_import import decide_action, validate_skill
        from durin.agent.skills_store import _import_allowlist

        allowlist = _import_allowlist()
        vr = validate_skill(d)
        verdict = entry["verdict"]
        needs = decide_action(entry["source"], verdict=verdict,
                              carries_code=vr.carries_code, allowlist=allowlist)
        reasons: list[dict] = []
        if verdict == "dangerous":
            reasons.append({"code": "verdict_dangerous", "detail": ""})
        elif verdict == "caution":
            reasons.append({"code": "verdict_caution", "detail": ""})
        if vr.carries_code:
            reasons.append({"code": "carries_code",
                            "detail": ", ".join(vr.code_artifacts[:8])})
        if entry["source"] and not any(entry["source"].startswith(p) for p in allowlist if p):
            reasons.append({"code": "untrusted_source", "detail": entry["source"]})
        if entry["install_specs"]:
            reasons.append({"code": "declared_deps",
                            "detail": ", ".join(entry["install_specs"])})
        entry["needs"] = needs
        entry["reasons"] = reasons
```

Also add `"needs": "confirm", "reasons": []` to the `entry = {...}` default dict so rows without a `.scan.json` still carry the keys.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_quarantine_reasons.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_surface.py tests/agent/test_quarantine_reasons.py
git commit -m "feat(skills): expose gate reasons + needs on quarantine rows"
```

---

## Task 5: API types

**Files:**
- Modify: `webui/src/lib/api.ts` (`QuarantineRow`, `JudgeResult`)

- [ ] **Step 1: Extend the types**

In `webui/src/lib/api.ts`, add to `QuarantineRow`:

```typescript
  /** Gate outcome: allow installs straight away; confirm/block need a prompt. */
  needs?: "allow" | "confirm" | "block";
  /** Why approval is required, in structured form (rendered as plain language). */
  reasons?: { code: string; detail?: string }[];
```

And to `JudgeResult` (the interface ending at the `error?: string` line):

```typescript
  summary?: string;
  error_code?: "unreachable" | "parse" | "no_model";
```

- [ ] **Step 2: Typecheck**

Run: `cd webui && npx tsc -p tsconfig.build.json --noEmit`
Expected: PASS (no type errors).

- [ ] **Step 3: Commit**

```bash
git add webui/src/lib/api.ts
git commit -m "feat(webui): QuarantineRow.needs/reasons + JudgeResult.summary/error_code types"
```

---

## Task 6: Triage UI — Why-it's-here, audit spinner, final summary, readable errors

**Files:**
- Modify: `webui/src/components/SkillsView.tsx`
- Modify: `webui/src/i18n/locales/en/common.json`, `webui/src/i18n/locales/es/common.json`
- Test: `webui/src/tests/skills-view.test.tsx` (extend)

- [ ] **Step 1: Add i18n copy**

Under `skills` in BOTH locale files, add (en shown; mirror in es):

```json
"whyHere": "Why it's here",
"reason": {
  "untrusted_source": "The source {{detail}} isn't in your trusted allowlist, so it needs your explicit approval.",
  "carries_code": "It carries executable code ({{detail}}) — review before approving.",
  "declared_deps": "It declares dependencies (not run automatically): {{detail}}.",
  "verdict_caution": "The security scan flagged it as caution.",
  "verdict_dangerous": "The security scan flagged it as dangerous."
},
"audit": {
  "running": "Auditing with AI…",
  "summaryLabel": "AI audit",
  "clean": "The AI reviewed it and found no problems.",
  "unreachable": "Couldn't reach the audit model. Check your connection and retry.",
  "no_model": "No audit model is configured.",
  "parse": "The audit response was unreadable. Retry.",
  "retry": "Retry audit"
}
```

es:

```json
"whyHere": "Por qué está acá",
"reason": {
  "untrusted_source": "La fuente {{detail}} no está en tu lista de confianza, por eso requiere tu aprobación explícita.",
  "carries_code": "Trae código ejecutable ({{detail}}) — revisá antes de aprobar.",
  "declared_deps": "Declara dependencias (no se ejecutan solas): {{detail}}.",
  "verdict_caution": "El escaneo de seguridad la marcó como precaución.",
  "verdict_dangerous": "El escaneo de seguridad la marcó como peligrosa."
},
"audit": {
  "running": "Auditando con IA…",
  "summaryLabel": "Auditoría IA",
  "clean": "La IA la revisó y no encontró problemas.",
  "unreachable": "No se pudo contactar el modelo de auditoría. Revisá tu conexión y reintentá.",
  "no_model": "No hay modelo de auditoría configurado.",
  "parse": "La respuesta de la auditoría no se pudo leer. Reintentá.",
  "retry": "Reintentar auditoría"
}
```

- [ ] **Step 2: Write the failing test**

Add to `webui/src/tests/skills-view.test.tsx`:

```typescript
  it("shows why-it's-here reasons and audit summary in the triage pane", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([
      {
        name: "firecrawl", status: "quarantined", source: "github:o/r", verdict: "safe", findings: [],
        needs: "confirm",
        reasons: [{ code: "untrusted_source", detail: "github:o/r" }],
      },
    ]);
    vi.mocked(api.judgeSkill).mockResolvedValue({
      name: "firecrawl", verdict: "safe", findings: [], judged: true,
      summary: "Reviewed instructions and rules; no injection or exfiltration.",
    });

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");
    await user.click(screen.getByRole("button", { name: /pending/i }));
    await user.click(await screen.findByRole("button", { name: /firecrawl/i }));

    // why-it's-here renders the reason in plain language
    expect(await screen.findByText(/isn't in your trusted allowlist/i)).toBeInTheDocument();

    // running the audit shows the AI summary afterward
    await user.click(screen.getByRole("button", { name: /audit with llm/i }));
    expect(await screen.findByText(/no injection or exfiltration/i)).toBeInTheDocument();
  });
```

(Note: `judgeSkill` must be added to the `vi.mock("@/lib/api", ...)` mock factory list and to `beforeEach` resets.)

- [ ] **Step 3: Run test to verify it fails**

Run: `cd webui && npx vitest run src/tests/skills-view.test.tsx -t "why-it's-here"`
Expected: FAIL — reasons section and audit summary not rendered.

- [ ] **Step 4: Implement the triage-pane additions**

In `SkillsView.tsx`:

a) Add audit state near the other `useState`s:

```typescript
  const [auditMsg, setAuditMsg] = useState<{ kind: "summary" | "error"; text: string } | null>(null);
```

b) Replace the `judgeOne` callback so it tracks the running state and surfaces summary / readable error (it already exists — extend it):

```typescript
  const judgeOne = useCallback(
    async (name: string) => {
      setActing(name);
      setAuditMsg(null);
      try {
        const r = await judgeSkill(token, name);
        if (r.judged) {
          setAuditMsg({
            kind: "summary",
            text: r.summary?.trim() || t("skills.audit.clean"),
          });
        } else {
          setAuditMsg({
            kind: "error",
            text: t(`skills.audit.${r.error_code ?? "unreachable"}`),
          });
        }
        await refresh();
      } catch {
        setAuditMsg({ kind: "error", text: t("skills.audit.unreachable") });
      } finally {
        setActing(null);
      }
    },
    [token, refresh, t],
  );
```

c) In the triage pane JSX, after the source line and before the Security section, add the Why-it's-here block:

```tsx
                  {triageRow.reasons && triageRow.reasons.length > 0 ? (
                    <div className="mt-4">
                      <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        {t("skills.whyHere")}
                      </p>
                      <ul className="flex flex-col gap-1">
                        {triageRow.reasons.map((r) => (
                          <li key={r.code} className="text-[12px] text-muted-foreground">
                            {t(`skills.reason.${r.code}`, { detail: r.detail ?? "" })}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
```

d) After the Security section, render the audit result/spinner:

```tsx
                  {acting === triageRow.name ? (
                    <p className="mt-3 flex items-center gap-1.5 text-[12px] text-muted-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                      {t("skills.audit.running")}
                    </p>
                  ) : auditMsg ? (
                    auditMsg.kind === "summary" ? (
                      <div className="mt-3">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                          {t("skills.audit.summaryLabel")}
                        </p>
                        <p className="text-[12px] text-muted-foreground">{auditMsg.text}</p>
                      </div>
                    ) : (
                      <div className="mt-3 flex flex-wrap items-center gap-2">
                        <span className="text-[12px] text-destructive">{auditMsg.text}</span>
                        <Button size="sm" variant="outline" disabled={acting === triageRow.name}
                          onClick={() => void judgeOne(triageRow.name)}>
                          {t("skills.audit.retry")}
                        </Button>
                      </div>
                    )
                  ) : null}
```

e) Clear `auditMsg` when switching the selected pending row — in `openTriage`, add `setAuditMsg(null);`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd webui && npx vitest run src/tests/skills-view.test.tsx`
Expected: PASS (all skills-view tests, including the new one).

- [ ] **Step 6: Build + commit**

```bash
cd webui && npm run build && cd ..
git add webui/src/components/SkillsView.tsx webui/src/lib/api.ts \
  webui/src/i18n/locales/en/common.json webui/src/i18n/locales/es/common.json \
  webui/src/tests/skills-view.test.tsx
git commit -m "feat(webui): triage why-it's-here reasons + audit spinner/summary/readable retry"
```

---

## Task 7: Full verification

- [ ] **Step 1: Backend suite**

Run: `pytest tests/ -q --maxfail=5`
Expected: PASS (no regressions; new tests green).

- [ ] **Step 2: Webui suite + build**

Run: `cd webui && npx vitest run && npm run build`
Expected: PASS; build clean.

- [ ] **Step 3: Live check (per repo convention)**

Run the dev server against the running gateway and confirm: a pending skill shows the "Why it's here" reason; "Audit with LLM" shows the spinner then either a summary or a readable retryable error.

```bash
cd webui && npm run dev   # http://127.0.0.1:5173 — proxies /api,/auth,ws to the gateway
```

---

## Self-Review notes

- **Spec coverage:** Section 1 → Task 1. Section 2 → Task 4 (+5/6 UI). Section 3 non-streaming (summary/verdict parsing + readable errors + spinner) → Tasks 2, 3, 5, 6. The Section 3 **live reasoning stream** is intentionally deferred to **Plan B** (separate subsystem: a new per-audit websocket stream channel).
- **Type consistency:** `JudgeOutcome(findings, verdict, summary)` is produced in Task 2 and consumed in Tasks 2 (`audit_skill`) and 3 (`web_skill_judge`). `error_code` values (`unreachable|parse|no_model`) match between Task 3, Task 5 type, and Task 6 i18n keys. `reasons[].code` values match between Task 4 and the Task 6 `skills.reason.*` keys.
- **No placeholders:** every code/test step carries real code and exact commands.
