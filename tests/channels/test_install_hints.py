"""What a channel tells the user to install must be installable."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import durin

_CHANNELS = Path(durin.__file__).parent / "channels"
_PYPROJECT = Path(durin.__file__).parent.parent / "pyproject.toml"


def test_install_hints_name_the_package_and_an_extra_that_exist():
    """The distribution is durin-agent; a hint naming another package, or an
    extra it does not have, sends the user to an install that fails."""
    extras = set(tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
                 ["project"]["optional-dependencies"])
    wrong: list[str] = []
    for path in sorted(_CHANNELS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "durin-ai" in text:
            wrong.append(f"{path.name}: names durin-ai")
        for extra in re.findall(r"durin-agent\[([a-z0-9_-]+)\]", text):
            if extra not in extras:
                wrong.append(f"{path.name}: durin-agent[{extra}] is not an extra")
    assert not wrong, wrong


def test_a_pairing_reply_does_not_ask_the_guest_to_approve_themselves():
    """/pairing approve is the owner's command; the guest can only pass the
    code on."""
    from durin.pairing.store import format_pairing_reply

    reply = format_pairing_reply("ABC123")

    assert "ABC123" in reply
    assert "In this chat" not in reply
    assert "owner" in reply
