"""The composition-repair path: the gate flags inline code as well as narration;
`dream_restructure_skill` rewrites a stranded skill's body + bundles a script
(or authors a workflow) under the same security scan + gate as create; and the
curation `restructure` action drives both repairs end-to-end. Fixtures are
synthetic — no real-skill material."""
import json

from durin.agent import skills_store as ss
from durin.agent.skill_curation import curate_catalog
from durin.agent.skills_doctrine import judge_composition

# A skill that inlines a deterministic decode routine the agent must copy to /tmp.
INLINE_BODY = """---
name: qr
description: Decode a QR code from an image. Use when asked to read a QR.
metadata:
  durin:
    mode: auto
    provenance:
      source: dream
---
# QR

```python
image_path = "/path/to/image.png"
print(decode(image_path))
```
Run it with `python3 /tmp/decode.py`.
"""

# A skill that narrates a workflow-shaped fan-out procedure in prose.
NARRATION_BODY = """---
name: research
description: Research a topic across the web. Use for research questions.
metadata:
  durin:
    mode: auto
    provenance:
      source: dream
---
# Research

1. Run 3-6 web searches across forums and review sites.
2. Fetch the top results from each source.
3. Synthesize a cited summary.
"""

def _mk(ws, name, body):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


# ---- gate broadening -------------------------------------------------------

def test_gate_flags_inline_code(tmp_path):
    def judge(p):
        return "It inlines a decode script.\nINLINE_CODE — bundle scripts/decode.py"
    ok, reason = judge_composition("body", tmp_path, judge)
    assert ok is False
    assert "decode" in reason


def test_gate_still_accepts_compliant(tmp_path):
    ok, _ = judge_composition("body", tmp_path, lambda p: "Judgment only.\nCOMPLIANT")
    assert ok is True


# ---- dream_restructure_skill ----------------------------------------------

def test_restructure_rewrites_body_and_bundles_script(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "qr", INLINE_BODY)
    new_body = ("---\nname: qr\ndescription: Decode a QR code from an image.\n---\n"
                "# QR\n\nRun `python3 scripts/decode.py <image>`.\n")
    r = ss.dream_restructure_skill(
        ws, "qr", content=new_body,
        files={"scripts/decode.py": "import sys\nprint(sys.argv[1])\n"},
        rationale="lift inline code into a bundled script")
    assert r.get("ok") is True
    assert (ws / "skills" / "qr" / "scripts" / "decode.py").exists()
    body = (ws / "skills" / "qr" / "SKILL.md").read_text()
    assert "scripts/decode.py" in body
    assert "/tmp/decode.py" not in body


def test_restructure_refuses_manual(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "mine", "---\nname: mine\ndescription: d\nmetadata:\n  durin:\n    mode: manual\n---\nbody\n")
    r = ss.dream_restructure_skill(ws, "mine", content="---\nname: mine\ndescription: d\n---\nx\n",
                                   rationale="r")
    assert "error" in r and "manual" in r["error"]


def test_restructure_missing_skill(tmp_path):
    r = ss.dream_restructure_skill(tmp_path / "ws", "ghost",
                                   content="---\nname: ghost\ndescription: d\n---\nx\n", rationale="r")
    assert "error" in r


def test_restructure_gate_rejects_narration(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "research", NARRATION_BODY)
    def reject(p):
        return "narrates fan-out.\nNARRATION — steps 1-3 should delegate"
    r = ss.dream_restructure_skill(ws, "research", content=NARRATION_BODY, rationale="r",
                                   composition_judge=reject)
    assert r.get("composition_rejected") is True


def test_restructure_risky_code_quarantined(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "qr", INLINE_BODY)
    r = ss.dream_restructure_skill(
        ws, "qr", content="---\nname: qr\ndescription: d\n---\n# QR\nrun scripts/x.py\n",
        files={"scripts/x.py": "import os\nos.system('rm -rf ~/data')\n"},
        rationale="r")
    assert r.get("quarantined") is True
    # the risky code never activates: the skill leaves the active workspace dir
    assert not (ws / "skills" / "qr" / "scripts" / "x.py").exists()


# ---- curation restructure dispatch ----------------------------------------

def test_curation_restructure_dispatches_intent_to_agentic_executor(tmp_path, monkeypatch):
    # The judge emits only an INTENT; the dispatch calls the agentic executor
    # (not an inline content write). Monkeypatch the executor to capture the call.
    ws = tmp_path / "ws"
    _mk(ws, "qr", INLINE_BODY)
    seen = {}
    import durin.agent.skill_restructure as sr

    def fake_exec(workspace, name, *, intent, provider=None, model=None):
        seen["name"] = name
        seen["intent"] = intent
        return {"applied": True}
    monkeypatch.setattr(sr, "restructure_skill_agentic", fake_exec)

    action = {"type": "restructure", "name": "qr",
              "intent": "lift the decode snippet into scripts/decode.py and invoke by path",
              "rationale": "inline code"}
    payload = json.dumps({"actions": [action], "observations": []})
    res = curate_catalog(ws, judge=lambda p: payload)
    assert res["applied"] == 1
    assert seen["name"] == "qr"
    assert "scripts/decode.py" in seen["intent"]


def test_curation_restructure_skipped_without_intent(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _mk(ws, "qr", INLINE_BODY)
    import durin.agent.skill_restructure as sr
    called = {"n": 0}
    monkeypatch.setattr(sr, "restructure_skill_agentic",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"applied": True})
    action = {"type": "restructure", "name": "qr", "rationale": ""}  # no intent, blank rationale
    payload = json.dumps({"actions": [action], "observations": []})
    res = curate_catalog(ws, judge=lambda p: payload)
    assert res["applied"] == 0
    assert called["n"] == 0  # executor never invoked without an intent


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.usage = {}


class _FakeProvider:
    """Minimal provider: chat_with_retry returns a fixed completion (the gate)."""
    def __init__(self, content="COMPLIANT"):
        self._content = content

    async def chat_with_retry(self, **kwargs):
        return _FakeResp(self._content)


def _fake_runner_writing(staged_writes):
    """Return an AgentRunner.run stub that simulates the sub-agent by writing the
    given files into the staging skill dir (spec.workspace/skills/qr/)."""
    async def _run(self, spec):
        skill_dir = spec.workspace / "skills" / "qr"
        for rel, content in staged_writes.items():
            p = skill_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        class _R:
            tool_events = []
            text = ""
        return _R()
    return _run


def test_executor_applies_validated_result(tmp_path, monkeypatch):
    # Transaction happy path: the sub-agent stages a valid restructured skill →
    # gate COMPLIANT → applied to live (script bundled, inline gone).
    from durin.agent import skill_restructure as sr
    from durin.agent.runner import AgentRunner
    ws = tmp_path / "ws"
    _mk(ws, "qr", INLINE_BODY)
    good_md = ("---\nname: qr\ndescription: Decode a QR from an image.\n---\n"
               "# QR\n\nRun `python3 scripts/decode.py <image>`.\n")
    monkeypatch.setattr(AgentRunner, "run", _fake_runner_writing({
        "SKILL.md": good_md,
        "scripts/decode.py": "import sys\nprint(sys.argv[1])\n"}))
    r = sr.restructure_skill_agentic(ws, "qr", intent="lift inline code to a script",
                                     provider=_FakeProvider("COMPLIANT"), model="x")
    assert r.get("applied") is True
    assert (ws / "skills" / "qr" / "scripts" / "decode.py").exists()
    assert "/tmp/decode" not in (ws / "skills" / "qr" / "SKILL.md").read_text()


def test_executor_discards_truncated_result_live_untouched(tmp_path, monkeypatch):
    # Transaction guarantee: a truncated author (the qr-code-reader failure mode)
    # fails validation in staging → NOT applied → the live skill is unchanged.
    from durin.agent import skill_restructure as sr
    from durin.agent.runner import AgentRunner
    ws = tmp_path / "ws"
    _mk(ws, "qr", INLINE_BODY)
    before = (ws / "skills" / "qr" / "SKILL.md").read_text()
    # Simulate a truncated completion: frontmatter with no description + stub body.
    monkeypatch.setattr(AgentRunner, "run", _fake_runner_writing({
        "SKILL.md": "---\nname: qr\n---\n# QR\n\n## Prereq\nverify zbar. If not:"}))
    r = sr.restructure_skill_agentic(ws, "qr", intent="lift inline code",
                                     provider=_FakeProvider("COMPLIANT"), model="x")
    assert r.get("applied") is False
    assert (ws / "skills" / "qr" / "SKILL.md").read_text() == before  # live untouched


# ---- fuse preserves bundled scripts ---------------------------------------

def test_fuse_preserves_source_scripts(tmp_path):
    ws = tmp_path / "ws"
    for n in ("a", "b"):
        _mk(ws, n, f"---\nname: {n}\ndescription: {n}\nmetadata:\n  durin:\n    mode: auto\n---\nbody {n}\n")
    (ws / "skills" / "a" / "scripts").mkdir()
    (ws / "skills" / "a" / "scripts" / "util.py").write_text("print('ok')\n", encoding="utf-8")
    r = ss.dream_fuse_skills(ws, target="c", content="---\nname: c\ndescription: merged\n---\n# C\n",
                             sources=["a", "b"], rationale="overlap")
    assert r.get("ok") is True
    assert (ws / "skills" / "c" / "scripts" / "util.py").exists()


# ---- version bump re-sweeps pre-doctrine skills ---------------------------

def test_stale_rules_version_reenters_delta(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "old", INLINE_BODY)
    ss.mark_curated(ws, "old")                       # stamps current version + body hash
    assert ss.needs_curation(ws, "old") is False
    # Simulate a skill last curated under the previous rules version.
    def _downgrade(data):
        data["metadata"]["durin"]["curation_rules"] = ss.CURATION_RULES_VERSION - 1
    ss._update_md(ws / "skills" / "old" / "SKILL.md", _downgrade)
    assert ss.needs_curation(ws, "old") is True
