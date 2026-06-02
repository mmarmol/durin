from durin.utils.prompt_templates import render_template


def test_curation_prompt_renders_with_catalog_and_usage():
    out = render_template("agent/skill_curation.md", strip=True,
                          catalog_json='{"a": "body"}', usage_json='{}')
    assert "content" in out.lower()
    assert "fuse" in out and "evolve" in out
    assert '{"a": "body"}' in out          # catalog embedded
    assert "actions" in out                 # output schema present
