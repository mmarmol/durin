/**
 * Display class per tool name. Decides where a structured tool event renders:
 *
 * - "hoist": first-class block in the thread flow (interactive panels, plan
 *   card, todo checklist) — never inside the collapsed activity cluster.
 * - "chip":  compact one-line confirmation rendered next to the cluster
 *   (job created, message sent, subagent spawned…).
 * - "trace": supporting evidence inside the collapsible cluster (default).
 *
 * Backend counterpart: durin/agent/user_payloads.py (channel contract — the
 * model no longer re-presents these payloads in prose; the channel renders
 * them from the tool arguments).
 */
export type ToolDisplayClass = "hoist" | "chip" | "trace";

const HOISTED = new Set([
  "ask_user_question",
  "request_secret",
  "todo_write",
  "exit_plan_mode",
]);

const CHIPPED = new Set([
  "spawn",
  "subagent_stop",
  "cron",
  "message",
  "sleep",
  "complete_goal",
  "long_task",
  "enter_plan_mode",
]);

export function toolDisplayClass(name: string | undefined): ToolDisplayClass {
  if (name && HOISTED.has(name)) return "hoist";
  if (name && CHIPPED.has(name)) return "chip";
  return "trace";
}
