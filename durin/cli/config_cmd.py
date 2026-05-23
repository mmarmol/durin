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
                and _SECRET_KEY_PATTERN.search(k)
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
    """Set one value. Validated against the schema before writing.

    Bootstraps a default config when none exists yet, so a fresh
    install can be configured purely from the command line without
    running the wizard first.
    """
    path = get_config_path()
    bootstrapped = not path.exists()
    raw = load_raw_config(path)  # {} when the file is absent
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
    if bootstrapped:
        console.print(f"[green]✓[/green] Created config at {path}")
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
        raise typer.Exit(1)

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
            raise typer.Exit(1)
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
