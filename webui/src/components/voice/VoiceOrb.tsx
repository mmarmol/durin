export type OrbState = "idle" | "listening" | "transcribing" | "thinking" | "speaking" | "error";

const LABEL: Record<OrbState, string> = {
  idle: "Start voice", listening: "Voice: listening", transcribing: "Voice: transcribing",
  thinking: "Voice: thinking", speaking: "Voice: speaking", error: "Voice: error",
};

export function VoiceOrb({ state, amplitude, onToggle }: { state: OrbState; amplitude: number; onToggle: () => void }) {
  const reactive = state === "listening" || state === "speaking";
  // amplitude (0..1) swells the outer ring; clamp so it never explodes.
  const a = Math.min(1, Math.max(0, amplitude));
  const ringR = 26 + (reactive ? a * 8 : 0);
  const ringO = state === "idle" ? 0.18 : 0.3 + (reactive ? a * 0.4 : 0);
  const color = state === "error" ? "var(--danger)" : "var(--accent)";
  const coreO = state === "idle" ? 0.55 : 0.95;

  return (
    <button
      type="button"
      aria-label={LABEL[state]}
      data-state={state}
      onClick={onToggle}
      style={{ background: "transparent", border: "none", padding: 0, cursor: "pointer", lineHeight: 0 }}
    >
      <svg width="56" height="56" viewBox="0 0 56 56" role="img" aria-hidden="true">
        <circle cx="28" cy="28" r="25" fill="none" stroke={color} strokeWidth="2"
                strokeDasharray="1.5 9" opacity={state === "idle" ? 0.18 : 0.34}>
          {state === "thinking" && (
            <animateTransform attributeName="transform" type="rotate" from="0 28 28" to="360 28 28" dur="6s" repeatCount="indefinite" />
          )}
        </circle>
        <circle cx="28" cy="28" r={ringR} fill="none" stroke={color} strokeWidth="1" opacity={ringO} />
        {state === "speaking" && (
          <circle cx="28" cy="28" r="10" fill="none" stroke={color} strokeWidth="1.5" opacity="0.6">
            <animate attributeName="r" values="10;25" dur="1.5s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.6;0" dur="1.5s" repeatCount="indefinite" />
          </circle>
        )}
        <circle cx="28" cy="28" r={7 + (reactive ? a * 2 : 0)} fill={color} opacity={coreO}>
          {state === "idle" && <animate attributeName="opacity" values="0.4;0.62;0.4" dur="4.5s" repeatCount="indefinite" />}
        </circle>
      </svg>
    </button>
  );
}
