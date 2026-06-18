import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ModelPickerPopover } from "@/components/thread/ModelPickerPopover";

vi.mock("@/lib/api", () => ({
  fetchModelPicker: vi.fn().mockResolvedValue([
    { name: "base-model", provider: "openai_codex", group: "Easy pick", role: "default", ref: "default" },
    { name: "gemini-2.5-pro", provider: "gemini", group: "gemini", role: "catalog", ref: "gemini gemini-2.5-pro", max_input_tokens: 1_000_000, supports_vision: true, supports_reasoning: true },
  ]),
}));

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ token: "t" }),
}));

describe("ModelPickerPopover", () => {
  beforeEach(() => localStorage.clear());

  it("shows picker entries on open and commits by ref", async () => {
    const onSelect = vi.fn();
    render(
      <ModelPickerPopover open onClose={() => {}} onSelect={onSelect} activeModel={null} />,
    );
    await waitFor(() => screen.getByText("base-model"));
    expect(screen.getByText("gemini-2.5-pro")).toBeInTheDocument();

    fireEvent.click(screen.getByText("base-model"));
    expect(onSelect).toHaveBeenCalledWith("default");
  });

  it("records the picked model name in localStorage recents", async () => {
    render(
      <ModelPickerPopover open onClose={() => {}} onSelect={() => {}} activeModel={null} />,
    );
    await waitFor(() => screen.getByText("gemini-2.5-pro"));
    fireEvent.click(screen.getByText("gemini-2.5-pro"));
    expect(JSON.parse(localStorage.getItem("durin.recentModels") || "[]")).toContain(
      "gemini-2.5-pro",
    );
  });

  it("renders capability info from the entry caps fields", async () => {
    render(
      <ModelPickerPopover open onClose={() => {}} onSelect={() => {}} activeModel={null} />,
    );
    await waitFor(() => screen.getByText("gemini-2.5-pro"));
    // gemini-2.5-pro carries max_input_tokens=1_000_000 → caps line shows "1M".
    expect(screen.getByText(/1M/)).toBeInTheDocument();
  });
});
