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
    """
    from durin.config.loader import get_config_path

    config_path = get_config_path()
    data_dir = config_path.parent
    return [
        config_path,
        config_path.with_suffix(config_path.suffix + ".d"),  # config.json.d/
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


def is_under(path: Path, directory: Path) -> bool:
    """True when *path* resolves under *directory* — by FILESYSTEM IDENTITY,
    not path text, so a case variant of any segment (``CONFIG.JSON``,
    ``Config.json.d``, ``.DURIN``) cannot slip past a guard written against
    the canonical spelling: on a case-insensitive filesystem (macOS APFS by
    default, most Windows volumes) the OS itself treats those as the exact
    same entry as the real one, so a plain text/``relative_to`` comparison
    (which durin used to rely on here) never even sees the collision.

    Walks every ancestor of *path* that already exists on disk (*path*
    itself included, when it exists) and checks each against *directory*
    for identity. When *path* does not exist at all yet — the target is a
    brand-new file under a GUARDED FILE's own name (``config.json`` itself,
    not a directory it lives under) — no ancestor walk can "see" it to stat
    it, so it is instead proven identical by comparing the parent directory
    (by identity) plus the basename (casefolded, since that's exactly what a
    case-insensitive filesystem does when it eventually creates the file).

``directory`` not existing on disk at all is handled two ways: a not-yet-
    created ordinary directory (a workspace's own session work dir, or a
    registry directory nothing has written into yet) has no reserved name to
    defend, so it degrades to the previous, purely textual containment
    check; a guarded FILE (``config.json`` and friends) is a small, fixed,
    always-reserved set of names, so the parent+casefold comparison below
    still applies even before durin has ever written it (a fresh instance).

    The casefold comparison does not itself check whether this filesystem is
    actually case-insensitive — it can't, portably and reliably, for every
    filesystem/mount combination durin might run on — so it treats a
    same-directory, casefold-matching name as a match unconditionally. On an
    ordinary case-SENSITIVE filesystem this can refuse an unrelated file that
    merely differs from a guarded name by case alone; that one-in-a-million
    false refusal (the model picks a different name and moves on) is the
    trade this guard deliberately takes over ever silently missing a real
    collision on a case-insensitive one.
    """
    directory = directory.resolve()
    for ancestor in (path, *path.parents):
        if ancestor.exists() and directory.exists() and _same_entry(ancestor, directory):
            return True
    if not path.exists():
        parent, gparent = path.parent, directory.parent
        if (parent.exists() and gparent.exists() and _same_entry(parent, gparent)
                and path.name.casefold() == directory.name.casefold()):
            return True
    if not directory.exists():
        try:
            path.relative_to(directory)
            return True
        except ValueError:
            return False
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
