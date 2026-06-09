from durin.utils.prompt_templates import render_template


def test_curation_prompt_renders_with_catalog_and_usage():
    out = render_template("agent/skill_curation.md", strip=True,
                          catalog_json='{"a": "body"}', usage_json='{}')
    assert "content" in out.lower()
    assert "fuse" in out and "evolve" in out
    assert '{"a": "body"}' in out          # catalog embedded
    assert "actions" in out                 # output schema present


def test_curation_prompt_renders_observations_and_declined_sections():
    out = render_template("agent/skill_curation.md", strip=True,
                          catalog_json='{"a": "body"}', usage_json='{}',
                          observations_json='[{"id": 1, "issue": "obs-text"}]',
                          declined_json='[{"id": 9, "issue": "declined-text"}]')
    assert '"issue": "obs-text"' in out
    assert '"issue": "declined-text"' in out
    assert "disposition" in out            # output schema asks for dispositions
    assert "keep" in out and "applied" in out and "declined" in out
