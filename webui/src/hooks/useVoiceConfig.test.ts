import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useVoiceConfig } from "./useVoiceConfig";

vi.mock("@/lib/api", () => ({
  getConfig: vi.fn(async () => ({
    config: { voice: { enabled: true, vad_threshold: 0.6, end_of_turn_silence_ms: 800 } },
    schema: {},
  })),
}));

describe("useVoiceConfig", () => {
  it("reads the voice config branch", async () => {
    const { result } = renderHook(() => useVoiceConfig("tok"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.enabled).toBe(true);
    expect(result.current.vadThreshold).toBe(0.6);
    expect(result.current.endOfTurnSilenceMs).toBe(800);
  });
});
