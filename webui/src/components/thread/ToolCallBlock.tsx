import { useState } from "react";
import { Check, Loader2, X } from "lucide-react";

import { cn } from "@/lib/utils";
import type { ToolProgressEvent } from "@/lib/types";

/**
 * Rich render of one structured tool-call event — the webui counterpart
 * of the terminal TUI's ToolCallBubble. The server already sends
 * structured ``tool_events`` (name / arguments / result / phase); this
 * component turns one merged event into:
 *
 * - ``edit_file`` → a coloured +/- diff of old vs new text.
 * - ``exec``      → ``$ command`` followed by its output.
 * - ``read_file`` / ``list_dir`` / ``grep`` / generic → result preview.
 *
 * Long bodies collapse to PREVIEW_LINES with a ``+N more`` toggle,
 * matching the TUI ergonomics. It does NOT add an outer fold — the
 * caller (TraceGroup) already provides the collapsible group.
 */

const PREVIEW_LINES = 6;

type Phase = "start" | "end" | "error" | string;

function phaseGlyph(phase: Phase | undefined) {
  if (phase === "end") return <Check className="h-3 w-3 text-emerald-500" aria-hidden />;
  if (phase === "error") return <X className="h-3 w-3 text-red-500" aria-hidden />;
  return <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" aria-hidden />;
}

function argString(args: unknown, key: string): string | null {
  if (!args || typeof args !== "object") return null;
  const v = (args as Record<string, unknown>)[key];
  return typeof v === "string" && v ? v : null;
}

/** Read an argument expected to be a list of strings (e.g. ask_user options). */
function argStringList(args: unknown, key: string): string[] {
  if (!args || typeof args !== "object") return [];
  const v = (args as Record<string, unknown>)[key];
  if (!Array.isArray(v)) return [];
  return v.map((o) => String(o).trim()).filter(Boolean);
}

/** One-line summary of what the call operates on (path / command / url / query). */
function summaryLine(ev: ToolProgressEvent): string {
  const a = ev.arguments;
  for (const key of [
    "path", "file_path", "filename", "command",
    "url", "query", "pattern", "question", "name",
  ]) {
    const v = argString(a, key);
    if (v) return v.length <= 90 ? v : v.slice(0, 87) + "…";
  }
  return "";
}

/** Coerce a result (string | {output} | object) into a display string. */
function resultText(result: unknown): string {
  if (result == null) return "";
  if (typeof result === "string") return result;
  if (typeof result === "object") {
    const out = (result as Record<string, unknown>).output;
    if (typeof out === "string") return out;
    try {
      return JSON.stringify(result, null, 2);
    } catch {
      return String(result);
    }
  }
  return String(result);
}

interface ToolCallBlockProps {
  event: ToolProgressEvent;
}

export function ToolCallBlock({ event }: ToolCallBlockProps) {
  const [expanded, setExpanded] = useState(false);
  const name = event.name || "tool";
  const summary = summaryLine(event);

  const bodyLines = renderBodyLines(event);
  const total = bodyLines.length;
  const truncated = !expanded && total > PREVIEW_LINES;
  const visible = truncated ? bodyLines.slice(0, PREVIEW_LINES) : bodyLines;

  return (
    <div
      className={cn(
        "rounded-md border-l-2 pl-2.5",
        event.phase === "error"
          ? "border-red-500/70"
          : event.phase === "end"
            ? "border-emerald-500/60"
            : "border-muted-foreground/40",
      )}
    >
      <div className="flex items-center gap-1.5 py-0.5 text-[11.5px]">
        {phaseGlyph(event.phase)}
        <span className="font-semibold text-foreground/90">{name}</span>
        {summary && (
          <span className="min-w-0 truncate font-mono text-muted-foreground/80">{summary}</span>
        )}
        {total > PREVIEW_LINES && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="ml-auto shrink-0 rounded px-1 text-[10.5px] text-muted-foreground underline-offset-2 hover:underline"
          >
            {expanded ? "collapse" : `+${total - PREVIEW_LINES} more`}
          </button>
        )}
      </div>
      {visible.length > 0 && (
        <pre className="overflow-x-auto whitespace-pre-wrap break-words pb-1 font-mono text-[11px] leading-relaxed">
          {visible.map((ln, i) => (
            <div key={i} className={ln.className}>
              {ln.text || " "}
            </div>
          ))}
        </pre>
      )}
    </div>
  );
}

interface BodyLine {
  text: string;
  className?: string;
}

/** Build the per-tool body as a list of (text, className) lines. */
function renderBodyLines(ev: ToolProgressEvent): BodyLine[] {
  const name = ev.name || "";

  if (name === "edit_file") {
    const oldText = argString(ev.arguments, "old_text") ?? "";
    const newText = argString(ev.arguments, "new_text") ?? "";
    const lines: BodyLine[] = [];
    for (const l of oldText.split("\n")) {
      lines.push({ text: `- ${l}`, className: "text-red-500/90" });
    }
    for (const l of newText.split("\n")) {
      lines.push({ text: `+ ${l}`, className: "text-emerald-500/90" });
    }
    return lines;
  }

  if (name === "exec") {
    const cmd = argString(ev.arguments, "command") ?? "";
    const lines: BodyLine[] = [];
    if (cmd) lines.push({ text: `$ ${cmd}`, className: "text-cyan-500/90" });
    const out = resultText(ev.result);
    if (ev.error) {
      lines.push({ text: String(ev.error), className: "text-red-500/90" });
    } else if (out) {
      for (const l of out.split("\n")) {
        lines.push({ text: l, className: "text-muted-foreground/90" });
      }
    }
    return lines;
  }

  // ask_user_question: the question + numbered options, built from the
  // call arguments — the raw result is an internal YIELD instruction.
  if (name === "ask_user_question") {
    const question = argString(ev.arguments, "question") ?? "";
    const lines: BodyLine[] = [];
    if (question) {
      lines.push({ text: `❓ ${question}`, className: "text-foreground/90" });
    }
    argStringList(ev.arguments, "options").forEach((opt, i) => {
      lines.push({ text: `   ${i + 1}. ${opt}`, className: "text-cyan-500/90" });
    });
    return lines;
  }

  // request_secret: what is needed + the command to store it. The
  // secret value never flows through here.
  if (name === "request_secret") {
    const secretName = argString(ev.arguments, "name") ?? "";
    const service = argString(ev.arguments, "service") ?? "";
    const purpose = argString(ev.arguments, "purpose") ?? "";
    const lines: BodyLine[] = [
      {
        text: `🔑 ${secretName || "(unnamed secret)"}${service ? `  · ${service}` : ""}`,
        className: "text-foreground/90",
      },
    ];
    if (purpose) {
      lines.push({ text: `   ${purpose}`, className: "text-muted-foreground/90" });
    }
    if (resultText(ev.result).includes("already exists")) {
      lines.push({
        text: "   already stored — nothing to do",
        className: "text-emerald-500/90",
      });
    } else if (secretName && service) {
      lines.push({
        text: `   $ durin secret set ${secretName} --service ${service} --scope exec`,
        className: "text-cyan-500/90",
      });
    }
    return lines;
  }

  // Generic / read_file / list_dir / grep: just the result (or error).
  if (ev.error) {
    return [{ text: String(ev.error), className: "text-red-500/90" }];
  }
  const out = resultText(ev.result);
  if (!out) return [];
  return out.split("\n").map((l) => ({
    text: l,
    className: "text-muted-foreground/90",
  }));
}
