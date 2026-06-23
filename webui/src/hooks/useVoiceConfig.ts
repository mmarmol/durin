import { useEffect, useState } from "react";
import { getConfig, getExtraStatus } from "@/lib/api";

interface VoiceConfigShape {
  enabled?: boolean;
  vad_threshold?: number;
  end_of_turn_silence_ms?: number;
}

export function useVoiceConfig(token: string) {
  const [enabled, setEnabled] = useState(true);
  const [vadThreshold, setVadThreshold] = useState(0.5);
  const [endOfTurnSilenceMs, setEndOfTurnSilenceMs] = useState(700);
  // `available` gates the orb: voice can only run when it can actually speak,
  // i.e. the TTS backend is usable. Local TTS needs the [tts] extra installed;
  // cloud providers are assumed reachable (the key is checked server-side).
  const [available, setAvailable] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const snap = await getConfig(token);
        const cfg = (snap.config as Record<string, unknown>) ?? {};
        const v = (cfg.voice ?? {}) as VoiceConfigShape;
        const ttsProvider =
          ((cfg.tts as { provider?: string } | undefined)?.provider) ?? "local";
        let ttsReady = ttsProvider !== "local";
        if (!ttsReady) {
          try {
            ttsReady = Boolean((await getExtraStatus(token, "tts")).present);
          } catch {
            ttsReady = false;
          }
        }
        if (cancelled) return;
        const en = typeof v.enabled === "boolean" ? v.enabled : true;
        setEnabled(en);
        setVadThreshold(typeof v.vad_threshold === "number" ? v.vad_threshold : 0.5);
        setEndOfTurnSilenceMs(
          typeof v.end_of_turn_silence_ms === "number" ? v.end_of_turn_silence_ms : 700,
        );
        setAvailable(en && ttsReady);
      } catch {
        if (!cancelled) setAvailable(false);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  return { enabled, vadThreshold, endOfTurnSilenceMs, available, loading };
}
