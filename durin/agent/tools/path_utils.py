"""Shared path helpers for workspace-scoped tools."""

import os
from pathlib import Path

from durin.config.paths import get_media_dir

WORKSPACE_BOUNDARY_NOTE = (
    " (this is a hard policy boundary, not a transient failure; "
    "do not retry with shell tricks or alternative tools, and ask "
    "the user how to proceed if the resource is genuinely required)"
)


# The write door each registry owns, keyed by the registry directory's name.
# The refusal has to name the RIGHT door: a single hardcoded "use skill_publish"
# message sent an agent trying to edit an automation definition off to the
# skills workflow, which cost it several turns before it found `automations(action=…)`.
_REGISTRY_DOORS = {
    "skills": "author the skill under skill-drafts/<name>/ and run skill_publish",
    "workflows": ("use workflow_write / workflow_edit, or workflow_script_write "
                  "for workflows/scripts/"),
    "automations": ("use the automations tool — action='create' replaces an existing "
                     "definition, action='enable'/'pause' toggles it — or the webui's "
                     "automations editor"),
}

# Denied directories that have no write door at all — nothing the agent calls
# writes here under any name, so the refusal must not claim a door exists (as
# the _REGISTRY_DOORS wording does). Keyed by the directory's name, value is
# why the agent can't write there.
_NO_DOOR_REASONS = {
    ".approvals": "approval records are written only by the server",
    "import-quarantine": "the import quarantine is written only by the fetch step",
}

# durin's own configuration and secret stores are never behind a write door at
# all — the person changes them via the dashboard or `durin config`, never the
# agent's file tools. Keyed by basename, same as _NO_DOOR_REASONS above; kept
# separate because these paths live under DURIN_HOME, almost never inside the
# workspace, so the ordinary allowed-directory containment check never even
# sees them (that's exactly how a write here would slip past the guard).
_DURIN_STORE_REASON = (
    "durin's configuration is changed by the person (dashboard, `durin config`), "
    "not by the agent's file tools"
)


def protected_durin_store_paths() -> list[Path]:
    """Absolute paths to durin's own config/secret/pairing stores, wherever
    DURIN_HOME actually is (``$DURIN_HOME``, or ``~/.durin`` by default; a
    test or an override via ``set_config_path`` points it elsewhere).

    Mirrors each store's own default-path derivation instead of guessing a
    layout: ``durin.config.loader.get_config_path()`` for the config file (and
    its ``.d/`` split-layout directory alongside it), and
    ``durin.security.secrets``/``durin.security.api_tokens``/
    ``durin.pairing.store``, whose stores all default to a sibling file in
    the same directory as the config file.

    Nothing is created here: ``is_under`` compares a path that does not
    exist yet by its nearest existing ancestor. An empty ``config.json.d/``
    is not inert — its mere presence makes the config readers treat a
    single-file config as split.
    """
    from durin.config.loader import get_config_path

    config_path = get_config_path()
    data_dir = config_path.parent
    split_dir = config_path.with_suffix(config_path.suffix + ".d")
    return [
        config_path,
        split_dir,  # config.json.d/
        data_dir / "secrets.json",
        data_dir / "api_tokens.json",
        data_dir / "pairing.json",
    ]


def _same_entry(a: Path, b: Path) -> bool:
    """True when *a* and *b* name the same filesystem entry (inode), not
    merely the same text. Tries ``os.path.samefile`` first — the standard
    identity check, and the one a test can monkeypatch to simulate a
    case-insensitive filesystem on a case-sensitive CI runner — then falls
    back to a raw ``(st_dev, st_ino)`` comparison for a platform/situation
    where ``samefile`` itself misbehaves. Either side missing (can't be
    stat'd) means no identity claim can be made: not the same entry.
    """
    try:
        if os.path.samefile(a, b):
            return True
    except OSError:
        pass
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _existing_prefix(path: Path) -> tuple[Path, tuple[str, ...]]:
    """Split *path* into its nearest existing ancestor (symlinks resolved)
    and the components below it that do not exist yet."""
    path = Path(os.path.realpath(path))
    rest: list[str] = []
    while not os.path.exists(path) and path != path.parent:
        rest.append(path.name)
        path = path.parent
    return path, tuple(reversed(rest))


def _fs_case_insensitive(path: Path) -> bool:
    """True when the filesystem at *path* folds case.

    Swaps the case of an existing entry's name and checks whether the
    swapped spelling resolves to the same entry. Starts at *path*'s nearest
    existing ancestor and walks up to the first one whose name has letters
    to swap: a directory named ``12345`` teaches nothing on its own, and
    stopping there answered "case-sensitive" on a volume that folds case.
    The lookup of a name happens in its parent directory, so this reads the
    volume of that parent — the same volume unless a mount point sits
    exactly there. No lettered ancestor at all: treated as case-sensitive.
    """
    current, _ = _existing_prefix(path)
    while current != current.parent:
        swapped = current.name.swapcase()
        if swapped != current.name:
            return _same_entry(current.parent / swapped, current)
        current = current.parent
    return False


def _names_match(names: tuple[str, ...], expected: tuple[str, ...], fold: bool) -> bool:
    if len(names) < len(expected):
        return False
    if fold:
        return all(a.casefold() == b.casefold() for a, b in zip(names, expected))
    return names[:len(expected)] == expected


def is_under(path: Path, directory: Path) -> bool:
    """True when *path* is *directory* or lies under it — by FILESYSTEM
    IDENTITY, not path text, so a case variant of any segment
    (``CONFIG.JSON``, ``Config.json.d``, ``.APPROVALS``) cannot slip past a
    guard written against the canonical spelling on a case-insensitive
    filesystem (macOS APFS by default, most Windows volumes).

    Either path may not exist yet (a fresh workspace has no ``.approvals/``,
    a fresh instance no ``config.json``), so each is split into its nearest
    existing ancestor plus the components that do not exist. *path* is
    under *directory* when some existing ancestor of *path* is the same
    entry as *directory*'s existing ancestor (``_same_entry``), and the
    components of *path* below that point begin with *directory*'s missing
    components — compared by name, casefolded only when that filesystem
    folds case (on a case-sensitive one ``.APPROVALS`` and ``.approvals``
    are two unrelated directories). Symlinks are resolved on both sides.
    """
    t_base, t_rest = _existing_prefix(path)
    g_base, g_rest = _existing_prefix(directory)
    fold: bool | None = None
    for ancestor in (t_base, *t_base.parents):
        if not _same_entry(ancestor, g_base):
            continue
        if not g_rest:
            return True
        if fold is None:
            fold = _fs_case_insensitive(g_base)
        below = t_base.relative_to(ancestor).parts + t_rest
        if _names_match(below, g_rest, fold):
            return True
    return False


def resolve_workspace_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
    work_dir: Path | None = None,
    denied_subdirs: list[Path] | None = None,
    deny_durin_stores: bool = False,
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

    ``deny_durin_stores``, when set, refuses a path under any of
    ``protected_durin_store_paths()`` — checked BEFORE the allowed-directory
    containment above, and regardless of it: DURIN_HOME is almost never inside
    the workspace, so a caller with no ``allowed_dir`` (``restrict_to_workspace``
    off, the common case) would otherwise resolve straight through to durin's
    own config/secrets with no check at all. Write-only (the write tools pass
    this; reads do not), so the agent can still read config.json for diagnosis.
    """
    from durin.agent.tools.work_area import anchored_base

    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        first = p.parts[0] if p.parts else ""
        base = anchored_base(first, workspace, work_dir)
        p = base / p
    resolved = p.resolve()
    if deny_durin_stores:
        for store_path in protected_durin_store_paths():
            if is_under(resolved, store_path):
                raise PermissionError(
                    f"Path {path} is under {store_path.name}, which the agent cannot "
                    f"write: {_DURIN_STORE_REASON}." + WORKSPACE_BOUNDARY_NOTE
                )
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
            door = _REGISTRY_DOORS.get(denied.name)
            if door:
                raise PermissionError(
                    f"Path {path} is under the protected {denied.name} registry, which owns "
                    f"its own validated + versioned write door: {door}."
                    + WORKSPACE_BOUNDARY_NOTE
                )
            reason = _NO_DOOR_REASONS.get(denied.name, "it is not writable by the agent")
            raise PermissionError(
                f"Path {path} is under {denied.name}, which the agent cannot write: "
                f"{reason}." + WORKSPACE_BOUNDARY_NOTE
            )
    return resolved
