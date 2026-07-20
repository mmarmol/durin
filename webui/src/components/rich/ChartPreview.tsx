import { useEffect, useLayoutEffect, useRef, useState } from "react";

// Content-addressed cache of rendered chart markup (SVG), keyed by spec
// source. The transcript remounts message subtrees on streaming/poll
// updates; without this cache each remount re-runs vega-embed and shows a
// blank host for a frame — the visible flash. On a cache hit a layout
// effect paints the stored SVG synchronously, before the browser shows the
// empty frame. Bounded so a long session can't grow it without limit.
const chartCache = new Map<string, string>();
const CHART_CACHE_MAX = 60;

function cacheChart(code: string, html: string): void {
  if (chartCache.size >= CHART_CACHE_MAX) {
    const oldest = chartCache.keys().next().value;
    if (oldest !== undefined) chartCache.delete(oldest);
  }
  chartCache.set(code, html);
}

/**
 * Returns true if the parsed Vega/Vega-Lite spec object contains any property
 * named "url" anywhere in its object tree.  Vega data sources use `data.url`
 * (and equivalents nested under layer/hconcat/vconcat/concat/facet/spec/datasets)
 * to fetch remote content.  An LLM-generated spec with a remote "url" would
 * cause vega-embed to issue a network request from the authenticated app origin,
 * defeating the non-negotiable network-isolation guarantee.  We reject such specs
 * before calling embed so no network I/O can ever originate here.
 */
function hasRemoteUrl(obj: unknown): boolean {
  if (obj === null || typeof obj !== "object") return false;
  for (const [key, val] of Object.entries(obj as Record<string, unknown>)) {
    if (key === "url" && typeof val === "string") return true;
    if (hasRemoteUrl(val)) return true;
  }
  return false;
}

/** Renders a Vega-Lite JSON spec to a chart via vega-embed (loaded lazily).
 *  Input is declarative JSON — no arbitrary JS executes.
 *  Specs referencing remote URLs are rejected before embed is called. */
export default function ChartPreview({ code, onRendered }: { code: string; onRendered?: (svg: string) => void }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(false);

  // Restore a cached render synchronously on (re)mount so a remount never
  // shows a blank host frame before the async embed below resolves.
  useLayoutEffect(() => {
    const cached = chartCache.get(code);
    if (cached != null && hostRef.current) {
      hostRef.current.innerHTML = cached;
      onRendered?.(cached);
    }
  }, [code, onRendered]);

  useEffect(() => {
    // Already rendered and cached — the layout effect painted it; skip the
    // expensive re-embed entirely.
    if (chartCache.has(code)) return;
    let cancelled = false;
    let view: { finalize: () => void } | null = null;
    setError(false);

    let spec: unknown;
    try {
      spec = JSON.parse(code);
    } catch {
      setError(true);
      return;
    }

    // Defense (a): reject any spec that references a remote URL.
    // LLM-generated specs must not trigger network I/O from the app origin.
    if (hasRemoteUrl(spec)) {
      setError(true);
      return;
    }

    void (async () => {
      try {
        const embedMod = await import("vega-embed");
        const embed = embedMod.default;
        if (cancelled || !hostRef.current) return;

        // Defense (b): pass a loader that refuses HTTP loads so that even if
        // a url reference somehow reaches vega-embed, it cannot fetch remotely.
        let loaderOpt: Record<string, unknown> = {};
        try {
          // vega re-exports vega-loader; construct a loader that blocks http.
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const vegaMod = await import("vega" as string as never) as unknown as { loader?: (...args: unknown[]) => unknown };
          if (typeof vegaMod.loader === "function") {
            const noopLoader = vegaMod.loader();
            // Override http and sanitize to reject all network requests.
            const blockedLoader = {
              ...noopLoader as object,
              http: () => Promise.reject(new Error("Network loads are disabled")),
              sanitize: () => Promise.reject(new Error("Network loads are disabled")),
            };
            loaderOpt = { loader: blockedLoader };
          }
        } catch {
          // vega not directly importable as ESM; defense (a) is sufficient.
        }

        // renderer "svg" (vs the default canvas) so the output is markup we
        // can cache and restore as innerHTML on a later remount.
        view = await embed(hostRef.current, spec as object, {
          actions: false,
          renderer: "svg",
          ...loaderOpt,
        });
        if (!cancelled && hostRef.current) {
          cacheChart(code, hostRef.current.innerHTML);
          onRendered?.(hostRef.current.innerHTML);
        }
      } catch {
        if (!cancelled) setError(true);
      }
    })();

    return () => {
      cancelled = true;
      view?.finalize();
    };
  }, [code]);

  if (error) {
    return (
      <div role="alert" className="p-4 text-sm text-destructive">
        Could not render this chart.
      </div>
    );
  }
  return <div ref={hostRef} className="w-max" />;
}
