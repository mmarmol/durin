# Automations

An **automation** is standing work: a definition that sits and waits, then fires a
**workflow** when something happens — a schedule tick, a matching message on a chat
channel, a webhook call, or another automation finishing. Where a workflow runs once
when you (or the agent) invoke it, an automation is what keeps that workflow running
on its own, after the conversation that defined it is long over — until, optionally,
it decides for itself that it's done.

An automation has four parts:

- **Triggers** — what invokes it (below).
- **A workflow** — what it runs. The automation only names a workflow by reference;
  the automation layer never judges the content of a run itself. Whatever counts as
  "done" or "worth escalating" is decided *inside* the workflow graph (a routing
  node's verdict, a script node's exit code, an approval gate) — see
  [Workflows](workflows.md). The workflow must already exist: the name is not
  checked when the automation is saved, and a run that names a missing workflow
  ends `failed`.
  A workflow run started by an automation does not give its nodes their `mcps`
  tools: a node that names MCP servers runs without them, and nothing in the run
  says so (see [Workflows](workflows.md)).
- **Delivery and help** — where it speaks (below).
- **An optional life condition** — when it stops (below).

## What invokes it

An automation can carry any mix of these, OR-ed together — any one firing starts a
run. With none at all, it fires only when you tell it to.

- **Schedule** — the same shapes as a cron job (`at` with `at_ms`, a Unix time in
  milliseconds; `every` with `every_ms`; or `cron` with `expr` and an optional
  `tz`), plus the `task` text the run receives.
- **Channel** — a message arriving on `email`, `telegram`, `slack`, `discord`, or
  `whatsapp` that matches a `filters` map (sender, chat, or a channel-specific
  field, all exact matches; `subject_contains` — email only — and the other
  `*_contains` keys are substring matches instead) and, optionally, a
  `semantic` condition judged by the model
  ("this looks like a customer complaint", not just a keyword). The condition is
  judged only for messages that already pass `filters`, and each judgement is a
  model call, so keep `filters` tight; with no model set under
  `agents.aux_models.automations` it uses the model the agent is running on. By default
  (`match: "wake_or_new"`), a matching message on a thread where a run is already
  **paused** resumes that run instead of starting a new one; set
  `match: "always_new"` to always start fresh instead.
- **Webhook** — a `POST` to `/api/v1/hooks/{hook}` from an external service (a CI
  pipeline, a monitoring tool, anything that can send JSON). Get the shared secret
  from `GET /api/v1/automations/hooks-secret`, and send it as the
  `X-Durin-Hook-Secret` header on every call. The body must be a JSON object. The
  run's task is the body's `text`,
  or the whole JSON body when it has none, capped at 4000 characters. durin answers
  `202` when an automation took the call and `404` when no enabled automation
  accepted it.
- **Chain** — another automation's outcome (`achieved`, `completed`, `failed`, or
  `any` of those three) triggers this one; the chained run's task is the upstream
  run's final output (its outcome summary when there is none). Chain a few
  automations together to build a pipeline of standing work that reacts to itself,
  not only to the outside world. durin refuses to save a chain that would loop back
  on itself, naming the cycle.

The `filters` keys every channel understands are `sender`, `sender_name`,
`sender_kind` (`human` or `bot`; email and WhatsApp do not report it, so a
`sender_kind` filter never matches there), `chat`, `chat_name` and `is_dm` (exact,
case-insensitive), plus `from_contains`, `sender_contains` and `text_contains`
(substring). Each channel adds its own: `subject_contains` on email; `app_id`,
`bot_id` and `surface` on Slack; `forum_topic` on Telegram; `guild` and `thread_id`
on Discord; `is_group` on WhatsApp. Every value is a string (`"is_dm": "true"`),
and every key you set must hold. A key the channel
does not provide never matches, and the save does not refuse it — durin only logs
a warning — so a trigger that stays silent is usually a misspelled key.

A **`correlate`** pattern (channel or webhook triggers) is worth calling out on its
own: a one-capture-group regex that pulls an id out of the message — a ticket
number, an invoice number — so that every future message carrying the *same* id
reaches the same paused run, even if it lands in a completely different thread.
Without `correlate`, resumption is scoped to the plain thread the message arrived
on.

**One run at a time, by default.** With `"concurrency": "single"` (the default),
an automation has at most one active run, a paused run included. While it lasts,
a schedule tick is skipped, a manual fire is refused, and a channel, webhook or
chain firing waits its turn — dropped if it is still waiting after
`automations.queue_ttl_s`. `"concurrency": "parallel"` lets runs overlap.

## Where it speaks

Two separate channels, because they answer different questions:

- **Delivery** (`delivery.channel`/`to`/`notify`) is the routine report: did the run
  succeed, and is that worth telling someone. `channel` names one of your connected
  channels and `to` the chat, room or address on it to write to (the same holds for
  `help`). `notify` controls how chatty it is —
  `always` (default), `failures_only`, `when_notable` (skip a completed run that
  *ended* on a silent route label — `NOTHING_TO_REPORT` by default; set
  `silent_labels` to change the list), or `never`.
- **Help** (`help.channel`/`to`) is the backstop for anything that needs a human:
  an operator question the workflow asked, an approval pause, an escalation. It's
  also where a failure or a stuck condition surfaces if `delivery` stayed quiet, and
  where reaching the goal is announced on an automation that has no `delivery`
  channel configured at all — getting there deserves to be just as audible as
  escalating.

**A live conversation always wins.** If you fire an automation from chat, its
outcome comes back into that same conversation regardless of `delivery`/`notify` —
you asked, so you hear back, every time, success or failure.

**A counterpart gets answered in place.** When a workflow's question is meant for
the *other party* rather than the operator — its `needs_input` text starts with the
literal tag `[TO:counterpart]` (no shipped workflow uses it; add it to your own) —
the question goes out, without the tag, on the same channel thread the triggering
message arrived on, not to `help`. A reply on that thread resumes the run. A run
with no triggering thread (a schedule or manual fire, or a webhook call whose
trigger has no `correlate` match) asks in `help` instead. A webhook call that did
match a `correlate` pattern has nowhere to post: its question waits on the paused
run in the dashboard, and the next call to the same hook carrying the same id
resumes the run with that call's text as the answer.

## When it stops

Most automations don't stop — they're recurring by design ("summarize the inbox
every morning," "watch this webhook forever"). But some model a **single case**: one
specific, nameable thing you want durin to keep after until it's actually resolved.
That's what a **life** condition is for.

```json
{
  "name": "chase-invoice-4471",
  "workflow": "chase-invoice",
  "triggers": [
    {"source": "channel", "channel": "email",
     "filters": {"subject_contains": "INV-4471"},
     "correlate": "INV-(\\d+)"},
    {"source": "schedule",
     "schedule": {"kind": "cron", "expr": "0 9 * * 1-5", "tz": "UTC"},
     "task": "send another payment reminder for invoice 4471"}
  ],
  "delivery": {"channel": "telegram", "to": "owner-chat", "notify": "when_notable"},
  "help": {"channel": "telegram", "to": "owner-chat"},
  "life": {"intent": "invoice 4471 is paid", "achieved_when": "label:PAID",
           "max_attempts": 10, "on_stuck": "notify"}
}
```

Every weekday morning this fires a reminder; any email with `INV-4471` in its
subject starts a run for the same case, in the same working folder (the
`INV-(\d+)` capture keys it, so a fresh email works as well as a reply on the
original thread) — and if an earlier email-triggered run is paused on a question to
that sender, the email resumes that run instead. `life.intent`
is a plain-language label for what "done" means; `life.achieved_when` is what
actually decides it: `any_completed` (the default) or `label:<LABEL>` — the route
label the run ended on, matched exactly. Here the `chase-invoice` workflow itself
ends on a `PAID` label once the payment clears, and that's the only thing that ends
this automation. `max_attempts` plus `on_stuck` bound how many unpaid reminders
happen before durin says something beyond the routine delivery notice: `notify`
(the default) tells the `help` destination, `escalate_pause` tells it and disables
the automation, and `keep` stays quiet.

**This is single-case, not a template: "chase invoice 4471" means one dedicated
automation named for that invoice**, not a generic "chase invoices" automation
expected to track every open invoice at once. The moment `life.achieved_when` is
met, this automation disables its own triggers — it did its job. A new invoice gets
its own new automation, typically created on the spot from the conversation where
you asked for it. An automation with **no** `life` condition has no such
restriction — a plain "run this on schedule / on this trigger and deliver the
result" definition can serve an ongoing, non-case-shaped purpose indefinitely.

## Answering a paused run

A workflow can pause mid-run — an operator question, or an approval gate (a work
node with `"approval": true`). While paused, an automation's run waits in the
`help` channel (or, for a counterpart-tagged question, on the triggering thread
itself) for a reply:

- **Reply in the channel.** A plain reply on the thread durin asked in resumes the
  run automatically — no separate action needed. For an approval, replying
  "approve", "reject", or free text describing what to change all work; free text
  that isn't a clear approve/reject is treated as a revision request back to the
  workflow. This works where durin's message opens a thread it can recognise a
  reply in: a `help` destination on Slack or email, and a counterpart question on
  the thread it was asked in. A `help` message sent to Telegram, Discord or
  WhatsApp only announces the pause; answer that one from the dashboard.
- **Answer from the dashboard.** The "Needs you" tray (below) resolves any paused
  run, whatever channel announced it.
- **Answer a question through the chat agent.** The agent's `automations` tool
  (`action="answer"`) resumes a run paused on a question with the answer you give
  it. It refuses a run paused for an approval: approving, revising or rejecting is
  your decision, never the agent's, so you make it with a channel reply or from
  the dashboard.

Every path resumes the exact paused run — same workflow state, same working
folder — not a new one.

## Managing automations

The web dashboard's Automations section lists every definition — its triggers,
what it runs, and its life condition — with a "Needs you" tray for pending
approvals and questions. Review/Answer expands the selected one inline
into a resolution card: an approval shows the exact proposal quoted and offers
approve / correct-with-a-comment / reject, a question is a free-text answer —
the same three-way resolution a channel reply drives, just from the
dashboard. The same card shows for each paused run on the dashboard's
**Pending** page, next to everything else that waits on you. A secondary "Stop this run" control on the
same card ends it instead, without a reply — the deterministic case: nothing
is running to wait for, so it stops immediately and does not resume. Resolving
one refreshes it out of the tray immediately. The section also has an editor
for creating a definition or changing an existing one's triggers, workflow,
delivery, help routing, and life condition visually, the same way the
Workflows pane lets you build a flow graph visually. Clicking a
definition opens its detail: a "Run now" button to fire a manual run on
demand, a pause/resume control that flips whether the automation is currently
enabled (it works just as well on one a life condition already disabled —
resuming it re-arms its triggers), live node-by-node progress for any run
currently in flight with a stop control, and a run history where each entry
shows its cause, outcome, and delivery record (or approval record, for a run a
human resolved) — with a link into the Workflows pane's own run detail for the
full execution trace (nodes, sessions, artifacts). Stopping an in-flight run
is a request, not instant: it asks the run to stop at its next workflow step
(an agent step in progress finishes and is kept; a running script step is
killed), then shows as interrupted — never delivered as an outcome, whatever
the delivery policy says. Clicking Stop again while that request is pending
offers a second, stronger option instead of repeating the same no-op: Force
stop interrupts a work node's turn mid-flight rather than waiting for the next
step boundary.

When durin itself stops or restarts with a run in flight, the run ends with it,
its running script step included, and shows as interrupted; it is not resumed, and
the automation's next run starts anew. One limit: when durin's process is killed
outright (a `kill -9`, the system running out of memory, a power loss), a script
step that was running keeps running. Nothing ends it and the automation's next run
does not wait for it, so the two can work at the same time. Only a resume of the
dead workflow run waits for such a script, and an automation never resumes one.

Beyond the dashboard, an automation can also be defined and driven through:

- **The agent, in chat.** Describe the standing work you want — "each time an email
  with INV-4471 in the subject arrives, run the chase-invoice workflow and ping me
  on Telegram if a reminder goes unanswered ten times" — and the agent drafts and
  creates the definition via the `automations` tool: `list`/`status` to check what
  exists and what's running, `create` to define or wholesale-replace one, `fire` to
  run it now, `answer` to reply to a paused run, `enable`/`pause` to toggle it. This
  goes through the same validation (and the same chain-cycle check) any other
  surface would use.
- **The HTTP API directly** — `GET`/`PUT`/`DELETE /api/v1/automations/{name}`,
  `.../fire`, `.../runs/{run_id}/answer`, `.../runs/{run_id}/stop`, and `.../runs`
  for a script or an external integration, with an API token that carries the
  `automations:read` scope (to read) or `automations:write` (to change, fire,
  answer or stop). `PUT` takes `{"definition": {...}}`, the same JSON shape as the
  example above. There's no partial-update route: `PUT` always replaces the named
  automation's definition wholesale (versioned as an edit, so the previous
  definition stays recoverable).

## Where things live

- **Definitions:** `<workspace>/automations/<name>.json` — a small git-versioned
  directory, the same mechanism workflows and skills use; every change is
  recoverable.
- **Run records:** `<workspace>/automations-runs/<name>/<run_id>.json`, one manifest
  per run — status, cause, delivery record, and the underlying workflow run it
  drove.
- **Retention:** finished run manifests are pruned to the most recent
  `automations.keep_runs` (default 20) per automation; a paused run is never pruned
  out from under an unanswered question.

## See also

- [Workflows](workflows.md) — what an automation actually runs, and how a workflow
  decides pass/fail, routing, and approval gates.
- The `automations` skill, which the agent consults before building one, covers the
  full value test — when an automation earns its place over a cron job, a plain
  workflow run, or just doing the task now — plus the single-case doctrine in more
  depth.
- [Automations internals](../internals/automations.md) covers the mechanism: the
  run flow, trigger matching, delivery routing precedence, and how a workspace with
  a pre-existing `loops/` directory migrates automatically.
