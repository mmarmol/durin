"""Shared path helpers for workspace-scoped tools."""

from pathlib import Path

from durin.config.paths import get_media_dir

WORKSPACE_BOUNDARY_NOTE = (
    " (this is a hard policy boundary, not a transient failure; "
    "do not retry with shell tricks or alternative tools, and ask "
    "the user how to proceed if the resource is genuinely required)"
)


def is_under(path: Path, directory: Path) -> bool:
    """Return True when path resolves under directory."""
    try:
        path.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def resolve_workspace_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
    work_dir: Path | None = None,
    denied_subdirs: list[Path] | None = None,
) -> Path:
    """Resolve path against workspace (or the session work dir) and enforce
    allowed-directory containment.

    Relative paths anchor to ``work_dir`` unless their first segment is a
    managed prefix (then to ``workspace``). With ``work_dir=None`` the original
    workspace-relative behavior is preserved.

    ``denied_subdirs`` is a second, narrower gate checked after the
    allowed-directory containment: even a path inside the allowed directory
    is refused if it falls under one of these subdirs. Callers use this to
    carve out a read-only or publish-only area (e.g. the skills registry)
    within an otherwise-writable workspace.
    """
    from durin.agent.tools.work_area import anchored_base

    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        first = p.parts[0] if p.parts else ""
        base = anchored_base(first, workspace, work_dir)
        p = base / p
    resolved = p.resolve()
    if allowed_dir:
        media_path = get_media_dir().resolve()
        all_dirs = [allowed_dir, media_path, *(extra_allowed_dirs or [])]
        if work_dir is not None:
            all_dirs.append(work_dir.resolve())
        if not any(is_under(resolved, d) for d in all_dirs):
            raise PermissionError(
                f"Path {path} is outside allowed directory {allowed_dir}"
                + WORKSPACE_BOUNDARY_NOTE
            )
    for denied in (denied_subdirs or []):
        if is_under(resolved, denied):
            raise PermissionError(
                f"Path {path} is under the protected skills registry. Author skills under "
                f"skill-drafts/<name>/ and run skill_publish to activate them." + WORKSPACE_BOUNDARY_NOTE
            )
    return resolved
