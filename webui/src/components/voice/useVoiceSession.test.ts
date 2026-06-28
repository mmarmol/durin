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
  const statusHandlers: Array<(s: string) => void> = [];
  const audioHandlers: Array<(cid: string, url: string) => void> = [];
  let status = "open";
  return {
    onVoiceState: (h: (cid: string, s: string) => void) => { handlers.push(h); return () => {}; },
    onVoiceAudio: (h: (cid: string, url: string) => void) => { audioHandlers.push(h); return () => {}; },
    onStatus: (h: (s: string) => void) => { statusHandlers.push(h); h(status); return () => {}; },
    sendVoiceStart: vi.fn(), sendVoiceStop: vi.fn(), sendVoiceUtterance: vi.fn(),
    sendVoiceBargeIn: vi.fn(), sendVoiceReadAll: vi.fn(),
    _emitState: (cid: string, s: string) => handlers.forEach((h) => h(cid, s)),
    _emitStatus: (s: string) => { status = s; statusHandlers.forEach((h) => h(s)); },
    _emitAudio: (cid: string, url: string) => audioHandlers.forEach((h) => h(cid, url)),
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

  it("uses a fresh audio element each session (no createMediaElementSource reuse)", async () => {
    // The Web Audio API forbids connecting one HTMLMediaElement to a second
    // MediaElementSourceNode. Reusing the element across sessions throws
    // InvalidStateError — model that constraint and assert it never trips.
    const connectedEls: object[] = [];
    vi.stubGlobal("AudioContext", class {
      createMediaStreamSource() { return { connect() {} }; }
      createAnalyser() { return { fftSize: 0, frequencyBinCount: 8, getByteTimeDomainData() {}, connect() {} }; }
      createMediaElementSource(el: object) {
        if (connectedEls.includes(el)) throw new Error("InvalidStateError: already connected");
        connectedEls.push(el);
        return { connect() {} };
      }
      close() {}
      get destination() { return {}; }
    });
    vi.stubGlobal("Audio", class { crossOrigin = ""; src = ""; play() { return Promise.resolve(); } pause() {} });

    const c = fakeClient();
    const { result } = renderHook(() => useVoiceSession(c as never, "c1", { vadThreshold: 0.5, endOfTurnSilenceMs: 700, idleTimeoutMs: 0 }));

    await act(async () => { result.current.toggle(); });
    await waitFor(() => expect(c.sendVoiceStart).toHaveBeenCalledTimes(1));
    await act(async () => { c._emitAudio("c1", "blob:one"); await Promise.resolve(); });

    // Stop, then open a second session and play again.
    await act(async () => { result.current.toggle(); });
    await act(async () => { result.current.toggle(); });
    await waitFor(() => expect(c.sendVoiceStart).toHaveBeenCalledTimes(2));
    await act(async () => { c._emitAudio("c1", "blob:two"); await Promise.resolve(); });

    expect(connectedEls.length).toBe(2);
    expect(connectedEls[0]).not.toBe(connectedEls[1]);
  });

  it("closes the leaked AudioContext when VAD start fails", async () => {
    // Mic-permission denial or a WASM load hiccup rejects MicVAD.new. The
    // half-built session must be torn down (context closed, orb back to idle)
    // so the user can retry — leaking contexts eventually wedges voice for good.
    let closes = 0;
    vi.stubGlobal("AudioContext", class {
      createMediaStreamSource() { return { connect() {} }; }
      createAnalyser() { return { fftSize: 0, frequencyBinCount: 8, getByteTimeDomainData() {}, connect() {} }; }
      createMediaElementSource() { return { connect() {} }; }
      close() { closes++; }
      get destination() { return {}; }
    });
    const { MicVAD } = await import("@ricky0123/vad-web");
    vi.mocked(MicVAD.new).mockRejectedValueOnce(new Error("wasm load failed"));

    const c = fakeClient();
    const { result } = renderHook(() => useVoiceSession(c as never, "c1", { vadThreshold: 0.5, endOfTurnSilenceMs: 700, idleTimeoutMs: 0 }));
    await act(async () => { result.current.toggle(); });

    await waitFor(() => expect(closes).toBe(1));
    expect(result.current.active).toBe(false);
    expect(c.sendVoiceStart).not.toHaveBeenCalled();
  });

  it("re-establishes the voice session on reconnect (self-heal)", async () => {
    const c = fakeClient();
    const { result } = renderHook(() =>
      useVoiceSession(c as never, "c1", { vadThreshold: 0.5, endOfTurnSilenceMs: 700, idleTimeoutMs: 0 }));
    await act(async () => { result.current.toggle(); });
    await waitFor(() => expect(c.sendVoiceStart).toHaveBeenCalledTimes(1));
    // The socket drops and reconnects; the server forgets the in-memory voice
    // session, so the browser must re-send voice_start to keep audio working.
    await act(async () => { c._emitStatus("reconnecting"); c._emitStatus("open"); });
    expect(c.sendVoiceStart).toHaveBeenCalledTimes(2);
    expect(c.sendVoiceStart).toHaveBeenLastCalledWith("c1");
  });
});
