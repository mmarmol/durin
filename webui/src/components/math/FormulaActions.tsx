// webui/src/components/math/FormulaActions.tsx
import { useCallback, useRef, useState } from "react";
import { Check, Copy, FileText } from "lucide-react";
import { useTranslation } from "react-i18next";

import { extractLatex, extractMathML } from "@/components/math/formula-extract";
import { cn } from "@/lib/utils";

/** Wraps a rendered KaTeX formula and exposes copy actions read from the live
 *  DOM (LaTeX source + MathML). MathML is written to the clipboard as
 *  ``text/html`` so Word / Google Docs paste it as an editable equation. */
export function FormulaActions({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  const ref = useRef<HTMLSpanElement>(null);
  const [copied, setCopied] = useState<"latex" | "word" | null>(null);

  const flash = useCallback((which: "latex" | "word") => {
    setCopied(which);
    setTimeout(() => setCopied(null), 1_500);
  }, []);

  const copyLatex = useCallback(() => {
    const root = ref.current;
    if (!root || !navigator.clipboard) return;
    const tex = extractLatex(root);
    if (tex) navigator.clipboard.writeText(tex).then(() => flash("latex"));
  }, [flash]);

  const copyWord = useCallback(() => {
    const root = ref.current;
    if (!root || !navigator.clipboard?.write) return;
    const mathml = extractMathML(root);
    const tex = extractLatex(root) ?? "";
    if (!mathml) return;
    const item = new ClipboardItem({
      "text/html": new Blob([mathml], { type: "text/html" }),
      "text/plain": new Blob([tex], { type: "text/plain" }),
    });
    navigator.clipboard.write([item]).then(() => flash("word"));
  }, [flash]);

  return (
    <span ref={ref} className="group/formula relative inline-flex items-center">
      {children}
      <span
        className={cn(
          "absolute -top-2 right-0 z-10 hidden gap-1 rounded-md border border-border/60",
          "bg-popover px-1 py-0.5 shadow-sm group-hover/formula:flex",
        )}
        role="group"
        aria-label={t("formula.actionsAria")}
      >
        <button
          type="button"
          onClick={copyLatex}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs hover:bg-muted"
          aria-label={t("formula.copyLatex")}
        >
          {copied === "latex" ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          {t("formula.copyLatex")}
        </button>
        <button
          type="button"
          onClick={copyWord}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs hover:bg-muted"
          aria-label={t("formula.copyWord")}
        >
          {copied === "word" ? <Check className="h-3 w-3" /> : <FileText className="h-3 w-3" />}
          {t("formula.copyWord")}
        </button>
      </span>
    </span>
  );
}
