"""Utility functions for durin."""

import base64
import json
import mimetypes
import os
import re
import shutil
import time
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import tiktoken
from loguru import logger


def strip_think(text: str) -> str:
    """Remove thinking blocks, unclosed trailing tags, and tokenizer-level
    template leaks occasionally emitted by some models (notably Gemma 4's
    Ollama renderer).

    Covers:
      1. Well-formed `<think>...</think>` and `<thought>...</thought>` blocks.
      2. Streaming prefixes where the block is never closed.
      3. *Malformed* opening tags missing the `>` — e.g. `<think广场…`. The
         model sometimes emits the tag name directly followed by user-facing
         content with no delimiter; without this step the literal `<think`
         leaks into the rendered message.
      4. Harmony-style channel markers like `<channel|>` / `<|channel|>`
         **at the start of the text** — conservative to avoid eating
         explanatory prose that mentions these tokens.
      5. Orphan closing tags `</think>` / `</thought>` **at the very start
         or end of the text** only, for the same reason.
      6. Trailing partial control tags split across stream chunks, such as
         `<thi`, `<thin`, or `<tho`.

    Since this is also applied before persisting to history (memory.py),
    the edge-only stripping of (4) and (5) is deliberate: stripping those
    tokens mid-text would silently rewrite any message where a user or the
    assistant discusses the tokens themselves.
    """
    # Well-formed blocks first.
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"^\s*<think>[\s\S]*$", "", text)
    text = re.sub(r"<thought>[\s\S]*?</thought>", "", text)
    text = re.sub(r"^\s*<thought>[\s\S]*$", "", text)
    # Malformed opening tags: `<think` / `<thought` where the next char is
    # NOT one that could continue a valid tag / identifier name. Explicitly
    # listing ASCII tag-name chars (letters, digits, `_`, `-`, `:`) plus
    # `>` / `/` — we can't use `\w` here because in Python's default
    # Unicode regex mode it matches CJK characters too, which would defeat
    # the primary fix for `<think广场…` leaks.
    text = re.sub(r"<think(?![A-Za-z0-9_\-:>/])", "", text)
    text = re.sub(r"<thought(?![A-Za-z0-9_\-:>/])", "", text)
    # Edge-only orphan closing tags (start or end of text).
    text = re.sub(r"^\s*</think>\s*", "", text)
    text = re.sub(r"\s*</think>\s*$", "", text)
    text = re.sub(r"^\s*</thought>\s*", "", text)
    text = re.sub(r"\s*</thought>\s*$", "", text)
    # Edge-only channel markers (harmony / Gemma 4 variant leaks).
    text = re.sub(r"^\s*<\|?channel\|?>\s*", "", text)
    # Stream chunks may end in the middle of a control tag. Strip only known
    # control-token prefixes at the very end.
    partial_control_tag = (
        r"</?(?:t|th|thi|thin|think|tho|thou|thoug|though|thought)>?"
        r"|<\|?(?:c|ch|cha|chan|chann|channe|channel)(?:\|?>?)?"
    )
    text = re.sub(rf"(?:{partial_control_tag})$", "", text)
    text = re.sub(r"^\s*<\|?$", "", text)
    return text.strip()


def extract_think(text: str) -> tuple[str | None, str]:
    """Extract thinking content from inline ``<think>`` / ``<thought>`` blocks.

    Returns ``(thinking_text, cleaned_text)``. Only closed blocks are
    extracted; unclosed streaming prefixes are stripped from the cleaned
    text but not surfaced — :func:`strip_think` handles that case.
    """
    parts: list[str] = []
    for m in re.finditer(r"<think>([\s\S]*?)</think>", text):
        parts.append(m.group(1).strip())
    for m in re.finditer(r"<thought>([\s\S]*?)</thought>", text):
        parts.append(m.group(1).strip())
    thinking = "\n\n".join(parts) if parts else None
    return thinking, strip_think(text)


class IncrementalThinkExtractor:
    """Stateful inline ``<think>`` extractor for streaming buffers.

    Streaming providers expose only a single content delta channel. When a
    model embeds reasoning in ``<think>...</think>`` blocks inside that
    channel, callers need to surface the reasoning incrementally as it
    arrives without re-emitting earlier text. This holds the "already
    emitted" cursor so the runner and the loop hook share one shape.
    """

    __slots__ = ("_emitted",)

    def __init__(self) -> None:
        self._emitted = ""

    def reset(self) -> None:
        self._emitted = ""

    async def feed(self, buf: str, emit: Any) -> bool:
        """Emit any new thinking text found in ``buf``.

        Returns True if anything was emitted this call. ``emit`` is an
        async callable taking a single string (typically
        ``hook.emit_reasoning``).
        """
        thinking, _ = extract_think(buf)
        if not thinking or thinking == self._emitted:
            return False
        new = thinking[len(self._emitted):].strip()
        self._emitted = thinking
        if not new:
            return False
        await emit(new)
        return True


def extract_reasoning(
    reasoning_content: str | None,
    thinking_blocks: list[dict[str, Any]] | None,
    content: str | None,
) -> tuple[str | None, str | None]:
    """Return ``(reasoning_text, cleaned_content)`` from one model response.

    Single source of truth for "what reasoning did this response carry, and
    what answer text remains after we peel it out". Fallback order:

    1. Dedicated ``reasoning_content`` (DeepSeek-R1, Kimi, MiMo, OpenAI
       reasoning models, Bedrock).
    2. Anthropic ``thinking_blocks``.
    3. Inline ``<think>`` / ``<thought>`` blocks in ``content``.

    Only one source contributes per response; lower-priority sources are
    ignored if a higher-priority one is present, but inline ``<think>``
    tags are still stripped from ``content`` so they never leak into the
    final answer.
    """
    if reasoning_content:
        return reasoning_content, strip_think(content) if content else content
    if thinking_blocks:
        parts = [
            tb.get("thinking", "")
            for tb in thinking_blocks
            if isinstance(tb, dict) and tb.get("type") == "thinking"
        ]
        joined = "\n\n".join(p for p in parts if p)
        return (joined or None), strip_think(content) if content else content
    if content:
        return extract_think(content)
    return None, content


def detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes, ignoring file extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def build_image_content_blocks(
    raw: bytes, mime: str, path: str, label: str
) -> list[dict[str, Any]]:
    """Build native image blocks plus a short text label."""
    b64 = base64.b64encode(raw).decode()
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
            "_meta": {"path": path},
        },
        {"type": "text", "text": label},
    ]


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


def current_time_str(timezone: str | None = None) -> str:
    """Return the current time string."""
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone) if timezone else None
    except (KeyError, Exception):
        tz = None

    now = datetime.now(tz=tz) if tz else datetime.now().astimezone()
    offset = now.strftime("%z")
    offset_fmt = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    tz_name = timezone or (time.strftime("%Z") or "UTC")
    return f"{now.strftime('%Y-%m-%d %H:%M (%A)')} ({tz_name}, UTC{offset_fmt})"


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')
_TOOL_RESULT_PREVIEW_CHARS = 1200
_TOOL_RESULTS_DIR = ".durin/tool-results"
_TOOL_RESULT_RETENTION_SECS = 7 * 24 * 60 * 60
_TOOL_RESULT_MAX_BUCKETS = 32


def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def image_placeholder_text(path: str | None, *, empty: str = "[image]") -> str:
    """Build an image placeholder string."""
    return f"[image: {path}]" if path else empty


# Extensions durin saves and reads as audio. The platform's mimetypes table
# varies (Linux's knows no ``.weba``, the extension a browser recording is
# saved with), so whether a file is audio must not depend on it alone.
AUDIO_SUFFIXES = frozenset({
    ".mp3", ".ogg", ".oga", ".opus", ".wav", ".weba", ".m4a", ".aac", ".flac",
})


def is_audio_path(path: str) -> bool:
    """True for a file durin treats as audio: one of :data:`AUDIO_SUFFIXES`,
    or any extension the platform's mimetypes table calls audio."""
    if Path(path).suffix.lower() in AUDIO_SUFFIXES:
        return True
    return (mimetypes.guess_type(path)[0] or "").startswith("audio/")


def media_placeholder_text(path: str) -> str:
    """Placeholder for an attachment kept as a path: ``[audio: path]`` for
    audio, ``[image: path]`` for the images that are the rest of it."""
    if is_audio_path(path):
        return f"[audio: {path}]"
    return image_placeholder_text(path)


def truncate_text(text: str, max_chars: int, direction: str = "head") -> str:
    """Truncate text with a stable suffix.

    Args:
        text: input to (possibly) truncate.
        max_chars: target ceiling; ``<= 0`` disables truncation.
        direction: ``"head"`` keeps the first ``max_chars`` characters
            (the default, suitable for file reads, web fetches, grep
            output — context is at the top). ``"tail"`` keeps the last
            ``max_chars`` (suitable for shell/exec output, build logs,
            and anything where errors arrive at the end of the buffer
            and the head is mostly setup noise).

    The truncation marker is inserted at the cut so the LLM can tell
    that data was dropped. Pi's split-direction policy (head for reads,
    tail for bash) is the design inspiration here — keeping the
    semantically valuable end of shell output saved many wasted
    re-runs in their telemetry.

    ``max_chars`` bounds the whole result, marker included: callers
    re-check sizes (the agent runner re-normalizes every tool result on
    each iteration), and a cut result longer than the cap would be cut
    again as if it were new oversized output. A cap too small to hold the
    marker gets a bare cut.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if direction == "tail":
        marker = "... (truncated) ...\n"
        if max_chars <= len(marker):
            return text[-max_chars:]
        return marker + text[-(max_chars - len(marker)):]
    marker = "\n... (truncated)"
    if max_chars <= len(marker):
        return text[:max_chars]
    return text[: max_chars - len(marker)] + marker


def find_legal_message_start(messages: list[dict[str, Any]]) -> int:
    """Find the first index whose tool results have matching assistant calls."""
    declared: set[str] = set()
    start = 0
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.add(str(tc["id"]))
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if tid and str(tid) not in declared:
                start = i + 1
                declared.clear()
    return start


def stringify_text_blocks(content: list[dict[str, Any]]) -> str | None:
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            return None
        if block.get("type") != "text":
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    return "\n".join(parts)


_PERSISTED_REFERENCE_MARKER = "[tool output persisted]"
_PERSISTED_PATH_PREFIX = "Full output saved to: "
_PERSISTED_SIZE_PREFIX = "Original size: "
_PERSISTED_SIZE_SUFFIX = " chars"


def _render_tool_result_reference(
    filepath: Path,
    *,
    original_size: int,
    line_count: int,
    preview: str,
    truncated_preview: bool,
) -> str:
    # The first three lines are parsed back by ``parse_persisted_reference``
    # (compaction keeps the recovery path from them); keep their format.
    # The instructions name the exact call because a model that is only
    # told "saved to a file" tends to re-run the original call instead.
    result = (
        f"{_PERSISTED_REFERENCE_MARKER}\n"
        f"{_PERSISTED_PATH_PREFIX}{filepath}\n"
        f"{_PERSISTED_SIZE_PREFIX}{original_size}{_PERSISTED_SIZE_SUFFIX}\n"
        f"Lines: {line_count}\n"
        "This result was too large for the context, so only a preview is shown. "
        "The whole result is on disk: do not re-run the call to get it back. "
        f'Read it with read_file(path="{filepath}"): each call returns one page '
        "that fits and ends with the offset to continue from. To find something "
        f'specific, use grep(pattern=..., path="{filepath}", output_mode="content").\n'
        f"Preview:\n{preview}"
    )
    if truncated_preview:
        result += "\n..."
    return result


def parse_persisted_reference(text: Any) -> tuple[str, int] | None:
    """Return ``(path, original_size)`` if ``text`` is a persisted-output reference.

    Mirrors the format produced by :func:`_render_tool_result_reference` so a
    later compaction pass can preserve the recovery path instead of dropping
    it. Returns ``None`` for anything that is not such a reference.
    """
    if not isinstance(text, str) or not text.startswith(_PERSISTED_REFERENCE_MARKER):
        return None
    path: str | None = None
    size = 0
    for line in text.splitlines():
        if line.startswith(_PERSISTED_PATH_PREFIX):
            path = line[len(_PERSISTED_PATH_PREFIX):].strip()
        elif line.startswith(_PERSISTED_SIZE_PREFIX) and line.endswith(_PERSISTED_SIZE_SUFFIX):
            digits = line[len(_PERSISTED_SIZE_PREFIX):-len(_PERSISTED_SIZE_SUFFIX)].strip()
            if digits.isdigit():
                size = int(digits)
    if path:
        return (path, size)
    return None


def _bucket_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _bucket_kind(path: Path) -> str:
    """The kind of session a bucket belongs to: its channel prefix
    (``websocket``, ``slack``, ``workflow``, …)."""
    return path.name.split("_", 1)[0]


def _cleanup_tool_result_buckets(root: Path, current_bucket: Path) -> None:
    siblings = [path for path in root.iterdir() if path.is_dir() and path != current_bucket]
    cutoff = time.time() - _TOOL_RESULT_RETENTION_SECS
    for path in siblings:
        if _bucket_mtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
    keep = max(_TOOL_RESULT_MAX_BUCKETS - 1, 0)
    # Each kind of session keeps its own most recent buckets, so a burst of
    # one kind — a wide workflow fan-out, where every node and worker saves
    # into its own bucket — cannot push out a chat's saved results and leave
    # its read-it-back pointers leading nowhere.
    kind = _bucket_kind(current_bucket)
    siblings = [path for path in siblings if path.exists() and _bucket_kind(path) == kind]
    if len(siblings) <= keep:
        return
    siblings.sort(key=_bucket_mtime, reverse=True)
    for path in siblings[keep:]:
        shutil.rmtree(path, ignore_errors=True)


def _write_text_atomic(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def persist_full_tool_result(
    workspace: Path | None,
    session_key: str | None,
    tool_call_id: str,
    text: str,
) -> Path | None:
    """Write *text* to the session's tool-results bucket; return the path.

    Best-effort recoverability primitive: callers append their own
    pointer/reference to whatever survives in context.
    """
    if workspace is None or not isinstance(text, str):
        return None
    root = ensure_dir(workspace / _TOOL_RESULTS_DIR)
    bucket = ensure_dir(root / safe_filename(session_key or "default"))
    try:
        _cleanup_tool_result_buckets(root, bucket)
    except Exception:
        logger.exception("Failed to clean stale tool result buckets in {}", root)
    path = bucket / f"{safe_filename(tool_call_id)}.full.txt"
    _write_text_atomic(path, text)
    return path


def render_structured_result(value: Any) -> str:
    """Render a dict/list tool result as text that can be read in line pages.

    A structured result reaches the model as compact JSON, where every line
    break inside a string is escaped, so a large result is one physical
    line that a line-based reader cannot page. This rendering is what gets
    saved to disk when the result is too large for the context: one
    ``key: value`` line per scalar, nesting by indentation, and every
    multi-line string written out verbatim between a header naming its line
    count and an end marker. (YAML literal blocks were not an option: one
    line with a trailing space forces the whole string back into a single
    escaped line.)
    """
    out: list[str] = []
    _render_structured_value(out, value, "", None)
    return "\n".join(out) + "\n"


def _render_structured_value(out: list[str], value: Any, indent: str, label: str | None) -> None:
    head = f"{indent}{label}:" if label is not None else None
    if isinstance(value, (dict, list)):
        if not value:
            empty = "{}" if isinstance(value, dict) else "[]"
            out.append(f"{head} {empty}" if head else f"{indent}{empty}")
            return
        if head:
            out.append(head)
            indent += "  "
        items = value.items() if isinstance(value, dict) else (
            (f"[{i}]", item) for i, item in enumerate(value)
        )
        for key, item in items:
            _render_structured_value(out, item, indent, str(key))
        return
    if isinstance(value, str) and "\n" in value:
        name = label or "text"
        body = value.splitlines()
        out.append(f"{indent}{name}: ({len(body)} lines follow)")
        out.extend(body)
        out.append(f"{indent}--- end of {name} ---")
        return
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    out.append(f"{head} {text}" if head else f"{indent}{text}")


def _pageable_text(text: str) -> str:
    """``text`` as it is saved for line paging.

    A one-line JSON object or array — the text a structured result became
    once in the context, or a command's or an API's JSON output — is saved
    indented, one field per line. It stays the same JSON, so a script or
    ``jq`` can still use the file. JSON that would not survive the round
    trip (a key repeated in one object) and any other text are saved as is.
    """
    stripped = text.strip()
    if "\n" in stripped or not stripped.startswith(("{", "[")):
        return text
    try:
        value = json.loads(stripped, object_pairs_hook=_unique_keys)
    except ValueError:
        return text
    if not isinstance(value, (dict, list)):
        return text
    return json.dumps(value, ensure_ascii=False, indent=2)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object's pairs as a dict, refusing a repeated key, which a
    dict would silently collapse."""
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError("repeated key")
    return out


def _same_file_text(path: Path, text: str) -> bool:
    """Whether ``path`` already holds exactly ``text``."""
    try:
        if path.stat().st_size != len(text.encode("utf-8")):
            return False
        return path.read_text(encoding="utf-8") == text
    except (OSError, UnicodeDecodeError):
        return False


def maybe_persist_tool_result(
    workspace: Path | None,
    session_key: str | None,
    tool_call_id: str,
    content: Any,
    *,
    max_chars: int,
    spill_text: str | None = None,
) -> Any:
    """Persist oversized tool output and replace it with a stable reference string.

    The size check is on ``content`` — what the model would receive. What
    is written to disk is ``spill_text`` when given (a readable rendering
    of the same result), otherwise the text of ``content``; the reference
    reports the size of what was written, since that is what the model
    pages through.
    """
    if workspace is None or max_chars <= 0:
        return content

    text_payload: str | None = None
    if isinstance(content, str):
        text_payload = content
    elif isinstance(content, list):
        text_payload = stringify_text_blocks(content)
        if text_payload is None:
            return content
    else:
        return content

    if len(text_payload) <= max_chars:
        return content

    root = ensure_dir(workspace / _TOOL_RESULTS_DIR)
    bucket = ensure_dir(root / safe_filename(session_key or "default"))
    try:
        _cleanup_tool_result_buckets(root, bucket)
    except Exception:
        logger.exception("Failed to clean stale tool result buckets in {}", root)
    path = bucket / f"{safe_filename(tool_call_id)}.txt"
    file_text = spill_text if spill_text is not None else _pageable_text(text_payload)
    # The current call's content is authoritative: a reused tool_call_id (the
    # positional tool_N fallback in the runner) must not keep stale bytes.
    # A result saved again unchanged — every iteration of a turn whose cap
    # is below the size it was kept at — is not rewritten; its bucket is
    # still marked as in use, as the write would have, so cleanup keeps
    # ranking it by its last use.
    if _same_file_text(path, file_text):
        with suppress(OSError):
            os.utime(bucket)
    else:
        _write_text_atomic(path, file_text)

    return _render_tool_result_reference(
        path,
        original_size=len(file_text),
        line_count=len(file_text.splitlines()),
        preview=file_text[:_TOOL_RESULT_PREVIEW_CHARS],
        truncated_preview=len(file_text) > _TOOL_RESULT_PREVIEW_CHARS,
    )


def split_message(content: str, max_len: int = 2000) -> list[str]:
    """
    Split content into chunks within max_len, preferring line breaks.

    Args:
        content: The text content to split.
        max_len: Maximum length per chunk (default 2000 for Discord compatibility).

    Returns:
        List of message chunks, each within max_len.
    """
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        # Try to break at newline first, then space, then hard break
        pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def build_assistant_message(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
    prompt_tokens: int | None = None,
) -> dict[str, Any]:
    """Build a provider-safe assistant message with optional reasoning fields.

    When ``prompt_tokens`` is provided (and non-zero), it is stamped onto
    the message as ``usage_prompt_tokens``. This is the authoritative
    count of tokens the provider actually charged for the prompt that
    produced this message — durin's compaction logic uses it as an
    "anchor" to skip estimating everything up to that point, falling
    back to tiktoken only for messages that came after. Inspired by
    pi's ``getLastAssistantUsage`` pattern.

    Providers normalise ``prompt_tokens`` across their native shapes
    (OpenAI ``prompt_tokens``, Anthropic ``input_tokens + cache_read +
    cache_creation``, Bedrock ``Converse.usage.inputTokens``) before
    handing it to the runner, so callers can treat the value uniformly.
    """
    msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_content is not None or thinking_blocks:
        msg["reasoning_content"] = reasoning_content if reasoning_content is not None else ""
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    if prompt_tokens is not None and prompt_tokens > 0:
        msg["usage_prompt_tokens"] = int(prompt_tokens)
    return msg


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for a single string via tiktoken (cl100k_base).

    Returns 0 on encoding failure so callers (telemetry, breakdowns)
    can always do arithmetic without guarding for None.
    """
    if not text:
        return 0
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return 0


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate prompt tokens with tiktoken.

    Counts all fields that providers send to the LLM: content, tool_calls,
    reasoning_content, tool_call_id, name, plus per-message framing overhead.
    """
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text", "")
                    if txt:
                        parts.append(txt)

        tc = msg.get("tool_calls")
        if tc:
            # default=str keeps a quirky-but-present payload counted instead
            # of raising — the field still contributes to the estimate.
            parts.append(json.dumps(tc, ensure_ascii=False, default=str))

        rc = msg.get("reasoning_content")
        if isinstance(rc, str) and rc:
            parts.append(rc)

        for key in ("name", "tool_call_id"):
            value = msg.get(key)
            if isinstance(value, str) and value:
                parts.append(value)

    if tools:
        parts.append(json.dumps(tools, ensure_ascii=False, default=str))

    text = "\n".join(parts)
    per_message_overhead = len(messages) * 4
    # Narrow guard: only the tiktoken load/encode may fail — and it falls back
    # to a char-based estimate, never a silent 0 that reads as an empty prompt
    # and corrupts budget/telemetry arithmetic (C3). A genuinely malformed
    # message shape now surfaces from the loop above instead of being swallowed.
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text)) + per_message_overhead
    except Exception:
        return len(text) // 4 + per_message_overhead


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate prompt tokens contributed by one persisted message."""
    content = message.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    parts.append(text)
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))

    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if message.get("tool_calls"):
        parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))

    rc = message.get("reasoning_content")
    if isinstance(rc, str) and rc:
        parts.append(rc)

    payload = "\n".join(parts)
    if not payload:
        return 4
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return max(4, len(enc.encode(payload)) + 4)
    except Exception:
        return max(4, len(payload) // 4 + 4)


def latest_prompt_tokens_anchor(
    messages: list[dict[str, Any]],
) -> tuple[int, int] | None:
    """Find the most recent assistant message carrying a real usage anchor.

    Returns ``(index, prompt_tokens)`` of the most recent message that
    has ``usage_prompt_tokens`` stamped — the authoritative count the
    provider reported at that point. Returns ``None`` when no
    persisted-usage message exists (fresh session, or all messages are
    synthetic).

    Pi-inspired: the anchor lets compaction reason about token cost
    using REAL numbers up to the anchor and estimate only the tail —
    instead of estimating the entire chain with tiktoken (which adds
    up systematic error on long sessions).
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict):
            continue
        tokens = msg.get("usage_prompt_tokens")
        if isinstance(tokens, int) and tokens > 0:
            return i, tokens
    return None


def estimate_prompt_tokens_chain(
    provider: Any,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Estimate prompt tokens for a chain of messages.

    Resolution order:

    1. **Usage anchor** — if any message carries ``usage_prompt_tokens``
       (set by the runner when a real provider response was received),
       use that count as the baseline — it covers the system prompt, the
       tool definitions and every message *before* the stamped one — and
       only tiktoken-estimate the stamped message and the ones after it,
       without the tool definitions. This is the cheapest AND most
       accurate path on long sessions. Source label: ``"anchored"``.
    2. **Provider counter** — if the provider exposes
       ``estimate_prompt_tokens(messages, tools, model)``, call it.
       Source label: ``"provider_counter"`` (or whatever the provider
       returns).
    3. **tiktoken** — global token count using ``cl100k_base``. Source
       label: ``"tiktoken"``.
    4. ``(0, "none")`` if all three fail.
    """
    anchor = latest_prompt_tokens_anchor(messages)
    if anchor is not None:
        anchor_idx, anchor_tokens = anchor
        # The stamp is the provider's count for the prompt that PRODUCED the
        # anchored message: the system prompt, the tool definitions and every
        # message before it. The anchored message itself and everything after
        # it are new, so they are estimated; the tool definitions are already
        # in the stamp and must not be added again.
        tail_tokens = estimate_prompt_tokens(messages[anchor_idx:])
        return anchor_tokens + tail_tokens, "anchored"

    provider_counter = getattr(provider, "estimate_prompt_tokens", None)
    if callable(provider_counter):
        with suppress(Exception):
            tokens, source = provider_counter(messages, tools, model)
            if isinstance(tokens, (int, float)) and tokens > 0:
                return int(tokens), str(source or "provider_counter")
    estimated = estimate_prompt_tokens(messages, tools)
    if estimated > 0:
        return int(estimated), "tiktoken"
    return 0, "none"


def build_status_content(
    *,
    version: str,
    model: str,
    start_time: float,
    last_usage: dict[str, int],
    context_window_tokens: int,
    session_msg_count: int,
    context_tokens_estimate: int,
    search_usage_text: str | None = None,
    active_task_count: int = 0,
    max_completion_tokens: int = 8192,
    compaction_trigger_tokens: int = 0,
    composition_payload: dict[str, Any] | None = None,
) -> str:
    """Build a human-readable runtime status snapshot.

    Args:
        search_usage_text: Optional pre-formatted web search usage string
                           (produced by SearchUsageInfo.format()). When provided
                           it is appended as an extra section.
        composition_payload: Optional last ``context.composition`` event
                             data (as cached by ``AgentLoop.context.last_composition``).
                             When provided, a "Last turn \u2014 composition"
                             breakdown is appended showing where the
                             prompt tokens are going.
    """
    uptime_s = int(time.time() - start_time)
    uptime = (
        f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m"
        if uptime_s >= 3600
        else f"{uptime_s // 60}m {uptime_s % 60}s"
    )
    last_in = last_usage.get("prompt_tokens", 0)
    last_out = last_usage.get("completion_tokens", 0)
    cached = last_usage.get("cached_tokens", 0)
    ctx_total = max(context_window_tokens, 0)
    # Measure against the point where compaction actually fires, not the raw
    # window and not the consolidation LLM's own input budget. Those two
    # differ from the trigger by up to 2x, so a percentage against either
    # reads as far more (or less) headroom than the session really has.
    ctx_budget = int(compaction_trigger_tokens or 0)
    if ctx_budget <= 0:
        ctx_budget = max(ctx_total - int(max_completion_tokens) - 1024, 1)
    ctx_pct = min(int((context_tokens_estimate / ctx_budget) * 100), 999) if ctx_budget > 0 else 0
    ctx_used_str = (
        f"{context_tokens_estimate // 1000}k"
        if context_tokens_estimate >= 1000
        else str(context_tokens_estimate)
    )
    ctx_total_str = f"{ctx_total // 1000}k" if ctx_total > 0 else "n/a"
    token_line = f"\U0001f4ca Tokens: {last_in} in / {last_out} out"
    if cached and last_in:
        token_line += f" ({cached * 100 // last_in}% cached)"
    lines = [
        f"\U0001f408 durin v{version}",
        f"\U0001f9e0 Model: {model}",
        token_line,
        f"\U0001f4da Context: {ctx_used_str}/{ctx_total_str} ({ctx_pct}% to compaction)",
        f"\U0001f4ac Session: {session_msg_count} messages",
        f"\u23f1 Uptime: {uptime}",
        f"\u26a1 Tasks: {active_task_count} active",
    ]
    if search_usage_text:
        lines.append(search_usage_text)
    if composition_payload:
        lines.extend(_format_composition_section(composition_payload))
    return "\n".join(lines)


def _format_composition_section(payload: dict[str, Any]) -> list[str]:
    """Render the last-turn composition breakdown for ``/status``.

    Two buckets: conversation (what your messages + memory contribute)
    vs infrastructure (identity + bootstrap + skills + tools \u2014 the
    fixed cost of the configuration). Tools are part of infrastructure
    but not highlighted as their own bucket: their share matters in
    small contexts but disappears as sessions grow.
    """
    from durin.agent.context import FROZEN_STABLE_LABELS, summarize_composition

    summary = summarize_composition(payload)
    total = summary["total"]
    if total <= 0:
        return []

    def _row(label: str, n: int, indent: int = 2, suffix: str = "") -> str:
        return f"{' ' * indent}{label:<24} {n:>6}{suffix}"

    def _pct(n: int) -> str:
        return f"{(100 * n // total) if total else 0}%"

    # When this build reused a frozen eager surface instead of rendering the
    # pinned block / hot layer live, the payload names the turn it was taken
    # on (AgentLoop._freeze_eager_surface). Appended only to FROZEN_STABLE_LABELS'
    # rows — the freeze covers just the pinned block and the hot layer,
    # nothing else in the infrastructure bucket.
    eager_frozen_turn = payload.get("eager_frozen_turn")
    frozen_labels = FROZEN_STABLE_LABELS

    out: list[str] = ["", "\U0001f9ee Last turn \u2014 composition"]
    out.append(f"  Prompt tokens          {total:>6}")

    conv_n = summary["conversation_tokens"]
    infra_n = summary["infra_tokens"]
    out.append("")
    out.append(f"  From this conversation: {conv_n:>5} ({_pct(conv_n)})")
    for label, n in sorted(
        summary["conversation_breakdown"].items(), key=lambda kv: -kv[1]
    ):
        out.append(_row(label, n, indent=4))
    out.append("")
    out.append(f"  From infrastructure:    {infra_n:>5} ({_pct(infra_n)})")
    for label, n in sorted(
        summary["infra_breakdown"].items(), key=lambda kv: -kv[1]
    ):
        suffix = (
            f"  (frozen at turn {eager_frozen_turn})"
            if eager_frozen_turn is not None and label in frozen_labels
            else ""
        )
        out.append(_row(label, n, indent=4, suffix=suffix))
    return out


def seed_workflows(workspace: Path) -> list[str]:
    """Reconcile bundled seed workflow JSONs into <workspace>/workflows/.

    Provenance-aware (durin.workflow.seeds): installs missing seeds, follows
    the wheel for seeds the user never edited, and turns updates to edited
    seeds into suggestions instead of overwrites. Returns the relative paths
    this pass wrote (installed + refreshed).
    """
    from durin.workflow.seeds import refresh_seeds

    report = refresh_seeds(Path(workspace))
    return [f"workflows/{name}.json" for name in report.changed]


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files

    try:
        tpl = pkg_files("durin") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    for item in tpl.iterdir():
        if item.name.endswith(".md") and not item.name.startswith("."):
            _write(item, workspace / item.name)
    souls_tpl = tpl / "souls"
    if souls_tpl.is_dir():
        for item in souls_tpl.iterdir():
            if item.name.endswith(".md") and not item.name.startswith("."):
                _write(item, workspace / "souls" / item.name)
    _write(None, workspace / "memory" / "history.jsonl")
    (workspace / "skills").mkdir(exist_ok=True)
    added.extend(seed_workflows(workspace))

    if added and not silent:
        from rich.console import Console

        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")

    # Initialize git for memory version control
    try:
        from durin.utils.gitstore import GitStore

        gs = GitStore(
            workspace,
            tracked_files=[
                "SOUL.md",
            ],
        )
        gs.init()
    except Exception:
        logger.exception("Failed to initialize git store for {}", workspace)

    # Initialize git for skill version control (separate repo, whole-tree)
    try:
        from durin.utils.gitstore import GitStore

        GitStore(workspace / "skills", subtree=True, label="skills").init()
    except Exception:
        logger.exception("Failed to initialize skills git store for {}", workspace)

    return added


_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def convert_gfm_tables(text: str) -> str:
    """Convert GFM pipe tables to bullet lists for surfaces without table support.

    Each body row becomes ``- **first-cell** — Header2: v2; Header3: v3``.
    Fenced code blocks are left untouched; non-table lines pass through.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        is_header = (
            not in_fence
            and "|" in line
            and i + 1 < len(lines)
            # A bare horizontal rule ("---") also matches _TABLE_SEPARATOR's
            # zero-pipe case; require at least one literal "|" on the
            # separator row so prose with a "---" divider isn't mistaken
            # for a table (GFM separators always have pipes, even for a
            # single column: "| - |").
            and "|" in lines[i + 1]
            and _TABLE_SEPARATOR.match(lines[i + 1]) is not None
        )
        if not is_header:
            out.append(line)
            i += 1
            continue
        headers = _table_cells(line)
        i += 2  # skip header + separator
        while i < len(lines) and "|" in lines[i] and lines[i].strip():
            cells = _table_cells(lines[i])
            first = cells[0] if cells else ""
            rest = "; ".join(
                f"{headers[j]}: {cells[j]}"
                for j in range(1, min(len(headers), len(cells)))
                if cells[j]
            )
            out.append(f"- **{first}** — {rest}" if rest else f"- **{first}**")
            i += 1
    return "\n".join(out)
