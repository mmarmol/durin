import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ThreadComposer } from "@/components/thread/ThreadComposer";
import type { SlashCommand } from "@/lib/types";

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ token: "t" }),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, fetchModelPicker: vi.fn().mockResolvedValue([]) };
});

const COMMANDS: SlashCommand[] = [
  { command: "/new", title: "New chat", description: "Start fresh", icon: "square-pen" },
];

describe("ThreadComposer — slash seeded from the + menu", () => {
  function openSlashFromMenu() {
    render(
      <ThreadComposer onSend={vi.fn()} slashCommands={COMMANDS} placeholder="Type…" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /slash commands/i }));
    return screen.getByPlaceholderText("Type…") as HTMLTextAreaElement;
  }

  it("seeds a lone '/' and opens the palette", () => {
    const input = openSlashFromMenu();
    expect(input.value).toBe("/");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("removes the seeded '/' when cancelled with Escape", () => {
    const input = openSlashFromMenu();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input.value).toBe("");
  });

  it("keeps a '/' the user typed themselves when cancelled", () => {
    render(
      <ThreadComposer onSend={vi.fn()} slashCommands={COMMANDS} placeholder="Type…" />,
    );
    const input = screen.getByPlaceholderText("Type…") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "/" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input.value).toBe("/");
  });
});
