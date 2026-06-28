/** Read the original TeX source KaTeX stores in its MathML annotation. */
export function extractLatex(root: HTMLElement): string | null {
  const annotation = root.querySelector(
    'annotation[encoding="application/x-tex"]',
  );
  const tex = annotation?.textContent?.trim();
  return tex ? tex : null;
}

/** Read the MathML `<math>` element KaTeX renders for accessibility, as markup.
 *  Word and Google Docs accept MathML pasted as an editable equation object. */
export function extractMathML(root: HTMLElement): string | null {
  const math = root.querySelector(".katex-mathml math");
  return math ? math.outerHTML : null;
}
