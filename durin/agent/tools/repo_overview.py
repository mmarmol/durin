"""Repository overview tool — Sprint A / T1.

Returns a depth-bounded structure tree of a workspace plus detected ecosystem
(Python, Node, Go, Rust, Ruby, Java/Kotlin, PHP), package manager (npm/pnpm/
yarn/bun), dependency files, and common entrypoints. NO embeddings, NO PageRank,
NO AST — purely structural. Lets the model orient before diving in.

Adapted from OpenCode's `repo_overview` tool (see
`docs/architecture/loop.md` §1). Adjustments vs OpenCode:
- Local path only (no git URL caching — Durin works in the active workspace)
- Reuses `_FsTool._IGNORE_DIRS` pattern from filesystem.py for noise filtering
- Emits `tool.repo_overview` telemetry via the inherited `_emit` helper
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from durin.agent.tools.base import tool_parameters
from durin.agent.tools.filesystem import ListDirTool, _FsTool
from durin.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

_STRUCTURE_LIMIT = 200
_MAX_DEPTH = 6
_DEFAULT_DEPTH = 3

_DEPENDENCY_FILES = (
    "package.json",
    "package-lock.json",
    "bun.lock",
    "bun.lockb",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "composer.json",
)

_COMMON_ENTRYPOINTS = (
    "index.ts",
    "index.tsx",
    "index.js",
    "index.mjs",
    "main.ts",
    "main.js",
    "main.py",
    "__main__.py",
    "app.py",
    "src/index.ts",
    "src/index.tsx",
    "src/index.js",
    "src/main.ts",
    "src/main.js",
    "src/main.py",
    "cmd/main.go",
    "src/lib.rs",
    "src/main.rs",
)


def _package_manager(files: set[str]) -> str | None:
    """Pick the most likely package manager from lockfile presence."""
    if "bun.lock" in files or "bun.lockb" in files:
        return "bun"
    if "pnpm-lock.yaml" in files:
        return "pnpm"
    if "yarn.lock" in files:
        return "yarn"
    if "package-lock.json" in files:
        return "npm"
    if "poetry.lock" in files:
        return "poetry"
    if "Pipfile.lock" in files:
        return "pipenv"
    if "Cargo.lock" in files:
        return "cargo"
    if "Gemfile.lock" in files:
        return "bundler"
    return None


def _ecosystems(files: set[str]) -> list[str]:
    """Detect ecosystems from sentinel files at the root."""
    out: list[str] = []
    if "package.json" in files:
        out.append("Node.js")
    if "pyproject.toml" in files or "requirements.txt" in files or "Pipfile" in files:
        out.append("Python")
    if "go.mod" in files:
        out.append("Go")
    if "Cargo.toml" in files:
        out.append("Rust")
    if "Gemfile" in files:
        out.append("Ruby")
    if "build.gradle" in files or "build.gradle.kts" in files or "pom.xml" in files:
        out.append("Java/Kotlin")
    if "composer.json" in files:
        out.append("PHP")
    return out


def _find_entrypoints(root: Path) -> list[str]:
    """Return any common entrypoint paths that actually exist."""
    found: list[str] = []
    for rel in _COMMON_ENTRYPOINTS:
        if (root / rel).is_file():
            found.append(rel)
    return found


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema(
            "Directory to inspect (default: workspace root). Relative paths "
            "are resolved against the workspace.",
        ),
        depth=IntegerSchema(
            _DEFAULT_DEPTH,
            description=(
                f"Maximum structure depth (1-{_MAX_DEPTH}, default {_DEFAULT_DEPTH}). "
                f"Total entries capped at {_STRUCTURE_LIMIT}."
            ),
            minimum=1,
            maximum=_MAX_DEPTH,
        ),
    )
)
class RepoOverviewTool(_FsTool):
    """One-shot orientation: tree + ecosystem + package manager + entrypoints."""

    _scopes = {"core", "subagent"}
    _IGNORE_DIRS = set(ListDirTool._IGNORE_DIRS)

    @property
    def name(self) -> str:
        return "repo_overview"

    @property
    def description(self) -> str:
        return (
            "Orient yourself in a codebase with one call. Returns a depth-"
            f"bounded directory tree (default depth {_DEFAULT_DEPTH}, capped at "
            f"{_STRUCTURE_LIMIT} entries) plus detected ecosystems (Python, "
            "Node.js, Go, Rust, etc.), package manager, dependency files, "
            "and common entrypoints. Use this before diving in with read_file "
            "or grep when you don't already know the layout. Noise directories "
            "(.git, node_modules, __pycache__, .venv, dist, build, etc.) are "
            "skipped."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        path: str | None = None,
        depth: int | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            target = self._resolve(path or ".")
            if not target.exists():
                return self._file_not_found_msg(path or ".", target)
            if not target.is_dir():
                return f"Error: Not a directory: {path}"

            actual_depth = depth if (depth and 1 <= depth <= _MAX_DEPTH) else _DEFAULT_DEPTH

            # Top-level file scan for ecosystem detection.
            try:
                top_files = {
                    entry.name
                    for entry in target.iterdir()
                    if entry.is_file()
                }
            except OSError:
                top_files = set()

            ecos = _ecosystems(top_files)
            pkg_mgr = _package_manager(top_files)
            dep_files = sorted(f for f in _DEPENDENCY_FILES if f in top_files)
            entrypoints = _find_entrypoints(target)

            lines, truncated = self._build_tree(target, actual_depth)

            out: list[str] = []
            display = self._display_path(target) or "."
            out.append(f"# Repository overview: {display}")
            out.append("")
            if ecos:
                out.append(f"Ecosystems: {', '.join(ecos)}")
            if pkg_mgr:
                out.append(f"Package manager: {pkg_mgr}")
            if dep_files:
                out.append(f"Dependency files: {', '.join(dep_files)}")
            if entrypoints:
                out.append(f"Entrypoints: {', '.join(entrypoints)}")
            if ecos or pkg_mgr or dep_files or entrypoints:
                out.append("")

            out.append(f"## Structure (depth={actual_depth})")
            out.append("")
            out.extend(lines)
            if truncated:
                out.append("")
                out.append(
                    f"(structure truncated at {_STRUCTURE_LIMIT} entries — "
                    "increase depth or inspect subdirectories with list_dir)"
                )

            result = "\n".join(out)

            self._emit("tool.repo_overview", {
                "path": display,
                "depth": actual_depth,
                "ecosystems": ecos,
                "package_manager": pkg_mgr,
                "dependency_files_count": len(dep_files),
                "entrypoints_count": len(entrypoints),
                "structure_lines": len(lines),
                "truncated": truncated,
                "result_chars": len(result),
            })
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error generating repo overview: {e}"

    def _build_tree(self, root: Path, max_depth: int) -> tuple[list[str], bool]:
        """Walk *root* depth-first, returning indented lines + truncated flag.

        Sorts entries directory-first then alphabetically. Skips entries in
        ``_IGNORE_DIRS``. Stops once total line count hits ``_STRUCTURE_LIMIT``.
        """
        lines: list[str] = []

        def visit(dir_path: Path, level: int) -> bool:
            """Returns True if we hit the cap during this visit (so caller bails)."""
            if level >= max_depth:
                return False
            if len(lines) >= _STRUCTURE_LIMIT:
                return True
            try:
                with os.scandir(dir_path) as it:
                    entries = list(it)
            except OSError:
                return False
            entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            for entry in entries:
                if entry.name in self._IGNORE_DIRS:
                    continue
                if len(lines) >= _STRUCTURE_LIMIT:
                    return True
                is_dir = entry.is_dir(follow_symlinks=False)
                indent = "  " * level
                lines.append(f"{indent}{entry.name}{'/' if is_dir else ''}")
                if is_dir:
                    if visit(Path(entry.path), level + 1):
                        return True
            return False

        truncated = visit(root, 0)
        return lines, truncated
