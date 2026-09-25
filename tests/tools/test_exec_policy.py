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
    # Shell wrappers: the wrapped command must still hit the floor.
    "bash -c 'mkfs.ext4 /dev/sda'",
    'sh -c "reboot"',
    "bash -lc 'shutdown now'",
    "`reboot`",
    "{ reboot; }",
    "env reboot",
    "time mkfs.ext4 /dev/sda",
    "sudo sh -c 'mkfs.ext4 /dev/sda'",
    # $HOME quoted with the closing quote right after HOME, not around the
    # whole target.
    'rm -rf "$HOME"/*',
    # halt / init 0 / systemctl halt|poweroff|reboot.
    "halt",
    "init 0",
    "systemctl halt",
    "systemctl reboot",
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
    # A word on the floor that is only a path component, not the whole
    # command name — the rest of the path continues past it: a folder name
    # must not hit the floor.
    "/w/shutdown/reboot.sh",
    "./reboot/run.sh",
    "sudo /opt/mkfs/bin/tool",
    "/Users/me/durin-worktrees/shutdown/.venv/bin/python -m pytest -q",
    "cd /x && /Users/me/durin-worktrees/reboot/.venv/bin/python -c 1",
    # "init 0" is command position only: a version number after a real
    # subcommand is not a runlevel.
    "npm init 0",
    "git init",
]

# Reasonable spellings of the command that would let the model approve its own
# pending request through a shell: a bare invocation, extra whitespace,
# "python -m durin ...", the command nested inside "bash -c '...'", the CLI's
# own options (-w/--workspace/--all/-c) between "approvals" and the verb, and
# a quoted "approvals" or "approve"/"reject" token.
APPROVALS_BYPASS = [
    "durin approvals approve req-123",
    "durin approvals reject req-123",
    "durin   approvals    approve   req-123",
    "python -m durin approvals approve req-123",
    "python3 -m durin approvals reject req-123",
    "bash -c 'durin approvals approve req-123'",
    'bash -c "durin approvals approve req-123"',
    "durin approvals -w /ws approve req-123",
    "durin approvals --workspace /ws approve req-123",
    "durin approvals --all approve req-123",
    "durin approvals -c cfg.json reject req-123",
    "durin 'approvals' approve req-123",
    'durin approvals "approve" req-123',
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
    # The rule is anchored at command position: the phrase appearing inside
    # a commit message or a grep pattern must not be refused.
    "git commit -m 'durin approvals approve flow'",
    "grep -rn 'durin approvals approve' docs/",
])
def test_approvals_near_misses_are_not_on_the_floor(command):
    refusal = ExecTool()._check(command, "/w")
    assert refusal is None or refusal.kind != "hard_floor"


def test_fork_bomb_pattern_is_not_quadratic_on_a_long_word():
    """A long argument with no fork bomb in it must not stall the guard: the
    (?<![\\w:]) lookbehind stops the function-name group from restarting the
    match at every character of a long word."""
    import re
    import time

    fork_bomb_pattern = next(p for p in _HARD_FLOOR_PATTERNS if "(?P<fn>" in p)
    command = ("echo " + "a" * 40_000).lower()

    start = time.perf_counter()
    result = re.search(fork_bomb_pattern, command)
    elapsed = time.perf_counter() - start

    assert result is None
    assert elapsed < 1.0


# Repetitions that drive the guard's regexes to their worst case: an anchor
# word the patterns retry at every occurrence, and the pairs ("sed ... -i",
# "dd ... of=") that used to scan open-endedly twice.
_ADVERSARIAL = ["rm ", "rm -r ", "dd ", "dd of=", "sed -i ", "mv ", "cp a b ", "sudo -a ",
                ">>", "(", "http://a ", "a/"]


@pytest.mark.parametrize("unit", _ADVERSARIAL)
def test_the_guard_stays_bounded_on_adversarial_input_up_to_the_cap(unit):
    """At the longest command the guard checks, even pathological input is
    checked in well under a second (the bound here is generous for slow CI);
    unbounded, these inputs held the event loop for seconds to minutes."""
    import time

    from durin.agent.tools.shell import MAX_CHECKED_COMMAND_CHARS

    command = (unit * (MAX_CHECKED_COMMAND_CHARS // len(unit) + 1))[:MAX_CHECKED_COMMAND_CHARS]
    for restrict in (False, True):
        tool = ExecTool(restrict_to_workspace=restrict, working_dir="/w")
        start = time.perf_counter()
        tool._check(command, "/w")
        assert time.perf_counter() - start < 3.0


def test_a_command_over_the_cap_is_refused_before_any_check():
    import time

    from durin.agent.tools.shell import MAX_CHECKED_COMMAND_CHARS

    command = "rm " * 400_000
    start = time.perf_counter()
    refusal = ExecTool()._check(command, "/w")
    assert time.perf_counter() - start < 0.5
    assert refusal is not None
    # Fail closed, and not something a person could approve past: the guard
    # never looked at it.
    assert refusal.kind == "guard" and not refusal.approvable
    assert str(MAX_CHECKED_COMMAND_CHARS) in refusal.message
    assert "write_file" in refusal.message

    at_cap = "echo " + "a" * (MAX_CHECKED_COMMAND_CHARS - len("echo "))
    assert ExecTool()._check(at_cap, "/w") is None


@pytest.mark.asyncio
async def test_an_approved_command_over_the_cap_still_never_runs(tmp_path):
    from durin.agent.tools.shell import MAX_CHECKED_COMMAND_CHARS

    marker = tmp_path / "ran"
    command = f"touch {marker} # " + "x" * MAX_CHECKED_COMMAND_CHARS
    out = await ExecTool(working_dir=str(tmp_path))._run(
        command, str(tmp_path), approved_rules=frozenset({RM_RULE}))
    assert out.startswith("Error: Command blocked")
    assert not marker.exists()


@pytest.mark.parametrize(("command", "blocked"), [
    ("sed -i 's/a/b/' memory/people/ada.md", True),
    ("sed -i.bak -e 's/a/b/' memory/x.md", True),
    ("sed --in-place 's/a/b/' ./memory/x.md", True),
    ("sed -e 's/a/b/' -i memory/x.md", True),
    ("sed 's/-i/x/' memory/x.md", True),
    ("sed 's/a/b/' memory/x.md", False),
    ("sed -i 's/a/b/' notes.md; cat memory/x.md", False),
    ("dd if=/dev/zero of=memory/x.md bs=1 count=1", True),
    ("dd of=/tmp/out if=memory/x.md", True),
    ("dd if=memory/x.md of=/tmp/out", False),
    ("dd if=/dev/zero of=/tmp/out; ls memory/", False),
])
def test_the_memory_vault_guard_for_sed_and_dd(command, blocked):
    """The sed -i and dd of= rules commit to the first flag they meet; that
    changes their cost, never what they refuse."""
    assert (ExecTool._guard_memory_mutation(command.lower()) is not None) is blocked
