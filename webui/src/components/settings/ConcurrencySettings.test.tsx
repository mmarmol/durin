// webui/src/components/settings/ConcurrencySettings.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { setConfigValue } from "@/lib/api";
import { ConcurrencySettings } from "./ConcurrencySettings";

vi.mock("@/lib/api", () => ({
  getConfig: vi.fn(async () => ({
    config: {
      agents: {
        defaults: {
          max_concurrent_interactive: 4,
          concurrency_ceiling: 12,
          max_concurrent_subagents: 3,
        },
      },
    },
    schema: {},
  })),
  setConfigValue: vi.fn(async () => ({
    agents: {
      defaults: {
        max_concurrent_interactive: 6,
        concurrency_ceiling: 12,
        max_concurrent_subagents: 3,
      },
    },
  })),
}));

vi.mock("@/hooks/useConcurrencySnapshot", () => ({
  useConcurrencySnapshot: () => ({
    lanes: {
      interactive: { active: 1, limit: 4, waiting: 0 },
      ceiling: { active: 5, limit: 12, waiting: 0 },
      subagents: { active: 2, limit: 3 },
    },
    queued: 0,
    work: [],
  }),
}));

function renderCard() {
  return render(<ConcurrencySettings token="tok" />);
}

describe("ConcurrencySettings", () => {
  it("shows the live ceiling readout from the snapshot", async () => {
    renderCard();
    await waitFor(() => expect(screen.getByText("5 / 12")).toBeInTheDocument());
  });

  it("saves an edited interactive cap", async () => {
    renderCard();
    const input = await screen.findByDisplayValue("4");
    fireEvent.change(input, { target: { value: "6" } });
    const saveButtons = screen.getAllByRole("button", { name: /save/i });
    fireEvent.click(saveButtons[0]);
    await waitFor(() =>
      expect(vi.mocked(setConfigValue)).toHaveBeenCalledWith(
        "tok",
        "agents.defaults.max_concurrent_interactive",
        6,
      ),
    );
  });
});
