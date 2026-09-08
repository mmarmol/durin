"""Tests for build_status_content cache hit rate display."""

from durin.utils.helpers import build_status_content


def test_status_shows_cache_hit_rate():
    content = build_status_content(
        version="0.1.0",
        model="glm-4-plus",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 2000, "completion_tokens": 300, "cached_tokens": 1200},
        context_window_tokens=128000,
        session_msg_count=10,
        context_tokens_estimate=5000,
    )
    assert "60% cached" in content
    assert "2000 in / 300 out" in content
    assert "Tasks: 0 active" in content


def test_status_no_cache_info():
    """Without cached_tokens, display should not show cache percentage."""
    content = build_status_content(
        version="0.1.0",
        model="glm-4-plus",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 2000, "completion_tokens": 300},
        context_window_tokens=128000,
        session_msg_count=10,
        context_tokens_estimate=5000,
    )
    assert "cached" not in content.lower()
    assert "2000 in / 300 out" in content
    assert "Tasks: 0 active" in content


def test_status_zero_cached_tokens():
    """cached_tokens=0 should not show cache percentage."""
    content = build_status_content(
        version="0.1.0",
        model="glm-4-plus",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 2000, "completion_tokens": 300, "cached_tokens": 0},
        context_window_tokens=128000,
        session_msg_count=10,
        context_tokens_estimate=5000,
    )
    assert "cached" not in content.lower()


def test_status_100_percent_cached():
    content = build_status_content(
        version="0.1.0",
        model="glm-4-plus",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 1000, "completion_tokens": 100, "cached_tokens": 1000},
        context_window_tokens=128000,
        session_msg_count=5,
        context_tokens_estimate=3000,
    )
    assert "100% cached" in content


def test_status_context_pct_uses_budget_not_total():
    """With no compaction trigger supplied, fall back to the input budget
    rather than the raw context window."""
    content = build_status_content(
        version="0.1.0",
        model="test",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 2000, "completion_tokens": 300},
        context_window_tokens=128000,
        session_msg_count=10,
        context_tokens_estimate=120000,
        max_completion_tokens=8192,
    )
    # budget = 128000 - 8192 - 1024 = 118784; pct = 120000/118784*100 ≈ 101%
    assert "(101% to compaction)" in content


def test_status_context_pct_capped_at_999():
    """Extreme overflow should be capped at 999."""
    content = build_status_content(
        version="0.1.0",
        model="test",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 2000, "completion_tokens": 300},
        context_window_tokens=10000,
        session_msg_count=10,
        context_tokens_estimate=100000,
        max_completion_tokens=4096,
    )
    assert "(999% to compaction)" in content


def test_status_composition_section_breakdown():
    """When composition_payload is passed, /status renders the two-bucket breakdown."""
    payload = {
        "stable_tokens": 2916,
        "stable_breakdown": {
            "identity": 492, "bootstrap": 921,
            "skills_active": 953, "skills_catalog": 550,
        },
        "context_tokens": 0,
        "volatile_tokens": 0,
        "volatile_breakdown": {},
        "history_msg_tokens": 418,
        "current_msg_tokens": 55,
        "tools_tokens": 1221,
        "estimated_total": 4610,
    }
    content = build_status_content(
        version="0.1.0",
        model="m",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 0, "completion_tokens": 0},
        context_window_tokens=128000,
        session_msg_count=2,
        context_tokens_estimate=0,
        composition_payload=payload,
    )
    assert "Last turn — composition" in content
    assert "Prompt tokens" in content
    assert "4610" in content
    assert "From this conversation" in content
    assert "From infrastructure" in content
    # Sub-components appear inside their bucket.
    assert "Prior turns" in content
    assert "Identity" in content
    assert "Tool definitions" in content


def test_status_composition_section_omitted_when_no_payload():
    """No composition_payload → no composition section (default behaviour)."""
    content = build_status_content(
        version="0.1.0",
        model="m",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 0, "completion_tokens": 0},
        context_window_tokens=128000,
        session_msg_count=2,
        context_tokens_estimate=0,
    )
    assert "Last turn — composition" not in content


def test_status_composition_section_handles_empty_payload():
    """An empty (zero-token) payload yields no composition section
    rather than '0%' rows for everything."""
    content = build_status_content(
        version="0.1.0",
        model="m",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 0, "completion_tokens": 0},
        context_window_tokens=128000,
        session_msg_count=2,
        context_tokens_estimate=0,
        composition_payload={"estimated_total": 0},
    )
    assert "Last turn — composition" not in content


def test_status_composition_section_shows_frozen_turn_suffix():
    """When the composition payload carries ``eager_frozen_turn`` (this
    build reused a frozen eager surface), the two memory rows — and only
    those two — get a "(frozen at turn N)" suffix."""
    payload = {
        "stable_tokens": 3000,
        "stable_breakdown": {
            "identity": 492, "bootstrap": 921,
            "memory_pinned": 300, "memory_hot": 400,
        },
        "context_tokens": 0,
        "volatile_tokens": 0,
        "volatile_breakdown": {},
        "history_msg_tokens": 418,
        "current_msg_tokens": 55,
        "tools_tokens": 1221,
        "estimated_total": 5807,
        "eager_frozen_turn": 3,
    }
    content = build_status_content(
        version="0.1.0",
        model="m",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 0, "completion_tokens": 0},
        context_window_tokens=128000,
        session_msg_count=4,
        context_tokens_estimate=0,
        composition_payload=payload,
    )
    assert "Memory pinned" in content
    assert "Memory hot layer" in content
    lines = content.splitlines()
    pinned_line = next(line for line in lines if "Memory pinned" in line)
    hot_line = next(line for line in lines if "Memory hot layer" in line)
    identity_line = next(line for line in lines if "Identity" in line)
    assert "(frozen at turn 3)" in pinned_line
    assert "(frozen at turn 3)" in hot_line
    assert "(frozen at turn 3)" not in identity_line


def test_status_composition_section_omits_frozen_suffix_when_live():
    """No ``eager_frozen_turn`` in the payload (a live render) means no
    suffix on the memory rows."""
    payload = {
        "stable_tokens": 700,
        "stable_breakdown": {"memory_pinned": 300, "memory_hot": 400},
        "context_tokens": 0,
        "volatile_tokens": 0,
        "volatile_breakdown": {},
        "history_msg_tokens": 0,
        "current_msg_tokens": 0,
        "tools_tokens": 0,
        "estimated_total": 700,
    }
    content = build_status_content(
        version="0.1.0",
        model="m",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 0, "completion_tokens": 0},
        context_window_tokens=128000,
        session_msg_count=1,
        context_tokens_estimate=0,
        composition_payload=payload,
    )
    assert "frozen at turn" not in content


def test_status_context_pct_prefers_the_compaction_trigger():
    """The meter is denominated by where compaction actually fires. The raw
    window and the consolidator's own input budget can differ from the trigger
    by ~2x, so either would read as far more headroom than the session has."""
    kwargs = dict(
        version="0.1.0",
        model="test",
        start_time=1000000.0,
        last_usage={"prompt_tokens": 2000, "completion_tokens": 300},
        context_window_tokens=231072,
        session_msg_count=10,
        context_tokens_estimate=86_652,
        max_completion_tokens=131072,
    )
    with_trigger = build_status_content(**kwargs, compaction_trigger_tokens=173_304)
    assert "(50% to compaction)" in with_trigger

    # Same session measured against the legacy budget (231072-131072-1024)
    # reads almost twice as full.
    without = build_status_content(**kwargs)
    assert "(87% to compaction)" in without
