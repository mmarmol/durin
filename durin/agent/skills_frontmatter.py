"""Read/write helpers for SKILL.md YAML frontmatter (durin namespace)."""
from __future__ import annotations

import re

import yaml

# Opening ---, YAML body (group 1), closing --- on its own line; supports CRLF.
_FM_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Empty dict if no frontmatter."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):
        data = {}
    return data, text[m.end():]


def frontmatter_broken(text: str) -> bool:
    """True when a frontmatter block is present but is not a parseable YAML
    mapping (e.g. an unquoted ": " inside a plain multi-line description)."""
    m = _FM_RE.match(text)
    if not m:
        return False
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return True
    return not isinstance(data, dict)


def recover_metadata(text: str) -> dict:
    """Best-effort read of the top-level ``metadata:`` block when the full
    frontmatter fails to parse.

    Hand-written fields (name, description) are where YAML typos happen; the
    ``metadata.durin`` blob is machine-written and parses on its own. Broken
    prose above it must not erase the skill's provenance/mode — so isolate the
    lines from ``metadata:`` up to the next top-level key and parse just those.
    Returns the metadata mapping, or {} when absent/unrecoverable."""
    m = _FM_RE.match(text)
    if not m:
        return {}
    lines = m.group(1).splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.rstrip() == "metadata:"), None)
    if start is None:
        return {}
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line and not line[0].isspace():
            break  # next top-level key
        block.append(line)
    try:
        data = yaml.safe_load("\n".join(block))
    except yaml.YAMLError:
        return {}
    meta = data.get("metadata") if isinstance(data, dict) else None
    return meta if isinstance(meta, dict) else {}


def join_frontmatter(data: dict, body: str) -> str:
    """Rebuild a SKILL.md string from frontmatter dict + body."""
    fm = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body}"


def ensure_durin(data: dict) -> dict:
    """Return data['metadata']['durin'], creating/repairing the path in place."""
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        data["metadata"] = meta
    durin = meta.get("durin")
    if not isinstance(durin, dict):
        durin = {}
        meta["durin"] = durin
    return durin
