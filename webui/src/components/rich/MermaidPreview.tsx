// webui/src/components/rich/MermaidPreview.tsx
import { useEffect, useRef, useState } from "react";

let nextId = 0;

/** Renders Mermaid diagram source to SVG. Mermaid is loaded lazily (this module
 *  is imported via React.lazy from RichBlock) and runs with securityLevel
 *  "strict" so labels cannot inject markup. */
export default function MermaidPreview({ code }: { code: string }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const idRef = useRef(`mmd-${nextId++}`);

  useEffect(() => {
    let cancelled = false;
    setError(false);
    setSvg(null);
    void (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
        const { svg: out } = await mermaid.render(idRef.current, code);
        if (!cancelled) setSvg(out);
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
