# TUI / WebUI Improvements Roadmap

> Source: gap analysis against durin's own webui + opencode's TUI (June 2026).
> Scope: chat UX, model picker, and interactive primitives.

---

## Phase 1: `/model` cascading picker (TUI)

**Problem**: The model picker (`ModelPickerScreen`) only lists configured presets. No provider exploration, no model metadata, no fuzzy search.

**Design**: Single fuzzy-searchable `OptionList` modal with 3 sections:

### Sections

1. **★ Recent** — last 5 used models (persisted in `~/.durin/tui-state.json`)
2. **Configured** — presets from `config.agents.presets` with inline metadata
3. **Explore** — providers with valid API key → expandable to their model catalog

### Per-model metadata

Each entry shows: `model_name · {ctx}K ctx · reasoning {✓|✗} · {cost}`

Source: `providers/data/model_capabilities.json` (vendored, no API calls).

### UX

- Type to fuzzy-filter (name + provider)
- `↑↓` to navigate, `Enter` to select, `Esc` to cancel
- `→` on a provider in Explore → expands its models inline
- Deprecated models hidden by default; `d` toggles them
- Selecting an explored model creates a temp preset and activates it

### Files

- Modify: `durin/cli/tui/screens/model_picker.py` (rewrite to multi-section + fuzzy)
- New: `durin/cli/tui/widgets/fuzzy_option_list.py` (reusable fuzzy-filtered OptionList)
- Modify: `durin/command/builtin.py` (`cmd_model` — no change needed, Ctrl+L already opens picker)

### Non-goals

- No live vendor API calls (use vendored catalog only)
- No provider configuration inside the picker (that's `durin onboard`)
- No model variant/effort picker (deferred — needs `dialog-variant.tsx` equivalent)

---

## Phase 2: AgentActivityCluster (TUI)

**Problem**: During an agent turn, the TUI stacks separate bubbles for every reasoning chunk and tool call. The chat floods with intermediate steps that should be collapsible.

**Design**: Group consecutive reasoning + tool-call bubbles into a single collapsible container during the active turn.

### Behavior

- **During turn**: shows a header line `⠋ Working… · {N} steps · {M} tool calls` with a collapsed body
- **Expand** (`→` or click): reveals the reasoning/tool bubbles inside
- **After turn ends**: collapses to a summary line `✓ Done · {N} steps · {M} tools` (expandable to review)
- User toggle state persists for the cluster's lifetime

### Webui reference

`AgentActivityCluster.tsx` (`webui/src/components/thread/AgentActivityCluster.tsx`):
- Groups `isReasoningOnlyAssistant` + `kind === "trace"` messages
- Fixed max height with inner scroll (`max-h-52`)
- Summary: "Working… · {reasoning} steps · {tools} tool calls"

### TUI implementation

- New container widget wrapping existing `MessageBubble`/`ToolCallBubble` instances
- The app's `_consume_outbound` routes reasoning/tool events into the active cluster instead of stacking them directly on ChatView
- CSS: left border accent, dim header, collapsible body

### Files

- New: `durin/cli/tui/widgets/activity_cluster.py`
- Modify: `durin/cli/tui/app.py` (routing logic in `_consume_outbound`)

---

## Phase 3: Prompt history (TUI)

**Problem**: No way to recall previous prompts. Terminal users expect Up/Down arrow history.

**Design**: Persistent prompt history with Up/Down recall.

### Behavior

- `↑` on empty input → recall last prompt
- `↑↑` → go further back (max 50 entries)
- `↓` → go forward
- History persists across sessions in `~/.durin/tui-state.json` under `prompt_history`
- Only submitted prompts are stored (not drafts)

### opencode reference

`prompt/history.tsx`: JSONL-backed, 50 entries max, up/down arrow recall.

### Files

- New: `durin/cli/tui/prompt_history.py`
- Modify: `durin/cli/tui/widgets/input_area.py` (key bindings for ↑/↓ on empty input)
- Modify: `durin/cli/tui/app.py` (store on submit, load on mount)

---

## Phase 4: Error cards (TUI)

**Problem**: Provider errors render as plain text. They should be visually distinct.

**Design**: Detect `Error: …` pattern in assistant messages and render with red border + error styling.

### Webui reference

`ErrorCard` in `MessageBubble.tsx`: detects `looksLikeProviderError(content)` via regex, renders as red-tinted card.

### TUI implementation

- In `_consume_outbound`, check if finalized assistant bubble content matches error pattern
- If yes, swap CSS class to error variant (red border, error color)
- Pattern: `/^\s*Error(\s*\(|:\s| calling )/`

### Files

- Modify: `durin/cli/tui/widgets/chat_view.py` (error CSS class + detection)
- Modify: `durin/cli/tui/app.py` (apply class on stream end)

---

## Phase 5: Stop button visibility (TUI)

**Problem**: Escape aborts but there's no visual affordance.

**Design**: Show `[Esc to stop]` in the WorkingIndicator while a turn is active.

### Files

- Modify: `durin/cli/tui/widgets/working_indicator.py` (append hint text)

---

## Phase 6: Toast notifications (TUI)

**Problem**: No transient feedback mechanism (copy success, save success, errors).

**Design**: Top-right toast widget that auto-dismisses after 2s.

### opencode reference

`toast.tsx`: info/success/warning/error variants, auto-dismiss, positioned top-right.

### TUI implementation

- New widget overlaying the ChatView (absolute positioning via Textual)
- API: `app.toast("Copied!", level="success")`
- Auto-dismiss timer, stack up to 3 toasts

### Files

- New: `durin/cli/tui/widgets/toast.py`
- Modify: `durin/cli/tui/app.py` (mount toast container, expose `toast()` method)

---

## Phase 7: File picker button (Webui)

**Problem**: Drag-and-drop and paste work, but there's no visible button to attach files. Users on desktop without drag habits may not discover the feature.

**Design**: Add a `📎` button next to the send button in `ThreadComposer` that opens the OS file picker.

### Files

- Modify: `webui/src/components/thread/ThreadComposer.tsx` (add file input button)

---

## Deferred (high value, high investment)

These are documented for future planning but not in scope for the current cycle:

| Feature | Description | Complexity |
|---|---|---|
| **Full diff viewer (TUI)** | `Ctrl+D` route with file tree, hunk navigation, split/unified view. opencode has 1059 lines. | Very high |
| **Command palette (TUI)** | `Ctrl+P` fuzzy-searchable list of all commands + keybindings. | High |
| **Sidebar panels (TUI)** | TODO list, modified files, MCP status — live panels. | High |
| **Which-key popup (TUI)** | Contextual keybinding hints popup. | Medium |
| **Prompt stash (TUI)** | Save draft before switching context, restore later. | Medium |
| **Frecency @file (TUI)** | Frequency+recency ranking for `@file` suggestions. | Low |
| **Latency footer (TUI)** | Show `{ms}` at the foot of each assistant reply. | Low |
| **Model variant picker** | Reasoning effort levels (`high`/`max`) selection. | Medium |

---

## Priority order

1. **Phase 1**: `/model` cascading picker — highest UX impact, self-contained
2. **Phase 2**: AgentActivityCluster — transforms the chat experience during turns
3. **Phase 3**: Prompt history — expected terminal UX, quick win
4. **Phase 4**: Error cards — small effort, good polish
5. **Phase 5**: Stop button — trivial, do alongside Phase 2
6. **Phase 6**: Toasts — infrastructure for other features
7. **Phase 7**: File picker button — webui polish
