import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let speechEndCb: ((a: Float32Array) => void) | null = null;
const vadStart = vi.fn();
const vadDestroy = vi.fn();

vi.mock("@ricky0123/vad-web", () => ({
  MicVAD: { new: vi.fn(async (opts: { onSpeechEnd: (a: Float32Array) => void }) => { speechEndCb = opts.onSpeechEnd; return { start: vadStart, destroy: vadDestroy }; }) },
  utils: { encodeWAV: () => new ArrayBuffer(8), arrayBufferToBase64: () => "AAA" },
}));

import { useVoiceSession } from "./useVoiceSession";

function fakeClient() {
  const handlers: Array<(cid: string, s: string) => void> = [];
  return {
    onVoiceState: (h: (cid: string, s: string) => void) => { handlers.push(h); return () => {}; },
    onVoiceAudio: () => () => {},
    sendVoiceStart: vi.fn(), sendVoiceStop: vi.fn(), sendVoiceUtterance: vi.fn(),
    sendVoiceBargeIn: vi.fn(), sendVoiceReadAll: vi.fn(),
    _emitState: (cid: string, s: string) => handlers.forEach((h) => h(cid, s)),
  };
}

beforeEach(() => {
  speechEndCb = null;
  vi.stubGlobal("AudioContext", class { createMediaStreamSource() { return { connect() {} }; } createAnalyser() { return { fftSize: 0, frequencyBinCount: 8, getByteTimeDomainData() {}, connect() {} }; } createMediaElementSource() { return { connect() {} }; } close() {} get destination() { return {}; } });
  vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: vi.fn(async () => ({})) } });
  vi.stubGlobal("matchMedia", () => ({ matches: false }));
});
afterEach(() => vi.unstubAllGlobals());

describe("useVoiceSession", () => {
  it("starts the VAD and emits voice_start on toggle", async () => {
    const c = fakeClient();
    const { result } = renderHook(() => useVoiceSession(c as never, "c1", { vadThreshold: 0.5, endOfTurnSilenceMs: 700, idleTimeoutMs: 0 }));
    await act(async () => { result.current.toggle(); });
    await waitFor(() => expect(c.sendVoiceStart).toHaveBeenCalledWith("c1"));
    expect(vadStart).toHaveBeenCalled();
  });

  it("sends an utterance as a wav data-url on speech end", async () => {
    const c = fakeClient();
    const { result } = renderHook(() => useVoiceSession(c as never, "c1", { vadThreshold: 0.5, endOfTurnSilenceMs: 700, idleTimeoutMs: 0 }));
    await act(async () => { result.current.toggle(); });
    await waitFor(() => expect(speechEndCb).not.toBeNull());
    await act(async () => { speechEndCb!(new Float32Array([0, 0.1])); });
    expect(c.sendVoiceUtterance).toHaveBeenCalledWith("c1", "data:audio/wav;base64,AAA");
  });

  it("reflects server voice_state", async () => {
    const c = fakeClient();
    const { result } = renderHook(() => useVoiceSession(c as never, "c1", { vadThreshold: 0.5, endOfTurnSilenceMs: 700, idleTimeoutMs: 0 }));
    await act(async () => { result.current.toggle(); });
    await act(async () => { c._emitState("c1", "speaking"); });
    expect(result.current.state).toBe("speaking");
  });

  it("auto-closes after the idle timeout when nothing happens", async () => {
    const c = fakeClient();
    const { result } = renderHook(() =>
      useVoiceSession(c as never, "c1", { vadThreshold: 0.5, endOfTurnSilenceMs: 700, idleTimeoutMs: 30 }));
    await act(async () => { result.current.toggle(); });
    await waitFor(() => expect(c.sendVoiceStart).toHaveBeenCalledWith("c1"));
    await waitFor(() => expect(c.sendVoiceStop).toHaveBeenCalledWith("c1"));
    expect(result.current.active).toBe(false);
  });

  it("does not auto-close while a turn is in progress", async () => {
    const c = fakeClient();
    const { result } = renderHook(() =>
      useVoiceSession(c as never, "c1", { vadThreshold: 0.5, endOfTurnSilenceMs: 700, idleTimeoutMs: 40 }));
    await act(async () => { result.current.toggle(); });
    await act(async () => { c._emitState("c1", "speaking"); });
    await new Promise((r) => setTimeout(r, 80));
    expect(c.sendVoiceStop).not.toHaveBeenCalled();
    expect(result.current.active).toBe(true);
  });
});
