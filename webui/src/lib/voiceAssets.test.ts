import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Fresh module per test so the once-guard resets without test-only production code.
beforeEach(() => vi.resetModules());
afterEach(() => vi.unstubAllGlobals());

describe("prefetchVoiceAssets", () => {
  it("warms the cache for the VAD model + worklet + WASM on first call", async () => {
    const fetchMock = vi.fn(async (_url: string) => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const { prefetchVoiceAssets, VAD_BASE_ASSET_PATH } = await import("./voiceAssets");

    prefetchVoiceAssets();

    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls).toContain(`${VAD_BASE_ASSET_PATH}silero_vad_v5.onnx`);
    expect(urls).toContain(`${VAD_BASE_ASSET_PATH}vad.worklet.bundle.min.js`);
    expect(urls.some((u) => u.endsWith(".wasm"))).toBe(true);
  });

  it("only prefetches once even if called repeatedly", async () => {
    const fetchMock = vi.fn(async (_url: string) => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const { prefetchVoiceAssets } = await import("./voiceAssets");

    prefetchVoiceAssets();
    const afterFirst = fetchMock.mock.calls.length;
    prefetchVoiceAssets();

    expect(afterFirst).toBeGreaterThan(0);
    expect(fetchMock.mock.calls.length).toBe(afterFirst);
  });

  it("swallows fetch errors (best-effort warming, never throws)", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
    const { prefetchVoiceAssets } = await import("./voiceAssets");
    expect(() => prefetchVoiceAssets()).not.toThrow();
  });
});
