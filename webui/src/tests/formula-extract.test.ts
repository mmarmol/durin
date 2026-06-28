import { describe, expect, it } from "vitest";

import { extractLatex, extractMathML } from "@/components/math/formula-extract";

function katexRoot(): HTMLElement {
  const root = document.createElement("span");
  root.className = "katex";
  root.innerHTML =
    '<span class="katex-mathml">' +
    '<math xmlns="http://www.w3.org/1998/Math/MathML">' +
    '<semantics><mrow><mi>E</mi></mrow>' +
    '<annotation encoding="application/x-tex">E = mc^2</annotation>' +
    "</semantics></math></span>" +
    '<span class="katex-html">E</span>';
  return root;
}

describe("formula-extract", () => {
  it("reads the TeX source from the annotation", () => {
    expect(extractLatex(katexRoot())).toBe("E = mc^2");
  });

  it("returns the MathML element markup", () => {
    const ml = extractMathML(katexRoot());
    expect(ml).toContain("<math");
    expect(ml).toContain("<mi>E</mi>");
  });

  it("returns null when there is no katex content", () => {
    const empty = document.createElement("span");
    expect(extractLatex(empty)).toBeNull();
    expect(extractMathML(empty)).toBeNull();
  });
});
