from durin.loops.checks import verify_goal
from durin.loops.spec import parse_loop


def _spec(checks):
    return parse_loop({"name": "l", "workflow": "w", "goal": {"intent": "goal met", "checks": checks}})


async def _judge_yes(intent, assertions, evidence):
    return {"intent_met": True, "assertions": {a: True for a in assertions}}


async def test_scripts_pass_and_fail(tmp_path):
    spec = _spec([
        {"kind": "script", "required": True, "command": "true"},
        {"kind": "script", "required": False, "command": "false"},
    ])
    v = await verify_goal(spec, "out", judge=_judge_yes, work_dir=str(tmp_path), timeout_s=10)
    assert v.reached is True  # required passed; supporting failure doesn't block
    assert v.results[0]["passed"] is True and v.results[1]["passed"] is False


async def test_required_script_failure_blocks_even_if_judge_says_yes(tmp_path):
    spec = _spec([{"kind": "script", "required": True, "command": "false"}])
    v = await verify_goal(spec, "out", judge=_judge_yes, work_dir=str(tmp_path), timeout_s=10)
    assert v.reached is False


async def test_judge_can_be_stricter(tmp_path):
    async def judge_no(intent, assertions, evidence):
        return {"intent_met": False, "assertions": {}}
    v = await verify_goal(_spec([]), "out", judge=judge_no, work_dir=str(tmp_path), timeout_s=10)
    assert v.reached is False and v.intent_met is False


async def test_required_assertion_failure_blocks(tmp_path):
    async def judge(intent, assertions, evidence):
        return {"intent_met": True, "assertions": {a: False for a in assertions}}
    spec = _spec([{"kind": "assertion", "required": True, "text": "customer confirmed"}])
    v = await verify_goal(spec, "out", judge=judge, work_dir=str(tmp_path), timeout_s=10)
    assert v.reached is False


async def test_script_timeout_counts_as_failure(tmp_path):
    spec = _spec([{"kind": "script", "required": True, "command": "sleep 5"}])
    v = await verify_goal(spec, "out", judge=_judge_yes, work_dir=str(tmp_path), timeout_s=1)
    assert v.reached is False and "timeout" in v.results[0]["detail"]
