import { useCallback, useEffect, useState } from "react";

import { getConfig, getExtraStatus } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

type Provider = "local" | "openai" | "groq" | "http";

interface TranscriptionStatus {
  /** True when audio input should be offered: the feature is enabled AND the
   *  configured provider is usable (local extra installed, or a cloud/http
   *  provider selected regardless of key — the key gap surfaces as a clear
   *  error on first attempt, not by hiding the feature). */
  available: boolean;
  /** Why it's unavailable, for a tooltip / hint. */
  reason: string | null;
}

/** Decide whether the composer's mic / attach-audio affordances should show.
 *
 * Hides them when transcription is disabled or when the local provider is
 * picked but the ``[stt]`` extra isn't installed. Cloud/HTTP providers are
 * always offered (a missing key produces a clear error on use). */
export function useTranscriptionStatus(): TranscriptionStatus {
  const { token } = useClient();
  const [status, setStatus] = useState<TranscriptionStatus>({
    available: true,
    reason: null,
  });

  const refresh = useCallback(async () => {
    try {
      const [snap, sttExtra] = await Promise.all([
        getConfig(token),
        getExtraStatus(token, "stt"),
      ]);
      const t = (snap.config as Record<string, unknown>)?.transcription as
        | {
            enabled?: boolean;
            provider?: Provider;
          }
        | undefined;
      const enabled = t?.enabled !== false; // default true
      if (!enabled) {
        setStatus({ available: false, reason: "Transcription is disabled in settings." });
        return;
      }
      const provider = t?.provider ?? "local";
      if (provider === "local" && !sttExtra.present) {
        setStatus({
          available: false,
          reason: "Local Whisper isn't installed. Add the [stt] extra or pick a cloud provider in Settings → Audio transcription.",
        });
        return;
      }
      setStatus({ available: true, reason: null });
    } catch {
      // Can't read config (e.g. offline) — don't hide the feature, let it
      // surface its own error. Optimistic default.
      setStatus({ available: true, reason: null });
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return status;
}
