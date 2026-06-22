---
name: cron
description: Schedule reminders and recurring tasks.
---

# Cron

Use the `cron` tool to schedule reminders or recurring tasks.

## Mode: reminder vs task

The `mode` parameter (default `reminder`) controls what happens when the job fires:

1. **reminder** — the `message` is delivered to the user as a brief, natural message.
2. **task** — the `message` is a task description; the agent executes it with full tools
   and delivers the result only if it produced something useful.

`mode` is independent of the schedule. A one-time job is just a job with an `at` schedule
(it auto-deletes after running), in either mode.

## Schedule (pick one)

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| 9am Vancouver time daily | cron_expr: "0 9 * * *", tz: "America/Vancouver" |
| once, at a specific time | at: ISO datetime string (compute from current time) |

Use `tz` with `cron_expr` for a specific IANA timezone. Without `tz`, the server's local
timezone is used.

## Optional: per-job model

`model` runs that job with a specific model preset/ref (omit to use the agent's default
model). Useful for routing a heavy recurring task to a cheaper or stronger model.

## Examples

Reminder (default mode):
```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

Task — agent executes each time:
```
cron(action="add", mode="task", message="Check HKUDS/durin GitHub stars and report", every_seconds=600)
```

Task on a specific model:
```
cron(action="add", mode="task", model="<model-ref>", message="Summarize today's commits", cron_expr="0 18 * * 1-5")
```

One-time reminder:
```
cron(action="add", message="Remind me about the meeting", at="<ISO datetime>")
```

List / update / remove:
```
cron(action="list")
cron(action="update", job_id="abc123", mode="task")
cron(action="remove", job_id="abc123")
```

## Notes

- `list` shows each job's recent run history (time, status, duration). System jobs (e.g.
  the daily memory dream) are visible but cannot be removed — only enabled/disabled.
- Jobs can also be created, edited, and run-on-demand from the web dashboard's Cron panel.
