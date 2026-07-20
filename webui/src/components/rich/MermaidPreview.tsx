// webui/src/components/rich/MermaidPreview.tsx
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

let nextId = 0;

// Content-addressed cache of rendered SVG. The transcript remounts message
// subtrees on streaming/poll updates; without this cache each remount re-runs
// mermaid.render and shows a blank "…" placeholder for a frame — the visible
// flash. Seeding initial state from the cache lets a remount paint the finished
// diagram immediately. Keyed by (theme signature + source) so switching palette
// or light/dark re-renders with the active colours instead of serving a
// stale-coloured SVG. Bounded so a long session can't grow it without limit.
const svgCache = new Map<string, string>();
const SVG_CACHE_MAX = 60;

function cacheSvg(key: string, svg: string): void {
  if (svgCache.size >= SVG_CACHE_MAX) {
    const oldest = svgCache.keys().next().value;
    if (oldest !== undefined) svgCache.delete(oldest);
  }
  svgCache.set(key, svg);
}

/** durin's active palette × mode, read from the ground truth that drives the
 *  CSS custom properties: the `.dark` class and `data-palette` attribute the
 *  theme hook stamps on `document.documentElement`. Reading the document
 *  (rather than a React theme value) keeps this correct however theme state is
 *  plumbed, and gives a stable signature for the render cache. */
function readTheme(): { palette: string; mode: "light" | "dark" } {
  const root = document.documentElement;
  return {
    palette: root.getAttribute("data-palette") ?? "ithildin",
    mode: root.classList.contains("dark") ? "dark" : "light",
  };
}

/** Maps durin's design tokens onto mermaid's `base`-theme variables. Pure: the
 *  caller supplies `resolve`, turning a token name (e.g. `--border`) into a
 *  concrete colour. Every value is a durin token — no colour is hardcoded — so
 *  a diagram tracks whatever palette (ithildin/forge/mithril) and mode
 *  (light/dark) is active. Names are mermaid's own theme-variable names. */
export function buildMermaidThemeVariables(
  resolve: (token: string) => string,
): Record<string, string> {
  const t = resolve;
  return {
    // Canvas behind the diagram — matches the container's surface so the SVG
    // padding is seamless.
    background: t("--background"),
    // Default node ("primary") is a calm neutral surface, not the saturated
    // accent, so nodes read like durin's cards.
    primaryColor: t("--secondary"),
    primaryTextColor: t("--secondary-foreground"),
    primaryBorderColor: t("--border"),
    secondaryColor: t("--muted"),
    secondaryTextColor: t("--foreground"),
    secondaryBorderColor: t("--border"),
    tertiaryColor: t("--accent"),
    tertiaryTextColor: t("--foreground"),
    tertiaryBorderColor: t("--border"),
    // Generic nodes (flowchart, class, state, er, …).
    mainBkg: t("--secondary"),
    nodeBkg: t("--secondary"),
    nodeBorder: t("--border"),
    nodeTextColor: t("--foreground"),
    border2: t("--border"),
    // Edges / links and their labels.
    lineColor: t("--muted-foreground"),
    arrowheadColor: t("--muted-foreground"),
    defaultLinkColor: t("--muted-foreground"),
    textColor: t("--foreground"),
    titleColor: t("--foreground"),
    edgeLabelBackground: t("--muted"),
    // Subgraph clusters.
    clusterBkg: t("--muted"),
    clusterBorder: t("--border"),
    // Notes (sequence and friends).
    noteBkgColor: t("--muted"),
    noteTextColor: t("--foreground"),
    noteBorderColor: t("--border"),
    // Sequence diagrams.
    actorBkg: t("--secondary"),
    actorBorder: t("--border"),
    actorTextColor: t("--foreground"),
    actorLineColor: t("--muted-foreground"),
    signalColor: t("--foreground"),
    signalTextColor: t("--foreground"),
    labelBoxBkgColor: t("--muted"),
    labelBoxBorderColor: t("--border"),
    labelTextColor: t("--foreground"),
    loopTextColor: t("--foreground"),
    activationBkgColor: t("--secondary"),
    activationBorderColor: t("--border"),
    sequenceNumberColor: t("--background"),
  };
}

/** Reads durin's live token values and assembles mermaid's theme config. A
 *  hidden probe lets the browser resolve `hsl(var(--token))` (including the
 *  custom-property indirection) to a concrete `rgb(...)` string, which mermaid's
 *  colour maths can parse — raw `var()` or space-separated hsl would not survive
 *  that library. `fontFamily` matches diagram text to the app. */
function readDurinTheme(): Record<string, string> {
  const probe = document.createElement("span");
  probe.style.display = "none";
  document.body.appendChild(probe);
  try {
    const vars = buildMermaidThemeVariables((token) => {
      probe.style.color = `hsl(var(${token}))`;
      return getComputedStyle(probe).color || `hsl(var(${token}))`;
    });
    const fontFamily = getComputedStyle(document.body).fontFamily;
    if (fontFamily) vars.fontFamily = fontFamily;
    return vars;
  } finally {
    probe.remove();
  }
}

/** Renders Mermaid diagram source to SVG, themed with durin's active palette.
 *  Mermaid is loaded lazily (this module is imported via React.lazy from
 *  RichBlock) and runs with securityLevel "strict" so labels cannot inject
 *  markup. */
export default function MermaidPreview({ code, onRendered }: { code: string; onRendered?: (svg: string) => void }) {
  const { t } = useTranslation();
  const [{ palette, mode }, setTheme] = useState(readTheme);
  const key = `${palette}:${mode} ${code}`;
  const [svg, setSvg] = useState<string | null>(() => svgCache.get(key) ?? null);
  const [error, setError] = useState(false);
  const idRef = useRef(`mmd-${nextId++}`);
  const hostRef = useRef<HTMLDivElement>(null);

  // Re-render when the palette/mode changes. The observer watches the ground
  // truth (the attributes that drive durin's CSS variables), so it fires for
  // any theme switch regardless of which component triggered it.
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(readTheme()));
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-palette"],
    });
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    const cached = svgCache.get(key);
    if (cached != null) {
      // Cache hit (a remount, or a return to a previously-seen theme): paint
      // the finished diagram, no flash.
      setError(false);
      setSvg(cached);
      onRendered?.(cached);
      return;
    }
    let cancelled = false;
    setError(false);
    setSvg(null);
    void (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          suppressErrorRendering: true,
          theme: "base",
          themeVariables: readDurinTheme(),
        });
        const { svg: out } = await mermaid.render(idRef.current, code);
        if (!cancelled) {
          cacheSvg(key, out);
          setSvg(out);
          onRendered?.(out);
        }
      } catch {
        if (!cancelled) setError(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [key]);

  useLayoutEffect(() => {
    const el = hostRef.current?.querySelector("svg");
    if (!el) return;
    const vb = el.getAttribute("viewBox");
    const w = vb ? Number(vb.split(/\s+/)[2]) : NaN;
    if (Number.isFinite(w) && w > 0) el.style.width = `${w}px`;
    el.style.maxWidth = "none";
    el.style.height = "auto";
  }, [svg]);

  if (error) {
    return (
      <div role="alert" className="p-4 text-sm text-destructive">
        {t("rich.errorDiagram")}
      </div>
    );
  }
  if (svg == null) {
    return <div className="p-4 text-sm text-muted-foreground">…</div>;
  }
  return (
    <div
      ref={hostRef}
      className="w-max"
      // Mermaid output with securityLevel "strict" is sanitized SVG.
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
