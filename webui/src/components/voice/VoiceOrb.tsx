import { useId } from "react";

export type OrbState =
  | "idle"
  | "listening"
  | "transcribing"
  | "thinking"
  | "speaking"
  | "error";

/** The ithildin orb: a glowing, gently morphing sphere that reads its state at a
 *  glance and swells with live audio. A radial gradient gives the 3D sphere; a
 *  blurred halo gives the glow; a turbulence displacement gives the liquid edge
 *  (calm when idle, livelier while listening/speaking). The state word lives in
 *  VoiceDock. Pass onClick to render as a button, omit it for a bare glyph. */
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
  const raw = useId();
  const uid = raw.replace(/:/g, "");
  const reduced =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const reactive = state === "listening" || state === "speaking";
  const a = Math.min(1, Math.max(0, amplitude));
  const c = size / 2;
  // durin's tokens are HSL triples consumed as hsl(var(--x)); --primary is the
  // palette's brand color (ithildin cyan / forge amber / mithril slate). A bare
  // var(--accent) here is both the wrong token (a near-white surface tint) AND
  // invalid as a color (no hsl() wrapper) — which is why the old orb fell back
  // to the dark text color and looked like a flat dot.
  const accent = state === "error" ? "hsl(var(--destructive))" : "hsl(var(--primary))";

  // Live audio swells the whole orb + brightens the halo.
  const scale = reactive ? 1 + a * 0.16 : 1;
  const haloO = (state === "idle" ? 0.22 : 0.4) + (reactive ? a * 0.45 : 0);
  // Liquid edge: bigger displacement + faster boil when active; near-still idle.
  const morph = state === "idle" ? size * 0.045 : size * 0.09 + (reactive ? a * size * 0.05 : 0);
  const boil = state === "speaking" ? "2.4s" : state === "listening" ? "3.2s" : "7s";

  const orbR = size * 0.3;
  const haloR = size * 0.42;

  const glyph = (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={label} style={{ color: accent }}>
      <defs>
        <radialGradient id={`fill-${uid}`} cx="40%" cy="36%" r="68%">
          <stop offset="0%" stopColor="#ffffff" stopOpacity="0.9" />
          <stop offset="38%" stopColor="currentColor" stopOpacity="0.95" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0.35" />
        </radialGradient>
        <radialGradient id={`halo-${uid}`} cx="50%" cy="50%" r="50%">
          <stop offset="55%" stopColor="currentColor" stopOpacity="0.5" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
        </radialGradient>
        <filter id={`glow-${uid}`} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation={size * 0.05} />
        </filter>
        <filter id={`morph-${uid}`} x="-40%" y="-40%" width="180%" height="180%">
          <feTurbulence type="fractalNoise" baseFrequency="0.018" numOctaves="2" seed="4" result="n">
            {!reduced && (
              <animate attributeName="baseFrequency" values="0.014;0.024;0.014" dur={boil} repeatCount="indefinite" />
            )}
          </feTurbulence>
          <feDisplacementMap in="SourceGraphic" in2="n" scale={reduced ? 0 : morph} />
        </filter>
      </defs>

      {/* glow halo */}
      <g transform={`translate(${c} ${c}) scale(${scale})`}>
        <circle r={haloR} fill={`url(#halo-${uid})`} opacity={haloO} filter={`url(#glow-${uid})`} />
      </g>

      {/* morphing sphere */}
      <g transform={`translate(${c} ${c}) scale(${scale})`} filter={`url(#morph-${uid})`}>
        <circle r={orbR} fill={`url(#fill-${uid})`} opacity={state === "idle" ? 0.82 : 1}>
          {!reduced && state === "idle" && (
            <animate attributeName="r" values={`${orbR};${orbR * 1.06};${orbR}`} dur="4.5s" repeatCount="indefinite" />
          )}
        </circle>
      </g>

      {/* thinking: a slow orbiting highlight (no audio to react to) */}
      {state === "thinking" && !reduced && (
        <g transform={`translate(${c} ${c})`}>
          <circle cx={orbR * 0.6} cy={0} r={size * 0.05} fill="#ffffff" opacity="0.7">
            <animateTransform attributeName="transform" type="rotate" from="0 0 0" to="360 0 0" dur="2.4s" repeatCount="indefinite" />
          </circle>
        </g>
      )}

      {/* speaking: an expanding ripple */}
      {state === "speaking" && !reduced && (
        <circle cx={c} cy={c} r={orbR} fill="none" stroke="currentColor" strokeWidth="1" opacity="0.5">
          <animate attributeName="r" values={`${orbR};${haloR * 1.15}`} dur="1.6s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.5;0" dur="1.6s" repeatCount="indefinite" />
        </circle>
      )}
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
