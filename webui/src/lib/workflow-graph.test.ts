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

  it("a kind:work node with on_pass/on_fail produces two labelled pass/fail edges", () => {
    const { edges } = workflowToFlow({
      name: "r",
      start: "prod",
      nodes: [
        { id: "prod", kind: "work", next: "gate" },
        { id: "gate", kind: "work", on_pass: "done", on_fail: "prod" },
        { id: "done", kind: "work" },
      ],
    });
    const pass = edges.find((e) => e.source === "gate" && e.target === "done");
    const fail = edges.find((e) => e.source === "gate" && e.target === "prod");
    expect(pass?.label).toBe("pass");
    expect(fail?.label).toBe("fail");
    // must NOT also emit a next edge from gate
    expect(edges.filter((e) => e.source === "gate")).toHaveLength(2);
  });

  it("a kind:work node with only next produces one edge (no pass/fail)", () => {
    const { edges } = workflowToFlow({
      name: "s",
      start: "a",
      nodes: [
        { id: "a", kind: "work", next: "b" },
        { id: "b", kind: "work" },
      ],
    });
    const fromA = edges.filter((e) => e.source === "a");
    expect(fromA).toHaveLength(1);
    expect(fromA[0].label).toBeUndefined();
  });

  it("a legacy kind:decision node still renders with pass/fail edges", () => {
    const { edges } = workflowToFlow({
      name: "legacy",
      start: "prod",
      nodes: [
        { id: "prod", kind: "work", next: "gate" },
        { id: "gate", kind: "decision", on_pass: "done", on_fail: "prod" },
        { id: "done", kind: "work" },
      ],
    });
    expect(edges.find((e) => e.source === "gate" && e.label === "pass")?.target).toBe("done");
    expect(edges.find((e) => e.source === "gate" && e.label === "fail")?.target).toBe("prod");
  });

  it("a routing kind:work node type is 'decision' so NodeCard shows the decision ring", () => {
    const { nodes } = workflowToFlow({
      name: "ring",
      start: "gate",
      nodes: [
        { id: "gate", kind: "work", on_pass: "done", on_fail: "gate" },
        { id: "done", kind: "work" },
      ],
    });
    const gate = nodes.find((n) => n.id === "gate")!;
    expect(gate.type).toBe("decision");
  });

  it("a non-routing kind:work node type stays 'work'", () => {
    const { nodes } = workflowToFlow({
      name: "plain",
      start: "a",
      nodes: [{ id: "a", kind: "work", next: null }],
    });
    expect(nodes[0].type).toBe("work");
  });
});
