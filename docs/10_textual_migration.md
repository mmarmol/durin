# Textual TUI Migration — D5

> The Daily Driver plan (docs/09) noted that a real "rich app" feel —
> persistent layout regions, differential rendering, mouse, modal
> screens — needs a full TUI framework, not just polish on top of
> prompt_toolkit. This doc owns that migration end-to-end so the
> work doesn't lose feature equivalence with the current CLI.

---

## 0. Context

Current state (post-D1):
- prompt_toolkit drives input + completers + bottom_toolbar.
- Rich renders message bodies via `_render_interactive_ansi` and `StreamRenderer`.
- Everything is linear stdout. Scroll is the terminal's job, not the app's.
- 21 slash commands fully wired through `MessageBus` → `CommandRouter`.

What's not convincing: structurally it's a REPL, not an app. No real
panels, no modal overlays, no mouse, no consistent visual hierarchy.

Target: [Textual](https://textual.textualize.io/) (8.x). Python's only
mature TUI framework that gives differential rendering + reactive
widgets + CSS-like styling + async-native event loop. Closest Python
analog to pi-tui (which is Node).

**Migration strategy: dual-mode, opt-in first.** The new TUI ships as
a separate entry point (`durin agent --tui`). The legacy mode stays
exactly as it is. Once the new mode reaches feature parity and gets
real use, the legacy path is retired.

---

## 1. Feature parity inventory (must NOT lose)

The migration is scoped against this list. Each item maps to a sub-task.

### 1.1 Modes

- [ ] Interactive REPL (D5.1 → D5.11)
- [ ] Print mode `-m "msg"` (unchanged — keeps current implementation)
- [ ] Logs visibility (`--logs`) (D5.10)

### 1.2 Input

- [ ] Single-line submit (Enter) (D5.2)
- [ ] History (up/down arrows) (D5.2)
- [ ] Drag-and-drop image/audio/doc → `.media/<sha>.<ext>` (D5.6)
- [ ] `@file` fuzzy completion (D5.8)
- [ ] `/command` prefix triggers slash dispatch (D5.4)
- [ ] Ctrl+L model picker (D5.5 as modal)
- [ ] Esc abort current task (D5.7)
- [ ] Ctrl+C clear / double Ctrl+C exit (D5.7)
- [ ] Ctrl+D / `exit` / `:q` / `/quit` quit (D5.7)

### 1.3 Output

- [ ] Streaming token-by-token (D5.3)
- [ ] Markdown rendering of assistant messages (D5.3)
- [ ] Code block syntax highlighting (D5.10 — Rich does it natively)
- [ ] Tool call progress indicators (D5.3)
- [ ] Reasoning content (italic, collapsible) (D5.3)
- [ ] User message echo (D5.3)
- [ ] System errors (red panel) (D5.3)

### 1.4 Status / chrome

- [ ] Persistent footer: session · model · tokens · context % · mem · vec (D5.2)
- [ ] Header: identity, channel, cwd (D5.2)
- [ ] Banner on first launch (logo + version) (D5.1)
- [ ] Terminal restore on exit (D5.1 — Textual handles this)
- [ ] Signal handling SIGINT/SIGTERM/SIGHUP (D5.1)
- [ ] Surrogate sanitization for emoji-rich input (D5.6)

### 1.5 Slash commands (all 21)

- [ ] /new /stop /restart /status /model /history /goal (D5.4)
- [ ] /dream /dream-log /dream-restore (D5.4)
- [ ] /help /pairing /plan /build /mode (D5.4)
- [ ] /sessions /resume /compact /copy /name /hotkeys (D5.4 + D5.5)

### 1.6 Modal overlays (new in TUI, replaces inline output for pickers)

- [ ] /sessions → SessionPicker modal (arrow keys + filter input) (D5.5)
- [ ] /model + Ctrl+L → ModelPicker modal (D5.5)
- [ ] /audit, /memory list (when D2 lands) → MemoryPicker modal (D5.5)

### 1.7 Theming

- [ ] Dark mode default (D5.10)
- [ ] Light mode flag (D5.10)
- [ ] Colors tied to durin's identity (D5.10)

### 1.8 Async + bus integration

- [ ] InboundMessage publishes from input submit (D5.3)
- [ ] OutboundMessage consumed via worker task (D5.3)
- [ ] Session switch via `_switch_chat_id` directive (D5.3)
- [ ] Cancellation propagates to running tasks (D5.7)

---

## 2. Sub-tasks

### D5.1 — Textual scaffolding + opt-in (~1-2 days)

- Add `textual>=8.2.0,<9.0.0` to pyproject.toml.
- Create `durin/cli/tui/` package: `__init__.py`, `app.py`.
- `DurinApp(App)` minimal: shows logo placeholder, Ctrl+Q quits.
- Add `--tui` flag to `durin agent`. Without it, legacy CLI runs.
- Smoke: `durin agent --tui` launches, shows placeholder, quits clean.
- ARCHITECTURE.md placeholder for §11.2 (full update in D5.12).

### D5.2 — Layout widgets + chrome (~2-3 days)

- `Header` widget: durin logo + workspace cwd + model preset.
- `ChatView` widget: scrollable container for message bubbles.
- `MessageBubble` widget: role-stamped (user/assistant/tool/system).
- `InputArea` widget: single-line Input + history binding.
- `FooterBar` widget: reuses `build_footer_text` payload.
- `durin.tcss` for layout + spacing + colors.

### D5.3 — Streaming + bus integration (~2-3 days)

- Worker task consumes `bus.consume_outbound`.
- Stream deltas → append to current `MessageBubble`'s reactive text.
- Stream end → finalize, render markdown.
- Tool call begin/end → ProgressBubble below current message.
- Reasoning → CollapsibleBubble (default collapsed).
- `_switch_chat_id` metadata mutates a reactive `current_chat_id`
  that propagates to Header + Footer + next inbound publish.

### D5.4 — Slash command surface (~1-2 days)

- `InputArea.on_submit`: if input starts with `/`, dispatch via
  the existing `CommandRouter` plumbing.
- Render command response as a system MessageBubble.
- Slash autocomplete dropdown via `Suggester` (read from
  `BUILTIN_COMMAND_SPECS`).

### D5.5 — Modal pickers (~2 days)

- `SessionPickerScreen(ModalScreen)`: SelectionList of saved sessions
  with live filter input. Enter dispatches `/resume <key>`.
- `ModelPickerScreen(ModalScreen)`: SelectionList of `model_presets`.
  Enter dispatches `/model <preset>`.
- Hooks: `/sessions` opens SessionPicker; Ctrl+L or `/model` opens
  ModelPicker.
- Cancellation closes the modal without side-effect.

### D5.6 — Drag-and-drop integration (~1 day)

- `InputArea.on_submit` pre-processes text via
  `durin.cli.dragdrop.process_dragged_paths` (reuse intact).
- Media list rides on `InboundMessage.media`.
- Surrogate sanitization runs before dragdrop (reuse
  `_sanitize_surrogates`).

### D5.7 — Key bindings (~1 day)

- `BINDINGS` on `DurinApp`:
  - `escape` → emit cancel event → call `loop._cancel_active_tasks`
    for current session.
  - `ctrl+c` → clear input; if input is empty, exit.
  - `ctrl+l` → open ModelPickerScreen.
  - `ctrl+q` → exit cleanly.
  - `ctrl+t` → toggle theme (dark / light).

### D5.8 — `@file` completion (~1-2 days)

- `Suggester` over `FileReferenceCompleter.scan_files`.
- Triggered when input has `@<prefix>` at the end of the buffer.
- Renders matches in a popup; Tab to accept first; arrows to navigate.

### D5.9 — Print mode left intact (~0 days)

- The `-m` flag continues to use the existing code path. The TUI app
  is opt-in via `--tui`.

### D5.10 — Theme + polish (~1 day)

- `durin.tcss` with palette derived from durin's identity colors.
- Code block styling (Rich + Pygments via Textual's built-in).
- Tool result collapsing (`Ctrl+O` toggles).
- Reasoning collapsing (`Ctrl+T` toggles).
- Light theme variant.

### D5.11 — Tests (~2 days)

- Snapshot tests for widgets via Textual's test harness.
- Pilot interaction tests: simulate typing, slash command, picker open,
  drag-and-drop pre-processor.
- Smoke test: app launches against a mocked AgentLoop and quits clean.

### D5.12 — ARCHITECTURE.md §11 rewrite + smoke + PR + tag (~1 day)

- Document the new TUI architecture (widgets, screens, bindings).
- E2E smoke harness comparable to `/tmp/d1_e2e.py`.
- PR + merge + tag `textual-migration` (or `d5`).

**Total estimate**: 17-21 days focused work, realistically 3 weeks
with testing + tweaks.

---

## 3. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Textual's async model conflicts with bus consumption | Use Textual's `run_worker` / `post_message` — designed for this |
| Streaming chunks land in wrong widget after race | Use a `current_message_id` reactive attribute; deltas tagged with id |
| Modal screens break key-binding propagation | Test modal Esc + bg input independently |
| Existing legacy CLI bit-rots while focus is on TUI | Keep both running; legacy path unchanged through D5.12 |
| Theme drift from current durin look | Lock down colors in `durin.tcss` early (D5.2) |
| Test coverage on TUI is hard | Snapshot tests + manual smoke. Don't try to e2e-test mouse |

---

## 4. Out of scope

- Mouse drag for resizing panels (Textual supports it but extra work).
- Inline image preview (terminals support it via Sixel / Kitty graphics
  but adapter work is significant).
- Real-time multi-pane chat (one chat at a time per app instance).
- Cross-platform keybinding compatibility tweaks beyond defaults.

---

## 5. Order of execution

Linear: D5.1 → D5.2 → D5.3 → D5.4 → D5.5 → D5.6 → D5.7 → D5.8 → D5.10 → D5.11 → D5.12.

D5.9 is a no-op (legacy print mode untouched). D5.8 can run in
parallel with D5.10 if there's split bandwidth.

Branch: `textual-migration` off `main`. One PR at the end after D5.12.

---

## Last updated: 2026-05-20
