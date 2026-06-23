export type OrbState =
  | "idle"
  | "listening"
  | "transcribing"
  | "thinking"
  | "speaking"
  | "error";

/** The ithildin orb: an audio-reactive voice indicator whose form reads the
 *  current state at a glance. The visible state word lives in VoiceDock; this is
 *  the glyph alone (with an aria-label for screen readers). Render as a button
 *  by passing onClick, or as a bare glyph (inside a larger control) without it. */
export function VoiceOrb({
  state,
  amplitude,
  size = 72,
  label,
  onClick,
}: {
  state: OrbState;
  amplitude: number;
  size?: number;
  label: string;
  onClick?: () => void;
}) {
  const reactive = state === "listening" || state === "speaking";
  const a = Math.min(1, Math.max(0, amplitude));
  const c = size / 2;
  const color = state === "error" ? "var(--danger)" : "var(--accent)";

  const coreR = size * 0.13 + (reactive ? a * size * 0.06 : 0);
  const coreO = state === "idle" ? 0.5 : 0.95;
  const ringR = size * 0.34 + (reactive ? a * size * 0.13 : 0);
  const ringO = state === "idle" ? 0.16 : 0.28 + (reactive ? a * 0.45 : 0);
  const dashO = state === "idle" ? 0.14 : state === "transcribing" ? 0.5 : 0.32;

  const glyph = (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={label}>
      <circle
        cx={c}
        cy={c}
        r={size * 0.44}
        fill="none"
        stroke={color}
        strokeWidth={size * 0.035}
        strokeDasharray={`${size * 0.03} ${size * 0.16}`}
        strokeLinecap="round"
        opacity={dashO}
      >
        {state === "thinking" && (
          <animateTransform attributeName="transform" type="rotate"
            from={`0 ${c} ${c}`} to={`360 ${c} ${c}`} dur="6s" repeatCount="indefinite" />
        )}
        {state === "transcribing" && (
          <animate attributeName="stroke-dashoffset" values={`0;${size * 0.38}`} dur="1.1s" repeatCount="indefinite" />
        )}
      </circle>

      <circle cx={c} cy={c} r={ringR} fill="none" stroke={color} strokeWidth="1" opacity={ringO} />

      {state === "speaking" && (
        <circle cx={c} cy={c} r={size * 0.18} fill="none" stroke={color} strokeWidth="1.5" opacity="0.55">
          <animate attributeName="r" values={`${size * 0.18};${size * 0.45}`} dur="1.5s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.55;0" dur="1.5s" repeatCount="indefinite" />
        </circle>
      )}

      <circle cx={c} cy={c} r={coreR} fill={color} opacity={coreO}>
        {state === "idle" && (
          <animate attributeName="opacity" values="0.38;0.6;0.38" dur="4.5s" repeatCount="indefinite" />
        )}
      </circle>
    </svg>
  );

  if (!onClick) return glyph;
  return (
    <button
      type="button"
      aria-label={label}
      data-state={state}
      onClick={onClick}
      style={{ background: "transparent", border: "none", padding: 0, cursor: "pointer", lineHeight: 0 }}
    >
      {glyph}
    </button>
  );
}
