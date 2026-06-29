export type RichKind = "html" | "svg" | "mermaid" | "chart";

const MAP: Record<string, RichKind> = {
  html: "html",
  svg: "svg",
  mermaid: "mermaid",
  "vega-lite": "chart",
  vega: "chart",
  chart: "chart",
};

/** Map a fenced-block language to a rich render kind, or null when the block
 *  should render as ordinary highlighted code. */
export function richKind(language: string | undefined): RichKind | null {
  if (!language) return null;
  return MAP[language.toLowerCase()] ?? null;
}
