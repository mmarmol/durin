import { describe, expect, it } from "vitest";

import { eventDisplayClass, toolDisplayClass } from "@/lib/tool-display";

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
      "tasks",
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

describe("toolDisplayClass — tier-3 chips", () => {
  it("chips memory/skill write operations", () => {
    for (const name of ["memory_store", "memory_upsert_entity", "memory_forget", "skill_import"]) {
      expect(toolDisplayClass(name)).toBe("chip");
    }
  });
});

describe("toolDisplayClass — subagent result", () => {
  it("hoists subagent_result as a first-class card", () => {
    expect(toolDisplayClass("subagent_result")).toBe("hoist");
  });
});

describe("eventDisplayClass — failed calls", () => {
  it("keeps hoisting and chipping successful calls", () => {
    expect(eventDisplayClass({ name: "exit_plan_mode", phase: "end" })).toBe("hoist");
    expect(eventDisplayClass({ name: "exit_plan_mode", phase: "start" })).toBe("hoist");
    expect(eventDisplayClass({ name: "message", phase: "end" })).toBe("chip");
  });

  it("demotes a rejected call to trace instead of a plan card", () => {
    // A rejected exit_plan_mode used to paint a full plan card with an
    // approve button, so a retry showed the user two plans in a row.
    expect(eventDisplayClass({ name: "exit_plan_mode", phase: "error" })).toBe("trace");
    expect(eventDisplayClass({ name: "message", phase: "error" })).toBe("trace");
    expect(eventDisplayClass({ name: "ask_user_question", phase: "error" })).toBe("trace");
  });
});
