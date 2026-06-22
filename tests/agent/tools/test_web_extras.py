import asyncio
import types

import durin.agent.tools.web as web


def test_ddgs_missing_triggers_ensure_then_retry(monkeypatch):
    """A missing `web` extra (ImportError) auto-installs via ensure_extra, then
    the search retries and succeeds — no 'install durin[web]' dead-end."""
    calls = {"ensure": 0, "search": 0}

    def fake_ensure(feature, *, config):
        calls["ensure"] += 1
        assert feature == "web_search"
        return types.SimpleNamespace(status="installed", needs_restart=False, message="")

    monkeypatch.setattr(web, "ensure_extra", fake_ensure)

    # First call raises ImportError (extra missing); second (post-install) works.
    seq = iter([ImportError("No module named 'ddgs'"), None])

    def fake_ddgs_text(query, n):
        err = next(seq)
        if err:
            raise err
        calls["search"] += 1
        return [{"title": "Hit", "href": "http://x", "body": "snippet"}]

    monkeypatch.setattr(web, "_ddgs_text", fake_ddgs_text)

    tool = web.WebSearchTool.__new__(web.WebSearchTool)
    tool.config = types.SimpleNamespace(timeout=5)
    tool._app_config = types.SimpleNamespace(
        install=types.SimpleNamespace(auto_install_extras=True)
    )

    out = asyncio.run(tool._search_duckduckgo("q", 1))
    assert calls["ensure"] == 1
    assert calls["search"] == 1
    assert "Hit" in out


def test_ddgs_missing_install_fails_returns_message(monkeypatch):
    """If the auto-install can't satisfy the extra, surface the message, not a retry."""
    def fake_ensure(feature, *, config):
        return types.SimpleNamespace(status="failed", needs_restart=False, message="boom")

    monkeypatch.setattr(web, "ensure_extra", fake_ensure)

    def fake_ddgs_text(query, n):
        raise ImportError("No module named 'ddgs'")

    monkeypatch.setattr(web, "_ddgs_text", fake_ddgs_text)

    tool = web.WebSearchTool.__new__(web.WebSearchTool)
    tool.config = types.SimpleNamespace(timeout=5)
    tool._app_config = None

    out = asyncio.run(tool._search_duckduckgo("q", 1))
    assert "boom" in out
    assert "unavailable" in out.lower()


def test_strip_tags_removes_script_with_whitespace_close():
    """Closing tags may carry whitespace (``</script >``, ``</script\n>``); the
    stripper must still drop the whole element, not leak its body."""
    assert web._strip_tags("a<script>evil()</script >b") == "ab"
    assert web._strip_tags("a<script>evil()</script\n>b") == "ab"
    assert web._strip_tags("a<script>evil()</script\t\n bar>b") == "ab"
    assert web._strip_tags("a<style>.x{}</style >b") == "ab"
    # plain close still works
    assert web._strip_tags("a<script>x</script>b") == "ab"
