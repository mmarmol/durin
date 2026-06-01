"""ToolCallBubble — visual block for one agent tool invocation.

Modelled on Claude Code's own tool rendering: a tight header summarising
the call, a dim body block showing arguments / result / diff. The body
is always visible in this first iteration; an interactive expand/collapse
toggle is on the roadmap once the basic shape is right.

Examples by tool:

- ``edit_file`` → unified diff with red / green lines.
- ``exec`` → ``IN: <command>`` + ``OUT: <stdout>`` blocks.
- ``read_file`` → path + first lines + truncation hint.
- generic → JSON-ish args + result preview.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

__all__ = ["ToolCallBubble"]


_STATUS_GLYPH = {
    "running": "○",
    "ok": "✓",
    "error": "✗",
}


class ToolCallBubble(Vertical):
    """One agent tool invocation rendered as a visual block."""

    DEFAULT_CSS = """
    ToolCallBubble {
        height: auto;
        padding: 0 0 0 1;
        margin: 0 2 1 2;
        border-left: thick $accent;
    }
    ToolCallBubble.ok { border-left: thick $success; }
    ToolCallBubble.error { border-left: thick $error; }
    ToolCallBubble.running { border-left: thick $accent; }
    ToolCallBubble > #tc-headline {
        height: 1;
        padding: 0 1;
        layout: horizontal;
    }
    ToolCallBubble > #tc-headline > #tc-header {
        width: 1fr;
        color: $text;
    }
    ToolCallBubble > #tc-headline > #tc-expand {
        width: auto;
        color: $text-muted;
        text-style: underline;
        padding: 0 1;
    }
    ToolCallBubble > #tc-headline > #tc-expand:hover {
        background: $accent 20%;
        color: $accent;
    }
    ToolCallBubble > #tc-headline > #tc-copy {
        width: auto;
        color: $accent;
        text-style: underline;
        padding: 0 1;
    }
    ToolCallBubble > #tc-headline > #tc-copy:hover {
        background: $accent 20%;
    }
    ToolCallBubble > #tc-body {
        height: auto;
        color: $text-muted;
        padding: 0 1;
    }
    ToolCallBubble > #tc-options {
        height: auto;
        padding: 0 1;
    }
    ToolCallBubble .tc-option {
        height: 1;
        color: $accent;
        padding: 0 1;
    }
    ToolCallBubble .tc-option:hover {
        background: $accent 20%;
        text-style: bold;
    }
    ToolCallBubble > #tc-opt-hint {
        height: auto;
        color: $text-muted;
        text-style: italic;
        padding: 0 1;
    }
    """

    # Lines of body content visible by default before truncation kicks
    # in. Past iterations dumped 1 KB of raw output into the chat for
    # every tool call; the user pushed back hard ("se ve todo, no se ni
    # cual fue la respuesta"). 2 lines + an explicit expand toggle
    # matches Claude Code / pi-agent ergonomics.
    PREVIEW_LINES: int = 2

    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__(classes="running")
        self._call_id: str = str(event.get("call_id") or "")
        self._name: str = str(event.get("name") or "tool")
        self._args: Any = event.get("arguments") or {}
        self._status: str = "running"
        # Cache of the last seen result so copy-to-clipboard can grab it.
        self._last_result_text: str = ""
        # Body is shown truncated by default; user clicks the toggle in
        # the header to expand. Cached on the bubble so re-renders keep
        # the expanded/collapsed choice the user made.
        self._expanded: bool = False
        # Latest full-body Rich renderable, kept so the toggle can swap
        # between truncated and expanded views without re-running the
        # tool-specific render pipeline.
        self._last_full_renderable: Any = None
        # How many lines the full body has — used to decide whether to
        # show the toggle at all and to label it (`+N more`).
        self._last_line_count: int = 0

    def compose(self) -> ComposeResult:
        # Header row: name + status + summary on the left, [expand] +
        # [copy] on the right. [expand] is empty until we know there's
        # truncated content to show.
        with Horizontal(id="tc-headline"):
            yield Static(self._header_text(), id="tc-header", markup=True)
            yield Static("", id="tc-expand", markup=False)
            yield Static("[copy]", id="tc-copy", markup=False)
        yield Static("", id="tc-body")
        # ask_user_question: the suggested answers render as their own
        # clickable rows. Clicking one loads it into the input, editable,
        # so the user can pick-and-tweak or write a free-form answer.
        if self._name == "ask_user_question":
            options = _option_list(
                self._args.get("options") if isinstance(self._args, dict) else None
            )
            if options:
                with Vertical(id="tc-options"):
                    for index, option in enumerate(options):
                        yield Static(
                            f"▸ {option}",
                            id=f"tc-opt-{index}",
                            classes="tc-option",
                            markup=False,
                        )
                yield Static(
                    "click an option to load it into the input — edit, then ⏎ to send",
                    id="tc-opt-hint",
                )
        # request_secret: a clickable row opens a masked prompt. The
        # value goes straight to the secret store, never the chat.
        if self._name == "request_secret":
            a = self._args if isinstance(self._args, dict) else {}
            if str(a.get("name") or "").strip() and str(a.get("service") or "").strip():
                yield Static(
                    "▸ enter the secret securely",
                    id="tc-secret-provide",
                    classes="tc-option",
                )
        # Populate body from the start args; result/error replace it later.
        self._update_body(self._render_running_body())

    def on_click(self, event) -> None:  # noqa: ANN001 — Textual Click event
        # Clicks on the right-side controls trigger the matching action;
        # anywhere else is a no-op so users can still interact with
        # terminal selection (Option-click on macOS).
        try:
            target = event.widget
        except Exception:  # noqa: BLE001
            return
        if target is None:
            return
        wid = getattr(target, "id", "")
        if wid == "tc-copy":
            self._copy_body_to_clipboard()
        elif wid == "tc-expand":
            self._toggle_expanded()
        elif wid.startswith("tc-opt-"):
            try:
                self._pick_option(int(wid.rsplit("-", 1)[-1]))
            except ValueError:
                pass
        elif wid == "tc-secret-provide":
            self._open_secret_prompt()

    def _toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self._rerender_body_with_truncation()

    def _pick_option(self, index: int) -> None:
        """Load an `ask_user_question` option into the input, editable.

        The answer is not sent on click — the user can edit or extend
        the suggestion (or replace it entirely) and submit on their own.
        """
        options = _option_list(
            self._args.get("options") if isinstance(self._args, dict) else None
        )
        if not 0 <= index < len(options):
            return
        try:
            from durin.cli.tui.widgets import InputArea

            inp = self.app.query_one(InputArea)
        except Exception:  # noqa: BLE001 - no input to fill (e.g. headless)
            return
        inp.value = options[index]
        inp.cursor_position = len(inp.value)
        inp.focus()

    def _open_secret_prompt(self) -> None:
        """Open the masked prompt for a `request_secret` credential."""
        a = self._args if isinstance(self._args, dict) else {}
        name = str(a.get("name") or "").strip()
        service = str(a.get("service") or "").strip()
        if not name or not service:
            return
        from durin.cli.tui.screens.secret_prompt import SecretPromptScreen

        self.app.push_screen(
            SecretPromptScreen(
                name=name,
                service=service,
                purpose=str(a.get("purpose") or "").strip(),
            ),
            self._on_secret_prompt_done,
        )

    def _on_secret_prompt_done(self, stored: bool | None) -> None:
        """After the masked prompt: tell the agent the secret exists.

        Metadata only — the value never leaves the secret store.
        """
        if not stored:
            return
        a = self._args if isinstance(self._args, dict) else {}
        name = str(a.get("name") or "").strip()
        service = str(a.get("service") or "").strip()
        note = (
            f"The user stored the secret '{name}' (service={service}, "
            f"scope=exec). It is available to your shell commands as "
            f"${name}. Please continue the task."
        )
        publish = getattr(self.app, "_publish_inbound", None)
        if publish is None:
            return
        import asyncio

        task = asyncio.create_task(publish(note, []))
        # Retain a strong ref on the (longer-lived) app so the loop can't GC
        # this fire-and-forget task before it runs (RUF006). The widget is
        # transient; the app outlives it.
        bg = getattr(self.app, "_background_tasks", None)
        if bg is not None:
            bg.add(task)
            task.add_done_callback(bg.discard)

    # ---- lifecycle ----

    def update_from_event(self, event: dict[str, Any]) -> None:
        """Apply a phase=end / phase=error event to this bubble."""
        phase = str(event.get("phase") or "")
        if phase == "end":
            self._status = "ok"
            self.remove_class("running")
            self.add_class("ok")
            self._update_body(self._render_result_body(event.get("result")))
        elif phase == "error":
            self._status = "error"
            self.remove_class("running")
            self.add_class("error")
            err = event.get("error") or "Tool execution failed"
            self._update_body(Text(str(err), style="red"))
        self._refresh_header()

    # ---- header ----

    def _header_text(self) -> str:
        glyph = _STATUS_GLYPH[self._status]
        summary = self._summary_line()
        return f"[bold]{self._name}[/bold]  [dim]{glyph}[/dim]  {summary}"

    def _refresh_header(self) -> None:
        try:
            self.query_one("#tc-header", Static).update(self._header_text())
        except Exception:  # noqa: BLE001
            pass

    def _summary_line(self) -> str:
        """One-line summary of what this call is operating on."""
        a = self._args if isinstance(self._args, dict) else {}
        for key in (
            "path", "file_path", "filename", "url", "query",
            "command", "pattern", "question", "name",
        ):
            value = a.get(key)
            if isinstance(value, str) and value:
                return value if len(value) <= 80 else value[:77] + "…"
        return ""

    # ---- body ----

    def _update_body(self, renderable: Any) -> None:
        """Replace the body content; respect the current expand state.

        The caller passes the FULL renderable. We cache it so the
        expand toggle can swap between truncated / full without
        re-running the tool-specific render pipeline.
        """
        self._last_full_renderable = renderable
        self._last_line_count = _line_count(renderable)
        self._rerender_body_with_truncation()

    def _rerender_body_with_truncation(self) -> None:
        """Render the body in its current (truncated or full) state."""
        full = self._last_full_renderable
        try:
            body = self.query_one("#tc-body", Static)
            toggle = self.query_one("#tc-expand", Static)
        except Exception:  # noqa: BLE001
            return
        total = self._last_line_count
        max_visible = self.PREVIEW_LINES
        if total <= max_visible or self._expanded:
            body.update(full)
            if total <= max_visible:
                toggle.update("")  # nothing to expand
            else:
                toggle.update("[collapse]")
        else:
            body.update(_truncate_to_lines(full, max_visible))
            toggle.update(f"[+{total - max_visible} more]")

    def _render_running_body(self) -> Any:
        """Body shown while the call is still in flight."""
        if self._name == "edit_file":
            a = self._args if isinstance(self._args, dict) else {}
            old = str(a.get("old_text") or "")
            new = str(a.get("new_text") or "")
            return _diff_renderable(old, new)
        if self._name == "exec":
            a = self._args if isinstance(self._args, dict) else {}
            return _exec_renderable(str(a.get("command") or ""), output=None)
        if self._name == "ask_user_question":
            a = self._args if isinstance(self._args, dict) else {}
            return _ask_user_renderable(str(a.get("question") or ""))
        if self._name == "request_secret":
            a = self._args if isinstance(self._args, dict) else {}
            return _request_secret_renderable(a, None)
        # Default: show the args as JSON-ish.
        return _args_text(self._args)

    def _render_result_body(self, result: Any) -> Any:
        """Body shown once the call finishes successfully."""
        result_text = _stringify_result(result)
        self._last_result_text = result_text
        if self._name == "edit_file":
            a = self._args if isinstance(self._args, dict) else {}
            old = str(a.get("old_text") or "")
            new = str(a.get("new_text") or "")
            return _diff_renderable(old, new)
        if self._name == "exec":
            a = self._args if isinstance(self._args, dict) else {}
            return _exec_renderable(
                str(a.get("command") or ""),
                output=result_text,
            )
        if self._name == "ask_user_question":
            a = self._args if isinstance(self._args, dict) else {}
            return _ask_user_renderable(str(a.get("question") or ""))
        if self._name == "request_secret":
            a = self._args if isinstance(self._args, dict) else {}
            return _request_secret_renderable(a, result_text)
        if self._name in ("read_file", "list_dir", "grep"):
            text = _stringify_result(result, limit=800)
            return _linkify(text) if text else _args_text(self._args)
        # Generic: result preview.
        out = _stringify_result(result, limit=400)
        if out:
            return _linkify(out)
        return _args_text(self._args)

    def _copy_body_to_clipboard(self) -> None:
        """Copy the most-recently-rendered body text to the system clipboard."""
        from durin.utils.clipboard import NoClipboardError, copy_text

        # Build a plain-text version of whatever the body currently shows.
        text = self._plain_body_text()
        if not text.strip():
            text = self._summary_line() or self._name
        try:
            copy_text(text)
            self._flash_copied("copied!")
        except NoClipboardError as e:
            self._flash_copied(f"copy failed: {e}")
        except Exception as e:  # noqa: BLE001
            self._flash_copied(f"copy failed: {e!r}")

    def _plain_body_text(self) -> str:
        """Return a plain-text version of the body suitable for clipboard.

        Tool results already passed through ``_stringify_result`` which
        strips decorative icons, so by the time we get here the text is
        already clean.
        """
        if self._name == "exec":
            # For exec, the user wants the output of the command, not
            # the command itself.
            return self._last_result_text
        if self._name == "edit_file":
            a = self._args if isinstance(self._args, dict) else {}
            return f"--- before\n{a.get('old_text', '')}\n+++ after\n{a.get('new_text', '')}"
        if self._last_result_text:
            return self._last_result_text
        # Fallback: extract from the body widget. Static stores its
        # renderable in `_Static__content`.
        try:
            ren = self.query_one("#tc-body", Static)._Static__content  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return ""
        if hasattr(ren, "plain"):
            return ren.plain  # type: ignore[no-any-return]
        return str(ren)

    def _flash_copied(self, label: str) -> None:
        """Briefly replace `[copy]` with feedback, then revert."""
        try:
            target = self.query_one("#tc-copy", Static)
            target.update(label)
            self.set_timer(1.4, lambda: target.update("[copy]"))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _args_text(args: Any) -> str:
    if not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return str(args)


def _line_count(renderable: Any) -> int:
    """Number of visual lines in ``renderable``. Works for str and Text."""
    if renderable is None:
        return 0
    if isinstance(renderable, Text):
        return renderable.plain.count("\n") + 1
    return str(renderable).count("\n") + 1


def _truncate_to_lines(renderable: Any, max_lines: int) -> Any:
    """Return a truncated copy of ``renderable`` with at most ``max_lines``.

    Preserves styling for :class:`rich.text.Text` (uses ``Text.split``
    so spans are clipped instead of stripped). Plain strings are
    cut on newline boundaries.
    """
    if renderable is None:
        return renderable
    if isinstance(renderable, Text):
        lines = renderable.split("\n", allow_blank=True)
        if len(lines) <= max_lines:
            return renderable
        head = Text("\n").join(lines[:max_lines])
        return head
    raw = str(renderable)
    pieces = raw.split("\n")
    if len(pieces) <= max_lines:
        return renderable
    return "\n".join(pieces[:max_lines])


def _stringify_result(result: Any, *, limit: int = 1200) -> str:
    """Convert any tool result to a clean displayable / copyable string.

    Tools sometimes decorate their text output with icons (📁 📄 🔧)
    that are cute but make the result harder to read and useless if
    copied. We strip them uniformly so both render and clipboard get
    the same clean text. Linkification (OSC 8 hyperlinks for URLs +
    paths) happens later, when this text is converted to a
    :class:`rich.text.Text` for display.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        # Common shape: {"output": "...", "files": [...], "embeds": [...]}
        if isinstance(result.get("output"), str):
            text = result["output"]
        else:
            try:
                text = json.dumps(result, ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                text = str(result)
    else:
        text = str(result)
    text = _strip_decorations(text)
    if len(text) > limit:
        return text[:limit] + f"\n… ({len(text) - limit:,} more chars)"
    return text


def _linkify(text: str) -> "Text":
    """Linkify a plain string for display, returning a Rich :class:`Text`.

    Lazy import so the `Text` instance Rich constructs lives only when
    we're actually about to render, not when this module is first read.
    """
    from durin.cli.tui.linkify import linkify

    return linkify(text)


# Decorative glyphs that durin's tools add for visual flair. They look
# nice in the bubble but the user almost never wants them in the
# clipboard (you can't `cd 📁 myproject`). Stripped at copy time only —
# the rendered display keeps them.
_DECORATION_GLYPHS = (
    "📁", "📂", "📄", "📃", "📝", "📊",   # file / folder
    "🔧", "⚙️", "🛠",                      # tools
    "✓", "✗", "✅", "❌", "✔", "✘",        # status
    "●", "○", "◉",                          # bullets
    "▶", "▸", "‣",                          # arrows
    "🔍", "🔎",                              # search
)


def _strip_decorations(text: str) -> str:
    """Strip leading decorative icons from each line, preserving paths.

    Returns text where each line has any leading decoration removed
    (the icon plus any single space that followed it). Whitespace
    indentation is preserved.
    """
    if not text:
        return text
    out_lines = []
    for line in text.splitlines():
        indent_len = len(line) - len(line.lstrip())
        indent = line[:indent_len]
        rest = line[indent_len:]
        for glyph in _DECORATION_GLYPHS:
            if rest.startswith(glyph):
                rest = rest[len(glyph):]
                # Eat at most one separating whitespace char.
                if rest.startswith(" "):
                    rest = rest[1:]
                break
        out_lines.append(indent + rest)
    return "\n".join(out_lines)


def _exec_renderable(command: str, *, output: str | None) -> Text:
    """Render ``exec``: ``$ <command>`` on its own line, then the output.

    No IN/OUT labels — the bubble header already shows the command,
    and the body itself is collapsed to 2 lines by default with an
    ``[+N more]`` toggle. URLs / paths in the output get linkified.
    """
    text = Text()
    text.append("$ ", style="bold cyan")
    text.append(command, style="default")
    if output is None:
        text.append("\n…", style="dim")
        return text
    if not output:
        return text
    text.append("\n")
    text.append(_linkify(output))
    return text


def _option_list(options: Any) -> list[str]:
    """Normalise an `ask_user_question` ``options`` argument to a clean list."""
    if not isinstance(options, (list, tuple)):
        return []
    return [str(o).strip() for o in options if str(o).strip()]


def _ask_user_renderable(question: str) -> Text:
    """Render the question line for ``ask_user_question``.

    Built from the call arguments — the raw tool result is an internal
    ``YIELD TO USER`` instruction the user should never see. The options
    render as separate clickable widgets below the body (see
    :meth:`ToolCallBubble.compose`).
    """
    text = Text()
    text.append("❓ ", style="bold yellow")
    text.append(question or "(no question)", style="default")
    return text


def _request_secret_renderable(args: Any, result: str | None) -> Text:
    """Render ``request_secret``: what is needed and the command to store it.

    The secret value never flows through here — only the request and,
    when the credential is still missing, the exact ``durin secret set``
    command the user runs in their own terminal.
    """
    a = args if isinstance(args, dict) else {}
    name = str(a.get("name") or "").strip()
    service = str(a.get("service") or "").strip()
    purpose = str(a.get("purpose") or "").strip()
    text = Text()
    text.append("🔑 ", style="bold yellow")
    text.append(name or "(unnamed secret)", style="bold")
    if service:
        text.append(f"  · {service}", style="dim")
    if purpose:
        text.append(f"\n   {purpose}", style="default")
    if result and "already exists" in result:
        text.append("\n   already stored — nothing to do", style="green")
    elif name and service:
        text.append("\n   $ ", style="bold cyan")
        text.append(
            f"durin secret set {name} --service {service} --scope exec",
            style="default",
        )
    return text


def _diff_renderable(old: str, new: str) -> Text:
    """Build a Rich `Text` containing a unified diff of `old` → `new`."""
    if not old and not new:
        return Text("(empty edit)", style="dim")
    diff = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            lineterm="",
            n=2,
            fromfile="before",
            tofile="after",
        )
    )
    if not diff:
        return Text("(no change)", style="dim")
    out = Text()
    for line in diff[2:]:  # skip the "--- before / +++ after" header
        if line.startswith("+"):
            out.append(line + "\n", style="green")
        elif line.startswith("-"):
            out.append(line + "\n", style="red")
        elif line.startswith("@@"):
            out.append(line + "\n", style="dim cyan")
        else:
            out.append(line + "\n", style="default")
    return out
