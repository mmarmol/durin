---
name: automations
description: How durin's automations work and — above all — when an automation earns its place over a cron job, a workflow run, or just doing the task now. Load before reaching for the `automations` tool, before creating an automation for a user, or when a request smells like standing work ("every time a mail like X arrives…", "keep chasing Y until it's paid", "watch this and escalate if…"). Covers the anatomy (triggers, delivery, help, an optional life condition), the single-case doctrine, how a run pauses for a reply and resumes, chaining one automation's outcome into another, and the value test — an automation is for work that STANDS: the clock or an event re-activates it, not this conversation.
---

# Automations — standing triggers that run a workflow and deliver the result

An **automation** is a persistent definition — triggers, a workflow body, delivery/help
routing, an optional life condition — that fires **runs**. Each run executes the workflow
once, classifies the result (`achieved`, `completed`, `failed`, `rejected`, or `paused` if
the workflow asks a question), delivers it per policy, and — when the automation carries a
**life** condition — checks whether that condition is now met, disabling the automation the
moment it is.

## When an automation earns its place — the value test

Default to doing the task now, in this conversation. An automation only wins when the work
is **standing**: it outlives this chat and must re-activate on events or a clock you will
not be around to watch. At least one of these must hold:

- **Event-driven recurrence** — "each time a matching message or webhook arrives, run the
  workflow for it" (support requests by mail, alerts from a monitoring hook).
- **Chained follow-through** — one automation's outcome should itself set off another
  (a `chain` trigger), so a pipeline of standing work reacts to itself, not just to the
  outside world.
- **Scheduled pursuit of a verifiable condition** — a schedule trigger paired with a
  **life** condition, where each firing pursues something until it is achieved, not merely
  executes and forgets.

## When NOT to use an automation

- **The user wants it done now** — just do it; an automation adds definition overhead and
  removes the work from the conversation.
- **Pure scheduling** — "run X every morning" with nothing to verify and no delivery
  policy worth configuring: that is a **cron job** (see the `cron` skill). An automation's
  schedule trigger earns its keep on top of a cron job only via triggers beyond the clock,
  delivery/help routing, or a life condition — not as a fancier cron.
- **A retry wrapper** — an automation does not blindly re-run a failed workflow by itself;
  a failing run is classified `failed` and follows the delivery/help policy like any other
  outcome. If a task needs internal retries or gates, that structure belongs *inside* its
  workflow graph.
- **A sustained objective within this chat** — that is `long_task` (see the `long-goal`
  skill): same runner, same conversation. An automation is detached and autonomous; prefer
  `long_task` when the user is present and collaborating.
- **One-shot orchestration** — if it runs once and reports, call `run_workflow` directly.
  An automation wraps a workflow only to add standing triggers and delivery.

## Anatomy of a definition

```json
{
  "name": "chase-invoice-4471",
  "workflow": "chase-invoice",
  "triggers": [
    {"source": "channel", "channel": "email",
     "filters": {"subject_contains": "INV-4471"},
     "correlate": "INV-(\\d+)"},
    {"source": "schedule", "schedule": {"kind": "cron", "expr": "0 9 * * 1-5", "tz": "UTC"},
     "task": "send another payment reminder for invoice 4471"}
  ],
  "delivery": {"channel": "telegram", "to": "owner-chat", "notify": "when_notable"},
  "help": {"channel": "telegram", "to": "owner-chat"},
  "life": {"intent": "invoice 4471 is paid", "achieved_when": "label:PAID", "max_attempts": 10, "on_stuck": "notify"},
  "concurrency": "single"
}
```

- **`triggers`** (any mix, OR-ed together) — see below. An automation with no triggers
  fires only manually.
- **`delivery`** — where a finished run's outcome is reported, and under what policy.
- **`help`** — the backstop channel for an operator question, an approval, or an
  escalation (see Delivery and help, below).
- **`life`** — optional. When set, the automation is pursuing a verifiable end state and
  disables itself once reached — see Life and the single-case doctrine, below.
- **`concurrency`** — `single` (default): one active run; a new matching event queues and
  drains once the active run finishes. `parallel`: one run per matching event.

## Triggers

- **`schedule`** — `{kind: "cron"|"every"|"at", ...}` plus a `task` (the text the fired
  run's workflow receives). Same schedule shapes as the `cron` tool.
- **`channel`** — `channel` (`email` / `telegram` / `slack` / `discord` / `whatsapp`),
  `filters` (see below), an optional `semantic` condition judged by the aux model
  (fail-closed: no judge configured or a judge error both mean no match), and `match`
  (`wake_or_new`, default — a matching message reaching a **paused** run wakes it, resuming
  that same run; `always_new` — always open a fresh run instead of waking the parked one).
- **`webhook`** — `hook` (the name that maps to `POST /api/v1/hooks/{hook}`, secret-gated —
  fetch the shared secret via the webui). Also accepts `semantic` and `correlate`.
- **`chain`** — `chain_automation` (the upstream automation's name) and `chain_when`
  (`achieved` / `completed` / `failed` / `any` — "any" means any of those three; an
  upstream run that ends `rejected`, `interrupted`, or `paused` never fires a chain
  regardless of `chain_when`). Saving an automation whose chain edges would close a cycle
  is rejected outright, with the cycle named in the error.
- **`correlate`** (channel or webhook) — a one-capture-group regex. Messages carrying the
  same captured value (a ticket id, an invoice number) reach the same paused run even
  across unrelated threads, ahead of the plain per-channel thread key.

**Channel `filters`** is an open key→value map. `from_contains` / `sender_contains` /
`text_contains` (every channel) and `subject_contains` (email only) are case-insensitive
substring tests on prose. Every other key is an *exact*, case-insensitive match: the core
facts every channel provides (`sender`, `sender_name`, `sender_kind` — `human`/`bot`/unset,
`chat`, `chat_name`, `is_dm`), or a channel-specific one (Slack: `app_id`, `bot_id`,
`surface`; Telegram: `forum_topic`; Discord: `guild`, `thread_id`; WhatsApp: `is_group`).
All set keys must hold. A key the named channel never provides matches nothing and is
warned about at save time — scope an app-driven automation with `sender_kind: "bot"` plus
`chat`, not a guess. A `sender_kind: "human"` trigger cannot self-trigger on durin's own
posts (durin writes as an app, so its own messages arrive `sender_kind: "bot"`); to answer
both an app and humans in the same room, give the automation two channel triggers.

## Delivery and help

**`delivery.notify`** decides whether a finished run's outcome is worth reporting on its
own terms: `always` (default), `failures_only`, `when_notable` (a failure, or a
completed/achieved run whose final route label is not in `silent_labels`), or `never`.
`silent_labels` defaults to `("NOTHING_TO_REPORT",)` — a workflow that routes to that label
on an uneventful run stays quiet under `when_notable`; set it to `[]` explicitly to silence
nothing. Regardless of `notify`, a run fired **from a conversation** always reports back
into that conversation — the asking session wins over the declared delivery channel, every
time, including on success.

**`help`** is the separate backstop lane for a run that needs a human mid-flight (an
operator question, a `WorkNode` approval pause) or has something to escalate (a failed/
interrupted run when `delivery` stayed quiet, a stuck life condition, an achieved condition
on an automation with no `delivery.channel` at all — reaching it deserves to be just
as audible as escalating). A workflow ending on a **counterpart-directed** ask (its
`needs_input` text tagged `[TO:counterpart]`) instead answers in-context on the triggering
channel thread — the counterpart, not the operator, sees that one.

## Runs

A run's status is one of `running · paused · achieved · completed · failed · rejected ·
interrupted`. `paused` covers every kind of "waiting for a reply" — an operator question,
an approval, or a counterpart-directed ask — the automation's `ask`/`ask_kind` on the run
record say which. `achieved` only happens on an automation carrying a `life` condition;
reaching it disables the automation's own triggers (this run's delivery and any chain
dispatch still happen first). `interrupted` is not a failure: the run was killed with its
process (a gateway restart) before producing a result; if nothing had started and the
automation is still enabled, a replacement is fired and named — and if that replacement
also fails to start, you are told that too.

## Using the `automations` tool

- `automations(action="list")` — what exists, enabled state, active runs. Check this
  before creating: the standing work may already be defined.
- `automations(action="status", name=…)` — a definition's triggers, life state, recent
  runs, and anything paused or queued.
- `automations(action="fire", name=…, task?)` — start a run now. **It returns immediately
  with the run id; it does not wait.** The outcome arrives on its own as a follow-up
  message when the run finishes — and so does the bad news if the run never starts at all,
  so silence means still running. Do not poll for it, and do not re-fire because you have
  not heard back. Use `action="status"` only if the user asks for an update. On a surface
  with no conversation origin wired, the tool says so up front instead of promising a
  follow-up.
- `automations(action="answer", name=…, run_id=…, answer=…, resolution?)` — reply to a
  paused run. `resolution` (`approve`/`revise`/`reject`) bypasses keyword parsing for a
  run paused on an **approval** specifically; leave it unset for a plain question and let
  the free-text `answer` carry the reply.
- `automations(action="enable"/"pause", name=…)` — pause removes the standing triggers;
  the definition and its run history stay.
- `automations(action="create", definition=<JSON>)` — same validation as the webui,
  including the chain-cycle check; a bad schedule, filter set, correlate regex, or a chain
  edge that would close a cycle is rejected at save time with the reason. **This is also
  how you change one.** There is no separate update action: `create` with an existing
  `name` replaces that definition wholesale (versioned as an edit, so the previous one
  stays recoverable). Send the *complete* definition — anything you leave out is gone, not
  merged.

Neither `list` nor `status` prints triggers, filters, or delivery/help settings in full, so
to change one part of a live automation, first **read** `automations/<name>.json` (reads
are allowed), then send the whole edited definition through `action="create"`. Do not write
that file directly: `automations/` is closed to generic file writes, because this tool is
what validates the definition and records the change in the version store.

Creating an automation is standing, autonomous behavior with a human lane attached —
confirm the user actually wants recurring/parked work (and which channel replies should
flow to) before defining one on their behalf.

## Life and the single-case doctrine

`life.intent` names the end state in prose; `life.achieved_when` is what actually decides
it, structurally, from the workflow's own result: `any_completed` (any successful run
counts) or `label:<LABEL>` (only a run whose workflow routed to that exact final label
counts). There is no judge call here — the workflow graph itself must produce the label
`life` checks, via its own routing/cases or a script-check node (see the `workflows`
skill). `max_attempts` plus `on_stuck` (`escalate_pause` disables and notifies,
`notify` just notifies, `keep` does neither) bound how many consecutive unachieved runs are
tolerated before something is said.

**Single-case doctrine: an automation carrying a `life` condition is single-case, not a
reusable template.** "Chase invoice 4471" means creating *one* dedicated automation named
for that case — not a generic "chase invoices" automation expected to track every invoice
at once. That dedicated automation disables itself the moment its `life` condition is
achieved; a new case gets its own new automation, typically created from chat via
`action="create"` on the spot. An automation with no `life` at all has no such
restriction — a plain "run this workflow on schedule / on this trigger and deliver the
result" definition can legitimately serve an ongoing, non-case-shaped purpose.

## Automations run workflows; verification lives in the workflow

An automation's `workflow` field only names a workflow by reference — the automation layer
never inspects or scores the run's content itself. Whatever counts as "done", "the right
answer", or "worth escalating" has to be decided *inside* the workflow graph: a routing
node's cases, a script-check node's exit code, an approval gate. `life.achieved_when:
"label:<LABEL>"` only reads a label the workflow's own routing already produced — write the
verification once, in the workflow, and both a manual `run_workflow` call and every
automation wrapping that workflow inherit it for free.
