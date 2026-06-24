import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useClient } from "@/providers/ClientProvider";
import { useVoiceConfig } from "@/hooks/useVoiceConfig";
import { useVoiceSession } from "./useVoiceSession";
import { VoiceOrb } from "./VoiceOrb";

/** Floating voice control, docked bottom-right.
 *
 * Idle: a compact orb to enter voice mode. Active: an expanded panel showing the
 * current state in words (listening / thinking / speaking), which chat it is
 * voicing, and a stop button — so it is always legible what voice is doing and
 * where it is happening. Voice always runs on a real, focused chat: if none is
 * active, `onEnsureChat` creates and focuses one first. */
export function VoiceDock({
  chatId,
  chatTitle,
  onEnsureChat,
  hideWhenIdle = false,
}: {
  chatId: string | null;
  chatTitle?: string | null;
  onEnsureChat: () => Promise<string | null>;
  /** Suppress the idle start-orb (e.g. on full-screen editor views where it
   * would float over the tool's own bottom-right controls). An in-progress
   * call still shows, so switching views never hides or interrupts it. */
  hideWhenIdle?: boolean;
}) {
  const { t } = useTranslation();
  const { client, token } = useClient();
  const cfg = useVoiceConfig(token);
  const { state, amplitude, active, toggle } = useVoiceSession(client, chatId, {
    vadThreshold: cfg.vadThreshold,
    endOfTurnSilenceMs: cfg.endOfTurnSilenceMs,
    idleTimeoutMs: cfg.idleTimeoutMs,
  });
  const pendingStart = useRef(false);

  // Deferred start: when we had to create+focus a chat first, start voice once
  // its id has propagated in (so the session binds to the visible chat).
  useEffect(() => {
    if (pendingStart.current && chatId && !active) {
      pendingStart.current = false;
      toggle();
    }
  }, [chatId, active, toggle]);

  if (cfg.loading || !cfg.available) return null;

  const handleToggle = () => {
    if (active || chatId) {
      toggle();
      return;
    }
    // No active chat: create + focus one, then the effect above starts voice.
    pendingStart.current = true;
    void onEnsureChat().then((id) => {
      if (!id) pendingStart.current = false;
    });
  };

  const stateLabel = t(`settings.voice.orb.${state}`);

  if (!active) {
    if (hideWhenIdle) return null;
    return (
      <div className="fixed bottom-4 right-4 z-50">
        <VoiceOrb
          state={state}
          amplitude={amplitude}
          size={56}
          label={t("settings.voice.orb.start")}
          onClick={handleToggle}
        />
      </div>
    );
  }

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
        onClick={handleToggle}
        className="grid h-8 w-8 shrink-0 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
          <rect x="2.5" y="2.5" width="9" height="9" rx="2" fill="currentColor" />
        </svg>
      </button>
    </div>
  );
}
