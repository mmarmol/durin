import { describe, expect, it } from "vitest";

import { mergeToolEvents } from "@/lib/tool-traces";
import type { ToolProgressEvent } from "@/lib/types";

/**
 * mergeToolEvents folds the server's per-phase tool events (start, then
 * end/error) into one entry per call_id so the UI can render a single
 * rich block per call — the webui counterpart of the terminal TUI's
 * start→end ToolCallBubble folding.
 */
describe("mergeToolEvents", () => {
  it("appends a fresh start event", () => {
    const out = mergeToolEvents(undefined, [
      { phase: "start", call_id: "c1", name: "exec", arguments: { command: "ls" } },
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].name).toBe("exec");
    expect(out[0].phase).toBe("start");
  });

  it("merges an end event into the matching start by call_id", () => {
    const start: ToolProgressEvent = {
      phase: "start", call_id: "c1", name: "exec", arguments: { command: "ls" },
    };
    const afterStart = mergeToolEvents(undefined, [start]);
    const afterEnd = mergeToolEvents(afterStart, [
      { phase: "end", call_id: "c1", result: "file1\nfile2" },
    ]);
    expect(afterEnd).toHaveLength(1);
    // name + arguments preserved from the start event…
    expect(afterEnd[0].name).toBe("exec");
    expect(afterEnd[0].arguments).toEqual({ command: "ls" });
    // …phase + result layered from the end event.
    expect(afterEnd[0].phase).toBe("end");
    expect(afterEnd[0].result).toBe("file1\nfile2");
  });

  it("keeps separate entries for distinct call_ids", () => {
    const out = mergeToolEvents(undefined, [
      { phase: "start", call_id: "a", name: "exec" },
      { phase: "start", call_id: "b", name: "read_file" },
    ]);
    expect(out).toHaveLength(2);
    expect(out.map((e) => e.name)).toEqual(["exec", "read_file"]);
  });

  it("merges an error event into the matching start", () => {
    const afterStart = mergeToolEvents(undefined, [
      { phase: "start", call_id: "x", name: "exec", arguments: { command: "bad" } },
    ]);
    const afterErr = mergeToolEvents(afterStart, [
      { phase: "error", call_id: "x", error: "command not found" },
    ]);
    expect(afterErr).toHaveLength(1);
    expect(afterErr[0].phase).toBe("error");
    expect(afterErr[0].error).toBe("command not found");
    expect(afterErr[0].name).toBe("exec");
  });

  it("appends events with no call_id rather than dropping them", () => {
    const out = mergeToolEvents(undefined, [
      { phase: "start", name: "exec" },
      { phase: "start", name: "grep" },
    ]);
    expect(out).toHaveLength(2);
  });

  it("ignores non-array input", () => {
    expect(mergeToolEvents(undefined, null)).toEqual([]);
    expect(mergeToolEvents(undefined, "nope")).toEqual([]);
    const existing = [{ phase: "start", call_id: "c1", name: "exec" }];
    expect(mergeToolEvents(existing, undefined)).toEqual(existing);
  });

  it("skips non-object entries in the incoming array", () => {
    const out = mergeToolEvents(undefined, [
      null,
      "garbage",
      { phase: "start", call_id: "c1", name: "exec" },
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].name).toBe("exec");
  });
});
