/** Drop duplicate tool_call objects (same id or identical formatted trace). */
export function dedupeToolCallsForUi(calls: unknown): unknown[] {
  if (!Array.isArray(calls) || calls.length === 0) return [];
  const seen = new Set<string>();
  const out: unknown[] = [];
  for (const c of calls) {
    let key: string | null = null;
    if (c && typeof c === "object" && "id" in c) {
      const id = (c as { id?: unknown }).id;
      if (typeof id === "string" && id.length > 0) key = `id:${id}`;
    }
    if (key == null) {
      key = formatToolCallTrace(c) ?? "";
    }
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(c);
  }
  return out;
}

export function formatToolCallTrace(call: unknown): string | null {
  if (!call || typeof call !== "object") return null;
  const item = call as {
    name?: unknown;
    arguments?: unknown;
    function?: { name?: unknown; arguments?: unknown };
  };
  const name =
    typeof item.function?.name === "string"
      ? item.function.name
      : typeof item.name === "string"
        ? item.name
        : "";
  if (!name) return null;
  const args = item.function?.arguments ?? item.arguments;
  if (typeof args === "string" && args.trim()) return `${name}(${args})`;
  if (args && typeof args === "object") return `${name}(${JSON.stringify(args)})`;
  return `${name}()`;
}

export function toolTraceLinesFromEvents(events: unknown): string[] {
  if (!Array.isArray(events)) return [];
  return events
    .filter((event) => {
      if (!event || typeof event !== "object") return false;
      return (event as { phase?: unknown }).phase === "start";
    })
    .map(formatToolCallTrace)
    .filter((trace): trace is string => !!trace);
}

import type { ToolProgressEvent } from "@/lib/types";

/**
 * Merge a fresh batch of tool events into an accumulator, keyed by
 * ``call_id``. The server emits one event per phase (``start`` then
 * ``end``/``error``); the UI wants ONE entry per call carrying the
 * latest phase + result so a block can show both the invocation and
 * its outcome — the same start→end folding the terminal TUI does.
 *
 * - A ``start`` event with a new ``call_id`` appends a fresh entry.
 * - An ``end``/``error`` event updates the matching entry in place
 *   (phase, result, error, files, embeds), preserving the original
 *   ``name`` / ``arguments`` from the ``start`` event.
 * - Events with no ``call_id`` (older payloads) are appended as-is so
 *   nothing is silently dropped.
 */
export function mergeToolEvents(
  existing: ToolProgressEvent[] | undefined,
  incoming: unknown,
): ToolProgressEvent[] {
  const out: ToolProgressEvent[] = existing ? [...existing] : [];
  if (!Array.isArray(incoming)) return out;
  for (const raw of incoming) {
    if (!raw || typeof raw !== "object") continue;
    const ev = raw as ToolProgressEvent;
    const callId = typeof ev.call_id === "string" ? ev.call_id : "";
    if (!callId) {
      out.push(ev);
      continue;
    }
    const idx = out.findIndex((e) => e.call_id === callId);
    if (idx === -1) {
      out.push(ev);
      continue;
    }
    // Update in place: keep name/arguments from whatever we had, layer
    // the newer phase + outcome fields on top.
    out[idx] = {
      ...out[idx],
      ...ev,
      name: ev.name ?? out[idx].name,
      arguments: ev.arguments ?? out[idx].arguments,
    };
  }
  return out;
}
