"""Tests for web_fetch multi-URL fan-out (urls[])."""

from __future__ import annotations

import asyncio
import json

import pytest

from durin.agent.tools.web import MAX_FETCH_URLS, WebFetchTool


@pytest.mark.asyncio
async def test_urls_fan_out_returns_one_record_per_url_in_order(monkeypatch):
    tool = WebFetchTool()

    async def fake_fetch_one(url, extract_mode="markdown", max_chars=None):
        return f"content of {url}"

    monkeypatch.setattr(tool, "_fetch_one", fake_fetch_one)
    out = await tool.execute(urls=["https://a", "https://b"])
    assert out == {"results": [
        {"url": "https://a", "content": "content of https://a"},
        {"url": "https://b", "content": "content of https://b"},
    ]}


@pytest.mark.asyncio
async def test_urls_run_concurrently(monkeypatch):
    tool = WebFetchTool()
    active = {"now": 0, "max": 0}

    async def fake_fetch_one(url, extract_mode="markdown", max_chars=None):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.02)
        active["now"] -= 1
        return url

    monkeypatch.setattr(tool, "_fetch_one", fake_fetch_one)
    await tool.execute(urls=["https://a", "https://b", "https://c"])
    assert active["max"] >= 2  # genuinely overlapped


@pytest.mark.asyncio
async def test_one_url_failure_does_not_abort_batch(monkeypatch):
    tool = WebFetchTool()

    async def fake_fetch_one(url, extract_mode="markdown", max_chars=None):
        if "bad" in url:
            raise RuntimeError("boom")
        return f"ok {url}"

    monkeypatch.setattr(tool, "_fetch_one", fake_fetch_one)
    out = await tool.execute(urls=["https://good", "https://bad"])
    recs = out["results"]
    assert recs[0] == {"url": "https://good", "content": "ok https://good"}
    assert recs[1]["url"] == "https://bad"
    assert "fetch failed" in recs[1]["error"]


@pytest.mark.asyncio
async def test_url_and_urls_mutually_exclusive():
    tool = WebFetchTool()
    out = await tool.execute(url="https://a", urls=["https://b"])
    assert json.loads(out)["error"] == "pass either `url` (single) or `urls` (list), not both"


@pytest.mark.asyncio
async def test_urls_cap_enforced():
    tool = WebFetchTool()
    out = await tool.execute(urls=["https://x"] * (MAX_FETCH_URLS + 1))
    assert "too many urls" in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_neither_url_nor_urls_is_error():
    tool = WebFetchTool()
    out = await tool.execute()
    assert json.loads(out)["error"] == "url or urls is required"
