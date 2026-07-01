from durin.config.schema import AgentDefaults


def test_defaults():
    d = AgentDefaults()
    assert d.max_concurrent_interactive == 4
    assert d.concurrency_ceiling == 12
    assert d.max_concurrent_subagents == 3


def test_overridable():
    d = AgentDefaults(max_concurrent_interactive=6, concurrency_ceiling=20)
    assert d.max_concurrent_interactive == 6
    assert d.concurrency_ceiling == 20
