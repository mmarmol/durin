import { describe, expect, it } from "vitest";

import { renderBodyLines } from "@/components/thread/ToolCallBlock";
import type { ToolProgressEvent } from "@/lib/types";

/**
 * execute_code returns a JSON envelope as its "text result". renderBodyLines
 * must parse it and show only the script's stdout + any error — never the raw
 * {"status":...,"output":...,"error":...} blob (which previously dumped the
 * traceback verbatim, once from `output` and once from `error`).
 */
describe("renderBodyLines: execute_code", () => {
  it("renders the error from the envelope in red, hiding the envelope keys", () => {
    const ev: ToolProgressEvent = {
      phase: "end",
      name: "execute_code",
      result: JSON.stringify({
        status: "error",
        output: "",
        tool_calls_made: 1,
        duration_seconds: 0.06,
        error: "Traceback (most recent call last):\n  ValueError: boom",
      }),
    };
    const lines = renderBodyLines(ev);
    const text = lines.map((l) => l.text).join("\n");
    expect(text).toContain("ValueError: boom");
    expect(text).not.toContain("tool_calls_made");
    expect(text).not.toContain("duration_seconds");
    expect(text).not.toContain('"status"');
    expect(lines.every((l) => l.className?.includes("red"))).toBe(true);
  });

  it("renders stdout in muted text and no red lines on success", () => {
    const ev: ToolProgressEvent = {
      phase: "end",
      name: "execute_code",
      result: JSON.stringify({
        status: "success",
        output: "hello\nworld",
        tool_calls_made: 0,
        duration_seconds: 0.01,
      }),
    };
    const lines = renderBodyLines(ev);
    expect(lines.map((l) => l.text)).toEqual(["hello", "world"]);
    expect(lines.some((l) => l.className?.includes("red"))).toBe(false);
  });

  it("shows both stdout and the error when a script prints before crashing", () => {
    const ev: ToolProgressEvent = {
      phase: "end",
      name: "execute_code",
      result: JSON.stringify({
        status: "error",
        output: "PARTIAL-OUTPUT",
        tool_calls_made: 0,
        duration_seconds: 0.01,
        error: "ValueError: boom",
      }),
    };
    const lines = renderBodyLines(ev);
    const muted = lines.filter((l) => !l.className?.includes("red")).map((l) => l.text);
    const red = lines.filter((l) => l.className?.includes("red")).map((l) => l.text);
    expect(muted).toContain("PARTIAL-OUTPUT");
    expect(red.join("\n")).toContain("ValueError: boom");
  });
});
