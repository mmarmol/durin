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
