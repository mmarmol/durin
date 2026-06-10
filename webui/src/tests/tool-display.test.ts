import { describe, expect, it } from "vitest";

import { toolDisplayClass } from "@/lib/tool-display";

describe("toolDisplayClass", () => {
  it("hoists interactive/presentational tools", () => {
    for (const name of [
      "ask_user_question",
      "request_secret",
      "todo_write",
      "exit_plan_mode",
    ]) {
      expect(toolDisplayClass(name)).toBe("hoist");
    }
  });

  it("chips lifecycle/confirmation tools", () => {
    for (const name of [
      "spawn",
      "cron",
      "message",
      "sleep",
      "complete_goal",
      "long_task",
      "enter_plan_mode",
      "subagent_stop",
    ]) {
      expect(toolDisplayClass(name)).toBe("chip");
    }
  });

  it("defaults to trace", () => {
    expect(toolDisplayClass("read_file")).toBe("trace");
    expect(toolDisplayClass("exec")).toBe("trace");
    expect(toolDisplayClass(undefined)).toBe("trace");
  });
});
