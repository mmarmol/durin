/** Colour slots a canvas drawing routine needs, resolved from durin's active
 *  design tokens rather than hardcoded — so a canvas tracks whatever
 *  palette (ithildin/forge/mithril) and light/dark mode is active. */
export interface CanvasTheme {
  text: string;
  textMuted: string;
  line: string;
  surface: string;
  border: string;
  accent: string;
  background: string;
}

/** Used when a token fails to resolve to a concrete colour (e.g. no
 *  stylesheet defining the custom properties is loaded, as in a test
 *  environment). Sane light-mode literals so a canvas still renders
 *  legibly instead of drawing with empty/invalid CSS. */
const FALLBACK: CanvasTheme = {
  text: "#16181a",
  textMuted: "#6b7075",
  line: "#a8adb3",
  surface: "#f4f5f6",
  border: "#d4d6d8",
  accent: "#2b9fd4",
  background: "#ffffff",
};

/** Resolves a durin CSS custom property (e.g. `--foreground`) to a concrete
 *  colour the canvas 2D API can consume directly. A canvas fillStyle can't
 *  resolve `var()` itself, so a hidden probe element asks the browser to do
 *  it: set the property through the token, then read the computed value
 *  back out as a resolved `rgb(...)` string. */
function resolveToken(probe: HTMLElement, token: string, fallback: string): string {
  probe.style.color = `hsl(var(${token}))`;
  return getComputedStyle(probe).color || fallback;
}

/** Reads durin's live token values into the colour slots a canvas needs. */
export function readCanvasTheme(): CanvasTheme {
  const probe = document.createElement("span");
  probe.style.display = "none";
  document.body.appendChild(probe);
  try {
    return {
      text: resolveToken(probe, "--foreground", FALLBACK.text),
      // textMuted and line share a live token (--muted-foreground) but keep
      // distinct fallbacks, so each is resolved independently rather than
      // reused from the other.
      textMuted: resolveToken(probe, "--muted-foreground", FALLBACK.textMuted),
      line: resolveToken(probe, "--muted-foreground", FALLBACK.line),
      surface: resolveToken(probe, "--card", FALLBACK.surface),
      border: resolveToken(probe, "--border", FALLBACK.border),
      // The brand accent is `--primary`; `--accent` in this token set is a
      // muted surface tint (~96% lightness in light mode), not a usable
      // stroke/highlight colour.
      accent: resolveToken(probe, "--primary", FALLBACK.accent),
      background: resolveToken(probe, "--background", FALLBACK.background),
    };
  } finally {
    probe.remove();
  }
}

/** Notifies `cb` whenever the ground truth behind durin's design tokens
 *  changes: the `.dark` class or `data-palette` attribute on the document
 *  root. Returns a disposer that stops the watch. Feature-detects
 *  `MutationObserver` and no-ops if it's unavailable, rather than throwing. */
export function watchTheme(cb: () => void): () => void {
  if (typeof MutationObserver === "undefined") return () => {};
  const observer = new MutationObserver(cb);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class", "data-palette"],
  });
  return () => observer.disconnect();
}
