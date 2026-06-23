import { describe, expect, it } from "vitest";

import { workflowToFlow, type WorkflowDef } from "./workflow-graph";

const DEF: WorkflowDef = {
  name: "wf",
  start: "draft",
  nodes: [
    { id: "draft", kind: "work", next: "gate" },
    { id: "gate", kind: "decision", on_pass: null, on_fail: "draft" },
  ],
};

describe("workflowToFlow", () => {
  it("maps nodes to typed flow nodes, marking the start", () => {
    const { nodes } = workflowToFlow(DEF);
    expect(nodes.map((n) => n.id).sort()).toEqual(["draft", "gate"]);
    const draft = nodes.find((n) => n.id === "draft")!;
    expect(draft.type).toBe("work");
    expect((draft.data as { isStart: boolean }).isStart).toBe(true);
  });

  it("lays nodes out in layers by distance from start", () => {
    const { nodes } = workflowToFlow(DEF);
    const draft = nodes.find((n) => n.id === "draft")!;
    const gate = nodes.find((n) => n.id === "gate")!;
    expect(gate.position.x).toBeGreaterThan(draft.position.x);
  });

  it("builds edges, including the labeled fail loop-back", () => {
    const { edges } = workflowToFlow(DEF);
    expect(edges.some((e) => e.source === "draft" && e.target === "gate")).toBe(true);
    const fail = edges.find((e) => e.source === "gate" && e.target === "draft");
    expect(fail?.label).toBe("fail");
  });

  it("includes parallel branch edges and the join edge", () => {
    const { edges } = workflowToFlow({
      name: "p",
      start: "fan",
      nodes: [
        { id: "fan", kind: "parallel", branches: ["a", "b"], next: "join" },
        { id: "a", kind: "work" },
        { id: "b", kind: "work" },
        { id: "join", kind: "work" },
      ],
    });
    expect(edges.filter((e) => e.source === "fan" && e.label === "branch")).toHaveLength(2);
    expect(edges.some((e) => e.source === "fan" && e.target === "join")).toBe(true);
  });

  it("ignores edges that point at unknown nodes", () => {
    const { edges } = workflowToFlow({
      name: "x",
      start: "a",
      nodes: [{ id: "a", kind: "work", next: "ghost" }],
    });
    expect(edges).toHaveLength(0);
  });
});
