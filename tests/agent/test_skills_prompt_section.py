"""What the system prompt says about skills: every built-in has a description
to show, and the guidance above the list matches the tool and the layout."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from durin.agent.context import ContextBuilder
from durin.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader
from durin.agent.skills_frontmatter import split_frontmatter
from durin.agent.tools.memory_search import MemorySearchTool
from durin.utils.prompt_templates import render_template

_BUILTIN_NAMES = sorted(p.parent.name for p in BUILTIN_SKILLS_DIR.glob("*/SKILL.md"))


def test_builtin_skills_are_shipped():
    assert _BUILTIN_NAMES


@pytest.mark.parametrize("name", _BUILTIN_NAMES)
def test_builtin_skill_has_a_description_for_the_prompt_catalog(tmp_path, name):
    """The catalog row of a skill whose frontmatter does not parse falls back
    to the bare name, so the model never learns what the skill is for."""
    metadata = SkillsLoader(tmp_path).get_skill_metadata(name)

    assert metadata is not None, f"{name}: frontmatter does not parse"
    description = metadata.get("description")
    assert isinstance(description, str) and description.strip(), name


@pytest.mark.parametrize("name", _BUILTIN_NAMES)
def test_builtin_skill_has_a_description_for_the_store_reader(name):
    """The skill store reads frontmatter through its own parser; it must find
    the same description the loader does."""
    text = (BUILTIN_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")

    data, _body = split_frontmatter(text)

    description = data.get("description")
    assert isinstance(description, str) and description.strip(), name


@pytest.mark.parametrize("name", _BUILTIN_NAMES)
def test_builtin_skill_row_in_the_prompt_carries_its_description(tmp_path, name):
    loader = SkillsLoader(tmp_path)
    description = loader.get_skill_metadata(name)["description"].strip()

    rows = [
        line for line in loader.build_skills_summary().splitlines()
        if line.startswith(f"- **{name}**")
    ]

    assert len(rows) == 1
    assert description in rows[0]


_CODE_SPAN = re.compile(r"`([^`]+)`")
_CALL = re.compile(r"^([A-Za-z_]\w*)\(")
_KEYWORD = re.compile(r"\b([A-Za-z_]\w*)=")


def _keywords_used_with(tool_name: str, guidance: str) -> set[str]:
    """Keyword-argument names the guidance writes in code where it talks about
    ``tool_name``: ``name=value`` inside the code spans of each paragraph that
    mentions the tool, leaving out calls written for a different tool."""
    names: set[str] = set()
    for paragraph in guidance.split("\n\n"):
        if tool_name not in paragraph:
            continue
        for span in _CODE_SPAN.findall(paragraph):
            call = _CALL.match(span)
            if call and call.group(1) != tool_name:
                continue
            names.update(_KEYWORD.findall(span))
    return names


def _guidance() -> str:
    """The skills section as the prompt builder renders it, without the list."""
    return render_template("agent/skills_section.md", skills_summary="")


def test_skills_guidance_is_what_the_system_prompt_carries(tmp_path: Path):
    prompt = ContextBuilder(tmp_path).build_system_prompt()

    for paragraph in _guidance().strip().split("\n\n"):
        assert paragraph in prompt


def test_skills_guidance_names_only_real_memory_search_parameters(tmp_path: Path):
    parameters = set(MemorySearchTool(workspace=tmp_path).parameters["properties"])

    used = _keywords_used_with("memory_search", _guidance())

    assert used, "the guidance no longer shows how to search for skills"
    assert used <= parameters, f"not memory_search parameters: {sorted(used - parameters)}"


def test_skills_guidance_does_not_point_above_itself_at_the_list():
    """The list is rendered after the guidance, so nothing in the guidance can
    refer to the listed skills as being above it."""
    marker = "- **probe** — a probe skill"
    section = render_template("agent/skills_section.md", skills_summary=marker)

    guidance, found, after = section.partition(marker)

    assert found and after.strip() == ""
    assert not re.search(r"\babove\b", guidance, flags=re.IGNORECASE)
