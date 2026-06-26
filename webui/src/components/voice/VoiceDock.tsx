import { useTranslation } from "react-i18next";
import type { OrbState } from "./VoiceOrb";
import { VoiceOrb } from "./VoiceOrb";

/** Floating status panel for an in-progress voice call. The idle entry now lives
 * in the composer (the orb button); this renders only while a call is active, so
 * switching views never hides or interrupts it. Voice session state is owned by
 * the app shell and passed in. */
export function VoiceDock({
  state,
  amplitude,
  active,
  chatTitle,
  onStop,
}: {
  state: OrbState;
  amplitude: number;
  active: boolean;
  chatTitle?: string | null;
  onStop: () => void;
}) {
  const { t } = useTranslation();
  if (!active) return null;

  const stateLabel = t(`settings.voice.orb.${state}`);
  return (
    <div className="fixed bottom-4 right-4 z-50 flex items-center gap-3 rounded-[20px] border bg-card/95 py-2 pl-3 pr-2 shadow-lg backdrop-blur-sm">
      <VoiceOrb state={state} amplitude={amplitude} size={60} label={stateLabel} />
      <div className="flex min-w-0 flex-col pr-1">
        <span className="text-[13px] font-medium leading-tight text-foreground">{stateLabel}</span>
        {chatTitle ? (
          <span className="max-w-[180px] truncate text-[11px] leading-tight text-muted-foreground">
            {chatTitle}
          </span>
        ) : null}
        {state === "speaking" ? (
          <span className="text-[11px] leading-tight text-muted-foreground">
            {t("settings.voice.orb.bargeHint")}
          </span>
        ) : null}
      </div>
      <button
        type="button"
        aria-label={t("settings.voice.orb.stop")}
        title={t("settings.voice.orb.stop")}
        onClick={onStop}
        className="grid h-8 w-8 shrink-0 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
          <rect x="2.5" y="2.5" width="9" height="9" rx="2" fill="currentColor" />
        </svg>
      </button>
    </div>
  );
}
