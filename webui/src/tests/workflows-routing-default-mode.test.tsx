// Switching a work node to routing pre-fills its mode with "read" (the default the
// backend gives a routing node), so the editor and a node authored without a mode agree.
import { fireEvent, render, screen } from "@testing-library/react";
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
    listSeedSuggestions: vi.fn(),
    listModes: vi.fn(),
    listWorkflowScripts: vi.fn(),
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
  vi.mocked(api.listSeedSuggestions).mockResolvedValue([]);
  vi.mocked(api.listModes).mockResolvedValue([]);
  vi.mocked(api.listWorkflowScripts).mockResolvedValue([]);
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

it("pre-fills mode read when a work node is switched to routing", async () => {
  vi.mocked(api.getWorkflow).mockResolvedValue({
    name: "demo",
    start: "check",
    nodes: [{ id: "check", kind: "work", mode: "build", prompt: "", next: null }],
  } as never);
  render(wrap(<WorkflowsView />));
  fireEvent.click(await screen.findByText(/check/));

  const modeSelect = (await screen.findByText("mode")).parentElement!.querySelector("select")!;
  expect(modeSelect).toHaveValue("build");

  fireEvent.click(screen.getByLabelText("routes"));
  expect(modeSelect).toHaveValue("read");
});
