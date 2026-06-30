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
async def test_persona_list_renders_markdown_not_text():
    """Test that /persona (no-args) output renders as formatted markdown, not verbatim text."""
    session = Session(key="cli:direct")
    out = await cmd_persona(_ctx("", session))
    assert out.metadata.get("render_as") != "text"
