import { useCallback, useEffect, useRef, useState } from "react";
import { MicVAD, utils } from "@ricky0123/vad-web";
import type { DurinClient } from "@/lib/durin-client";
import { ONNX_WASM_BASE_PATH, VAD_BASE_ASSET_PATH } from "@/lib/voiceAssets";
import type { OrbState } from "./VoiceOrb";

interface Cfg { vadThreshold: number; endOfTurnSilenceMs: number; idleTimeoutMs: number }

export function useVoiceSession(client: DurinClient, chatId: string | null, cfg: Cfg) {
  const [state, setState] = useState<OrbState>("idle");
  const [amplitude, setAmplitude] = useState(0);
  const [active, setActive] = useState(false);
  const stateRef = useRef<OrbState>("idle");
  stateRef.current = state;
  const vadRef = useRef<{ start: () => void; destroy: () => void } | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const micAnalyserRef = useRef<AnalyserNode | null>(null);
  const playAnalyserRef = useRef<AnalyserNode | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (!active || !chatId) return;
    const offState = client.onVoiceState((cid, s) => { if (cid === chatId) setState(s as OrbState); });
    const offAudio = client.onVoiceAudio((cid, url) => { if (cid === chatId) void play(url); });
    return () => { offState(); offAudio(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, chatId, client]);

  // Self-heal: the gateway holds the voice session in memory per-connection and
  // drops it on a socket reconnect, while the browser stays active and keeps
  // transcribing — so replies would otherwise come back as text with no audio.
  // On a real reconnect (down → open), re-send voice_start to re-establish it.
  useEffect(() => {
    if (!active || !chatId) return;
    let wasDown = false;
    return client.onStatus((s) => {
      if (s === "reconnecting" || s === "connecting" || s === "closed") wasDown = true;
      else if (s === "open" && wasDown) { wasDown = false; client.sendVoiceStart(chatId); }
    });
  }, [active, chatId, client]);

  const play = useCallback(async (url: string) => {
    const ctx = ctxRef.current; if (!ctx) return;
    const el = audioRef.current ?? new Audio();
    audioRef.current = el; el.crossOrigin = "anonymous"; el.src = url;
    if (!playAnalyserRef.current) {
      const src = ctx.createMediaElementSource(el);
      const an = ctx.createAnalyser(); an.fftSize = 2048;
      src.connect(an); an.connect(ctx.destination);
      playAnalyserRef.current = an;
    }
    try { await el.play(); } catch { /* autoplay blocked; user gesture already happened on toggle */ }
  }, []);

  const loop = useCallback(() => {
    const an = stateRef.current === "speaking" ? playAnalyserRef.current : micAnalyserRef.current;
    if (an) {
      const data = new Uint8Array(an.frequencyBinCount);
      an.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
      setAmplitude(Math.sqrt(sum / data.length));
    } else setAmplitude(0);
    rafRef.current = requestAnimationFrame(loop);
  }, []);

  const start = useCallback(async () => {
    if (active || !chatId) return;
    const ctx = new AudioContext(); ctxRef.current = ctx;
    const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    const vad = await MicVAD.new({
      positiveSpeechThreshold: cfg.vadThreshold,
      negativeSpeechThreshold: Math.max(0, cfg.vadThreshold - 0.15),
      redemptionMs: cfg.endOfTurnSilenceMs,
      model: "v5",
      baseAssetPath: VAD_BASE_ASSET_PATH,
      onnxWASMBasePath: ONNX_WASM_BASE_PATH,
      getStream: async () => {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        const src = ctx.createMediaStreamSource(stream);
        const an = ctx.createAnalyser(); an.fftSize = 2048;
        src.connect(an); // not to destination — avoids mic feedback
        micAnalyserRef.current = an;
        return stream;
      },
      onSpeechStart: () => {
        if (stateRef.current === "speaking") {
          audioRef.current?.pause();
          client.sendVoiceBargeIn(chatId);
          setState("listening");
        }
      },
      onSpeechEnd: (audio: Float32Array) => {
        const b64 = utils.arrayBufferToBase64(utils.encodeWAV(audio));
        client.sendVoiceUtterance(chatId, `data:audio/wav;base64,${b64}`);
      },
    });
    vadRef.current = vad;
    vad.start();
    client.sendVoiceStart(chatId);
    setActive(true);
    setState("listening");
    if (!reduced) rafRef.current = requestAnimationFrame(loop);
  }, [active, chatId, cfg, client, loop]);

  const stop = useCallback(() => {
    if (chatId) client.sendVoiceStop(chatId);
    vadRef.current?.destroy(); vadRef.current = null;
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    void ctxRef.current?.close(); ctxRef.current = null;
    micAnalyserRef.current = null; playAnalyserRef.current = null;
    setActive(false); setState("idle"); setAmplitude(0);
  }, [chatId, client]);

  const toggle = useCallback(() => { if (active) stop(); else void start(); }, [active, start, stop]);
  useEffect(() => () => { if (vadRef.current) stop(); }, [stop]);

  // Auto-close after a stretch of silence. The clock runs only in the idle
  // "listening" state and is reset by any state transition (a turn moves through
  // transcribing/thinking/speaking), so it never fires mid-exchange. 0 disables it.
  useEffect(() => {
    if (!active || cfg.idleTimeoutMs <= 0 || state !== "listening") return;
    const id = setTimeout(() => { stop(); }, cfg.idleTimeoutMs);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, state, cfg.idleTimeoutMs, stop]);

  return { state, amplitude, active, toggle };
}
