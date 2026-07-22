// webui/src/components/WorkflowsView.seedbanner.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  applySeedSuggestion,
  dismissSeedSuggestion,
  listSeedSuggestions,
} from "@/lib/api";
import { SeedUpdatesBanner } from "./WorkflowsView";

vi.mock("@/lib/api", () => ({
  applySeedSuggestion: vi.fn(async () => ({ applied: true, error: "" })),
  dismissSeedSuggestion: vi.fn(async () => ({ dismissed: true, error: "" })),
  listSeedSuggestions: vi.fn(async () => [
    {
      name: "debug",
      reason: "edited",
      created_at: "2026-07-23T00:00:00+00:00",
      diff: "--- debug.json (yours)\n+++ debug.json (new builtin)\n+new line",
    },
  ]),
  // The banner lives in WorkflowsView's module; its siblings import these too.
  listWorkflows: vi.fn(async () => []),
  listWorkflowScripts: vi.fn(async () => []),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock("@/lib/client", () => ({
  useClient: () => ({ token: "tok" }),
}));

describe("SeedUpdatesBanner", () => {
  beforeEach(() => {
    vi.mocked(listSeedSuggestions).mockClear();
    vi.mocked(applySeedSuggestion).mockClear();
    vi.mocked(dismissSeedSuggestion).mockClear();
  });

  it("renders nothing when there are no suggestions", async () => {
    vi.mocked(listSeedSuggestions).mockResolvedValueOnce([]);
    render(<SeedUpdatesBanner token="tok" onApplied={() => undefined} />);
    await waitFor(() => expect(listSeedSuggestions).toHaveBeenCalled());
    expect(screen.queryByTestId("seed-updates-banner")).toBeNull();
  });

  it("lists a pending suggestion and toggles its diff", async () => {
    render(<SeedUpdatesBanner token="tok" onApplied={() => undefined} />);
    await screen.findByTestId("seed-updates-banner");
    expect(screen.getByText("debug")).toBeTruthy();
    expect(screen.getByText("workflows.seedBanner.reasonEdited")).toBeTruthy();

    fireEvent.click(screen.getByText("workflows.seedBanner.viewDiff"));
    expect(screen.getByText(/new builtin/)).toBeTruthy();
    fireEvent.click(screen.getByText("workflows.seedBanner.hideDiff"));
    expect(screen.queryByText(/new builtin/)).toBeNull();
  });

  it("apply calls the API, notifies the parent, and reloads", async () => {
    const onApplied = vi.fn();
    vi.mocked(listSeedSuggestions)
      .mockResolvedValueOnce([
        { name: "debug", reason: "edited", created_at: "", diff: "" },
      ])
      .mockResolvedValueOnce([]);
    render(<SeedUpdatesBanner token="tok" onApplied={onApplied} />);
    await screen.findByTestId("seed-updates-banner");

    fireEvent.click(screen.getByText("workflows.seedBanner.apply"));

    await waitFor(() => expect(applySeedSuggestion).toHaveBeenCalledWith("tok", "debug"));
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith("debug"));
    await waitFor(() =>
      expect(screen.queryByTestId("seed-updates-banner")).toBeNull());
  });

  it("dismiss calls the API and reloads without notifying", async () => {
    const onApplied = vi.fn();
    vi.mocked(listSeedSuggestions)
      .mockResolvedValueOnce([
        { name: "debug", reason: "unknown-provenance", created_at: "", diff: "" },
      ])
      .mockResolvedValueOnce([]);
    render(<SeedUpdatesBanner token="tok" onApplied={onApplied} />);
    await screen.findByTestId("seed-updates-banner");
    expect(screen.getByText("workflows.seedBanner.reasonUnknown")).toBeTruthy();

    fireEvent.click(screen.getByText("workflows.seedBanner.dismiss"));

    await waitFor(() => expect(dismissSeedSuggestion).toHaveBeenCalledWith("tok", "debug"));
    expect(onApplied).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByTestId("seed-updates-banner")).toBeNull());
  });
});
