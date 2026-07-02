import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AgentPickerPopover } from "@/components/thread/AgentPickerPopover";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  listPersonas: vi.fn().mockResolvedValue({
    personas: [{ name: "durin", description: "default" }],
    default: "durin",
  }),
}));
vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ token: "t" }),
}));

const MODES = [
  { name: "build" },
  { name: "plan" },
  { name: "explore" },
] as never;

describe("AgentPickerPopover", () => {
  it("shows mode and persona on one pill", () => {
    render(
      <AgentPickerPopover
        activeMode="build"
        modes={MODES}
        onModeSelect={() => {}}
        activePersona="durin"
        onPersonaSelect={() => {}}
      />,
    );
    const pill = screen.getByRole("button", { name: /agent/i });
    expect(pill.textContent).toContain("build");
    expect(pill.textContent).toContain("durin");
  });

  it("selects a mode and a persona from the popover sections", async () => {
    const onModeSelect = vi.fn();
    const onPersonaSelect = vi.fn();
    render(
      <AgentPickerPopover
        activeMode="build"
        modes={MODES}
        onModeSelect={onModeSelect}
        activePersona={null}
        onPersonaSelect={onPersonaSelect}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /agent/i }));
    fireEvent.click(screen.getByRole("option", { name: /plan/ }));
    expect(onModeSelect).toHaveBeenCalledWith("plan");

    fireEvent.click(screen.getByRole("button", { name: /agent/i }));
    await waitFor(() => screen.getByRole("option", { name: /durin/ }));
    fireEvent.click(screen.getByRole("option", { name: /durin/ }));
    expect(onPersonaSelect).toHaveBeenCalledWith("durin");
  });

  it("falls back to the default persona in the pill label when none is active", async () => {
    render(
      <AgentPickerPopover
        activeMode="build"
        modes={MODES}
        onModeSelect={() => {}}
        activePersona={null}
        onPersonaSelect={() => {}}
      />,
    );
    const pill = screen.getByRole("button", { name: /agent/i });
    await waitFor(() => expect(pill.textContent).toContain("durin"));
    expect(pill.textContent).toContain("build");
  });

  it("marks the default persona selected in the popover when none is active", async () => {
    render(
      <AgentPickerPopover
        activeMode="build"
        modes={MODES}
        onModeSelect={() => {}}
        activePersona={null}
        onPersonaSelect={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /agent/i }));
    const option = await screen.findByRole("option", { name: /durin/ });
    await waitFor(() => expect(option).toHaveAttribute("aria-selected", "true"));
  });

  it("keeps an explicit activePersona over the default", async () => {
    render(
      <AgentPickerPopover
        activeMode="build"
        modes={MODES}
        onModeSelect={() => {}}
        activePersona="other"
        onPersonaSelect={() => {}}
      />,
    );
    const pill = screen.getByRole("button", { name: /agent/i });
    expect(pill.textContent).toContain("other");
    expect(pill.textContent).not.toContain("durin");
  });
});
