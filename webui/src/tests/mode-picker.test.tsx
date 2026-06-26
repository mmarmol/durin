import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ModePicker } from "@/components/thread/ModePicker";
import type { ModeInfo } from "@/lib/api";

const MODES: ModeInfo[] = [
  { name: "build", description: "Full access to all tools.", icon: null, builtin: true },
  { name: "plan", description: "Read-only planning mode.", icon: null, builtin: true },
  { name: "explore", description: "Read-only exploration.", icon: null, builtin: true },
];

describe("ModePicker", () => {
  it("shows the active mode and lists every registered mode on open", () => {
    render(<ModePicker activeMode="plan" modes={MODES} onSelect={vi.fn()} />);
    const pill = screen.getByRole("button", { name: /mode/i });
    expect(pill).toHaveTextContent("plan");

    fireEvent.click(pill);
    // Mode-agnostic: one option per registered mode, by name.
    expect(screen.getAllByRole("option")).toHaveLength(3);
    expect(screen.getByRole("option", { name: /build/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /explore/ })).toBeInTheDocument();
  });

  it("calls onSelect with the chosen mode name", () => {
    const onSelect = vi.fn();
    render(<ModePicker activeMode="build" modes={MODES} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: /mode/i }));
    fireEvent.click(screen.getByRole("option", { name: /explore/ }));
    expect(onSelect).toHaveBeenCalledWith("explore");
  });

  it("renders nothing when no modes are registered", () => {
    const { container } = render(
      <ModePicker activeMode="build" modes={[]} onSelect={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
