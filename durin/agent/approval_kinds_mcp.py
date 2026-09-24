"""Approval kind ``mcp_change``: add, update, install or enable an MCP server.

Each of these puts a server's command or endpoint into the agent's tool
surface, so ``mcp_manage`` files it as an approval request instead of doing it
on the model's word. The request records the exact change: the full server
config for add, update and install (an install is resolved against the
registry when it is requested, so the person approves the config that will be
written, not a registry ref whose content could change underneath), or just
the server name for enable. Its hash covers the server's current config entry,
so a request whose server changed after it was filed is stale instead of
overwriting that change.

A request never holds a credential. ``secret_safe_config`` scans every field
that can carry one — ``env``, ``headers``, ``url``, ``args``, ``oauth``,
``command``, ``version``, ``sampling.model``, ``enabled_tools`` and
``tool_timeouts`` keys — against the store-backed redactor and a set of
credential-shaped key names. A value equal to a stored secret becomes that
secret's ``${secret:NAME}`` reference in ``env``/``headers`` — the only
sections the connection layer resolves a reference from
(``durin/agent/tools/mcp_connection.py``, ``_resolve_secret_map``). Everywhere
else (``url``, ``args``, ``oauth``, and the scalar/list fields above) a
credential is refused outright rather than turned into a reference: a
reference placed there would never resolve, either because the field itself
is never passed through ``_resolve_secret_map`` (``url``/``args``), or because
the OAuth flow (``durin/agent/tools/mcp_oauth.py``, ``_seed_static_client``)
forwards ``client_secret`` straight into the SDK's client-registration call
without resolving it. A literal ``oauth.client_secret`` is therefore always
refused, even when it equals a stored secret's value; the message points at
configuring it outside the agent.

Known accepted false positive: a key ending in ``_key``/``-key`` (e.g.
``CACHE_KEY``) is always treated as credential-shaped, even for a value like
a cache namespace that plainly isn't one. Narrowing the ``key``-suffix rule
to reduce this would also let real API keys stored under a `*_KEY` name
through, which is the worse failure mode — so it is left as-is.

Execution uses the ``McpService`` handed in as ``ExecDeps.mcp``. Without one
(an approval decided from the CLI, or any process without the gateway's live
MCP connections) it builds a config-only ``McpService``: the change is written
to config, and the running gateway applies it when it restarts or when the
server is reconnected.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from durin.agent.approval_executors import (
    ApprovalExecError,
    ExecDeps,
    Prepared,
    register,
)

KIND = "mcp_change"

# Header names that carry a credential whatever their value looks like.
_AUTH_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie",
                           "x-api-key", "api-key"})
# Whole key "words" (after splitting on '-'/'_'/camelCase and lowercasing)
# that name a credential. Whole-word, not substring: the shared
# CREDENTIAL_KEY_RE's bare "token"/"secret" alternatives match ANY substring,
# so "TOKENIZER_PATH" or "max_tokens" (plural) would wrongly trip it. This
# module needs the opposite trade-off from that shared write-back guard (fewer
# false positives on ordinary settings, at the cost of missing a credential
# whose name doesn't isolate to one of these components), so it keeps its own
# word list instead of reusing that regex.
_CREDENTIAL_WORDS = frozenset({
    "key", "apikey", "token", "secret", "password", "passwd", "passphrase",
    "credential", "credentials",
})
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SPLIT_RE = re.compile(r"[-_\s]+")
# Values shorter than this are never treated as credentials: they are too
# likely to be ordinary settings ("true", "30"). The secret redactor uses the
# same floor for the same reason.
_MIN_SECRET_LEN = 8
# A URL username this long, with both letters and digits, and no password, is
# itself the credential (e.g. "https://<token>@host") rather than an actual
# account name.
_MIN_SECRET_USERNAME_LEN = 20
# Non-default security settings worth calling out to the reviewer explicitly
# (beyond what the "server" line already shows), in the order they are shown.
_SECURITY_FIELDS = ("malware_check", "spawn_egress_policy", "allow_private_url")

_CONFIG_ONLY_NOTE = (
    "Saved to config. This process has no live MCP connections, so the running "
    "gateway applies the change when it restarts or when the server is "
    "reconnected from the MCP page."
)


class SecretValueError(ValueError):
    """A server config holds a credential that is not a stored-secret reference."""


def as_dict(result: Any) -> dict:
    """A service result as a plain dict."""
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    return {"status": getattr(result, "status", None), "ok": getattr(result, "ok", None)}


def _key_components(key: str) -> list[str]:
    spaced = _CAMEL_BOUNDARY_RE.sub("_", key)
    return [p.lower() for p in _KEY_SPLIT_RE.split(spaced) if p]


def _is_credential_key(key: str) -> bool:
    return any(part in _CREDENTIAL_WORDS for part in _key_components(key))


def _is_credential(key: str, value: str, redactor: Any) -> bool:
    if len(value) < _MIN_SECRET_LEN:
        return False
    if key.lower() in _AUTH_HEADERS or _is_credential_key(key):
        return True
    # A credential-shaped value under a neutral key: a vendor token prefix, a
    # bearer token, a JWT, a private-key block, or a substring equal to a
    # stored secret's value.
    return redactor.redact_text(value) != value


def _looks_like_credential(value: str, redactor: Any) -> bool:
    return bool(value) and redactor.redact_text(value) != value


def _looks_like_secret_username(username: str, redactor: Any) -> bool:
    if _looks_like_credential(username, redactor):
        return True
    return (len(username) >= _MIN_SECRET_USERNAME_LEN
            and any(c.isalpha() for c in username) and any(c.isdigit() for c in username))


def _url_is_unsafe(url: str, redactor: Any) -> bool:
    """True when *url* embeds a credential: a stored/pattern-shaped value
    anywhere in it, a ``user:pass@`` password, a bare token used as the
    username, a query parameter named like a credential, or a fragment that
    is itself credential-shaped or holds one under a credential-named key."""
    if _looks_like_credential(url, redactor):
        return True
    parts = urlsplit(url)
    if parts.password:
        return True
    if parts.username and not parts.password and _looks_like_secret_username(parts.username, redactor):
        return True
    if any(_is_credential_key(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return True
    if parts.fragment:
        if _looks_like_credential(parts.fragment, redactor):
            return True
        if any(_is_credential_key(key) for key, _ in parse_qsl(parts.fragment, keep_blank_values=True)):
            return True
    return False


def _flag_name(arg: str) -> str | None:
    """*arg* stripped of leading dashes ("--token" -> "token"), or ``None``
    when it isn't dash-prefixed at all."""
    stripped = arg.lstrip("-")
    return stripped if stripped and stripped != arg else None


def _arg_problems(args: list, redactor: Any, marker: str) -> list[str]:
    """Refuse an ``args`` entry that is itself credential-shaped, a
    ``--flag value`` / ``--flag=value`` / ``KEY=value`` pair whose name is
    credential-shaped (the connection layer spawns these as literal argv, so
    a stored value here can only ever be a literal, never a reference — see
    the module docstring), or not a string at all.
    """
    problems: list[str] = []
    i, n = 0, len(args)
    while i < n:
        item = args[i]
        if not isinstance(item, str):
            problems.append(f"args[{i}] (must be a string)")
            i += 1
            continue
        if not item:
            i += 1
            continue
        if marker in item:
            problems.append(f"args[{i}]")
            i += 1
            continue
        if "=" in item:
            key_part, _, value_part = item.partition("=")
            name = _flag_name(key_part) or key_part
            if _is_credential_key(name) and len(value_part) >= _MIN_SECRET_LEN:
                problems.append(f"args[{i}]")
                i += 1
                continue
        flag = _flag_name(item)
        if flag and _is_credential_key(flag) and i + 1 < n:
            nxt = args[i + 1]
            if isinstance(nxt, str) and len(nxt) >= _MIN_SECRET_LEN:
                problems.append(f"args[{i + 1}]")
                i += 2
                continue
        if _looks_like_credential(item, redactor):
            problems.append(f"args[{i}]")
        i += 1
    return problems


def _oauth_problems(oauth_value: Any) -> list[str]:
    """Refuse a literal credential in ``oauth``'s string fields.

    Validates against ``MCPOAuthConfig`` first so a non-credential field (an
    int ``callback_port``, a ``None`` scope) is never touched — only fields
    whose NAME is credential-shaped (``client_secret``) are inspected. An
    already-valid ``${secret:NAME}`` reference is kept (checked for presence,
    same as env/headers); a literal is always refused, even when it equals a
    stored secret's value — see the module docstring for why oauth never
    gets an auto-generated reference the way env/headers do.
    """
    from durin.config.schema import MCPOAuthConfig
    from durin.security.secrets import get_secret_store, parse_secret_ref

    mapping = _as_mapping(oauth_value)
    if mapping is None:
        return []
    try:
        oc = MCPOAuthConfig.model_validate(mapping)
    except Exception:  # noqa: BLE001 — a genuinely malformed oauth value is
        # reported cleanly by the caller's own MCPServerConfig validation.
        return []
    store = get_secret_store()
    problems: list[str] = []
    for field_name in type(oc).model_fields:
        if not _is_credential_key(field_name):
            continue
        value = getattr(oc, field_name)
        if not isinstance(value, str) or not value:
            continue
        ref = parse_secret_ref(value)
        if ref is not None:
            if store.get(ref) is None:
                problems.append(f"oauth.{field_name} (no stored secret named {ref})")
            continue
        problems.append(f"oauth.{field_name} (never a reference — set it outside the agent)")
    return problems


def _as_mapping(value: Any) -> dict | None:
    """*value* as a fresh, mutable ``dict``, or ``None`` when it isn't
    Mapping-like. Accepts a plain ``dict`` as well as a duck-typed Mapping
    (e.g. ``MappingProxyType``, ``OrderedDict``) so a caller passing one of
    those in Python (never JSON) still gets scanned, materialized into an
    ordinary dict this module can safely mutate."""
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "items") and not isinstance(value, (str, bytes)):
        try:
            return dict(value)
        except (TypeError, ValueError):
            return None
    return None


def _build_redactor(store: Any) -> Any:
    from durin.security.secrets import SecretRedactor

    values = {name: entry.value for name, entry in store.all().items()
              if len(entry.value) >= _MIN_SECRET_LEN}
    return SecretRedactor(values, patterns=True)


def secret_safe_config(server: str, config: dict) -> dict:
    """A copy of *config* with no literal credential anywhere in it.

    See the module docstring for which fields are scanned and how each one
    is treated (turned into a reference vs. refused outright). The refusal
    message names the fields, never their values.
    """
    from durin.security.secrets import (
        REDACTION_MARKER_PREFIX,
        get_secret_store,
        make_ref,
        parse_secret_ref,
    )

    store = get_secret_store(reload=True)
    by_value: dict[str, str] = {}
    for name, entry in sorted(store.all().items()):
        if len(entry.value) >= _MIN_SECRET_LEN:
            by_value.setdefault(entry.value, name)
    redactor = _build_redactor(store)
    # A shallow copy: only env/headers are ever mutated below (replaced with
    # a freshly-materialized plain dict each), so nothing else needs copying.
    # copy.deepcopy(config) previously blew up on a non-dict Mapping (e.g. a
    # MappingProxyType) nested inside — deepcopy falls back to pickling for a
    # type it doesn't know how to clone, and mappingproxy isn't picklable.
    safe = dict(config)
    problems: list[str] = []

    for section in ("env", "headers"):
        mapping = _as_mapping(safe.get(section))
        if mapping is None:
            continue
        for key, value in list(mapping.items()):
            if isinstance(value, str):
                if not value:
                    continue
            else:
                # dict[str, str]: a non-string value never reaches
                # MCPServerConfig.model_validate, whose pydantic error would
                # otherwise echo it verbatim.
                problems.append(f"{section}.{key} (must be a string)")
                continue
            ref = parse_secret_ref(value)
            if ref is not None:
                if store.get(ref) is None:
                    problems.append(f"{section}.{key} (no stored secret named {ref})")
                continue
            if value in by_value:
                mapping[key] = make_ref(by_value[value])
            elif REDACTION_MARKER_PREFIX in value or _is_credential(str(key), value, redactor):
                problems.append(f"{section}.{key}")
        safe[section] = mapping

    problems.extend(_oauth_problems(safe.get("oauth")))

    url = safe.get("url")
    if isinstance(url, str):
        if url and (REDACTION_MARKER_PREFIX in url or _url_is_unsafe(url, redactor)):
            problems.append("url")
    elif url is not None:
        problems.append("url (must be a string)")

    args = safe.get("args")
    if isinstance(args, list):
        problems.extend(_arg_problems(args, redactor, REDACTION_MARKER_PREFIX))

    for scalar_field in ("command", "version"):
        value = safe.get(scalar_field)
        if isinstance(value, str) and value and (
                REDACTION_MARKER_PREFIX in value or _looks_like_credential(value, redactor)):
            problems.append(scalar_field)

    sampling = safe.get("sampling")
    if isinstance(sampling, dict):
        model_value = sampling.get("model")
        if isinstance(model_value, str) and model_value and (
                REDACTION_MARKER_PREFIX in model_value or _looks_like_credential(model_value, redactor)):
            problems.append("sampling.model")

    enabled_tools = safe.get("enabled_tools")
    if isinstance(enabled_tools, list):
        for i, item in enumerate(enabled_tools):
            if isinstance(item, str) and item and (
                    REDACTION_MARKER_PREFIX in item or _looks_like_credential(item, redactor)):
                problems.append(f"enabled_tools[{i}]")

    tool_timeouts = safe.get("tool_timeouts")
    if isinstance(tool_timeouts, dict) and any(
            isinstance(k, str) and k and (REDACTION_MARKER_PREFIX in k or _looks_like_credential(k, redactor))
            for k in tool_timeouts):
        # Naming the specific key would echo it — it IS the credential here.
        problems.append("tool_timeouts (a key looks like a credential)")

    if problems:
        raise SecretValueError(
            f"Not done: {', '.join(problems)} of MCP server {server!r} must not hold "
            "a literal credential. In env/headers, call request_secret so the user "
            "stores it, then pass the whole value as ${secret:NAME} (a prefix such "
            "as 'Bearer ' is part of the stored value). Nowhere else — url, args, "
            "oauth, command, version, sampling.model, enabled_tools, tool_timeouts — "
            "is a reference resolved, so a credential can't go there at all; "
            "configure those outside the agent instead.")
    return safe


def _current_entry(name: str) -> dict | None:
    from durin.config.loader import load_config

    sc = load_config().tools.mcp_servers.get(name)
    return sc.model_dump(mode="json") if sc is not None else None


def change_hash(payload: dict) -> str:
    """sha256 over the server's current config entry plus the request."""
    blob = json.dumps(
        {"current": _current_entry(str(payload.get("name") or "")), "payload": payload},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _redact_for_display(text: str) -> str:
    """*text* with any stored-secret/pattern-shaped substring masked.

    Forces a fresh store read first: this runs on config that may not have
    gone through ``secret_safe_config`` in this call (``prepare_enable`` reads
    an already-persisted server, possibly written by a hand-edit that bypassed
    this module entirely), so a stale cached store must not hide a credential
    that IS in fact stored.
    """
    from durin.security.secrets import get_secret_store, redact_secrets

    get_secret_store(reload=True)
    return redact_secrets(text)


def _escape_for_display(text: str) -> str:
    """Neutralize control characters so a config value cannot inject extra
    lines into the text-channel approval prompt (each detail value there
    becomes exactly one printed line)."""
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def _target(config: dict) -> str:
    command = str(config.get("command") or "")
    if command:
        args = [str(a) for a in config.get("args") or []]
        target = shlex.join([command, *args])
    else:
        target = str(config.get("url") or "")
    return _escape_for_display(_redact_for_display(target))


def _fmt_setting(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _assignments(mapping: Any) -> str:
    if not isinstance(mapping, dict) or not mapping:
        return ""
    return ", ".join(f"{_escape_for_display(str(k))}={_escape_for_display(str(v))}"
                     for k, v in sorted(mapping.items()))


def _security_notes(config: dict) -> str:
    notes = [f"{f}={_fmt_setting(config[f])}" for f in _SECURITY_FIELDS if f in config]
    sampling = config.get("sampling")
    if isinstance(sampling, dict) and sampling.get("enabled"):
        notes.append(f"sampling.enabled={_fmt_setting(sampling['enabled'])}")
    return ", ".join(notes)


def _extra_detail(config: dict, *, runtime_command: str | None = None) -> dict:
    """Detail keys ``_serialize_approval`` prints beyond ``server``/``source``.

    The "server" line alone (command/args or url) hides what actually changes
    behind the scenes: env/header assignments (safe to show — a value here is
    either a plain setting or a ``${secret:NAME}`` reference, never a literal
    secret, by the time this is called), non-default security settings, and
    an install's runtime-install command.
    """
    extra: dict[str, str] = {}
    env_line = _assignments(config.get("env"))
    if env_line:
        extra["env"] = env_line
    headers_line = _assignments(config.get("headers"))
    if headers_line:
        extra["headers"] = headers_line
    security_line = _security_notes(config)
    if security_line:
        extra["security"] = security_line
    if runtime_command:
        extra["runtime"] = f"then runs: {_escape_for_display(runtime_command)}"
    return extra


def _prepared(action: str, name: str, *, summary: str, payload: dict,
              detail: dict) -> Prepared:
    full = {"action": action, "name": name, **payload}
    return Prepared(kind=KIND, summary=summary,
                    detail={"action": action, "name": name, **detail},
                    payload=full, change_hash=change_hash(full))


def _validate_config(config: dict) -> Any:
    """``MCPServerConfig.model_validate``, with a clean, value-free message on
    failure. Pydantic's own ``ValidationError`` embeds the offending input in
    each error (``input_value=...``) — exactly what must never reach a
    message that might hold a scrubbed-past-this-point credential (a
    ``list``/``int`` typo instead of a real field, a whole-field string where
    a mapping or object was expected)."""
    from pydantic import ValidationError

    from durin.config.schema import MCPServerConfig

    try:
        return MCPServerConfig.model_validate(config)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors(include_input=False))
        raise SecretValueError(
            f"Not done: {fields or 'the config'} has the wrong shape for an MCP "
            "server config. The value is not shown here — it may have held a "
            "credential — fix its type and try again.") from None


def prepare_upsert(action: str, name: str, config: dict) -> Prepared:
    """An ``add`` or ``update`` of server *name* with *config*.

    Runs the credential scrub on the model's raw dict FIRST — before any
    schema validation touches it — so a non-string ``env``/``headers`` value
    is refused with a clean message instead of surfacing pydantic's own
    ``ValidationError``, which echoes the offending input verbatim. The
    scrubbed dict is then validated and re-dumped through ``MCPServerConfig``:
    this resolves an alias/field-name collision (``spawnEgressPolicy`` vs
    ``spawn_egress_policy``) to the single value that will actually apply,
    and drops unknown keys. Finally the NORMALIZED dump is scrubbed again:
    ``model_validate`` accepts (and normalizes) Python shapes the first scrub
    doesn't recognize as the section it should scan — a ``tuple`` of args, a
    ``MappingProxyType`` for env, an ``MCPOAuthConfig`` instance for oauth —
    so a credential entering through one of those would sail through the
    first pass's plain ``isinstance(x, dict)``/``isinstance(x, list)`` checks
    untouched. Only after both passes is the result what gets stored and
    shown to the reviewer — exactly the config that will be written, never a
    raw dict that could show one thing and apply another.
    """
    safe = secret_safe_config(name, config)
    sc = _validate_config(safe)
    if not sc.command and not sc.url:
        raise ValueError("a server needs a command (stdio) or a url (http)")
    normalized = secret_safe_config(name, sc.model_dump(mode="json", exclude_defaults=True))
    target = _target(normalized)
    return _prepared(action, name, summary=f"{action} MCP server {name!r}",
                     payload={"config": normalized},
                     detail={"server": f"{name} → {target}", "target": target,
                             "config": normalized, **_extra_detail(normalized)})


def prepare_enable(name: str) -> Prepared:
    """Switching configured server *name* back on."""
    from durin.config.loader import load_config

    sc = load_config().tools.mcp_servers.get(name)
    if sc is None:
        raise ValueError(f"no MCP server named {name!r}")
    if sc.command:
        target = shlex.join([sc.command, *sc.args])
    else:
        target = sc.url
    target = _escape_for_display(_redact_for_display(target))
    return _prepared("enable", name, summary=f"enable MCP server {name!r}", payload={},
                     detail={"server": f"{name} → {target}", "target": target})


async def prepare_install(detail: Any, *, ref: str, prefer: str) -> Prepared:
    """An install of registry server *detail*, resolved to the config it writes."""
    from durin.agent import mcp_install

    name = (ref.rsplit("/", 1)[-1] or ref).strip()
    use_local = (prefer == "local" and detail.packages) or (
        not detail.remotes and detail.packages)
    runtime_plan: dict | None = None
    if use_local:
        rt = mcp_install.package_runtime(detail.packages[0])
        if not mcp_install.runtime_present(rt):
            cmd = mcp_install.runtime_install_command(rt)
            runtime_plan = {"runtime": rt, "command": cmd, "auto_installable": cmd is not None}
    sc = mcp_install.build_server_config_from_detail(detail, prefer=prefer, secret_env_refs={})
    await mcp_install.autodetect_oauth(
        sc, has_declared_headers=bool(detail.remotes and detail.remotes[0].headers))
    config = secret_safe_config(name, sc.model_dump(mode="json", exclude_defaults=True))
    target = _target(config)
    runtime_command = runtime_plan.get("command") if runtime_plan else None
    return _prepared(
        "install", name, summary=f"install MCP server {name!r} from {ref}",
        payload={"ref": ref, "config": config, "runtime_plan": runtime_plan},
        detail={"server": f"{name} → {target}", "target": target, "source": ref,
                "config": config, "runtime_plan": runtime_plan,
                **_extra_detail(config, runtime_command=runtime_command)})


async def _install_runtime(plan: dict | None, deps: ExecDeps) -> str | None:
    """Run an install's runtime-install command, if any.

    A blocked command, a timeout, or a non-zero exit fails the whole install
    (``ApprovalExecError``) instead of silently proceeding to add a server
    whose runtime never actually got installed. Reuses
    ``skills_import._install_step_failed`` for that judgment rather than
    re-deriving it: the same ExecTool return shapes (a blocked-command
    string, a timeout string, an "Exit code: N" tail) apply here.
    """
    from durin.agent.skills_import import _install_step_failed

    if not plan:
        return None
    command = plan.get("command")
    runtime = plan.get("runtime")
    if command and deps.exec_run is not None:
        output = str(await deps.exec_run(command=command))
        if _install_step_failed(output):
            raise ApprovalExecError(
                f"installing the '{runtime}' runtime failed: {output[-2000:]}")
        return f"ran: {command}"
    if command:
        return (f"runtime '{runtime}' is missing: run `{command}` on the host, then "
                "reconnect the server")
    return f"runtime '{runtime}' missing and not auto-installable — install it manually"


def _without_config(info: dict) -> dict:
    # The stored server config can hold a credential typed into the dashboard
    # as a literal, and this result is kept in the approval record.
    return {k: v for k, v in info.items() if k != "config"}


async def apply(payload: dict, deps: ExecDeps) -> dict:
    """Perform one recorded MCP change.

    ``mcp_manage`` also calls this directly under ``install_policy: auto``,
    where no approval record exists.
    """
    from durin.config.schema import MCPServerConfig
    from durin.service.mcp import McpServerNameCommand, McpServerUpsertCommand, McpService
    from durin.service.principal import Principal

    action = payload.get("action")
    name = str(payload.get("name") or "")
    service = deps.mcp if deps.mcp is not None else McpService()
    principal = Principal.local()
    out: dict[str, Any] = {"name": name}
    if action == "enable":
        result = await service.enable(McpServerNameCommand(name=name), principal)
    elif action in ("add", "update", "install"):
        if action == "install":
            out["runtime"] = await _install_runtime(payload.get("runtime_plan"), deps)
        sc = MCPServerConfig.model_validate(payload.get("config") or {})
        method = service.update if action == "update" else service.add
        result = await method(McpServerUpsertCommand(name=name, config=sc), principal)
    else:
        raise ApprovalExecError(f"unknown MCP change {action!r}")
    info = as_dict(result)
    out["result"] = _without_config(info)
    if action == "install":
        out["needs_oauth"] = info.get("status") == "needs_auth"
    if deps.mcp is None:
        out["note"] = _CONFIG_ONLY_NOTE
    return out


def _hash(workspace: Path, payload: dict) -> str:
    return change_hash(payload)


async def _execute(workspace: Path, payload: dict, deps: ExecDeps) -> dict:
    return await apply(payload, deps)


register(KIND, hash_fn=_hash, execute_fn=_execute)
