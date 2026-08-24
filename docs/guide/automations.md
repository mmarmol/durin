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
  [Workflows](workflows.md).
- **Delivery and help** — where it speaks (below).
- **An optional life condition** — when it stops (below).

## What invokes it

An automation can carry any mix of these, OR-ed together — any one firing starts a
run. With none at all, it fires only when you tell it to.

- **Schedule** — the same shapes as a cron job (`at`, `every`, or `cron` with an
  expression and timezone), plus the task text the run receives.
- **Channel** — a message arriving on `email`, `telegram`, `slack`, `discord`, or
  `whatsapp` that matches a `filters` map (sender, chat, or a channel-specific
  field, all exact matches; `subject_contains` — email only — and the other
  `*_contains` keys are substring matches instead) and, optionally, a
  `semantic` condition judged by the model
  ("this looks like a customer complaint", not just a keyword). By default
  (`match: "wake_or_new"`), a matching message on a thread where a run is already
  **paused** resumes that run instead of starting a new one; set
  `match: "always_new"` to always start fresh instead.
- **Webhook** — a `POST` to `/api/v1/hooks/{hook}` from an external service (a CI
  pipeline, a monitoring tool, anything that can send JSON). Get the shared secret
  from `GET /api/v1/automations/hooks-secret`, and send it as the
  `X-Durin-Hook-Secret` header on every call.
- **Chain** — another automation's outcome (`achieved`, `completed`, `failed`, or
  `any` of those three) triggers this one. Chain a few automations together to build
  a pipeline of standing work that reacts to itself, not only to the outside world.
  durin refuses to save a chain that would loop back on itself, naming the cycle.

A **`correlate`** pattern (channel or webhook triggers) is worth calling out on its
own: a one-capture-group regex that pulls an id out of the message — a ticket
number, an invoice number — so that every future message carrying the *same* id
reaches the same paused run, even if it lands in a completely different thread.
Without `correlate`, resumption is scoped to the plain thread the message arrived
on.

## Where it speaks

Two separate channels, because they answer different questions:

- **Delivery** (`delivery.channel`/`to`/`notify`) is the routine report: did the run
  succeed, and is that worth telling someone. `notify` controls how chatty it is —
  `always` (default), `failures_only`, `when_notable` (skip a completed run whose
  workflow routed to a "nothing to report" label — configurable via
  `silent_labels`), or `never`.
- **Help** (`help.channel`/`to`) is the backstop for anything that needs a human:
  an operator question the workflow asked, an approval pause, an escalation. It's
  also where a failure or a stuck condition surfaces if `delivery` stayed quiet, and
  where reaching the goal is announced on an automation that has no `delivery`
  channel configured at all — getting there deserves to be just as audible as
  escalating.

**A live conversation always wins.** If you fire an automation from chat, its
outcome comes back into that same conversation regardless of `delivery`/`notify` —
you asked, so you hear back, every time, success or failure.

**A counterpart gets answered in place.** When a workflow's question is tagged for
the *other party* rather than the operator (durin's own internal convention — you
won't normally write this tag yourself; the seed workflows that correspond with an
external party already do), the reply goes back on the same channel thread the
triggering message arrived on, not to `help`. A reply on that thread resumes the run.

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

Every weekday morning this fires a reminder; any reply on the matching email thread
(matched by the `INV-4471`/`INV-(\d+)` correlation, so it works even from a fresh
reply, not just the original thread) resumes the same standing case. `life.intent`
is a plain-language label for what "done" means; `life.achieved_when` is what
actually decides it — here, the `chase-invoice` workflow itself routes to a `PAID`
label once the payment clears, and that's the only thing that ends this automation.
`max_attempts` plus `on_stuck` bound how many unpaid reminders happen before durin
says something beyond the routine delivery notice (`notify`, `escalate_pause` and
disable, or stay quiet).

**This is single-case, not a template: "chase invoice 4471" means one dedicated
automation named for that invoice**, not a generic "chase invoices" automation
expected to track every open invoice at once. The moment `life.achieved_when` is
met, this automation disables its own triggers — it did its job. A new invoice gets
its own new automation, typically created on the spot from the conversation where
you asked for it. An automation with **no** `life` condition has no such
restriction — a plain "run this on schedule / on this trigger and deliver the
result" definition can serve an ongoing, non-case-shaped purpose indefinitely.

## Answering a paused run

A workflow can pause mid-run — an operator question, or a `WorkNode` approval gate.
While paused, an automation's run waits in the `help` channel (or, for a
counterpart-tagged question, on the triggering thread itself) for a reply:

- **Reply in the channel.** A plain reply on the thread durin asked in resumes the
  run automatically — no separate action needed. For an approval, replying
  "approve", "reject", or free text describing what to change all work; free text
  that isn't a clear approve/reject is treated as a revision request back to the
  workflow.
- **Reply via the `automations` tool** (`action="answer"`) — the same mechanism the
  chat agent uses on your behalf, and what a script or another agent talks to
  directly. An explicit `resolution` (`approve`/`revise`/`reject`) is only for an
  approval pause, and skips free-text interpretation entirely.

Either path resumes the exact paused run — same workflow state, same working
folder — not a new one.

## Managing automations today

The web dashboard's Automations section lists every definition — its triggers,
what it runs, and its life condition — with a "Needs you" tray for pending
approvals and questions. Review/Answer expands the selected one inline
into a resolution card: an approval shows the exact proposal quoted and offers
approve / correct-with-a-comment / reject, a question is a free-text answer —
the same three-way resolution a channel reply or the `automations` tool
drives, just from the dashboard. Resolving one refreshes it out of the tray
immediately. The section also has an editor for creating a definition or
changing an existing one's triggers, workflow, delivery, help routing, and
life condition visually, the same way the Workflows pane already lets you
build a flow graph visually. Clicking a definition opens its detail: a
"Run now" button to fire a manual run on demand, a pause/resume control that
flips whether the automation is currently enabled (it works just as well on
one a life condition already disabled — resuming it re-arms its triggers),
live node-by-node progress for any run currently in flight with a stop
control to cancel it early, and a run history where each entry shows its
cause, outcome, and delivery record (or approval record, for a run a human
resolved) — with a link into the Workflows pane's own run detail for the
full execution trace (nodes, sessions, artifacts). Beyond the dashboard, an
automation can also be defined and driven through:

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
  for a script or an external integration. There's no partial-update route: `PUT` always replaces the named
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
