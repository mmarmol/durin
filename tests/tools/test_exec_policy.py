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


def _python_heredoc(chars: int) -> str:
    lines, total, i = [], 0, 0
    while total < chars:
        line = f"result_{i} = transform(records[{i}], mode='fast', retries={i % 5})  # step {i}\n"
        lines.append(line)
        total += len(line)
        i += 1
    return "python3 - <<'EOF'\n" + "".join(lines) + "EOF"


def _rm_script(chars: int) -> str:
    return "".join(f"rm -f build/obj/module_{i}.o\n" for i in range(chars // 28))


def _json_argument(chars: int) -> str:
    import json

    items: dict = {}
    while len(json.dumps(items)) < chars - 200:
        i = len(items)
        items[f"key_{i}"] = {"name": f"item {i}", "tags": ["a", "time", "env"], "count": i}
    return "echo '" + json.dumps(items) + "' > payload.json"


@pytest.mark.parametrize("command", [
    _python_heredoc(9_500), _rm_script(9_500), _json_argument(9_500),
], ids=["python-heredoc", "rm-f-script", "json-argument"])
def test_realistic_long_commands_are_checked_quickly(command):
    """Long commands agents really send, up to the cap, are checked, not
    refused, and the check is cheap (milliseconds; the bound here is generous
    for slow CI)."""
    import time

    from durin.agent.tools.shell import MAX_CHECKED_COMMAND_CHARS

    assert len(command) <= MAX_CHECKED_COMMAND_CHARS
    for restrict in (False, True):
        tool = ExecTool(restrict_to_workspace=restrict, working_dir="/w")
        start = time.perf_counter()
        refusal = tool._check(command, "/w")
        assert time.perf_counter() - start < 2.0
        assert refusal is None or refusal.kind == "deny"


@pytest.mark.parametrize("command", [
    _python_heredoc(18_000), _rm_script(18_000), _json_argument(18_000),
], ids=["python-heredoc-at-cap", "rm-f-script-at-cap", "json-argument-at-cap"])
def test_realistic_long_commands_at_the_new_cap_are_checked_quickly(command):
    """The same realistic shapes as test_realistic_long_commands_are_checked_quickly,
    scaled up near the current 20_000-character cap (fix round 1 lowered it from
    200_000 — see MAX_CHECKED_COMMAND_CHARS's own comment): a realistic command
    stays cheap right up to the cap's edge, not just well under it."""
    import time

    from durin.agent.tools.shell import MAX_CHECKED_COMMAND_CHARS

    assert len(command) <= MAX_CHECKED_COMMAND_CHARS
    for restrict in (False, True):
        tool = ExecTool(restrict_to_workspace=restrict, working_dir="/w")
        start = time.perf_counter()
        refusal = tool._check(command, "/w")
        assert time.perf_counter() - start < 2.0
        assert refusal is None or refusal.kind == "deny"


@pytest.mark.parametrize("unit", ["rm ", "cp ", "sudo -x ", "http://a ", "mv x "],
                         ids=["rm", "cp", "sudo", "url", "mv"])
def test_adversarial_repetition_at_the_cap_is_still_checked_quickly(unit):
    """The cap exists for the guard's worst case: an anchor word repeated up to
    the cap makes several patterns quadratic. At the cap that must stay well
    under a few seconds of work (a fraction of a second on a dev machine; the
    bound is generous for slow CI)."""
    import time

    from durin.agent.tools.shell import MAX_CHECKED_COMMAND_CHARS

    command = (unit * (MAX_CHECKED_COMMAND_CHARS // len(unit)))[:MAX_CHECKED_COMMAND_CHARS]
    assert len(command.strip()) <= MAX_CHECKED_COMMAND_CHARS
    start = time.perf_counter()
    ExecTool(restrict_to_workspace=True, working_dir="/w")._check(command, "/w")
    assert time.perf_counter() - start < 3.0


# ---------------------------------------------------------------------------
# Fix round 1: two shapes survive the linear-time pre-checks (see
# MAX_CHECKED_COMMAND_CHARS's own comment) because their gate literal is
# trivial to satisfy without ever forming a real match — a bare "-" for the
# rm-recursive hard-floor pattern, one leading "memory/" token for every
# memory-vault pattern. Both still drive the same O(n^2) the pre-checks
# otherwise fix. This is what forced the cap back down from 200_000 to
# 20_000, not a bypass: _check's verdict on these shapes is unchanged
# (see test_hard_floor_precheck_never_disagrees_with_the_unfiltered_regex
# and friends above), only how large a command may reach the regex at all.
# ---------------------------------------------------------------------------


def _rm_dash_only(chars: int) -> str:
    return ("rm -" * (chars // 4 + 1))[:chars]


def _memory_then_verb(chars: int, verb: str) -> str:
    prefix = "memory/z "
    body = verb * ((chars - len(prefix)) // len(verb) + 1)
    return (prefix + body)[:chars]


_CAP_SHAPES = {
    "rm-dash-only": _rm_dash_only,
    "memory-then-rm": lambda n: _memory_then_verb(n, "rm "),
    "memory-then-dd": lambda n: _memory_then_verb(n, "dd "),
    "memory-then-tee": lambda n: _memory_then_verb(n, "tee "),
    "memory-then-sed-i": lambda n: _memory_then_verb(n, "sed -i "),
    "memory-then-cp": lambda n: _memory_then_verb(n, "cp "),
    "memory-then-mv": lambda n: _memory_then_verb(n, "mv "),
}


@pytest.mark.parametrize("shape_name", list(_CAP_SHAPES), ids=list(_CAP_SHAPES))
def test_surviving_quadratic_shapes_are_checked_quickly_at_the_new_cap(shape_name):
    """At the (lowered) cap, even the two shapes the pre-checks cannot fix
    stay well under the 3s budget (generous for CI; the real numbers are in
    the fix-round report)."""
    import time

    from durin.agent.tools.shell import MAX_CHECKED_COMMAND_CHARS

    command = _CAP_SHAPES[shape_name](MAX_CHECKED_COMMAND_CHARS)
    assert len(command) <= MAX_CHECKED_COMMAND_CHARS
    start = time.perf_counter()
    ExecTool(restrict_to_workspace=True, working_dir="/w")._check(command, "/w")
    assert time.perf_counter() - start < 3.0


@pytest.mark.parametrize("shape_name", list(_CAP_SHAPES), ids=list(_CAP_SHAPES))
def test_surviving_quadratic_shapes_are_refused_unchecked_at_200k(shape_name):
    """At 200k characters — the size these shapes used to run uncapped at,
    taking tens of seconds each (the reason the cap came back down) — the
    length cap itself now refuses the command before any pattern runs, fast.
    This is the shape actually failing to reach the guard, not a test
    failure: it pins that raising the cap back to 200k without also fixing
    these two shapes would be unsafe."""
    import time

    from durin.agent.tools.shell import MAX_CHECKED_COMMAND_CHARS

    command = _CAP_SHAPES[shape_name](200_000)
    assert len(command) > MAX_CHECKED_COMMAND_CHARS
    start = time.perf_counter()
    refusal = ExecTool(restrict_to_workspace=True, working_dir="/w")._check(command, "/w")
    assert time.perf_counter() - start < 0.5
    assert refusal is not None and refusal.kind == "guard" and not refusal.approvable


def test_a_command_over_the_cap_is_refused_before_any_check():
    import time

    from durin.agent.tools.shell import MAX_CHECKED_COMMAND_CHARS

    command = "rm " * (MAX_CHECKED_COMMAND_CHARS // 3) + "rm x"
    assert len(command.strip()) > MAX_CHECKED_COMMAND_CHARS
    start = time.perf_counter()
    refusal = ExecTool()._check(command, "/w")
    assert time.perf_counter() - start < 0.5
    assert refusal is not None
    # Fail closed, and not something a person could approve past: the guard
    # never looked at it.
    assert refusal.kind == "guard" and not refusal.approvable
    assert f"{MAX_CHECKED_COMMAND_CHARS}" in refusal.message
    assert "write_file" in refusal.message


@pytest.mark.asyncio
async def test_an_approved_command_over_the_cap_still_never_runs(tmp_path, monkeypatch):
    import durin.agent.tools.shell as shell

    monkeypatch.setattr(shell, "MAX_CHECKED_COMMAND_CHARS", 1_000)
    marker = tmp_path / "ran"
    command = f"touch {marker} # " + "x" * 1_000
    out = await ExecTool(working_dir=str(tmp_path))._run(
        command, str(tmp_path), approved_rules=frozenset({RM_RULE}))
    assert out.startswith("Error: Command blocked")
    assert not marker.exists()


async def _ticks_while(coro) -> tuple[object, int]:
    """Run *coro* next to a 10 ms ticker; return its result and how many
    times the ticker ran meanwhile."""
    import asyncio

    ticks = 0
    stop = asyncio.Event()

    async def _ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(0)
    try:
        before = ticks
        result = await coro
        during = ticks - before
    finally:
        stop.set()
        await ticker
    return result, during


@pytest.mark.asyncio
async def test_the_guard_runs_off_the_event_loop(tmp_path, monkeypatch):
    """While a command is being checked, the loop keeps serving other chats:
    a check that takes 0.3 s leaves the ticker ticking throughout."""
    import time

    real_check = ExecTool._check

    def _slow_check(self, *args, **kwargs):
        time.sleep(0.3)
        return real_check(self, *args, **kwargs)

    monkeypatch.setattr(ExecTool, "_check", _slow_check)
    out, ticks = await _ticks_while(
        ExecTool(working_dir=str(tmp_path))._run("rm -rf build", str(tmp_path)))
    assert out.startswith("Error: Command blocked by deny pattern filter")
    assert ticks >= 10


@pytest.mark.asyncio
async def test_a_pathological_command_leaves_the_loop_turns_between_patterns(
    tmp_path, monkeypatch,
):
    """On a pathological command several patterns are slow, and a single
    regex call holds the GIL for its own duration, so the loop can only run
    between pattern calls. That needs the check to run in a worker thread, not
    on the loop's own thread. Asserted by thread identity: how many times a
    ticker gets in depends on scheduling, not on the code."""
    import threading

    import durin.agent.tools.shell as shell

    monkeypatch.setattr(shell, "MAX_CHECKED_COMMAND_CHARS", 9_000)
    loop_thread = threading.current_thread()
    ran_in: list[threading.Thread] = []
    real_check = ExecTool._check

    def _recording_check(self, *args, **kwargs):
        ran_in.append(threading.current_thread())
        return real_check(self, *args, **kwargs)

    monkeypatch.setattr(ExecTool, "_check", _recording_check)
    command = ("sudo -a " * 1_100) + "; rm -rf build"
    out = await ExecTool(working_dir=str(tmp_path))._run(command, str(tmp_path))
    assert out.startswith("Error: Command blocked by deny pattern filter")
    assert ran_in and all(t is not loop_thread for t in ran_in)


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


# ---------------------------------------------------------------------------
# Equivalence proof for the linear-time rewrite: a cheap literal pre-check
# (_HARD_FLOOR_PRECHECKS / _DENY_PRECHECKS / _guard_memory_mutation's own
# "memory/" gate) now runs before several patterns that used to be quadratic
# under an adversarial repeat of an anchor word with no trigger literal
# anywhere. None of the pattern TEXT changed — the pre-check only ever SKIPS
# a regex call that would have found nothing anyway (every literal it checks
# for is read directly off the pattern it gates: absent, the pattern cannot
# match). These tests hold that promise to a battery of cases: every FLOOR/
# NOT_FLOOR/APPROVALS case already in this file, the vault fixtures above,
# and new cases built to specifically probe the pre-check's edges (the
# trigger literal present but positioned so the real regex must still
# decide, a long run of the anchor before the trigger, the trigger absent
# entirely). Comparing "does the pre-check allow it through" against "does
# the raw, unfiltered regex match" — the actual old behavior, still runnable
# since no pattern text changed — is the equivalence proof requested for
# this item: run the same case against the old (unfiltered) and new (gated)
# form of every pattern.
# ---------------------------------------------------------------------------

_TRICKY_HARD_FLOOR_CASES = [
    # The literal present, but not at a real command position, or spelled as
    # part of a longer word — the pre-check must still let the real regex
    # decide (and the regex must still say no).
    "mkfs_is_just_a_directory_name/run.sh",
    "echo 'talking about mkfs and diskpart here'",
    "grep -r 'shutdown reboot poweroff halt' src/",
    "echo init 0 is not a runlevel here",
    "cat notes-about-durin-approvals-approve.txt",
    "grep -rn 'durin approvals approve' docs/",
    # A long run of the adversarial anchor BEFORE the real trigger appears —
    # the case that used to cost the most backtracking per starting position.
    "sudo -x " * 500 + "mkfs.ext4 /dev/sda",
    "sudo -x " * 500 + "shutdown -h now",
    "sudo -x " * 500 + "init 0",
    "sudo -x " * 500 + "durin approvals approve req-1",
    "rm " * 500 + "rm -rf /",
    # The trigger literal absent entirely (the actual adversarial shape).
    "sudo -x " * 500,
    "rm " * 500,
]

_TRICKY_DENY_HISTORY_CASES = [
    "echo 'history.jsonl is just a filename in this sentence'",
    "cp " * 500 + "cp a.txt history.jsonl",
    "mv " * 500 + "mv a.txt history.jsonl",
    "tee " * 500 + "tee history.jsonl",
    "dd " * 500 + "dd of=history.jsonl",
    "sed " * 500 + "sed -i history.jsonl",
    "cp " * 500,
]

_TRICKY_MEMORY_VAULT_CASES = [
    "echo 'memory/ is just a path fragment in this sentence'",
    "rm " * 500 + "rm -rf memory/people/ada.md",
    "cp " * 500 + "cp a.txt memory/x.md",
    "sed " * 500 + "sed -i memory/x.md",
    "dd " * 500 + "dd of=memory/x.md",
    "rm " * 500,
]


def test_hard_floor_precheck_never_disagrees_with_the_unfiltered_regex():
    import re

    from durin.agent.tools.shell import _HARD_FLOOR_PRECHECKS, _cheap_prefilter_ok

    cases = [c.lower() for c in (FLOOR + NOT_FLOOR + APPROVALS_BYPASS + _TRICKY_HARD_FLOOR_CASES)]
    for pattern, clauses in _HARD_FLOOR_PRECHECKS.items():
        for case in cases:
            allowed_through = _cheap_prefilter_ok(case, clauses)
            really_matches = re.search(pattern, case) is not None
            assert allowed_through or not really_matches, (
                f"pre-check skipped a real hard-floor match: pattern={pattern!r} case={case!r}"
            )


def test_deny_precheck_never_disagrees_with_the_unfiltered_regex():
    import re

    from durin.agent.tools.shell import _DENY_PRECHECKS, _cheap_prefilter_ok

    cases = [c.lower() for c in (FLOOR + NOT_FLOOR + _TRICKY_DENY_HISTORY_CASES)]
    for pattern, clauses in _DENY_PRECHECKS.items():
        for case in cases:
            allowed_through = _cheap_prefilter_ok(case, clauses)
            really_matches = re.search(pattern, case) is not None
            assert allowed_through or not really_matches, (
                f"pre-check skipped a real deny match: pattern={pattern!r} case={case!r}"
            )


def test_memory_vault_precheck_never_disagrees_with_the_unfiltered_scan():
    """_guard_memory_mutation's own "memory/" gate (checked once, before the
    per-pattern loop) must never skip a command any of the 7 vault patterns
    would have matched."""
    import re

    from durin.agent.tools.shell import ExecTool as _ExecTool

    cases = [c.lower() for c in (FLOOR + NOT_FLOOR + _TRICKY_MEMORY_VAULT_CASES)] + [
        cmd.lower() for cmd, _blocked in [
            ("sed -i 's/a/b/' memory/people/ada.md", True),
            ("dd if=/dev/zero of=memory/x.md bs=1 count=1", True),
            ("dd if=/dev/zero of=/tmp/out; ls memory/", False),
        ]
    ]
    for case in cases:
        gate_says_maybe = "memory/" in case
        really_matches = any(
            re.search(p, case) for p in _ExecTool._MEMORY_MUTATION_PATTERNS
        )
        assert gate_says_maybe or not really_matches, (
            f"memory/ gate skipped a real vault match: case={case!r}"
        )
        # And the full guard's own answer must be unaffected either way.
        assert (_ExecTool._guard_memory_mutation(case) is not None) == really_matches
