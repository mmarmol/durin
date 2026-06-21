/** Pick the best audio MIME this browser can record with MediaRecorder.
 *
 * Preference order (spec §4.6):
 *   1. audio/webm;codecs=opus  (Chrome, Firefox — smaller, broadly accepted)
 *   2. audio/webm               (webm without explicit codec)
 *   3. audio/mp4                (Safari fallback)
 *
 * Returns "" when MediaRecorder is unavailable or no candidate is supported —
 * callers should hide the mic button in that case.
 */
export function pickAudioMime(): string {
  if (typeof MediaRecorder === "undefined") return "";
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
  ];
  for (const c of candidates) {
    if (MediaRecorder.isTypeSupported(c)) return c;
  }
  return "";
}
