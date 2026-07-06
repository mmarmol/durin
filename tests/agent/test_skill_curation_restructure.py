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

WF_DEF = {
    "name": "synth", "description": "fan out searches and synthesize",
    "start": "only",
    "nodes": [{"id": "only", "title": "t", "kind": "work",
               "mode": "read", "tools": "none", "prompt": "p"}],
}


def _mk(ws, name, body):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def _gate_or(prompt, curation_json):
    """A judge double: answer the composition GATE prompt COMPLIANT, and the
    curation prompt with the given action JSON."""
    if "before it is saved" in prompt:      # unique to the gate prompt
        return "COMPLIANT"
    return curation_json


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

def test_curation_restructure_lifts_inline_code(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "qr", INLINE_BODY)
    action = {"type": "restructure", "name": "qr",
              "content": ("---\nname: qr\ndescription: Decode a QR from an image.\n---\n"
                          "# QR\n\nRun `python3 scripts/decode.py <image>`.\n"),
              "files": {"scripts/decode.py": "import sys\nprint(sys.argv[1])\n"},
              "rationale": "lift inline code"}
    payload = json.dumps({"actions": [action], "observations": []})
    res = curate_catalog(ws, judge=lambda p: _gate_or(p, payload))
    assert res["applied"] == 1
    assert (ws / "skills" / "qr" / "scripts" / "decode.py").exists()


def test_curation_restructure_authors_workflow_then_delegates(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "research", NARRATION_BODY)
    action = {"type": "restructure", "name": "research",
              "content": ("---\nname: research\ndescription: Research a topic.\n---\n"
                          "# Research\n\nrun_workflow `synth` with the topic.\n"),
              "workflow": {"name": "synth", "definition": WF_DEF},
              "rationale": "delegate to authored workflow"}
    payload = json.dumps({"actions": [action], "observations": []})
    res = curate_catalog(ws, judge=lambda p: _gate_or(p, payload))
    assert res["applied"] == 1
    from durin.workflow.loader import load_workflow
    assert load_workflow(ws, "synth") is not None
    assert "run_workflow" in (ws / "skills" / "research" / "SKILL.md").read_text()


def test_curation_restructure_aborts_on_invalid_workflow(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "research", NARRATION_BODY)
    action = {"type": "restructure", "name": "research",
              "content": "---\nname: research\ndescription: d\n---\n# R\ndelegates via run_workflow\n",
              "workflow": {"name": "bad", "definition": {"not": "a valid graph"}},
              "rationale": "x"}
    payload = json.dumps({"actions": [action], "observations": []})
    res = curate_catalog(ws, judge=lambda p: _gate_or(p, payload))
    assert res["applied"] == 0
    # body is NOT rewritten to point at a workflow that failed to land, and the
    # invalid workflow never landed (only the curation stamp is added to frontmatter)
    body = (ws / "skills" / "research" / "SKILL.md").read_text()
    assert "Run 3-6 web searches" in body        # original narration preserved
    assert "delegates via run_workflow" not in body
    from durin.workflow.loader import workflows_dir
    assert not (workflows_dir(ws) / "bad.json").exists()


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
