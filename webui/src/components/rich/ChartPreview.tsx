import { useEffect, useRef, useState } from "react";

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
export default function ChartPreview({ code }: { code: string }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
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

        view = await embed(hostRef.current, spec as object, { actions: false, ...loaderOpt });
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
  return <div ref={hostRef} className="overflow-x-auto bg-white p-4" />;
}
