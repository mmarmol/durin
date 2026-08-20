// The node config panel's reuse toggle: `reuse` is only meaningful alongside
// `output_file` (mirroring the backend parser's requirement), so clearing
// output_file — directly, or indirectly by clearing output_schema, which
// already cascades to clear output_file — must also clear reuse. Otherwise
// the row hides but the node keeps `reuse: "if-unchanged"` with no
// output_file: the badge on the canvas card persists and the server rejects
// the save.
import { render, screen, waitFor } from "@testing-library/react";
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

function reuseNode(extra: Record<string, unknown> = {}) {
  return {
    id: "plan", kind: "work", mode: "build", prompt: "", next: null,
    output_schema: { type: "object" }, output_file: "out.json", reuse: "if-unchanged",
    ...extra,
  };
}

async function openNode(def: Record<string, unknown>, nodeText: RegExp) {
  vi.mocked(api.getWorkflow).mockResolvedValue(def as never);
  const user = userEvent.setup();
  render(wrap(<WorkflowsView />));
  const nodeEl = await screen.findByText(nodeText);
  const { fireEvent } = await import("@testing-library/react");
  fireEvent.click(nodeEl);
  return user;
}

it("clearing output_file also clears reuse", async () => {
  const user = await openNode(
    { name: "demo", start: "plan", nodes: [reuseNode()] },
    /plan/,
  );

  expect(await screen.findByText("reuse if unchanged")).toBeInTheDocument();

  const outputFileLabel = await screen.findByText("output file");
  const outputFileInput = outputFileLabel.parentElement!.querySelector("input")!;
  await user.clear(outputFileInput);

  await waitFor(() => expect(screen.queryByText("reuse if unchanged")).not.toBeInTheDocument());
  // The reuse row itself is gated on output_file, so it disappears too — not
  // just hidden while the underlying value lingers.
  expect(screen.queryByText("reuse when unchanged (skip if the producer matches)")).toBeNull();
});

it("clearing output_schema cascades through output_file to clear reuse", async () => {
  await openNode(
    { name: "demo", start: "plan", nodes: [reuseNode()] },
    /plan/,
  );

  expect(await screen.findByText("reuse if unchanged")).toBeInTheDocument();

  // SchemaField's own textarea holds the raw JSON and only commits (via onChange
  // -> patch) on blur; clearing it to empty and blurring commits
  // `output_schema: undefined`, which already cascades to output_file.
  const schemaLabel = await screen.findByText("output schema (JSON)");
  const schemaInput = schemaLabel.parentElement!.querySelector("textarea")!;
  const user = userEvent.setup();
  await user.clear(schemaInput);
  await user.tab();

  await waitFor(() => expect(screen.queryByText("reuse if unchanged")).not.toBeInTheDocument());
});
