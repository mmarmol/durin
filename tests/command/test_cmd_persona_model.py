import asyncio
from types import SimpleNamespace
from durin.command.router import CommandContext
from durin.config.schema import Config, PersonaConfig
from durin.session.manager import Session


def _ctx(args, model=None):
    cfg = Config(
        personas={
            "tutor": PersonaConfig(soul="default", model=model),
            "plain": PersonaConfig(soul="default", model=None),
        }
    )
    loop = SimpleNamespace(app_config=cfg)
    msg = SimpleNamespace(channel="cli", chat_id="direct", metadata={})
    session = Session(key="cli:direct")
    return CommandContext(
        msg=msg,
        session=session,
        key="cli:direct",
        raw=f"/persona {args}".strip(),
        args=args,
        loop=loop,
    )


def test_switch_to_model_pinned_persona_applies_model():
    ctx = _ctx("tutor", model="anthropic claude-3-5-sonnet-20241022")
    calls = []
    ctx.loop.set_model_preset = lambda *a, **k: calls.append((a, k))
    from durin.command.builtin import cmd_persona
    asyncio.run(cmd_persona(ctx))
    assert calls, "expected set_model_preset to be called for a model-pinned persona"
    assert calls[0] == (("anthropic claude-3-5-sonnet-20241022",), {"publish_update": True})


def test_switch_to_persona_without_model_does_not_apply_model():
    ctx = _ctx("plain", model=None)
    calls = []
    ctx.loop.set_model_preset = lambda *a, **k: calls.append((a, k))
    from durin.command.builtin import cmd_persona
    asyncio.run(cmd_persona(ctx))
    assert not calls, "expected set_model_preset NOT to be called for a persona with no model"
