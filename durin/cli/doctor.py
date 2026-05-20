"""`durin doctor` — diagnose install, config, providers, and runtime state.

Runs a battery of small checks and prints a status table. Each check
returns ``ok`` / ``warn`` / ``fail`` with an actionable fix message.
Exit code 0 only when every check is ``ok`` or ``warn`` — ``fail`` flips
the process exit so CI / shell pipelines can gate on it.

Optional behaviour:
- ``--ping``: tests reachability of the active provider's ``api_base``.
- ``--fix``: applies the small subset of fixes that are always safe
  (creating the workspace directory, replaying config migration).
- ``--json``: machine-readable output for scripts.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal

import typer
from rich.console import Console
from rich.table import Table

from durin import __version__
from durin.config.loader import get_config_path, load_config

console = Console()

Status = Literal["ok", "warn", "fail"]

_STATUS_GLYPH = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}
_STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2}

_PYTHON_MIN = (3, 11)


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    fix: str | None = None
    category: str = "general"
    # When this result is a missing optional extra, record which extra it
    # belongs to so `--install-missing` can group + install correctly.
    extra: str | None = None


# ---------------------------------------------------------------------------
# Individual checks — each returns a CheckResult.
# Checks are intentionally small + isolated so they can be unit-tested.
# ---------------------------------------------------------------------------


def check_python_version() -> CheckResult:
    v = sys.version_info
    if (v.major, v.minor) >= _PYTHON_MIN:
        return CheckResult(
            "python", "ok", f"Python {v.major}.{v.minor}.{v.micro}", category="system",
        )
    return CheckResult(
        "python", "fail",
        f"Python {v.major}.{v.minor}.{v.micro} (need >= {_PYTHON_MIN[0]}.{_PYTHON_MIN[1]})",
        fix=f"Install Python {_PYTHON_MIN[0]}.{_PYTHON_MIN[1]} or newer.",
        category="system",
    )


def check_durin_version() -> CheckResult:
    return CheckResult(
        "durin version", "ok", f"durin {__version__}", category="system",
    )


def check_config_file() -> CheckResult:
    path = get_config_path()
    if not path.exists():
        return CheckResult(
            "config file", "fail",
            f"Missing at {path}",
            fix="Run `durin onboard` (add `--wizard` for the interactive form).",
            category="config",
        )
    try:
        path.read_text(encoding="utf-8")
    except OSError as e:
        return CheckResult("config file", "fail", f"Cannot read {path}: {e}", category="config")
    return CheckResult("config file", "ok", str(path), category="config")


def check_config_parses() -> CheckResult:
    path = get_config_path()
    if not path.exists():
        return CheckResult("config valid", "fail", "No config to validate.", category="config")
    try:
        with path.open(encoding="utf-8") as f:
            json.load(f)
    except json.JSONDecodeError as e:
        return CheckResult(
            "config valid", "fail",
            f"JSON parse error: {e}",
            fix="Edit the file by hand, or back it up and run `durin onboard` to start over.",
            category="config",
        )
    try:
        load_config(path)
    except Exception as e:  # noqa: BLE001 — pydantic ValidationError or downstream
        return CheckResult(
            "config valid", "fail",
            f"Schema validation failed: {e}",
            fix="Run `durin upgrade --migrate-only`, or revert to `~/.durin/config.json.bak`.",
            category="config",
        )
    return CheckResult("config valid", "ok", "Schema validation passed.", category="config")


def check_workspace() -> CheckResult:
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001 — config errors are caught by check_config_parses
        return CheckResult(
            "workspace", "warn", "Could not load config to resolve workspace.", category="config",
        )
    ws = cfg.workspace_path
    if not ws.exists():
        return CheckResult(
            "workspace", "warn",
            f"Missing at {ws}",
            fix="It will be created on first agent run, or run `durin doctor --fix`.",
            category="config",
        )
    if not os.access(ws, os.W_OK):
        return CheckResult(
            "workspace", "fail",
            f"{ws} is not writable.",
            fix=f"chmod +w {ws}",
            category="config",
        )
    return CheckResult("workspace", "ok", str(ws), category="config")


def check_state_dirs_writable() -> CheckResult:
    """Verify ~/.durin and ~/.cache/durin are writable (or at least their parents)."""
    home = Path.home()
    targets = [home / ".durin", home / ".cache" / "durin"]
    problems: list[str] = []
    for t in targets:
        # Walk up to the first existing ancestor and require it to be writable.
        anchor = t
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        if not os.access(anchor, os.W_OK):
            problems.append(f"{anchor} is not writable")
    if problems:
        return CheckResult(
            "state dirs writable", "fail",
            "; ".join(problems),
            fix="Check filesystem permissions on your $HOME.",
            category="config",
        )
    return CheckResult(
        "state dirs writable", "ok",
        "~/.durin and ~/.cache/durin are reachable + writable.",
        category="config",
    )


def check_at_least_one_provider() -> CheckResult:
    """At least one provider must be usable (api_key set, OAuth token, or local backend)."""
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return CheckResult(
            "providers", "warn", "Could not load config to inspect providers.", category="providers",
        )
    from durin.providers.registry import PROVIDERS

    usable: list[str] = []
    for spec in PROVIDERS:
        p = getattr(cfg.providers, spec.name, None)
        if p is None:
            continue
        if spec.is_oauth:
            # OAuth providers report status via `durin provider login` and store
            # tokens outside config.json — we treat them as "usable" if the
            # token file exists. Check this opportunistically.
            if _oauth_token_present(spec.name):
                usable.append(f"{spec.label} (OAuth)")
        elif spec.is_local:
            if p.api_base:
                usable.append(f"{spec.label} ({p.api_base})")
        else:
            if p.api_key:
                usable.append(spec.label)

    if usable:
        return CheckResult(
            "providers", "ok",
            f"{len(usable)} configured: " + ", ".join(usable[:3]) + ("…" if len(usable) > 3 else ""),
            category="providers",
        )
    return CheckResult(
        "providers", "fail",
        "No provider is configured.",
        fix="Set one via `durin config set providers.<vendor>.api_key …` or `durin provider login …`.",
        category="providers",
    )


def _oauth_token_present(provider_name: str) -> bool:
    """Best-effort check for an OAuth token file on disk."""
    home = Path.home()
    candidates = [
        home / ".durin" / "oauth" / f"{provider_name}.json",
        home / f".{provider_name}" / "auth.json",
    ]
    return any(c.exists() for c in candidates)


def check_default_model_resolvable() -> CheckResult:
    """The default-preset model should resolve to either capabilities or an override."""
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return CheckResult(
            "default model", "warn", "Could not load config.", category="providers",
        )
    try:
        preset = cfg.resolve_preset()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "default model", "fail",
            f"Preset {cfg.agents.defaults.model_preset!r} cannot be resolved: {e}",
            fix="`durin config set agents.defaults.modelPreset default`",
            category="providers",
        )
    model = preset.model
    if not model:
        return CheckResult(
            "default model", "fail",
            "agents.defaults.model is empty.",
            fix="`durin config set agents.defaults.model glm-5.1` (or your preferred model)",
            category="providers",
        )
    return CheckResult("default model", "ok", f"{model} (preset: {cfg.agents.defaults.model_preset!r})", category="providers")


def check_executable(name: str, *, required: bool, hint: str) -> CheckResult:
    found = shutil.which(name)
    if found:
        return CheckResult(name, "ok", found, category="tools")
    status: Status = "fail" if required else "warn"
    return CheckResult(name, status, f"`{name}` not on PATH", fix=hint, category="tools")


def check_optional_extra(import_name: str, *, extra: str, purpose: str) -> CheckResult:
    """Verify an optional extra's import works. Always returns ok/warn (never fail)."""
    try:
        importlib.import_module(import_name)
        return CheckResult(import_name, "ok", f"{import_name} importable", category="extras", extra=extra)
    except ImportError:
        from durin.cli.upgrade import install_hint

        return CheckResult(
            import_name, "warn",
            f"Not installed — needed for: {purpose}",
            fix=install_hint([extra]),
            category="extras",
            extra=extra,
        )


def check_cache_size() -> CheckResult:
    cache = Path.home() / ".cache" / "durin"
    if not cache.exists():
        return CheckResult("cache size", "ok", "no cache yet", category="state")
    total = 0
    for root, _dirs, files in os.walk(cache, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    gb = total / (1024 ** 3)
    if gb > 10:
        return CheckResult(
            "cache size", "warn",
            f"{gb:.1f} GB at {cache}",
            fix="`durin uninstall --keep-config --keep-workspace --yes` to drop caches.",
            category="state",
        )
    if gb > 1:
        return CheckResult("cache size", "ok", f"{gb:.2f} GB at {cache}", category="state")
    mb = total / (1024 ** 2)
    return CheckResult("cache size", "ok", f"{mb:.1f} MB at {cache}", category="state")


def check_provider_reachable(*, timeout: float = 3.0) -> CheckResult:
    """`--ping`: HEAD/GET against the configured provider's api_base."""
    try:
        cfg = load_config()
        preset = cfg.resolve_preset()
    except Exception:  # noqa: BLE001
        return CheckResult("provider ping", "warn", "Could not resolve active provider.", category="providers")
    from durin.providers.registry import find_by_name

    spec_name = preset.provider if preset.provider != "auto" else None
    if not spec_name:
        return CheckResult(
            "provider ping", "warn",
            "agents.defaults.provider is 'auto'; skipping ping.",
            category="providers",
        )
    spec = find_by_name(spec_name)
    if spec is None:
        return CheckResult("provider ping", "warn", f"unknown provider {spec_name!r}", category="providers")
    p = getattr(cfg.providers, spec.name, None)
    base = p.api_base if p and getattr(p, "api_base", None) else getattr(spec, "default_base_url", None)
    if not base:
        return CheckResult("provider ping", "warn", f"{spec.label}: no api_base set.", category="providers")

    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            r = client.get(base)
            return CheckResult(
                "provider ping", "ok",
                f"{spec.label} HTTP {r.status_code} ({base})",
                category="providers",
            )
    except Exception as e:  # noqa: BLE001 — network errors of any flavor
        return CheckResult(
            "provider ping", "fail",
            f"{spec.label} unreachable at {base}: {e}",
            fix="Check the api_base + network. Some endpoints reject bare GET — see HTTP code.",
            category="providers",
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class DoctorReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, r: CheckResult) -> None:
        self.results.append(r)

    @property
    def worst(self) -> Status:
        if not self.results:
            return "ok"
        return max(self.results, key=lambda r: _STATUS_ORDER[r.status]).status

    @property
    def counts(self) -> dict[Status, int]:
        c: dict[Status, int] = {"ok": 0, "warn": 0, "fail": 0}
        for r in self.results:
            c[r.status] += 1
        return c


def run_checks(*, ping: bool = False) -> DoctorReport:
    report = DoctorReport()
    report.add(check_python_version())
    report.add(check_durin_version())
    report.add(check_config_file())
    report.add(check_config_parses())
    report.add(check_workspace())
    report.add(check_state_dirs_writable())
    report.add(check_at_least_one_provider())
    report.add(check_default_model_resolvable())
    report.add(check_executable("git", required=False, hint="Install git so `durin upgrade` can pull editable installs."))
    report.add(check_optional_extra("fastembed", extra="memory", purpose="vector recall over memory/"))
    report.add(check_optional_extra("lancedb", extra="memory", purpose="vector index storage"))
    report.add(check_optional_extra("mcp", extra="mcp", purpose="MCP server mode"))
    report.add(check_cache_size())
    if ping:
        report.add(check_provider_reachable())
    return report


def apply_safe_fixes() -> list[str]:
    """Apply the small subset of always-safe fixes. Returns a list of human messages."""
    applied: list[str] = []
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        cfg = None
    if cfg is not None:
        ws = cfg.workspace_path
        if not ws.exists():
            ws.mkdir(parents=True, exist_ok=True)
            applied.append(f"Created workspace at {ws}")
    # Replay schema migration (no-op if up to date).
    from durin.cli.upgrade import migrate_config_file

    if migrate_config_file():
        applied.append("Re-saved config with current schema defaults.")
    return applied


def collect_missing_extras(report: DoctorReport) -> list[str]:
    """Return the unique list of extras whose import failed in this report."""
    seen: list[str] = []
    for r in report.results:
        if r.category == "extras" and r.status == "warn" and r.extra and r.extra not in seen:
            seen.append(r.extra)
    return seen


def install_missing_extras(extras: list[str], *, assume_yes: bool = False) -> int:
    """Run the mode-aware install command for ``extras``. Returns the exit code.

    pipx installs use ``--force`` to swap the venv layout, which is mildly
    destructive (anything injected separately gets dropped). We confirm
    before doing it unless ``assume_yes`` is set.
    """
    from durin.cli.upgrade import detect_install_mode, install_hint

    if not extras:
        console.print("[dim]No missing extras to install.[/dim]")
        return 0
    info = detect_install_mode()
    cmd_str = install_hint(extras, mode=info.mode)
    console.print(f"[bold]Detected install mode:[/bold] {info.mode}")
    console.print(f"[bold]Would run:[/bold] [cyan]{cmd_str}[/cyan]")
    if info.mode == "unknown":
        console.print(
            "[red]Cannot auto-install: install mode is unknown.[/red] "
            "Run the command above manually."
        )
        return 1
    if info.mode == "editable":
        console.print(
            "[yellow]Editable mode: run the command above from the source root yourself.[/yellow]"
        )
        return 0
    if not assume_yes:
        if not typer.confirm("Run it?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            return 1
    # Re-derive the command as a list (instead of shell-quoted string) so we
    # don't shell out and don't need to parse our own quoting.
    if info.mode == "pipx":
        bracket = f"[{','.join(extras)}]" if extras else ""
        cmd = ["pipx", "install", "--force", f"durin-agent{bracket}"]
    else:
        bracket = f"[{','.join(extras)}]" if extras else ""
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", f"durin-agent{bracket}"]
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    proc = subprocess.run(cmd)
    return proc.returncode


def render_table(report: DoctorReport) -> None:
    by_category: dict[str, list[CheckResult]] = {}
    for r in report.results:
        by_category.setdefault(r.category, []).append(r)
    for category, rows in by_category.items():
        table = Table(title=category, show_header=True, header_style="bold")
        table.add_column("", width=2)
        table.add_column("Check")
        table.add_column("Detail", overflow="fold")
        for r in rows:
            table.add_row(_STATUS_GLYPH[r.status], r.name, r.message)
        console.print(table)

    fixes = [r for r in report.results if r.status in ("warn", "fail") and r.fix]
    if fixes:
        console.print("\n[bold]Suggested fixes:[/bold]")
        for r in fixes:
            # `r.fix` is plain text and may contain `[extra]` literals that
            # Rich would otherwise interpret as markup tags. Use highlight=False
            # to disable Rich parsing entirely for the fix string.
            console.print(f"  [dim]{r.name}:[/dim] ", end="")
            console.out(r.fix)

    counts = report.counts
    summary = f"\n{counts['ok']} ok · {counts['warn']} warn · {counts['fail']} fail"
    color = "green" if report.worst == "ok" else ("yellow" if report.worst == "warn" else "red")
    console.print(f"[{color}]{summary}[/{color}]")


def render_json(report: DoctorReport) -> None:
    payload = {
        "worst": report.worst,
        "counts": report.counts,
        "results": [asdict(r) for r in report.results],
    }
    # Plain `print` — never inject Rich ANSI codes into machine output.
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def run_doctor(
    *,
    ping: bool = False,
    fix: bool = False,
    as_json: bool = False,
    install_missing: bool = False,
    assume_yes: bool = False,
) -> int:
    if fix:
        applied = apply_safe_fixes()
        if applied and not as_json:
            console.print("[bold]Applied fixes:[/bold]")
            for m in applied:
                console.print(f"  [green]✓[/green] {m}")
            console.print("")
    report = run_checks(ping=ping)
    if install_missing:
        extras = collect_missing_extras(report)
        if extras:
            console.print(f"\n[bold]Missing extras:[/bold] {', '.join(extras)}")
            rc = install_missing_extras(extras, assume_yes=assume_yes)
            if rc != 0:
                return rc
            # Re-run the checks so the user sees the updated state.
            console.print("\n[bold]Re-checking…[/bold]\n")
            report = run_checks(ping=ping)
    if as_json:
        render_json(report)
    else:
        render_table(report)
    return 0 if report.worst != "fail" else 1


def register(app: typer.Typer) -> None:
    """Attach the `doctor` command to a Typer app."""

    @app.command("doctor")
    def doctor(
        ping: bool = typer.Option(False, "--ping", help="Test reachability of the active provider's api_base."),
        fix: bool = typer.Option(False, "--fix", help="Apply safe fixes (create workspace, re-save config)."),
        install_missing: bool = typer.Option(
            False,
            "--install-missing",
            help="Auto-install any missing optional extras (uses the right command for the detected install mode).",
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Diagnose install, config, providers, and runtime state."""
        rc = run_doctor(
            ping=ping,
            fix=fix,
            as_json=as_json,
            install_missing=install_missing,
            assume_yes=yes,
        )
        if rc != 0:
            raise typer.Exit(rc)
