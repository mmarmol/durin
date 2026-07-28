---
name: loops
description: How durin's loops work and — above all — when a loop earns its place over a cron job, a workflow run, or just doing the task now. Load before reaching for the `loops` tool, before creating a loop for a user, or when a request smells like standing work ("every time a mail like X arrives…", "keep pursuing Y until it's done", "watch this and escalate if…"). Covers the anatomy (triggers, verifiable goals, checks, concurrency), the two human lanes (counterpart vs operator), how runs wake on replies and correlation keys, and the value test — a loop is for a STANDING goal that new information should re-activate; it is not a scheduler, not a retry wrapper, and not a substitute for finishing the task in this conversation.
---

# Loops — standing goals that iterate on new information

A **loop** is a persistent definition — triggers, a verifiable goal, a workflow body —
that fires **runs**. Each run executes the workflow once and checks the result against
the goal: reached → `done`; not reached but progress is possible → the loop waits for
*new information* (a reply, a matching event, an operator answer) and resumes; stuck →
it escalates to a human. The core principle: **a loop iterates on new information,
never on the clock**. There are no blind timer retries — if nothing new arrived,
re-running would only reproduce the same result.

## When a loop earns its place — the value test

Default to doing the task now, in this conversation. A loop only wins when the work is
**standing**: it outlives this chat and must re-activate on events you will not be
around to see. At least one of these must hold:

- **Event-driven recurrence** — "each time a matching message/webhook arrives, handle
  it as a fresh case" (support requests by mail, alerts from a monitoring hook).
- **A goal that outlives the turn** — done-ness depends on the outside world answering
  back (a counterpart must confirm, a ticket must close), so the work parks and must
  *wake* when the answer arrives.
- **Scheduled pursuit of a verifiable outcome** — a cron trigger where each firing
  pursues and *verifies* a goal, not merely executes a task.

## When NOT to use a loop

- **The user wants it done now** — just do it; a loop adds definition overhead and
  removes the work from the conversation.
- **Pure scheduling** — "run X every morning" with no goal to verify and no iteration:
  that is a **cron job** (see the `cron` skill). A loop's cron trigger is for *pursue
  and verify*, not *execute and forget*.
- **A retry wrapper** — a loop does not re-run on failure by itself; failing runs
  escalate to a human. If a task needs internal retries or gates, that structure
  belongs *inside* its workflow graph.
- **A sustained objective within this chat** — that is `long_task` (see the
  `long-goal` skill): same runner, same conversation. A loop is detached and
  autonomous; prefer `long_task` when the user is present and collaborating.
- **One-shot orchestration** — if it runs once and reports, call `run_workflow`
  directly. A loop wraps a workflow only to add standing triggers + goal verification.

## Anatomy of a definition

```json
{
  "name": "support-inbox",
  "workflow": "handle-support-case",
  "goal": {
    "intent": "the customer's request is resolved and confirmed",
    "checks": [
      {"kind": "script", "required": true, "command": "check-ticket-closed.sh"},
      {"kind": "assertion", "required": false, "text": "customer confirmed the fix"}
    ]
  },
  "triggers": [
    {"source": "channel", "channel": "email",
     "filters": {"subject_contains": "SUPPORT"},
     "semantic": "the message reports a real problem (not marketing)",
     "correlate": "TICKET-(\\d+)"}
  ],
  "concurrency": "single",
  "stuck_after": 3
}
```

- **Triggers** (any mix): `cron` (a schedule), `channel` (email / telegram / slack /
  discord / whatsapp, with structural `filters` and an optional `semantic` condition
  judged by the aux model, fail-closed), or `webhook` (a named hook POSTed by an
  external system, secret-gated).
  A loop with no triggers fires only manually. `correlate` (channel or webhook) is a
  one-capture-group regex: messages carrying the same captured value — a ticket id, an
  order number — reach the same case even across unrelated threads.
- **Channel `filters`** is an open key→value map. Keys ending in `_contains`
  (`sender_contains`, `subject_contains`, `text_contains`) are case-insensitive
  substring tests on prose. Every other key is an *exact* match, on either a fact every
  channel provides — `sender`, `sender_name`, `sender_kind` (`human`/`bot`), `chat`,
  `chat_name`, `is_dm` — or a channel-specific one (Slack: `app_id`, `bot_id`,
  `surface`; Discord: `guild`, `thread_id`). Exact, not substring: a filter for room
  `support` must not also fire in `support-escalations`. All set keys must hold. A key
  the named channel never provides matches nothing and is warned about on save, so
  scope an app-driven loop with `sender_kind: "bot"` plus `chat`, not with a guess.
- **A bot-only key silently excludes every human.** `bot_id` / `app_id` exist only on
  app-authored messages, so a trigger filtered by one *cannot* fire for a person
  writing in the same room — the filter is not "prefer this bot", it is "bots, and
  only this one". When a loop must answer **both** an app and humans in one channel,
  give it **two triggers**: one filtered by `bot_id` (or `app_id`), one by
  `sender_kind: "human"`. Triggers are OR-ed, filters within a trigger are AND-ed —
  there is no way to express this with a single trigger. A `sender_kind: "human"`
  trigger also cannot self-trigger on durin's own posts: durin writes as an app, so
  its messages arrive as `sender_kind: "bot"`.
- **Goal** = natural-language `intent` + typed `checks`. A `script` check passes iff
  its command exits 0 — it cannot be sweet-talked; an `assertion` is judged against the
  run's evidence. A failing **required** check blocks `done` no matter what the judge
  thinks; the judge can only be stricter than the checks. With `"checks_sufficient":
  true` (script checks only, at least one required), passing checks alone decide —
  zero LLM calls, ideal for monitor-style loops.
- **Concurrency**: `single` (default) = one active case; new matching events queue and
  drain as cases finish. `parallel` = one run per case.
- **`stuck_after`**: consecutive no-progress verifications before the loop escalates
  to the operator instead of silently spinning.

## Runs, waking, and the two human lanes

A run's status is one of `running · needs_operator · waiting_info · done · no_goal ·
escalated · error`. Two distinct humans may be in the picture:

- **The counterpart** — whoever is on the other side of the case (the customer who
  mailed in). When the workflow needs *their* answer, it ends at a `__needs_input__`
  gate with the ask prefixed `[TO:counterpart]`: the run parks as `waiting_info`, the
  question is delivered **in-context on the origin channel** (same mail thread, same
  telegram topic, same slack thread), and their reply wakes exactly that run.
- **The operator** — the durin owner. Untagged asks, escalations, and errors land in
  the webui's Loops → Activity inbox (`needs_operator`), answerable there, via the
  `loops` tool, or on the configured `operator_channel`.

The `[TO:counterpart]` contract has one structural requirement: the workflow must
actually **end `needs_input`** (a `cases` route to the reserved `__needs_input__`
terminal — see the `workflows` skill). A workflow that merely *completes* with that
text as final output ends the run; there is nothing to wake.

## Using the `loops` tool

- `loops(action="list")` — what exists, enabled state, active runs. Check this before
  creating: the standing work may already be defined.
- `loops(action="status", name=…)` — a loop's recent runs and pending asks.
- `loops(action="fire", name=…, task?)` — start a run now (manual trigger).
- `loops(action="answer", name=…, run_id=…, answer=…)` — reply to a waiting run.
- `loops(action="enable"/"pause", name=…)` — pause removes the standing triggers;
  definitions and history stay.
- `loops(action="create", definition=<JSON>)` — same validation as the webui; a bad
  schedule, filter set, or correlate regex is rejected at save time with the reason.
  **This is also how you change one.** There is no separate update action: `create`
  with an existing `name` replaces that definition wholesale (versioned as an edit, so
  the previous one stays recoverable). Send the *complete* definition — anything you
  leave out is gone, not merged.

Neither `list` nor `status` prints triggers, filters or checks, so to change one part of
a live loop, first **read** `loops/<name>.json` (reads are allowed), then send the whole
edited definition through `action="create"`. Do not write that file: `loops/` is closed
to generic file writes, because this tool is what validates the schedule/filters and
records the change in the version store.

Creating a loop is standing, autonomous behavior with a human lane attached — confirm
the user actually wants recurring/parked work (and on which channel replies should
flow) before defining one on their behalf.

## Designing the goal — the part that decides everything

The goal is what separates a loop from a scheduled prompt. Write `intent` as an
outcome, not an activity ("the report is published and linked", not "work on the
report"). Prefer at least one **required `script` check** — a verifiable, exit-code
fact the run cannot argue with. Reserve `assertion` checks for judgments only evidence
can settle. If every check can be a script, set `checks_sufficient` and the loop needs
no judge at all. A loop whose goal cannot be verified will oscillate between `no_goal`
and escalation — that is a definition smell, not a runtime bug.
