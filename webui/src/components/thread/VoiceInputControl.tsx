import { useEffect, useRef, useState } from "react";
import { ChevronDown, Keyboard, Radio } from "lucide-react";
import { useTranslation } from "react-i18next";

import { MicButton, type MicButtonHandle } from "@/components/thread/MicButton";
import { cn } from "@/lib/utils";

interface VoiceInputControlProps {
  /** Receives a recorded audio File (wired into the transcription pipeline). */
  onRecorded: (file: File) => void;
  /** Disables the mic recorder (e.g. a recording is already in flight). */
  recordDisabled?: boolean;
  /** Whether dictation (mic record) is offered at all. */
  audioInputAllowed?: boolean;
  /** Enter (or toggle) hands-free voice mode. Omitted → no voice option. */
  onEnterVoice?: () => void;
  voiceActive?: boolean;
  variant?: "hero" | "thread";
}

/** Groups the two voice affordances into one control. When idle, the mic button
 *  records (dictation) and a chevron opens a menu to dictate or switch to
 *  hands-free voice. During an active call (or when dictation is unavailable),
 *  it collapses to the orb itself so ending the call stays a single click. */
export function VoiceInputControl({
  onRecorded,
  recordDisabled,
  audioInputAllowed = true,
  onEnterVoice,
  voiceActive = false,
  variant = "thread",
}: VoiceInputControlProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const micRef = useRef<MicButtonHandle>(null);
  const isHero = variant === "hero";

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  if (!audioInputAllowed && !onEnterVoice) return null;

  // Active call (or no dictation available): the orb is shown directly so its
  // single click toggles voice — no dropdown to end a live call.
  if (onEnterVoice && (voiceActive || !audioInputAllowed)) {
    return (
      <button
        type="button"
        aria-label={voiceActive ? t("settings.voice.orb.stop") : t("settings.voice.orb.start")}
        title={voiceActive ? t("settings.voice.orb.stop") : t("settings.voice.orb.start")}
        onClick={onEnterVoice}
        className={cn(
          "inline-flex items-center justify-center rounded-full",
          "border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card",
          isHero ? "h-9 w-9" : "h-7.5 w-7.5",
        )}
      >
        <span
          aria-hidden
          className={cn(
            "rounded-full bg-primary ring-2 ring-primary/20",
            isHero ? "h-3.5 w-3.5" : "h-3 w-3",
            voiceActive && "ring-primary/45 motion-safe:animate-pulse",
          )}
        />
      </button>
    );
  }

  return (
    <div ref={containerRef} className="relative inline-flex items-center gap-0.5">
      <MicButton
        ref={micRef}
        onRecorded={onRecorded}
        disabled={recordDisabled}
        variant={variant}
      />
      {onEnterVoice ? (
        <>
          <button
            type="button"
            aria-label={t("thread.composer.voice.title")}
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className={cn(
              "inline-flex items-center justify-center rounded-full text-muted-foreground hover:text-foreground",
              isHero ? "h-9 w-5" : "h-7.5 w-4",
            )}
          >
            <ChevronDown className="h-3.5 w-3.5" aria-hidden />
          </button>
          {open ? (
            <div
              role="menu"
              className={cn(
                "absolute bottom-full left-0 z-50 mb-2 w-[200px]",
                "rounded-xl border border-border/70 bg-popover p-1 shadow-xl",
                "animate-in fade-in-0 zoom-in-95 slide-in-from-bottom-2 duration-200",
              )}
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  micRef.current?.startRecording();
                }}
                className={cn(
                  "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px]",
                  "text-foreground/85 transition-colors hover:bg-muted/60",
                )}
              >
                <Keyboard className="h-4 w-4 flex-none text-muted-foreground" aria-hidden />
                {t("thread.composer.voice.dictation")}
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  onEnterVoice();
                }}
                className={cn(
                  "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px]",
                  "text-foreground/85 transition-colors hover:bg-muted/60",
                )}
              >
                <Radio className="h-4 w-4 flex-none text-muted-foreground" aria-hidden />
                {t("thread.composer.voice.handsFree")}
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
