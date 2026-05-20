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
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pydantic
import typer
from rich.console import Console
from rich.syntax import Syntax

from durin.config.loader import get_config_path, save_config
from durin.config.schema import Config

console = Console()

# Field-name patterns whose values get masked by `config show` (without --raw).
# Match is case-insensitive on the leaf key.
_SECRET_KEY_PATTERN = re.compile(
    r"(api_?key|secret|token|password|client_secret|access_key|refresh_token)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public helpers (also reused by tests)
# ---------------------------------------------------------------------------


def load_raw_config(path: Path) -> dict[str, Any]:
    """Return the on-disk JSON dict, or an empty default if the file is missing."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def parse_value(raw: str) -> Any:
    """Decode a value typed on the command line.

    JSON literals (booleans, null, numbers, arrays, objects, quoted strings)
    are decoded; anything else is kept as a plain string.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def get_at(data: Any, dotted: str) -> Any:
    """Walk a dotted path through nested dicts and lists. Raises KeyError."""
    cursor: Any = data
    for part in dotted.split("."):
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
    """Return a deep copy of ``data`` with ``value`` written at ``dotted``.

    Intermediate dicts are created on the fly; lists are addressed by
    integer index. Existing scalars on the path are replaced.
    """
    out = copy.deepcopy(data) if data else {}
    cursor: Any = out
    parts = dotted.split(".")
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
    """Return a deep copy with any value whose key matches a secret pattern masked."""
    if isinstance(data, dict):
        return {
            k: ("***" if isinstance(v, str) and v and _SECRET_KEY_PATTERN.search(k) else mask_secrets(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [mask_secrets(v) for v in data]
    return data


def validate_dict(data: dict[str, Any]) -> Config:
    """Validate ``data`` against the Config schema. Raises ValidationError."""
    return Config.model_validate(data)


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
        except KeyError:
            console.print(f"[red]No such key: {section}[/red]")
            raise typer.Exit(1)
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
    """Print one value. JSON-encoded when the value is a dict/list."""
    path = get_config_path()
    if not path.exists():
        console.print(f"[red]No config at {path}.[/red] Run [cyan]durin onboard[/cyan].")
        raise typer.Exit(1)
    data = load_raw_config(path)
    try:
        value = get_at(data, _normalize_dotted_path(key))
    except KeyError:
        console.print(f"[red]No such key: {key}[/red]")
        raise typer.Exit(1)
    if isinstance(value, (dict, list)):
        console.print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    elif value is None:
        console.print("null")
    else:
        console.print(str(value))


@config_app.command("set")
def cmd_set(
    key: str = typer.Argument(..., help="Dotted path through the config."),
    value: str = typer.Argument(..., help="New value (JSON-decoded when possible)."),
) -> None:
    """Set one value. Validated against the schema before writing."""
    path = get_config_path()
    if not path.exists():
        console.print(f"[red]No config at {path}.[/red] Run [cyan]durin onboard[/cyan].")
        raise typer.Exit(1)
    raw = load_raw_config(path)
    # Canonicalize the dict to alias-form (camelCase) before mutating so
    # we don't end up with parallel snake_case + camelCase keys that
    # pydantic's alias-first resolution would silently drop.
    try:
        canonical = validate_dict(raw).model_dump(mode="json", by_alias=True)
    except pydantic.ValidationError as e:
        console.print(f"[red]On-disk config is invalid; refusing to edit.[/red]")
        console.print(str(e))
        raise typer.Exit(1)
    new_data = set_at(canonical, _normalize_dotted_path(key), parse_value(value))
    try:
        config = validate_dict(new_data)
    except pydantic.ValidationError as e:
        console.print(f"[red]Validation failed; config not modified.[/red]")
        console.print(str(e))
        raise typer.Exit(1)
    save_config(config, path)
    console.print(f"[green]✓[/green] {key} updated.")


def _normalize_dotted_path(dotted: str) -> str:
    """Convert each snake_case segment in a dotted path to camelCase.

    The on-disk config is stored in alias form, so a user typing
    ``providers.zhipu.api_key`` should set ``providers.zhipu.apiKey``.
    Numeric segments (list indices) are passed through.
    """
    def _camel(seg: str) -> str:
        if seg.isdigit():
            return seg
        if "_" not in seg:
            return seg
        head, *rest = seg.split("_")
        return head + "".join(p[:1].upper() + p[1:] for p in rest)

    return ".".join(_camel(s) for s in dotted.split("."))


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
    original = path.read_text(encoding="utf-8")
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
            validate_dict(data)
        except (json.JSONDecodeError, pydantic.ValidationError) as e:
            console.print(f"[red]Edit rejected; config left untouched.[/red]")
            console.print(str(e))
            raise typer.Exit(1)
        path.write_text(edited, encoding="utf-8")
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
