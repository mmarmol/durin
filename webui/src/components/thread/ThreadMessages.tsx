import { MessageBubble } from "@/components/MessageBubble";
import {
  AgentActivityCluster,
  isAgentActivityMember,
} from "@/components/thread/AgentActivityCluster";
import { HoistedToolBlock, ToolChipRow } from "@/components/thread/ToolBlocks";
import { toolDisplayClass } from "@/lib/tool-display";
import type { ToolProgressEvent, UIMessage } from "@/lib/types";

interface ThreadMessagesProps {
  messages: UIMessage[];
  /** When true, agent turn still in flight — keeps activity cluster expanded. */
  isStreaming?: boolean;
  /** Retry callback — passed to the last finished assistant message only. */
  onRetryLast?: () => void;
  /** Edit callback — passed to the last user message only. */
  onEditLastUser?: () => void;
}

export type DisplayUnit =
  | { type: "cluster"; messages: UIMessage[] }
  | { type: "single"; message: UIMessage }
  | { type: "toolBlock"; event: ToolProgressEvent; answered: boolean; key: string }
  | { type: "toolChips"; events: ToolProgressEvent[]; key: string };

/** True when this unit index is the last assistant text slice before the next user message (or end of thread). */
export function isFinalAssistantSliceBeforeNextUser(
  units: DisplayUnit[],
  index: number,
): boolean {
  const u = units[index];
  if (u.type !== "single" || u.message.role !== "assistant") return true;
  for (let j = index + 1; j < units.length; j++) {
    const v = units[j];
    if (v.type === "single" && v.message.role === "user") break;
    return false;
  }
  return true;
}

/**
 * Split a trace message's structured events by display class
 * (lib/tool-display.ts): user-facing events leave the cluster as
 * first-class blocks/chips; plumbing events stay as supporting evidence.
 */
function partitionTrace(m: UIMessage): {
  rest: UIMessage | null;
  hoisted: ToolProgressEvent[];
  chips: ToolProgressEvent[];
} {
  const events = m.toolEvents ?? [];
  if (events.length === 0) return { rest: m, hoisted: [], chips: [] };
  const hoisted = events.filter((e) => toolDisplayClass(e.name) === "hoist");
  const chips = events.filter((e) => toolDisplayClass(e.name) === "chip");
  if (hoisted.length === 0 && chips.length === 0) {
    return { rest: m, hoisted, chips };
  }
  const trace = events.filter((e) => toolDisplayClass(e.name) === "trace");
  const rest = trace.length > 0 ? { ...m, toolEvents: trace } : null;
  return { rest, hoisted, chips };
}

function buildDisplayUnits(messages: UIMessage[]): DisplayUnit[] {
  const out: DisplayUnit[] = [];
  let i = 0;
  while (i < messages.length) {
    const m = messages[i];
    if (isAgentActivityMember(m)) {
      const cluster: UIMessage[] = [];
      const hoisted: { event: ToolProgressEvent; msgId: string }[] = [];
      const chips: { event: ToolProgressEvent; msgId: string }[] = [];
      while (i < messages.length && isAgentActivityMember(messages[i])) {
        const member = messages[i];
        if (member.kind === "trace") {
          const p = partitionTrace(member);
          if (p.rest) cluster.push(p.rest);
          hoisted.push(...p.hoisted.map((event) => ({ event, msgId: member.id })));
          chips.push(...p.chips.map((event) => ({ event, msgId: member.id })));
        } else {
          cluster.push(member);
        }
        i += 1;
      }
      if (cluster.length > 0) out.push({ type: "cluster", messages: cluster });
      if (chips.length > 0) {
        out.push({
          type: "toolChips",
          events: chips.map((c) => c.event),
          key: `chips-${chips[0].msgId}-${chips[0].event.call_id ?? "0"}`,
        });
      }
      // An interaction is answered once any later user message exists.
      const answered = messages.slice(i).some((later) => later.role === "user");
      for (const h of hoisted) {
        out.push({
          type: "toolBlock",
          event: h.event,
          answered,
          key: `block-${h.msgId}-${h.event.call_id ?? "0"}`,
        });
      }
      continue;
    }
    out.push({ type: "single", message: m });
    i += 1;
  }
  return out;
}

export function ThreadMessages({
  messages,
  isStreaming = false,
  onRetryLast,
  onEditLastUser,
}: ThreadMessagesProps) {
  const units = buildDisplayUnits(messages);

  // Find the last single-message unit indices for retry/edit targeting.
  let lastAssistantUnitIdx = -1;
  let lastUserUnitIdx = -1;
  for (let i = units.length - 1; i >= 0; i--) {
    const u = units[i];
    if (u.type !== "single") continue;
    if (lastAssistantUnitIdx === -1 && u.message.role === "assistant" && !u.message.isStreaming) {
      lastAssistantUnitIdx = i;
    }
    if (lastUserUnitIdx === -1 && u.message.role === "user") {
      lastUserUnitIdx = i;
    }
    if (lastAssistantUnitIdx !== -1 && lastUserUnitIdx !== -1) break;
  }

  return (
    <div className="flex w-full flex-col">
      {units.map((unit, index) => {
        const prev = units[index - 1];
        const marginTop =
          index > 0
            ? marginAfterPrevUnit(prev)
            : "";
        const next = units[index + 1];
        const hasBodyBelow =
          unit.type === "cluster"
          && next?.type === "single"
          && next.message.role === "assistant";

        return (
          <div key={unitKey(unit, index)} className={marginTop}>
            {unit.type === "cluster" ? (
              <AgentActivityCluster
                messages={unit.messages}
                isTurnStreaming={isStreaming}
                hasBodyBelow={hasBodyBelow}
              />
            ) : unit.type === "toolBlock" ? (
              <HoistedToolBlock event={unit.event} answered={unit.answered} />
            ) : unit.type === "toolChips" ? (
              <ToolChipRow events={unit.events} />
            ) : (
              <MessageBubble
                message={unit.message}
                showAssistantCopyAction={
                  unit.message.role === "assistant"
                    ? isFinalAssistantSliceBeforeNextUser(units, index)
                    : true
                }
                onRetry={
                  !isStreaming && index === lastAssistantUnitIdx && onRetryLast
                    ? onRetryLast
                    : undefined
                }
                onEdit={
                  index === lastUserUnitIdx && onEditLastUser
                    ? onEditLastUser
                    : undefined
                }
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

function unitKey(unit: DisplayUnit, index: number): string {
  if (unit.type === "cluster") {
    const anchor = unit.messages[0]?.id;
    return anchor != null ? `cluster-${anchor}` : `cluster-idx-${index}`;
  }
  if (unit.type === "toolBlock" || unit.type === "toolChips") {
    return unit.key;
  }
  return unit.message.renderKey ?? unit.message.id;
}

function marginAfterPrevUnit(prev: DisplayUnit): string {
  if (prev.type === "cluster") {
    return "mt-4";
  }
  if (prev.type === "toolBlock" || prev.type === "toolChips") {
    return "mt-2";
  }
  const p = prev.message;
  const denseP =
    p.kind === "trace"
    || (
      p.role === "assistant"
      && p.content.trim().length === 0
      && (!!p.reasoning || !!p.reasoningStreaming)
    );
  if (denseP) {
    return "mt-2";
  }
  return "mt-5";
}
