// webui/src/components/rich/MermaidPreview.tsx
import { useEffect, useRef, useState } from "react";

let nextId = 0;

// Content-addressed cache of rendered SVG, keyed by diagram source. The
// transcript remounts message subtrees on streaming/poll updates; without
// this cache each remount re-runs mermaid.render and shows a blank "…"
// placeholder for a frame — the visible flash. Seeding initial state from
// the cache lets a remount paint the finished diagram immediately. Bounded
// so a long session can't grow it without limit.
const svgCache = new Map<string, string>();
const SVG_CACHE_MAX = 60;

function cacheSvg(code: string, svg: string): void {
  if (svgCache.size >= SVG_CACHE_MAX) {
    const oldest = svgCache.keys().next().value;
    if (oldest !== undefined) svgCache.delete(oldest);
  }
  svgCache.set(code, svg);
}

/** Renders Mermaid diagram source to SVG. Mermaid is loaded lazily (this module
 *  is imported via React.lazy from RichBlock) and runs with securityLevel
 *  "strict" so labels cannot inject markup. */
export default function MermaidPreview({ code }: { code: string }) {
  const [svg, setSvg] = useState<string | null>(() => svgCache.get(code) ?? null);
  const [error, setError] = useState(false);
  const idRef = useRef(`mmd-${nextId++}`);

  useEffect(() => {
    const cached = svgCache.get(code);
    if (cached != null) {
      // Cache hit (e.g. a remount): paint the finished diagram, no flash.
      setError(false);
      setSvg(cached);
      return;
    }
    let cancelled = false;
    setError(false);
    setSvg(null);
    void (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
        const { svg: out } = await mermaid.render(idRef.current, code);
        if (!cancelled) {
          cacheSvg(code, out);
          setSvg(out);
        }
      } catch {
        if (!cancelled) setError(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code]);

  if (error) {
    return (
      <div role="alert" className="p-4 text-sm text-destructive">
        Could not render this diagram.
      </div>
    );
  }
  if (svg == null) {
    return <div className="p-4 text-sm text-muted-foreground">…</div>;
  }
  return (
    <div
      className="flex justify-center overflow-x-auto bg-white p-4"
      // Mermaid output with securityLevel "strict" is sanitized SVG.
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
