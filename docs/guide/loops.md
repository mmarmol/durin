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

  If your goal is fully mechanical — every check is a script and at least
  one is required — you can turn on **Checks are sufficient** to skip the
  judge altogether: the goal counts as reached the moment the required
  checks pass, with no LLM call and no extra latency. It's off by default,
  and it's unavailable once you add an assertion check (an assertion always
  needs the judge to grade it).
- **Triggers** — what fires the loop: a schedule (a cron expression or a
  repeating interval), an inbound email that matches conditions you set, or
  a manual/chat request. See **Channel triggers** below for the email case,
  and **Current boundaries** for what's not there yet.
- **Escalation** — if a loop fails to reach its goal several times in a row
  (configurable, default 3), it stops quietly retrying and instead notifies
  an operator — you — that it's stuck.

## Creating a loop from the web dashboard

Open the **Loops** section from the sidebar, switch to the **Definitions**
tab, and click **New loop**. The form has these sections:

- **Name** — the loop's identifier (locked once you're editing an existing
  loop).
- **Triggers** — add zero or more. Each is either a **Cron** expression
  (with an optional timezone), a plain **Interval** in seconds, or a
  **Channel: email** trigger that fires on an inbound email matching
  conditions you set — see **Channel triggers** below. A loop with no
  triggers isn't scheduled — it only runs when fired manually or from chat.
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

## Channel triggers: an email-triggered loop, end to end

A channel trigger fires a loop off an inbound message instead of a clock —
today that's email. Walking through a support-ticket loop end to end covers
everything a channel trigger can do.

### Creating the trigger

Add a trigger, switch its source to **Channel: email**, and set:

- **From contains** / **Subject contains** — plain substring filters (either,
  both, or neither), checked without regard to case. A support loop might
  filter to `From contains: support@` so only mail sent to the support alias
  matches.
- **Semantic condition** (optional) — a sentence describing what the message
  should be about, e.g. "the customer is reporting a problem with the
  product, not asking a sales question." Every email that passes the
  filters above is summarized and handed to the model to grade against this
  sentence; only a match fires the loop. See **When to use a semantic
  condition** below before reaching for this.
- **Match policy** — see **Match policies** below.

Save the loop enabled, and point it at a workflow that reads the ticket and
either resolves it or asks for more information.

### A customer email arrives

Mail is picked up on the next IMAP poll (`poll_interval_seconds`), not
instantly — once a matching email is seen, the loop fires. The run shows up
in the Activity feed with `source: channel` and the full email context
(subject and body, as the channel formats it) as its task, running the
loop's workflow against the ticket.

### The workflow needs more information — and the customer's reply wakes it

If the workflow can't resolve the ticket without asking the customer
something, and it addresses that question to the customer rather than to
you, the run parks as **waiting reply**: durin sends the question back into
the same email thread and remembers that thread is waiting on this run. When
the customer replies — to that thread, whenever they get around to it — the
reply doesn't start a new run or fall through as an ordinary message: it
resumes the same paused run with the customer's answer, exactly where it
left off. This works across however long the customer takes to reply; the
loop isn't holding a connection open, it's just waiting for the next message
on that thread.

### Answering for the customer

Sometimes the customer won't reply, or you already know the answer. A
**waiting reply** row in the Activity feed shows the pending question
read-only with an **Answer as operator** toggle — click it and you get the
same reply box a `needs you` run has. Send an answer there and the run
resumes exactly as if the customer had replied themselves.

### When another email arrives while the loop is busy

If the loop's **Concurrency** is **Single** (the default) and a second
matching email arrives while a run is already in flight, it isn't dropped
and it isn't fired on top of the first: it's held until the loop frees up,
then fired automatically — no need to resend it. You'll see the loop's row
in the Definitions tab pick up a queued-count badge while events are
waiting. A cron-triggered loop behaves differently in the same situation
(it simply skips that tick, see **Current boundaries**) — an email trigger's
match is never silently thrown away that way, because someone is waiting on
a reply to that email.

### Match policies, in plain language

A channel trigger's **Match policy** controls what a matching message is
*for*: **"Wake the waiting run when the thread matches"** (the default) is
what you want for a back-and-forth like a support ticket, where a reply
should continue the same conversation rather than start a fresh one.
**"Always open a new run"** is for triggers where every matching message is
its own independent unit of work — a notification inbox where each email is
a separate item to process, not a thread to converse in. One consequence:
an "always open a new run" loop that asks the customer a counterpart
question will not have that run woken by their thread reply — it stays
**waiting reply** until you answer it yourself through the **Answer as
operator** override.

### When to use a semantic condition

A structural filter (from/subject contains) is free and instant — it's a
plain substring check, no model call involved. A semantic condition costs an
LLM call on **every message that passes the structural filters**, so it
adds latency and usage cost to each candidate message, not just to the ones
that end up matching. Reach for one only when a substring filter genuinely
can't express what you're after — e.g. distinguishing "a bug report" from
"a feature request" in a shared support inbox where both land in the same
address with similar subjects. If a filter on sender or subject would
already narrow things down well enough, skip the semantic condition and
save the call.

## Reading the Activity view

The **Activity** tab is a live feed of every run, across every loop, newest
first — with anything waiting on you pinned to the top. A toggle in the
top-right corner switches between a **List** view (the default) and a
**Board** view — see **Reading the board** below — and your choice is
remembered the next time you open the tab. Each run shows the loop name,
what it was asked to do, its status, where it came from (`cron`, `manual`,
`chat`, `channel`), and when it started. The statuses you'll see:

| Status | Meaning |
|---|---|
| running | The workflow is currently executing. |
| needs you | Paused, waiting on an answer from you (see below). |
| waiting reply | Paused, waiting on a reply from whoever triggered it (e.g. the customer on a channel-triggered loop) — you can still answer it yourself. |
| done | Completed and the goal was verified reached. |
| no goal | Completed (or ran out of passes) but the goal wasn't reached. |
| escalated | Missed the goal too many times in a row — an operator has been notified. |
| error | The run failed outright (a tool/provider error, or the run was aborted). |

Each loop's row in the **Definitions** tab also shows how many runs are
currently active and how many need you, plus a queued-count badge when
channel events are waiting for their turn (see **Channel triggers** above).

## Reading the board

Switch the Activity tab to **Board** and the same runs are laid out as five
fixed columns instead of one list: **Needs you**, **Waiting reply**,
**Running**, **Done**, and **Attention**. The first four match the statuses
above one-to-one; **Attention** is where `no goal`, `escalated`, and `error`
runs all land together — three different reasons a run didn't reach its
goal, grouped into the one place that means "look at this." Each column
header shows a live count, and cards within a column are sorted newest
first. Clicking a card expands the same run detail described below, right
in place.

A run's column follows its status, not the other way around — you can't
drag a card to a different column. The board is a different way of looking
at the same feed the List view shows, not a separate way of managing runs.

## The run detail panel

Click any run — in List or Board — to expand its detail inline. Beyond the
status and timestamps already visible in the row, it shows:

- **Origin** — where the run came from: the channel (e.g. `email`), the
  sender, and, for a channel-triggered run, the subject and a short
  reference to the conversation thread.
- **Task** — the full instruction the run's workflow was given, truncated
  with a **Show more** toggle when it's long.
- **Ask** — the question the run is currently paused on, if any.
- **Detail** — an error message, shown when there's a specific failure
  reason behind a `no goal`, `escalated`, or `error` outcome.
- **Checks** — a table of every goal check the run was graded against: what
  kind it was (script or assertion), the command or assertion text, and
  whether it passed, with any extra detail the check produced. A run shows no checks table
  when goal verification never ran: still running, paused on a question,
  or finished without reaching verification (it ran out of passes, or
  failed before completing).
- **Workflow run** — a copyable reference to the underlying workflow run,
  for cross-checking in the Workflows tab.

This is the evidence behind a run's status: instead of just knowing a run
was marked `no goal`, you can see exactly which check failed, or read the
error that sent it to `error`.

## Outcomes at a glance

Each loop's row in the **Definitions** tab also carries a compact outcome
strip: up to ten dots, oldest to newest left to right, one per recent
finished run. A filled dot is a `done` run, a muted dot is `no goal`, and a
red dot is `escalated` or `error`. Hover a dot to see its status and when it
finished.

Next to the dots, a percentage shows **convergence** — the share of this
loop's finished runs that reached their goal — and, only when there have
been any, an **esc** percentage for how many of those runs were escalated.
Both percentages are computed over the loop's retained finished runs (older
runs are pruned over time), not just
the ten dots shown, so a loop with a long history can show a stable
percentage even while the dots themselves only cover its most recent runs.
The strip stays empty until a loop has at least one finished run.

## The needs-you badge

The **Loops** entry in the sidebar carries a small badge whenever any run,
across every loop, is paused on **needs you**. It's a quick way to tell —
without opening the tab — whether something is waiting on you right now,
and it updates on the same refresh cycle as the Activity feed. A run
waiting on a **reply** from its counterpart (e.g. the customer on a
channel-triggered loop) doesn't count toward this badge — it isn't waiting
on you unless you choose to step in with **Answer as operator**.

## Answering an ask

A run lands on **needs you** when its workflow paused to ask a question
addressed to you — the same `needs_input` pause a workflow can hit on its
own, just surfaced through the loop. That row shows the question inline with
a reply box right there in the Activity feed: type your answer and send it,
and the loop resumes the same run from where it paused — it doesn't start
over. A **waiting reply** run works the same way once you click **Answer as
operator**; see **Channel triggers** above for when a run lands there
instead.

## Pausing and deleting a loop

From the **Definitions** tab, **Edit** a loop and use **Save as paused**
to disable it without losing the definition — its triggers are removed
from the schedule until you re-enable it. **Delete** removes the
definition and its scheduled triggers entirely (with a confirmation
first); past run history is unaffected.

## Current boundaries

- **Channel triggers today are email only.** A loop that should react to a
  message in a specific chat channel (Telegram, Slack, …) isn't triggerable
  that way yet; schedule it or fire it yourself instead.
- **Firing on demand works from the dashboard, chat, or the API.** Each
  loop's row in the Definitions tab has a **Run now** button; you can also
  ask the agent to fire a loop, or use the `loops` tool's `fire` action.
  Either way, watch the result show up in the Activity feed.
- **What happens when a `Single`-concurrency loop is already busy depends on
  how it was triggered.** A scheduled tick that lands while a run is in
  flight is silently skipped (it'll fire again next time). A manual or chat
  fire in the same situation is refused with a "busy" message, so you know
  your explicit request didn't just vanish. A matching channel event is
  queued instead of skipped or refused — see **Channel triggers** above.

## See also

[Loops internals](../internals/loops.md) covers the architecture — the
run lifecycle, goal verification, cron wiring, and concurrency semantics.
[Workflows](workflows.md) covers what a loop's body can actually do.
