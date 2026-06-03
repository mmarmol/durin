from durin.agent.skills_import import decide_action

AL = ["github:NousResearch/"]


def test_dangerous_blocks_even_allowlisted():
    assert decide_action("github:NousResearch/x", verdict="dangerous", carries_code=False, allowlist=AL) == "block"


def test_caution_confirms():
    assert decide_action("github:NousResearch/x", verdict="caution", carries_code=False, allowlist=AL) == "confirm"


def test_code_confirms_even_safe_and_allowlisted():
    assert decide_action("github:NousResearch/x", verdict="safe", carries_code=True, allowlist=AL) == "confirm"


def test_out_of_allowlist_confirms_even_safe_no_code():
    assert decide_action("github:rando/x", verdict="safe", carries_code=False, allowlist=AL) == "confirm"


def test_allowlisted_safe_no_code_allows():
    assert decide_action("github:NousResearch/x", verdict="safe", carries_code=False, allowlist=AL) == "allow"


def test_empty_allowlist_everything_confirms():
    assert decide_action("path://x", verdict="safe", carries_code=False, allowlist=[]) == "confirm"
