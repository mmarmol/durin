"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

_PLATFORM_ALIASES = {
    "darwin": "macos", "macos": "macos", "osx": "macos", "mac": "macos",
    "linux": "linux",
    "win32": "windows", "windows": "windows", "win": "windows",
}


def _current_platform() -> str:
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    if p.startswith("win"):
        return "windows"
    return p

# Opening ---, YAML body (group 1), closing --- on its own line; supports CRLF.
_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None, disabled_skills: set[str] | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.disabled_skills = disabled_skills or set()

    def _skill_entries_from_dir(self, base: Path, source: str, *, skip_names: set[str] | None = None) -> list[dict[str, str]]:
        if not base.exists():
            return []
        entries: list[dict[str, str]] = []
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            name = skill_dir.name
            if skip_names is not None and name in skip_names:
                continue
            entries.append({"name": name, "path": str(skill_file), "source": source})
        return entries

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = self._skill_entries_from_dir(self.workspace_skills, "workspace")
        workspace_names = {entry["name"] for entry in skills}
        if self.builtin_skills and self.builtin_skills.exists():
            skills.extend(
                self._skill_entries_from_dir(self.builtin_skills, "builtin", skip_names=workspace_names)
            )

        if self.disabled_skills:
            skills = [s for s in skills if s["name"] not in self.disabled_skills]

        skills = [s for s in skills if self._platform_ok(s["name"])]

        if filter_unavailable:
            return [skill for skill in skills if self._check_requirements(self._get_skill_meta(skill["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        roots = [self.workspace_skills]
        if self.builtin_skills:
            roots.append(self.builtin_skills)
        for root in roots:
            path = root / name / "SKILL.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    def skill_dir(self, name: str) -> Path | None:
        """Resolve a skill's directory (workspace shadows builtin); None if absent."""
        for root in (self.workspace_skills, self.builtin_skills):
            if root and (root / name / "SKILL.md").is_file():
                return root / name
        return None

    def _linked_files(self, skill_dir: Path) -> dict[str, list[str]]:
        """Map a skill's bundled files by kind (references/scripts/templates/assets)
        to paths relative to the skill dir — the progressive-disclosure handle for
        multi-file skills. Empty kinds are omitted."""
        out: dict[str, list[str]] = {}
        for sub in ("references", "scripts", "templates", "assets"):
            d = skill_dir / sub
            if not d.is_dir():
                continue
            files = []
            for p in d.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(skill_dir)
                # Skip compiled-cache and hidden noise — not authored bundle files.
                if "__pycache__" in rel.parts or p.suffix in (".pyc", ".pyo"):
                    continue
                files.append(str(rel))
            if files:
                out[sub] = sorted(files)
        return out

    def view_skill(self, name: str, file_path: str | None = None) -> dict | None:
        """Assemble the ``skill_view`` payload for a skill: its body (frontmatter
        stripped), a map of bundled files, and readiness (missing bins/env routed
        to the tools that resolve them). With ``file_path``, returns one bundled
        sub-file instead, traversal-guarded.

        Returns None when the skill does not exist; a dict carrying an ``error``
        key when ``file_path`` is outside the skill or not a file.
        """
        skill_dir = self.skill_dir(name)
        if skill_dir is None:
            return None
        if file_path:
            base = skill_dir.resolve()
            target = (skill_dir / file_path).resolve()
            if base not in target.parents or not target.is_file():
                return {"name": name, "error": f"No bundled file '{file_path}' in skill '{name}'."}
            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return {"name": name, "error": f"'{file_path}' is not a UTF-8 text file; run it with exec instead of reading it."}
            return {"name": name, "file": file_path, "content": content}

        content = self.load_skill(name) or ""
        meta = self._get_skill_meta(name)
        requires = meta.get("requires", {}) if isinstance(meta, dict) else {}
        missing_bins = [b for b in (requires.get("bins") or []) if not shutil.which(b)]
        missing_env = [e for e in (requires.get("env") or []) if not os.environ.get(e)]
        readiness: dict[str, object] = {"ready": not (missing_bins or missing_env)}
        if missing_bins:
            readiness["missing_bins"] = missing_bins
            readiness["install_hint"] = (
                "Install the missing CLIs with the skill_install_deps tool before "
                "running this skill's scripts."
            )
        if missing_env:
            readiness["missing_env"] = missing_env
            readiness["secret_hint"] = (
                "Provide the missing environment variables with the request_secret tool."
            )
        payload: dict[str, object] = {
            "name": name,
            "skill_dir": str(skill_dir),
            "content": self._strip_frontmatter(content),
            "readiness": readiness,
        }
        linked = self._linked_files(skill_dir)
        if linked:
            payload["linked_files"] = linked
            payload["usage_hint"] = (
                "To read a bundled file, call skill_view again with file_path set to "
                "one of the paths above. Run a script via the exec tool using skill_dir."
            )
        return payload

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = [
            f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}"
            for name in skill_names
            if (markdown := self.load_skill(name))
        ]
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(
        self,
        exclude: set[str] | None = None,
        include: set[str] | None = None,
    ) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Skills whose frontmatter declares ``disable_model_invocation: true``
        (or the camelCase variant ``disableModelInvocation: true`` for
        pi-compat) are filtered out of this summary so they never appear
        in the model's tool/skill list. They remain loadable
        programmatically via ``load_skill(name)`` — useful for internal
        utilities, debugging skills, or skills the agent should only run
        when invoked by code, not by the LLM choosing them itself.

        Args:
            exclude: Set of skill names to omit from the summary.
            include: When provided, only these names appear — used by the
                hot working-set tier. ``exclude`` still wins (a name in both
                is skipped).

        Returns:
            Markdown-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        lines: list[str] = []
        for entry in all_skills:
            skill_name = entry["name"]
            if exclude and skill_name in exclude:
                continue
            if include is not None and skill_name not in include:
                continue
            # ``_get_skill_meta`` returns the nested durin-specific blob
            # (under ``metadata.durin``); the disable flag lives at the
            # top level of the YAML frontmatter, so we fetch that
            # separately. Two metadata reads is cheap — the underlying
            # ``load_skill`` already memoizes the file read.
            top_level_meta = self.get_skill_metadata(skill_name) or {}
            if self._is_model_invocation_disabled(top_level_meta):
                continue
            meta = self._get_skill_meta(skill_name)
            available = self._check_requirements(meta)
            desc = self._get_skill_description(skill_name)
            if available:
                lines.append(f"- **{skill_name}** — {desc}  `{entry['path']}`")
            else:
                missing = self._get_missing_requirements(meta)
                suffix = f" (unavailable: {missing})" if missing else " (unavailable)"
                lines.append(f"- **{skill_name}** — {desc}{suffix}  `{entry['path']}`")
        return "\n".join(lines)

    @staticmethod
    def _is_model_invocation_disabled(skill_meta: dict | None) -> bool:
        """Return True when the skill opts out of being shown to the model.

        Accepts ``disable_model_invocation`` (snake_case, durin convention)
        or ``disableModelInvocation`` (camelCase, pi convention) — either
        spelling works in user-written frontmatter. Truthiness is the
        Python-standard check, so ``true``, ``"true"``, and ``1`` all
        disable. Defaults to False (skill visible).
        """
        if not skill_meta:
            return False
        return bool(
            skill_meta.get("disable_model_invocation")
            or skill_meta.get("disableModelInvocation")
            or skill_meta.get("disable-model-invocation")
        )

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return ", ".join(
            [f"CLI: {command_name}" for command_name in required_bins if not shutil.which(command_name)]
            + [f"ENV: {env_name}" for env_name in required_env_vars if not os.environ.get(env_name)]
        )

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if not content.startswith("---"):
            return content
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if match:
            return content[match.end():].strip()
        return content

    def _parse_durin_metadata(self, raw: object) -> dict:
        """Extract durin/openclaw metadata from a frontmatter field.

        ``raw`` may be a dict (already parsed by yaml.safe_load) or a JSON str.
        """
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        payload = data.get("durin", data.get("openclaw", {}))
        return payload if isinstance(payload, dict) else {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return all(shutil.which(cmd) for cmd in required_bins) and all(
            os.environ.get(var) for var in required_env_vars
        )

    def _platform_ok(self, name: str) -> bool:
        """Honor the agentskills.io root ``platforms`` field. No field = all
        platforms. Accepts standard (macos/linux/windows) + common aliases
        (darwin/win32)."""
        meta = self.get_skill_metadata(name) or {}
        plats = meta.get("platforms")
        if not plats:
            return True
        if isinstance(plats, str):
            plats = [plats]
        normalized = {
            _PLATFORM_ALIASES.get(str(p).lower().strip(), str(p).lower().strip())
            for p in plats
        }
        return _current_platform() in normalized

    def _get_skill_meta(self, name: str) -> dict:
        """Get durin metadata for a skill (cached in frontmatter)."""
        raw_meta = self.get_skill_metadata(name) or {}
        return self._parse_durin_metadata(raw_meta.get("metadata"))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        return [
            entry["name"]
            for entry in self.list_skills(filter_unavailable=True)
            if (meta := self.get_skill_metadata(entry["name"]) or {})
            and (
                self._parse_durin_metadata(meta.get("metadata")).get("always")
                or meta.get("always")
            )
        ]

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content or not content.startswith("---"):
            return None
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if not match:
            return None
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(parsed, dict):
            return None
        # yaml.safe_load returns native types (int, bool, list, etc.);
        # keep values as-is so downstream consumers get correct types.
        metadata: dict[str, object] = {}
        for key, value in parsed.items():
            metadata[str(key)] = value
        return metadata
