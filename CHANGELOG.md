# Changelog

User-facing changes per release, newest first. Each release also ships these
notes as a [GitHub Release](https://github.com/mmarmol/durin/releases).
Entries are curated at release time from the merged pull requests since the
previous tag — highlights first, then changes grouped by area.

## 0.3.0 — 2026-07-18

### Highlights

- **Script nodes can authenticate:** a workflow script node declares the stored
  secrets it needs (`"secrets": ["ZENDESK_API_TOKEN"]`) and they arrive as
  environment variables — so an authenticated `curl` stays a zero-token,
  instant script step instead of becoming a full agent turn. Injection requires
  the secret's `exec` scope grant, unresolvable names abort the run pre-flight
  naming the node, and script output is redacted against the secret store so a
  leaked credential can never persist into sessions or run records. (#399)
- **Workflows declare what they produce:** the output descriptor accepts an
  `artifacts` list — the files the run promises to leave in its working folder.
  Every node sees the contract while working, and promised files missing after
  completion are reported as a warning in the result, the manifest, and
  `tasks(status)`, so a composed pipeline learns the gap immediately instead of
  failing confusingly downstream. (#399)
- **No more sleep+status babysitting:** background workflow results were always
  push-delivered as a follow-up message, but the tool guidance taught the agent
  to poll with sleep+status loops — blocking the chat for minutes. The guidance
  now teaches the real contract (report the run, end the turn, the follow-up
  wakes you), and a deterministic backstop makes `sleep` remind the agent about
  running push-delivered work at wake time, correcting a polling loop on its
  first iteration. (#399)
- **Mid-run visibility for workflow runs:** the run manifest records the shared
  working folder from the first write plus per-node durations, and
  `tasks(status)` renders the folder path, each node's latest-pass duration,
  and a listing of the folder's current files — a live window onto a run's
  artifacts while it executes. (#399)

### Changes

- **Workflows:** script-node `secrets` field in the visual editor; declared
  artifacts editable on the Output canvas object (one `path | description` per
  line); secret-resolution errors point the agent at the `workflows` skill;
  `run_workflow`'s description now names multi-way `cases` routing and the
  `__needs_input__` terminal. (#399)
- **Skills:** the `workflows` skill teaches the background waiting contract,
  script-node secrets, and the declared-artifacts contract across its overview,
  authoring schema, and patterns. (#399)
- **Web UI:** scalable type-filter popover for the memory Entities toolbar.
  (#398)
- **CLI:** `durin status` counts entities, Library documents, and fragments
  separately. (#397)

## 0.2.0 — 2026-07-18

First stable release. Highlights: the memory Entities view family (graph,
cards, table) with Obsidian-style gestures and camera controls, MCP OAuth
tokens surviving gateway restarts, and session-entity graph edges drawn from
page provenance. Full pull-request list:
[v0.2.0 release notes](https://github.com/mmarmol/durin/releases/tag/v0.2.0).
