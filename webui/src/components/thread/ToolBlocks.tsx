import type { ReactNode } from "react";
import { Check, ClipboardList, FileText, GitBranch, KeyRound, Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { MarkdownText } from "@/components/MarkdownText";
import { useThreadActions } from "@/components/thread/ThreadActionsContext";
import { AskUserAnswer, RequestSecretPanel } from "@/components/thread/ToolCallBlock";
import { cn } from "@/lib/utils";
import type { ToolProgressEvent } from "@/lib/types";

/**
 * First-class blocks for user-facing tool events, hoisted OUT of the
 * collapsed activity cluster (lib/tool-display.ts decides which). The
 * payload (event.arguments) is the canonical user-facing copy — the model
 * no longer re-presents it in prose (durin/agent/user_payloads.py).
 */

function args(event: ToolProgressEvent): Record<string, unknown> {
  return (event.arguments ?? {}) as Record<string, unknown>;
}

export function HoistedToolBlock({
  event,
  answered,
}: {
  event: ToolProgressEvent;
  answered: boolean;
}) {
  switch (event.name) {
    case "ask_user_question":
      return <AskUserBlock event={event} answered={answered} />;
    case "request_secret":
      return (
        <BlockShell
          tone="accent"
          icon={<KeyRound className="h-3.5 w-3.5" aria-hidden />}
        >
          <RequestSecretPanel event={event} />
        </BlockShell>
      );
    case "todo_write":
      return <TodoListBlock event={event} />;
    case "exit_plan_mode":
      return <PlanBlock event={event} answered={answered} />;
    case "subagent_result":
      return <SubagentResultBlock event={event} />;
    case "workflow_progress":
      return <WorkflowProgressBlock event={event} />;
    default:
      return null;
  }
}

function BlockShell({
  icon,
  children,
  tone = "default",
}: {
  icon: ReactNode;
  children: ReactNode;
  tone?: "default" | "accent";
}) {
  return (
    <div
      className={cn(
        "w-full rounded-lg border px-3 py-2",
        tone === "accent"
          ? "border-primary/35 bg-primary/5"
          : "border-border/60 bg-muted/25",
      )}
    >
      <div className="flex items-start gap-2">
        <span className="mt-0.5 shrink-0 text-muted-foreground">{icon}</span>
        <div className="min-w-0 flex-1">{children}</div>
      </div>
    </div>
  );
}

function AskUserBlock({
  event,
  answered,
}: {
  event: ToolProgressEvent;
  answered: boolean;
}) {
  const { t } = useTranslation();
  const a = args(event);
  const question = typeof a.question === "string" ? a.question : "";
  if (answered) {
    return (
      <BlockShell icon={<Check className="h-3.5 w-3.5 text-emerald-500" aria-hidden />}>
        <div className="text-[13px] text-foreground/85">{question}</div>
        <div className="mt-0.5 text-[11.5px] text-muted-foreground">
          {t("message.askUser.answered")}
        </div>
      </BlockShell>
    );
  }
  return (
    <BlockShell tone="accent" icon={<span aria-hidden>❓</span>}>
      <AskUserAnswer event={event} />
    </BlockShell>
  );
}

function TodoListBlock({ event }: { event: ToolProgressEvent }) {
  const { t } = useTranslation();
  const a = args(event);
  const todos = Array.isArray(a.todos)
    ? (a.todos as Array<Record<string, string>>)
    : [];
  if (todos.length === 0) return null;
  return (
    <BlockShell icon={<ClipboardList className="h-3.5 w-3.5" aria-hidden />}>
      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t("message.todos.title")}
      </div>
      <ul className="space-y-0.5">
        {todos.map((todo, i) => {
          const status = todo.status ?? "pending";
          const label =
            status === "in_progress" ? todo.activeForm || todo.content : todo.content;
          return (
            <li key={i} className="flex items-start gap-2 text-[13px]">
              <span
                aria-hidden
                className={cn(
                  "mt-0.5 shrink-0",
                  status === "completed" && "text-emerald-500",
                  status === "in_progress" && "text-amber-500",
                  status === "pending" && "text-muted-foreground/60",
                )}
              >
                {status === "completed" ? "✔" : status === "in_progress" ? "◐" : "○"}
              </span>
              <span
                className={cn(
                  "min-w-0",
                  status === "completed" &&
                    "text-muted-foreground line-through decoration-muted-foreground/40",
                  status === "in_progress" && "font-medium text-foreground",
                )}
              >
                {label}
              </span>
            </li>
          );
        })}
      </ul>
    </BlockShell>
  );
}

function PlanBlock({
  event,
  answered,
}: {
  event: ToolProgressEvent;
  answered: boolean;
}) {
  const { t } = useTranslation();
  const actions = useThreadActions();
  const a = args(event);
  const plan = typeof a.plan === "string" ? a.plan : "";
  if (!plan) return null;
  return (
    <BlockShell tone="accent" icon={<FileText className="h-3.5 w-3.5" aria-hidden />}>
      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t("message.plan.title")}
      </div>
      <MarkdownText className="text-[13px]">{plan}</MarkdownText>
      {!answered && actions ? (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => actions.sendUserMessage("/build")}
            className={cn(
              "rounded-md bg-primary px-3 py-1 text-[12px] font-medium",
              "text-primary-foreground hover:opacity-90",
            )}
          >
            {t("message.plan.approve")}
          </button>
          <span className="text-[11px] text-muted-foreground">
            {t("message.plan.hint")}
          </span>
        </div>
      ) : null}
    </BlockShell>
  );
}

function SubagentResultBlock({ event }: { event: ToolProgressEvent }) {
  const { t } = useTranslation();
  const a = args(event);
  const label = typeof a.label === "string" ? a.label : "";
  const task = typeof a.task === "string" ? a.task : "";
  const running = event.phase === "running";
  const failed = event.phase === "error";
  const steps = event.progress?.iteration ?? 0;
  const tool = event.progress?.tool ?? "";
  const body =
    typeof event.error === "string" && failed
      ? event.error
      : typeof event.result === "string"
        ? event.result
        : "";
  return (
    <BlockShell icon={<span aria-hidden>{failed ? "🛑" : "🤖"}</span>}>
      <div className="mb-0.5 flex flex-wrap items-baseline gap-x-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          {t("message.subagent.title")} · {label}
        </span>
        {task ? (
          <span className="min-w-0 truncate text-[11.5px] text-muted-foreground/80">
            {task}
          </span>
        ) : null}
      </div>
      {running ? (
        <span className="text-[12px] text-muted-foreground">
          {t("message.subagent.running", { steps })}
          {tool ? ` · ${tool}` : ""}
        </span>
      ) : body ? (
        <div
          className={cn(
            "max-h-72 overflow-y-auto scrollbar-thin",
            failed && "text-red-500/90",
          )}
        >
          <MarkdownText className="text-[13px]">{body}</MarkdownText>
        </div>
      ) : null}
    </BlockShell>
  );
}

function WorkflowProgressBlock({ event }: { event: ToolProgressEvent }) {
  const { t } = useTranslation();
  const wf = (event.arguments as { workflow?: string } | undefined)?.workflow ?? "";
  const nodes = event.nodes ?? [];
  const icon = (s: string) =>
    s === "done" ? <Check className="h-3 w-3 text-emerald-600" aria-hidden />
    : s === "failed" ? <X className="h-3 w-3 text-destructive" aria-hidden />
    : <Loader2 className="h-3 w-3 animate-spin text-amber-600" aria-hidden />;
  return (
    <BlockShell icon={<GitBranch className="h-3.5 w-3.5" aria-hidden />}>
      <div className="mb-0.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t("message.workflow.title")}{wf ? ` · ${wf}` : ""}
      </div>
      <ul className="flex flex-col gap-1">
        {nodes.map((n) => (
          <li key={n.id} className="flex items-center gap-2 text-[12.5px]">
            {icon(n.status)}<span>{n.id}</span>
          </li>
        ))}
      </ul>
    </BlockShell>
  );
}

/** Compact one-line confirmations (display class "chip"). */
export function ToolChipRow({ events }: { events: ToolProgressEvent[] }) {
  if (events.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5 px-2">
      {events.map((event, i) => (
        <span
          key={event.call_id ?? i}
          className={cn(
            "inline-flex items-center gap-1 rounded-full border border-border/60",
            "bg-muted/30 px-2.5 py-0.5 text-[11.5px] text-muted-foreground",
          )}
        >
          {chipLabel(event)}
        </span>
      ))}
    </div>
  );
}

function chipLabel(event: ToolProgressEvent): string {
  const a = args(event);
  const s = (key: string) => (typeof a[key] === "string" ? (a[key] as string) : "");
  switch (event.name) {
    case "spawn":
      return `🤖 spawn ${s("name") || s("task").slice(0, 40)}`.trim();
    case "subagent_stop":
      return `🛑 subagent ${s("name")}`.trim();
    case "cron":
      return `⏰ cron ${s("action")} ${s("name")}`.trim();
    case "message":
      return `📤 ${s("channel") || "message"}`;
    case "sleep":
      return `⏳ ${s("reason") || `sleep ${a.seconds ?? ""}s`}`.trim();
    case "complete_goal":
      return "🏁 goal completed";
    case "long_task":
      return `🎯 ${s("ui_summary") || s("goal").slice(0, 40) || "long task"}`.trim();
    case "enter_plan_mode":
      return "📐 plan mode";
    case "memory_store":
      return "🧠 memory saved";
    case "memory_upsert_entity":
      return `🧠 ${s("ref") || "entity updated"}`;
    case "memory_forget":
      return `🧠 forgot ${s("uri")}`.trim();
    case "skill_import":
      return `🧩 import ${s("source").slice(0, 40)}`.trim();
    default:
      return event.name ?? "tool";
  }
}
