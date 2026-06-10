#!/usr/bin/env python3
"""
Minimal validator for durin skill folders.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

MAX_SKILL_NAME_LENGTH = 64
ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "metadata",
    "always",
    "license",
    "allowed-tools",
}
ALLOWED_RESOURCE_DIRS = {"scripts", "references", "assets"}
PLACEHOLDER_MARKERS = ("[todo", "todo:")
RESOURCE_LINK_RE = re.compile(r"\[[^\]]*\]\(((?:references|scripts|assets)/[^)#?]+)\)")


def _extract_frontmatter(content: str) -> Optional[str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _parse_simple_frontmatter(frontmatter_text: str) -> Optional[dict[str, str]]:
    """Fallback parser for simple frontmatter when PyYAML is unavailable."""
    parsed: dict[str, str] = {}
    current_key: Optional[str] = None
    multiline_key: Optional[str] = None

    for raw_line in frontmatter_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        is_indented = raw_line[:1].isspace()
        if is_indented:
            if current_key is None:
                return None
            current_value = parsed[current_key]
            parsed[current_key] = f"{current_value}\n{stripped}" if current_value else stripped
            continue

        if ":" not in stripped:
            return None

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            return None

        if value in {"|", ">"}:
            parsed[key] = ""
            current_key = key
            multiline_key = key
            continue

        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        parsed[key] = value
        current_key = key
        multiline_key = None

    if multiline_key is not None and multiline_key not in parsed:
        return None
    return parsed


def _load_frontmatter(frontmatter_text: str) -> tuple[Optional[dict], Optional[str]]:
    if yaml is not None:
        try:
            frontmatter = yaml.safe_load(frontmatter_text)
        except yaml.YAMLError as exc:
            return None, f"Invalid YAML in frontmatter: {exc}"
        if not isinstance(frontmatter, dict):
            return None, "Frontmatter must be a YAML dictionary"
        return frontmatter, None

    frontmatter = _parse_simple_frontmatter(frontmatter_text)
    if frontmatter is None:
        return None, "Invalid YAML in frontmatter: unsupported syntax without PyYAML installed"
    return frontmatter, None


def _validate_skill_name(name: str, folder_name: str) -> Optional[str]:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        return (
            f"Name '{name}' should be hyphen-case "
            "(lowercase letters, digits, and single hyphens only)"
        )
    if len(name) > MAX_SKILL_NAME_LENGTH:
        return (
            f"Name is too long ({len(name)} characters). "
            f"Maximum is {MAX_SKILL_NAME_LENGTH} characters."
        )
    if name != folder_name:
        return f"Skill name '{name}' must match directory name '{folder_name}'"
    return None


def _validate_description(description: str) -> Optional[str]:
    trimmed = description.strip()
    if not trimmed:
        return "Description cannot be empty"
    lowered = trimmed.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return "Description still contains TODO placeholder text"
    if "<" in trimmed or ">" in trimmed:
        return "Description cannot contain angle brackets (< or >)"
    if len(trimmed) > 1024:
        return f"Description is too long ({len(trimmed)} characters). Maximum is 1024 characters."
    return None


def validate_skill(skill_path):
    """Validate a skill folder. Returns (valid, message).

    valid is False when any ERROR-level issue exists. WARNING-level issues
    are reported in the message but do not fail validation.
    """
    skill_path = Path(skill_path).resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not skill_path.exists():
        return False, f"Skill folder not found: {skill_path}"
    if not skill_path.is_dir():
        return False, f"Path is not a directory: {skill_path}"

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"Could not read SKILL.md: {exc}"

    frontmatter_text = _extract_frontmatter(content)
    if frontmatter_text is None:
        return False, "Invalid frontmatter format"

    frontmatter, error = _load_frontmatter(frontmatter_text)
    if error:
        return False, error

    # From here on, collect every issue instead of returning at the first.
    unexpected_keys = sorted(set(frontmatter.keys()) - ALLOWED_FRONTMATTER_KEYS)
    if unexpected_keys:
        allowed = ", ".join(sorted(ALLOWED_FRONTMATTER_KEYS))
        unexpected = ", ".join(unexpected_keys)
        errors.append(
            f"Unexpected key(s) in SKILL.md frontmatter: {unexpected}. Allowed properties are: {allowed}"
        )

    if "name" not in frontmatter:
        errors.append("Missing 'name' in frontmatter")
    else:
        name = frontmatter["name"]
        if not isinstance(name, str):
            errors.append(f"Name must be a string, got {type(name).__name__}")
        else:
            name_error = _validate_skill_name(name.strip(), skill_path.name)
            if name_error:
                errors.append(name_error)

    if "description" not in frontmatter:
        errors.append("Missing 'description' in frontmatter")
    else:
        description = frontmatter["description"]
        if not isinstance(description, str):
            errors.append(f"Description must be a string, got {type(description).__name__}")
        else:
            description_error = _validate_description(description)
            if description_error:
                errors.append(description_error)

    always = frontmatter.get("always")
    if always is not None and not isinstance(always, bool):
        errors.append(f"'always' must be a boolean, got {type(always).__name__}")

    for child in skill_path.iterdir():
        if child.name == "SKILL.md":
            continue
        if child.is_dir() and child.name in ALLOWED_RESOURCE_DIRS:
            continue
        if child.is_symlink():
            continue
        errors.append(
            f"Unexpected file or directory in skill root: {child.name}. "
            "Only SKILL.md, scripts/, references/, and assets/ are allowed."
        )

    body = content.split("---", 2)[2] if content.count("---") >= 2 else content
    _check_resource_links(skill_path, body, errors)
    _check_orphan_resources(skill_path, body, warnings)
    _check_script_syntax(skill_path, errors)

    return _build_result(errors, warnings)


def _check_script_syntax(skill_path: Path, errors: list[str]) -> None:
    """Syntax-check bundled scripts WITHOUT executing them — the validator may
    run over untrusted skills."""
    scripts_dir = skill_path / "scripts"
    if not scripts_dir.is_dir():
        return
    for script in sorted(scripts_dir.rglob("*")):
        if not script.is_file() or "__pycache__" in script.parts:
            continue
        if script.suffix == ".py":
            try:
                compile(script.read_text(encoding="utf-8"), str(script), "exec")
            except (OSError, SyntaxError, ValueError) as exc:
                errors.append(f"Script has syntax errors: {script.name} ({exc})")
        elif script.suffix == ".sh":
            bash = shutil.which("bash")
            if bash is None:
                continue
            proc = subprocess.run(
                [bash, "-n", str(script)], capture_output=True, text=True, timeout=10
            )
            if proc.returncode != 0:
                stderr = proc.stderr.strip()
                detail = stderr.splitlines()[-1] if stderr else "bash -n failed"
                errors.append(f"Script has syntax errors: {script.name} ({detail})")


def _check_orphan_resources(skill_path: Path, body: str, warnings: list[str]) -> None:
    """references/ and scripts/ files the body never mentions are
    undiscoverable by the agent. assets/ is exempt (used in output, not read)."""
    for sub in ("references", "scripts"):
        directory = skill_path / sub
        if not directory.is_dir():
            continue
        for resource in sorted(directory.rglob("*")):
            if not resource.is_file() or "__pycache__" in resource.parts:
                continue
            rel = resource.relative_to(skill_path).as_posix()
            if rel not in body:
                warnings.append(f"Resource never mentioned in SKILL.md: {rel}")


def _check_resource_links(skill_path: Path, body: str, errors: list[str]) -> None:
    """Markdown links into resource dirs must resolve; inline-code paths are
    illustrative by convention and are not checked."""
    for match in RESOURCE_LINK_RE.finditer(body):
        rel = match.group(1).strip()
        if not (skill_path / rel).exists():
            errors.append(f"Linked resource does not exist: {rel}")


def _build_result(errors: list[str], warnings: list[str]) -> tuple[bool, str]:
    lines = [f"ERROR: {item}" for item in errors]
    lines.extend(f"WARNING: {item}" for item in warnings)
    if not lines:
        return True, "Skill is valid!"
    return not errors, "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)
