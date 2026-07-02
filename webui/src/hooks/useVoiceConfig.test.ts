import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getConfig, getExtraStatus } from "@/lib/api";
import { useVoiceConfig } from "./useVoiceConfig";

vi.mock("@/lib/api", () => ({ getConfig: vi.fn(), getExtraStatus: vi.fn() }));
const mockGetConfig = vi.mocked(getConfig);
const mockGetExtraStatus = vi.mocked(getExtraStatus);

beforeEach(() => {
  mockGetConfig.mockReset();
  mockGetExtraStatus.mockReset();
});

describe("useVoiceConfig", () => {
  it("reads the voice config branch", async () => {
    mockGetConfig.mockResolvedValue({
      config: {
        voice: { enabled: true, vad_threshold: 0.6, end_of_turn_silence_ms: 800 },
        tts: { provider: "openai" },
      },
      schema: {},
    });
    const { result } = renderHook(() => useVoiceConfig("tok"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.enabled).toBe(true);
    expect(result.current.vadThreshold).toBe(0.6);
    expect(result.current.endOfTurnSilenceMs).toBe(800);
  });

  it("available=true when local TTS extra is installed", async () => {
    mockGetConfig.mockResolvedValue({
      config: { voice: { enabled: true }, tts: { provider: "local" } },
      schema: {},
    });
    mockGetExtraStatus.mockResolvedValue({
      present: true, extra: "tts", approx_size: "~1 GB", needs_restart: false, label: "Local TTS",
    });
    const { result } = renderHook(() => useVoiceConfig("tok"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.available).toBe(true);
  });

  it("available=false when local TTS extra is missing (the orb stays hidden)", async () => {
    mockGetConfig.mockResolvedValue({
      config: { voice: { enabled: true }, tts: { provider: "local" } },
      schema: {},
    });
    mockGetExtraStatus.mockResolvedValue({
      present: false, extra: "tts", approx_size: "~1 GB", needs_restart: false, label: "Local TTS",
    });
    const { result } = renderHook(() => useVoiceConfig("tok"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.available).toBe(false);
  });

  it("available=true for a cloud TTS provider without checking the extra", async () => {
    mockGetConfig.mockResolvedValue({
      config: { voice: { enabled: true }, tts: { provider: "openai" } },
      schema: {},
    });
    const { result } = renderHook(() => useVoiceConfig("tok"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.available).toBe(true);
    expect(mockGetExtraStatus).not.toHaveBeenCalled();
  });

  it("available=false when voice is disabled even if TTS is usable", async () => {
    mockGetConfig.mockResolvedValue({
      config: { voice: { enabled: false }, tts: { provider: "openai" } },
      schema: {},
    });
    const { result } = renderHook(() => useVoiceConfig("tok"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.available).toBe(false);
  });
});
