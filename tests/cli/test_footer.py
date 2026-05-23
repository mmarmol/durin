"""Tests for the persistent footer (D1.6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from durin.cli.footer import build_footer_html, build_footer_text


class _FakeSession:
    def __init__(self, messages=None, display_name: str | None = None) -> None:
        self.messages = list(messages or [])
        self.metadata = {"display_name": display_name} if display_name else {}


class _FakeSessionManager:
    def __init__(self, session: _FakeSession | None = None) -> None:
        self._session = session

    def get_or_create(self, _key: str) -> _FakeSession:
        if self._session is None:
            self._session = _FakeSession()
        return self._session


def _fake_loop(
    tmp_path: Path,
    *,
    model: str = "claude-opus-4-7",
    preset: str = "default",
    context_window: int = 200_000,
    session: _FakeSession | None = None,
):
    return SimpleNamespace(
        workspace=str(tmp_path),
        model=model,
        model_preset=preset,
        context_window_tokens=context_window,
        sessions=_FakeSessionManager(session),
    )


# ---------------------------------------------------------------------------
# build_footer_text (data layer)
# ---------------------------------------------------------------------------


def test_footer_text_minimal(tmp_path: Path) -> None:
    loop = _fake_loop(tmp_path)
    p = build_footer_text(loop, "cli", "direct")
    assert p["session_key"] == "cli:direct"
    assert p["display_name"] == ""
    assert p["model"] == "claude-opus-4-7"
    assert p["preset"] == "default"
    assert p["msg_count"] == 0
    assert p["token_estimate"] == 0
    assert p["context_window"] == 200_000
    assert p["context_pct"] == 0
    assert p["mem_count"] == 0
    assert p["vec_index"] is False


def test_footer_text_with_messages(tmp_path: Path) -> None:
    session = _FakeSession(messages=[
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "hi there"},
    ])
    loop = _fake_loop(tmp_path, session=session)
    p = build_footer_text(loop, "cli", "direct")
    assert p["msg_count"] == 2
    # Real tiktoken count: both have short content; expect <50 tokens total.
    assert 0 < p["token_estimate"] < 50
    assert p["context_pct"] == 0  # tiny / 200_000 → 0%


def test_footer_text_with_display_name(tmp_path: Path) -> None:
    session = _FakeSession(display_name="my-project")
    loop = _fake_loop(tmp_path, session=session)
    p = build_footer_text(loop, "cli", "direct")
    assert p["display_name"] == "my-project"


def test_footer_text_counts_memory_entries(tmp_path: Path) -> None:
    mem = tmp_path / "memory" / "stable"
    mem.mkdir(parents=True)
    (mem / "a.md").write_text("x", encoding="utf-8")
    (mem / "b.md").write_text("x", encoding="utf-8")
    (tmp_path / "memory" / "episodic").mkdir(parents=True)
    (tmp_path / "memory" / "episodic" / "c.md").write_text("x", encoding="utf-8")

    loop = _fake_loop(tmp_path)
    p = build_footer_text(loop, "cli", "direct")
    assert p["mem_count"] == 3


def test_footer_text_detects_vector_index(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True)
    (mem / ".index.lance").mkdir()
    loop = _fake_loop(tmp_path)
    p = build_footer_text(loop, "cli", "direct")
    assert p["vec_index"] is True


def test_footer_text_skips_dotted_paths_in_memory(tmp_path: Path) -> None:
    """Files inside .index.lance/ or similar shouldn't bump the count."""
    mem = tmp_path / "memory" / "stable"
    mem.mkdir(parents=True)
    (mem / "real.md").write_text("x", encoding="utf-8")
    hidden = tmp_path / "memory" / ".index.lance"
    hidden.mkdir(parents=True)
    (hidden / "internal.md").write_text("x", encoding="utf-8")  # bogus but possible

    loop = _fake_loop(tmp_path)
    p = build_footer_text(loop, "cli", "direct")
    assert p["mem_count"] == 1


def test_footer_text_resilient_to_broken_session(tmp_path: Path) -> None:
    """get_or_create raising must not break the footer payload."""

    class _BadSessions:
        def get_or_create(self, _key):
            raise RuntimeError("boom")

    loop = SimpleNamespace(
        workspace=str(tmp_path),
        model="m",
        model_preset="p",
        context_window_tokens=0,
        sessions=_BadSessions(),
    )
    p = build_footer_text(loop, "cli", "direct")
    assert p["msg_count"] == 0
    assert p["token_estimate"] == 0


# ---------------------------------------------------------------------------
# build_footer_html (render layer)
# ---------------------------------------------------------------------------


def test_footer_html_contains_session_and_model(tmp_path: Path) -> None:
    loop = _fake_loop(tmp_path)
    payload = build_footer_text(loop, "cli", "direct")
    html = build_footer_html(payload)
    # HTML object stores its raw text on .value
    text = html.value
    assert "cli:direct" in text
    assert "claude-opus-4-7" in text
    assert "vec✗" in text
    assert "mem:0" in text


def test_footer_html_shows_display_name_when_present(tmp_path: Path) -> None:
    session = _FakeSession(display_name="my-project")
    loop = _fake_loop(tmp_path, session=session)
    payload = build_footer_text(loop, "cli", "direct")
    html = build_footer_html(payload)
    assert "my-project" in html.value


def test_footer_html_no_context_window(tmp_path: Path) -> None:
    """When context window is 0, footer shows raw token count without %."""
    loop = _fake_loop(tmp_path, context_window=0)
    payload = build_footer_text(loop, "cli", "direct")
    html = build_footer_html(payload)
    assert "0 tokens" in html.value
    assert "%" not in html.value


def test_footer_omits_cache_and_composition_on_boot(tmp_path: Path) -> None:
    """Before the first turn, the footer must NOT show cache / conv / infra
    snippets (they require post-turn data)."""
    loop = _fake_loop(tmp_path)
    payload = build_footer_text(loop, "cli", "direct")
    assert payload["cache_pct"] is None
    assert payload["conv_pct"] is None
    assert payload["infra_pct"] is None
    html = build_footer_html(payload)
    assert "cache:" not in html.value
    assert "conv:" not in html.value
    assert "infra:" not in html.value


def test_footer_shows_cache_pct_after_first_call(tmp_path: Path) -> None:
    """Once cache.usage has fired, the footer carries cache:X%%."""
    loop = _fake_loop(tmp_path)
    loop._last_cache_usage = {
        "prompt_tokens": 4000, "cached_tokens": 2500, "cache_ratio_pct": 62.0,
    }
    payload = build_footer_text(loop, "cli", "direct")
    assert payload["cache_pct"] == 62
    html = build_footer_html(payload)
    assert "cache:62%" in html.value


def test_footer_shows_composition_after_first_build(tmp_path: Path) -> None:
    """Once context.composition has fired (cached on context.last_composition),
    the footer shows conv:X%% · infra:Y%%."""
    loop = _fake_loop(tmp_path)
    # Fake ContextBuilder with a last_composition payload.
    loop.context = SimpleNamespace(last_composition={
        "stable_breakdown": {"identity": 500, "bootstrap": 900, "skills_catalog": 600},
        "context_tokens": 0,
        "volatile_tokens": 0,
        "volatile_breakdown": {},
        "history_msg_tokens": 400,
        "current_msg_tokens": 100,
        "tools_tokens": 1200,
        "estimated_total": 3700,
    })
    payload = build_footer_text(loop, "cli", "direct")
    # Conversation = history + current = 500. Infra = stable + tools = 500+900+600+1200 = 3200.
    # 500/3700 = 13%, 3200/3700 = 86%.
    assert payload["conv_pct"] == 13
    assert payload["infra_pct"] == 86
    html = build_footer_html(payload)
    assert "conv:13%" in html.value
    assert "infra:86%" in html.value
