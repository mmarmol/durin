import pytest

from durin.agent.skills_import import run_install_specs


@pytest.mark.asyncio
async def test_run_install_specs_calls_exec_for_each():
    calls = []

    async def mock_exec(*, command):
        calls.append(command)
        return f"output of {command}"

    specs = [
        {"kind": "brew", "value": "gh", "command": "brew install gh", "needs_privileges": False},
        {"kind": "brew", "value": "jq", "command": "brew install jq", "needs_privileges": False},
    ]
    results = await run_install_specs(specs, exec_run=mock_exec)
    assert len(results) == 2
    assert results[0]["command"] == "brew install gh"
    assert results[0]["success"] is True
    assert calls == ["brew install gh", "brew install jq"]


@pytest.mark.asyncio
async def test_run_install_specs_partial_failure():
    async def mock_exec(*, command):
        if "fail" in command:
            raise RuntimeError("boom")
        return "ok"

    specs = [
        {"command": "brew install gh", "needs_privileges": False},
        {"command": "brew install fail", "needs_privileges": False},
    ]
    results = await run_install_specs(specs, exec_run=mock_exec)
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert "boom" in results[1]["error"]


@pytest.mark.asyncio
async def test_run_install_specs_treats_a_blocked_command_as_failed():
    # The exec runner can refuse instead of raising: it returns the policy's
    # own "Error: Command blocked" text as ordinary output. That refusal must
    # not be recorded as a successful install step.
    async def mock_exec(*, command):
        return "Error: Command blocked by deny pattern filter (rule: rm -rf)"

    specs = [{"command": "rm -rf /", "needs_privileges": False}]
    results = await run_install_specs(specs, exec_run=mock_exec)
    assert results[0]["success"] is False
    assert results[0]["output"].startswith("Error: Command blocked")
