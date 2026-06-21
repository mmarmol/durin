import { describe, expect, it, afterEach } from "vitest";
import { pickAudioMime } from "./audioMime";

describe("pickAudioMime", () => {
  const realMR = (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder;

  afterEach(() => {
    // Restore between tests.
    if (realMR) (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder = realMR;
  });

  it("prefers audio/webm;codecs=opus when supported", () => {
    class FakeMR {
      static isTypeSupported(m: string) {
        return m === "audio/webm;codecs=opus";
      }
    }
    (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder = FakeMR as unknown as typeof MediaRecorder;
    expect(pickAudioMime()).toBe("audio/webm;codecs=opus");
  });

  it("falls back to audio/mp4 for Safari-like support", () => {
    class FakeMR {
      static isTypeSupported(m: string) {
        return m === "audio/mp4";
      }
    }
    (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder = FakeMR as unknown as typeof MediaRecorder;
    expect(pickAudioMime()).toBe("audio/mp4");
  });

  it("returns empty string when MediaRecorder is absent", () => {
    delete (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder;
    expect(pickAudioMime()).toBe("");
  });
});
