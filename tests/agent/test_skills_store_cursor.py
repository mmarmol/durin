from durin.agent import skills_store as ss


def test_needs_curation_tracks_body_changes(tmp_path):
    ws = tmp_path / "ws"
    d = ws / "skills" / "git-helper"; d.mkdir(parents=True)
    md = d / "SKILL.md"
    md.write_text("---\nname: git-helper\n---\nv1 body\n", encoding="utf-8")

    assert ss.needs_curation(ws, "git-helper") is True          # never reviewed
    ss.mark_curated(ws, "git-helper")                            # stamp body hash
    assert ss.needs_curation(ws, "git-helper") is False         # unchanged → skip
    assert ss.needs_curation(ws, "git-helper") is False         # stamping didn't re-trigger

    md.write_text("---\nname: git-helper\n---\nv2 changed\n", encoding="utf-8")
    assert ss.needs_curation(ws, "git-helper") is True          # body changed
