import io
import json
import zipfile

from durin.agent.skill_resolve import SkillCandidate, resolve_candidates
from durin.agent.skills_import import fetch_candidate


def test_resolve_clawhub_slug():
    res = resolve_candidates("clawhub:web-scraper")
    assert len(res.candidates) == 1
    assert res.candidates[0].kind == "clawhub"
    assert res.candidates[0].ref == "clawhub:web-scraper"
    assert res.candidates[0].name == "web-scraper"


def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _fake_clawhub(zip_bytes):
    def _go(url):
        if url.endswith("/skills/web-scraper"):
            return json.dumps({"latestVersion": {"version": "1.0.0"}}).encode()
        if "/download?" in url:
            return zip_bytes
        raise AssertionError(f"unexpected url: {url}")
    return _go


def test_fetch_clawhub_unpacks_zip(monkeypatch, tmp_path):
    zb = _zip({"SKILL.md": "---\nname: web-scraper\ndescription: d\n---\nbody\n",
               "scripts/run.sh": "echo hi\n"})
    monkeypatch.setattr("durin.agent.skills_import._http_get_bytes", _fake_clawhub(zb))
    cand = SkillCandidate("web-scraper", "clawhub:web-scraper", "clawhub")
    qdir = fetch_candidate(cand, quarantine_root=tmp_path)
    assert (qdir / "SKILL.md").is_file()
    assert (qdir / "scripts" / "run.sh").is_file()
    assert (qdir / ".scan.json").is_file()


def test_fetch_clawhub_skips_zip_slip(monkeypatch, tmp_path):
    zb = _zip({"SKILL.md": "---\nname: web-scraper\ndescription: d\n---\n",
               "../evil.sh": "pwned\n"})
    monkeypatch.setattr("durin.agent.skills_import._http_get_bytes", _fake_clawhub(zb))
    cand = SkillCandidate("web-scraper", "clawhub:web-scraper", "clawhub")
    qdir = fetch_candidate(cand, quarantine_root=tmp_path)
    assert (qdir / "SKILL.md").is_file()
    # traversal prevented: nothing written outside the quarantine dir
    assert not (tmp_path / ".." / "evil.sh").resolve().exists()
