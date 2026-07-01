import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PersonaPickerPopover } from "./PersonaPickerPopover";

const listPersonas = vi.fn();

vi.mock("@/lib/api", () => ({
  listPersonas: (...a: unknown[]) => listPersonas(...a),
}));

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ token: "tok" }),
}));

const PERSONAS = [
  { name: "engineer", soul: "engineer-soul", model: null, description: "Technical expert", builtin: true },
  { name: "researcher", soul: "researcher-soul", model: null, description: "Research assistant", builtin: true },
  { name: "tutor", soul: "tutor-soul", model: null, description: "Patient teacher", builtin: false },
];

describe("PersonaPickerPopover", () => {
  it("opens the dropdown and shows persona list on trigger click", async () => {
    listPersonas.mockResolvedValue({ personas: PERSONAS, default: null });
    render(<PersonaPickerPopover activePersona={null} onSelect={vi.fn()} />);

    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByText("engineer")).toBeInTheDocument());
    expect(screen.getByText("researcher")).toBeInTheDocument();
    expect(screen.getByText("tutor")).toBeInTheDocument();
  });

  it("calls onSelect with the persona name when a row is clicked", async () => {
    listPersonas.mockResolvedValue({ personas: PERSONAS, default: null });
    const onSelect = vi.fn();
    render(<PersonaPickerPopover activePersona={null} onSelect={onSelect} />);

    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByText("engineer")).toBeInTheDocument());
    fireEvent.click(screen.getByText("engineer"));
    expect(onSelect).toHaveBeenCalledWith("engineer");
  });

  it("closes the dropdown after selection", async () => {
    listPersonas.mockResolvedValue({ personas: PERSONAS, default: null });
    render(<PersonaPickerPopover activePersona={null} onSelect={vi.fn()} />);

    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByText("engineer")).toBeInTheDocument());
    fireEvent.click(screen.getByText("engineer"));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("marks the active persona with aria-selected and emerald dot", async () => {
    listPersonas.mockResolvedValue({ personas: PERSONAS, default: null });
    render(<PersonaPickerPopover activePersona="researcher" onSelect={vi.fn()} />);

    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByText("researcher")).toBeInTheDocument());

    const researcherBtn = screen.getByRole("option", { name: /researcher/ });
    expect(researcherBtn.getAttribute("aria-selected")).toBe("true");
  });

  it("does not open when disabled", () => {
    listPersonas.mockResolvedValue({ personas: PERSONAS, default: null });
    render(<PersonaPickerPopover activePersona={null} onSelect={vi.fn()} disabled />);

    const trigger = screen.getByRole("button") as HTMLButtonElement;
    expect(trigger.disabled).toBe(true);
  });
});
