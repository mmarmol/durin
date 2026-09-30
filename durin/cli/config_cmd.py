"""`durin config` subcommand implementations.

These wrap ``durin.config.loader`` so the CLI can show, get, set, and edit
single keys in ``~/.durin/config.json`` without forcing the user through
the full onboard wizard. All writes go through ``Config.model_validate``
so a malformed edit never replaces a working config on disk.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import pydantic
import typer
from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax

from durin.config.loader import get_config_path, save_config
from durin.config.schema import Config
from durin.security.secrets import CREDENTIAL_KEY_RE

console = Console()


# ---------------------------------------------------------------------------
# Public helpers (also reused by tests)
# ---------------------------------------------------------------------------


def load_raw_config(path: Path) -> dict[str, Any]:
    """Return the on-disk config dict, transparent to the storage layout.

    Delegates to :func:`read_persisted_config` so it returns the merged
    view whether the config is a single monolith or the split per-topic
    directory. A missing file (or a bare split marker) yields ``{}``.
    """
    from durin.config.loader import read_persisted_config

    return read_persisted_config(path)


def parse_value(raw: str) -> Any:
    """Decode a value typed on the command line.

    JSON literals (booleans, null, numbers, arrays, objects, quoted strings)
    are decoded; anything else is kept as a plain string.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _parse_path(path: str) -> list[tuple[str, bool]]:
    """``(key, literal)`` pairs of a config path.

    Keys are separated by dots. A key written in brackets — ``["glm-5.3"]``,
    ``['glm-5.3']`` or ``[glm-5.3]`` — is *literal*: it may contain dots and
    is never case-normalized, which is how a map key such as a model name is
    addressed (model names routinely contain dots). Raises ``ValueError`` for
    a malformed path: an empty key, an unclosed bracket or quote, or text
    right after a closing bracket.
    """
    segments: list[tuple[str, bool]] = []
    buf = ""
    after_bracket = False
    i, n = 0, len(path)
    while i < n:
        ch = path[i]
        if ch == ".":
            if buf:
                segments.append((buf, False))
                buf = ""
            elif not after_bracket:
                raise ValueError(f"empty key in config path {path!r}")
            after_bracket = False
            i += 1
            if i == n:
                raise ValueError(f"config path {path!r} ends with a dot")
            continue
        if ch == "[":
            if buf:
                segments.append((buf, False))
                buf = ""
            i += 1
            if i < n and path[i] in "\"'":
                quote = path[i]
                end = path.find(quote, i + 1)
                if end == -1 or end + 1 >= n or path[end + 1] != "]":
                    raise ValueError(f"unclosed quoted key in config path {path!r}")
                key, i = path[i + 1:end], end + 2
            else:
                end = path.find("]", i)
                if end == -1:
                    raise ValueError(f"unclosed '[' in config path {path!r}")
                key, i = path[i:end].strip(), end + 1
            if not key:
                raise ValueError(f"empty key in config path {path!r}")
            segments.append((key, True))
            after_bracket = True
            if i < n and path[i] not in ".[":
                raise ValueError(f"unexpected text after ']' in config path {path!r}")
            continue
        if ch == "]":
            raise ValueError(f"unbalanced ']' in config path {path!r}")
        buf += ch
        i += 1
    if buf:
        segments.append((buf, False))
    if not segments:
        raise ValueError("empty config path")
    return segments


def path_segments(path: str) -> list[str]:
    """The keys of a config path, in order (see ``_parse_path`` for the
    bracket form of a key that contains dots). Raises ``ValueError``."""
    return [key for key, _ in _parse_path(path)]


def _render_path(segments: list[tuple[str, bool]]) -> str:
    """A path string for *segments*: dotted, with a key that was written in
    brackets — or that could not be read back from a dotted form — kept in
    brackets."""
    out = ""
    for key, literal in segments:
        if literal or any(c in key for c in ".[]\"'"):
            quote = "'" if '"' in key else '"'
            out += f"[{quote}{key}{quote}]"
        else:
            out += f".{key}" if out else key
    return out


def get_at(data: Any, dotted: str) -> Any:
    """Walk a config path through nested dicts and lists. Raises KeyError
    (or ValueError for a malformed path)."""
    cursor: Any = data
    for part in path_segments(dotted):
        if isinstance(cursor, dict):
            if part not in cursor:
                raise KeyError(dotted)
            cursor = cursor[part]
        elif isinstance(cursor, list):
            try:
                idx = int(part)
            except ValueError as exc:
                raise KeyError(dotted) from exc
            if idx < 0 or idx >= len(cursor):
                raise KeyError(dotted)
            cursor = cursor[idx]
        else:
            raise KeyError(dotted)
    return cursor


def set_at(data: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    """Return a deep copy of ``data`` with ``value`` written at the config
    path ``dotted``.

    Intermediate dicts are created on the fly; lists are addressed by
    integer index. Existing scalars on the path are replaced.
    """
    out = copy.deepcopy(data) if data else {}
    cursor: Any = out
    parts = path_segments(dotted)
    for part in parts[:-1]:
        if isinstance(cursor, list):
            cursor = cursor[int(part)]
            continue
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    last = parts[-1]
    if isinstance(cursor, list):
        cursor[int(last)] = value
    else:
        cursor[last] = value
    return out


def mask_secrets(data: Any) -> Any:
    """Return a deep copy with secret-keyed values masked.

    A ``${secret:NAME}`` reference is shown verbatim — it is not a
    secret, it is a pointer into the secret store, and the whole point
    of the design is that config (with references) is safe to share.
    Only literal secret values are masked.
    """
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            if (
                isinstance(v, str)
                and v
                and CREDENTIAL_KEY_RE.search(k)
                and not _is_secret_ref(v)
            ):
                out[k] = "***"
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(data, list):
        return [mask_secrets(v) for v in data]
    return data


def _is_secret_ref(value: str) -> bool:
    """True when *value* is a ``${secret:NAME}`` store reference."""
    from durin.security.secrets import is_secret_ref

    return is_secret_ref(value)


def validate_dict(data: dict[str, Any]) -> Config:
    """Validate ``data`` against the Config schema. Raises ValidationError."""
    return Config.model_validate(data)


class ConfigKeyError(ValueError):
    """A config path that does not name a place in the config."""


_DOTTED_KEY_HINT = (
    "A key that contains dots, such as a model name, goes in brackets: "
    'providers.zai_coding_plan.models["glm-5.3"].context_window_tokens'
)


def apply_setting(data: dict[str, Any], path: str, value: Any) -> Config:
    """*data* with *value* written at the config path *path*, validated.

    Raises ``pydantic.ValidationError`` when the result is not a valid
    config, and ``ConfigKeyError`` when *path* is malformed or the validated
    config does not hold the value where *path* says. Validation silently
    drops a key that is not a field of its section, so without this check a
    mistyped field — or a model name whose dots split it into several keys
    (``models.glm-5.3.x`` wrote ``x`` under ``models["glm-5"]["3"]`` and
    validation threw the ``"3"`` away) — was written nowhere, or somewhere
    unrelated, while the command reported success.
    """
    try:
        normalized = _normalize_dotted_path(path)
        new_data = set_at(data, normalized, value)
    except (ValueError, IndexError) as exc:
        raise ConfigKeyError(f"{path} does not name a config key: {exc}") from None
    config = validate_dict(new_data)
    try:
        get_at(config.model_dump(mode="json", by_alias=False), normalized)
    except KeyError:
        raise ConfigKeyError(f"{path} does not name a config key. {_DOTTED_KEY_HINT}") from None
    return config


# ---------------------------------------------------------------------------
# Typer wiring
# ---------------------------------------------------------------------------


config_app = typer.Typer(
    help="Inspect and edit durin's config.json.",
    no_args_is_help=True,
)


@config_app.command("path")
def cmd_path() -> None:
    """Print the absolute path to config.json and exit."""
    console.print(str(get_config_path()))


@config_app.command("show")
def cmd_show(
    section: str | None = typer.Argument(None, help="Optional dotted section, e.g. 'providers.zhipu'."),
    raw: bool = typer.Option(False, "--raw", help="Show secrets unmasked (as on disk)."),
) -> None:
    """Print the config (or one section), with secrets masked by default."""
    path = get_config_path()
    if not path.exists():
        console.print(f"[red]No config at {path}.[/red] Run [cyan]durin onboard[/cyan].")
        raise typer.Exit(1)
    data = load_raw_config(path)
    payload: Any = data
    if section:
        try:
            payload = get_at(data, _normalize_dotted_path(section))
        except (KeyError, ValueError):
            console.print(f"[red]No such key: {escape(section)}[/red]")
            raise typer.Exit(1) from None
    if not raw:
        payload = mask_secrets(payload)
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    if console.is_terminal:
        console.print(Syntax(text, "json", theme="ansi_dark", background_color="default"))
    else:
        console.print(text)


@config_app.command("get")
def cmd_get(
    key: str = typer.Argument(..., help="Dotted path through the config (e.g. agents.defaults.model)."),
) -> None:
    """Print one value. JSON-encoded when the value is a dict/list.

    Returns the **effective** value: schema defaults are applied, so
    keys the user never wrote to disk still resolve. For an as-on-disk
    view use ``durin config show``.
    """
    from durin.config.loader import load_config

    path = get_config_path()
    if not path.exists():
        console.print(f"[red]No config at {path}.[/red] Run [cyan]durin onboard[/cyan].")
        raise typer.Exit(1)
    # Load with defaults applied so keys with schema defaults resolve
    # even when the user never wrote them to disk (e.g.
    # `memory.embedding.model` before the first onboard pass through
    # the memory section). Fall back to the raw on-disk dict if the
    # schema rejects the config — better to surface a value than refuse
    # all queries.
    try:
        cfg = load_config(path)
        data = cfg.model_dump(by_alias=False, mode="json")
    except Exception:  # noqa: BLE001
        data = load_raw_config(path)
    try:
        value = get_at(data, _normalize_dotted_path(key))
    except (KeyError, ValueError):
        console.print(f"[red]No such key: {escape(key)}[/red]")
        raise typer.Exit(1) from None
    if isinstance(value, (dict, list)):
        console.print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    elif value is None:
        console.print("null")
    else:
        console.print(str(value))


def _resolve_ref(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow a JSON-schema ``$ref`` chain into ``$defs``."""
    while "$ref" in node:
        node = defs[node["$ref"].split("/")[-1]]
    return node


def _child_schema(node: dict[str, Any], seg: str, defs: dict[str, Any]) -> dict[str, Any] | None:
    """Return the schema node for path segment *seg* inside object node *node*.

    Properties are matched by snake_case or camelCase name (the schema
    serializes aliases). For dict-valued fields (``additionalProperties``)
    the segment is a user-chosen key, so the value schema is returned.
    """
    from pydantic.alias_generators import to_camel

    resolved = _resolve_ref(node, defs)
    candidates = [resolved] + [_resolve_ref(s, defs) for s in resolved.get("anyOf", [])]
    for cand in candidates:
        props = cand.get("properties", {})
        for name in (seg, to_camel(seg)):
            if name in props:
                return props[name]
        extra = cand.get("additionalProperties")
        if isinstance(extra, dict):
            return extra
    return None


def _type_str(node: dict[str, Any], defs: dict[str, Any]) -> str:
    """Human-readable type for a schema node."""
    if "$ref" in node:
        return node["$ref"].split("/")[-1]
    if "anyOf" in node:
        return " | ".join(_type_str(s, defs) for s in node["anyOf"])
    if "enum" in node:
        return "enum"
    t = node.get("type")
    if isinstance(t, str):
        return t
    if "properties" in node or "additionalProperties" in node:
        return "object"
    return "any"


def _schema_constraints(node: dict[str, Any]) -> dict[str, Any]:
    """Collect display-worthy constraints from a node (and its anyOf branches)."""
    keys = (
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "enum", "const", "pattern", "minLength", "maxLength",
    )
    out: dict[str, Any] = {}
    for n in [node, *node.get("anyOf", [])]:
        for k in keys:
            if k in n:
                out.setdefault(k, n[k])
    return out


@config_app.command("schema")
def cmd_schema(
    key: str | None = typer.Argument(
        None, help="Dotted config path (e.g. memory.owner); omit to list top-level sections."
    ),
) -> None:
    """Describe config keys from the schema: type, default, constraints, description."""
    schema = Config.model_json_schema()
    defs = schema.get("$defs", {})

    if not key:
        for name, prop in schema.get("properties", {}).items():
            console.print(f"[cyan]{_normalize_dotted_path(name)}[/cyan]  {prop.get('description', '')}")
        console.print("[dim]Use `durin config schema <dotted.key>` for one key.[/dim]")
        return

    try:
        normalized = _normalize_dotted_path(key)
    except ValueError:
        console.print(f"[red]No such config key: {escape(key)}[/red]")
        raise typer.Exit(1) from None
    node: dict[str, Any] = schema
    for seg in path_segments(normalized):
        child = _child_schema(node, seg, defs)
        if child is None:
            console.print(f"[red]No such config key: {escape(key)}[/red]")
            raise typer.Exit(1)
        node = child

    console.print(f"[bold]{escape(normalized)}[/bold]")
    console.print(f"  type: {_type_str(node, defs)}")
    try:
        default = get_at(Config().model_dump(mode="json", by_alias=False), normalized)
        console.print(f"  default: {json.dumps(default, ensure_ascii=False)}")
    except KeyError:
        if "default" in node:
            console.print(f"  default: {json.dumps(node['default'], ensure_ascii=False)}")
    for name, value in _schema_constraints(node).items():
        console.print(f"  {name}: {json.dumps(value, ensure_ascii=False)}")
    resolved = _resolve_ref(node, defs)
    description = node.get("description") or resolved.get("description")
    if description:
        console.print(f"  description: {description}")
    if resolved.get("properties"):
        console.print("  keys:")
        for name, prop in resolved["properties"].items():
            console.print(
                f"    [cyan]{_normalize_dotted_path(name)}[/cyan]  {prop.get('description', '')}"
            )


@config_app.command("set")
def cmd_set(
    key: str = typer.Argument(..., help="Dotted path through the config."),
    value: str = typer.Argument(..., help="New value (JSON-decoded when possible)."),
) -> None:
    """Set one value. Validated against the schema before writing.

    Bootstraps a default config when none exists yet, so a fresh
    install can be configured purely from the command line without
    running the wizard first.
    """
    path = get_config_path()
    bootstrapped = not path.exists()
    raw = load_raw_config(path)  # {} when the file is absent
    # Canonicalize the dict to snake_case (the on-disk + field-name form)
    # before mutating, so set_at writes the canonical key and pydantic's
    # snake field names resolve without parallel camelCase duplicates.
    try:
        canonical = validate_dict(raw).model_dump(mode="json", by_alias=False)
    except pydantic.ValidationError as e:
        console.print("[red]On-disk config is invalid; refusing to edit.[/red]")
        console.print(str(e))
        raise typer.Exit(1) from None
    try:
        config = apply_setting(canonical, key, parse_value(value))
    except ConfigKeyError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        console.print("Config not modified.")
        raise typer.Exit(1) from None
    except pydantic.ValidationError as e:
        console.print("[red]Validation failed; config not modified.[/red]")
        console.print(str(e))
        raise typer.Exit(1) from None
    save_config(config, path)
    if bootstrapped:
        console.print(f"[green]✓[/green] Created config at {path}")
    console.print(f"[green]✓[/green] {escape(key)} updated.")


def _snake(seg: str) -> str:
    """camelCase → snake_case for one key; numeric keys (list indices) and
    keys that already contain an underscore pass through."""
    if seg.isdigit() or "_" in seg:
        return seg
    out: list[str] = []
    for i, ch in enumerate(seg):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


@lru_cache(maxsize=1)
def _field_name_schema() -> dict[str, Any]:
    """The config's JSON schema with properties under their field names."""
    return Config.model_json_schema(by_alias=False)


def _normalize_key(
    seg: str, literal: bool, node: dict[str, Any] | None, defs: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """The canonical form of one path key, and the schema node under it.

    A field name is case-tolerant (``apiKey`` → ``api_key``). A key of a
    typed map — a model name under ``providers.<p>.models``, a preset name,
    a header name — is the user's and is kept as typed: case-normalizing it
    turned ``MiniMax-M2`` into ``mini_max-_m2``. Outside the typed schema
    (a free-form dict) a key keeps the plain snake_case rule.
    """
    if node is None:
        return (seg if literal else _snake(seg)), None
    resolved = _resolve_ref(node, defs)
    candidates = [resolved] + [_resolve_ref(s, defs) for s in resolved.get("anyOf", [])]
    names = [seg] if literal else [seg, _snake(seg)]
    for cand in candidates:
        props = cand.get("properties", {})
        for name in names:
            if name in props:
                return name, props[name]
    for cand in candidates:
        extra = cand.get("additionalProperties")
        if isinstance(extra, dict):
            return seg, extra
        items = cand.get("items")
        if isinstance(items, dict) and seg.isdigit():
            return seg, items
    return (seg if literal else _snake(seg)), None


def _normalize_dotted_path(dotted: str) -> str:
    """The canonical form of a config path: field names in snake_case (input
    is case-tolerant, so ``providers.zhipu.apiKey`` resolves the same as
    ``providers.zhipu.api_key``), map keys and list indices as typed, and a
    key that contains dots in brackets. Raises ``ValueError`` for a
    malformed path.
    """
    schema = _field_name_schema()
    defs = schema.get("$defs", {})
    node: dict[str, Any] | None = schema
    out: list[tuple[str, bool]] = []
    for seg, literal in _parse_path(dotted):
        key, node = _normalize_key(seg, literal, node, defs)
        out.append((key, literal))
    return _render_path(out)


@config_app.command("import")
def cmd_import(
    source: str = typer.Argument(
        ..., help="An old config.json, config.json.d/ dir, or a ~/.durin directory."
    ),
) -> None:
    """Import an existing config and migrate its plaintext secrets.

    Copies the config from SOURCE into place, then moves any plaintext
    provider API keys it carried into the secret store. Use it to
    replicate a setup on a fresh install without re-running the wizard
    (e.g. `durin config import ~/.durin_backup`).
    """
    from durin.config.loader import backup_config, load_config, save_config
    from durin.security.secrets import migrate_plaintext_provider_keys

    src = Path(source).expanduser()
    if src.is_dir():
        src_config = src / "config.json"
        if not src_config.exists() and src.name == "config.json.d":
            src_config = src.parent / "config.json"
    else:
        src_config = src
    if not src_config.exists():
        console.print(f"[red]No config found at {source}.[/red]")
        raise typer.Exit(1)

    try:
        imported = load_config(src_config)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Could not read config from {source}: {e}[/red]")
        raise typer.Exit(1) from None

    dest = get_config_path()
    backup = backup_config(dest)
    if backup is not None:
        console.print(f"[dim]Existing config backed up to {backup}[/dim]")
    save_config(imported, dest)
    created = migrate_plaintext_provider_keys(dest)

    console.print(f"[green]✓[/green] Imported config from {source}.")
    if created:
        console.print(
            f"[green]✓[/green] Moved {len(created)} plaintext key(s) into the "
            f"secret store: {', '.join(created)}"
        )
    console.print(
        "[dim]Review with `durin config show` and `durin secret list`.[/dim]"
    )


@config_app.command("edit")
def cmd_edit() -> None:
    """Open config.json in $EDITOR; restore on validation failure."""
    path = get_config_path()
    if not path.exists():
        console.print(f"[red]No config at {path}.[/red] Run [cyan]durin onboard[/cyan].")
        raise typer.Exit(1)
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or _default_editor()
    if shutil.which(editor) is None:
        console.print(f"[red]Editor {editor!r} not found on PATH.[/red] Set $EDITOR.")
        raise typer.Exit(1)
    # Edit the merged view (works for both monolith and split layouts);
    # the write goes back through save_config, which re-splits as needed.
    original = json.dumps(load_raw_config(path), indent=2, ensure_ascii=False)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        tmp.write(original)
        tmp_path = Path(tmp.name)
    try:
        subprocess.run([editor, str(tmp_path)], check=False)
        edited = tmp_path.read_text(encoding="utf-8")
        if edited == original:
            console.print("[yellow]No changes.[/yellow]")
            return
        try:
            data = json.loads(edited)
            config = validate_dict(data)
        except (json.JSONDecodeError, pydantic.ValidationError) as e:
            console.print("[red]Edit rejected; config left untouched.[/red]")
            console.print(str(e))
            raise typer.Exit(1) from None
        save_config(config, path)
        console.print(f"[green]✓[/green] Config updated at {path}.")
    finally:
        with __import__("contextlib").suppress(FileNotFoundError):
            tmp_path.unlink()


def _default_editor() -> str:
    """Pick a sane default editor for the current platform."""
    for candidate in ("nano", "vim", "vi"):
        if shutil.which(candidate):
            return candidate
    return "vi"
