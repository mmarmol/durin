"""Format session jsonl as navigable markdown with stable turn anchors.

Produces ``<key>.md`` views of ``<key>.jsonl`` so memory entries can
reference specific turns via ``[turn 42](sessions/<key>.md#turn-42)``
links that the user can click in any markdown viewer.

Deterministic: same jsonl input → byte-identical markdown output.

Anchor stability: each message at the N-th line position of the jsonl
(1-indexed, ignoring the metadata line) gets a stable ``## turn-N``
header regardless of consolidation activity. When ``last_consolidated``
is greater than zero an additional ``## consolidated-1`` super-anchor
groups the consolidated range so derived memory entries can reference
the rollup; the underlying per-turn anchors stay resolvable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["SessionMdError", "regenerate_session_md", "render_session_md"]


class SessionMdError(ValueError):
    """Raised when a session jsonl cannot be rendered as markdown."""


def render_session_md(jsonl_path: Path) -> str:
    """Render a session jsonl file to deterministic markdown."""
    text = jsonl_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines:
        return "# Session (empty)\n"

    try:
        meta = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise SessionMdError(f"line 0 is not valid JSON: {exc}") from exc

    if not isinstance(meta, dict) or meta.get("_type") != "metadata":
        raise SessionMdError("line 0 is not a metadata record")

    key = meta.get("key") or jsonl_path.stem
    created_at = meta.get("created_at", "")
    updated_at = meta.get("updated_at", "")
    try:
        last_consolidated = int(meta.get("last_consolidated", 0) or 0)
    except (TypeError, ValueError):
        last_consolidated = 0

    out: list[str] = [f"# Session {key}", ""]
    if created_at:
        out.append(f"- Created: {created_at}")
    if updated_at:
        out.append(f"- Updated: {updated_at}")
    if created_at or updated_at:
        out.append("")

    if last_consolidated > 0:
        out.append("## consolidated-1")
        out.append("")
        out.append(
            f"Turns 1..{last_consolidated} have been consolidated. "
            "The rolling summary lives in "
            "`<key>.meta.json::derived._last_summary` and `history.jsonl`."
        )
        out.append("")

    for i, raw in enumerate(lines[1:], start=1):
        raw = raw.rstrip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                msg = {"role": "(malformed)", "content": str(msg)}
        except json.JSONDecodeError:
            msg = {"role": "(malformed)", "content": raw}

        out.append(f"## turn-{i}")
        out.append("")
        out.append(_render_msg(msg, consolidated=i <= last_consolidated))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _render_msg(msg: dict[str, Any], *, consolidated: bool) -> str:
    role = msg.get("role", "unknown")
    timestamp = msg.get("timestamp", "")
    content = msg.get("content", "")
    tool_calls = msg.get("tool_calls")

    parts: list[str] = []
    header = f"**{role}**"
    if timestamp:
        header += f" · `{timestamp}`"
    if consolidated:
        header += " · *(consolidated)*"
    parts.append(header)
    parts.append("")

    if isinstance(content, list):
        # Some providers return content as a list of structured blocks.
        rendered_blocks: list[str] = []
        for block in content:
            if isinstance(block, dict):
                rendered_blocks.append(str(block.get("text", "")))
            else:
                rendered_blocks.append(str(block))
        content = "\n".join(b for b in rendered_blocks if b)
    if not isinstance(content, str):
        content = str(content)
    if content:
        parts.append(content)
        parts.append("")

    if tool_calls:
        parts.append("```json")
        parts.append(
            json.dumps(
                tool_calls,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        parts.append("```")

    return "\n".join(parts).rstrip()


def regenerate_session_md(
    jsonl_path: Path,
    md_path: Path | None = None,
) -> Path:
    """Write the markdown view alongside the jsonl file.

    Returns the path written. Default output is the same stem with a
    ``.md`` suffix in the same directory.
    """
    if md_path is None:
        md_path = jsonl_path.with_suffix(".md")
    md_path.write_text(render_session_md(jsonl_path), encoding="utf-8")
    return md_path
