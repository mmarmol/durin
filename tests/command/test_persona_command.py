import pytest
from types import SimpleNamespace
from durin.command.router import CommandContext
from durin.command.builtin import cmd_persona
from durin.config.schema import Config, PersonaConfig
from durin.session.manager import Session


def _ctx(args, session):
    cfg = Config(personas={"mine": PersonaConfig(soul="default")})
    loop = SimpleNamespace(app_config=cfg)
    msg = SimpleNamespace(channel="cli", chat_id="direct", metadata={})
    return CommandContext(msg=msg, session=session, key="cli:direct", raw=f"/persona {args}".strip(), args=args, loop=loop)


@pytest.mark.asyncio
async def test_status_lists_available():
    session = Session(key="cli:direct")
    out = await cmd_persona(_ctx("", session))
    assert "mine" in out.content and "researcher" in out.content  # user + built-in


@pytest.mark.asyncio
async def test_set_known_persona_updates_session():
    session = Session(key="cli:direct")
    out = await cmd_persona(_ctx("mine", session))
    assert session.metadata["persona"] == "mine"
    assert "mine" in out.content


@pytest.mark.asyncio
async def test_default_clears_persona():
    session = Session(key="cli:direct")
    session.metadata["persona"] = "mine"
    await cmd_persona(_ctx("default", session))
    assert "persona" not in session.metadata


@pytest.mark.asyncio
async def test_unknown_persona_errors_and_does_not_set():
    session = Session(key="cli:direct")
    out = await cmd_persona(_ctx("ghost", session))
    assert "Unknown persona" in out.content
    assert "persona" not in session.metadata
