# Loops

A **loop** is durin doing something for you repeatedly, checking each time
whether it actually got there, and asking you when it can't figure out the
rest on its own. Where a [workflow](workflows.md) is one multi-step run, a
loop is a workflow with a **goal** attached — "keep doing this until it's
actually done" instead of "do this once."

A loop iterates on **new information**, not the clock: it doesn't wake up on
a timer and just retry blindly. It fires because something told it to — a
schedule tick, you asking for it in chat, or a manual run — runs its
workflow, checks the result against the goal, and either closes out, tries
again next time it fires, or — if it keeps missing the goal — stops and asks
you what to do.

## The four parts of a loop

- **A workflow** — the actual work. Any workflow you already have (or one
  you build for the purpose) can be a loop's body.
- **A goal** — a plain-language description of what "done" means (the
  *intent*), plus optional **checks** that verify it mechanically:
  - a **script check** is a shell command; exit code 0 means it passed
    (e.g. `curl -f https://my-service/health`, or a test command).
  - an **assertion check** is a sentence an LLM judge grades against the
    workflow's output (e.g. "the report mentions this week's top 3
    regressions").

  Any check can be marked **required** — a failing required check blocks
  "done" no matter what, even if everything else looks fine. The judge that
  grades assertions also independently judges the overall intent, so a run
  can pass every check and still not count as done if the judge isn't
  convinced the actual goal was met.
- **Triggers** — what fires the loop. Today that's a schedule (a cron
  expression, or a repeating interval) or a manual/chat request; see
  **V1 boundaries** below for what's not there yet.
- **Escalation** — if a loop fails to reach its goal several times in a row
  (configurable, default 3), it stops quietly retrying and instead notifies
  an operator — you — that it's stuck.

## Creating a loop from the web dashboard

Open the **Loops** section from the sidebar, switch to the **Definitions**
tab, and click **New loop**. The form has these sections:

- **Name** — the loop's identifier (locked once you're editing an existing
  loop).
- **Triggers** — add zero or more. Each is either a **Cron** expression
  (with an optional timezone) or a plain **Interval** in seconds. A loop
  with no triggers isn't scheduled — it only runs when fired manually or
  from chat.
- **Goal** — the **Intent** (what "done" means, in your own words), then
  any number of **Checks**: pick **Script** (a shell command) or
  **Assertion** (a sentence to grade), and toggle **Required**.
- **Workflow** — pick the workflow this loop runs each time it fires, from
  your existing workflows. Alongside it, **Stuck after** sets how many
  consecutive missed-goal runs trigger an escalation.
- **Concurrency** — **Single** (the default: skip or refuse a new fire while
  one is already in flight) or **Parallel** (let multiple runs of this loop
  overlap).
- **Operator** — an optional **Channel** (e.g. `telegram`) and
  **Recipient** (a chat id or user id) to notify when the loop needs you —
  either because a run is asking a question or because it's been escalated.
  Leave both blank and the loop still shows up in the Activity view; you
  just won't get pushed a message about it.

Save with **Save & enable** to activate its triggers immediately, or
**Save as paused** to keep it defined but dormant — useful while you're
still tuning the goal or checks.

## Creating a loop from chat

Ask the agent to create one, or use the `loops` tool's `create` action
directly with a JSON definition matching the same shape as the form above:

```json
{
  "name": "daily-digest",
  "workflow": "research-to-answer",
  "goal": {
    "intent": "a digest of yesterday's activity was posted",
    "checks": [{"kind": "script", "required": true, "command": "test -f /tmp/digest.md"}]
  },
  "triggers": [{"source": "cron", "schedule": {"kind": "cron", "expr": "0 8 * * *"}}],
  "concurrency": "single",
  "stuck_after": 3,
  "operator_channel": "telegram",
  "operator_to": "123456"
}
```

This goes through the exact same validation and cron-registration path as
the webui form, so a loop you create in chat behaves identically to one you
build visually. The same tool also has `list`, `status`, `fire`, `answer`,
`enable`, and `pause` actions — so you can, for instance, ask "fire the
daily-digest loop now" or "pause daily-digest" without opening the
dashboard at all.

## Reading the Activity view

The **Activity** tab is a live feed of every run, across every loop, newest
first — with anything waiting on you pinned to the top. Each run shows the
loop name, what it was asked to do, its status, where it came from
(`cron`, `manual`, `chat`), and when it started. The statuses you'll see:

| Status | Meaning |
|---|---|
| running | The workflow is currently executing. |
| needs you | Paused, waiting on an answer (see below). |
| done | Completed and the goal was verified reached. |
| no goal | Completed (or ran out of steps) but the goal wasn't reached. |
| escalated | Missed the goal too many times in a row — an operator has been notified. |
| error | The run failed outright (a tool/provider error, or the run was aborted). |

## Answering an ask

A run lands on **needs you** when its workflow paused to ask a question —
the same `needs_input` pause a workflow can hit on its own, just surfaced
through the loop. That row shows the question inline with a reply box right
there in the Activity feed: type your answer and send it, and the loop
resumes the same run from where it paused — it doesn't start over.

## Pausing and deleting a loop

From the **Definitions** tab, **Edit** a loop and use **Save as paused**
to disable it without losing the definition — its triggers are removed
from the schedule until you re-enable it. **Delete** removes the
definition and its scheduled triggers entirely (with a confirmation
first); past run history is unaffected.

## V1 boundaries

- **Triggers today are cron (schedule) and manual/chat only.** A loop that
  should react to an inbound event on a channel — a new email, a message in
  a specific chat — isn't triggerable that way yet; for now, schedule it or
  fire it yourself.
- **Firing on demand is a chat/API action, not (yet) a dashboard button.**
  The Definitions and Activity views don't have a "run now" button in this
  version — ask the agent to fire a loop, or use the `loops` tool's `fire`
  action, and watch the result show up in the Activity feed.
- **A single, cron-fired loop skips rather than queues.** If a scheduled
  tick lands while a `concurrency: "single"` loop already has a run in
  flight, that tick is silently skipped (it'll fire again next time). A
  manual or chat fire in the same situation is refused with a "busy"
  message instead, so you know your explicit request didn't just vanish.

## See also

[Loops internals](../internals/loops.md) covers the architecture — the
run lifecycle, goal verification, cron wiring, and concurrency semantics.
[Workflows](workflows.md) covers what a loop's body can actually do.
