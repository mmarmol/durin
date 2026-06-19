import { useEffect, useRef, useState } from "react";

import { pickAudioMime } from "@/lib/audioMime";

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
}

export function MicButton({ onRecorded, disabled }: MicButtonProps) {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);

  const supported = typeof MediaRecorder !== "undefined";

  async function start() {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const mime = pickAudioMime();
      const rec = mime
        ? new MediaRecorder(stream, { mimeType: mime })
        : new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = () => {
        const type = rec.mimeType || "audio/webm";
        const blob = new Blob(chunksRef.current, { type });
        const ext = type.includes("mp4") ? "m4a" : "webm";
        const file = new File([blob], `recording.${ext}`, { type: blob.type });
        onRecorded(file);
        streamRef.current?.getTracks().forEach((t) => t.stop());
      };
      rec.start();
      recorderRef.current = rec;
      setRecording(true);
    } catch {
      setError("Allow microphone access to record.");
    }
  }

  function stop() {
    recorderRef.current?.stop();
    setRecording(false);
  }

  // Release the mic if the component unmounts mid-recording.
  useEffect(() => {
    return () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  return (
    <span className="relative inline-flex items-center">
      <button
        type="button"
        aria-label="mic"
        disabled={!supported || disabled}
        onClick={recording ? stop : start}
        className={
          "inline-flex h-8 w-8 items-center justify-center rounded-md border text-sm " +
          (recording
            ? "animate-pulse border-red-500 bg-red-600 text-white"
            : "border-border bg-background hover:bg-accent")
        }
        title={recording ? "Stop recording" : "Record audio"}
      >
        {recording ? "⏹" : "🎙"}
      </button>
      {error && (
        <span role="alert" className="ml-2 text-xs text-red-500">
          {error}
        </span>
      )}
    </span>
  );
}
