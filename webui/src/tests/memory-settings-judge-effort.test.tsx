import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MemorySettings } from "@/components/settings/MemorySettings";
import { getConfig, setConfigValue } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getConfig: vi.fn(),
  setConfigValue: vi.fn(),
  getExtraStatus: vi.fn(() => Promise.resolve({ present: true })),
}));

const PATH = "memory.dream.auto_absorb.judge_reasoning_effort";

function serve(autoAbsorb: Record<string, unknown>) {
  vi.mocked(getConfig).mockResolvedValue({
    config: { memory: { dream: { enabled: true, auto_absorb: { enabled: true, ...autoAbsorb } } } },
  } as never);
  vi.mocked(setConfigValue).mockResolvedValue({} as never);
}

describe("MemorySettings first-judge reasoning effort", () => {
  beforeEach(() => vi.clearAllMocks());

  it("saves a chosen effort under the config key", async () => {
    serve({});
    render(<MemorySettings token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: "Low" }));
    await waitFor(() => expect(setConfigValue).toHaveBeenCalledWith("t", PATH, "low"));
  });

  it("clears the override with null when the memory model's setting is chosen", async () => {
    serve({ judge_reasoning_effort: "high" });
    render(<MemorySettings token="t" />);
    const high = await screen.findByRole("button", { name: "High" });
    expect(high).toBeDisabled(); // the stored value is the selected one
    fireEvent.click(screen.getByRole("button", { name: "Memory model's setting" }));
    await waitFor(() => expect(setConfigValue).toHaveBeenCalledWith("t", PATH, null));
  });

  it("shows the memory model's setting as selected when the key is unset", async () => {
    serve({});
    render(<MemorySettings token="t" />);
    expect(await screen.findByRole("button", { name: "Memory model's setting" })).toBeDisabled();
  });
});
