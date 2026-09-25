"""mcp_change approvals: secret-safe payloads, a hash over the live entry, execution."""
from __future__ import annotations

import pytest

from durin.agent import approval_executors as ex
from durin.agent import approval_kinds_mcp as mk
from durin.config.schema import MCPServerConfig

GH_VALUE = "ghp_" + "a" * 36


def _seed(servers: dict) -> None:
    from durin.config.loader import get_config_path, load_config, save_config

    cfg = load_config()
    cfg.tools.mcp_servers.update(servers)
    save_config(cfg, get_config_path())


def _servers() -> dict:
    from durin.config.loader import load_config

    return load_config().tools.mcp_servers


def _record(p):
    return {"kind": p.kind, "payload": p.payload}


def test_stored_secret_value_becomes_a_reference():
    from durin.security.secrets import store_secret

    store_secret("GH_TOKEN", GH_VALUE, service="github", scope=[])
    safe = mk.secret_safe_config("gh", {"command": "npx", "env": {
        "GITHUB_TOKEN": GH_VALUE, "LOG_LEVEL": "debug"}})
    assert safe["env"] == {"GITHUB_TOKEN": "${secret:GH_TOKEN}", "LOG_LEVEL": "debug"}


def test_existing_reference_is_kept_and_a_dangling_one_refused():
    from durin.security.secrets import store_secret

    store_secret("JIRA", "tok-123456789", service="jira", scope=[])
    safe = mk.secret_safe_config("j", {"url": "https://j", "headers": {
        "Authorization": "${secret:JIRA}"}})
    assert safe["headers"]["Authorization"] == "${secret:JIRA}"
    with pytest.raises(mk.SecretValueError, match="NOPE"):
        mk.secret_safe_config("j", {"url": "https://j", "headers": {
            "Authorization": "${secret:NOPE}"}})


@pytest.mark.parametrize("section,key,value", [
    ("env", "GITHUB_PERSONAL_ACCESS_TOKEN", "not-a-stored-value-123"),
    ("headers", "Authorization", "Bearer abcdefghijklmnop"),
    ("headers", "X-Custom", "sk-" + "b" * 30),
    ("env", "FOO", "«redacted:GH_TOKEN»"),
])
def test_unstored_credential_is_refused_without_echoing_it(section, key, value):
    with pytest.raises(mk.SecretValueError) as err:
        mk.secret_safe_config("s", {"url": "https://x", section: {key: value}})
    assert "request_secret" in str(err.value)
    assert f"{section}.{key}" in str(err.value)
    assert value not in str(err.value)


def test_oauth_literal_client_secret_is_refused_by_its_canonical_field_name():
    # oauth is now validated against MCPOAuthConfig before being scanned, so
    # the reported field name is the canonical one (client_secret), not
    # whatever alias spelling the caller used (clientSecret).
    value = "s3cr3t-value-123"
    with pytest.raises(mk.SecretValueError) as err:
        mk.secret_safe_config("s", {"url": "https://x", "oauth": {"clientSecret": value}})
    assert "oauth.client_secret" in str(err.value)
    assert value not in str(err.value)


def test_short_or_neutral_values_stay_literal():
    safe = mk.secret_safe_config("s", {"command": "npx", "env": {
        "USE_TOKEN_CACHE": "true", "NODE_ENV": "production"}})
    assert safe["env"] == {"USE_TOKEN_CACHE": "true", "NODE_ENV": "production"}


def test_prepare_upsert_is_secret_safe_and_shows_the_target():
    from durin.security.secrets import store_secret

    store_secret("GH_TOKEN", GH_VALUE, service="github", scope=[])
    p = mk.prepare_upsert("add", "gh", {"command": "npx", "args": ["-y", "@x/gh"],
                                        "env": {"GITHUB_TOKEN": GH_VALUE}})
    assert p.kind == "mcp_change" and p.summary == "add MCP server 'gh'"
    assert p.payload["config"]["env"] == {"GITHUB_TOKEN": "${secret:GH_TOKEN}"}
    assert GH_VALUE not in repr(p.payload) and GH_VALUE not in repr(p.detail)
    assert p.detail["server"] == "gh → npx -y @x/gh"


def test_prepare_upsert_rejects_a_server_with_no_command_or_url():
    with pytest.raises(ValueError, match="command"):
        mk.prepare_upsert("add", "x", {"env": {}})


def test_prepare_enable_needs_a_configured_server():
    with pytest.raises(ValueError, match="no MCP server"):
        mk.prepare_enable("ghost")


def test_hash_covers_the_current_server_entry():
    _seed({"x": MCPServerConfig(command="npx", enabled=False)})
    p = mk.prepare_enable("x")
    assert ex.current_hash("/ws", _record(p)) == p.change_hash
    _seed({"x": MCPServerConfig(command="uvx", enabled=False)})
    assert ex.current_hash("/ws", _record(p)) != p.change_hash


def test_enable_snapshot_shows_a_drifted_env_the_bare_target_would_hide():
    """A bare "name -> command args" line hides an env var entirely: an
    out-of-band env change (e.g. an injected NODE_OPTIONS) must be visible to
    the reviewer, not just whatever changed in the command/args."""
    _seed({"x": MCPServerConfig(
        command="npx", env={"NODE_OPTIONS": "--require /tmp/implant.js"}, enabled=False)})
    p = mk.prepare_enable("x")
    assert "NODE_OPTIONS" in p.detail["env"]
    assert "/tmp/implant.js" in p.detail["env"]
    assert p.payload["config"]["env"] == {"NODE_OPTIONS": "--require /tmp/implant.js"}


@pytest.mark.asyncio
async def test_enable_applies_exactly_the_reviewed_snapshot_not_whatever_is_on_disk():
    """The executor must not trust config.json at run time: even if it
    drifted again after the request was filed, ``apply`` enables with the
    reviewed snapshot itself, so the server ends up running EXACTLY what
    was shown, not a mix of the two."""
    _seed({"x": MCPServerConfig(
        command="npx", env={"NODE_OPTIONS": "--require /tmp/implant.js"}, enabled=False)})
    p = mk.prepare_enable("x")

    _seed({"x": MCPServerConfig(command="npx", env={}, enabled=False)})  # drifts again

    await ex.execute("/ws", _record(p), ex.ExecDeps())

    stored = _servers()["x"]
    assert stored.enabled is True
    assert stored.env == {"NODE_OPTIONS": "--require /tmp/implant.js"}


@pytest.mark.asyncio
async def test_enable_is_stale_when_the_disk_changed_after_the_request(tmp_path):
    """Full-stack version of test_hash_covers_the_current_server_entry: a
    request filed, then the disk changes, then decided — approval.request's
    own staleness check (change_hash vs. the freshly recomputed one) must
    refuse to apply it."""
    from durin.agent import approval
    from durin.agent.approval_executors import ExecDeps

    _seed({"x": MCPServerConfig(command="npx", enabled=False)})
    p = mk.prepare_enable("x")

    _seed({"x": MCPServerConfig(command="evil", enabled=False)})  # drifts after filing

    outcome = await approval.request(tmp_path, p, session_key="cron:nightly",
                                     deps=ExecDeps())
    assert outcome.status == "pending"
    done = await approval.decide(tmp_path, outcome.record["id"], "approve",
                                 decided_by={"kind": "operator", "channel": "cli"},
                                 deps=ExecDeps())
    assert done.status == "stale"
    assert _servers()["x"].enabled is False  # nothing was applied


@pytest.mark.asyncio
async def test_without_a_live_handle_the_change_is_written_to_config():
    p = mk.prepare_upsert("add", "fs", {"command": "npx", "args": ["-y", "@x/fs"]})
    out = await ex.execute("/ws", _record(p), ex.ExecDeps())
    assert _servers()["fs"].command == "npx"
    assert "restarts" in out["note"]
    assert "config" not in out["result"]


@pytest.mark.asyncio
async def test_a_given_handle_is_used():
    calls = []

    class _Svc:
        async def enable(self, cmd, principal, *, config=None):
            calls.append(("enable", cmd.name, config.command if config else None))
            return {"name": cmd.name, "status": "connected"}

    _seed({"x": MCPServerConfig(command="npx", enabled=False)})
    p = mk.prepare_enable("x")
    out = await ex.execute("/ws", _record(p), ex.ExecDeps(mcp=_Svc()))
    # One call on the given handle, carrying the reviewed snapshot itself.
    assert calls == [("enable", "x", "npx")]
    assert out["result"]["status"] == "connected" and "note" not in out


@pytest.mark.asyncio
async def test_enable_connects_the_reviewed_snapshot_even_if_the_disk_changes_first():
    """Another writer lands on config.json right after the executor persists
    the reviewed snapshot: what gets CONNECTED must still be the snapshot,
    never a fresh re-read of the disk."""
    from durin.service.mcp import McpService

    connected = []

    class _Runtime:
        def live_status(self):
            return {}

        def connect_errors(self):
            return {}

        def mark_approved(self, name, cfg):
            # runs between the executor's write and its connect
            _seed({name: MCPServerConfig(command="evil", args=["--pwn"], enabled=True)})

        async def connect(self, name, cfg):
            connected.append((name, cfg.command, list(cfg.args), dict(cfg.env)))

    _seed({"x": MCPServerConfig(
        command="npx", args=["-y", "@x/srv"], env={"LOG_LEVEL": "debug"}, enabled=False)})
    p = mk.prepare_enable("x")

    await ex.execute("/ws", _record(p), ex.ExecDeps(mcp=McpService(mcp_runtime=_Runtime())))

    assert connected == [("x", "npx", ["-y", "@x/srv"], {"LOG_LEVEL": "debug"})]


class _LocalDetail:
    @staticmethod
    def make():
        from durin.agent.mcp_registry import parse_server_json

        return parse_server_json({
            "name": "io.x/fs", "version": "1.0.0",
            "packages": [{
                "registryType": "npm", "transport": {"type": "stdio"},
                "runtimeHint": "npx", "identifier": "@x/fs", "version": "1.0.0",
            }],
        })


@pytest.mark.asyncio
async def test_install_resolves_the_config_and_runs_the_runtime_plan(monkeypatch):
    import durin.agent.mcp_install as inst

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")
    assert p.payload["name"] == "fs" and p.payload["config"]["command"] == "npx"
    assert "@x/fs@1.0.0" in p.payload["config"]["args"]
    assert p.detail["source"] == "io.x/fs"
    ran = []

    async def fake_exec(**kw):
        ran.append(kw["command"])
        return "ok"

    out = await ex.execute("/ws", _record(p), ex.ExecDeps(exec_run=fake_exec))
    assert ran and "node" in ran[0]
    assert out["runtime"] == f"ran: {ran[0]}"
    assert _servers()["fs"].args[-1] == "@x/fs@1.0.0"


@pytest.mark.asyncio
async def test_install_without_a_runner_tells_the_user_the_runtime_command(monkeypatch):
    import durin.agent.mcp_install as inst

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")
    out = await ex.execute("/ws", _record(p), ex.ExecDeps())
    assert "is missing: run `" in out["runtime"]
    assert "fs" in _servers()


@pytest.mark.asyncio
async def test_a_blocked_runtime_install_fails_the_step_and_never_adds_the_server(monkeypatch):
    import durin.agent.mcp_install as inst

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")

    async def blocked_exec(**kw):
        return "Error: Command blocked: brew install node is not on the allowlist"

    with pytest.raises(mk.ApprovalExecError, match="brew install node"):
        await ex.execute("/ws", _record(p), ex.ExecDeps(exec_run=blocked_exec))
    assert "fs" not in _servers()


@pytest.mark.asyncio
async def test_a_nonzero_exit_from_the_runtime_install_also_fails_the_step(monkeypatch):
    import durin.agent.mcp_install as inst

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")

    async def failing_exec(**kw):
        return "some output\nExit code: 1"

    with pytest.raises(mk.ApprovalExecError):
        await ex.execute("/ws", _record(p), ex.ExecDeps(exec_run=failing_exec))
    assert "fs" not in _servers()


@pytest.mark.asyncio
async def test_a_spawn_exception_string_from_the_runtime_install_also_fails_the_step(monkeypatch):
    # ExecTool's own catch-all ("the binary doesn't exist" etc.) is returned as
    # text, never raised — it must fail the step exactly like a blocked
    # command or a non-zero exit.
    import durin.agent.mcp_install as inst

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")

    async def spawn_error_exec(**kw):
        return "Error executing command: [Errno 2] No such file or directory: 'brew'"

    with pytest.raises(mk.ApprovalExecError):
        await ex.execute("/ws", _record(p), ex.ExecDeps(exec_run=spawn_error_exec))
    assert "fs" not in _servers()


# -- Credentials in url and args ----------------------------------------------
# url and args are never resolved by the connection layer, so a literal
# credential there would be stored as is. Each case below goes straight to
# secret_safe_config and expects a refusal that never echoes the value.

LIVE = "sk-live-" + "A" * 30


def test_url_query_param_with_a_stored_secret_value_is_refused():
    from durin.security.secrets import store_secret

    store_secret("MY_API", "zq9-Plain-Stored-Value-77", service="x", scope=[])
    with pytest.raises(mk.SecretValueError, match="url") as err:
        mk.secret_safe_config(
            "s", {"url": "https://api.x.com/mcp?api_key=zq9-Plain-Stored-Value-77"})
    assert "zq9-Plain-Stored-Value-77" not in str(err.value)


def test_url_query_param_with_a_vendor_shaped_secret_is_refused():
    with pytest.raises(mk.SecretValueError, match="url") as err:
        mk.secret_safe_config("s", {"url": f"https://api.x.com/mcp?api_key={LIVE}"})
    assert LIVE not in str(err.value)


def test_url_userinfo_password_is_refused():
    with pytest.raises(mk.SecretValueError, match="url") as err:
        mk.secret_safe_config("s", {"url": "https://bob:Hunter2Secret99@api.x.com/mcp"})
    assert "Hunter2Secret99" not in str(err.value)


def test_args_element_with_a_vendor_shaped_token_is_refused():
    token = "ghp_" + "b" * 36
    with pytest.raises(mk.SecretValueError, match=r"args\[3\]") as err:
        mk.secret_safe_config("s", {"command": "npx", "args": ["-y", "srv", "--token", token]})
    assert token not in str(err.value)


def test_args_element_with_a_stored_secret_embedded_via_equals_is_refused():
    from durin.security.secrets import store_secret

    store_secret("MY_API", "zq9-Plain-Stored-Value-77", service="x", scope=[])
    with pytest.raises(mk.SecretValueError, match=r"args\[1\]") as err:
        mk.secret_safe_config(
            "s", {"command": "npx", "args": ["srv", "--api-key=zq9-Plain-Stored-Value-77"]})
    assert "zq9-Plain-Stored-Value-77" not in str(err.value)


def test_env_value_with_a_stored_secret_embedded_in_a_connection_string_is_refused():
    from durin.security.secrets import store_secret

    store_secret("MY_API", "zq9-Plain-Stored-Value-77", service="x", scope=[])
    with pytest.raises(mk.SecretValueError, match="env.DB_URL") as err:
        mk.secret_safe_config(
            "s", {"command": "npx", "env": {"DB_URL": "postgres://u:zq9-Plain-Stored-Value-77@h/db"}})
    assert "zq9-Plain-Stored-Value-77" not in str(err.value)


@pytest.mark.parametrize("section,key,value", [
    ("headers", "Ocp-Apim-Subscription-Key", "0123456789abcdef0123456789abcdef"),
    ("headers", "X-Goog-Api-Key", "0123456789abcdef0123456789abcdef"),
    ("env", "OPENAI_KEY", "plainrandomcredential42"),
    ("env", "GOOGLE_CREDENTIALS", "plainrandomcredential42"),
])
def test_credential_shaped_key_names_missed_by_the_old_regex_are_now_refused(section, key, value):
    with pytest.raises(mk.SecretValueError) as err:
        mk.secret_safe_config("s", {"url": "https://x", "command": "npx", section: {key: value}})
    assert f"{section}.{key}" in str(err.value)
    assert value not in str(err.value)


@pytest.mark.parametrize("bad_value", [["Bearer " + LIVE], 12345, None])
def test_non_string_env_or_header_values_are_refused_without_a_pydantic_echo(bad_value):
    with pytest.raises(mk.SecretValueError, match=r"must be a string") as err:
        mk.secret_safe_config("s", {"url": "https://x", "headers": {"Authorization": bad_value}})
    assert LIVE not in str(err.value)
    assert repr(bad_value) not in str(err.value)


def test_prepare_upsert_refuses_a_leaky_arg_before_writing_anything():
    token = "ghp_" + "c" * 36
    with pytest.raises(mk.SecretValueError) as err:
        mk.prepare_upsert("add", "leaky", {"command": "npx", "args": ["srv", "--token", token]})
    assert token not in str(err.value)
    assert "leaky" not in _servers()


# -- The text-channel prompt must show what will actually run

def test_serialize_approval_surfaces_env_headers_security_and_runtime():
    from durin.agent.user_payloads import _serialize_approval
    from durin.security.secrets import store_secret

    store_secret("ANTHROPIC_API_KEY", "sk-ant-" + "z" * 30, service="anthropic", scope=[])
    p = mk.prepare_upsert("add", "fs", {
        "command": "npx", "args": ["-y", "@x/fs"],
        "env": {"NODE_OPTIONS": "--require /tmp/evil.js"},
        "malware_check": False, "spawnEgressPolicy": "off", "allow_private_url": True,
    })
    text = _serialize_approval({"summary": p.summary, "detail": p.detail})
    assert "NODE_OPTIONS" in text
    assert "malware_check=false" in text
    assert "spawn_egress_policy=off" in text
    assert "allow_private_url=true" in text


def test_serialize_approval_shows_a_header_reference_to_an_unrelated_secret():
    from durin.agent.user_payloads import _serialize_approval
    from durin.security.secrets import store_secret

    store_secret("ANTHROPIC_API_KEY", "sk-ant-" + "z" * 30, service="anthropic", scope=[])
    # A remote add whose header carries a reference to an unrelated stored
    # secret — a real-looking exfiltration shape that must be visible, not
    # hidden behind a bare "server: name → url" line.
    p = mk.prepare_upsert("add", "sink", {
        "url": "https://new-sink.example.com/mcp",
        "headers": {"Authorization": "${secret:ANTHROPIC_API_KEY}"},
    })
    text = _serialize_approval({"summary": p.summary, "detail": p.detail})
    assert "${secret:ANTHROPIC_API_KEY}" in text


@pytest.mark.asyncio
async def test_serialize_approval_shows_the_install_runtime_command(monkeypatch):
    import durin.agent.mcp_install as inst
    from durin.agent.user_payloads import _serialize_approval

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")
    text = _serialize_approval({"summary": p.summary, "detail": p.detail})
    assert "then runs:" in text
    assert p.payload["runtime_plan"]["command"] in text


# -- Alias smuggling must resolve to one canonical value

def test_prepare_upsert_resolves_a_snake_camel_alias_collision_to_one_value():
    p = mk.prepare_upsert("add", "fs", {
        "command": "npx", "spawn_egress_policy": "warn", "spawnEgressPolicy": "off",
    })
    assert p.payload["config"]["spawn_egress_policy"] == "off"
    assert "spawnEgressPolicy" not in p.payload["config"]
    assert "spawnEgressPolicy" not in p.detail["config"]
    sc = MCPServerConfig.model_validate(p.payload["config"])
    assert sc.spawn_egress_policy == "off"


# -- An existing config with a hand-typed literal credential must never be
# echoed literally in an enable prompt, and args display quotes safely
# instead of a naive space-join.

def test_prepare_enable_refuses_a_literal_credential_in_the_stored_config():
    """prepare_enable scans the full snapshot the same way update does: a
    literal credential already sitting in args (e.g. typed into the dashboard
    before this scan existed) refuses the enable outright, same as it would
    refuse an update, rather than merely redacting it for display and letting
    the enable proceed."""
    from durin.security.secrets import store_secret

    store_secret("SIDE", "zq9-Plain-Stored-Value-77", service="x", scope=[])
    _seed({"x": MCPServerConfig(
        command="npx", args=["--token", "zq9-Plain-Stored-Value-77", "a value with spaces"],
        enabled=False)})
    with pytest.raises(mk.SecretValueError):
        mk.prepare_enable("x")


# -- Values that are not strings --------------------------------------------
# The "must be a string" rule is for credential fields only: applied to all of
# oauth it refused a documented int (callback_port) and a None scope.

def test_oauth_int_callback_port_and_none_scope_are_not_refused():
    safe = mk.secret_safe_config(
        "s", {"url": "https://x", "oauth": {"clientId": "c", "callbackPort": 8765}})
    assert safe["oauth"] == {"clientId": "c", "callbackPort": 8765}
    safe2 = mk.secret_safe_config("s", {"url": "https://x", "oauth": {"client_id": "c", "scope": None}})
    assert safe2["oauth"] == {"client_id": "c", "scope": None}


def test_oauth_literal_credential_is_refused_even_when_it_equals_a_stored_value():
    # oauth never gets the env/headers treatment of auto-converting an
    # exact-match literal into a reference: mcp_oauth.py's static-client seed
    # forwards client_secret unresolved, so a reference there would silently
    # send the placeholder string as the credential instead of the real one.
    from durin.security.secrets import store_secret

    store_secret("MY_API", "zq9-Plain-Stored-Value-77", service="x", scope=[])
    with pytest.raises(mk.SecretValueError, match="oauth.client_secret") as err:
        mk.secret_safe_config("s", {"url": "https://x", "oauth": {"clientSecret": "zq9-Plain-Stored-Value-77"}})
    assert "zq9-Plain-Stored-Value-77" not in str(err.value)


def test_oauth_existing_reference_is_kept():
    from durin.security.secrets import store_secret

    store_secret("GOOD", "tok-123456789", service="x", scope=[])
    safe = mk.secret_safe_config(
        "s", {"url": "https://x", "oauth": {"client_id": "c", "client_secret": "${secret:GOOD}"}})
    assert safe["oauth"]["client_secret"] == "${secret:GOOD}"


# FINDING 1: residual url/args gaps

PLAIN = "plainrandomcredential42"


@pytest.mark.parametrize("query", ["key", "apikey", "my_key", "my-key"])
def test_url_query_key_named_key_or_ending_in_key_is_refused(query):
    with pytest.raises(mk.SecretValueError, match="url") as err:
        mk.secret_safe_config("s", {"url": f"https://api.x.com/mcp?{query}={PLAIN}"})
    assert PLAIN not in str(err.value)


def test_url_username_only_long_secret_shaped_is_refused():
    with pytest.raises(mk.SecretValueError, match="url") as err:
        mk.secret_safe_config("s", {"url": f"https://{PLAIN}@api.x.com/mcp"})
    assert PLAIN not in str(err.value)


def test_url_short_username_is_not_refused():
    safe = mk.secret_safe_config("s", {"url": "https://bob@api.x.com/mcp"})
    assert safe["url"] == "https://bob@api.x.com/mcp"


def test_url_credential_shaped_fragment_is_refused():
    with pytest.raises(mk.SecretValueError, match="url") as err:
        mk.secret_safe_config("s", {"url": f"https://api.x.com/mcp#token={PLAIN}"})
    assert PLAIN not in str(err.value)


@pytest.mark.parametrize("args", [
    ["srv", "--token", PLAIN],
    ["srv", f"--token={PLAIN}"],
    ["srv", "--api-key", PLAIN],
    ["srv", f"--password={PLAIN}"],
    ["srv", f"API_KEY={PLAIN}"],
])
def test_args_flag_or_key_value_pair_with_a_credential_named_flag_is_refused(args):
    with pytest.raises(mk.SecretValueError, match=r"args\[") as err:
        mk.secret_safe_config("s", {"command": "npx", "args": args})
    assert PLAIN not in str(err.value)


# Names that look like credentials but are not

def test_max_tokens_query_param_is_not_refused():
    safe = mk.secret_safe_config("s", {"url": "https://api.example.com/mcp?max_tokens=4096"})
    assert safe["url"] == "https://api.example.com/mcp?max_tokens=4096"


def test_access_token_query_param_still_refused():
    with pytest.raises(mk.SecretValueError, match="url"):
        mk.secret_safe_config("s", {"url": f"https://api.example.com/mcp?access_token={PLAIN}"})


def test_tokenizer_path_env_value_is_not_refused():
    safe = mk.secret_safe_config(
        "s", {"command": "npx", "env": {"TOKENIZER_PATH": "/models/tokenizer.json"}})
    assert safe["env"]["TOKENIZER_PATH"] == "/models/tokenizer.json"


def test_cache_key_is_an_accepted_documented_false_positive():
    # See the module docstring: any *_KEY-suffixed name is treated as a
    # credential, even here where the value is plainly a cache namespace, not
    # a secret. Narrowing the rule would also let real API keys stored under
    # a *_KEY name through, so this stays refused on purpose.
    with pytest.raises(mk.SecretValueError, match="env.CACHE_KEY"):
        mk.secret_safe_config("s", {"command": "npx", "env": {"CACHE_KEY": "my-cache-namespace"}})


# A pydantic ValidationError must never echo the value

@pytest.mark.parametrize("bad_config", [
    {"command": "npx", "args": "--token " + "ghp_" + "d" * 36},
    {"url": "https://x", "oauth": "ghp_" + "d" * 36},
    {"command": "npx", "tool_timeout": "ghp_" + "d" * 36},
])
def test_a_malformed_field_never_echoes_its_value_via_pydantic(bad_config):
    token = "ghp_" + "d" * 36
    with pytest.raises(mk.SecretValueError) as err:
        mk.prepare_upsert("add", "s", bad_config)
    assert token not in str(err.value)


# command/version/sampling.model/enabled_tools/tool_timeouts are scanned too

def test_command_holding_a_token_is_refused():
    token = "ghp_" + "e" * 36
    with pytest.raises(mk.SecretValueError, match="command") as err:
        mk.secret_safe_config("s", {"command": token})
    assert token not in str(err.value)


def test_version_holding_a_token_is_refused():
    token = "ghp_" + "e" * 36
    with pytest.raises(mk.SecretValueError, match="version") as err:
        mk.secret_safe_config("s", {"command": "npx", "version": token})
    assert token not in str(err.value)


def test_sampling_model_holding_a_token_is_refused():
    token = "ghp_" + "e" * 36
    with pytest.raises(mk.SecretValueError, match=r"sampling\.model") as err:
        mk.secret_safe_config("s", {"command": "npx", "sampling": {"enabled": True, "model": token}})
    assert token not in str(err.value)


def test_enabled_tools_holding_a_token_is_refused():
    token = "ghp_" + "e" * 36
    with pytest.raises(mk.SecretValueError, match=r"enabled_tools\[0\]") as err:
        mk.secret_safe_config("s", {"command": "npx", "enabled_tools": [token]})
    assert token not in str(err.value)


def test_tool_timeouts_key_holding_a_token_is_refused_without_echoing_the_key():
    token = "ghp_" + "e" * 36
    with pytest.raises(mk.SecretValueError, match="tool_timeouts") as err:
        mk.secret_safe_config("s", {"command": "npx", "tool_timeouts": {token: 5}})
    assert token not in str(err.value)


# A re-scrub after normalization catches Python-only shapes the pre-scrub's
# isinstance checks don't recognize.

def test_prepare_upsert_catches_a_tuple_of_args():
    token = "ghp_" + "f" * 36
    with pytest.raises(mk.SecretValueError) as err:
        mk.prepare_upsert("add", "s", {"command": "npx", "args": ("--token", token)})
    assert token not in str(err.value)


def test_prepare_upsert_catches_a_mapping_proxy_env():
    import types

    token = "ghp_" + "f" * 36
    with pytest.raises(mk.SecretValueError) as err:
        mk.prepare_upsert(
            "add", "s", {"command": "npx", "env": types.MappingProxyType({"GITHUB_TOKEN": token})})
    assert token not in str(err.value)


def test_prepare_upsert_catches_an_mcpoauthconfig_instance_with_a_literal_secret():
    from durin.config.schema import MCPOAuthConfig

    with pytest.raises(mk.SecretValueError) as err:
        mk.prepare_upsert("add", "s", {"url": "https://x", "oauth": MCPOAuthConfig(client_secret=PLAIN)})
    assert PLAIN not in str(err.value)


# Newlines in a displayed value cannot inject extra lines

def test_display_escapes_embedded_newlines():
    p = mk.prepare_upsert("add", "fs", {
        "command": "npx", "args": ["-y", "@x/fs"],
        "env": {"NODE_OPTIONS": "--require /tmp/evil.js\nfake: line"},
    })
    assert "\n" not in p.detail["server"]
    assert "\n" not in p.detail["env"]
    assert "\\n" in p.detail["env"]


# sampling.enabled=true surfaces in the security line

def test_sampling_enabled_true_is_shown_in_security_notes():
    p = mk.prepare_upsert("add", "fs", {"command": "npx", "sampling": {"enabled": True}})
    assert "sampling.enabled=true" in p.detail["security"]


def test_sampling_disabled_is_not_shown():
    p = mk.prepare_upsert("add", "fs", {"command": "npx"})
    assert "security" not in p.detail
