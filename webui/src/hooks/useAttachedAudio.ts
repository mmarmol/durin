import { useCallback, useEffect, useRef, useState } from "react";

/** Lifecycle of one audio attachment. Audio is not re-encoded (unlike images),
 * so there is no "encoding" stage — it goes straight to ``ready``.
 * Intermediate transcription phases mirror the server's progress events. */
export type AudioAttachmentStatus =
  | "ready"
  | "error"
  | "downloading"
  | "loading"
  | "transcribing";

export interface AttachedAudio {
  id: string;
  file: File;
  /** Optimistic ``blob:`` preview URL for the ``<audio>`` player; revoked on
   * ``remove`` / ``clear`` / unmount. */
  previewUrl: string;
  status: AudioAttachmentStatus;
  /** Duration in seconds, populated lazily by the player if needed. */
  durationS?: number;
  error?: string;
}

export interface UseAttachedAudioApi {
  audio: AttachedAudio[];
  /** Enqueue new files. Returns the list of accepted and rejected files so the
   * caller can surface inline errors and obtain attachment ids immediately.
   * Files rejected client-side (wrong MIME, limit) are *not* added to
   * ``audio``. */
  enqueue: (files: Iterable<File>) => {
    accepted: AttachedAudio[];
    rejected: Array<{ file: File; reason: string }>;
  };
  /** Update the status of a specific attachment by id. Used to reflect
   * server-side transcription progress phases. */
  setStatus: (id: string, status: AudioAttachmentStatus) => void;
  remove: (id: string) => void;
  /** Revoke every staged blob URL and drop all attachments. Called after a
   * successful submit. */
  clear: () => void;
  /** ``true`` when we've hit ``MAX_AUDIO_PER_MESSAGE``. */
  full: boolean;
}

export const MAX_AUDIO_PER_MESSAGE = 1;
export const MAX_AUDIO_BYTES = 25 * 1024 * 1024;

/** MIME whitelist — mirrors the server's ``_AUDIO_MIME_ALLOWED`` and the
 * ``<input accept>`` attr. */
const ACCEPTED_MIMES: ReadonlySet<string> = new Set([
  "audio/mpeg",
  "audio/ogg",
  "audio/opus",
  "audio/wav",
  "audio/webm",
  "audio/x-m4a",
  "audio/aac",
  "audio/flac",
]);

/** Normalize a MIME type by stripping the ``;codecs=...`` suffix so that
 * ``audio/webm;codecs=opus`` matches the whitelist entry ``audio/webm``.
 * MediaRecorder produces the codec-qualified form; the whitelist uses bare. */
function normalizeMime(mime: string): string {
  return mime.split(";")[0].trim().toLowerCase();
}

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `aud-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** Manage the lifecycle of audio attached to the Composer (spec §5.1).
 *
 * Responsibilities in one place:
 *   - validation (MIME whitelist, count cap, size cap)
 *   - blob URL creation + revocation
 */
export function useAttachedAudio(): UseAttachedAudioApi {
  const [audio, setAudio] = useState<AttachedAudio[]>([]);
  // Ref mirror so ``enqueue`` can see the authoritative length when invoked
  // multiple times in a single tick. ``state`` is stale for that second call.
  const audioRef = useRef<AttachedAudio[]>([]);
  audioRef.current = audio;

  const enqueue = useCallback((files: Iterable<File>) => {
    const rejected: Array<{ file: File; reason: string }> = [];
    const toAdd: AttachedAudio[] = [];
    let slot = MAX_AUDIO_PER_MESSAGE - audioRef.current.length;

    for (const file of files) {
      if (!ACCEPTED_MIMES.has(normalizeMime(file.type))) {
        rejected.push({ file, reason: "unsupported_type" });
        continue;
      }
      if (file.size > MAX_AUDIO_BYTES) {
        rejected.push({ file, reason: "too_large" });
        continue;
      }
      if (slot <= 0) {
        rejected.push({ file, reason: "too_many" });
        continue;
      }
      slot -= 1;
      toAdd.push({
        id: uuid(),
        file,
        previewUrl: URL.createObjectURL(file),
        status: "ready",
      });
    }
    if (toAdd.length > 0) {
      const next = [...audioRef.current, ...toAdd];
      audioRef.current = next;
      setAudio(next);
    }
    return { accepted: toAdd, rejected };
  }, []);

  const remove = useCallback((id: string) => {
    setAudio((prev) => {
      const target = prev.find((a) => a.id === id);
      if (target) {
        try {
          URL.revokeObjectURL(target.previewUrl);
        } catch {
          /* best-effort */
        }
      }
      const next = prev.filter((a) => a.id !== id);
      audioRef.current = next;
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setAudio((prev) => {
      for (const a of prev) {
        try {
          URL.revokeObjectURL(a.previewUrl);
        } catch {
          /* best-effort */
        }
      }
      audioRef.current = [];
      return [];
    });
  }, []);

  // Final safety net: revoke any outstanding blob URLs on unmount. Safe
  // under StrictMode double-invoke because revoked blob URLs are only
  // referenced from in-hook chip state, which is rebuilt on remount.
  useEffect(() => {
    return () => {
      for (const a of audioRef.current) {
        try {
          URL.revokeObjectURL(a.previewUrl);
        } catch {
          /* best-effort cleanup on unmount */
        }
      }
    };
  }, []);

  const setStatus = useCallback((id: string, status: AudioAttachmentStatus) => {
    setAudio((prev) => {
      const idx = prev.findIndex((a) => a.id === id);
      if (idx === -1) return prev;
      const next = [...prev];
      next[idx] = { ...next[idx], status };
      audioRef.current = next;
      return next;
    });
  }, []);

  const full = audio.length >= MAX_AUDIO_PER_MESSAGE;
  return { audio, enqueue, setStatus, remove, clear, full };
}
