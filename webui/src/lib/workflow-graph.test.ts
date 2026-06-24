import { describe, expect, it } from "vitest";

import { safeSubflowTargets, workflowToFlow, type WorkflowDef } from "./workflow-graph";

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

  it("a routing kind:work node resolves to the internal 'decision' flow-type (component selection)", () => {
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

  it("a def with input descriptor yields an input_obj node and an edge from it to start", () => {
    const { nodes, edges } = workflowToFlow({
      name: "io",
      start: "a",
      input: { text: true, file: true },
      nodes: [{ id: "a", kind: "work", next: null }],
    });
    const inputNode = nodes.find((n) => n.type === "input_obj");
    expect(inputNode).toBeDefined();
    expect(inputNode!.data).toMatchObject({ input: { text: true, file: true } });
    const edge = edges.find((e) => e.source === inputNode!.id && e.target === "a");
    expect(edge).toBeDefined();
  });

  it("a def with output descriptor yields an output_obj node and an edge from the terminal node to it", () => {
    const { nodes, edges } = workflowToFlow({
      name: "io",
      start: "a",
      output: { file: true },
      nodes: [{ id: "a", kind: "work", next: null }],
    });
    const outputNode = nodes.find((n) => n.type === "output_obj");
    expect(outputNode).toBeDefined();
    expect(outputNode!.data).toMatchObject({ output: { file: true } });
    const edge = edges.find((e) => e.target === outputNode!.id && e.source === "a");
    expect(edge).toBeDefined();
  });

  it("a def with neither input nor output yields no I/O object nodes", () => {
    const { nodes } = workflowToFlow({
      name: "plain",
      start: "a",
      nodes: [{ id: "a", kind: "work", next: null }],
    });
    expect(nodes.every((n) => n.type !== "input_obj" && n.type !== "output_obj")).toBe(true);
  });

  it("a static parallel (branches=[a,b], next=c) emits branch edges and a merge edge", () => {
    const { edges } = workflowToFlow({
      name: "sfan",
      start: "fan",
      nodes: [
        { id: "fan", kind: "parallel", branches: ["a", "b"], next: "c" },
        { id: "a", kind: "work" },
        { id: "b", kind: "work" },
        { id: "c", kind: "work" },
      ],
    });
    const branchEdges = edges.filter((e) => e.source === "fan" && e.label === "branch");
    expect(branchEdges).toHaveLength(2);
    expect(branchEdges.map((e) => e.target).sort()).toEqual(["a", "b"]);
    const merge = edges.find((e) => e.source === "fan" && e.target === "c");
    expect(merge).toBeDefined();
  });

  it("a dynamic parallel (worker=w, list_from=orch, next=done) emits list/worker/merge edges and the worker node carries the dynamicWorker marker", () => {
    const { nodes, edges } = workflowToFlow({
      name: "dfan",
      start: "orch",
      nodes: [
        { id: "orch", kind: "work", next: "fan" },
        { id: "fan", kind: "parallel", worker: "w", list_from: "orch", next: "done" },
        { id: "w", kind: "work" },
        { id: "done", kind: "work" },
      ],
    });
    const listEdge = edges.find((e) => e.source === "fan" && e.target === "orch" && e.label === "list");
    expect(listEdge).toBeDefined();
    const workerEdge = edges.find((e) => e.source === "fan" && e.target === "w" && e.label === "worker");
    expect(workerEdge).toBeDefined();
    const mergeEdge = edges.find((e) => e.source === "fan" && e.target === "done");
    expect(mergeEdge).toBeDefined();
    const workerNode = nodes.find((n) => n.id === "w");
    expect((workerNode?.data as Record<string, unknown>).dynamicWorker).toBe(true);
  });

  it("connects OUTPUT from a routing node that ends on pass but loops on fail (evaluator-optimizer)", () => {
    const { edges } = workflowToFlow({
      name: "eo",
      start: "draft",
      output: { text: true },
      nodes: [
        { id: "draft", kind: "work", next: "critique" },
        { id: "critique", kind: "work", on_pass: null, on_fail: "draft" },
      ],
    });
    // critique ends on pass (on_pass null) → it is the terminal, OUTPUT connects from it.
    expect(edges.some((e) => e.source === "critique" && e.target === "__output__")).toBe(true);
    // draft loops the flow, it is not a terminal.
    expect(edges.some((e) => e.source === "draft" && e.target === "__output__")).toBe(false);
  });

  it("connects OUTPUT only from the merge node of a static parallel, not its branches (concurrent-review)", () => {
    const { edges } = workflowToFlow({
      name: "cr",
      start: "produce",
      output: { text: true },
      nodes: [
        { id: "produce", kind: "work", next: "fan" },
        { id: "fan", kind: "parallel", branches: ["review_bugs", "review_security"], next: "synthesize" },
        { id: "review_bugs", kind: "work" },
        { id: "review_security", kind: "work" },
        { id: "synthesize", kind: "work", next: null },
      ],
    });
    const toOutput = edges.filter((e) => e.target === "__output__").map((e) => e.source).sort();
    expect(toOutput).toEqual(["synthesize"]);
  });

  it("uses def.ui.positions for a node when present", () => {
    const { nodes } = workflowToFlow({ name: "p", start: "a", ui: { positions: { a: { x: 500, y: 40 } } }, nodes: [{ id: "a", kind: "work", next: null }] });
    const a = nodes.find((n) => n.id === "a")!;
    expect(a.position).toEqual({ x: 500, y: 40 });
  });

  it("connects OUTPUT from both branches of a routing split (routing-triage)", () => {
    const { edges } = workflowToFlow({
      name: "rt",
      start: "classify",
      output: { text: true },
      nodes: [
        { id: "classify", kind: "work", on_pass: "code", on_fail: "analysis" },
        { id: "code", kind: "work", next: null },
        { id: "analysis", kind: "work", next: null },
      ],
    });
    const toOutput = edges.filter((e) => e.target === "__output__").map((e) => e.source).sort();
    expect(toOutput).toEqual(["analysis", "code"]);
    // classify routes to both, it is not itself a terminal.
    expect(edges.some((e) => e.source === "classify" && e.target === "__output__")).toBe(false);
  });
});

describe("safeSubflowTargets", () => {
  it("excludes self and any workflow that can reach the current one", () => {
    const refs = { A: ["B"], B: ["C"], C: [], D: [] };
    // current = C: B->C and A->B->C reach C, so calling them from C would loop; D is safe.
    expect(safeSubflowTargets("C", refs).sort()).toEqual(["D"]);
    expect(safeSubflowTargets("A", refs).sort()).toEqual(["B", "C", "D"]); // none reach A
  });
});
