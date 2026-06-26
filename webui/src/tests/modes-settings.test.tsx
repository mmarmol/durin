import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModesSettings } from "@/components/settings/ModesSettings";

const listModes = vi.fn();
const upsertMode = vi.fn();
const deleteMode = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listModes: (...a: unknown[]) => listModes(...a),
    upsertMode: (...a: unknown[]) => upsertMode(...a),
    deleteMode: (...a: unknown[]) => deleteMode(...a),
  };
});

const MODES = [
  { name: "build", description: "full access", icon: null, builtin: true, allowed: null, denied: [], prompt_suffix: "" },
  { name: "reviewer", description: "reads", icon: null, builtin: false, allowed: ["read_file"], denied: [], prompt_suffix: "" },
];

describe("ModesSettings", () => {
  beforeEach(() => {
    listModes.mockResolvedValue(MODES);
    upsertMode.mockResolvedValue(MODES[1]);
    deleteMode.mockResolvedValue(true);
  });

  it("lists modes; built-ins are read-only (duplicate, no delete), customs are editable", async () => {
    render(<ModesSettings token="t" />);
    await screen.findByText("build");
    expect(screen.getByText("reviewer")).toBeInTheDocument();
    // The built-in offers Duplicate (fork), and exposes no edit/delete.
    expect(screen.getAllByTitle(/duplicate/i)).toHaveLength(1);
    // The custom mode is editable + deletable.
    expect(screen.getByTitle(/^edit$/i)).toBeInTheDocument();
    expect(screen.getByTitle(/^delete$/i)).toBeInTheDocument();
  });

  it("opens the editor and saves a new mode via upsert", async () => {
    render(<ModesSettings token="t" />);
    await screen.findByText("build");
    fireEvent.click(screen.getByText(/new mode/i));
    fireEvent.change(screen.getByPlaceholderText("reviewer"), { target: { value: "checker" } });
    fireEvent.click(screen.getByText(/save mode/i));
    await waitFor(() => expect(upsertMode).toHaveBeenCalled());
    expect(upsertMode.mock.calls[0][1].name).toBe("checker");
  });
});
