from durin.telemetry.schema import EVENTS, SkillAuthoredEvent


def test_skill_authored_registered():
    assert "skill.authored" in EVENTS
    assert EVENTS["skill.authored"] is SkillAuthoredEvent


def test_skill_authored_fields():
    ann = SkillAuthoredEvent.__annotations__
    for field in ("name", "actor", "ramp", "composition", "files_count"):
        assert field in ann
