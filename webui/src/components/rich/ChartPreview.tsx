import { useEffect, useRef, useState } from "react";

/** Renders a Vega-Lite JSON spec to a chart via vega-embed (loaded lazily).
 *  Input is declarative JSON — no arbitrary JS executes. */
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

    void (async () => {
      try {
        const embed = (await import("vega-embed")).default;
        if (cancelled || !hostRef.current) return;
        view = await embed(hostRef.current, spec as object, { actions: false });
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
