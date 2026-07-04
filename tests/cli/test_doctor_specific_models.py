"""`durin doctor` flags specific-model knobs that don't resolve to a servable
(provider, model) — the class of silent failure-open breakage found live
(judge model left behind on a provider change → 404 on every call)."""

from __future__ import annotations

from unittest.mock import patch

from durin.cli.doctor import check_specific_models
from durin.config.schema import AuxModelConfig, Config


def _cfg() -> Config:
    c = Config()
    c.agents.defaults.provider = "nvidia"
    c.agents.defaults.model = "nemotron-3"
    c.providers.nvidia.api_key = "k-nvidia"
    return c


def _run(cfg):
    with patch("durin.cli.doctor.load_config", return_value=cfg):
        return check_specific_models()


def test_ok_when_nothing_configured():
    r = _run(_cfg())
    assert r.status == "ok"
    assert "none configured" in r.message


def test_warns_on_orphaned_judge_model():
    # The live incident: a judge model whose provider is gone/unconfigured.
    cfg = _cfg()
    cfg.skills.security.llm_judge.model = "glm-5-turbo"
    r = _run(cfg)
    assert r.status == "warn"
    assert "skills.security.llm_judge" in r.message
    assert "glm-5-turbo" in r.message
    assert r.fix


def test_ok_when_bare_name_autodetects():
    cfg = _cfg()
    cfg.providers.zhipu.api_key = "k-zhipu"
    cfg.skills.security.llm_judge.model = "glm-5-turbo"   # glm → zhipu, configured
    r = _run(cfg)
    assert r.status == "ok"
    assert "glm-5-turbo" in r.message


def test_warns_on_explicit_unconfigured_provider():
    cfg = _cfg()
    cfg.agents.aux_models.vision = AuxModelConfig(model="some-vision", provider="gemini")
    r = _run(cfg)
    assert r.status == "warn"
    assert "aux_models.vision" in r.message
    assert "gemini" in r.message


def test_warns_on_unresolvable_preset_ref():
    cfg = _cfg()
    cfg.agents.aux_models.memory = AuxModelConfig(preset="ghost-preset")
    r = _run(cfg)
    assert r.status == "warn"
    assert "ghost-preset" in r.message


def test_ok_with_healthy_pairs():
    cfg = _cfg()
    cfg.providers.zhipu.api_key = "k-zhipu"
    cfg.skills.security.llm_judge.model = "glm-4.6"
    cfg.skills.security.llm_judge.provider = "zhipu"
    cfg.memory.dream.model_override = "glm-4.6"           # autodetects zhipu
    r = _run(cfg)
    assert r.status == "ok"
