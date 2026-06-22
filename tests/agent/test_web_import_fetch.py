"""web_import_fetch: auto-install on `allow`, short-circuit already-installed.

The webui import endpoint used to always park a freshly-fetched skill in
quarantine, even when the §8.C gate cleared it (`allow`) and even when the
skill was already installed. These tests lock in the new behaviour:

* already installed (no ``replace``) → short-circuit BEFORE the costly
  fetch + LLM judge, returning ``already_installed``;
* gate says ``allow`` → auto-install, return ``installed``;
* gate says ``confirm`` / ``block`` → leave ``quarantined``.
"""

from __future__ import annotations

from types import SimpleNamespace

from durin.agent import skills_store as ss


def _res(*cands, reason=None):
    return SimpleNamespace(candidates=list(cands), unresolved_reason=reason)


def _cand(name, ref="github:o/r/x"):
    return SimpleNamespace(name=name, ref=ref, kind="skill", detail="")


def test_already_installed_short_circuits_before_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skill_resolve.resolve_candidates",
                        lambda s: _res(_cand("pdf")))
    monkeypatch.setattr(ss, "_installed_skill_names", lambda w: {"pdf"})
    called = {"fetch": False}

    def _fetch(*a, **k):
        called["fetch"] = True
        return tmp_path / "q"
    monkeypatch.setattr("durin.agent.skills_import.fetch_candidate", _fetch)

    status, payload = ss.web_import_fetch(tmp_path, "github:o/r")
    assert status == 200
    assert payload["already_installed"] == "pdf"
    assert called["fetch"] is False, "must not fetch/judge an already-installed skill"


def test_replace_bypasses_already_installed(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skill_resolve.resolve_candidates",
                        lambda s: _res(_cand("pdf")))
    monkeypatch.setattr(ss, "_installed_skill_names", lambda w: {"pdf"})
    monkeypatch.setattr("durin.agent.skills_import.fetch_candidate", lambda *a, **k: tmp_path / "q")
    monkeypatch.setattr("durin.security.skill_scan.scan_skill",
                        lambda d: SimpleNamespace(verdict="safe", findings=[]))
    monkeypatch.setattr("durin.agent.skills_import.validate_skill",
                        lambda d: SimpleNamespace(carries_code=False, ok=True, name="pdf", errors=[]))
    monkeypatch.setattr("durin.agent.skills_import.decide_action", lambda *a, **k: "allow")
    seen = {}

    def _install(ws, qd, *, source, allowlist, confirmed, replace, **k):
        seen["replace"] = replace
        return {"ok": True, "name": "pdf", "commit": "deadbeef"}
    monkeypatch.setattr("durin.agent.skills_import.install_imported_skill", _install)

    status, payload = ss.web_import_fetch(tmp_path, "github:o/r", replace=True)
    assert payload.get("installed") == "pdf"
    assert seen["replace"] is True


def test_auto_install_when_gate_allows(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skill_resolve.resolve_candidates",
                        lambda s: _res(_cand("calc")))
    monkeypatch.setattr(ss, "_installed_skill_names", lambda w: set())
    monkeypatch.setattr("durin.agent.skills_import.fetch_candidate", lambda *a, **k: tmp_path / "q")
    monkeypatch.setattr("durin.security.skill_scan.scan_skill",
                        lambda d: SimpleNamespace(verdict="safe", findings=[]))
    monkeypatch.setattr("durin.agent.skills_import.validate_skill",
                        lambda d: SimpleNamespace(carries_code=False, ok=True, name="calc", errors=[]))
    monkeypatch.setattr("durin.agent.skills_import.decide_action", lambda *a, **k: "allow")
    monkeypatch.setattr("durin.agent.skills_import.install_imported_skill",
                        lambda *a, **k: {"ok": True, "name": "calc", "commit": "abc123"})

    status, payload = ss.web_import_fetch(tmp_path, "github:o/r")
    assert status == 200
    assert payload.get("installed") == "calc"
    assert payload.get("commit") == "abc123"
    assert "quarantined" not in payload


def test_quarantine_when_gate_needs_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skill_resolve.resolve_candidates",
                        lambda s: _res(_cand("tool")))
    monkeypatch.setattr(ss, "_installed_skill_names", lambda w: set())
    monkeypatch.setattr("durin.agent.skills_import.fetch_candidate", lambda *a, **k: tmp_path / "q")
    monkeypatch.setattr("durin.security.skill_scan.scan_skill",
                        lambda d: SimpleNamespace(verdict="caution", findings=[]))
    monkeypatch.setattr("durin.agent.skills_import.validate_skill",
                        lambda d: SimpleNamespace(carries_code=True, ok=True, name="tool", errors=[]))
    monkeypatch.setattr("durin.agent.skills_import.decide_action", lambda *a, **k: "confirm")

    status, payload = ss.web_import_fetch(tmp_path, "github:o/r")
    assert payload.get("quarantined") == "tool"
    assert payload.get("needs") == "confirm"
    assert "installed" not in payload
