"""The exec policy: the hard floor, and refusals that carry their rules."""
from __future__ import annotations

import pytest

from durin.agent.runner import AgentRunner
from durin.agent.tools.shell import _HARD_FLOOR_PATTERNS, ExecTool

RM_RULE = r"\brm\s+-[rf]{1,2}\b"
DD_RULE = r"\bdd\s+if="

FLOOR = [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf / --no-preserve-root",
    "sudo rm -rf /",
    "rm -Rf /",
    "rm -r -f /",
    "rm --recursive --force /",
    "rm -rf -- /",
    'rm -rf "/"',
    "rm -rf ~",
    "rm -fr ~",
    "rm -rf ~/",
    "rm -rf ~/*",
    "cd /tmp && rm -rf ~",
    "rm -rf $HOME",
    "rm -rf ${HOME}",
    'rm -rf "$HOME"',
    "rm -rf $HOME/",
    "mkfs.ext4 /dev/sdb1",
    "sudo mkfs -t ext4 /dev/sdb1",
    "mkfs /dev/sda",
    "diskpart",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "dd of=/dev/nvme0n1 if=image.iso",
    "sudo dd if=x.img of=/dev/disk2",
    "cat image.iso > /dev/sdb",
    "echo x >/dev/nvme0n1",
    r"echo x > \\.\PhysicalDrive0",
    ":(){ :|:& };:",
    ":(){ :|: & };:",
    "bomb(){ bomb|bomb& };bomb",
    "shutdown -h now",
    "sudo shutdown -r now",
    "sudo -u root shutdown now",
    "reboot",
    "echo done && reboot",
    "systemctl poweroff",
    "/sbin/reboot",
    "poweroff",
]

NOT_FLOOR = [
    "rm -rf build",
    "rm -rf /tmp/build",
    "rm -rf ~/projects/x",
    "rm -rf $HOME/projects",
    "rm -rf /home/me/tmp",
    "rm -f /",
    "rm -rf ./build; ls /",
    "man mkfs",
    "grep -r mkfs src/",
    "dd if=/dev/zero of=disk.img bs=1M count=10",
    "echo hi > /dev/null",
    "pytest tests/test_shutdown.py",
    "grep -r shutdown src/",
    "git commit -m 'fix reboot loop'",
    "./shutdown.sh",
    "echo reboot",
    "systemctl status reboot.target",
]

# Reasonable spellings of the command that would let the model approve its own
# pending request through a shell: a bare invocation, extra whitespace,
# "python -m durin ...", and the command nested inside "bash -c '...'".
APPROVALS_BYPASS = [
    "durin approvals approve req-123",
    "durin approvals reject req-123",
    "durin   approvals    approve   req-123",
    "python -m durin approvals approve req-123",
    "python3 -m durin approvals reject req-123",
    "bash -c 'durin approvals approve req-123'",
    'bash -c "durin approvals approve req-123"',
]


@pytest.mark.parametrize("command", FLOOR)
def test_floor_commands_are_refused_and_never_approvable(command):
    refusal = ExecTool()._check(command, "/w")
    assert refusal is not None and refusal.kind == "hard_floor"
    assert not refusal.approvable
    assert refusal.message.startswith("Error: Command blocked by the exec hard floor")


@pytest.mark.parametrize("command", NOT_FLOOR)
def test_near_misses_are_not_on_the_floor(command):
    refusal = ExecTool()._check(command, "/w")
    assert refusal is None or refusal.kind != "hard_floor"


def test_floor_holds_against_allow_patterns_and_any_approval():
    tool = ExecTool(allow_patterns=[r".*"])
    everything = (frozenset(tool.deny_patterns) | frozenset(_HARD_FLOOR_PATTERNS)
                  | {"tools.exec.allow_patterns"})
    refusal = tool._check("rm -rf / --no-preserve-root", "/w", approved_rules=everything)
    assert refusal is not None and refusal.kind == "hard_floor"


def test_floor_refusal_is_a_policy_boundary_for_the_runner():
    text = ExecTool()._guard_command("rm -fr ~", "/w")
    assert AgentRunner._is_command_policy_block(text)
    assert "not even with the user's approval" in text
    assert "allow_patterns" not in text


def test_deny_refusal_carries_every_matched_rule():
    refusal = ExecTool()._check("rm -rf build && dd if=a of=b", "/w")
    assert refusal.kind == "deny" and refusal.approvable
    assert refusal.rules == (RM_RULE, DD_RULE)
    assert refusal.headline == (
        f"Error: Command blocked by deny pattern filter (rule: {RM_RULE}, rule: {DD_RULE})")


def test_approval_lifts_only_the_approved_rules():
    tool = ExecTool()
    cmd = "rm -rf build && dd if=a of=b"
    partial = tool._check(cmd, "/w", approved_rules=frozenset({RM_RULE}))
    assert partial.kind == "deny" and partial.rules == (DD_RULE,)
    assert tool._check(cmd, "/w", approved_rules=frozenset({RM_RULE, DD_RULE})) is None


def test_approval_does_not_lift_the_other_guards():
    refusal = ExecTool()._check("rm -rf memory/stable", "/w",
                                approved_rules=frozenset({RM_RULE}))
    assert refusal is not None and refusal.kind == "guard"
    assert "memory_upsert_entity" in refusal.message
    assert not refusal.approvable


def test_allowlist_refusal_is_structural_and_approvable():
    tool = ExecTool(allow_patterns=[r"^ls\b"])
    refusal = tool._check("cat notes.txt", "/w")
    assert refusal.kind == "allowlist" and refusal.approvable
    assert refusal.rules == ("tools.exec.allow_patterns",)
    assert tool._check("cat notes.txt", "/w", approved_rules=frozenset(refusal.rules)) is None


def test_guard_command_still_returns_the_refusal_text():
    tool = ExecTool()
    assert tool._guard_command("rm -rf build", "/w") == tool._check("rm -rf build", "/w").message
    assert tool._guard_command("echo ok", "/w") is None


@pytest.mark.parametrize("command", APPROVALS_BYPASS)
def test_approvals_cli_is_on_the_hard_floor(command):
    """The model must not be able to approve its own pending request by
    shelling out to the CLI. This backs up the CLI's own TTY requirement
    (approvals approve/reject refuse to run without one) as a second,
    independent layer that holds even if that check were ever bypassed."""
    tool = ExecTool(allow_patterns=[r".*"])
    refusal = tool._check(command, "/w")
    assert refusal is not None and refusal.kind == "hard_floor"
    assert not refusal.approvable


def test_approvals_cli_floor_holds_against_any_approval():
    tool = ExecTool(allow_patterns=[r".*"])
    cmd = "durin approvals approve req-123"
    everything = (frozenset(tool.deny_patterns) | frozenset(_HARD_FLOOR_PATTERNS)
                  | {"tools.exec.allow_patterns"})
    refusal = tool._check(cmd, "/w", approved_rules=everything)
    assert refusal is not None and refusal.kind == "hard_floor"


@pytest.mark.parametrize("command", [
    "durin approvals list",
    "durin approvals discard req-123",
    "echo 'approving requests is done via durin approvals'",
])
def test_approvals_near_misses_are_not_on_the_floor(command):
    refusal = ExecTool()._check(command, "/w")
    assert refusal is None or refusal.kind != "hard_floor"
