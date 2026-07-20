"""File system tools: read, write, edit, list."""

import asyncio
import difflib
import mimetypes
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.file_state import FileStates, _hash_file, current_file_states
from durin.agent.tools.path_utils import resolve_workspace_path
from durin.agent.tools.post_edit_check import run_post_edit_check
from durin.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.telemetry.logger import current_telemetry
from durin.utils.atomic_write import atomic_write_bytes, atomic_write_text
from durin.utils.helpers import build_image_content_blocks, detect_image_mime


class _FsTool(Tool, ContextAware):
    """Shared base for filesystem tools — common init and path resolution."""

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        extra_allowed_dirs: list[Path] | None = None,
        file_states: FileStates | None = None,
        post_edit_config: Any = None,
        guard_skills_dir: bool = True,
    ):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._extra_allowed_dirs = extra_allowed_dirs
        # PostEditCheckConfig | None — only Write/Edit consume it.
        self._post_edit_config = post_edit_config
        # Whether _resolve_write() refuses writes under workspace/skills/. On
        # by default so every LLM-facing instance (main loop, subagents,
        # execute_code — all built via `create(ctx)`) is protected. Callers
        # that hand-build a tool over an isolated, non-live workspace (e.g. a
        # throwaway staging copy that is itself validated before ever
        # reaching the live tree) may pass False — see skill_restructure.py.
        self._guard_skills_dir = guard_skills_dir
        # Explicit state is used by isolated runners like Dream/subagents.
        # Main AgentLoop tools leave this unset and resolve state from the
        # current async task, which keeps shared tool instances session-safe.
        self._explicit_file_states = file_states
        self._fallback_file_states = FileStates()
        self._request_ctx: RequestContext | None = None

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    def _work_dir(self) -> Path | None:
        """Return the per-session work directory path, or None when no session is set.

        Pure path computation — does NOT create the directory. The directory is
        created lazily by the write utilities (atomic_write_text/bytes call
        parent.mkdir before writing), so read-only sessions never litter the
        workspace with empty work/<session>/ directories.
        """
        sk = self._request_ctx.session_key if self._request_ctx else None
        if not sk or self._workspace is None:
            return None
        from durin.agent.tools.work_area import session_work_dir
        return session_work_dir(self._workspace, sk)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        from durin.agent.skills import BUILTIN_SKILLS_DIR

        restrict = (
            ctx.config.restrict_to_workspace
            or ctx.config.exec.sandbox
        )
        allowed_dir = Path(ctx.workspace) if restrict else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        return cls(
            workspace=Path(ctx.workspace),
            allowed_dir=allowed_dir,
            extra_allowed_dirs=extra_read,
            file_states=ctx.file_state_store,
            post_edit_config=getattr(ctx.config, "post_edit_check", None),
        )

    @property
    def _file_states(self) -> FileStates:
        if self._explicit_file_states is not None:
            return self._explicit_file_states
        return current_file_states(self._fallback_file_states)

    def _resolve(self, path: str) -> Path:
        return resolve_workspace_path(
            path,
            self._workspace,
            self._allowed_dir,
            self._extra_allowed_dirs,
            work_dir=self._work_dir(),
        )

    def _resolve_write(self, path: str) -> Path:
        """Like `_resolve`, but additionally refuses paths under the skills
        registry — reads of `skills/` stay legitimate (builtins, installed
        skills), but generic write tools must not touch it directly. Skill
        authoring goes through `skill-drafts/<name>/` + `skill_publish`.
        """
        denied = (
            [self._workspace / "skills"]
            if self._guard_skills_dir and self._workspace is not None
            else None
        )
        return resolve_workspace_path(
            path,
            self._workspace,
            self._allowed_dir,
            self._extra_allowed_dirs,
            work_dir=self._work_dir(),
            denied_subdirs=denied,
        )

    def _display_path(self, fp: Path) -> str:
        """Workspace-relative path for telemetry; falls back to absolute."""
        if self._workspace is not None:
            with suppress(ValueError):
                return fp.relative_to(self._workspace).as_posix()
        return str(fp)

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit a telemetry event if a logger is bound for this task.

        Failures are silently swallowed — telemetry must never break a tool call.
        """
        logger_obj = current_telemetry()
        if logger_obj is None:
            return
        try:
            logger_obj.log(event_type, data)
        except Exception:
            pass

    def _file_not_found_msg(self, path: str, fp: Path) -> str:
        """Build a 'File not found' error with 'Did you mean ...?' suggestions.

        Shared by ReadFileTool (T3) and EditFileTool — saves the model a turn
        when it guesses a path wrong (typo, wrong dir, etc.). Uses fuzzy match
        on filenames in the same directory; falls back to a plain error when
        the parent doesn't exist or no close match is found.
        """
        parent = fp.parent
        suggestions: list[str] = []
        if parent.is_dir():
            try:
                siblings = [f.name for f in parent.iterdir() if f.is_file()]
            except OSError:
                siblings = []
            close = difflib.get_close_matches(fp.name, siblings, n=3, cutoff=0.6)
            suggestions = [str(parent / c) for c in close]
        parts = [f"Error: File not found: {path}"]
        if suggestions:
            parts.append("Did you mean: " + ", ".join(suggestions) + "?")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


_BLOCKED_DEVICE_PATHS = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/stdout", "/dev/stderr",
    "/dev/tty", "/dev/console",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})


def _is_blocked_device(path: str | Path) -> bool:
    """Check if path is a blocked device that could hang or produce infinite output."""
    import re
    raw = str(path)

    # Resolve symlinks to check the actual target
    try:
        resolved = str(Path(raw).resolve())
    except (OSError, ValueError):
        resolved = raw

    if raw in _BLOCKED_DEVICE_PATHS or resolved in _BLOCKED_DEVICE_PATHS:
        return True
    if re.match(r"/proc/\d+/fd/[012]$", raw) or re.match(r"/proc/self/fd/[012]$", raw):
        return True
    if re.match(r"/proc/\d+/fd/[012]$", resolved) or re.match(r"/proc/self/fd/[012]$", resolved):
        return True

    # Check if resolved path starts with /dev/ (covers symlinks to devices)
    if resolved.startswith("/dev/"):
        return True
    return False


def _parse_page_range(pages: str, total: int) -> tuple[int, int]:
    """Parse a page range like '2-5' into 0-based (start, end) inclusive."""
    parts = pages.strip().split("-")
    if len(parts) == 1:
        p = int(parts[0])
        return max(0, p - 1), min(p - 1, total - 1)
    start = int(parts[0])
    end = int(parts[1])
    return max(0, start - 1), min(end - 1, total - 1)


# Cap on paths per read_file call. Generous: reads are cheap local IO,
# the cap just bounds how much file content lands in the model's context
# from a single call.
MAX_READ_PATHS: int = 15


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("The file path to read. Use this OR `paths` (not both)."),
        paths=ArraySchema(
            items=StringSchema("A file path to read"),
            description=(
                f"List of file paths to read in one call (max {MAX_READ_PATHS}). "
                "Results come back in the same order, each as a `{path, content}` "
                "record with an `error` field on entries that failed. Prefer this "
                "form whenever 2+ independent files need reading. `offset`/`limit`/"
                "`pages` are not supported with `paths` — use single `path` for "
                "paginated reads."
            ),
        ),
        offset=IntegerSchema(
            1,
            description="Line number to start reading from (1-indexed, default 1)",
            minimum=1,
        ),
        limit=IntegerSchema(
            2000,
            description="Maximum number of lines to read (default 2000)",
            minimum=1,
        ),
        pages=StringSchema("Page range for PDF files, e.g. '1-5' (default: all, max 20 pages)"),
    )
)
class ReadFileTool(_FsTool):
    """Read file contents with optional line-based pagination."""
    _scopes = {"core", "subagent", "memory"}

    _MAX_CHARS = 128_000
    _DEFAULT_LIMIT = 2000
    _MAX_PDF_PAGES = 20

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read a file (text, image, or document). "
            "Pass `path` for a single file. When you know you need two or more "
            "files, ALWAYS pass them together as `paths` (a list) instead of "
            "reading one at a time — it returns them all in a single call. "
            "For example, to read a module with its test: "
            '`paths: ["src/auth.py", "tests/test_auth.py"]`. '
            "Text output format: LINE_NUM|CONTENT. "
            "Images return visual content for analysis. "
            "Supports PDF, DOCX, XLSX, PPTX documents. "
            "Use offset and limit for large text files (single `path` only). "
            "Reads exceeding ~128K chars are truncated."
        )

    @property
    def read_only(self) -> bool:
        return True

    def fanout_size(self, arguments: dict[str, Any]) -> int:
        paths = arguments.get("paths")
        return len(paths) if isinstance(paths, list) and paths else 1

    async def execute(
        self,
        path: str | None = None,
        paths: list[str] | None = None,
        offset: int = 1,
        limit: int | None = None,
        pages: str | None = None,
        verbatim: bool = False,
        **kwargs: Any,
    ) -> Any:
        # Mutually exclusive surfaces, mirroring memory_drill / web_fetch.
        if path and paths:
            return "Error: pass either `path` (single) or `paths` (list), not both"
        if paths is not None and path is None:
            if not isinstance(paths, list) or len(paths) == 0:
                return "Error: paths must be a non-empty list"
            if len(paths) > MAX_READ_PATHS:
                return f"Error: too many paths ({len(paths)}); cap is {MAX_READ_PATHS} per call"
            # Each read is independent local IO touching a distinct file-state
            # key, so awaiting them together is safe; this collapses N reads
            # into one tool call (one round-trip, guaranteed grouping).
            results = await asyncio.gather(*[
                self._read_one_safe(str(p)) for p in paths
            ])
            return {"results": results}

        return await self._read_one(path, offset, limit, pages, verbatim=verbatim)

    async def _read_one_safe(self, path: str) -> dict[str, Any]:
        """Batch helper — never raises, always returns a record carrying the
        path so the caller can match it back to its request."""
        if not path:
            return {"path": path, "error": "empty path"}
        try:
            content = await self._read_one(path)
        except Exception as exc:  # defensive: one read must not abort the batch
            return {"path": path, "error": f"read failed: {exc}"}
        return {"path": path, "content": content}

    async def _read_one(self, path: str | None = None, offset: int = 1, limit: int | None = None, pages: str | None = None, verbatim: bool = False, **kwargs: Any) -> Any:
        try:
            if not path:
                return "Error reading file: Unknown path"

            # Device path blacklist
            if _is_blocked_device(path):
                return f"Error: Reading {path} is blocked (device path that could hang or produce infinite output)."

            fp = self._resolve(path)
            if _is_blocked_device(fp):
                return f"Error: Reading {fp} is blocked (device path that could hang or produce infinite output)."
            if not fp.exists():
                return self._file_not_found_msg(path, fp)
            if not fp.is_file():
                return f"Error: Not a file: {path}"

            # PDF support
            if fp.suffix.lower() == ".pdf":
                return self._read_pdf(fp, pages)

            # Office document support
            if fp.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
                return self._read_office_doc(fp)

            raw = fp.read_bytes()

            if verbatim:
                # Programmatic consumers (execute_code scripts) need the file's
                # real content to parse, not the numbered display view (`1| …`),
                # the truncation/pagination footer, or the dedup "unchanged"
                # stub — any of which breaks json.loads / csv / line counting.
                try:
                    text = raw.decode("utf-8").replace("\r\n", "\n")
                except UnicodeDecodeError:
                    return (
                        f"Error: Cannot read binary file {path} as text "
                        "(use open(path, 'rb') in the script for raw bytes)."
                    )
                self._emit("tool.read_file", {
                    "path": self._display_path(fp),
                    "kind": "text",
                    "verbatim": True,
                    "result_chars": len(text),
                })
                return text

            if not raw:
                return f"(Empty file: {path})"

            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if mime and mime.startswith("image/"):
                return build_image_content_blocks(raw, mime, str(fp), f"(Image file: {path})")

            # Read dedup: same path + offset + limit + unchanged mtime → stub
            # Always check for external modifications before dedup
            entry = self._file_states.get(fp)
            try:
                current_mtime = os.path.getmtime(fp)
            except OSError:
                current_mtime = 0.0
            if entry and entry.can_dedup and entry.offset == offset and entry.limit == limit:
                if current_mtime != entry.mtime:
                    # File was modified externally - force full read and mark as not dedupable
                    entry.can_dedup = False
                    self._file_states.record_read(fp, offset=offset, limit=limit)  # Update state with new mtime
                    # Continue to read full content (don't return dedup message)
                else:
                    # File unchanged - return dedup message
                    # But only if content is actually unchanged (not just mtime)
                    current_hash = _hash_file(str(fp))
                    if current_hash == entry.content_hash:
                        self._emit("tool.read_file", {
                            "path": self._display_path(fp),
                            "offset": offset,
                            "limit": limit,
                            "kind": "text",
                            "dedup": True,
                        })
                        return f"[File unchanged since last read: {path}]"
                    else:
                        # Content changed despite same mtime - force full read
                        entry.can_dedup = False
                        self._file_states.record_read(fp, offset=offset, limit=limit)
            else:
                # No previous state or marked as not dedupable - read full content
                self._file_states.record_read(fp, offset=offset, limit=limit)
                # Force full read by setting can_dedup to False for this read
                if entry:
                    entry.can_dedup = False

            # Read the file content after dedup check
            raw = fp.read_bytes()
            try:
                text_content = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Binary file - return error message
                mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
                if mime and mime.startswith("image/"):
                    return build_image_content_blocks(raw, mime, str(fp), f"(Image file: {path})")
                return f"Error: Cannot read binary file {path} (MIME: {mime or 'unknown'}). Only UTF-8 text and images are supported."

            # Normalize CRLF -> LF before line-splitting. Primarily a Windows
            # concern (git checkouts with autocrlf, editors saving CRLF) but
            # applied on all platforms so downstream StrReplace/Grep behavior
            # is consistent regardless of where the file was written.
            text_content = text_content.replace("\r\n", "\n")

            all_lines = text_content.splitlines()
            total = len(all_lines)

            if offset < 1:
                offset = 1
            if offset > total:
                return f"Error: offset {offset} is beyond end of file ({total} lines)"

            start = offset - 1
            end = min(start + (limit or self._DEFAULT_LIMIT), total)
            numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
            result = "\n".join(numbered)

            if len(result) > self._MAX_CHARS:
                trimmed, chars = [], 0
                for line in numbered:
                    chars += len(line) + 1
                    if chars > self._MAX_CHARS:
                        break
                    trimmed.append(line)
                end = start + len(trimmed)
                result = "\n".join(trimmed)

            if end < total:
                result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
                truncated = True
            else:
                result += f"\n\n(End of file — {total} lines total)"
                truncated = False
            self._file_states.record_read(fp, offset=offset, limit=limit)
            self._emit("tool.read_file", {
                "path": self._display_path(fp),
                "offset": offset,
                "limit": limit or self._DEFAULT_LIMIT,
                "total_lines": total,
                "returned_lines": end - start,
                "result_chars": len(result),
                "kind": "text",
                "truncated": truncated,
                "dedup": False,
            })
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"

    def _read_pdf(self, fp: Path, pages: str | None) -> str:
        try:
            from pypdf import PdfReader
        except ImportError:
            return "Error: PDF reading requires pypdf. Install with: pip install pypdf"

        try:
            reader = PdfReader(str(fp))
            total_pages = len(reader.pages)
        except Exception as e:
            return f"Error reading PDF: {e}"

        if pages:
            try:
                start, end = _parse_page_range(pages, total_pages)
            except (ValueError, IndexError):
                reader.close()
                return f"Error: Invalid page range '{pages}'. Use format like '1-5'."
            if start > end or start >= total_pages:
                reader.close()
                return f"Error: Page range '{pages}' is out of bounds (document has {total_pages} pages)."
        else:
            start = 0
            end = min(total_pages - 1, self._MAX_PDF_PAGES - 1)

        if end - start + 1 > self._MAX_PDF_PAGES:
            end = start + self._MAX_PDF_PAGES - 1

        parts: list[str] = []
        for i in range(start, end + 1):
            text = (reader.pages[i].extract_text() or "").strip()
            if text:
                parts.append(f"--- Page {i + 1} ---\n{text}")
        reader.close()

        if not parts:
            return f"(PDF has no extractable text: {fp})"

        result = "\n\n".join(parts)
        if end < total_pages - 1:
            result += f"\n\n(Showing pages {start + 1}-{end + 1} of {total_pages}. Use pages='{end + 2}-{min(end + 1 + self._MAX_PDF_PAGES, total_pages)}' to continue.)"
        if len(result) > self._MAX_CHARS:
            result = result[:self._MAX_CHARS] + "\n\n(PDF text truncated at ~128K chars)"
        return result

    def _read_office_doc(self, fp: Path) -> str:
        from durin.utils.document import extract_text

        result = extract_text(fp)

        if result is None:
            return f"Error: Unsupported file format: {fp.suffix}"

        if result.startswith("[error:"):
            return f"Error reading {fp.suffix.upper()} file: {result}"

        if not result:
            return f"({fp.suffix.upper().lstrip('.')} has no extractable text: {fp})"

        if len(result) > self._MAX_CHARS:
            result = result[:self._MAX_CHARS] + "\n\n(Document text truncated at ~128K chars)"

        return result


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("The file path to write to"),
        content=StringSchema("The content to write"),
        required=["path", "content"],
    )
)
class WriteFileTool(_FsTool):
    """Write content to a file."""
    _scopes = {"core", "subagent", "memory"}

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Overwrites if the file already exists; "
            "creates parent directories as needed. "
            "For partial edits, prefer edit_file instead."
        )

    async def execute(self, path: str | None = None, content: str | None = None, **kwargs: Any) -> str:
        try:
            if not path:
                raise ValueError("Unknown path")
            if content is None:
                raise ValueError("Unknown content")
            fp = self._resolve_write(path)
            atomic_write_text(fp, content)
            self._file_states.record_write(fp)
            msg = f"Successfully wrote {len(content)} characters to {fp}"
            check = await run_post_edit_check(fp, self._post_edit_config)
            if check:
                msg += check
            return msg
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

_QUOTE_TABLE = str.maketrans({
    "\u2018": "'", "\u2019": "'",  # curly single → straight
    "\u201c": '"', "\u201d": '"',  # curly double → straight
    "'": "'", '"': '"',            # identity (kept for completeness)
})


def _normalize_quotes(s: str) -> str:
    return s.translate(_QUOTE_TABLE)


def _curly_double_quotes(text: str) -> str:
    parts: list[str] = []
    opening = True
    for ch in text:
        if ch == '"':
            parts.append("\u201c" if opening else "\u201d")
            opening = not opening
        else:
            parts.append(ch)
    return "".join(parts)


def _curly_single_quotes(text: str) -> str:
    parts: list[str] = []
    opening = True
    for i, ch in enumerate(text):
        if ch != "'":
            parts.append(ch)
            continue
        prev_ch = text[i - 1] if i > 0 else ""
        next_ch = text[i + 1] if i + 1 < len(text) else ""
        if prev_ch.isalnum() and next_ch.isalnum():
            parts.append("\u2019")
            continue
        parts.append("\u2018" if opening else "\u2019")
        opening = not opening
    return "".join(parts)


def _preserve_quote_style(old_text: str, actual_text: str, new_text: str) -> str:
    """Preserve curly quote style when a quote-normalized fallback matched."""
    if _normalize_quotes(old_text.strip()) != _normalize_quotes(actual_text.strip()) or old_text == actual_text:
        return new_text

    styled = new_text
    if any(ch in actual_text for ch in ("\u201c", "\u201d")) and '"' in styled:
        styled = _curly_double_quotes(styled)
    if any(ch in actual_text for ch in ("\u2018", "\u2019")) and "'" in styled:
        styled = _curly_single_quotes(styled)
    return styled


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _reindent_like_match(old_text: str, actual_text: str, new_text: str) -> str:
    """Preserve the outer indentation from the actual matched block."""
    old_lines = old_text.split("\n")
    actual_lines = actual_text.split("\n")
    if len(old_lines) != len(actual_lines):
        return new_text

    comparable = [
        (old_line, actual_line)
        for old_line, actual_line in zip(old_lines, actual_lines)
        if old_line.strip() and actual_line.strip()
    ]
    if not comparable or any(
        _normalize_quotes(old_line.strip()) != _normalize_quotes(actual_line.strip())
        for old_line, actual_line in comparable
    ):
        return new_text

    old_ws = _leading_ws(comparable[0][0])
    actual_ws = _leading_ws(comparable[0][1])
    if actual_ws == old_ws:
        return new_text

    if old_ws:
        if not actual_ws.startswith(old_ws):
            return new_text
        delta = actual_ws[len(old_ws):]
    else:
        delta = actual_ws

    if not delta:
        return new_text

    return "\n".join((delta + line) if line else line for line in new_text.split("\n"))


@dataclass(slots=True)
class _MatchSpan:
    start: int
    end: int
    text: str
    line: int


def _find_exact_matches(content: str, old_text: str) -> list[_MatchSpan]:
    matches: list[_MatchSpan] = []
    start = 0
    while True:
        idx = content.find(old_text, start)
        if idx == -1:
            break
        matches.append(
            _MatchSpan(
                start=idx,
                end=idx + len(old_text),
                text=content[idx : idx + len(old_text)],
                line=content.count("\n", 0, idx) + 1,
            )
        )
        start = idx + max(1, len(old_text))
    return matches


def _find_trim_matches(content: str, old_text: str, *, normalize_quotes: bool = False) -> list[_MatchSpan]:
    old_lines = old_text.splitlines()
    if not old_lines:
        return []

    content_lines = content.splitlines()
    content_lines_keepends = content.splitlines(keepends=True)
    if len(content_lines) < len(old_lines):
        return []

    offsets: list[int] = []
    pos = 0
    for line in content_lines_keepends:
        offsets.append(pos)
        pos += len(line)
    offsets.append(pos)

    if normalize_quotes:
        stripped_old = [_normalize_quotes(line.strip()) for line in old_lines]
    else:
        stripped_old = [line.strip() for line in old_lines]

    matches: list[_MatchSpan] = []
    window_size = len(stripped_old)
    for i in range(len(content_lines) - window_size + 1):
        window = content_lines[i : i + window_size]
        if normalize_quotes:
            comparable = [_normalize_quotes(line.strip()) for line in window]
        else:
            comparable = [line.strip() for line in window]
        if comparable != stripped_old:
            continue

        start = offsets[i]
        end = offsets[i + window_size]
        if content_lines_keepends[i + window_size - 1].endswith("\n"):
            end -= 1
        matches.append(
            _MatchSpan(
                start=start,
                end=end,
                text=content[start:end],
                line=i + 1,
            )
        )
    return matches


def _find_quote_matches(content: str, old_text: str) -> list[_MatchSpan]:
    norm_content = _normalize_quotes(content)
    norm_old = _normalize_quotes(old_text)
    matches: list[_MatchSpan] = []
    start = 0
    while True:
        idx = norm_content.find(norm_old, start)
        if idx == -1:
            break
        matches.append(
            _MatchSpan(
                start=idx,
                end=idx + len(old_text),
                text=content[idx : idx + len(old_text)],
                line=content.count("\n", 0, idx) + 1,
            )
        )
        start = idx + max(1, len(norm_old))
    return matches


def _find_block_anchor_matches(content: str, old_text: str) -> list[_MatchSpan]:
    """T2 — Block-anchor matcher: match first+last line exactly (after strip),
    fuzzy-match middle lines with similarity threshold.

    Applies only
    when ``old_text`` has 3+ lines — needs anchors top and bottom plus at
    least one middle line. Useful when the model knows the start and end of
    a block but the interior has changed slightly (reformatted, comment added,
    whitespace shifted). Middle similarity is best-match containment of the
    old lines in the candidate block (insertion-tolerant, truncation-
    rejecting); thresholds are 0.66 for a single candidate and 0.85 when
    several blocks share anchors.
    """
    old_lines = old_text.splitlines()
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    if len(old_lines) < 3:
        return []

    content_lines = content.splitlines()
    content_lines_keepends = content.splitlines(keepends=True)
    if len(content_lines) < len(old_lines):
        return []

    offsets: list[int] = []
    pos = 0
    for line in content_lines_keepends:
        offsets.append(pos)
        pos += len(line)
    offsets.append(pos)

    first_anchor = old_lines[0].strip()
    last_anchor = old_lines[-1].strip()
    if not first_anchor or not last_anchor:
        return []

    # Find every (first_anchor, last_anchor) candidate span (first match
    # of last_anchor after each first_anchor occurrence).
    candidates: list[tuple[int, int]] = []
    for i in range(len(content_lines)):
        if content_lines[i].strip() != first_anchor:
            continue
        for j in range(i + 2, len(content_lines)):
            if content_lines[j].strip() == last_anchor:
                candidates.append((i, j))
                break
    if not candidates:
        return []

    # Similarity = containment of old middle lines in the candidate middle:
    # each old line scored against its BEST match among the candidate's
    # middle lines (not index-aligned, so inserted lines — the strategy's
    # whole purpose — don't misalign the comparison), averaged over ALL old
    # lines (so a truncated candidate can't score on a lucky prefix the way
    # check_len=min() allowed). Calibration on the suite's cases: insertions
    # 1.0, rephrased comment 0.83, truncated middle 0.61, unrelated middle
    # 0.44, rewritten body 0.34. Single candidate: 0.66. Multiple: 0.85.
    threshold = 0.66 if len(candidates) == 1 else 0.85
    middle_old = [line.strip() for line in old_lines[1:-1]]

    matches: list[_MatchSpan] = []
    for start_line, end_line in candidates:
        middle_actual = [content_lines[k].strip() for k in range(start_line + 1, end_line)]
        if middle_old:
            if not middle_actual:
                continue
            sim_sum = 0.0
            for a in middle_old:
                if not a:
                    sim_sum += 1.0
                    continue
                sim_sum += max(
                    difflib.SequenceMatcher(None, a, b).ratio()
                    for b in middle_actual
                )
            similarity = sim_sum / len(middle_old)
        else:
            similarity = 1.0

        if similarity < threshold:
            continue

        start = offsets[start_line]
        end = offsets[end_line + 1]
        if content_lines_keepends[end_line].endswith("\n"):
            end -= 1
        matches.append(
            _MatchSpan(
                start=start,
                end=end,
                text=content[start:end],
                line=start_line + 1,
            )
        )
    return matches


# Ordered cascade — used both for matching and telemetry (we report which
# strategy found the match so we can measure how often each layer earns its
# keep). Keep this in sync with the cascade in `_find_matches_with_strategy`.
_MATCH_STRATEGIES = (
    "exact",
    "line_trimmed",
    "line_trimmed_quote_normalized",
    "quote_normalized",
    "block_anchor",
)


def _find_matches_with_strategy(content: str, old_text: str) -> tuple[list[_MatchSpan], str | None]:
    """Locate all matches using the progressive cascade, return strategy used."""
    cascade = (
        ("exact", lambda: _find_exact_matches(content, old_text)),
        ("line_trimmed", lambda: _find_trim_matches(content, old_text)),
        ("line_trimmed_quote_normalized", lambda: _find_trim_matches(content, old_text, normalize_quotes=True)),
        ("quote_normalized", lambda: _find_quote_matches(content, old_text)),
        ("block_anchor", lambda: _find_block_anchor_matches(content, old_text)),
    )
    for name, matcher in cascade:
        matches = matcher()
        if matches:
            return matches, name
    return [], None


def _find_matches(content: str, old_text: str) -> list[_MatchSpan]:
    """Backwards-compatible wrapper. Prefer ``_find_matches_with_strategy``."""
    matches, _ = _find_matches_with_strategy(content, old_text)
    return matches


def _collapse_internal_whitespace(text: str) -> str:
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


def _diagnose_near_match(old_text: str, actual_text: str) -> list[str]:
    """Return actionable hints describing why text was close but not exact."""
    hints: list[str] = []

    if old_text.lower() == actual_text.lower() and old_text != actual_text:
        hints.append("letter case differs")
    if _collapse_internal_whitespace(old_text) == _collapse_internal_whitespace(actual_text) and old_text != actual_text:
        hints.append("whitespace differs")
    if old_text.rstrip("\n") == actual_text.rstrip("\n") and old_text != actual_text:
        hints.append("trailing newline differs")
    if _normalize_quotes(old_text) == _normalize_quotes(actual_text) and old_text != actual_text:
        hints.append("quote style differs")

    return hints


def _best_window(old_text: str, content: str) -> tuple[float, int, list[str], list[str]]:
    """Find the closest line-window match and return ratio/start/snippet/hints."""
    lines = content.splitlines(keepends=True)
    old_lines = old_text.splitlines(keepends=True)
    window = max(1, len(old_lines))

    best_ratio, best_start = -1.0, 0
    best_window_lines: list[str] = []

    for i in range(max(1, len(lines) - window + 1)):
        current = lines[i : i + window]
        ratio = difflib.SequenceMatcher(None, old_lines, current).ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, i
            best_window_lines = current

    actual_text = "".join(best_window_lines).replace("\r\n", "\n").rstrip("\n")
    hints = _diagnose_near_match(old_text.replace("\r\n", "\n").rstrip("\n"), actual_text)
    return best_ratio, best_start, best_window_lines, hints


def _find_match(content: str, old_text: str) -> tuple[str | None, int]:
    """Locate old_text in content with a multi-level fallback chain:

    1. Exact substring match
    2. Line-trimmed sliding window (handles indentation differences)
    3. Smart quote normalization (curly ↔ straight quotes)

    Both inputs should use LF line endings (caller normalises CRLF).
    Returns (matched_fragment, count) or (None, 0).
    """
    matches = _find_matches(content, old_text)
    if not matches:
        return None, 0
    return matches[0].text, len(matches)


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("The file path to edit"),
        old_text=StringSchema("The text to find and replace"),
        new_text=StringSchema("The text to replace with"),
        replace_all=BooleanSchema(description="Replace all occurrences (default false)"),
        required=["path", "old_text", "new_text"],
    )
)
class EditFileTool(_FsTool):
    """Edit a file by replacing text with fallback matching."""
    _scopes = {"core", "subagent", "memory"}

    _MAX_EDIT_FILE_SIZE = 1024 * 1024 * 1024  # 1 GiB
    _MARKDOWN_EXTS = frozenset({".md", ".mdx", ".markdown"})

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing old_text with new_text. "
            "Tolerates minor whitespace/indentation differences and curly/straight quote mismatches. "
            "If old_text matches multiple times, you must provide more context "
            "or set replace_all=true. Shows a diff of the closest match on failure."
        )

    @staticmethod
    def _strip_trailing_ws(text: str) -> str:
        """Strip trailing whitespace from each line."""
        return "\n".join(line.rstrip() for line in text.split("\n"))

    async def execute(
        self, path: str | None = None, old_text: str | None = None,
        new_text: str | None = None,
        replace_all: bool = False, **kwargs: Any,
    ) -> str:
        try:
            if not path:
                raise ValueError("Unknown path")
            if old_text is None:
                raise ValueError("Unknown old_text")
            if new_text is None:
                raise ValueError("Unknown new_text")

            # .ipynb detection
            if path.endswith(".ipynb"):
                return "Error: This is a Jupyter notebook. Use the notebook_edit tool instead of edit_file."

            fp = self._resolve_write(path)

            # Create-file semantics: old_text='' + file doesn't exist → create
            if not fp.exists():
                if old_text == "":
                    atomic_write_text(fp, new_text)
                    self._file_states.record_write(fp)
                    return f"Successfully created {fp}"
                return self._file_not_found_msg(path, fp)

            # File size protection
            try:
                fsize = fp.stat().st_size
            except OSError:
                fsize = 0
            if fsize > self._MAX_EDIT_FILE_SIZE:
                return f"Error: File too large to edit ({fsize / (1024**3):.1f} GiB). Maximum is 1 GiB."

            # Create-file: old_text='' but file exists and not empty → reject
            if old_text == "":
                raw = fp.read_bytes()
                content = raw.decode("utf-8")
                if content.strip():
                    return f"Error: Cannot create file — {path} already exists and is not empty."
                atomic_write_text(fp, new_text)
                self._file_states.record_write(fp)
                return f"Successfully edited {fp}"

            # Read-before-edit check
            warning = self._file_states.check_read(fp)

            raw = fp.read_bytes()
            # Capture a content hash of the bytes we are about to edit against.
            # Immediately before writing we re-hash to detect concurrent changes.
            # Optimistic CAS: capture a content hash before editing; re-hash before writing to detect concurrent changes.
            read_hash = _hash_file(str(fp))
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")
            norm_old = old_text.replace("\r\n", "\n")
            matches, strategy = _find_matches_with_strategy(content, norm_old)

            if not matches:
                self._emit("tool.edit_file", {
                    "path": self._display_path(fp),
                    "match_strategy": None,
                    "matches": 0,
                    "outcome": "not_found",
                    "old_text_chars": len(old_text),
                    "new_text_chars": len(new_text),
                })
                return self._not_found_msg(old_text, content, path)
            count = len(matches)
            if count > 1 and not replace_all:
                line_numbers = [match.line for match in matches]
                preview = ", ".join(f"line {n}" for n in line_numbers[:3])
                if len(line_numbers) > 3:
                    preview += ", ..."
                location_hint = f" at {preview}" if preview else ""
                self._emit("tool.edit_file", {
                    "path": self._display_path(fp),
                    "match_strategy": strategy,
                    "matches": count,
                    "outcome": "ambiguous",
                    "old_text_chars": len(old_text),
                    "new_text_chars": len(new_text),
                })
                return (
                    f"Warning: old_text appears {count} times{location_hint}. "
                    "Provide more context to make it unique, or set replace_all=true."
                )

            norm_new = new_text.replace("\r\n", "\n")

            # Trailing whitespace stripping (skip markdown to preserve double-space line breaks)
            if fp.suffix.lower() not in self._MARKDOWN_EXTS:
                norm_new = self._strip_trailing_ws(norm_new)

            selected = matches if replace_all else matches[:1]
            new_content = content
            for match in reversed(selected):
                replacement = _preserve_quote_style(norm_old, match.text, norm_new)
                replacement = _reindent_like_match(norm_old, match.text, replacement)

                # Delete-line cleanup: when deleting text (new_text=''), consume trailing
                # newline to avoid leaving a blank line
                end = match.end
                if replacement == "" and not match.text.endswith("\n") and content[end:end + 1] == "\n":
                    end += 1

                new_content = new_content[: match.start] + replacement + new_content[end:]
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")

            # Optimistic CAS: re-hash the file immediately before writing.
            # If another process changed it since our read, abort to avoid
            # a silent lost update.
            if _hash_file(str(fp)) != read_hash:
                return (
                    "Error: file changed on disk since it was read — "
                    "re-read the file and retry the edit."
                )

            atomic_write_bytes(fp, new_content.encode("utf-8"))
            self._file_states.record_write(fp)
            self._emit("tool.edit_file", {
                "path": self._display_path(fp),
                "match_strategy": strategy,
                "matches": len(matches),
                "applied": len(selected),
                "outcome": "edited",
                "replace_all": replace_all,
                "old_text_chars": len(old_text),
                "new_text_chars": len(new_text),
            })
            msg = f"Successfully edited {fp}"
            if strategy and strategy != "exact":
                msg += (
                    f"\n(note: old_text matched via the '{strategy}' fallback, "
                    "not exactly — re-read the file if the result looks unexpected)"
                )
            check = await run_post_edit_check(fp, self._post_edit_config)
            if check:
                msg += check
            if warning:
                msg = f"{warning}\n{msg}"
            return msg
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {e}"

    @staticmethod
    def _not_found_msg(old_text: str, content: str, path: str) -> str:
        best_ratio, best_start, best_window_lines, hints = _best_window(old_text, content)
        if best_ratio > 0.5:
            diff = "\n".join(difflib.unified_diff(
                old_text.splitlines(keepends=True),
                best_window_lines,
                fromfile="old_text (provided)",
                tofile=f"{path} (actual, line {best_start + 1})",
                lineterm="",
            ))
            hint_text = ""
            if hints:
                hint_text = "\nPossible cause: " + ", ".join(hints) + "."
            return (
                f"Error: old_text not found in {path}."
                f"{hint_text}\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
            )

        if hints:
            return (
                f"Error: old_text not found in {path}. "
                f"Possible cause: {', '.join(hints)}. "
                "Copy the exact text from read_file and try again."
            )
        return f"Error: old_text not found in {path}. No similar text found. Verify the file content."


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------

@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("The directory path to list"),
        recursive=BooleanSchema(description="Recursively list all files (default false)"),
        max_entries=IntegerSchema(
            200,
            description="Maximum entries to return (default 200)",
            minimum=1,
        ),
        offset=IntegerSchema(
            0,
            description="Skip the first N entries (pagination; default 0)",
            minimum=0,
        ),
        required=["path"],
    )
)
class ListDirTool(_FsTool):
    """List directory contents with optional recursion."""
    _scopes = {"core", "subagent"}

    _DEFAULT_MAX = 200
    _IGNORE_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".coverage", "htmlcov",
    }

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. "
            "Set recursive=true to explore nested structure; use offset to "
            "page past max_entries. "
            "Common noise directories (.git, node_modules, __pycache__, etc.) are auto-ignored."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self, path: str | None = None, recursive: bool = False,
        max_entries: int | None = None, offset: int = 0, **kwargs: Any,
    ) -> str:
        try:
            if path is None:
                raise ValueError("Unknown path")
            dp = self._resolve(path)
            if not dp.exists():
                return f"Error: Directory not found: {path}"
            if not dp.is_dir():
                return f"Error: Not a directory: {path}"

            cap = max_entries or self._DEFAULT_MAX
            items: list[str] = []
            total = 0

            if recursive:
                for item in sorted(dp.rglob("*")):
                    if any(p in self._IGNORE_DIRS for p in item.parts):
                        continue
                    total += 1
                    if total <= offset:
                        continue
                    if len(items) < cap:
                        rel = item.relative_to(dp)
                        items.append(f"{rel}/" if item.is_dir() else str(rel))
            else:
                for item in sorted(dp.iterdir()):
                    if item.name in self._IGNORE_DIRS:
                        continue
                    total += 1
                    if total <= offset:
                        continue
                    if len(items) < cap:
                        pfx = "📁 " if item.is_dir() else "📄 "
                        items.append(f"{pfx}{item.name}")

            if not items and total == 0:
                self._emit("tool.list_dir", {
                    "path": self._display_path(dp),
                    "recursive": recursive,
                    "max_entries": cap,
                    "offset": offset,
                    "displayed": 0,
                    "total_before_cap": 0,
                    "truncated": False,
                })
                return f"Directory {path} is empty"

            if not items and total > 0:
                return (
                    f"(offset {offset} is beyond the end; "
                    f"directory has {total} entries)"
                )

            result = "\n".join(items)
            truncated = total > offset + cap
            if truncated:
                result += (
                    f"\n\n(showing entries {offset + 1}-{offset + len(items)} "
                    f"of {total}; use offset={offset + cap} to continue)"
                )
            self._emit("tool.list_dir", {
                "path": self._display_path(dp),
                "recursive": recursive,
                "max_entries": cap,
                "offset": offset,
                "displayed": len(items),
                "total_before_cap": total,
                "truncated": truncated,
            })
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {e}"
