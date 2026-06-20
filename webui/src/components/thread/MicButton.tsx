import { useEffect, useRef, useState } from "react";
import { Mic, Square } from "lucide-react";

import { pickAudioMime } from "@/lib/audioMime";
import { cn } from "@/lib/utils";

/** Microphone recorder button (spec §5.2).
 *
 * Uses the native ``MediaRecorder`` API (no external deps). On stop, produces
 * a ``File`` and hands it to ``onRecorded`` — typically wired into
 * ``useAttachedAudio.enqueue`` so the recording flows through the same path as
 * an attached file.
 *
 * Renders disabled when ``MediaRecorder`` is unavailable (older browsers).
 */
interface MicButtonProps {
  onRecorded: (file: File) => void;
  disabled?: boolean;
  /** "hero" (large, with border/shadow) or "thread" (compact). Mirrors the
   *  composer's Paperclip sizing so the two buttons line up. */
  variant?: "hero" | "thread";
}

export function MicButton({ onRecorded, disabled, variant = "thread" }: MicButtonProps) {
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const supported = typeof MediaRecorder !== "undefined";

  async function start() {
    setError(null);
    setElapsed(0);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const mime = pickAudioMime();
      const rec = mime
        ? new MediaRecorder(stream, { mimeType: mime })
        : new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = () => {
        const type = rec.mimeType || "audio/webm";
        const chunks = chunksRef.current;
        const blob = new Blob(chunks, { type });
        streamRef.current?.getTracks().forEach((t) => t.stop());
        if (blob.size === 0) {
          setError("No audio captured — try speaking louder or closer to the mic.");
          return;
        }
        const ext = type.includes("mp4") ? "m4a" : "webm";
        const file = new File([blob], `recording.${ext}`, { type: blob.type });
        onRecorded(file);
      };
      rec.onerror = () => {
        setError("Recording error.");
        setRecording(false);
        if (timerRef.current !== null) {
          clearInterval(timerRef.current);
          timerRef.current = null;
        }
      };
      // timeslice=250ms so ondataavailable fires periodically (not just once
      // at stop) — this makes onstop reliable across browsers and ensures
      // chunksRef is populated before the Blob is assembled.
      rec.start(250);
      recorderRef.current = rec;
      setRecording(true);
      timerRef.current = setInterval(() => {
        setElapsed((e) => e + 1);
      }, 1000);
    } catch {
      setError("Allow microphone access to record.");
    }
  }

  function stop() {
    const rec = recorderRef.current;
    if (rec && rec.state !== "inactive") {
      rec.stop();
    }
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setElapsed(0);
    setRecording(false);
  }

  // Release the mic and elapsed timer if the component unmounts mid-recording.
  useEffect(() => {
    return () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
      }
    };
  }, []);

  const isHero = variant === "hero";

  return (
    <span className="relative inline-flex items-center">
      <button
        type="button"
        aria-label="mic"
        disabled={!supported || disabled}
        onClick={recording ? stop : start}
        className={cn(
          "inline-flex items-center justify-center rounded-full text-muted-foreground hover:text-foreground",
          isHero
            ? "h-9 w-9 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card"
            : "h-7.5 w-7.5 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card",
          recording && "border-red-500 text-red-500 hover:text-red-600",
        )}
        title={recording ? "Stop recording" : "Record audio"}
      >
        {recording ? (
          <Square className={cn(isHero ? "h-4 w-4" : "h-3.5 w-3.5")} fill="currentColor" />
        ) : (
          <Mic className={cn(isHero ? "h-5 w-5" : "h-4 w-4")} />
        )}
      </button>
      {recording ? (
        <span
          aria-live="off"
          className="ml-1.5 font-mono text-xs tabular-nums text-red-500"
        >
          {`${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}`}
        </span>
      ) : null}
      {error && (
        <span role="alert" className="ml-2 text-xs text-red-500">
          {error}
        </span>
      )}
    </span>
  );
}
