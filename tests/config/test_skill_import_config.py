from durin.config.schema import DEFAULT_SKILL_ALLOWLIST, Config


def test_default_allowlist_ships_vetted_vendors():
    al = Config().skills.security.allowlist
    assert al == DEFAULT_SKILL_ALLOWLIST
    assert len(al) == 12
    assert "github:anthropics/" in al and "github:obra/" in al
    # default_factory gives each Config its own list (no shared-mutable default)
    Config().skills.security.allowlist.append("x")
    assert "x" not in Config().skills.security.allowlist


def test_allowlist_camel_roundtrip():
    cfg = Config.model_validate({"skills": {"security": {"allowlist": ["github:NousResearch/"]}}})
    assert cfg.skills.security.allowlist == ["github:NousResearch/"]


def test_default_caps():
    si = Config().skills.security
    assert si.max_files == 100
    assert si.max_total_bytes == 3 * 1024 * 1024
    assert si.max_file_bytes == 1024 * 1024


def test_default_github_token_secret_empty():
    assert Config().skills.security.github_token_secret == ""


def test_llm_judge_defaults():
    j = Config().skills.security.llm_judge
    assert j.trigger == "off"         # opt-in: deterministic scan + human gate are the floor
    assert j.max_severity == "caution"
    assert j.model == ""


def test_new_fields_camel_roundtrip():
    cfg = Config.model_validate({"skills": {"security": {
        "githubTokenSecret": "gh_tok",
        "maxFiles": 50,
        "llmJudge": {"trigger": "always", "maxSeverity": "dangerous", "model": "fast"},
    }}})
    si = cfg.skills.security
    assert si.github_token_secret == "gh_tok"
    assert si.max_files == 50
    assert si.llm_judge.trigger == "always"
    assert si.llm_judge.max_severity == "dangerous"
    assert si.llm_judge.model == "fast"
