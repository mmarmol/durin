"""P6 — skills.install_policy default + acceptance."""
from durin.config.schema import Config


def _cfg(d):
    return Config.model_validate(d)


def test_install_policy_default_is_approve():
    assert _cfg({}).skills.install_policy == "approve"


def test_install_policy_accepts_never_and_auto():
    for v in ("never", "approve", "auto"):
        assert _cfg({"skills": {"install_policy": v}}).skills.install_policy == v
