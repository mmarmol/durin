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


def test_parse_oci_docker_env_injection():
    """OCI servers (e.g. github) declare secrets as a `-e NAME={var}` runtime arg, not
    in environmentVariables. The parser must lift that into an env input so the install
    form prompts for it and the secret store collects it; the raw -e arg is consumed."""
    d = parse_server_json({
        "name": "io.github.github/github-mcp-server",
        "version": "1.4.0",
        "packages": [{
            "registryType": "oci",
            "identifier": "ghcr.io/github/github-mcp-server:1.4.0",
            "transport": {"type": "stdio"},
            "runtimeArguments": [{
                "type": "named", "name": "-e",
                "value": "GITHUB_PERSONAL_ACCESS_TOKEN={token}",
                "isRequired": True,
                "variables": {"token": {"isRequired": True, "isSecret": True}},
            }],
        }],
    })
    pkg = d.packages[0]
    assert pkg.registry_type == "oci"
    assert pkg.identifier == "ghcr.io/github/github-mcp-server:1.4.0"
    tok = [e for e in pkg.env if e.name == "GITHUB_PERSONAL_ACCESS_TOKEN"][0]
    assert tok.is_secret is True
    assert tok.is_required is True
    assert pkg.runtime_arguments == []  # the -e arg is consumed into env, not left raw


def test_parse_oci_passes_through_non_env_runtime_args():
    """Non `-e` docker args (volume mounts, flags) survive as rendered argv."""
    d = parse_server_json({
        "name": "io.x/dockerized", "version": "1.0.0",
        "packages": [{
            "registryType": "oci", "identifier": "x/y:1.0.0",
            "transport": {"type": "stdio"},
            "runtimeArguments": [
                {"type": "named", "name": "--network", "value": "host"},
                {"type": "positional", "value": "--verbose"},
            ],
        }],
    })
    assert d.packages[0].runtime_arguments == ["--network", "host", "--verbose"]
    assert d.packages[0].env == []
