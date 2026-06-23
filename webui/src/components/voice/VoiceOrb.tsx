import "./orb.css";

export type OrbState =
  | "idle"
  | "listening"
  | "transcribing"
  | "thinking"
  | "speaking"
  | "error";

/** The ithildin voice orb: a solid, glowing accent sphere (visible on any
 *  background) with a bright light that sweeps around its edge
 *  (Apple-Intelligence-style) and a soft glow halo. One chromatic only —
 *  durin's restraint — so everything is the brand accent (hsl(var(--primary)),
 *  or --destructive on error) plus white specular light. The sweep quickens by
 *  state (idle → speaking); the sphere swells + the halo brightens with live
 *  audio while listening/speaking. Honors prefers-reduced-motion (orb.css).
 *  Pass onClick for a button, omit it for a bare glyph in a larger control. */
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
  const a = Math.min(1, Math.max(0, amplitude));
  const reactive = state === "listening" || state === "speaking";
  const tok = state === "error" ? "--destructive" : "--primary";

  const spinDur =
    state === "speaking" ? "2.2s"
    : state === "transcribing" ? "1.4s"
    : state === "thinking" ? "3s"
    : state === "listening" ? "4.4s"
    : "8s";

  // Live audio swells the sphere and brightens the glow.
  const scale = reactive ? 1 + a * 0.14 : 1;
  const haloOpacity = (state === "idle" ? 0.55 : 0.8) + (reactive ? a * 0.2 : 0);
  const sweepOpacity = state === "idle" ? 0.7 : 0.95;

  // A solid, glossy sphere of the accent — bright off-center highlight so it
  // reads as a lit object, not a flat disc.
  const sphere =
    `radial-gradient(circle at 36% 32%,` +
    ` hsl(0 0% 100% / 0.95) 0%,` +
    ` hsl(var(${tok})) 42%,` +
    ` hsl(var(${tok}) / 0.92) 76%,` +
    ` hsl(var(${tok}) / 0.7) 100%)`;
  // A bright arc that, masked to the rim and rotated, travels around the edge.
  const sweep =
    `conic-gradient(from 0deg,` +
    ` transparent 0deg,` +
    ` hsl(var(${tok}) / 0.6) 60deg,` +
    ` hsl(0 0% 100% / 0.95) 96deg,` +
    ` hsl(var(${tok}) / 0.6) 132deg,` +
    ` transparent 200deg,` +
    ` transparent 360deg)`;
  const ringMask = "radial-gradient(circle, transparent 60%, #000 73%)";

  const inner = (extra: Record<string, unknown>) => (
    <div style={{ position: "relative", width: size, height: size, lineHeight: 0 }} {...extra}>
      {/* soft glow halo */}
      <div
        style={{
          position: "absolute",
          inset: -size * 0.12,
          borderRadius: "50%",
          background: `radial-gradient(circle, hsl(var(${tok}) / 0.55), transparent 68%)`,
          filter: `blur(${size * 0.08}px)`,
          opacity: haloOpacity,
          transform: `scale(${scale})`,
        }}
      />
      {/* solid sphere */}
      <div
        style={{
          position: "absolute",
          inset: size * 0.12,
          borderRadius: "50%",
          background: sphere,
          boxShadow: `0 0 ${size * 0.18}px hsl(var(${tok}) / 0.5)`,
          transform: `scale(${scale})`,
        }}
      />
      {/* edge sweep (the travelling light) */}
      <div
        className="durin-orb-rim"
        style={{
          position: "absolute",
          inset: size * 0.06,
          borderRadius: "50%",
          background: sweep,
          opacity: sweepOpacity,
          mixBlendMode: "screen",
          WebkitMaskImage: ringMask,
          maskImage: ringMask,
          animationDuration: spinDur,
        }}
      />
      {/* idle: a gentle breathing wash so it feels alive at rest */}
      {state === "idle" && (
        <div
          className="durin-orb-breathe"
          style={{
            position: "absolute",
            inset: size * 0.12,
            borderRadius: "50%",
            background: `radial-gradient(circle, hsl(0 0% 100% / 0.5), transparent 60%)`,
          }}
        />
      )}
    </div>
  );

  if (!onClick) return inner({ role: "img", "aria-label": label, "data-state": state });
  return (
    <button
      type="button"
      aria-label={label}
      data-state={state}
      onClick={onClick}
      style={{ background: "transparent", border: "none", padding: 0, cursor: "pointer", lineHeight: 0 }}
    >
      {inner({})}
    </button>
  );
}
