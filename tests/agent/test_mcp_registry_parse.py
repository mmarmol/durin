"""Task 3 — server.json parsing."""
import json
import pathlib

from durin.agent.mcp_registry import _hit_from_server, parse_server_json

_SAMPLE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures/server_json_sample.json").read_text()
)


def test_parse_detail():
    d = parse_server_json(_SAMPLE)
    assert d.name == "io.github.acme/jira"
    assert d.version == "1.4.0"
    assert d.repository == "https://github.com/acme/jira-mcp"
    pkg = d.packages[0]
    assert pkg.runtime_hint == "npx"
    assert pkg.transport_type == "stdio"
    assert pkg.package_arguments == ["--stdio"]
    secret = [e for e in pkg.env if e.name == "JIRA_TOKEN"][0]
    assert secret.is_secret is True
    assert secret.is_required is True
    assert d.remotes[0].transport_type == "streamable-http"
    assert d.remotes[0].url == "https://mcp.acme.com/jira"


def test_hit_kind_is_both_when_packages_and_remotes():
    h = _hit_from_server(_SAMPLE, registry="official")
    assert h.kind == "both"
    assert h.ref == "io.github.acme/jira"


def test_hit_kind_local_and_remote():
    assert _hit_from_server({"name": "a", "packages": [{}]}, registry="official").kind == "local"
    assert _hit_from_server({"name": "b", "remotes": [{}]}, registry="official").kind == "remote"
