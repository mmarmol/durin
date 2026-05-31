# Plan: P12 — Memory entries browser + Obsidian-aware renderer

**Status**: planned, not started.
**Created**: 2026-05-31.
**Closes**: backlog P12, plus partial debt (VAULT_README.md promises a
`durin memory forget` CLI that doesn't exist).

---

## Goal

Operators can browse / read / archive individual memory entries
(episodic, stable, corpus, session_summary) from the web UI, without
dropping to the CLI. While we're at it, the markdown renderer learns
Obsidian's `[[wikilink]]` syntax so source_refs and related fields
render as clickable references — matches what the on-disk vault
already looks like in Obsidian itself.

Three deliverables:

1. **Backend**: read + archive endpoints for individual entries +
   backlinks lookup.
2. **Renderer**: `remark-wiki-link` plugged into the existing
   `MarkdownTextRenderer` so `[[memory/...]]` becomes a real link.
3. **UI**: new "Entries" tab inside the MemoryGraphView side panel
   when an entity is selected.

Plus the CLI gap: `durin memory forget <uri>` — promised by
`VAULT_README.md` but missing.

---

## Scope

### In scope
- Browse entries that reference a selected entity (filter by
  `entities` field on the entry's frontmatter).
- Read full body + frontmatter of any entry the operator opens.
- Archive an entry (moves to `memory/archive/<class>/<id>.md`, reuses
  existing `archive_episodic` for episodic + a new generic helper
  for stable/corpus/session_summary).
- Wikilink resolution in markdown: `[[memory/episodic/abc]]` →
  clickable link that selects the target node/entry in the same view.
- Backlinks panel: "this entry is referenced by N other entries"
  (grep through `source_refs` and `related` frontmatter fields, plus
  body wikilinks).
- CLI `durin memory forget <uri>` parity with what VAULT_README claims.

### Out of scope (defer)
- Standalone "Entries" Settings section with global browse (top-level
  search across all entries decoupled from any entity). Memory Graph
  contextual browse covers the daily-driver case.
- Obsidian callouts (`> [!info] title`), embeds (`![[uri]]`),
  Dataview queries, transclusion. Not used heavily in our entries
  yet; if they become common, drop in `remark-obsidian` (one extra
  plugin) — leave a comment in the renderer pointing to that path.
- Editing entries from the UI. Read + archive only. The agent's
  `memory_store` tool stays the canonical write path.
- Frontmatter property pills (rendered as colored badges). Plain
  `key: value` rendering is fine for v1.

---

## Architecture notes

### Why "extend the MemoryGraph drawer" instead of a new top-level section

- The Memory Graph already loads + selects entities, exposes a side
  drawer with tabs (Info / Body / History / Sources / Archive). Adding
  "Entries" alongside reuses the drawer chrome, the selection state,
  the close handlers — and the operator's mental model is already
  "I'm looking at this entity, show me what's around it".
- Reused: `MemoryGraphView` selection state, side drawer layout,
  `getModelCapabilities`-style fetcher pattern, `MarkdownTextRenderer`.

### Storage / classes recap

- `memory/episodic/<id>.md`: observations from conversations.
- `memory/stable/<id>.md`: long-lived facts.
- `memory/corpus/<id>.md`: chunks of ingested documents.
- `memory/session_summary/<id>.md`: per-session digests.
- `memory/entities/<type>/<slug>.md`: canonical entity pages (NOT
  this scope — those already render in the graph drawer).
- `memory/archive/<class>/<id>.md`: target for forget.

URIs the UI passes around: `memory/<class>/<id>` (no `.md` suffix,
consistent with what `memory_search` returns today).

### Wikilink format we emit / expect

On-disk, our frontmatter uses `[[memory/episodic/abc123]]` (see
`durin/memory/vault_readme.py` + how Dream writes source_refs).
`remark-wiki-link`'s default Obsidian-shortestPossible mode matches
this fine: the wiki link target is the path; the renderer turns it
into an `<a href>`.

We'll wire `onClick` interception on the rendered `<a>` so it
dispatches into the MemoryGraphView's `selectNode(uri)` instead of
doing a real navigation.

### Why `remark-wiki-link` and not `remark-obsidian`

- `remark-wiki-link` (flowershow): mature, narrow, just wikilinks.
  Maintained, used by Quartz and others.
- `remark-obsidian` family: multiple forks, all small, all add
  features we don't need yet (callouts, embeds). YAGNI for v1.

If wikilink-only becomes restrictive later, swap in `remark-obsidian`
in one line — same `react-markdown` plugin slot.

---

## Phases

### Phase 0 — CLI `durin memory forget` (cleanup of pre-existing debt)

**Why first**: the VAULT_README promises this command. Frontend will
display a "Or run `durin memory forget <uri>`" hint in the archive
confirmation tooltip; that hint must be true.

**Files**:
- `durin/cli/memory_cmd.py`: new `@memory_app.command("forget")`
  function. Accepts `<uri>` positional. Resolves to filesystem path
  (`memory/<class>/<id>.md`), moves to `memory/archive/<class>/<id>.md`,
  best-effort drops vector index row + FTS row.
- `tests/cli/test_memory_forget.py`: smoke test (creates tmp
  workspace, calls forget, asserts file moved + indexes pruned).

**Acceptance**:
- `durin memory forget memory/episodic/abc123` exits 0 when the
  entry exists, moves the file, removes vector/FTS rows.
- Exits 1 with a clear message when the entry doesn't exist.
- Refuses to forget `memory/entities/...` paths (entity pages have
  their own absorb/revert lifecycle).

---

### Phase 1 — Backend: 3 endpoints

**Files**:
- `durin/channels/websocket.py`: three new handlers + route entries.

**Endpoints**:

#### `GET /api/memory/entry?uri=memory/<class>/<id>`
Returns:
```json
{
  "uri": "memory/episodic/abc",
  "class_name": "episodic",
  "frontmatter": { "headline": "...", "valid_from": "...",
                   "entities": ["person:marcelo"],
                   "source_refs": ["[[memory/...]]"], ... },
  "body": "raw markdown body",
  "exists": true
}
```
404 when the file doesn't exist. Implementation: walk
`workspace/memory/<class>/<id>.md`, parse frontmatter via
`durin.memory.storage.load_entry`.

#### `GET /api/memory/forget?uri=memory/<class>/<id>`
Returns:
```json
{ "result": "archived" | "not_found" | "protected" }
```
"protected" when uri starts with `memory/entities/` — entities use
absorb/revert lifecycle. For episodic, calls existing
`archive_episodic(workspace, path, into_uri=None, reason="user_forget")`.
For stable/corpus/session_summary, mirrors the same move pattern
(write a thin helper `archive_generic_entry` if needed).

#### `GET /api/memory/backlinks?uri=memory/<class>/<id>`
Returns:
```json
{
  "uri": "memory/episodic/abc",
  "backlinks": [
    { "uri": "memory/episodic/def", "context": "field: source_refs",
      "headline": "..." },
    ...
  ]
}
```
Implementation: walk `memory/**/*.md`, grep frontmatter (`source_refs`,
`related`) AND body for `[[<uri>]]`. Cap at 50 results, paginated
later if needed. Synchronous walk is fine — workspaces have O(thousands)
of entries max in normal operation, well under 100ms.

**Tests**:
- `tests/channels/test_memory_entry_endpoints.py`: 3 endpoints + auth
  + protected paths + 404 paths.

**Acceptance**:
- All three endpoints return valid JSON under the existing API token
  check.
- Backlinks correctly finds wikilink references in both frontmatter
  and body.
- forget on an `entities/...` URI returns `protected` with HTTP 403.
- forget removes vector + FTS rows best-effort (don't fail the
  archive if index removal fails — log and continue).

---

### Phase 2 — Markdown renderer: wikilinks

**Files**:
- `webui/package.json`: add `remark-wiki-link` dependency.
- `webui/src/components/MarkdownTextRenderer.tsx`: import +
  configure the plugin. Hook component override on `<a>` to
  intercept wikilink clicks.

**Implementation sketch**:
```tsx
import wikiLinkPlugin from "remark-wiki-link";

<ReactMarkdown
  remarkPlugins={[
    remarkGfm,
    remarkMath,
    [wikiLinkPlugin, {
      hrefTemplate: (permalink) => `#memory/${permalink}`,
      wikiLinkClassName: "wiki-link",
      // Obsidian-style shortestPossible matching is default.
    }],
  ]}
  components={{
    a: ({ href, children, ...rest }) => {
      if (href?.startsWith("#memory/")) {
        return (
          <a
            href={href}
            onClick={(e) => {
              e.preventDefault();
              onWikiLinkClick?.(href.slice(1)); // "memory/<class>/<id>"
            }}
            className="wiki-link"
          >
            {children}
          </a>
        );
      }
      return <a href={href} {...rest}>{children}</a>;
    },
  }}
>
  {children}
</ReactMarkdown>
```

The `onWikiLinkClick` prop is **new**: the renderer doesn't know about
the MemoryGraphView selection state, so the caller injects a handler.
Default (no handler) → wikilinks render as plain anchors with the `#memory/...`
fragment, no navigation.

**Files updated to pass the handler**:
- `MemoryGraphView.tsx` (when rendering entry/entity body in the
  drawer): pass `onWikiLinkClick={selectNode}`.
- Other callers of `MarkdownTextRenderer` (chat bubbles, tool
  blocks) leave the prop unset — wikilinks in chat would be unusual
  and we don't want to hijack them.

**Tests**:
- `webui/src/tests/markdown-wikilinks.test.tsx`: render a body with
  `[[memory/episodic/abc]]`, assert the rendered `<a>` has the right
  class + href, click triggers `onWikiLinkClick("memory/episodic/abc")`.

**Acceptance**:
- `[[memory/episodic/abc]]` in a rendered body shows up as a link
  with the `wiki-link` class.
- Click on that link dispatches `onWikiLinkClick` when provided.
- Plain links (`[text](https://...)`) keep working as before — no
  regression.

---

### Phase 3 — UI: Entries tab in MemoryGraphView drawer

**Files**:
- `webui/src/components/MemoryGraphView.tsx`: extend the existing
  drawer tabs (currently Info / Body / History / Sources / Archive
  for entity selections). Add an "Entries" tab that's available for
  EVERY selected node, not just entities.
- `webui/src/lib/api.ts`: three new client functions
  (`fetchMemoryEntry`, `forgetMemoryEntry`, `fetchMemoryBacklinks`).

**Behaviour**:

When the operator selects a node in the graph:

- For an **entity** node (existing path): "Entries" tab lists all
  episodic/stable/corpus/session_summary that have this entity in
  their frontmatter `entities` field. Use the existing
  `searchMemoryApi` with the entity ref as query, or a dedicated
  `/api/memory/entries?entity=person:marcelo` filter endpoint if
  search isn't precise enough.
- For an entry node (less common — only if we expose entries as
  graph nodes later): show backlinks + frontmatter + body.

Each entry row in the tab:
- Headline + valid_from + class badge.
- Click → loads `fetchMemoryEntry(uri)` into a side sub-panel:
  frontmatter table, body (rendered with the new wikilink-aware
  renderer), backlinks panel, Archive button.

**Archive button confirmation**: small inline confirm (no modal — same
pattern as the delete-session inline confirm). On confirm:
`forgetMemoryEntry(uri)` → on success, remove the row from the list
and clear the sub-panel.

**Tests**:
- `webui/src/tests/memory-entries-tab.test.tsx`: render the tab,
  click an entry, click Archive, confirm, assert row removed.

**Acceptance**:
- Selecting an entity shows "Entries" tab with the right contextual
  list.
- Clicking an entry opens its detail in the sub-panel.
- Wikilinks in the body are clickable and navigate to the target
  entry (recursive, until the operator hits an entry without
  wikilinks).
- Archive button removes the entry from disk (verified by re-loading
  the list).
- Backlinks panel populated and clickable.

---

### Phase 4 — i18n + docs polish

**Files**:
- `webui/src/i18n/locales/{en,es,id}/common.json`: keys for tabs,
  buttons, empty states, confirmations.
- `durin/memory/vault_readme.py`: now that `durin memory forget`
  exists, the README hint is honest. No change needed but verify
  the wording matches reality.

**Estimated key count**: ~15 per locale.

---

## Dependencies

### Frontend (npm)
- `remark-wiki-link` (likely ^2.x as of 2026-05). Sub-15kb gzipped,
  zero runtime deps beyond unified.

### Backend (Python)
- None new. Reuses `durin.memory.storage`, `durin.memory.archive`,
  existing vector/FTS removal helpers.

---

## Risks

| Risk | Mitigation |
|---|---|
| `remark-wiki-link`'s "shortestPossible" matching disagrees with how Dream emits wikilinks (it emits full `memory/<class>/<id>` paths) | Pass plugin option to disable shortestPossible — use full-path matching only. Test with real Dream output before shipping. |
| Backlinks grep is O(N) over all entries; large vaults (10k+ entries) slow down | Cap at 50 results, add a cursor param later. For v1, assume <2000 entries (current size). |
| Archive endpoint races with Dream's archive_consumed (both writing to memory/archive/) | Both go through `archive_episodic` which is atomic file move. No corruption risk, just possible "already archived" 404 — handle it gracefully. |
| Clicking a wikilink to a non-existent entry (broken link) | Show a "not found" toast/inline message instead of an empty drawer. |
| `remark-wiki-link` not updated for react-markdown v9 | Verify peer compat before installing. If incompatible, fall back to a tiny inline parser (~30 LOC) using a remark regex visitor. |

---

## Success criteria (post-merge)

1. From the Memory Graph, select an entity → click "Entries" tab →
   see a contextual list of memory entries that mention that entity.
2. Click an entry → drawer shows frontmatter, body (with wikilinks
   rendered as links), backlinks list.
3. Click a wikilink in a body → navigate to the linked entry (the
   drawer updates to show it).
4. Click "Archive" on an entry → entry disappears from list, file
   moves to `memory/archive/<class>/<id>.md` on disk.
5. `durin memory forget memory/episodic/abc123` exits 0 on a real
   episodic, moves the file, prunes vector/FTS rows.
6. Webui suite passes (existing 142 + new test files); backend
   suite passes (existing 1100+ + new endpoint tests).

---

## Execution order

Phases are deliberately ordered so each is verifiable in isolation
(can ship/commit each as a standalone PR if we want, or roll up):

1. Phase 0 — CLI forget (cleanest first; closes README debt).
2. Phase 1 — 3 backend endpoints (with tests; no UI change yet).
3. Phase 2 — Markdown renderer wikilinks (with tests; no UI hookup
   yet — wikilinks render as plain anchors).
4. Phase 3 — UI tab + wire-up.
5. Phase 4 — i18n + docs.

Each phase is its own commit (per project convention — easy to
revert single steps if a regression shows up).

---

## Estimated effort

- Phase 0: ~30 min
- Phase 1: ~1.5 h (3 endpoints + tests)
- Phase 2: ~1 h (plug-in + handler + test)
- Phase 3: ~2 h (tab + sub-panel + archive flow + test)
- Phase 4: ~30 min (i18n keys)

Total: ~5–6 h with verification + reinstall + visual check.

---

## What we explicitly are NOT doing here

- Full Obsidian-format coverage (callouts, embeds, Dataview).
  Wikilinks-only for v1.
- Editing entries from the UI.
- Frontmatter pretty rendering (pills/badges). Plain table for v1.
- Standalone "Entries" Settings section. The MemoryGraph drawer is
  the entry point.
- Bulk operations (select N entries, archive all). One-at-a-time
  for v1.
- TUI panel for entries. CLI `durin memory forget` is the TUI-side
  surface; the rich browse stays web-only.

Any of the above can be follow-up items if a real use case appears.
