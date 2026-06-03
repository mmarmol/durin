from durin.config.schema import Config


def test_default_allowlist_empty():
    assert Config().memory.skill_import.allowlist == []


def test_allowlist_camel_roundtrip():
    cfg = Config.model_validate({"memory": {"skillImport": {"allowlist": ["github:NousResearch/"]}}})
    assert cfg.memory.skill_import.allowlist == ["github:NousResearch/"]


def test_default_caps():
    si = Config().memory.skill_import
    assert si.max_files == 100
    assert si.max_total_bytes == 3 * 1024 * 1024
    assert si.max_file_bytes == 1024 * 1024


def test_default_install_specs_policy_is_never():
    assert Config().memory.skill_import.install_specs_policy == "never"


def test_default_github_token_secret_empty():
    assert Config().memory.skill_import.github_token_secret == ""


def test_llm_judge_defaults():
    j = Config().memory.skill_import.llm_judge
    assert j.trigger == "off"         # opt-in: deterministic scan + human gate are the floor
    assert j.max_severity == "caution"
    assert j.model == ""


def test_new_fields_camel_roundtrip():
    cfg = Config.model_validate({"memory": {"skillImport": {
        "githubTokenSecret": "gh_tok",
        "maxFiles": 50,
        "installSpecsPolicy": "ask",
        "llmJudge": {"trigger": "always", "maxSeverity": "dangerous", "model": "fast"},
    }}})
    si = cfg.memory.skill_import
    assert si.github_token_secret == "gh_tok"
    assert si.max_files == 50
    assert si.install_specs_policy == "ask"
    assert si.llm_judge.trigger == "always"
    assert si.llm_judge.max_severity == "dangerous"
    assert si.llm_judge.model == "fast"
