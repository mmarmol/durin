import { useEffect, useState } from "react";
import { getConfig } from "@/lib/api";

interface VoiceConfigShape {
  enabled?: boolean;
  vad_threshold?: number;
  end_of_turn_silence_ms?: number;
}

export function useVoiceConfig(token: string) {
  const [enabled, setEnabled] = useState(true);
  const [vadThreshold, setVadThreshold] = useState(0.5);
  const [endOfTurnSilenceMs, setEndOfTurnSilenceMs] = useState(700);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const snap = await getConfig(token);
        const v = ((snap.config as Record<string, unknown>)?.voice ?? {}) as VoiceConfigShape;
        if (cancelled) return;
        setEnabled(typeof v.enabled === "boolean" ? v.enabled : true);
        setVadThreshold(typeof v.vad_threshold === "number" ? v.vad_threshold : 0.5);
        setEndOfTurnSilenceMs(typeof v.end_of_turn_silence_ms === "number" ? v.end_of_turn_silence_ms : 700);
      } catch {
        // keep defaults
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [token]);

  return { enabled, vadThreshold, endOfTurnSilenceMs, loading };
}
