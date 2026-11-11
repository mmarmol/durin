import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import MarkdownTextRenderer from "@/components/MarkdownTextRenderer";

// The dashboard imports KaTeX's stylesheet itself, while the markup comes from
// the KaTeX copy the markdown plugin resolves. KaTeX renames its layout classes
// between releases, so two copies at different versions mean markup the
// stylesheet has no rules for: formulas render as unstyled, overlapping boxes.
// package.json pins every dependent to the dashboard's own copy; this fails if
// that pin and the direct dependency drift apart.
const stylesheet = readFileSync(
  createRequire(import.meta.url).resolve("katex/dist/katex.min.css"),
  "utf8",
);

function layoutClasses(markdown: string): string[] {
  const { container } = render(<MarkdownTextRenderer>{markdown}</MarkdownTextRenderer>);
  const html = container.querySelector(".katex .katex-html");
  expect(html).not.toBeNull();
  const classes = new Set<string>();
  html!.querySelectorAll("*").forEach((el) => el.classList.forEach((c) => classes.add(c)));
  return [...classes];
}

describe("KaTeX stylesheet and renderer", () => {
  it("styles the layout boxes the renderer emits", () => {
    const classes = layoutClasses("$\\sqrt[3]{x} + \\frac{a}{b}$");
    // The boxes every formula is built from, whatever the release calls them.
    const structural = classes.filter((c) => /(^|-)(base|strut|root)$/.test(c));
    expect(structural.length).toBeGreaterThanOrEqual(3);
    for (const cls of structural) {
      expect(stylesheet, `no rule for .${cls}`).toMatch(new RegExp(`\\.${cls}(?![\\w-])`));
    }
  });
});
