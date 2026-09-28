import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useTranscriptionStatus } from "@/hooks/useTranscriptionStatus";

const getConfig = vi.fn();
const getExtraStatus = vi.fn();

vi.mock("@/lib/api", () => ({
  getConfig: (...args: unknown[]) => getConfig(...args),
  getExtraStatus: (...args: unknown[]) => getExtraStatus(...args),
}));
vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ token: "t" }),
}));

function withTranscription(transcription: Record<string, unknown>) {
  getConfig.mockResolvedValue({ config: { transcription } });
}

beforeEach(() => {
  getConfig.mockReset();
  getExtraStatus.mockReset();
  getExtraStatus.mockResolvedValue({ present: false });
});

describe("useTranscriptionStatus", () => {
  it("reports the configured mode", async () => {
    withTranscription({ provider: "groq", mode: "preview" });
    const { result } = renderHook(() => useTranscriptionStatus());
    await waitFor(() => expect(result.current.mode).toBe("preview"));
    expect(result.current.available).toBe(true);
  });

  it("offers audio with transcription off even without a local engine", async () => {
    // Off sends the recording as it is; nothing needs transcribing.
    withTranscription({ provider: "local", mode: "off" });
    const { result } = renderHook(() => useTranscriptionStatus());
    await waitFor(() => expect(result.current.mode).toBe("off"));
    expect(result.current.available).toBe(true);
  });

  it("still hides audio when a local engine is needed and missing", async () => {
    withTranscription({ provider: "local", mode: "auto" });
    const { result } = renderHook(() => useTranscriptionStatus());
    await waitFor(() => expect(result.current.available).toBe(false));
  });
});
