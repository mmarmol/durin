// The node config panel's turn-budget group: max_turns is editable on work (LLM)
// nodes, max_reentries appears only once a turn budget exists, reentry_prompt only
// once re-entries exist — mirroring the backend parser's field chain — and none of
// the three render on a script node (agent-only fields, rejected by the parser).
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { WorkflowsView } from "@/components/WorkflowsView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listWorkflows: vi.fn(),
    listPersonas: vi.fn(),
    getWorkflow: vi.fn(),
    getWorkflowRecommendations: vi.fn(),
    listWorkflowRuns: vi.fn(),
    listAllWorkflowRuns: vi.fn(),
  };
});

beforeEach(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  vi.mocked(api.listWorkflows).mockResolvedValue(["demo"]);
  vi.mocked(api.listPersonas).mockResolvedValue({ personas: [], default: null });
  vi.mocked(api.getWorkflowRecommendations).mockResolvedValue([]);
  vi.mocked(api.listWorkflowRuns).mockResolvedValue([]);
  vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

function wrap(children: ReactNode) {
  return (
    <ClientProvider
      client={{} as unknown as import("@/lib/durin-client").DurinClient}
      token="tok"
    >
      {children}
    </ClientProvider>
  );
}

async function openNode(def: Record<string, unknown>, nodeText: RegExp) {
  vi.mocked(api.getWorkflow).mockResolvedValue(def as never);
  const user = userEvent.setup();
  render(wrap(<WorkflowsView />));
  const nodeEl = await screen.findByText(nodeText);
  // Plain click, not userEvent: the latter's mousedown hits d3-drag's handler,
  // which happy-dom cannot satisfy (no view on the event); xyflow's node
  // selection fires on the click event either way.
  fireEvent.click(nodeEl);
  return user;
}

it("shows the turn budget chain on a work node and gates each dependent field", async () => {
  const user = await openNode(
    {
      name: "demo",
      start: "analyze",
      nodes: [{ id: "analyze", kind: "work", mode: "build", prompt: "", next: null, max_turns: 24 }],
    },
    /analyze/,
  );

  const turnBudget = await screen.findByText("turn budget");
  expect(turnBudget).toBeInTheDocument();
  const turnInput = turnBudget.parentElement!.querySelector("input")!;
  expect(turnInput).toHaveValue(24);

  // max_turns present -> re-entries visible; steering hidden until re-entries >= 1.
  const reentries = screen.getByText("re-entries");
  expect(screen.queryByText("re-entry steering")).toBeNull();
  const reentriesInput = reentries.parentElement!.querySelector("input")!;
  await user.type(reentriesInput, "1");
  expect(await screen.findByText("re-entry steering")).toBeInTheDocument();
});

it("hides re-entries entirely while the node has no turn budget", async () => {
  await openNode(
    {
      name: "demo",
      start: "analyze",
      nodes: [{ id: "analyze", kind: "work", mode: "build", prompt: "", next: null }],
    },
    /analyze/,
  );
  await screen.findByText("turn budget");
  expect(screen.queryByText("re-entries")).toBeNull();
});

it("renders none of the budget fields on a script node", async () => {
  await openNode(
    {
      name: "demo",
      start: "step",
      nodes: [{ id: "step", kind: "script", command: "echo hi", next: null }],
    },
    /step/,
  );
  await screen.findByText("max visits");   // the script panel is open
  expect(screen.queryByText("turn budget")).toBeNull();
  expect(screen.queryByText("re-entries")).toBeNull();
  expect(screen.queryByText("re-entry steering")).toBeNull();
});
