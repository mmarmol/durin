import { describe, expect, it } from "vitest";
import { ONNX_WASM_BASE_PATH, VAD_BASE_ASSET_PATH } from "@/lib/voiceAssets";

describe("voice assets", () => {
  it("are served from a same-origin /vad path (offline-safe, not a CDN)", () => {
    expect(VAD_BASE_ASSET_PATH).toBe("/vad/");
    expect(ONNX_WASM_BASE_PATH).toBe("/vad/");
    expect(VAD_BASE_ASSET_PATH.startsWith("http")).toBe(false);
  });
});
