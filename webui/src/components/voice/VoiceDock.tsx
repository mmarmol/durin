import { useClient } from "@/providers/ClientProvider";
import { useVoiceConfig } from "@/hooks/useVoiceConfig";
import { useVoiceSession } from "./useVoiceSession";
import { VoiceOrb } from "./VoiceOrb";

export function VoiceDock({ chatId }: { chatId: string | null }) {
  const { client, token } = useClient();
  const cfg = useVoiceConfig(token);
  const { state, amplitude, toggle } = useVoiceSession(client, chatId, {
    vadThreshold: cfg.vadThreshold,
    endOfTurnSilenceMs: cfg.endOfTurnSilenceMs,
  });
  if (cfg.loading || !cfg.available) return null;
  return (
    <div className="fixed bottom-4 right-4 z-50">
      <VoiceOrb state={state} amplitude={amplitude} onToggle={toggle} />
    </div>
  );
}
