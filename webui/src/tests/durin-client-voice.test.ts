import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DurinClient } from "@/lib/durin-client";

class FakeSocket {
  static instances: FakeSocket[] = [];
  static readonly OPEN = 1;
  url: string; readyState = 1; sent: string[] = [];
  onopen: (() => void) | null = null; onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null; onclose: (() => void) | null = null;
  constructor(url: string) { this.url = url; FakeSocket.instances.push(this); }
  send(d: string) { this.sent.push(d); }
  close() { this.readyState = 3; this.onclose?.(); }
  fakeOpen() { this.readyState = 1; this.onopen?.(); }
  fakeMessage(p: unknown) { this.onmessage?.({ data: JSON.stringify(p) } as MessageEvent); }
}
const last = () => FakeSocket.instances.at(-1)!;

beforeEach(() => { FakeSocket.instances = []; });
afterEach(() => vi.restoreAllMocks());

function makeClient() {
  const c = new DurinClient({ url: "ws://t", reconnect: false, socketFactory: (u) => new FakeSocket(u) as unknown as WebSocket });
  c.connect(); last().fakeOpen();
  return c;
}

describe("DurinClient voice", () => {
  it("sends a voice_utterance frame with wav media", () => {
    const c = makeClient();
    c.sendVoiceUtterance("c1", "data:audio/wav;base64,AAA");
    const f = JSON.parse(last().sent.at(-1)!);
    expect(f).toEqual({ type: "voice_utterance", chat_id: "c1", media: [{ data_url: "data:audio/wav;base64,AAA" }], webui: true });
  });

  it("sends voice_start/stop/barge_in/read_all", () => {
    const c = makeClient();
    c.sendVoiceStart("c1"); c.sendVoiceStop("c1"); c.sendVoiceBargeIn("c1"); c.sendVoiceReadAll("c1", "full text");
    const types = last().sent.map((s) => JSON.parse(s).type);
    expect(types).toEqual(expect.arrayContaining(["voice_start", "voice_stop", "voice_barge_in", "voice_read_all"]));
    expect(JSON.parse(last().sent.at(-1)!)).toEqual({ type: "voice_read_all", chat_id: "c1", text: "full text", webui: true });
  });

  it("routes voice_state and voice_audio to global handlers", () => {
    const c = makeClient();
    const states: Array<[string, string]> = [];
    const audios: Array<[string, string]> = [];
    c.onVoiceState((cid, s) => states.push([cid, s]));
    c.onVoiceAudio((cid, url) => audios.push([cid, url]));
    last().fakeMessage({ event: "voice_state", chat_id: "c1", state: "listening" });
    last().fakeMessage({ event: "voice_audio", chat_id: "c1", url: "/api/media/x", mime: "audio/wav" });
    expect(states).toEqual([["c1", "listening"]]);
    expect(audios).toEqual([["c1", "/api/media/x"]]);
  });

  it("sends a voice_preview frame with voice + language", () => {
    const c = makeClient();
    c.sendVoicePreview("F4", "es");
    const f = JSON.parse(last().sent.at(-1)!);
    expect(f).toEqual({ type: "voice_preview", voice: "F4", language: "es", webui: true });
  });

  it("routes voice_preview_audio (url + error) to global handlers", () => {
    const c = makeClient();
    const events: Array<[string | null, string | undefined]> = [];
    c.onVoicePreviewAudio((url, error) => events.push([url, error]));
    last().fakeMessage({ event: "voice_preview_audio", url: "/api/media/p", mime: "audio/wav" });
    last().fakeMessage({ event: "voice_preview_audio", error: "tts_unavailable" });
    expect(events).toEqual([
      ["/api/media/p", undefined],
      [null, "tts_unavailable"],
    ]);
  });
});
