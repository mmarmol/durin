import { useState } from "react";
import { ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import type {
  ApprovalDecision,
  ApprovalDecisionResult,
  PendingApproval,
} from "@/lib/types";

/** Detail keys with a rendering of their own. Every other key shows as a
 *  plain `key: value` row, so a new kind's detail needs no change here. */
const RENDERED_KEYS = new Set(["verdict", "findings", "new_findings", "command", "diff"]);

/** Same tones as the skills security chip, so a verdict reads the same
 *  everywhere. An unknown verdict falls back to muted. */
const VERDICT_TONE: Record<string, string> = {
  safe: "bg-primary/10 text-primary",
  caution: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  dangerous: "bg-destructive/10 text-destructive",
};

function displayValue(value: unknown): string {
  if (value == null) return "";
  if (Array.isArray(value)) return value.map(displayValue).filter(Boolean).join(", ");
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/** True for a detail value that reads better pretty-printed (an object or
 *  array) than as the one-line JSON `displayValue` gives every other kind of
 *  value — an MCP `config`, for example. */
function isNestedValue(value: unknown): boolean {
  return value !== null && typeof value === "object";
}

function displayNested(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return displayValue(value);
  }
}

/**
 * One scan finding as text. A skill-scan finding is `{category, severity,
 * where, detail}` (durin/agent/skills_store.py) and reads as
 * `[severity] category: detail (where)`, omitting empty parts. A finding
 * with none of those three fields falls back to `message`/`title`/
 * `description`/`rule` (other kinds' shapes), and a plain string or number
 * renders as-is. JSON is the last resort, for a shape this does not know.
 */
function findingLine(finding: unknown): string {
  if (finding && typeof finding === "object" && !Array.isArray(finding)) {
    const f = finding as Record<string, unknown>;
    const severity = displayValue(f.severity ?? f.level);
    const category = displayValue(f.category);
    const where = displayValue(f.where);
    const detail = displayValue(f.detail);
    const isScanShaped = category !== "" || where !== "" || detail !== "";
    const text = isScanShaped
      ? [category, detail].filter(Boolean).join(": ")
      : displayValue(f.message ?? f.title ?? f.description ?? f.rule) || displayValue(f);
    let line = severity ? `[${severity}] ${text}`.trim() : text;
    if (isScanShaped && where) line += ` (${where})`;
    return line;
  }
  return displayValue(finding);
}

function diffTone(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "text-muted-foreground";
  if (line.startsWith("@@")) return "text-cyan-600 dark:text-cyan-400";
  if (line.startsWith("+")) return "text-emerald-600 dark:text-emerald-400";
  if (line.startsWith("-")) return "text-red-600 dark:text-red-400";
  return "text-foreground/80";
}

/**
 * An approval a turn is waiting on: what would run, what was reviewed (scan
 * verdict, findings, command, diff, other detail) and Approve / Reject.
 *
 * The verdict leaves only through `onDecide`, never as a chat message, so
 * the model can neither see nor forge it. In a chat the caller passes the
 * socket decision. A list of pending items can pass its own call. The card
 * reports the outcome of the call. It disappears when the server's snapshot
 * no longer carries the approval.
 */
export function ApprovalCard({
  approval,
  onDecide,
}: {
  approval: PendingApproval;
  onDecide: (decision: ApprovalDecision) => Promise<ApprovalDecisionResult>;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<"idle" | "sending" | "done" | "error">("idle");
  const [note, setNote] = useState("");

  const detail = approval.detail ?? {};
  const verdict = typeof detail.verdict === "string" ? detail.verdict : "";
  const findings: unknown[] = Array.isArray(detail.findings) ? detail.findings : [];
  const newFindings: unknown[] = Array.isArray(detail.new_findings) ? detail.new_findings : [];
  const command = typeof detail.command === "string" ? detail.command : "";
  const diff = typeof detail.diff === "string" ? detail.diff : "";
  const rows = Object.entries(detail).filter(
    ([key, value]) => !RENDERED_KEYS.has(key) && displayValue(value) !== "",
  );
  const locked = state === "sending" || state === "done";

  const decide = async (decision: ApprovalDecision) => {
    setState("sending");
    setNote("");
    try {
      const result = await onDecide(decision);
      const outcome: Record<string, string> = {
        pending: t("message.approval.handedOff"),
        applied: t("message.approval.applied"),
        rejected: t("message.approval.rejected"),
      };
      setNote(outcome[result.status] ?? result.message);
      setState("done");
    } catch (err) {
      setNote(err instanceof Error ? err.message : String(err));
      setState("error");
    }
  };

  return (
    <div
      role="region"
      aria-label={t("message.approval.title")}
      className={cn(
        "mb-2 rounded-lg border border-border/60 border-l-2 border-l-amber-500",
        "bg-muted/25 px-3 py-2",
      )}
    >
      <div className="flex items-start gap-2">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" aria-hidden />
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-[12.5px] font-semibold text-foreground">
              {t("message.approval.title")}
            </span>
            <span className="text-[11px] text-muted-foreground">
              {t(`message.approval.kind.${approval.kind}`, { defaultValue: approval.kind })}
            </span>
          </div>
          <div className="text-[13px] leading-snug text-foreground/90">{approval.summary}</div>
          <div className="max-h-[40vh] space-y-1.5 overflow-y-auto">
            {verdict ? (
              <div className="flex items-center gap-1.5 text-[11.5px]">
                <span className="text-muted-foreground">{t("message.approval.verdict")}</span>
                <span
                  className={cn(
                    "rounded-full px-2 py-0.5 text-[11px] font-medium leading-none",
                    VERDICT_TONE[verdict] ?? "bg-muted text-muted-foreground",
                  )}
                >
                  {t(`skills.verdict.${verdict}`, { defaultValue: verdict })}
                </span>
              </div>
            ) : null}
            {findings.length > 0 ? (
              <div className="text-[11.5px]">
                <div className="text-muted-foreground">{t("message.approval.findings")}</div>
                <ul className="list-disc pl-4 text-foreground/85">
                  {findings.map((finding, i) => (
                    <li key={i}>{findingLine(finding)}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {newFindings.length > 0 ? (
              <div className="text-[11.5px]">
                <div className="text-muted-foreground">{t("message.approval.newFindings")}</div>
                <ul className="list-disc pl-4 text-foreground/85">
                  {newFindings.map((finding, i) => (
                    <li key={i}>{findingLine(finding)}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {command ? (
              <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] text-cyan-600 dark:text-cyan-400">
                {`$ ${command}`}
              </pre>
            ) : null}
            {rows.length > 0 ? (
              <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-[11.5px]">
                {rows.map(([key, value]) => (
                  <div key={key} className="contents">
                    <dt className="text-muted-foreground">{key}</dt>
                    <dd className="min-w-0 break-words font-mono text-foreground/85">
                      {isNestedValue(value) ? (
                        <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md bg-background/60 p-1.5 text-[11px]">
                          {displayNested(value)}
                        </pre>
                      ) : (
                        displayValue(value)
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
            ) : null}
            {diff ? (
              <pre className="overflow-x-auto rounded-md bg-background/60 p-2 font-mono text-[11px] leading-relaxed">
                {diff.split("\n").map((line, i) => (
                  <div key={i} className={diffTone(line)}>
                    {line || " "}
                  </div>
                ))}
              </pre>
            ) : null}
          </div>
          <div className="text-[11px] text-muted-foreground">{t("message.approval.hint")}</div>
          <div className="flex flex-wrap items-center gap-2 pt-0.5">
            <button
              type="button"
              disabled={locked}
              onClick={() => void decide("approve")}
              className={cn(
                "rounded-md bg-primary px-3 py-1 text-[12px] font-medium",
                "text-primary-foreground hover:opacity-90 disabled:opacity-40",
              )}
            >
              {t("message.approval.approve")}
            </button>
            <button
              type="button"
              disabled={locked}
              onClick={() => void decide("reject")}
              className={cn(
                "rounded-md border border-border/70 px-3 py-1 text-[12px] font-medium",
                "text-foreground/85 hover:bg-muted/60 disabled:opacity-40",
              )}
            >
              {t("message.approval.reject")}
            </button>
            {state === "sending" ? (
              <span className="text-[11px] text-muted-foreground">
                {t("message.approval.sending")}
              </span>
            ) : null}
            {state === "done" ? (
              <span className="text-[11.5px] text-emerald-600/90 dark:text-emerald-400/90">
                {`✓ ${note}`}
              </span>
            ) : null}
            {state === "error" ? (
              <span role="alert" className="text-[11.5px] text-red-500">
                {note}
              </span>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
